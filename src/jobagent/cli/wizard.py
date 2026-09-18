"""The interactive search builder.

The user says what they want; the engine works out how to find it. So nothing here asks
about ATS platforms, board tokens, query syntax or source adapters -- those are the
application's problem.

The questionnaire goes beyond the obvious fields because the obvious fields are
tech-shaped. Shift patterns, licensure, compensation basis and travel decide whether a
job is viable in most of the professions this tool is meant to serve, and a search that
cannot express them is not generic.

Every answer maps to a field on SearchSpec, so anything asked here can equally be written
in a YAML file and run headlessly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import questionary
from questionary import Choice

from ..domain.spec import SearchSpec

STYLE = questionary.Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:green"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan"),
])


def _list(
    prompt: str, *, help_text: str = "", default: str = "", clarify: str = ""
) -> list[str]:
    """Ask for a comma-separated list.

    Free text rather than a fixed menu throughout: any menu of job titles or skills would
    encode a profession, and this tool must not have one.

    With ``clarify`` (a description of where the list is matched), entries short enough
    to be ordinary words are confirmed one by one before they are accepted.
    """
    message = prompt if not help_text else f"{prompt}\n  ({help_text})"
    answer = questionary.text(message, default=default, style=STYLE).ask()
    if answer is None:
        raise KeyboardInterrupt
    entries = _split(answer)
    return _clarify_short(entries, clarify) if clarify else entries


def _split(answer: str) -> list[str]:
    return [part.strip() for part in answer.split(",") if part.strip()]


# At or under this length an entry is as likely to be an everyday word as a name: a
# two-letter language is also a verb, and a one-letter one turns up in "Plan C".
_SHORT_ENTRY = 3


def _clarify_short(entries: list[str], scope: str) -> list[str]:
    """Confirm, rewrite or drop each entry short enough to hit ordinary prose.

    A user typed a two-letter language and a one-letter one into a list that rules a job
    out on any hit. Both are legal entries and both match common English wherever it
    appears as a standalone word, so the search would have quietly lost good jobs and
    the user would never have known why. Asking costs one question; guessing costs
    matches.
    """
    short = [entry for entry in entries if len(entry) <= _SHORT_ENTRY]
    if not short:
        return entries

    questionary.print(
        f"  Short entries match {scope} wherever they stand alone as a word, ordinary\n"
        "  sentences included. Tick the ones to keep exactly as typed; you will be asked\n"
        "  to rewrite or drop the rest.",
        style="yellow",
    )
    kept = questionary.checkbox(
        "Keep as typed?", choices=[Choice(entry, checked=False) for entry in short],
        style=STYLE,
    ).ask()
    if kept is None:
        raise KeyboardInterrupt

    result: list[str] = []
    for entry in entries:
        if entry not in short or entry in kept:
            result.append(entry)
            continue
        replacement = questionary.text(
            f"Replace '{entry}' with (the full name of the tool or language usually "
            "works; blank drops it)",
            style=STYLE,
        ).ask()
        if replacement is None:
            raise KeyboardInterrupt
        result.extend(_split(replacement))
    return result


def _confirm(prompt: str, default: bool = False) -> bool:
    answer = questionary.confirm(prompt, default=default, style=STYLE).ask()
    if answer is None:
        raise KeyboardInterrupt
    return bool(answer)


def run_wizard(existing: SearchSpec | None = None) -> SearchSpec:
    """Build a SearchSpec by asking, then show what the rules will do before saving."""
    data: dict[str, Any] = existing.model_dump() if existing else {}

    questionary.print("\nDescribe the job you are looking for.", style="bold cyan")
    questionary.print(
        "Lists are comma-separated. Leave anything blank to skip it.\n", style="dim"
    )

    name = questionary.text(
        "Name this search", default=data.get("name", ""), style=STYLE,
        validate=lambda v: bool(v.strip()) or "a name is required",
    ).ask()
    if name is None:
        raise KeyboardInterrupt
    data["name"] = name.strip()

    # --- the work itself ---------------------------------------------------------
    # Each question says where in the posting it looks, because that is the difference
    # between a filter and a deal-breaker: a phrase in the company blurb is evidence
    # about the company, not the job, and a live run showed a React search losing good
    # matches to a backend stack mentioned in passing.
    questionary.print("\nThe role", style="bold")
    questionary.print(
        "Each filter reads one part of the posting: TITLE, DUTIES (the responsibilities\n"
        "section, or the whole posting when there is none) or ANYWHERE (the entire posting,\n"
        "company blurb included). Anything that rules a job out does so on a single hit,\n"
        "so the wider the scope, the shorter and more deliberate the list should be.\n",
        style="dim",
    )
    data["titles"] = _list(
        "Job titles you want",
        help_text=(
            "TITLE only, as it would appear in a posting. Type a compound with its hyphen "
            "or space and the hyphenated, spaced and joined spellings are all found"
        ),
        default=", ".join(data.get("titles", [])),
    )
    data["related_titles"] = _list(
        "Other titles you would accept",
        help_text="TITLE only; the same work under a different name",
        default=", ".join(data.get("related_titles", [])),
    )
    data["excluded_titles"] = _list(
        "Titles to exclude outright",
        help_text="TITLE only; any of these in the title rules the job out",
        default=", ".join(data.get("excluded_titles", [])),
    )
    data["responsibilities_include"] = _list(
        "Duties the job should involve",
        help_text=(
            "DUTIES; a differently-titled role still qualifies if its work matches. "
            "Keep phrases specific: a bare 'component' matches 'one component of pay'"
        ),
        default=", ".join(data.get("responsibilities_include", [])),
        clarify="in the duties section",
    )
    data["responsibilities_exclude"] = _list(
        "Duties you do not want",
        help_text=(
            "DUTIES only: the work itself, not a stack listed in passing. One hit rules "
            "the job out, so avoid short everyday words; 'go' matches 'go above and beyond'"
        ),
        default=", ".join(data.get("responsibilities_exclude", [])),
        clarify="in the duties section",
    )
    data["required_skills"] = _list(
        "Skills or tools the job must mention",
        help_text=(
            "ANYWHERE, matched as a name in your casing, so a capitalised tool name "
            "will not match the same word used as a verb; a lower-case entry matches either"
        ),
        default=", ".join(data.get("required_skills", [])),
        clarify="anywhere in the posting",
    )
    data["excluded_skills"] = _list(
        "Skills or tools that rule a job out",
        help_text=(
            "ANYWHERE, as a name. One mention in the company blurb counts, so keep this "
            "to true deal-breakers; a backend you would rather not touch belongs under duties"
        ),
        default=", ".join(data.get("excluded_skills", [])),
        clarify="anywhere in the posting",
    )

    # --- level -------------------------------------------------------------------
    questionary.print("\nLevel", style="bold")
    levels = questionary.checkbox(
        "Exclude these seniority levels",
        choices=[
            Choice("intern", checked="intern" in data.get("seniority_exclude", [])),
            Choice("entry", checked="entry" in data.get("seniority_exclude", [])),
            Choice("mid", checked="mid" in data.get("seniority_exclude", [])),
            Choice("senior", checked="senior" in data.get("seniority_exclude", [])),
            Choice("staff", checked="staff" in data.get("seniority_exclude", [])),
            Choice("management", checked="management" in data.get("seniority_exclude", [])),
        ],
        style=STYLE,
    ).ask()
    data["seniority_exclude"] = levels or []

    # --- where -------------------------------------------------------------------
    questionary.print("\nWhere", style="bold")
    workplace = questionary.checkbox(
        "Acceptable working arrangements",
        choices=[
            Choice("remote", checked="remote" in data.get("workplace", ["remote"])),
            Choice("hybrid", checked="hybrid" in data.get("workplace", [])),
            Choice("onsite", checked="onsite" in data.get("workplace", [])),
        ],
        style=STYLE,
    ).ask()
    data["workplace"] = workplace or []

    if workplace and workplace != ["remote"]:
        data["locations"] = _list(
            "Cities or metros",
            help_text="e.g. Dallas, Fort Worth",
            default=", ".join(data.get("locations", [])),
        )

    # --- terms -------------------------------------------------------------------
    questionary.print("\nTerms", style="bold")
    # Pre-checked from what is already saved. Hard-coding full_time silently narrowed
    # an existing search to full-time whenever the user accepted the shown defaults.
    stored_types = data.get("employment_types") or ["full_time"]
    employment = questionary.checkbox(
        "Employment types you will consider",
        choices=[
            Choice(name, checked=name in stored_types)
            for name in ("full_time", "part_time", "contract", "internship")
        ],
        style=STYLE,
    ).ask()
    data["employment_types"] = employment or []

    if _confirm("Do you have a minimum pay requirement?", bool(data.get("salary_min"))):
        basis = questionary.select(
            "Is that hourly or annual?",
            choices=["year", "hour"], default=data.get("salary_period", "year"), style=STYLE,
        ).ask()
        amount = questionary.text(
            f"Minimum pay per {basis}",
            default=str(int(data["salary_min"])) if data.get("salary_min") else "",
            style=STYLE,
            validate=lambda v: v.replace(",", "").replace(".", "").isdigit() or "enter a number",
        ).ask()
        if amount:
            data["salary_min"] = float(amount.replace(",", ""))
            data["salary_period"] = basis
    else:
        # Answering "no" has to CLEAR an existing floor. Leaving the old value meant an
        # edit could never remove a pay constraint.
        data["salary_min"] = None

    freshness = questionary.text(
        "Only show jobs posted within how many days? (blank for any)",
        default=str(data.get("max_age_days") or ""), style=STYLE,
    ).ask()
    data["max_age_days"] = int(freshness) if freshness and freshness.isdigit() else None

    # --- constraints that decide viability outside tech --------------------------
    questionary.print("\nConstraints", style="bold")
    credentials = _list(
        "Credentials that should rule a job OUT when required",
        help_text=(
            "read in context: a licence or certification the posting REQUIRES rules it "
            "out; jobs that merely prefer it are kept"
        ),
        default=", ".join(r["term"] for r in data.get("excluded_requirements", [])),
    )
    data["excluded_requirements"] = [{"term": term, "when": "required"} for term in credentials]

    data["required_credentials"] = _list(
        "Credentials you hold and want the job to mention",
        default=", ".join(data.get("required_credentials", [])),
    )
    data["shift_exclude"] = _list(
        "Shifts or schedules you cannot work",
        help_text="ANYWHERE in the posting; e.g. night shift, weekends, on-call",
        default=", ".join(data.get("shift_exclude", [])),
        clarify="anywhere in the posting",
    )
    data["needs_visa_sponsorship"] = _confirm(
        "Do you need visa sponsorship?", bool(data.get("needs_visa_sponsorship"))
    )
    data["exclude_security_clearance"] = _confirm(
        "Exclude jobs requiring a security clearance?",
        bool(data.get("exclude_security_clearance")),
    )
    data["deal_breakers"] = _list(
        "Anything else that rules a job out",
        help_text=(
            "ANYWHERE, including the company description; the widest filter here. "
            "One mention rules the job out, so list only what you would never accept"
        ),
        default=", ".join(data.get("deal_breakers", [])),
        clarify="anywhere in the posting",
    )

    # --- employers ---------------------------------------------------------------
    data["companies"] = _list(
        "Employers you especially want (ranked higher)",
        default=", ".join(data.get("companies", [])),
    )
    data["excluded_companies"] = _list(
        "Employers to exclude", default=", ".join(data.get("excluded_companies", []))
    )

    # --- uncertainty -------------------------------------------------------------
    questionary.print("\nWhen a posting does not say", style="bold")
    policy = questionary.select(
        "Many postings omit location, pay or working arrangement. What should happen?",
        choices=[
            Choice("Keep them, marked as unconfirmed (recommended)", value="flag"),
            Choice("Exclude anything that cannot be confirmed", value="strict"),
        ],
        style=STYLE,
    ).ask()
    data["unverifiable_policy"] = policy or "flag"

    data.setdefault("countries", ["US"])
    spec = SearchSpec.model_validate(data)
    _preview(spec)
    return spec


def _preview(spec: SearchSpec) -> None:
    """Show the rules that were just built, before anything is saved.

    A filter is easy to get wrong and expensive to get wrong quietly, so the rules are
    stated back in the user's own words before they commit to them.
    """
    from ..domain.spec import compile_gates

    questionary.print("\nThese rules will be applied:", style="bold cyan")
    for gate in compile_gates(spec):
        questionary.print(f"  - {_describe_gate(gate)}", style="")
    if spec.unverifiable_policy == "flag":
        questionary.print(
            "  - postings that do not state something will be kept and marked", style="dim"
        )
    else:
        questionary.print(
            "  - postings that do not state something will be excluded", style="dim"
        )
    questionary.print("")


def _describe_gate(gate: Any) -> str:
    name = gate.name
    if name.startswith("requirement:"):
        term = name.split(":", 1)[1]
        return f"exclude jobs that REQUIRE {term} (jobs where it is preferred are kept)"
    # One description per gate, computed only for that gate. These used to be f-strings
    # in a dict literal, so every description was evaluated for every gate: the
    # workplace line read ``.value`` off the country gate's plain strings and crashed
    # the wizard after the last question and before the search was saved.
    mapping: dict[str, Callable[[], str]] = {
        "title_excludes": lambda: f"exclude titles containing {gate.phrases}",
        "seniority_excludes": lambda: f"exclude seniority levels {gate.levels}",
        "workplace": lambda: f"only {[w.value for w in gate.allowed]} roles",
        "country": lambda: f"only jobs in {gate.allowed}",
        "location": lambda: f"only jobs located in {gate.places}",
        "relevance": lambda: "the job must match a wanted title or duty",
        "required_keywords": lambda: f"the posting must mention {gate.phrases}",
        "required_skills": lambda: f"the posting must name the skills {gate.phrases}",
        "required_credentials": lambda: f"the posting must mention {gate.phrases}",
        "excluded_responsibilities": lambda: (
            f"exclude jobs whose duties include {gate.phrases}"
        ),
        "excluded_skills": lambda: f"exclude any mention of the skills {gate.phrases}",
        "excluded_content": lambda: f"exclude any mention of {gate.phrases}",
        "company_excludes": lambda: f"exclude employers {gate.companies}",
        "employment_type": lambda: f"only {gate.allowed} roles",
        "salary_floor": lambda: f"pay must be at least {gate.minimum:,.0f} per {gate.period}",
        "freshness": lambda: f"posted within {gate.max_age_days} days",
        "sponsorship": lambda: "exclude roles that will not sponsor a visa",
    }
    describe = mapping.get(name)
    return describe() if describe else name
