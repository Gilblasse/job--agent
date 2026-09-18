"""Turning "not jobs like this" into a rule the next run applies.

The user picks a job, says why in their own words, and chooses what the reason maps to.
The words are kept verbatim as the record; the rule is what changes the search. Nothing
here interprets the text -- there is no model to do it and guessing wrong would delete
jobs the user wanted.

Pure: no I/O. The CLI asks the questions and the store keeps the answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .normalize import detect_seniority_levels, title_core
from .spec import SearchSpec
from .taxonomy import Taxonomy
from .text import find_phrase

RuleKind = Literal["employer", "title", "duty", "skill", "anywhere", "seniority", "hide"]

# Which spec list each kind feeds. The scope of each list is the wizard's: TITLE reads
# the title, DUTIES the responsibilities section, ANYWHERE the whole posting.
SPEC_FIELD: dict[str, str] = {
    "employer": "excluded_companies",
    "title": "excluded_titles",
    "duty": "responsibilities_exclude",
    "skill": "excluded_skills",
    "anywhere": "deal_breakers",
    "seniority": "seniority_exclude",
}

# Kinds whose value the user must supply; the rest are filled from the job.
NEEDS_VALUE = {"title", "duty", "skill", "anywhere"}


@dataclass(frozen=True)
class DismissRule:
    kind: RuleKind
    value: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


def parse_rule(text: str) -> DismissRule:
    """Read a rule from its command-line form.

    ``employer`` and ``hide`` stand alone; ``seniority`` may name a level or take the
    job's; the rest are ``kind=phrase``.
    """
    kind, _, value = text.strip().partition("=")
    kind, value = kind.strip().lower(), value.strip()
    if kind not in SPEC_FIELD and kind != "hide":
        raise ValueError(
            f"unknown rule {text!r}; expected one of employer, title=..., duty=..., "
            "skill=..., anywhere=..., seniority[=level], hide"
        )
    if kind in NEEDS_VALUE and not value:
        raise ValueError(f"{kind} needs a phrase: {kind}=...")
    if kind in {"employer", "hide"} and value:
        raise ValueError(f"{kind} takes no value")
    return DismissRule(kind, value)  # type: ignore[arg-type]


def suggest_rules(title: str, company: str, taxonomy: Taxonomy) -> list[DismissRule]:
    """The rules a job itself suggests, for the user to pick from.

    The title suggestion is the title without its place or requisition suffix, so what
    the user excludes is the wording of the role rather than one posting's decoration.
    """
    rules = [DismissRule("employer", company)]
    core = title_core(title)
    if core:
        rules.append(DismissRule("title", core))
    for level, _ in detect_seniority_levels(title, taxonomy):
        rules.append(DismissRule("seniority", level))
    rules.append(DismissRule("hide"))
    return rules


def title_conflicts(spec: SearchSpec, phrase: str) -> list[str]:
    """Wanted titles that an excluded-title phrase would also exclude.

    Title exclusion is phrase-contains, so excluding a word that sits inside a wanted
    title deletes the wanted title too. The caller refuses such a rule and names the
    conflict rather than letting the search quietly empty itself.
    """
    return [
        wanted for wanted in spec.all_wanted_titles
        if find_phrase(wanted, phrase, inflect=True)
    ]


def apply_rules(spec: SearchSpec, rules: list[DismissRule], taxonomy: Taxonomy) -> SearchSpec:
    """A copy of the spec with the rules added to their lists.

    Rebuilt through validation rather than ``model_copy`` so the spec's own cleaning --
    whitespace collapsed, duplicates dropped case-insensitively -- applies to the new
    entries as it does to the wizard's.
    """
    data = spec.model_dump()
    for rule in rules:
        if rule.kind == "hide":
            continue
        if rule.kind == "seniority" and rule.value not in taxonomy.seniority:
            raise ValueError(
                f"unknown seniority level {rule.value!r}; "
                f"expected one of {', '.join(taxonomy.seniority)}"
            )
        if not rule.value:
            raise ValueError(f"{rule.kind} rule has no value")
        field = SPEC_FIELD[rule.kind]
        data[field] = [*data[field], rule.value]
    return SearchSpec.model_validate(data)


def describe(rule: DismissRule) -> str:
    """The rule in the wizard's scope language, so the user sees what it will do."""
    value = rule.value
    return {
        "employer": f"EMPLOYER: exclude {value!r}",
        "title": f"TITLE: exclude titles containing {value!r}",
        "duty": f"DUTIES: exclude jobs whose duties include {value!r}",
        "skill": f"ANYWHERE (as a name): exclude any mention of {value!r}",
        "anywhere": f"ANYWHERE: exclude any mention of {value!r}",
        "seniority": f"LEVEL: exclude seniority {value!r}",
        "hide": "hide this job only; no rule added",
    }[rule.kind]


def complete_rules(
    rules: list[DismissRule], *, title: str, company: str, spec: SearchSpec, taxonomy: Taxonomy
) -> list[DismissRule]:
    """Fill in the rules that take their value from the job, and refuse the ones that
    would misfire.

    ``employer`` takes the job's company; a bare ``seniority`` takes the title's highest
    stated level; a ``title`` rule is refused when its phrase also sits inside a wanted
    title, because excluding it would empty the search.
    """
    levels = detect_seniority_levels(title, taxonomy)
    completed: list[DismissRule] = []
    for item in rules or [DismissRule("hide")]:
        if item.kind == "employer":
            item = DismissRule("employer", company)
        elif item.kind == "seniority" and not item.value:
            if not levels:
                raise ValueError(
                    f"the title {title!r} states no seniority level; "
                    "name one: seniority=LEVEL"
                )
            order = list(taxonomy.seniority)
            highest = max(levels, key=lambda pair: order.index(pair[0]))[0]
            item = DismissRule("seniority", highest)
        elif item.kind == "title":
            conflicts = title_conflicts(spec, item.value)
            if conflicts:
                raise ValueError(
                    f"{item.value!r} also appears in a title you want "
                    f"({', '.join(conflicts)}); make it more specific"
                )
        completed.append(item)
    return completed
