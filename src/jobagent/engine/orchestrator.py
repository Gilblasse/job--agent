"""Running a search end to end.

Fan out to every source, normalize what comes back, collapse duplicates, decide which
record is authoritative, apply the user's rules, persist, and report honestly what was
searched.

The invariant that shapes the whole file: one failing source must never fail the run. A
search that reached four of seven sources is a useful result accompanied by an honest
coverage report, not an error.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import partial

from ..domain.matching import evaluate
from ..domain.models import (
    Coverage,
    Decision,
    Job,
    MatchResult,
    RawPosting,
    SourceReport,
    SourceStatus,
)
from ..domain.spec import SearchSpec, compile_gates
from ..domain.taxonomy import Taxonomy
from ..infra.store import Store
from ..ports import Fetcher, NullProgress, RunProgress
from ..sources.catalog import all_adapters
from .planning import build_plans
from .resolve import resolve


@dataclass
class RunOutcome:
    """What one execution of a search produced."""

    run_id: int
    coverage: Coverage = field(default_factory=Coverage)
    found: int = 0
    matched: int = 0
    rejected: int = 0
    new: int = 0
    matches: list[tuple[Job, MatchResult, bool]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.found} postings, {self.matched} matched, {self.rejected} rejected, "
            f"{self.new} new"
        )


class _Guarded:
    """A progress display that cannot break the run.

    The hooks are called from inside the source workers, whose exceptions are read as
    the source having failed. A bug in a progress bar would then mark every source
    failed and empty the results, so display errors are dropped here: the run's output
    is the product, the bar is not.
    """

    def __init__(self, inner: RunProgress):
        self._inner = inner

    def __getattr__(self, name: str):
        hook = getattr(self._inner, name)

        def call(*args, **kwargs):
            try:
                hook(*args, **kwargs)
            except Exception:  # noqa: BLE001 - see class docstring
                pass

        return call


def run_search(
    spec: SearchSpec,
    store: Store,
    fetcher: Fetcher,
    *,
    search_id: int,
    taxonomy: Taxonomy | None = None,
    today: date | None = None,
    now: datetime | None = None,
    only_sources: list[str] | None = None,
    max_workers: int = 4,
    progress: RunProgress | None = None,
) -> RunOutcome:
    """Execute a search and persist everything it learned.

    ``progress`` hears about each board read, each source finished and each job judged;
    it is for display and nothing in the run depends on it.
    """
    taxonomy = taxonomy or Taxonomy.default()
    today = today or date.today()
    now = now or datetime.now()
    progress = _Guarded(progress or NullProgress())

    run_id = store.start_run(search_id, now)
    outcome = RunOutcome(run_id=run_id)

    plans = build_plans(spec, store, fetcher, today, only_sources)
    adapters = all_adapters()
    postings: list[RawPosting] = []

    for plan in plans:
        plan.request.on_unit = partial(progress.unit_done, plan.source)
    progress.discovery_started({plan.source: plan.boards for plan in plans})

    # Sources run concurrently because they are independent and mostly I/O-bound. The
    # per-host limiter inside the fetcher keeps concurrency from becoming a burst: several
    # platforms host thousands of tenants behind a single name.
    def execute(plan) -> tuple[str, list[RawPosting], SourceReport]:
        adapter = adapters.get(plan.source)
        if adapter is None:
            return plan.source, [], SourceReport(
                source=plan.source, status=SourceStatus.SKIPPED, note="no adapter"
            )
        try:
            result = adapter.discover(plan.request)
        except Exception as error:  # noqa: BLE001 - contained so one source cannot end the run
            status, note = adapter.classify_failure(error)
            return plan.source, [], SourceReport(source=plan.source, status=status, note=note)
        report = result.report or SourceReport(source=plan.source, status=SourceStatus.OK)
        return plan.source, result.postings, report

    # Handled in completion order so the display can say a source is done when it is,
    # but gathered in plan order afterwards so the coverage table, the resolve order
    # and therefore the job ids read the same every run.
    results: dict[int, tuple[list[RawPosting], SourceReport]] = {}
    if plans:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(execute, plan): index for index, plan in enumerate(plans)}
            for future in as_completed(futures):
                source, found, report = future.result()
                results[futures[future]] = (found, report)
                _record_board_health(store, report)
                progress.source_done(source, report)
    for index in sorted(results):
        found, report = results[index]
        postings.extend(found)
        outcome.coverage.add(report)

    outcome.found = len(postings)

    jobs = resolve(postings, taxonomy, now)
    gates = compile_gates(spec)
    progress.evaluation_started(len(jobs))

    for job in jobs:
        result = evaluate(job, spec, taxonomy, today, gates)
        job_id = store.upsert_job(job, now)

        # Asked before the current verdict is written, so "new" means new to this search
        # rather than new to this run.
        is_new = not store.job_seen_by_search(job_id, search_id)
        store.record_match(job_id, search_id, run_id, result, is_new)

        matched = result.decision is Decision.MATCH
        if matched:
            outcome.matched += 1
            outcome.new += int(is_new)
            outcome.matches.append((job, result, is_new))
        else:
            outcome.rejected += 1
        progress.job_evaluated(matched, is_new)

    outcome.matches.sort(key=lambda item: item[1].score, reverse=True)

    store.finish_run(
        # The run's own clock, not the wall clock: a caller running with a fixed clock
        # would otherwise persist a finished_at from a different timeline than
        # started_at.
        run_id, now, outcome.coverage, found=outcome.found,
        matched=outcome.matched, rejected=outcome.rejected, new_count=outcome.new,
    )
    return outcome


def _record_board_health(store: Store, report: SourceReport) -> None:
    """Update per-board health from a source's outcome.

    Rate limiting is recorded but never counted against a board. Conflating "we were
    throttled" with "this tenant is gone" is how a registry quietly loses its best
    sources: the boards most worth reading are the ones most likely to be throttled.
    """
    outcomes = report.__dict__.get("outcomes")
    if not outcomes:
        return
    for entry in outcomes:
        registry_id = getattr(entry.target, "registry_id", None)
        if registry_id is None:
            continue
        store.record_board_outcome(
            registry_id, ok=entry.ok, kind=getattr(entry, "failure_kind", "")
        )
