"""The seams between the pure domain and the outside world.

Every capability the engine needs from its environment -- network, clock, database -- is
declared here as a Protocol. Nothing in ``domain/`` or ``engine/`` imports httpx or
sqlite3 directly.

This is what makes the whole thing testable offline: the test suite supplies a fetcher
that replays recorded fixtures, and the engine cannot tell the difference. It is also
what keeps the door open to running the same engine in a serverless function against a
cloud database, without rewriting the search logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from .domain.models import Coverage, Job, MatchResult, RawPosting, SourceReport


class FetchError(Exception):
    """A request could not be completed."""

    def __init__(self, message: str, *, blocked: bool = False, status: int | None = None):
        super().__init__(message)
        self.blocked = blocked
        self.status = status


@dataclass
class FetchResponse:
    """A completed HTTP response, reduced to what adapters actually use."""

    url: str
    status: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        import json

        return json.loads(self.text) if self.text else None


@runtime_checkable
class Fetcher(Protocol):
    """The single point of network egress.

    Adapters receive one of these and have no other way to reach the network. Robots
    policy, rate limiting, backoff and the user agent are therefore enforced in one place
    rather than trusted to fourteen adapters to remember.
    """

    def get(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse: ...

    def post_json(
        self, url: str, *, payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> FetchResponse: ...

    @property
    def requests_made(self) -> int: ...


@runtime_checkable
class Clock(Protocol):
    """Time, injected so that freshness logic is testable."""

    def now(self) -> datetime: ...
    def today(self) -> date: ...


@runtime_checkable
class SourceAdapter(Protocol):
    """One place jobs can be discovered.

    An adapter maps its platform's shape onto ``RawPosting`` and reports honestly what
    happened. It must never raise for an ordinary failure: a dead board is a
    ``SourceReport`` with a status, because one broken source must not fail a whole run.
    """

    name: str

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult: ...

    def check(self, fetcher: Fetcher) -> SourceReport:
        """Probe for the go/no-go gate: is this source reachable and returning data?"""
        ...


@dataclass
class DiscoveryRequest:
    """What the engine asks a source for.

    ``terms`` are the positive side of the spec only. Exclusions are never passed to a
    source: filtering happens after retrieval so that a job is only ever dropped by a
    rule the user can see, not by a query that failed to ask for it.
    """

    terms: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=lambda: ["US"])
    locations: list[str] = field(default_factory=list)
    fetcher: Fetcher | None = None
    budget: int = 100
    since: date | None = None
    today: date | None = None  # injected so freshness logic is testable
    targets: list[Any] = field(default_factory=list)  # registry entries, for ATS fan-out
    # Called once per unit of work finished -- a board read, a search request answered --
    # with the number of postings it yielded. For progress display only; adapters must
    # not depend on it being set.
    on_unit: Callable[[int], None] | None = None


@dataclass
class DiscoveryResult:
    """What a source returned, plus what happened while getting it."""

    postings: list[RawPosting] = field(default_factory=list)
    report: SourceReport | None = None


@runtime_checkable
class JobRepository(Protocol):
    """Persistence for searches, runs, jobs, matches and user status."""

    def save_spec(self, name: str, spec_yaml: str) -> int: ...
    def get_spec(self, name: str) -> tuple[int, str] | None: ...
    def list_specs(self) -> list[tuple[int, str, str]]: ...
    def delete_spec(self, name: str) -> bool: ...

    def start_run(self, search_id: int, started: datetime) -> int: ...
    def finish_run(self, run_id: int, finished: datetime, coverage: Coverage) -> None: ...

    def upsert_job(self, job: Job, seen: datetime) -> tuple[int, bool]: ...
    def record_match(
        self, job_id: int, search_id: int, run_id: int, result: MatchResult, is_new: bool
    ) -> None: ...
    def set_user_status(self, job_id: int, status: str, note: str = "") -> None: ...


@runtime_checkable
class RunProgress(Protocol):
    """How a run reports what it is doing while it is doing it.

    Called from the source worker threads as well as the main thread, so an
    implementation must be thread-safe and must not touch the store.
    """

    def discovery_started(self, units: dict[str, int]) -> None:
        """Fan-out is about to begin; ``units`` is each source's planned unit count."""

    def unit_done(self, source: str, found: int) -> None:
        """One unit of a source's work finished, yielding ``found`` postings."""

    def source_done(self, source: str, report: SourceReport) -> None:
        """A source finished, whatever its outcome."""

    def evaluation_started(self, total: int) -> None:
        """Every source has returned; ``total`` jobs are about to be judged."""

    def job_evaluated(self, matched: bool, is_new: bool) -> None:
        """One job judged."""


class NullProgress:
    """The default: a run that reports nothing, for scripts and tests."""

    def discovery_started(self, units: dict[str, int]) -> None:
        pass

    def unit_done(self, source: str, found: int) -> None:
        pass

    def source_done(self, source: str, report: SourceReport) -> None:
        pass

    def evaluation_started(self, total: int) -> None:
        pass

    def job_evaluated(self, matched: bool, is_new: bool) -> None:
        pass
