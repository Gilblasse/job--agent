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


def _list(prompt: str, *, help_text: str = "", default: str = "") -> list[str]:
    """Ask for a comma-separated list.

    Free text rather than a fixed menu throughout: any menu of job titles or skills would
    encode a profession, and this tool must not have one.
    """
    message = prompt if not help_text else f"{prompt}\n  ({help_text})"
    answer = questionary.text(message, default=default, style=STYLE).ask()
    if answer is None:
        raise KeyboardInterrupt
    return [part.strip() for part in answer.split(",") if part.strip()]


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
    questionary.print("\nThe role", style="bold")
    data["titles"] = _list(
        "Job titles you want", help_text="exactly as they would appear in a posting",
        default=", ".join(data.get("titles", [])),
    )
    data["related_titles"] = _list(
        "Other titles you would accept",
        help_text="roles that are the same work under a different name",
        default=", ".join(data.get("related_titles", [])),
    )
    data["excluded_titles"] = _list(
        "Titles to exclude outright",
        default=", ".join(data.get("excluded_titles", [])),
    )
    data["responsibilities_include"] = _list(
        "Duties the job should involve",
        help_text="matched against the description, so a differently-titled role can still qualify",
        default=", ".join(data.get("responsibilities_include", [])),
    )
    data["responsibilities_exclude"] = _list(
        "Duties you do not want",
        help_text="any mention of these rules the job out",
        default=", ".join(data.get("responsibilities_exclude", [])),
    )
    data["required_skills"] = _list(
        "Skills or tools the job must mention",
        default=", ".join(data.get("required_skills", [])),
    )
    data["excluded_skills"] = _list(
        "Skills or tools that rule a job out",
        default=", ".join(data.get("excluded_skills", [])),
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
        help_text="a licence or certification; jobs that merely PREFER it are still kept",
        default=", ".join(r["term"] for r in data.get("excluded_requirements", [])),
    )
    data["excluded_requirements"] = [{"term": term, "when": "required"} for term in credentials]

    data["required_credentials"] = _list(
        "Credentials you hold and want the job to mention",
        default=", ".join(data.get("required_credentials", [])),
    )
    data["shift_exclude"] = _list(
        "Shifts or schedules you cannot work",
        help_text="e.g. night shift, weekends, on-call",
        default=", ".join(data.get("shift_exclude", [])),
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
        help_text="free text; any mention rules the job out",
        default=", ".join(data.get("deal_breakers", [])),
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
    mapping = {
        "title_excludes": f"exclude titles containing {getattr(gate, 'phrases', [])}",
        "seniority_excludes": f"exclude seniority levels {getattr(gate, 'levels', [])}",
        "workplace": f"only {[w.value for w in getattr(gate, 'allowed', [])]} roles",
        "country": f"only jobs in {getattr(gate, 'allowed', [])}",
        "relevance": "the job must match a wanted title or duty",
        "excluded_content": f"exclude any mention of {getattr(gate, 'phrases', [])}",
        "company_excludes": f"exclude employers {getattr(gate, 'companies', [])}",
        "salary_floor": f"pay must be at least {getattr(gate, 'minimum', 0):,.0f}",
        "freshness": f"posted within {getattr(gate, 'max_age_days', 0)} days",
        "security_clearance": "exclude roles requiring a security clearance",
        "sponsorship": "exclude roles that will not sponsor a visa",
    }
    return mapping.get(name, name)
