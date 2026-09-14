"""Profession-neutral vocabulary used by normalization and gates.

Nothing here names an industry or a job family. These are the generic English patterns by
which postings express seniority, workplace arrangement, employment type, and whether a
qualification is demanded or merely welcomed.

The defaults live in code so the domain layer stays free of I/O. ``data/taxonomy.yml``
ships as an override that infrastructure can load and merge, which is what keeps the
vocabulary tunable without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# Ordered low to high. Used to reason about "exclude senior roles" without hardcoding any
# particular ladder: a search excludes levels by name, and these are the words to look for.
DEFAULT_SENIORITY: dict[str, list[str]] = {
    "intern": ["intern", "internship", "co-op", "coop"],
    "entry": ["entry level", "entry-level", "junior", "jr", "associate", "trainee",
              "apprentice", "graduate", "new grad", "i", "assistant"],
    "mid": ["mid level", "mid-level", "intermediate", "ii", "iii"],
    "senior": ["senior", "sr", "sr.", "iv", "lead", "specialist iv"],
    "staff": ["staff", "principal", "distinguished", "fellow"],
    # "Manager" is deliberately absent. In much of the labour market it is a job-family
    # noun rather than a rank -- Project Manager, Case Manager, Account Manager and
    # Program Manager are individual contributors -- so treating it as a seniority level
    # silently deletes an entire category of work from any search that excludes
    # management. A user who means the word literally can still exclude it by title.
    "management": ["head of", "supervisor", "chief", "director", "vp",
                   "vice president", "president", "partner"],
}

DEFAULT_REMOTE_PHRASES: list[str] = [
    "100% remote", "fully remote", "remote first", "remote-first", "work from home",
    "work from anywhere", "wfh", "telecommute", "telework", "virtual position",
    "remote position", "remote role", "remote opportunity", "remote",
]

DEFAULT_HYBRID_PHRASES: list[str] = [
    "hybrid", "partially remote", "part remote", "flexible hybrid", "days in office",
    "days per week in the office", "days onsite", "in office 2", "in office 3",
    "split between", "blend of remote and", "hybrid schedule", "hybrid work",
]

DEFAULT_ONSITE_PHRASES: list[str] = [
    "on site", "on-site", "onsite", "in person", "in-person", "in office", "in-office",
    "no remote", "not remote", "not a remote", "office based", "office-based",
]

DEFAULT_EMPLOYMENT_TYPES: dict[str, list[str]] = {
    "full_time": ["full time", "full-time", "fulltime", "permanent", "regular"],
    "part_time": ["part time", "part-time", "parttime"],
    "contract": ["contract", "contractor", "w2 contract", "1099", "fixed term",
                 "fixed-term", "temporary", "temp", "seasonal"],
    "internship": ["internship", "intern", "co-op"],
    "volunteer": ["volunteer", "unpaid"],
}

# Markers that say a qualification is demanded. Deliberately conservative: a false
# REQUIRED reading discards a job the user wanted.
DEFAULT_REQUIRED_MARKERS: list[str] = [
    "required", "requires", "require", "requirement", "must have", "must possess",
    "must hold", "must be", "mandatory", "is a must", "essential", "minimum qualification",
    "minimum qualifications", "you must", "we require",
]

# Deliberately NOT obligation markers: "active", "valid", "licensed", "current".
# They are adjectives attached to the credential, so they always sit at distance zero
# from it and would win every proximity contest against a trailing "preferred" --
# turning the very common "Active CPA license preferred" into a rejection.

# Markers that say a qualification is welcome but optional.
DEFAULT_PREFERRED_MARKERS: list[str] = [
    "preferred", "prefer", "a plus", "plus", "nice to have", "nice-to-have", "desirable",
    "desired", "bonus", "ideally", "ideal candidate", "or equivalent", "equivalent experience",
    "pursuing", "working toward", "working towards", "progress toward", "progressing toward",
    "in progress", "candidate", "eligible to sit", "optional", "helpful", "advantageous",
    "welcome", "would be great", "good to have", "willingness to obtain", "ability to obtain",
]

# Phrasings that invert a nearby "required". Checked before scoring so that
# "no CPA required" is never read as evidence that a CPA is required.
DEFAULT_NEGATION_PATTERNS: list[str] = [
    r"\bno\s+[\w\s]{0,24}?\brequired\b",
    r"\bnot\s+required\b",
    r"\bnot\s+necessary\b",
    r"\bnot\s+mandatory\b",
    r"\bdo(?:es)?\s+not\s+require\b",
    r"\bwithout\s+[\w\s]{0,24}?\brequired\b",
    r"\bno\s+need\s+for\b",
]

# Headings under which an unqualified mention reads as a requirement.
DEFAULT_REQUIREMENT_HEADINGS: list[str] = [
    "requirements", "required qualifications", "minimum qualifications", "qualifications",
    "what you need", "what you'll need", "must haves", "must-haves", "basic qualifications",
]

DEFAULT_PREFERENCE_HEADINGS: list[str] = [
    "preferred qualifications", "nice to have", "nice-to-haves", "bonus points",
    "preferred skills", "pluses", "what would set you apart", "extra credit",
]


@dataclass(frozen=True)
class Taxonomy:
    """The vocabulary bundle passed into normalization and gate evaluation."""

    seniority: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_SENIORITY))
    remote_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_REMOTE_PHRASES))
    hybrid_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_HYBRID_PHRASES))
    onsite_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_ONSITE_PHRASES))
    employment_types: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_EMPLOYMENT_TYPES)
    )
    required_markers: list[str] = field(default_factory=lambda: list(DEFAULT_REQUIRED_MARKERS))
    preferred_markers: list[str] = field(default_factory=lambda: list(DEFAULT_PREFERRED_MARKERS))
    negation_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_NEGATION_PATTERNS))
    requirement_headings: list[str] = field(
        default_factory=lambda: list(DEFAULT_REQUIREMENT_HEADINGS)
    )
    preference_headings: list[str] = field(
        default_factory=lambda: list(DEFAULT_PREFERENCE_HEADINGS)
    )

    @classmethod
    def default(cls) -> Taxonomy:
        return cls()

    def merged(self, overrides: dict[str, object]) -> Taxonomy:
        """Return a copy with ``overrides`` applied.

        Lists replace wholesale rather than append: a user who narrows the remote
        vocabulary means to narrow it, not to add to a list they cannot see.
        """
        valid = {f.name for f in self.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        clean = {k: v for k, v in overrides.items() if k in valid and v is not None}
        return replace(self, **clean)  # type: ignore[arg-type]

    def seniority_level(self, label: str) -> int | None:
        """Rank of a named seniority level, low to high; None if unknown."""
        order = list(self.seniority)
        return order.index(label) if label in order else None
