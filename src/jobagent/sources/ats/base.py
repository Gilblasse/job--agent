"""Fan-out across employer ATS boards.

No ATS offers cross-company search -- Lever says so in its own documentation, and the
rest simply have no search parameter. Discovery is therefore fan-out over a registry of
known boards, one request per tenant, which makes two things load-bearing: the registry's
size, and not wasting requests on boards that are dead or rate-limiting us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ...domain.models import AuthorityTier, RawPosting, SourceReport, SourceStatus
from ...ports import DiscoveryRequest, DiscoveryResult, Fetcher, FetchError
from ..base import SourceAdapter, dedupe_postings

# Search terms reach an adapter joined by a unit separator, a control character no
# legitimate job title contains.
TERM_SEPARATOR = "\x1f"


@dataclass
class BoardTarget:
    """One company's board on one platform."""

    company: str
    token: str
    registry_id: int | None = None
    board_url: str = ""
    domain: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class BoardOutcome:
    """Per-board result, so registry health can be updated without guessing."""

    target: BoardTarget
    ok: bool
    count: int = 0
    failure_kind: str = ""


@dataclass
class AtsAdapter(SourceAdapter):
    """Shared fan-out loop. Subclasses supply the URL and the parser."""

    name: str = "ats"
    probe_tokens: tuple[str, ...] = ()
    # A board costs many requests: list pages plus one per description.
    costly: bool = False
    # The platform accepts the search terms, so they are pushed down to it.
    server_search: bool = False

    # -- subclass hooks -----------------------------------------------------------

    def fetch_board(
        self, fetcher: Fetcher, target: BoardTarget, today: date | None = None
    ) -> list[RawPosting]:
        """Read one board. ``today`` is the run's date, for relative-date parsing."""
        raise NotImplementedError

    # -- the loop -----------------------------------------------------------------

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        started = self._timer()
        fetcher = request.fetcher
        if fetcher is None:
            return DiscoveryResult(
                report=self._report(SourceStatus.SKIPPED, note="no fetcher supplied")
            )

        targets = [t for t in request.targets if isinstance(t, BoardTarget)]
        if not targets:
            return DiscoveryResult(
                report=self._report(
                    SourceStatus.SKIPPED, started=started,
                    note="no registered boards — run: jobagent company seed",
                )
            )

        postings: list[RawPosting] = []
        outcomes: list[BoardOutcome] = []
        blocked = False
        failures = 0
        meter = _meter_for(fetcher)

        # The budget is a REQUEST allowance, not a board count. A Workday board can issue
        # three list calls plus twenty detail calls, so treating each board as one unit
        # let a budget of 400 become thousands of requests and made the busiest platform
        # the likeliest to be throttled.
        attempted = targets[: request.budget]
        skipped_for_budget = len(targets) - len(attempted)
        for target in attempted:
            if meter is not None and meter.used >= request.budget:
                # Stop on the allowance, not on the board count.
                skipped_for_budget += len(attempted) - len(outcomes)
                break
            if blocked:
                # Skipped, not read: no unit of work happened, so no progress is reported
                # for it. The orchestrator's display completes the remainder when the
                # source finishes.
                outcomes.append(BoardOutcome(target, ok=False, failure_kind="host_blocked"))
                continue

            outcome, found = self._read_board(fetcher, target, request.today)
            outcomes.append(outcome)
            postings.extend(found)
            if outcome.failure_kind in ("rate_limited", "blocked"):
                blocked = True
            elif not outcome.ok:
                failures += 1
            # Outside the read, so a fault in a progress display is never recorded
            # against the board as a parse error.
            if request.on_unit is not None:
                request.on_unit(len(found))

        requests = meter.used if meter is not None else 0
        searched = sum(1 for o in outcomes if o.ok)
        def boards(count: int) -> str:
            return f"{count} board" if count == 1 else f"{count} boards"

        if blocked:
            status = SourceStatus.BLOCKED
            note = f"host stopped serving after {searched}/{len(targets)} boards"
        elif failures and searched:
            status = SourceStatus.PARTIAL
            note = f"{searched}/{len(targets)} boards read, {failures} unavailable"
        elif failures:
            status = SourceStatus.UNAVAILABLE
            note = f"all {boards(failures)} unavailable"
        else:
            status = SourceStatus.OK
            note = f"{boards(searched)} read"

        if skipped_for_budget:
            # Saying "200 boards read" while silently skipping 400 more overstates the
            # coverage of the run.
            note += (
                f"; {skipped_for_budget} more not reached within this run's budget"
            )

        result = DiscoveryResult(
            postings=dedupe_postings(postings),
            report=self._report(
                status, found=len(postings), requests=requests, started=started, note=note
            ),
        )
        result.report.__dict__["outcomes"] = outcomes  # consumed by the orchestrator
        return result

    def _read_board(
        self, fetcher: Fetcher, target: BoardTarget, today: date | None
    ) -> tuple[BoardOutcome, list[RawPosting]]:
        """One board, with every way it can fail turned into an outcome."""
        try:
            found = self.fetch_board(fetcher, target, today)
        except FetchError as error:
            if error.blocked:
                # The host has stopped serving us. Every remaining board on this
                # platform lives behind the same hostname, so continuing would be
                # both futile and rude.
                kind = "rate_limited" if error.status == 429 else "blocked"
            else:
                # 404 means the tenant is gone and should count against the board;
                # a 5xx is the server having a bad day and should not.
                kind = "gone" if error.status in (404, 410) else "unavailable"
            return BoardOutcome(target, ok=False, failure_kind=kind), []
        except Exception:  # noqa: BLE001 - a malformed board must not end the run
            return BoardOutcome(target, ok=False, failure_kind="parse_error"), []
        # Empty is not failure: a real board with nothing open right now.
        return BoardOutcome(target, ok=True, count=len(found)), list(found or [])

    def probe_targets(self) -> list[BoardTarget]:
        """Reference boards used by the go/no-go gate.

        Expressed as targets rather than names so platforms with composite tenancy can
        supply the whole identity.
        """
        return [BoardTarget(company=token, token=token) for token in self.probe_tokens]

    def check(self, fetcher: Fetcher) -> SourceReport:  # type: ignore[override]
        """Probe reference boards until one returns actual postings."""
        started = self._timer()
        meter = _meter_for(fetcher)
        errors: list[str] = []

        for target in self.probe_targets():
            token = target.token
            try:
                postings = self.fetch_board(fetcher, target)
            except FetchError as error:
                status, note = self.classify_failure(error)
                if status is SourceStatus.BLOCKED:
                    return self._report(
                        status, requests=_used(meter), started=started, note=note
                    )
                errors.append(f"{token}: {note}")
                continue
            except Exception as error:  # noqa: BLE001
                errors.append(f"{token}: {type(error).__name__}: {error}")
                continue

            if postings:
                return self._report(
                    SourceStatus.OK, found=len(postings),
                    requests=_used(meter), started=started,
                    note=f"probe tenant {token!r} returned {len(postings)} postings",
                )
            errors.append(f"{token}: reachable but returned no postings")

        return self._report(
            SourceStatus.UNAVAILABLE, requests=_used(meter), started=started,
            note="; ".join(errors) or "no probe tenants configured",
        )


def require_ok(response: Any, what: str) -> Any:
    """Raise unless the response actually succeeded.

    Adapters used to treat any non-2xx as "this board has nothing open", which made a
    dead tenant, a server error and a bad API key all look like a healthy, empty board.
    The user saw "No matches" over a coverage table claiming every source was searched,
    and the registry reset the board's failure count on each 404 so it was never evicted.

    Status is mapped to a cause so the caller can tell "stop asking" from "try later":

    - 401/403 -> blocked for this run; retrying a refusal is how a polite client stops
      being one
    - 404/410 -> this board is gone, and it counts against the board
    - 5xx     -> the server is unwell; not the board's fault
    """
    status = getattr(response, "status", 0)
    if 200 <= status < 300:
        return response
    if status in (401, 403):
        raise FetchError(
            f"{what}: HTTP {status} (refused; check credentials or access)",
            blocked=True, status=status,
        )
    if status in (404, 410):
        raise FetchError(f"{what}: HTTP {status} (board not found)", status=status)
    if status == 429:
        raise FetchError(f"{what}: HTTP 429 (rate limited)", blocked=True, status=429)
    raise FetchError(f"{what}: HTTP {status}", status=status)


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def ats_authority(url: str, domain: str | None = None) -> AuthorityTier:
    from ...domain.dedup import url_authority

    return url_authority(url, domain)


def _meter_for(fetcher: Fetcher):
    """A per-source request counter, when the fetcher provides one.

    Test doubles need not implement it, so callers tolerate None.
    """
    usage = getattr(fetcher, "usage", None)
    return usage() if callable(usage) else None


def _used(meter) -> int:
    return meter.used if meter is not None else 0
