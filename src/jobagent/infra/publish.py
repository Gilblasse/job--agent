"""Moving a run from the scratch database to the cloud.

The runner keeps no state between runs. It pulls the two things a run needs from the
cloud -- the searches and the board registry -- runs the unchanged engine against a
scratch SQLite file, and publishes what it learned back, keyed by the cloud's own ids:
jobs by identity, runs by the search's immutable uid.

Everything published goes through the lease guard, and each run is published as soon as
its search finishes so the search the user asked for lands first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .store import Lease, SearchGone, Store


@dataclass
class PullReport:
    searches: int = 0
    boards: int = 0


@dataclass
class RunPublish:
    """What happened to one local run on its way to the cloud."""

    search_name: str
    search_uid: str
    local_run_id: int
    cloud_run_id: int | None = None
    found: int = 0
    matched: int = 0
    rejected: int = 0
    new: int = 0
    jobs: int = 0
    rows_written: int = 0
    abandoned: int = 0
    expired: int = 0
    revision_moved: bool = False
    skipped: str = ""

    def summary(self) -> str:
        if self.skipped:
            return f"{self.search_name}: skipped ({self.skipped})"
        note = " (rules changed since this run)" if self.revision_moved else ""
        return (
            f"{self.search_name}: {self.found} postings, {self.matched} matched, "
            f"{self.rejected} rejected, {self.new} new; {self.rows_written} rows written{note}"
        )


@dataclass
class PublishReport:
    runs: list[RunPublish] = field(default_factory=list)
    boards: int = 0
    stale_jobs_removed: int = 0
    rows_written: int = 0

    def summary(self) -> str:
        lines = [run.summary() for run in self.runs]
        lines.append(
            f"registry health for {self.boards} boards; {self.stale_jobs_removed} stale "
            f"jobs removed; {self.rows_written} rows written in total"
        )
        return "\n".join(lines)


def pull(cloud: Store, local: Store) -> PullReport:
    """Copy the searches (identity and revision intact) and the registry into ``local``."""
    report = PullReport()
    for row in cloud.conn.execute("SELECT uid, name, spec_yaml, revision FROM searches"):
        local.import_search(row["uid"], row["name"], row["spec_yaml"], int(row["revision"]))
        report.searches += 1
    report.boards = local.insert_registry_rows(cloud.registry_rows())
    return report


def publish_run(local: Store, cloud: Store, run_id: int, lease: Lease, now: datetime) -> RunPublish:
    """Publish one finished local run. Every write is guarded; the run becomes visible
    (``completed``) only as the last step."""
    run = local.get_run(run_id)
    search = local.get_search(search_id=int(run["search_id"]))
    outcome = RunPublish(
        search_name=search["name"] if search else "?", search_uid=run["search_uid"] or "",
        local_run_id=run_id, found=int(run["found"]), matched=int(run["matched"]),
        rejected=int(run["rejected"]),
    )

    outcome.abandoned = cloud.abandon_stale_runs(lease, now)

    jobs = local.jobs_for_run(run_id)
    ids, written = cloud.upsert_jobs(jobs, lease, now)
    outcome.jobs = len(jobs)
    outcome.rows_written += written

    entry = cloud.search_index().get(outcome.search_uid)
    if entry is None:
        outcome.skipped = "the search no longer exists in the cloud"
        return outcome
    outcome.revision_moved = int(entry["revision"]) != int(run["spec_revision"] or 0)

    try:
        cloud_run = cloud.insert_run(run, lease, now)
    except SearchGone:
        outcome.skipped = "the search was deleted while the run was being published"
        return outcome
    outcome.cloud_run_id = cloud_run
    outcome.rows_written += cloud.copy_run_sources(cloud_run, local.run_sources(run_id), lease, now)
    outcome.rows_written += cloud.record_matches(
        cloud_run, int(entry["id"]), local.matches_for_run(run_id), ids, lease, now
    )
    outcome.new = cloud.update_new_count(cloud_run, lease, now)
    finished = datetime.fromisoformat(run["finished_at"]) if run["finished_at"] else now
    cloud.complete_run(cloud_run, finished, lease, now)
    outcome.expired = cloud.expire_runs(int(entry["id"]), lease, now)
    return outcome


def finish_publish(
    local: Store, cloud: Store, lease: Lease, now: datetime, since: datetime,
    report: PublishReport,
) -> PublishReport:
    """After the last run: board health back to the cloud, and retention."""
    health = local.registry_health_rows(since)
    report.boards = len(health)
    report.rows_written += cloud.update_registry_health(health, lease, now)
    report.stale_jobs_removed = cloud.delete_stale_jobs(lease, now)
    report.rows_written += sum(run.rows_written for run in report.runs)
    return report
