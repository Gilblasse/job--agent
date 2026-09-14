"""Core domain types.

Pure data: no I/O, no network, no database. Everything here is safe to construct in a
unit test and safe to reason about without a running system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, StrEnum


class WorkplaceType(StrEnum):
    """Where the work physically happens.

    UNKNOWN is a first-class value, not a failure. Most job sources do not state this
    reliably, and silently guessing ONSITE would make the remote gate lie.
    """

    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class AuthorityTier(int, Enum):
    """How authoritative a URL is for applying to a job.

    Ordered so that a plain ``max()`` picks the best record in a duplicate cluster.
    """

    UNVERIFIED = 1
    OFFICIAL_ATS = 2
    EMPLOYER_SITE = 3


class SourceStatus(StrEnum):
    """Outcome of querying one source during one run.

    Recorded per run so coverage can be reported honestly: a search that reached four of
    seven sources says so, rather than presenting partial results as complete.
    """

    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class UserStatus(StrEnum):
    """Where the user stands with a job."""

    NEW = "new"
    SEEN = "seen"
    SAVED = "saved"
    APPLIED = "applied"
    REJECTED = "rejected"
    CLOSED = "closed"


class VerificationState(StrEnum):
    """Whether we have confirmed the posting still exists.

    UNVERIFIED means we have not checked, which is different from GONE. Conflating them
    would present an unchecked job as a dead one.
    """

    UNVERIFIED = "unverified"
    LIVE = "live"
    GONE = "gone"


class Decision(StrEnum):
    """What the matching engine concluded about a job for a given search."""

    MATCH = "match"
    REJECTED = "rejected"


class GateOutcome(StrEnum):
    """Result of evaluating one hard gate against one job.

    UNVERIFIABLE is deliberately distinct from FAIL: the posting did not supply the
    information the gate needs. Treating that as a failure silently discards jobs whose
    only flaw is a terse description.
    """

    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class SalaryRange:
    """A compensation range as advertised.

    ``period`` matters outside tech, where hourly rates are the norm; comparing an hourly
    figure against an annual minimum without it produces nonsense.
    """

    minimum: float | None = None
    maximum: float | None = None
    currency: str = "USD"
    period: str = "year"  # year | month | week | day | hour

    def annualized(self) -> tuple[float | None, float | None]:
        """Convert to approximate annual figures for comparison.

        Uses 2080 working hours a year. This is an estimate for ranking, not a contract.
        """
        factors = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}
        factor = factors.get(self.period, 1)
        lo = self.minimum * factor if self.minimum is not None else None
        hi = self.maximum * factor if self.maximum is not None else None
        return lo, hi


@dataclass(frozen=True)
class Location:
    """A normalized work location.

    ``raw`` is always kept: location strings are wildly inconsistent across sources, and
    the original text is the evidence a user needs when a location gate rejects a job.
    """

    raw: str = ""
    city: str | None = None
    region: str | None = None  # state / province
    country: str | None = None  # ISO 3166-1 alpha-2, uppercase

    def display(self) -> str:
        parts = [p for p in (self.city, self.region, self.country) if p]
        return ", ".join(parts) if parts else (self.raw or "Unspecified")


@dataclass
class RawPosting:
    """What a source adapter emits, before normalization.

    Adapters do as little interpretation as possible: they map their platform's field
    names onto this shape and leave judgement to the domain layer. That keeps
    platform quirks out of the matching logic.
    """

    source: str
    external_id: str
    title: str
    company: str
    url: str
    description_html: str = ""
    description_text: str = ""
    location_raw: str = ""
    country_hint: str | None = None
    workplace_hint: WorkplaceType = WorkplaceType.UNKNOWN
    employment_type: str | None = None
    department: str | None = None
    salary: SalaryRange | None = None
    posted_at: date | None = None
    apply_url: str | None = None
    authority: AuthorityTier = AuthorityTier.UNVERIFIED
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class JobSourceRef:
    """One place a job was seen. A job may have several."""

    source: str
    url: str
    external_id: str = ""
    authority: AuthorityTier = AuthorityTier.UNVERIFIED
    first_seen: datetime | None = None
    last_seen: datetime | None = None


@dataclass
class Job:
    """A normalized, deduplicated opportunity: the canonical record.

    ``identity`` is the dedup key; ``sources`` keeps every URL the job was seen at so
    provenance survives deduplication.
    """

    identity: str
    title: str
    company: str
    url: str
    description_text: str = ""
    location: Location = field(default_factory=Location)
    workplace: WorkplaceType = WorkplaceType.UNKNOWN
    employment_type: str | None = None
    department: str | None = None
    salary: SalaryRange | None = None
    posted_at: date | None = None
    authority: AuthorityTier = AuthorityTier.UNVERIFIED
    sources: list[JobSourceRef] = field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    verification: VerificationState = VerificationState.UNVERIFIED
    verified_at: datetime | None = None

    def searchable_text(self) -> str:
        """Everything a phrase gate should look at, in its original casing.

        Deliberately not lowercased. Every matcher here is already case-insensitive, and
        folding the case would only degrade the evidence quoted back to the user: a
        rejection reading "matched 'cpa required'" looks like a bug next to the posting
        it came from.
        """
        parts = [self.title, self.company, self.description_text, self.location.raw]
        return "\n".join(p for p in parts if p)


@dataclass(frozen=True)
class GateResult:
    """Why one hard gate passed, failed, or could not be decided.

    ``evidence`` quotes the text that triggered the outcome. Without it a rejection is an
    assertion the user has to take on faith.
    """

    gate: str
    outcome: GateOutcome
    rule: str
    evidence: str = ""
    detail: str = ""

    def describe(self) -> str:
        base = f"{self.gate}: {self.outcome.value} ({self.rule})"
        if self.evidence:
            base += f" — matched {self.evidence!r}"
        if self.detail:
            base += f" — {self.detail}"
        return base


@dataclass(frozen=True)
class Signal:
    """One contribution to a job's rank, with the evidence behind it.

    The ranking score is the sum of these. Showing the ledger is what makes "why did this
    rank highly" answerable without reading the code.
    """

    name: str
    points: float
    evidence: str = ""

    def describe(self) -> str:
        sign = "+" if self.points >= 0 else ""
        text = f"{sign}{self.points:g} {self.name}"
        if self.evidence:
            text += f" ({self.evidence})"
        return text


@dataclass
class MatchResult:
    """The engine's full, explainable verdict on one job for one search."""

    decision: Decision
    score: float = 0.0
    gates: list[GateResult] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)

    @property
    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.outcome is GateOutcome.FAIL]

    @property
    def unverifiable_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.outcome is GateOutcome.UNVERIFIABLE]

    @property
    def is_uncertain(self) -> bool:
        """True when the job matched but some requirement could not be confirmed."""
        return self.decision is Decision.MATCH and bool(self.unverifiable_gates)

    def rejection_reason(self) -> str:
        """A one-line answer to "why was this rejected?"."""
        failed = self.failed_gates
        if not failed:
            return ""
        return "; ".join(g.describe() for g in failed)


@dataclass
class SourceReport:
    """What happened with one source during one run.

    ``skipped_cached`` distinguishes "nothing changed since last run" from "found
    nothing", which otherwise look identical in a coverage table.
    """

    source: str
    status: SourceStatus
    found: int = 0
    requests: int = 0
    skipped_cached: int = 0
    duration_ms: int = 0
    note: str = ""


@dataclass
class Coverage:
    """Per-run source coverage: the honest answer to "what did you actually search?"."""

    reports: list[SourceReport] = field(default_factory=list)

    def add(self, report: SourceReport) -> None:
        self.reports.append(report)

    @property
    def searched(self) -> list[SourceReport]:
        return [r for r in self.reports if r.status in (SourceStatus.OK, SourceStatus.PARTIAL)]

    @property
    def degraded(self) -> list[SourceReport]:
        return [r for r in self.reports if r.status not in (SourceStatus.OK, SourceStatus.SKIPPED)]

    def summary(self) -> str:
        ok = len([r for r in self.reports if r.status is SourceStatus.OK])
        return f"{ok}/{len(self.reports)} sources fully searched"
