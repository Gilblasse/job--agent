"""The user's search, as data.

A ``SearchSpec`` is what the wizard produces, what YAML round-trips, and what the engine
compiles into gates and ranking weights. Keeping it a plain validated document -- rather
than code -- is what lets the same binary serve an accountant and a nurse without a
change: swapping professions swaps this file, nothing else.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .gates import (
    CompanyExcludeGate,
    CountryGate,
    EmploymentTypeGate,
    FreshnessGate,
    Gate,
    LocationGate,
    PhraseExcludesGate,
    PhraseRequiresGate,
    RelevanceGate,
    RequirementGate,
    ResponsibilityExcludesGate,
    SalaryFloorGate,
    SeniorityExcludeGate,
    SponsorshipGate,
    TitleExcludesGate,
    UnverifiablePolicy,
    WorkplaceGate,
)
from .models import WorkplaceType

WorkplaceName = Literal["remote", "hybrid", "onsite"]


class ExcludedRequirement(BaseModel):
    """A qualification that disqualifies a role when the employer demands it.

    The brief's "exclude CPA-required roles" case. ``when`` decides how literally to read
    a mention: by default only a genuine demand disqualifies, so "CPA a plus" survives.
    """

    term: str
    when: Literal["required", "preferred_or_required", "mentioned"] = "required"
    unverifiable: Literal["flag", "strict"] = "flag"


class RankingWeights(BaseModel):
    """How much each kind of evidence contributes to a job's rank.

    Exposed as data so a user can say "I care more about title fit than pay" without
    touching code, and so every point in the ledger traces back to a named setting.
    """

    title_match: float = 10.0
    related_title_match: float = 6.0
    keyword_match: float = 2.0
    required_skill_match: float = 4.0
    responsibility_match: float = 3.0
    industry_match: float = 2.0
    target_company_match: float = 8.0
    preferred_workplace: float = 5.0
    preferred_location: float = 4.0
    salary_disclosed: float = 1.5
    salary_above_floor: float = 3.0
    freshness: float = 3.0
    authority_bonus: float = 2.0
    uncertainty_penalty: float = -4.0


class SearchSpec(BaseModel):
    """Everything the user said they want, and everything they said they do not.

    Every list defaults to empty and every empty list means "no constraint". That makes a
    minimal spec legal and keeps the wizard free to skip questions that do not apply to
    the user's field.
    """

    name: str = Field(..., min_length=1)
    description: str = ""

    # --- what the role is -------------------------------------------------------
    titles: list[str] = Field(default_factory=list)
    related_titles: list[str] = Field(default_factory=list)
    excluded_titles: list[str] = Field(default_factory=list)
    title_match: Literal["hard", "soft"] = "hard"

    keywords: list[str] = Field(default_factory=list)
    required_keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)

    required_skills: list[str] = Field(default_factory=list)
    excluded_skills: list[str] = Field(default_factory=list)

    responsibilities_include: list[str] = Field(default_factory=list)
    responsibilities_exclude: list[str] = Field(default_factory=list)

    # --- where and how ----------------------------------------------------------
    workplace: list[WorkplaceName] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=lambda: ["US"])
    locations: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)

    # --- level and pay ----------------------------------------------------------
    seniority_exclude: list[str] = Field(default_factory=list)
    salary_min: float | None = None
    salary_period: Literal["year", "month", "week", "day", "hour"] = "year"

    # --- employers and sectors --------------------------------------------------
    industries: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)

    # --- qualifications and constraints beyond the brief's list -----------------
    excluded_requirements: list[ExcludedRequirement] = Field(default_factory=list)
    required_credentials: list[str] = Field(default_factory=list)
    shift_exclude: list[str] = Field(default_factory=list)
    needs_visa_sponsorship: bool = False
    exclude_security_clearance: bool = False
    deal_breakers: list[str] = Field(default_factory=list)

    # --- behaviour --------------------------------------------------------------
    max_age_days: int | None = None
    unverifiable_policy: Literal["flag", "strict"] = "flag"
    ranking: RankingWeights = Field(default_factory=RankingWeights)
    source_budget: int = 400

    @field_validator("countries")
    @classmethod
    def _upper_countries(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip().upper() for v in value if v.strip()]
        # This build is US-scoped: its sources are US employer boards and the federal
        # system. An empty list disabled the country gate entirely, and any other value
        # asked for jobs no configured source can supply -- either way a YAML file could
        # quietly leave the scope the product documents.
        if not cleaned:
            return ["US"]
        unsupported = [c for c in cleaned if c != "US"]
        if unsupported:
            raise ValueError(
                f"this build searches US sources only; unsupported countries: {unsupported}"
            )
        return cleaned

    @field_validator(
        "titles", "related_titles", "excluded_titles", "keywords", "required_keywords",
        "excluded_keywords", "required_skills", "excluded_skills",
        "responsibilities_include", "responsibilities_exclude", "locations",
        "industries", "companies", "excluded_companies", "required_credentials",
        "shift_exclude", "deal_breakers", "employment_types",
    )
    @classmethod
    def _clean_phrases(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            cleaned = " ".join(str(item).split())
            if cleaned and cleaned.lower() not in {s.lower() for s in seen}:
                seen.append(cleaned)
        return seen

    # --- serialization ----------------------------------------------------------

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_defaults=True), sort_keys=False, allow_unicode=True
        )

    @classmethod
    def from_yaml(cls, text: str) -> SearchSpec:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("a search spec must be a YAML mapping")
        return cls.model_validate(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchSpec:
        return cls.model_validate(data)

    # --- derived views ----------------------------------------------------------

    @property
    def all_wanted_titles(self) -> list[str]:
        return self.titles + self.related_titles

    @property
    def workplace_types(self) -> list[WorkplaceType]:
        mapping = {
            "remote": WorkplaceType.REMOTE,
            "hybrid": WorkplaceType.HYBRID,
            "onsite": WorkplaceType.ONSITE,
        }
        return [mapping[w] for w in self.workplace if w in mapping]

    def discovery_terms(self) -> list[str]:
        """Terms to push into sources that support server-side search.

        Deliberately built from the *positive* side of the spec only. Exclusions are
        applied after retrieval, never folded into the query: narrowing the query with
        them is what turns broad discovery into a self-fulfilling prophecy, where a job
        is missed because the query never asked for it rather than because it failed a
        rule.
        """
        terms: list[str] = []
        for phrase in self.titles + self.related_titles + self.required_keywords + self.keywords:
            if phrase and phrase not in terms:
                terms.append(phrase)
        return terms


def compile_gates(spec: SearchSpec) -> list[Gate]:
    """Turn a spec into the ordered list of hard gates the engine will apply.

    Cheap structural checks come first so an obviously disqualified job never pays for a
    full-text scan. Order has no effect on the verdict -- every gate is evaluated so the
    user can see all reasons a job failed, not just the first.
    """
    default_policy = UnverifiablePolicy(spec.unverifiable_policy)
    gates: list[Gate] = []

    if spec.excluded_titles:
        gates.append(TitleExcludesGate(phrases=spec.excluded_titles))

    if spec.seniority_exclude:
        gates.append(
            SeniorityExcludeGate(levels=spec.seniority_exclude, policy=default_policy)
        )

    if spec.workplace_types:
        gates.append(WorkplaceGate(allowed=spec.workplace_types, policy=default_policy))

    if spec.countries:
        gates.append(CountryGate(allowed=spec.countries, policy=default_policy))

    # A named place is a requirement, not a preference. Treating it as ranking-only is how
    # a search for work in one metro quietly returns another.
    if spec.locations:
        gates.append(
            LocationGate(
                places=spec.locations,
                remote_exempt="remote" in spec.workplace,
                policy=default_policy,
            )
        )

    if spec.excluded_companies:
        gates.append(CompanyExcludeGate(companies=spec.excluded_companies))

    # Relevance: the job must actually be the kind of work asked for. Title phrases are
    # read from the title and responsibility phrases from the duties section -- an
    # "Accounting Specialist" doing accounts payable is a real match that a title-only
    # gate would throw away, while a marketing role that mentions an accountant is not.
    if spec.title_match == "hard" and (spec.all_wanted_titles or spec.responsibilities_include):
        gates.append(
            RelevanceGate(
                title_phrases=spec.all_wanted_titles,
                responsibility_phrases=spec.responsibilities_include,
                policy=default_policy,
            )
        )

    if spec.required_keywords:
        gates.append(PhraseRequiresGate(name="required_keywords", phrases=spec.required_keywords))
    if spec.required_skills:
        # Skills are names, not words: no inflection, and matched with the user's casing,
        # or "React" matches "react to volatility shifts". A lower-case skill in the spec
        # still matches case-insensitively.
        gates.append(
            PhraseRequiresGate(
                name="required_skills", phrases=spec.required_skills,
                inflect=False, match_case=True,
            )
        )
    if spec.required_credentials:
        gates.append(
            PhraseRequiresGate(name="required_credentials", phrases=spec.required_credentials)
        )

    # Barred responsibilities are read from the part of the posting that describes the
    # work, mirroring how wanted responsibilities are read. Everything else here is meant
    # to fire on any mention, so it stays posting-wide.
    if spec.responsibilities_exclude:
        gates.append(
            ResponsibilityExcludesGate(
                phrases=spec.responsibilities_exclude, policy=default_policy
            )
        )
    if spec.excluded_skills:
        gates.append(
            PhraseExcludesGate(
                name="excluded_skills", phrases=spec.excluded_skills,
                inflect=False, match_case=True,
            )
        )
    barred = spec.excluded_keywords + spec.deal_breakers + spec.shift_exclude
    if barred:
        gates.append(PhraseExcludesGate(name="excluded_content", phrases=barred))

    if spec.exclude_security_clearance:
        # A requirement gate, not a phrase gate. A plain exclusion rejected
        # "Security clearance NOT required", which is the same mistake the requirement
        # classifier was written to avoid for credentials.
        for term in ("security clearance", "TS/SCI", "top secret clearance"):
            gates.append(
                RequirementGate(term=term, when="required", policy=default_policy)
            )

    if spec.needs_visa_sponsorship:
        # Only an explicit refusal excludes; silence is unverifiable rather than a pass.
        # Most postings say nothing about sponsorship, and treating that silence as a
        # confident match showed users jobs they cannot take.
        gates.append(
            SponsorshipGate(name="sponsorship", policy=default_policy)
        )

    for requirement in spec.excluded_requirements:
        gates.append(
            RequirementGate(
                term=requirement.term,
                when=requirement.when,
                policy=UnverifiablePolicy(requirement.unverifiable),
            )
        )

    if spec.employment_types:
        gates.append(
            EmploymentTypeGate(allowed=spec.employment_types, policy=default_policy)
        )

    if spec.salary_min:
        gates.append(
            SalaryFloorGate(
                minimum=spec.salary_min, period=spec.salary_period, policy=default_policy
            )
        )

    if spec.max_age_days:
        gates.append(FreshnessGate(max_age_days=spec.max_age_days, policy=default_policy))

    return gates
