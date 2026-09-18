"""The questions behind ``jobagent dismiss``.

Kept apart from the command so the prompts can be replaced in tests the same way the
wizard's are. The command decides what to do with the answers; this module only asks.
"""

from __future__ import annotations

import questionary
from questionary import Choice

from ..domain.feedback import NEEDS_VALUE, DismissRule, title_conflicts
from ..domain.spec import SearchSpec
from .wizard import STYLE

_PROMPTS: dict[str, str] = {
    "employer": "Never show this employer again",
    "title": "Exclude titles worded like this one  (TITLE only)",
    "duty": "Exclude a duty this job involves  (DUTIES section)",
    "skill": "Exclude a skill or tool it names  (ANYWHERE, as a name)",
    "anywhere": "A phrase that rules a job out wherever it appears  (ANYWHERE)",
    "hide": "Just hide this job; add no rule",
}


def ask_reason() -> str:
    answer = questionary.text(
        "Why don't you want jobs like this? (kept with the job, in your words)",
        style=STYLE,
        validate=lambda v: bool(v.strip()) or "a reason is required",
    ).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer.strip()


def ask_rules(spec: SearchSpec, suggestions: list[DismissRule]) -> list[DismissRule]:
    """One checkbox of what the reason could mean, then a phrase for each that needs one.

    The suggestions carry the job's own employer, core title and detected levels, so
    the common cases are one keypress; duties, skills and phrases are typed because the
    posting cannot say which of its words the user objects to.
    """
    suggested = {rule.kind: rule for rule in suggestions if rule.kind != "seniority"}
    levels = [rule for rule in suggestions if rule.kind == "seniority"]

    choices: list[Choice] = []
    for kind in ("employer", "title"):
        rule = suggested.get(kind)
        if rule:
            choices.append(Choice(f"{_PROMPTS[kind]}: {rule.value!r}", value=rule))
    for rule in levels:
        choices.append(Choice(f"Exclude this seniority level: {rule.value!r}", value=rule))
    for kind in ("duty", "skill", "anywhere"):
        choices.append(Choice(_PROMPTS[kind], value=DismissRule(kind)))  # type: ignore[arg-type]
    choices.append(Choice(_PROMPTS["hide"], value=DismissRule("hide")))

    picked = questionary.checkbox(
        "What should the next run do about it?", choices=choices, style=STYLE
    ).ask()
    if picked is None:
        raise KeyboardInterrupt
    if not picked:
        return [DismissRule("hide")]

    rules: list[DismissRule] = []
    for rule in picked:
        if rule.kind == "title":
            rules.append(_ask_title(spec, rule.value))
        elif rule.kind in NEEDS_VALUE and not rule.value:
            rules.append(_ask_phrase(rule))
        else:
            rules.append(rule)
    return rules


def _ask_title(spec: SearchSpec, default: str) -> DismissRule:
    """The title wording to exclude, refused while it would also exclude a wanted title."""
    while True:
        phrase = questionary.text(
            "Title wording to exclude", default=default, style=STYLE,
            validate=lambda v: bool(v.strip()) or "enter a phrase",
        ).ask()
        if phrase is None:
            raise KeyboardInterrupt
        phrase = phrase.strip()
        conflicts = title_conflicts(spec, phrase)
        if not conflicts:
            return DismissRule("title", phrase)
        questionary.print(
            f"  {phrase!r} also appears in a title you want: {', '.join(conflicts)}. "
            "Make it more specific.",
            style="yellow",
        )


def _ask_phrase(rule: DismissRule) -> DismissRule:
    labels = {
        "duty": "Duty to exclude (read from the responsibilities section)",
        "skill": "Skill or tool to exclude (matched anywhere, as a name in your casing)",
        "anywhere": "Phrase that rules a job out (matched anywhere in the posting)",
    }
    phrase = questionary.text(
        labels[rule.kind], style=STYLE, validate=lambda v: bool(v.strip()) or "enter a phrase"
    ).ask()
    if phrase is None:
        raise KeyboardInterrupt
    return DismissRule(rule.kind, phrase.strip())
