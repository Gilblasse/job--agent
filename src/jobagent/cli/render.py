"""Turning results into something a person can scan.

A search can return hundreds of rows. The job of this module is to make the important
question -- what is new, what is strong, and why -- answerable at a glance, and to make
uncertainty visible rather than rounding it away.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..domain.models import SourceStatus
from ..engine.doctor import DoctorResult

console = Console()

STATUS_STYLE = {
    "new": "bold green", "seen": "dim", "saved": "cyan", "applied": "bold blue",
    "rejected": "red", "closed": "strike dim", "dismissed": "dim",
}

SOURCE_STYLE = {
    SourceStatus.OK: "green", SourceStatus.PARTIAL: "yellow",
    SourceStatus.UNAVAILABLE: "red", SourceStatus.BLOCKED: "magenta",
    SourceStatus.FAILED: "red", SourceStatus.SKIPPED: "dim",
}


def results_table(
    rows: list[Any], *, title: str, show_score: bool = True, links: bool = True
) -> Table:
    """The scan view.

    The first column is the job's id, not a row number: ``show``, ``mark`` and
    ``dismiss`` all take the id, and a numbering the user could not act on sent them to
    the wrong job. The title is a terminal hyperlink where the terminal supports one,
    and the Link column carries the plain URL for those that do not.
    """
    table = Table(title=title, header_style="bold", expand=True, title_justify="left")
    table.add_column("ID", width=6, justify="right")
    if show_score:
        table.add_column("Score", width=6, justify="right")
    table.add_column("Title", ratio=3, no_wrap=False)
    table.add_column("Employer", ratio=2)
    table.add_column("Where", ratio=2)
    table.add_column("Status", width=10)
    if links:
        # Folded rather than truncated: an ellipsis makes the URL uncopyable, which
        # defeats the column.
        table.add_column("Link", ratio=3, overflow="fold")

    for row in rows:
        status = row["user_status"]
        marks = []
        if row["is_new"]:
            marks.append("NEW")
        if row["uncertain"]:
            # Surfaced rather than hidden: the job matched, but something in the user's
            # rules could not be confirmed from the posting.
            marks.append("?")
        label = Text(status, style=STATUS_STYLE.get(status, ""))
        if marks:
            label.append(" " + " ".join(marks), style="bold yellow")

        where = row["location_raw"] or "-"
        if row["workplace"] and row["workplace"] != "unknown":
            where = f"{where} ({row['workplace']})"

        url = row["url"] or ""
        title_cell = Text(row["title"], style=f"link {url}") if url else Text(row["title"])

        cells: list[Any] = [str(row["id"])]
        if show_score:
            cells.append(str(round(row["score"])))
        cells += [title_cell, row["company"], where, label]
        if links:
            cells.append(Text(url, style=f"link {url}") if url else Text("-"))
        table.add_row(*cells)
    return table


def actions_hint() -> str:
    """What the id column is for, printed under every results table."""
    return "[dim]jobagent show <id>  ·  jobagent dismiss <id>  ·  jobagent mark <id> saved[/dim]"


def explanation_panel(row: Any, explanation: dict[str, Any]) -> Panel:
    """Render why a job matched or was rejected, evidence included."""
    lines: list[str] = []
    gates = explanation.get("gates", [])
    signals = explanation.get("signals", [])

    failed = [g for g in gates if g["outcome"] == "fail"]
    unknown = [g for g in gates if g["outcome"] == "unverifiable"]
    passed = [g for g in gates if g["outcome"] == "pass" and g.get("evidence")]

    if failed:
        lines.append("[bold red]Rejected because[/bold red]")
        for gate in failed:
            # The rule is clipped: a relevance gate can carry a dozen phrases, and
            # wrapping all of them buries the evidence line that actually explains
            # the rejection.
            lines.append(f"  [red]x[/red] {gate['gate']}: {_clip(gate['rule'], 90)}")
            if gate.get("evidence"):
                lines.append(f"      matched [yellow]{gate['evidence']!r}[/yellow]")
            if gate.get("detail"):
                lines.append(f"      {_clip(gate['detail'])}")
        lines.append("")

    if signals:
        lines.append(f"[bold green]Score {row['score']:.0f}[/bold green]")
        for signal in signals:
            sign = "+" if signal["points"] >= 0 else ""
            style = "green" if signal["points"] >= 0 else "yellow"
            evidence = f" [dim]({signal['evidence']})[/dim]" if signal.get("evidence") else ""
            lines.append(
                f"  [{style}]{sign}{signal['points']:g}[/{style}] {signal['name']}{evidence}"
            )
        lines.append("")

    if unknown:
        lines.append("[bold yellow]Could not be confirmed[/bold yellow]")
        for gate in unknown:
            lines.append(f"  [yellow]?[/yellow] {gate['gate']}: {_clip(gate['detail'], 90)}")
        lines.append("")

    if passed:
        lines.append("[dim]Rules satisfied[/dim]")
        for gate in passed:
            lines.append(f"  [green]ok[/green] [dim]{gate['gate']}: {gate['evidence']}[/dim]")

    return Panel(
        "\n".join(lines) or "[dim]no explanation recorded[/dim]",
        title=f"{row['title']} — {row['company']}",
        border_style="blue",
    )


def job_panel(row: Any, sources: list[Any], explanation: dict[str, Any]) -> list[Any]:
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim", width=14)
    facts.add_column()
    facts.add_row("Employer", row["company"])
    facts.add_row("Location", row["location_raw"] or "-")
    facts.add_row("Workplace", row["workplace"])
    facts.add_row("Employment", row["employment_type"] or "-")
    if row["salary_min"] or row["salary_max"]:
        low = f"{row['salary_min']:,.0f}" if row["salary_min"] else "?"
        high = f"{row['salary_max']:,.0f}" if row["salary_max"] else "?"
        facts.add_row("Pay", f"{low}-{high} {row['salary_currency']}/{row['salary_period']}")
    facts.add_row("Posted", row["posted_at"] or "not stated")
    facts.add_row("First seen", (row["first_seen"] or "")[:10])
    facts.add_row("Last seen", (row["last_seen"] or "")[:10])
    facts.add_row("Verified", row["verification"])
    facts.add_row("Status", row["user_status"] if "user_status" in row.keys() else "-")
    facts.add_row("Apply at", f"[link={row['url']}]{row['url']}[/link]")

    panels: list[Any] = [Panel(facts, title=row["title"], border_style="cyan")]

    if len(sources) > 1:
        # Shown because deduplication is otherwise invisible: the user deserves to know
        # this one row stands for several sightings, and where they were.
        table = Table(title="Also seen at", expand=True, title_justify="left")
        table.add_column("Source")
        table.add_column("Authority", width=10)
        table.add_column("URL", ratio=3)
        for ref in sources:
            tier = {3: "employer", 2: "ats", 1: "unverified"}.get(ref["authority"], "?")
            table.add_row(ref["source"], tier, ref["url"])
        panels.append(table)

    panels.append(explanation_panel(row, explanation))
    return panels


def coverage_table(rows: list[Any]) -> Table:
    """What was actually searched -- never an implication that everything was."""
    table = Table(
        title="Source coverage", expand=True, header_style="bold", title_justify="left"
    )
    table.add_column("Source")
    table.add_column("Status", width=12)
    table.add_column("Found", width=7, justify="right")
    table.add_column("Requests", width=9, justify="right")
    table.add_column("Time", width=8, justify="right")
    table.add_column("Note", ratio=3)

    for row in rows:
        status = SourceStatus(row["status"])
        table.add_row(
            row["source"],
            Text(status.value, style=SOURCE_STYLE.get(status, "")),
            str(row["found"]),
            str(row["requests"]),
            f"{row['duration_ms'] / 1000:.1f}s",
            row["note"] or "",
        )
    return table


def doctor_table(result: DoctorResult) -> list[Any]:
    table = Table(title="Source check", expand=True, header_style="bold", title_justify="left")
    table.add_column("Source")
    table.add_column("Status", width=12)
    table.add_column("Found", width=7, justify="right")
    table.add_column("Detail", ratio=3)

    for report in sorted(result.reports, key=lambda r: r.source):
        table.add_row(
            report.source,
            Text(report.status.value, style=SOURCE_STYLE.get(report.status, "")),
            str(report.found),
            report.note,
        )

    registry = Table(title="Registry", expand=True, title_justify="left")
    registry.add_column("Platform")
    registry.add_column("Boards", justify="right")
    registry.add_column("Routable", width=10)
    for platform, count in sorted(result.registry_counts.items(), key=lambda kv: -kv[1]):
        routable = platform in result.working_ats
        registry.add_row(
            platform, str(count),
            Text("yes" if routable else "no", style="green" if routable else "dim"),
        )

    notes = (
        "\n\n" + "\n".join(f"[yellow]note:[/yellow] {n}" for n in result.notes)
        if result.notes else ""
    )
    verdict = (
        Panel(
            f"[bold green]GATE PASSED[/bold green]\n{result.summary()}{notes}",
            border_style="green",
        )
        if result.passed
        else Panel(
            "[bold red]GATE FAILED[/bold red]\n"
            + result.summary()
            + "\n\n"
            + "\n".join(f"  - {failure}" for failure in result.failures)
            + notes,
            border_style="red",
        )
    )
    return [table, registry, verdict]


def _clip(text: str, width: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 3] + "..."


def load_explanation(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["explanation"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
