"""Fan-out across employer ATS boards.

No ATS offers cross-company search -- Lever says so in its own documentation, and the
rest simply have no search parameter. Discovery is therefore fan-out over a registry of
known boards, one request per tenant, which makes two things load-bearing: the registry's
size, and not wasting requests on boards that are dead or rate-limiting us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...domain.models import AuthorityTier, RawPosting, SourceReport, SourceStatus
from ...ports import DiscoveryRequest, DiscoveryResult, Fetcher, FetchError
from ..base import SourceAdapter, dedupe_postings


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

    # -- subclass hooks -----------------------------------------------------------

    def fetch_board(self, fetcher: Fetcher, target: BoardTarget) -> list[RawPosting]:
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
                    note="no registered boards for this platform",
                )
            )

        postings: list[RawPosting] = []
        outcomes: list[BoardOutcome] = []
        blocked = False
        failures = 0
        before = fetcher.requests_made

        for target in targets[: request.budget]:
            if blocked:
                outcomes.append(BoardOutcome(target, ok=False, failure_kind="host_blocked"))
                continue
            try:
                found = self.fetch_board(fetcher, target)
            except FetchError as error:
                if error.blocked:
                    # The host has stopped serving us. Every remaining board on this
                    # platform lives behind the same hostname, so continuing would be
                    # both futile and rude.
                    blocked = True
                    kind = "rate_limited" if error.status == 429 else "blocked"
                    outcomes.append(BoardOutcome(target, ok=False, failure_kind=kind))
                else:
                    failures += 1
                    outcomes.append(BoardOutcome(target, ok=False, failure_kind="unavailable"))
                continue
            except Exception:  # noqa: BLE001 - a malformed board must not end the run
                failures += 1
                outcomes.append(BoardOutcome(target, ok=False, failure_kind="parse_error"))
                continue

            if found:
                postings.extend(found)
                outcomes.append(BoardOutcome(target, ok=True, count=len(found)))
            else:
                # Empty is not failure: a real board with nothing open right now.
                outcomes.append(BoardOutcome(target, ok=True, count=0))

        requests = fetcher.requests_made - before
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

        result = DiscoveryResult(
            postings=dedupe_postings(postings),
            report=self._report(
                status, found=len(postings), requests=requests, started=started, note=note
            ),
        )
        result.report.__dict__["outcomes"] = outcomes  # consumed by the orchestrator
        return result

    def probe_targets(self) -> list[BoardTarget]:
        """Reference boards used by the go/no-go gate.

        Expressed as targets rather than names so platforms with composite tenancy can
        supply the whole identity.
        """
        return [BoardTarget(company=token, token=token) for token in self.probe_tokens]

    def check(self, fetcher: Fetcher) -> SourceReport:  # type: ignore[override]
        """Probe reference boards until one returns actual postings."""
        started = self._timer()
        before = fetcher.requests_made
        errors: list[str] = []

        for target in self.probe_targets():
            token = target.token
            try:
                postings = self.fetch_board(fetcher, target)
            except FetchError as error:
                status, note = self.classify_failure(error)
                if status is SourceStatus.BLOCKED:
                    return self._report(
                        status, requests=fetcher.requests_made - before, started=started, note=note
                    )
                errors.append(f"{token}: {note}")
                continue
            except Exception as error:  # noqa: BLE001
                errors.append(f"{token}: {type(error).__name__}: {error}")
                continue

            if postings:
                return self._report(
                    SourceStatus.OK, found=len(postings),
                    requests=fetcher.requests_made - before, started=started,
                    note=f"probe tenant {token!r} returned {len(postings)} postings",
                )
            errors.append(f"{token}: reachable but returned no postings")

        return self._report(
            SourceStatus.UNAVAILABLE, requests=fetcher.requests_made - before, started=started,
            note="; ".join(errors) or "no probe tenants configured",
        )


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def ats_authority(url: str, domain: str | None = None) -> AuthorityTier:
    from ...domain.dedup import url_authority

    return url_authority(url, domain)
