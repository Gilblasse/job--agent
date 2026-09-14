"""Deciding whether a job matches, and explaining the answer.

Two stages that are never allowed to merge. Hard gates decide admission and cannot be
outvoted by a good score. Ranking then orders what survived, as a ledger of named
contributions rather than one opaque number, so "why is this at the top" and "why was
this thrown out" both have real answers.
"""

from __future__ import annotations

from datetime import date

from .gates import Gate, SalaryFloorGate, UnverifiablePolicy
from .models import (
    AuthorityTier,
    Decision,
    GateOutcome,
    GateResult,
    Job,
    MatchResult,
    Signal,
)
from .spec import SearchSpec, compile_gates
from .taxonomy import Taxonomy
from .text import first_matching_phrase

# Ceilings on how much repeated evidence of one kind can contribute. Without them a
# posting that says "Python" nine times outranks a better-matched job that says it twice,
# which measures verbosity rather than fit.
MAX_KEYWORD_HITS = 5
MAX_RESPONSIBILITY_HITS = 4
MAX_SKILL_HITS = 5


def evaluate(
    job: Job, spec: SearchSpec, taxonomy: Taxonomy, today: date, gates: list[Gate] | None = None
) -> MatchResult:
    """Apply every gate, then rank if the job survived.

    All gates are evaluated even after one fails. Stopping at the first failure would be
    faster and would leave the user asking "what else was wrong with it?" every time they
    loosened a rule and saw the same job rejected again.
    """
    gates = gates if gates is not None else compile_gates(spec)
    results: list[GateResult] = [gate.evaluate(job, taxonomy, today) for gate in gates]

    rejected = False
    for gate, result in zip(gates, results, strict=True):
        if result.outcome is GateOutcome.FAIL:
            rejected = True
        elif (
            result.outcome is GateOutcome.UNVERIFIABLE
            and gate.policy is UnverifiablePolicy.STRICT
        ):
            rejected = True

    if rejected:
        return MatchResult(decision=Decision.REJECTED, score=0.0, gates=results, signals=[])

    signals = rank(job, spec, taxonomy, today, results)
    score = round(sum(signal.points for signal in signals), 2)
    return MatchResult(decision=Decision.MATCH, score=score, gates=results, signals=signals)


def rank(
    job: Job, spec: SearchSpec, taxonomy: Taxonomy, today: date, gates: list[GateResult]
) -> list[Signal]:
    """Build the ranking ledger: every point, with the evidence that earned it."""
    weights = spec.ranking
    signals: list[Signal] = []
    text = job.searchable_text()

    hit = first_matching_phrase(job.title, spec.titles)
    if hit:
        signals.append(Signal("title matches a wanted title", weights.title_match, hit[0]))
    else:
        related = first_matching_phrase(job.title, spec.related_titles)
        if related:
            signals.append(
                Signal("title matches a related title", weights.related_title_match, related[0])
            )

    def accumulate(phrases: list[str], weight: float, label: str, cap: int) -> None:
        found = [p for p in phrases if first_matching_phrase(text, [p])][:cap]
        if found:
            signals.append(
                Signal(label, round(weight * len(found), 2), ", ".join(found))
            )

    accumulate(spec.keywords, weights.keyword_match, "keywords present", MAX_KEYWORD_HITS)
    accumulate(
        spec.required_skills, weights.required_skill_match, "skills present", MAX_SKILL_HITS
    )
    accumulate(
        spec.responsibilities_include, weights.responsibility_match,
        "responsibilities present", MAX_RESPONSIBILITY_HITS,
    )
    accumulate(spec.industries, weights.industry_match, "industry match", 2)

    target = first_matching_phrase(job.company, spec.companies)
    if target:
        signals.append(Signal("target employer", weights.target_company_match, target[0]))

    # The first listed workplace preference is treated as the preferred one; the gate has
    # already established the job is in the acceptable set.
    if spec.workplace_types and job.workplace is spec.workplace_types[0]:
        signals.append(
            Signal("preferred workplace", weights.preferred_workplace, job.workplace.value)
        )

    if spec.locations:
        where = f"{job.location.display()} {job.location.raw}"
        place = first_matching_phrase(where, spec.locations)
        if place:
            signals.append(Signal("preferred location", weights.preferred_location, place[0]))

    if job.salary is not None:
        signals.append(
            Signal("pay disclosed", weights.salary_disclosed, _describe_salary(job))
        )
        if spec.salary_min:
            # The same comparison the gate makes: the annualized floor against the
            # advertised MINIMUM. Using the raw spec figure, or the top of the range,
            # let the ledger award "pay above floor" to a job the gate would not.
            floor = SalaryFloorGate(
                minimum=spec.salary_min, period=spec.salary_period
            ).annual_floor()
            low, _high = job.salary.annualized()
            if low is not None and low >= floor:
                signals.append(
                    Signal("pay above floor", weights.salary_above_floor, f"{low:,.0f}/year")
                )

    if job.posted_at is not None and weights.freshness:
        age = (today - job.posted_at).days
        if age <= 30:
            # Linear decay over 30 days. Recency is a real signal -- an older posting is
            # likelier to be filled -- but a weak one next to actual fit.
            points = round(weights.freshness * (1 - age / 30), 2)
            if points > 0:
                signals.append(Signal("recently posted", points, f"{age}d old"))

    if job.authority is AuthorityTier.EMPLOYER_SITE:
        signals.append(
            Signal("authoritative employer posting", weights.authority_bonus, "employer site")
        )

    uncertain = [g for g in gates if g.outcome is GateOutcome.UNVERIFIABLE]
    if uncertain and weights.uncertainty_penalty:
        signals.append(
            Signal(
                "unconfirmed requirements",
                round(weights.uncertainty_penalty * min(len(uncertain), 3), 2),
                ", ".join(g.gate for g in uncertain),
            )
        )

    return signals


def _describe_salary(job: Job) -> str:
    if job.salary is None:
        return ""
    salary = job.salary
    if salary.minimum and salary.maximum:
        return f"{salary.minimum:,.0f}-{salary.maximum:,.0f} {salary.currency}/{salary.period}"
    value = salary.minimum or salary.maximum or 0
    return f"{value:,.0f} {salary.currency}/{salary.period}"


def explain(result: MatchResult) -> list[str]:
    """Human-readable lines describing a verdict.

    Rejections lead with the failing rule and the text that triggered it. Matches lead
    with the ledger. Uncertainty is always stated rather than rounded away.
    """
    lines: list[str] = []
    if result.decision is Decision.REJECTED:
        lines.append("Rejected because:")
        lines.extend(f"  - {g.describe()}" for g in result.failed_gates)
        strict = [
            g for g in result.unverifiable_gates
            if g not in result.failed_gates
        ]
        if strict:
            lines.append("Could not be confirmed:")
            lines.extend(f"  - {g.describe()}" for g in strict)
        return lines

    lines.append(f"Matched, score {result.score:g}:")
    lines.extend(f"  {s.describe()}" for s in result.signals)
    if result.unverifiable_gates:
        lines.append("Could not be confirmed:")
        lines.extend(f"  - {g.describe()}" for g in result.unverifiable_gates)
    passed = [g for g in result.gates if g.outcome is GateOutcome.PASS and g.evidence]
    if passed:
        lines.append("Rules satisfied:")
        lines.extend(f"  - {g.describe()}" for g in passed)
    return lines
