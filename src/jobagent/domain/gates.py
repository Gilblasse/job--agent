"""Hard gates: the non-negotiable half of matching.

A gate answers one yes/no question about a job and always says why. Gates never score and
never rank -- a job that fails a gate is out regardless of how well it scores elsewhere,
which is what "exclude Senior roles" has to mean to be worth anything.

Three outcomes, not two. UNVERIFIABLE means the posting did not contain the information
the gate needs, which is materially different from failing. Each gate carries a policy
saying what to do about that: ``flag`` keeps the job and labels it, ``strict`` rejects it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from .models import GateOutcome, GateResult, Job, SalaryRange, WorkplaceType
from .normalize import detect_seniority_levels
from .taxonomy import Taxonomy
from .text import (
    clause_around,
    find_all_phrases,
    find_phrase,
    first_matching_phrase,
    snippet,
)


class UnverifiablePolicy(StrEnum):
    """What to do when a gate cannot reach a verdict."""

    FLAG = "flag"      # keep the job, mark it uncertain, rank it lower
    STRICT = "strict"  # treat "cannot confirm" as "does not qualify"


class RequirementContext(StrEnum):
    """Whether a qualification is demanded, merely welcomed, or unclear."""

    REQUIRED = "required"
    PREFERRED = "preferred"
    AMBIGUOUS = "ambiguous"   # both readings present and equally close
    UNSTATED = "unstated"     # mentioned, but nothing says whether it is needed
    ABSENT = "absent"         # not mentioned at all


@dataclass(frozen=True)
class RequirementFinding:
    """The outcome of reading how a posting treats one qualification."""

    context: RequirementContext
    evidence: str = ""
    marker: str = ""


def _marker_spans(clause: str, markers: list[str]) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for marker in markers:
        for span in find_all_phrases(clause, marker):
            spans.append((span[0], span[1], marker))
    return spans


def _negated_regions(clause: str, patterns: list[str]) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, clause, re.IGNORECASE):
            regions.append(match.span())
    return regions


def _inside(span: tuple[int, int], regions: list[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in regions)


def _distance(term: tuple[int, int], marker: tuple[int, int]) -> int:
    if marker[1] <= term[0]:
        return term[0] - marker[1]
    if marker[0] >= term[1]:
        return marker[0] - term[1]
    return 0


def _heading_context(text: str, position: int, taxonomy: Taxonomy) -> str | None:
    """Find the nearest preceding section heading, if any.

    Postings frequently list a bare "CPA" under a "Requirements:" heading with no inline
    marker at all. Without this, every such posting would read as UNSTATED and the gate
    would be useless on exactly the documents it matters most for.
    """
    window = text[max(0, position - 1500):position].lower()

    # Ranked by where the heading ENDS, then by its length. Both parts matter: the
    # nearest heading wins, and on a tie the more specific one does. Without the length
    # tie-break, "Preferred Qualifications" loses to the bare "qualifications" sitting
    # inside it, and every preferred-qualifications section would read as mandatory.
    best: tuple[int, int, str] | None = None
    candidates = [(h, "required") for h in taxonomy.requirement_headings]
    candidates += [(h, "preferred") for h in taxonomy.preference_headings]
    for heading, kind in candidates:
        for match in _heading_occurrences(window, heading):
            ranked = (match + len(heading), len(heading), kind)
            if best is None or ranked[:2] > best[:2]:
                best = ranked
    return best[2] if best else None


# A heading starts a line and is followed by a colon or a line break. Matching the bare
# words anywhere meant a sentence like "we have no specific requirements" promoted every
# credential mentioned in the next 1500 characters to "required".
def _heading_occurrences(window: str, heading: str) -> list[int]:
    pattern = re.compile(
        r"(?:^|\n)[\s\-*#>]{0,4}" + re.escape(heading) + r"\s*[:\-–—]?\s*(?:\n|$)",
        re.IGNORECASE,
    )
    return [m.start() for m in pattern.finditer(window)]


def classify_requirement(text: str, term: str, taxonomy: Taxonomy) -> RequirementFinding:
    """Decide whether ``text`` demands ``term``, merely prefers it, or does not say.

    This is the piece that makes "exclude CPA-required roles" usable. Matching the bare
    word "CPA" would reject every posting that says "CPA a plus", which is most of them,
    and the user would quietly lose the jobs they were looking for.

    Method: locate each mention, read only the clause containing it, and weigh
    requirement markers against preference markers by proximity. Negations are resolved
    first, so "no CPA required" contributes evidence *against* a requirement rather than
    for one. Ties yield AMBIGUOUS instead of a coin flip.

    Across multiple mentions, REQUIRED wins: if a posting demands the credential anywhere,
    it is required, whatever a later sentence softens it to.
    """
    if not text or not term:
        return RequirementFinding(RequirementContext.ABSENT)

    # inflect: 'Licensed CPAs required' must not read as absent just for the plural.
    occurrences = find_all_phrases(text, term, inflect=True)
    if not occurrences:
        return RequirementFinding(RequirementContext.ABSENT)

    findings: list[RequirementFinding] = []
    for span in occurrences:
        clause = clause_around(text, span)
        local = find_phrase(clause, term, inflect=True)
        if not local:
            continue

        negated = _negated_regions(clause, taxonomy.negation_patterns)
        required = [
            s for s in _marker_spans(clause, taxonomy.required_markers)
            if not _inside((s[0], s[1]), negated)
        ]
        preferred = _marker_spans(clause, taxonomy.preferred_markers)
        # A negation is itself strong evidence the credential is not demanded.
        preferred += [(start, end, "not required") for start, end in negated]

        nearest_required = (
            min((_distance(local, (s[0], s[1])), s[2]) for s in required) if required else None
        )
        nearest_preferred = (
            min((_distance(local, (s[0], s[1])), s[2]) for s in preferred) if preferred else None
        )

        evidence = snippet(text, span, 70)
        if nearest_required and not nearest_preferred:
            findings.append(
                RequirementFinding(RequirementContext.REQUIRED, evidence, nearest_required[1])
            )
        elif nearest_preferred and not nearest_required:
            findings.append(
                RequirementFinding(RequirementContext.PREFERRED, evidence, nearest_preferred[1])
            )
        elif nearest_required and nearest_preferred:
            if nearest_required[0] < nearest_preferred[0]:
                findings.append(
                    RequirementFinding(RequirementContext.REQUIRED, evidence, nearest_required[1])
                )
            elif nearest_preferred[0] < nearest_required[0]:
                findings.append(
                    RequirementFinding(
                        RequirementContext.PREFERRED, evidence, nearest_preferred[1]
                    )
                )
            else:
                findings.append(RequirementFinding(RequirementContext.AMBIGUOUS, evidence))
        else:
            heading = _heading_context(text, span[0], taxonomy)
            if heading == "required":
                findings.append(
                    RequirementFinding(
                        RequirementContext.REQUIRED, evidence, "requirements section"
                    )
                )
            elif heading == "preferred":
                findings.append(
                    RequirementFinding(
                        RequirementContext.PREFERRED, evidence, "preferred-qualifications section"
                    )
                )
            else:
                findings.append(RequirementFinding(RequirementContext.UNSTATED, evidence))

    for wanted in (
        RequirementContext.REQUIRED,
        RequirementContext.AMBIGUOUS,
        RequirementContext.PREFERRED,
        RequirementContext.UNSTATED,
    ):
        for finding in findings:
            if finding.context is wanted:
                return finding
    return RequirementFinding(RequirementContext.ABSENT)


@dataclass
class Gate:
    """Base class. Subclasses implement ``evaluate``."""

    name: str = "gate"
    policy: UnverifiablePolicy = UnverifiablePolicy.FLAG

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        raise NotImplementedError

    def _pass(self, rule: str, evidence: str = "", detail: str = "") -> GateResult:
        return GateResult(self.name, GateOutcome.PASS, rule, evidence, detail)

    def _fail(self, rule: str, evidence: str = "", detail: str = "") -> GateResult:
        return GateResult(self.name, GateOutcome.FAIL, rule, evidence, detail)

    def _unknown(self, rule: str, detail: str) -> GateResult:
        return GateResult(self.name, GateOutcome.UNVERIFIABLE, rule, "", detail)


@dataclass
class TitleIncludesGate(Gate):
    """The title must contain at least one of the wanted phrases."""

    phrases: list[str] = field(default_factory=list)
    name: str = "title_includes"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"title must mention one of {self.phrases}"
        if not self.phrases:
            return self._pass(rule, detail="no title requirement configured")
        hit = first_matching_phrase(job.title, self.phrases)
        if hit:
            return self._pass(rule, evidence=hit[0])
        return self._fail(rule, evidence=job.title, detail="no wanted phrase in title")


@dataclass
class TitleExcludesGate(Gate):
    """The title must not contain any of the barred phrases."""

    phrases: list[str] = field(default_factory=list)
    name: str = "title_excludes"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"title must not mention {self.phrases}"
        hit = first_matching_phrase(job.title, self.phrases, inflect=True)
        if hit:
            return self._fail(rule, evidence=hit[0], detail=f"title is {job.title!r}")
        return self._pass(rule)


@dataclass
class PhraseExcludesGate(Gate):
    """No barred phrase may appear anywhere in the posting."""

    phrases: list[str] = field(default_factory=list)
    name: str = "phrase_excludes"
    # Skills are names of things, not words. Stemming a name is how "Rust" would match
    # "rusted"; ignoring its case is how "React" matched "react to volatility shifts".
    inflect: bool = True
    match_case: bool = False

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"posting must not mention {self.phrases}"
        text = job.searchable_text()
        if not text.strip():
            return self._unknown(rule, "posting has no text to search")
        for phrase in self.phrases:
            span = find_phrase(text, phrase, inflect=self.inflect, match_case=self.match_case)
            if span:
                return self._fail(
                    rule, evidence=text[span[0]:span[1]], detail=snippet(text, span, 60)
                )
        return self._pass(rule)


@dataclass
class PhraseRequiresGate(Gate):
    """At least one wanted phrase must appear somewhere in the posting."""

    phrases: list[str] = field(default_factory=list)
    name: str = "phrase_requires"
    # See PhraseExcludesGate: skills are names, and are matched as the user wrote them.
    inflect: bool = True
    match_case: bool = False

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"posting must mention one of {self.phrases}"
        if not self.phrases:
            return self._pass(rule, detail="no phrase requirement configured")
        text = job.searchable_text()
        if not text.strip():
            return self._unknown(rule, "posting has no text to search")
        hit = first_matching_phrase(
            text, self.phrases, inflect=self.inflect, match_case=self.match_case
        )
        if hit:
            return self._pass(rule, evidence=hit[0], detail=snippet(text, hit[1], 60))
        return self._fail(rule, detail="none of the wanted phrases appear")


_BULLET = ("-", "*", "\u2022", "\u25e6", "\u2013", "\u2014", ">", "#")


def _as_heading(line: str) -> str:
    """A line reduced to what it would be as a heading: no bullet, no trailing colon."""
    stripped = line.strip().lstrip("".join(_BULLET) + " ").rstrip(" :\u2013\u2014-")
    return " ".join(stripped.split()).lower()


def responsibility_text(text: str, taxonomy: Taxonomy) -> str | None:
    """The part of a posting that describes the work, or None when it has no such part.

    Everything from a responsibilities heading down to the next heading. Bounded by the
    next heading rather than a fixed window because a duties list can be three lines or
    thirty, and reading past it into "About us" is precisely the mistake being avoided.

    A heading is a line that is either a known section name or short, unpunctuated,
    capitalised and not a bullet. Bullets are checked on the raw line: a first version
    stripped the marker first and then mistook every short bullet for a heading.
    """
    if not text:
        return None
    wanted = {h.lower() for h in taxonomy.responsibility_headings}
    known = wanted | {
        h.lower() for h in taxonomy.requirement_headings + taxonomy.preference_headings
    }

    lines = text.split("\n")
    sections: list[str] = []
    index = 0
    while index < len(lines):
        if _as_heading(lines[index]) not in wanted:
            index += 1
            continue
        body: list[str] = []
        index += 1
        while index < len(lines):
            raw = lines[index].strip()
            reduced = _as_heading(raw)
            is_bullet = raw[:1] in _BULLET or raw[:2].rstrip(".)").isdigit()
            words = reduced.split()
            looks_like_heading = reduced in known or (
                not is_bullet and 1 <= len(words) <= 6 and raw[:1].isupper()
                and not raw.endswith((".", ",", ";"))
            )
            if looks_like_heading:
                break
            body.append(lines[index])
            index += 1
        sections.append("\n".join(body))
    joined = "\n".join(sections).strip()
    return joined if sections else None


@dataclass
class RelevanceGate(Gate):
    """The posting must be the kind of work asked for.

    Two kinds of evidence, read from two different places:

    - A wanted title, in the title. Never in the body: on the first live run a marketing
      role passed an accounting search because its description said "liaise with our
      accountant".
    - A wanted responsibility, in the part of the body that describes the work. Not in
      the company blurb: a sales role at a vendor of accounts-payable software passed on
      its own product pitch. Postings with no recognisable sections are read in full,
      because refusing to read them would discard real matches for a heuristic.

    Either is enough. A wanted title with an off-topic body is still the job the user
    asked for by name; an unlisted title doing the wanted work is the case the
    responsibility phrases exist to catch.

    A title that names none of the wanted work, on a posting with no description to
    read, FAILS rather than coming back unverifiable. The title is the employer's own
    one-line statement of what the job is and it is always present, so the title branch
    always reaches a verdict; UNVERIFIABLE is for when no branch can. The first version
    said unverifiable here, and a Workday board whose descriptions were over budget put
    twenty software and sales roles into an accounting search as flags.
    """

    title_phrases: list[str] = field(default_factory=list)
    responsibility_phrases: list[str] = field(default_factory=list)
    name: str = "relevance"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = (
            f"title must mention one of {self.title_phrases}, or the work must include "
            f"one of {self.responsibility_phrases}"
        )
        if not self.title_phrases and not self.responsibility_phrases:
            return self._pass(rule, detail="no relevance requirement configured")

        hit = first_matching_phrase(job.title, self.title_phrases, inflect=True)
        if hit:
            return self._pass(rule, evidence=hit[0], detail=f"title is {job.title!r}")

        if not self.responsibility_phrases:
            return self._fail(rule, evidence=job.title, detail="no wanted phrase in title")

        body = job.description_text
        if not body.strip():
            return self._fail(
                rule, evidence=job.title,
                detail="title names none of the wanted work, and no description was retrieved",
            )

        duties = responsibility_text(body, taxonomy)
        haystack = duties if duties is not None else body
        where = "the responsibilities section" if duties is not None else "the posting"
        hit = first_matching_phrase(haystack, self.responsibility_phrases, inflect=True)
        if hit:
            return self._pass(
                rule, evidence=hit[0], detail=f"in {where}: {snippet(haystack, hit[1], 60)}"
            )
        return self._fail(
            rule, evidence=job.title,
            detail=f"no wanted phrase in title, and none of the responsibilities in {where}",
        )


@dataclass
class ResponsibilityExcludesGate(Gate):
    """No barred responsibility may appear in the part of the posting describing the work.

    The mirror of ``RelevanceGate``'s responsibility branch, and scoped the same way for
    the same reason. Excluding "auditing" as a responsibility is meant to drop jobs whose
    work is auditing; read against the whole posting it dropped an accounts-payable
    manager whose requirements section asked for "attention to audit trail". Barred
    keywords and deal-breakers stay posting-wide -- those are meant to fire on any mention.

    Postings with no recognisable sections are read in full, as in the relevance gate.
    A posting with no text at all cannot be checked, so it is unverifiable, not clear.
    """

    phrases: list[str] = field(default_factory=list)
    name: str = "excluded_responsibilities"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"the work must not include {self.phrases}"
        if not self.phrases:
            return self._pass(rule, detail="no responsibility exclusions configured")
        # The title is part of the work statement: "Auditor" is an auditing job.
        hit = first_matching_phrase(job.title, self.phrases, inflect=True)
        if hit:
            return self._fail(rule, evidence=hit[0], detail=f"title is {job.title!r}")
        body = job.description_text
        if not body.strip():
            return self._unknown(rule, "no description to check the responsibilities against")
        duties = responsibility_text(body, taxonomy)
        haystack = duties if duties is not None else body
        where = "the responsibilities section" if duties is not None else "the posting"
        hit = first_matching_phrase(haystack, self.phrases, inflect=True)
        if hit:
            return self._fail(
                rule, evidence=hit[0], detail=f"in {where}: {snippet(haystack, hit[1], 60)}"
            )
        return self._pass(rule, detail=f"none of the barred responsibilities in {where}")


@dataclass
class WorkplaceGate(Gate):
    """The role's onsite/hybrid/remote arrangement must be one the user accepts."""

    allowed: list[WorkplaceType] = field(default_factory=list)
    name: str = "workplace"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        names = [w.value for w in self.allowed]
        rule = f"workplace must be one of {names}"
        if not self.allowed:
            return self._pass(rule, detail="no workplace requirement configured")
        if job.workplace is WorkplaceType.UNKNOWN:
            return self._unknown(rule, "posting does not state its workplace arrangement")
        if job.workplace in self.allowed:
            return self._pass(rule, evidence=job.workplace.value)
        return self._fail(rule, evidence=job.workplace.value)


@dataclass
class CountryGate(Gate):
    """The role must be in one of the accepted countries.

    Remote roles are the interesting case. A posting that says "Remote" and nothing else
    genuinely does not state a country, so it comes back UNVERIFIABLE rather than being
    assumed domestic -- assuming would quietly admit jobs the user cannot take.
    """

    allowed: list[str] = field(default_factory=lambda: ["US"])
    name: str = "country"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"country must be one of {self.allowed}"
        if not self.allowed:
            return self._pass(rule, detail="no country requirement configured")
        country = job.location.country
        if country is None:
            hint = (
                "remote posting with no country stated"
                if job.workplace is WorkplaceType.REMOTE
                else "posting does not state a country"
            )
            return self._unknown(rule, hint)
        if country in self.allowed:
            return self._pass(rule, evidence=country, detail=job.location.display())
        return self._fail(rule, evidence=country, detail=job.location.display())


@dataclass
class SeniorityExcludeGate(Gate):
    """Bar named seniority levels, read from the title."""

    levels: list[str] = field(default_factory=list)
    name: str = "seniority_excludes"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"seniority must not be {self.levels}"
        if not self.levels:
            return self._pass(rule, detail="no seniority exclusion configured")
        found = detect_seniority_levels(job.title, taxonomy)
        if not found:
            return self._unknown(rule, f"title {job.title!r} states no seniority level")

        # Every level the title names is checked, not just the highest. "Senior Staff
        # Accountant" names both, and a search excluding "senior" must catch it even
        # though "staff" outranks it.
        for level, evidence in found:
            if level in self.levels:
                return self._fail(rule, evidence=evidence, detail=f"detected level {level!r}")
        detected = ", ".join(level for level, _ in found)
        return self._pass(rule, evidence=found[0][1], detail=f"detected level {detected!r}")


@dataclass
class RequirementGate(Gate):
    """Bar roles that demand a named qualification.

    ``when`` selects how strict the reading is:

    - ``required``  -- reject only when the posting demands it (the default, and the one
      that makes "exclude CPA-required" mean what a person means by it)
    - ``mentioned`` -- reject on any mention at all
    - ``preferred_or_required`` -- reject when demanded or preferred, but not on a bare
      mention
    """

    term: str = ""
    when: str = "required"
    name: str = "requirement"

    def __post_init__(self) -> None:
        if self.name == "requirement" and self.term:
            self.name = f"requirement:{self.term}"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"reject when {self.term!r} is {self.when.replace('_', ' ')}"
        text = job.searchable_text()
        if not text.strip():
            return self._unknown(rule, "posting has no text to search")

        finding = classify_requirement(text, self.term, taxonomy)
        context = finding.context

        if context is RequirementContext.ABSENT:
            return self._pass(rule, detail=f"{self.term!r} not mentioned")

        if self.when == "mentioned":
            return self._fail(rule, evidence=self.term, detail=finding.evidence)

        if context is RequirementContext.REQUIRED:
            return self._fail(
                rule,
                evidence=self.term,
                detail=f"stated as required via {finding.marker!r}: {finding.evidence}",
            )
        if context is RequirementContext.PREFERRED:
            if self.when == "preferred_or_required":
                return self._fail(
                    rule, evidence=self.term, detail=f"stated as preferred: {finding.evidence}"
                )
            return self._pass(
                rule,
                evidence=self.term,
                detail=f"mentioned but only preferred ({finding.marker!r})",
            )
        if context is RequirementContext.AMBIGUOUS:
            return self._unknown(
                rule, f"{self.term!r} mentioned with conflicting wording: {finding.evidence}"
            )
        return self._unknown(
            rule, f"{self.term!r} mentioned without saying if it is required: {finding.evidence}"
        )


@dataclass
class SalaryFloorGate(Gate):
    """The advertised pay must clear a floor.

    The floor is annualized before comparison, using the period the user gave it in.
    Without that, "at least $30 an hour" was compared against an annual figure, so a
    $11/hour role cleared a $30 floor by a factor of two thousand and the explanation
    cheerfully reported the rule as satisfied.

    Most postings publish no pay at all, so the default policy is FLAG. Running this
    strict silently discards the majority of the market.
    """

    minimum: float = 0.0
    period: str = "year"
    name: str = "salary_floor"

    def annual_floor(self) -> float:
        return SalaryRange(minimum=self.minimum, period=self.period).annualized()[0] or 0.0

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        floor = self.annual_floor()
        rule = (
            f"pay must be at least {self.minimum:,.0f} per {self.period} "
            f"({floor:,.0f}/year)"
        )
        if not self.minimum:
            return self._pass(rule, detail="no salary floor configured")
        if job.salary is None:
            return self._unknown(rule, "posting advertises no pay")

        low, high = job.salary.annualized()
        # A floor is a guarantee, so it is judged on what the employer promises AT LEAST.
        # Comparing the top of the range let "$50k - $100k" clear an $80k floor even
        # though the posting may pay $50k.
        if low is None:
            return self._unknown(
                rule,
                f"posting states only an upper figure ({high:,.0f}/year); "
                "no guaranteed minimum to compare",
            )

        # Quote the posting's own wording next to the normalized figure: a bare
        # "22,880/year" is a number this tool computed, not evidence from the posting.
        stated = _describe_pay(job)
        if low >= floor:
            return self._pass(rule, evidence=stated, detail=f"minimum {low:,.0f}/year")
        return self._fail(rule, evidence=stated, detail=f"minimum {low:,.0f}/year")


@dataclass
class FreshnessGate(Gate):
    """The posting must be recent enough."""

    max_age_days: int = 0
    name: str = "freshness"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"posted within {self.max_age_days} days"
        if not self.max_age_days:
            return self._pass(rule, detail="no freshness requirement configured")
        if job.posted_at is None:
            return self._unknown(rule, "posting has no date")
        age = (today - job.posted_at).days
        if age <= self.max_age_days:
            return self._pass(rule, evidence=f"{age}d old")
        return self._fail(rule, evidence=f"{age}d old")


@dataclass
class CompanyExcludeGate(Gate):
    """Bar named employers."""

    companies: list[str] = field(default_factory=list)
    name: str = "company_excludes"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"employer must not be one of {self.companies}"
        if not self.companies:
            return self._pass(rule, detail="no employer exclusion configured")
        hit = first_matching_phrase(job.company, self.companies)
        if hit:
            return self._fail(rule, evidence=hit[0])
        return self._pass(rule)


@dataclass
class LocationGate(Gate):
    """The role must be in one of the places the user named.

    Without this, naming cities only nudges the ranking, and a search for "hybrid project
    manager in Dallas" happily returns San Francisco -- which is not a ranking problem,
    it is a wrong answer.

    Remote roles are exempt when the user accepts remote work: a fully remote job is not
    bound to a metro, and failing it for "not being in Dallas" would be nonsense.
    """

    places: list[str] = field(default_factory=list)
    remote_exempt: bool = True
    name: str = "location"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"location must be one of {self.places}"
        if not self.places:
            return self._pass(rule, detail="no location requirement configured")

        if self.remote_exempt and job.workplace is WorkplaceType.REMOTE:
            return self._pass(
                rule, evidence="remote", detail="remote roles are not tied to a place"
            )

        haystack = " ".join(
            filter(None, [job.location.raw, job.location.city, job.location.region])
        )
        if not haystack.strip():
            return self._unknown(rule, "posting does not state where the work is")

        hit = first_matching_phrase(haystack, self.places)
        if hit:
            return self._pass(rule, evidence=hit[0], detail=job.location.display())
        return self._fail(rule, evidence=job.location.display())


@dataclass
class EmploymentTypeGate(Gate):
    """The role must be one of the employment types the user will take.

    A user who says "full-time only" and is shown a 1099 contract has been ignored, not
    served. Postings state this inconsistently, so an unstated type is UNVERIFIABLE
    rather than a rejection.
    """

    allowed: list[str] = field(default_factory=list)
    name: str = "employment_type"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = f"employment type must be one of {self.allowed}"
        if not self.allowed:
            return self._pass(rule, detail="no employment-type requirement configured")

        # The posting's own field first; it is the only reliable statement of this.
        declared = job.employment_type
        if declared:
            normalized = _normalize_employment(declared, taxonomy)
            if normalized is None:
                return self._unknown(rule, f"unrecognized employment type {declared!r}")
            if normalized in self.allowed:
                return self._pass(rule, evidence=normalized, detail=f"posting says {declared!r}")
            return self._fail(rule, evidence=normalized, detail=f"posting says {declared!r}")
        return self._unknown(rule, "posting does not state an employment type")


def _normalize_employment(value: str, taxonomy: Taxonomy) -> str | None:
    """Map a posting's own wording onto the taxonomy's labels."""
    if value in taxonomy.employment_types:
        return value
    for label, terms in taxonomy.employment_types.items():
        for term in terms:
            if find_phrase(value, term):
                return label
    return None


def _describe_pay(job: Job) -> str:
    """The pay as the posting states it, for use as gate evidence."""
    salary = job.salary
    if salary is None:
        return ""
    unit = f"{salary.currency}/{salary.period}"
    if salary.minimum is not None and salary.maximum is not None:
        return f"{salary.minimum:,.0f}-{salary.maximum:,.0f} {unit}"
    value = salary.minimum if salary.minimum is not None else salary.maximum
    return f"{value:,.0f} {unit}" if value is not None else ""


# Phrases that amount to "we will not sponsor". Kept separate from a generic exclusion
# because the absence of any of them is not evidence that sponsorship IS offered.
SPONSORSHIP_REFUSALS = [
    "no sponsorship", "not able to sponsor", "unable to sponsor", "cannot sponsor",
    "will not sponsor", "does not sponsor", "do not sponsor", "without sponsorship",
    "no visa sponsorship", "not provide sponsorship", "not offer sponsorship",
    "must be authorized to work", "must already be authorized",
    "without the need for sponsorship", "not require sponsorship now or in the future",
]

SPONSORSHIP_OFFERS = [
    "visa sponsorship available", "we sponsor", "will sponsor", "sponsorship available",
    "offer sponsorship", "provide sponsorship", "h-1b sponsorship", "open to sponsorship",
]


@dataclass
class SponsorshipGate(Gate):
    """For a candidate who needs sponsorship.

    Three outcomes, because a posting has three things it can do: refuse sponsorship,
    offer it, or -- overwhelmingly the common case -- say nothing. Reading silence as a
    pass presented jobs the user cannot take as confident matches.
    """

    name: str = "sponsorship"

    def evaluate(self, job: Job, taxonomy: Taxonomy, today: date) -> GateResult:
        rule = "employer must be willing to sponsor a visa"
        text = job.searchable_text()
        if not text.strip():
            return self._unknown(rule, "posting has no text to search")

        refusal = first_matching_phrase(text, SPONSORSHIP_REFUSALS)
        if refusal:
            return self._fail(
                rule, evidence=refusal[0], detail=snippet(text, refusal[1], 60)
            )
        offer = first_matching_phrase(text, SPONSORSHIP_OFFERS)
        if offer:
            return self._pass(rule, evidence=offer[0], detail=snippet(text, offer[1], 60))
        return self._unknown(rule, "posting does not say whether it sponsors")
