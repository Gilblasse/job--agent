"""The jobagent command line."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..domain.models import SourceStatus, UserStatus
from ..domain.spec import SearchSpec
from ..engine.doctor import run_doctor
from ..engine.orchestrator import run_search
from ..engine.verify import verify_jobs
from ..infra.http import HttpFetcher
from ..infra.store import DEFAULT_DB_PATH, Store
from ..sources.catalog import CATALOG, EXCLUDED, ats_adapters
from ..sources.registry import add_from_url, import_csv, seed_registry
from . import export as exporters
from .render import coverage_table, doctor_table, job_panel, load_explanation, results_table

console = Console()

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find, filter and track jobs from employer ATS boards and official employer systems.",
)
search_app = typer.Typer(no_args_is_help=True, help="Create and manage saved searches.")
sources_app = typer.Typer(no_args_is_help=True, help="Inspect and check job sources.")
company_app = typer.Typer(no_args_is_help=True, help="Manage the company board registry.")
app.add_typer(search_app, name="search")
app.add_typer(sources_app, name="sources")
app.add_typer(company_app, name="company")

DbOption = typer.Option(str(DEFAULT_DB_PATH), "--db", help="Path to the local database.")


def open_store(db: str) -> Store:
    return Store(db)


def load_spec(store: Store, name: str) -> tuple[int, SearchSpec]:
    found = store.get_spec(name)
    if found is None:
        console.print(f"[red]No search named {name!r}.[/red] Try: jobagent search list")
        raise typer.Exit(1)
    search_id, spec_yaml = found
    return search_id, SearchSpec.from_yaml(spec_yaml)


# --------------------------------------------------------------------------- search


@search_app.command("create")
def search_create(
    db: str = DbOption,
    from_file: Path | None = typer.Option(
        None, "--from", help="Create from a YAML spec instead of the interactive wizard."
    ),
) -> None:
    """Build a search, interactively or from a YAML file."""
    if from_file:
        spec = SearchSpec.from_yaml(from_file.read_text())
    else:
        from .wizard import run_wizard

        try:
            spec = run_wizard()
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            raise typer.Exit(130) from None

    store = open_store(db)
    store.save_spec(spec.name, spec.to_yaml())
    console.print(f"[green]Saved search {spec.name!r}.[/green]")
    console.print(f"Run it with: [bold]jobagent run {spec.name}[/bold]")


@search_app.command("list")
def search_list(db: str = DbOption) -> None:
    """List saved searches."""
    rows = open_store(db).list_specs()
    if not rows:
        console.print("[dim]No searches yet. Create one with: jobagent search create[/dim]")
        return
    table = Table(title="Saved searches", title_justify="left", expand=True)
    table.add_column("Name")
    table.add_column("Runs", justify="right", width=6)
    table.add_column("Last run")
    table.add_column("Updated")
    for row in rows:
        table.add_row(
            row["name"], str(row["runs"]), (row["last_run"] or "never")[:19],
            (row["updated_at"] or "")[:19],
        )
    console.print(table)


@search_app.command("show")
def search_show(name: str, db: str = DbOption) -> None:
    """Print a search's spec as YAML."""
    _, spec = load_spec(open_store(db), name)
    console.print(Panel(spec.to_yaml(), title=name, border_style="cyan"))


@search_app.command("edit")
def search_edit(name: str, db: str = DbOption) -> None:
    """Re-run the wizard over an existing search."""
    from .wizard import run_wizard

    store = open_store(db)
    _, spec = load_spec(store, name)
    try:
        updated = run_wizard(spec)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled; nothing changed.[/yellow]")
        raise typer.Exit(130) from None
    store.save_spec(updated.name, updated.to_yaml())
    console.print(f"[green]Updated {updated.name!r}.[/green]")


@search_app.command("export")
def search_export(name: str, db: str = DbOption, out: Path | None = typer.Option(None)) -> None:
    """Write a search's spec to a YAML file."""
    _, spec = load_spec(open_store(db), name)
    text = spec.to_yaml()
    if out:
        out.write_text(text)
        console.print(f"[green]Wrote {out}[/green]")
    else:
        console.print(text)


@search_app.command("delete")
def search_delete(
    name: str, db: str = DbOption,
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Delete a search and everything recorded for it."""
    if not yes and not typer.confirm(f"Delete {name!r} and its results?"):
        raise typer.Exit(1)
    if open_store(db).delete_spec(name):
        console.print(f"[green]Deleted {name!r}.[/green]")
    else:
        console.print(f"[red]No search named {name!r}.[/red]")
        raise typer.Exit(1)


# ------------------------------------------------------------------------------ run


@app.command("run")
def run(
    name: str,
    db: str = DbOption,
    sources: str | None = typer.Option(
        None, "--sources", help="Comma-separated subset of sources to use."
    ),
    budget: int | None = typer.Option(
        None, "--budget", help="Override how many boards to fan out to."
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Print only the summary."),
) -> None:
    """Execute a saved search."""
    store = open_store(db)
    search_id, spec = load_spec(store, name)
    if budget is not None:
        # An explicit 0 is a real answer ("search nothing"), and a truthiness check
        # silently substituted the saved budget for it.
        if budget < 0:
            console.print("[red]--budget cannot be negative.[/red]")
            raise typer.Exit(1)
        spec = spec.model_copy(update={"source_budget": budget})

    # Without this a first run quietly searches nothing: discovery is fan-out over the
    # registry, so an empty registry means an empty result that looks like an empty market.
    if not store.registry_counts():
        console.print("[dim]Registry is empty; seeding it first...[/dim]")
        report = seed_registry(store)
        console.print(f"[dim]Seeded {report.total} employer boards.[/dim]")

    only = [s.strip() for s in sources.split(",")] if sources else None

    # There is deliberately no flag to disable robots checking. Shipping one would make
    # circumventing a site's stated wishes a supported feature of the tool.
    with HttpFetcher() as fetcher:
        with console.status(f"Searching for {name!r}..."):
            outcome = run_search(
                spec, store, fetcher, search_id=search_id,
                today=date.today(), now=datetime.now(), only_sources=only,
            )

    if not quiet:
        console.print(coverage_table(store.run_sources(outcome.run_id)))

    console.print(Panel(outcome.summary(), title=f"Run {outcome.run_id}", border_style="green"))

    if outcome.new:
        rows = store.results(search_id, only_new=True, limit=20)
        console.print(results_table(rows, title=f"New matches ({outcome.new})"))
        console.print(f"[dim]Full results: jobagent results {name}[/dim]")
    elif outcome.matched:
        console.print(
            f"[dim]No new matches. {outcome.matched} known matches: "
            f"jobagent results {name}[/dim]"
        )
    else:
        # "Nothing matched" and "nothing was searched" are different facts, and reporting
        # the second as the first is the most misleading thing this command could do.
        # Skipped sources count here: a skipped source searched nothing.
        unsearched = [
            r for r in outcome.coverage.reports if r.status is not SourceStatus.OK
        ]
        if unsearched:
            console.print(
                "[yellow]No matches — but "
                f"{len(unsearched)} of {len(outcome.coverage.reports)} sources did not "
                "fully answer, so this result may be incomplete.[/yellow]"
            )
            for report in unsearched:
                console.print(f"  [yellow]{report.source}: {report.status.value}[/yellow] "
                              f"[dim]{report.note}[/dim]")
        else:
            console.print("[dim]No matches. Try relaxing a rule, or add more companies.[/dim]")


# -------------------------------------------------------------------------- results


@app.command("results")
def results(
    name: str,
    db: str = DbOption,
    new: bool = typer.Option(False, "--new", help="Only jobs not shown before."),
    rejected: bool = typer.Option(False, "--rejected", help="Show rejected jobs and why."),
    saved: bool = typer.Option(False, "--saved"),
    applied: bool = typer.Option(False, "--applied"),
    closed: bool = typer.Option(False, "--closed"),
    seen: bool = typer.Option(False, "--seen", help="Only jobs already shown."),
    sort: str = typer.Option("score", "--sort", help="score | date | company | title"),
    min_score: float | None = typer.Option(None, "--min-score"),
    limit: int = typer.Option(30, "--limit"),
    explain: bool = typer.Option(False, "--explain", help="Show the reasoning for each row."),
) -> None:
    """Review what a search found."""
    store = open_store(db)
    search_id, _ = load_spec(store, name)

    statuses = [
        status for flag, status in (
            (saved, "saved"), (applied, "applied"), (closed, "closed"), (seen, "seen")
        ) if flag
    ]
    rows = store.results(
        search_id, only_new=new, decision="rejected" if rejected else "match",
        statuses=statuses or None, min_score=min_score, limit=limit, order=sort,
    )
    if not rows:
        console.print("[dim]Nothing to show for those filters.[/dim]")
        return

    label = "Rejected" if rejected else ("New matches" if new else "Matches")
    console.print(results_table(rows, title=f"{label} — {name}", show_score=not rejected))

    if explain:
        for row in rows:
            from .render import explanation_panel

            console.print(explanation_panel(row, load_explanation(row)))


@app.command("show")
def show(job_id: int, db: str = DbOption, search: str | None = typer.Option(None)) -> None:
    """Show one job in full, including why it matched and every URL it was seen at."""
    store = open_store(db)
    row = store.get_job(job_id)
    if row is None:
        console.print(f"[red]No job {job_id}.[/red]")
        raise typer.Exit(1)

    search_id = load_spec(store, search)[0] if search else None
    if search_id is None:
        found = store.conn.execute(
            "SELECT search_id FROM job_search_matches WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        search_id = found["search_id"] if found else None

    explanation = store.latest_explanation(job_id, search_id) if search_id else {}
    merged = dict(row)
    merged.setdefault("score", 0)
    match = store.conn.execute(
        "SELECT m.score AS score, COALESCE(u.status, 'new') AS user_status "
        "FROM job_search_matches m "
        "LEFT JOIN user_job_status u ON u.job_id = m.job_id "
        "WHERE m.job_id=? ORDER BY m.id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    merged["score"] = match["score"] if match else 0
    merged["user_status"] = (match["user_status"] if match else None) or "new"

    for panel in job_panel(merged, store.job_source_refs(job_id), explanation):
        console.print(panel)


@app.command("mark")
def mark(
    job_id: int,
    status: str = typer.Argument(..., help="saved | applied | rejected | seen | closed"),
    db: str = DbOption,
    note: str = typer.Option("", "--note"),
) -> None:
    """Record where you stand with a job."""
    allowed = {s.value for s in UserStatus}
    if status not in allowed:
        console.print(f"[red]Status must be one of: {', '.join(sorted(allowed))}[/red]")
        raise typer.Exit(1)
    store = open_store(db)
    if store.get_job(job_id) is None:
        console.print(f"[red]No job {job_id}.[/red]")
        raise typer.Exit(1)
    store.set_user_status(job_id, status, note)
    console.print(f"[green]Job {job_id} marked {status}.[/green]")


@app.command("coverage")
def coverage(
    name: str | None = typer.Argument(None, help="Search name; defaults to its latest run."),
    run_id: int | None = typer.Option(None, "--run"),
    db: str = DbOption,
) -> None:
    """Show which sources were searched, and which were not."""
    store = open_store(db)
    if run_id is None:
        if name is None:
            console.print("[red]Give a search name or --run.[/red]")
            raise typer.Exit(1)
        search_id, _ = load_spec(store, name)
        latest = store.latest_run(search_id)
        if latest is None:
            console.print("[dim]That search has not been run yet.[/dim]")
            return
        run_id = int(latest["id"])

    rows = store.run_sources(run_id)
    if not rows:
        console.print(f"[dim]No coverage recorded for run {run_id}.[/dim]")
        return
    console.print(coverage_table(rows))

    run_row = store.get_run(run_id)
    if run_row:
        console.print(
            f"[dim]Run {run_id}: {run_row['found']} postings, {run_row['matched']} matched, "
            f"{run_row['rejected']} rejected, {run_row['new_count']} new[/dim]"
        )


@app.command("export")
def export_results(
    name: str,
    db: str = DbOption,
    fmt: str = typer.Option("csv", "--format", help="csv | json | md"),
    out: Path | None = typer.Option(None, "--out"),
    rejected: bool = typer.Option(False, "--rejected"),
    limit: int = typer.Option(500, "--limit"),
) -> None:
    """Export results."""
    if fmt not in exporters.FORMATS:
        console.print(f"[red]Format must be one of: {', '.join(exporters.FORMATS)}[/red]")
        raise typer.Exit(1)
    store = open_store(db)
    search_id, _ = load_spec(store, name)
    rows = store.results(
        search_id, decision="rejected" if rejected else "match", limit=limit
    )
    text = exporters.FORMATS[fmt](rows)
    if out:
        out.write_text(text)
        console.print(f"[green]Wrote {len(rows)} rows to {out}[/green]")
    else:
        console.print(text)


@app.command("verify")
def verify(
    name: str, db: str = DbOption,
    limit: int = typer.Option(25, "--limit", help="How many jobs to re-check."),
) -> None:
    """Re-check whether saved jobs are still open."""
    store = open_store(db)
    search_id, _ = load_spec(store, name)
    rows = store.results(search_id, limit=limit)
    with HttpFetcher() as fetcher:
        with console.status(f"Re-checking {len(rows)} jobs..."):
            summary = verify_jobs(store, fetcher, [int(r["id"]) for r in rows])
    console.print(summary.describe())


# --------------------------------------------------------------------------- sources


@sources_app.command("list")
def sources_list() -> None:
    """Show which sources this build reads, and which it deliberately does not."""
    table = Table(title="Sources", title_justify="left", expand=True)
    table.add_column("Source")
    table.add_column("Kind", width=9)
    table.add_column("Priority", width=8)
    table.add_column("Notes", ratio=3)
    for info in CATALOG.values():
        table.add_row(info.name, info.kind, info.priority, info.note)
    console.print(table)

    excluded = Table(title="Not used, and why", title_justify="left", expand=True)
    excluded.add_column("Source")
    excluded.add_column("Reason", ratio=4)
    for name, reason in EXCLUDED.items():
        excluded.add_row(name, reason)
    console.print(excluded)


@sources_app.command("doctor")
def sources_doctor(db: str = DbOption) -> None:
    """Check that this tool can actually search from here.

    Exits non-zero when the gate fails, so it can be used in a script.
    """
    store = open_store(db)
    if not store.registry_counts():
        console.print("[dim]Registry is empty; seeding it first...[/dim]")
        seed_registry(store)

    with HttpFetcher() as fetcher:
        with console.status("Probing sources..."):
            result = run_doctor(store, fetcher)

    for panel in doctor_table(result):
        console.print(panel)
    raise typer.Exit(0 if result.passed else 1)


# --------------------------------------------------------------------------- company


@company_app.command("seed")
def company_seed(
    db: str = DbOption,
    us_only: bool = typer.Option(False, "--us-only", help="Only load US-signalled boards."),
) -> None:
    """Load the bundled company registry."""
    report = seed_registry(open_store(db), only_us=us_only)
    console.print(
        f"[green]Registry: {report.added} added, {report.existing} already present, "
        f"{report.total} total.[/green]"
    )

    # A seeded board is not a searchable one. Some platforms are seeded ahead of their
    # adapter, and reporting one number would overstate the reach.
    shipped = set(ats_adapters())
    usable = sum(c for p, c in report.by_platform.items() if p in shipped)
    for platform, count in sorted(report.by_platform.items(), key=lambda kv: -kv[1]):
        suffix = "" if platform in shipped else "  [dim](no adapter shipped yet)[/dim]"
        console.print(f"  {platform:16} {count}{suffix}")
    console.print(f"[bold]{usable} boards are searchable with the adapters in this build.[/bold]")


@company_app.command("add")
def company_add(
    url: str,
    db: str = DbOption,
    name: str | None = typer.Option(None, "--name", help="Employer name."),
) -> None:
    """Register a company from a board or careers URL."""
    store = open_store(db)
    added = add_from_url(store, url, company=name)
    if added:
        console.print(f"[green]Registered {added[0].company} ({added[0].token}).[/green]")
        return

    # Not a board URL, so it may be a branded careers page with the board embedded.
    console.print("[dim]Not a direct board URL; looking for an embedded board...[/dim]")
    from ..sources.discovery import discover_boards

    with HttpFetcher() as fetcher:
        try:
            boards = discover_boards(fetcher, url)
        except Exception as error:  # noqa: BLE001
            console.print(f"[red]Could not read that page: {error}[/red]")
            raise typer.Exit(1) from None

    if not boards:
        console.print("[red]No ATS board found on that page.[/red]")
        raise typer.Exit(1)
    for board in boards:
        # Persist the routing metadata too. Dropping it sent Workday boards to the
        # default wd5/External endpoint, where they returned nothing and looked empty.
        store.add_company(
            company=name or board.token, ats=board.platform, token=board.registry_token(),
            board_url=board.url, source="careers-page", us_signal=True,
            notes=json.dumps(board.extra) if board.extra else "",
        )
        console.print(f"[green]Registered {board.platform}:{board.registry_token()}[/green]")


@company_app.command("import")
def company_import(path: Path, db: str = DbOption) -> None:
    """Bulk-load companies from a CSV with a url column, or ats and token columns."""
    added, problems = import_csv(open_store(db), path)
    console.print(f"[green]Imported {added} boards.[/green]")
    for problem in problems[:10]:
        console.print(f"  [yellow]{problem}[/yellow]")
    if len(problems) > 10:
        console.print(f"  [yellow]...and {len(problems) - 10} more[/yellow]")


@company_app.command("list")
def company_list(
    db: str = DbOption,
    platform: str | None = typer.Option(None, "--platform"),
    limit: int = typer.Option(30, "--limit"),
) -> None:
    """Show registered company boards and their health."""
    store = open_store(db)
    rows = store.registry_targets(
        platforms=[platform] if platform else None, limit=limit, max_failures=99
    )
    if not rows:
        console.print("[dim]Registry is empty. Run: jobagent company seed[/dim]")
        return
    table = Table(title="Company registry", title_justify="left", expand=True)
    table.add_column("Company", ratio=2)
    table.add_column("Platform", width=16)
    table.add_column("Token", ratio=2)
    table.add_column("US", width=4)
    table.add_column("Fails", width=6, justify="right")
    for row in rows:
        table.add_row(
            row["company"], row["ats"], row["token"],
            "yes" if row["us_signal"] else "", str(row["consecutive_failures"]),
        )
    console.print(table)
    counts = store.registry_counts()
    console.print(f"[dim]{sum(counts.values())} healthy boards: {counts}[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
