"""The store's cloud-facing behaviour, exercised on plain sqlite3.

Batch writes must land exactly what the per-row path lands; an unchanged posting must
cost nothing; a results screen pinned to one run must not move; retention must never
resurface an old match or touch a run someone may still be reading.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from jobagent.domain.models import (
    AuthorityTier,
    Coverage,
    Decision,
    JobSourceRef,
    MatchResult,
)
from jobagent.infra import schema
from jobagent.infra.store import AVAILABLE_RUNS, Store
from tests.conftest import make_job

NOW = datetime(2026, 9, 14, 12, 0, 0)
LATER_TODAY = NOW + timedelta(hours=2)
TOMORROW = NOW + timedelta(days=1)


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


def job(title: str, **kwargs):
    kwargs.setdefault("description", f"We are hiring a {title}. Responsibilities: ledger work.")
    kwargs.setdefault("external_id", title.replace(" ", "-").lower())
    return make_job(title=title, **kwargs)


def completed_run(store: Store, search_id: int, when: datetime) -> int:
    run_id = store.start_run(search_id, when, spec_yaml="name: s")
    store.finish_run(run_id, when, Coverage(), found=0, matched=0, rejected=0, new_count=0)
    return run_id


def verdict(store, item, search_id, run_id, decision=Decision.MATCH, score=1.0, is_new=False):
    job_id = store.upsert_job(item, NOW)
    result = MatchResult(decision=decision, score=score)
    store.record_match(job_id, search_id, run_id, result, is_new, job=item)
    return job_id


def rows(store: Store, sql: str) -> list[tuple]:
    return [tuple(row) for row in store.conn.execute(sql)]


class TestBatchEqualsPerRow:
    def test_upsert_jobs_lands_what_upsert_job_lands(self):
        items = [job(f"Role {n}") for n in range(6)]
        items[1].sources.append(JobSourceRef(source="lever", url="https://x.example/1"))
        one, many = Store(":memory:"), Store(":memory:")
        for item in items:
            one.upsert_job(item, NOW)
        ids, _written = many.upsert_jobs(items, now=NOW)

        columns = "identity, title, company, url, content_hash, first_seen, last_seen"
        assert rows(one, f"SELECT {columns} FROM jobs ORDER BY identity") == rows(
            many, f"SELECT {columns} FROM jobs ORDER BY identity"
        )
        assert rows(one, "SELECT url, source, last_seen FROM job_sources ORDER BY url") == rows(
            many, "SELECT url, source, last_seen FROM job_sources ORDER BY url"
        )
        assert set(ids) == {item.identity for item in items}
        for identity, job_id in ids.items():
            assert many.get_job(job_id)["identity"] == identity

    def test_a_repeat_batch_is_an_update_not_a_duplicate(self, store):
        items = [job("Repeat")]
        store.upsert_jobs(items, now=NOW)
        store.upsert_jobs(items, now=TOMORROW)
        assert store.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1


class TestChangeDetection:
    def existing(self, store, item):
        return store.conn.execute(
            store._EXISTING_JOB.format("?"), (item.identity,)
        ).fetchone()

    def sources(self, store, job_id):
        return {
            row["url"]: row["last_seen"]
            for row in store.conn.execute(
                "SELECT url, last_seen FROM job_sources WHERE job_id = ?", (job_id,)
            )
        }

    def test_an_unchanged_posting_seen_again_today_costs_nothing(self, store):
        item = job("Steady")
        job_id = store.upsert_job(item, NOW)
        stmts = store._job_statements(
            item, self.existing(store, item), self.sources(store, job_id), LATER_TODAY
        )
        assert stmts == []

    def test_an_unchanged_posting_is_touched_once_a_day(self, store):
        item = job("Steady")
        job_id = store.upsert_job(item, NOW)
        stmts = store._job_statements(
            item, self.existing(store, item), self.sources(store, job_id), TOMORROW
        )
        assert len(stmts) == 2
        assert stmts[0][0].startswith("UPDATE jobs SET last_seen")
        assert stmts[1][0].startswith("UPDATE job_sources SET last_seen")
        store.upsert_job(item, TOMORROW)
        assert store.get_job(job_id)["last_seen"] == TOMORROW.isoformat()
        assert store.job_source_refs(job_id)[0]["last_seen"] == TOMORROW.isoformat()

    def test_changed_content_is_written_in_full(self, store):
        item = job("Changing")
        job_id = store.upsert_job(item, NOW)
        before = store.get_job(job_id)["content_hash"]
        edited = job(
            "Changing",
            description="Now with a much longer description of the work, its scope and its team.",
        )
        stmts = store._job_statements(
            edited, self.existing(store, item), self.sources(store, job_id), LATER_TODAY
        )
        assert stmts[0][0].startswith("UPDATE jobs SET title=")
        store.upsert_job(edited, LATER_TODAY)
        after = store.get_job(job_id)
        assert after["content_hash"] != before
        assert after["description_text"].startswith("Now with")

    def test_a_more_authoritative_sighting_is_written_in_full(self, store):
        item = job("Authority")
        job_id = store.upsert_job(item, NOW)
        better = job(
            "Authority", url="https://careers.acme.com/jobs/authority",
            authority=AuthorityTier.EMPLOYER_SITE,
        )
        store.upsert_job(better, LATER_TODAY)
        assert store.get_job(job_id)["url"] == "https://careers.acme.com/jobs/authority"

    def test_a_sparser_sighting_touches_but_never_overwrites(self, store):
        item = job("Rich", description="A long description with plenty of detail in it.")
        job_id = store.upsert_job(item, NOW)
        sparse = job("Rich", description="")
        store.upsert_job(sparse, TOMORROW)
        row = store.get_job(job_id)
        assert row["description_text"].startswith("A long description")
        assert row["last_seen"] == TOMORROW.isoformat()


class TestSourcesAreDecidedPerUrl:
    def test_a_new_url_for_an_unchanged_job_is_inserted_the_same_day(self, store):
        item = job("Stable")
        job_id = store.upsert_job(item, NOW)
        again = job("Stable")
        again.sources.append(JobSourceRef(source="lever", url="https://jobs.lever.co/acme/stable"))
        store.upsert_job(again, LATER_TODAY)
        refs = {ref["url"]: ref["last_seen"] for ref in store.job_source_refs(job_id)}
        assert "https://jobs.lever.co/acme/stable" in refs
        assert refs[item.sources[0].url] == NOW.isoformat()  # untouched: same day

    def test_only_the_url_encountered_that_day_moves(self, store):
        item = job("Stable")
        item.sources.append(JobSourceRef(source="lever", url="https://jobs.lever.co/acme/stable"))
        job_id = store.upsert_job(item, NOW)
        only_first = job("Stable")  # carries the greenhouse URL only
        store.upsert_job(only_first, TOMORROW)
        refs = {ref["url"]: ref["last_seen"] for ref in store.job_source_refs(job_id)}
        assert refs[only_first.sources[0].url] == TOMORROW.isoformat()
        assert refs["https://jobs.lever.co/acme/stable"] == NOW.isoformat()


class TestRunScopedResults:
    @pytest.fixture
    def world(self, store):
        search_id = store.save_spec("s", "name: s")
        run_a = completed_run(store, search_id, NOW)
        run_b = completed_run(store, search_id, TOMORROW)
        x, y, z = job("X role"), job("Y role"), job("Z role")
        verdict(store, x, search_id, run_a, score=5)
        verdict(store, y, search_id, run_a, score=3)
        verdict(store, z, search_id, run_a, Decision.REJECTED)
        verdict(store, x, search_id, run_b, score=7)
        return store, search_id, run_a, run_b, (x, y, z)

    def test_a_job_in_run_a_stays_visible_in_a_although_run_b_judged_it_too(self, world):
        store, search_id, run_a, run_b, (x, y, _) = world
        assert {r["title"] for r in store.results(search_id, run_id=run_a)} == {"X role", "Y role"}
        assert {r["title"] for r in store.results(search_id, run_id=run_b)} == {"X role"}
        assert store.count_results(search_id, run_id=run_a) == 2
        assert store.count_results(search_id, run_id=run_b) == 1

    def test_rejections_of_the_run_are_listed_with_the_same_query(self, world):
        store, search_id, run_a, _, _ = world
        rejected = store.results(search_id, run_id=run_a, decision="rejected")
        assert [r["title"] for r in rejected] == ["Z role"]
        assert store.count_results(search_id, run_id=run_a, decision="rejected") == 1

    def test_the_historical_view_returns_the_latest_verdict(self, world):
        store, search_id, _, run_b, _ = world
        latest = {r["title"]: r for r in store.results(search_id)}
        assert latest["X role"]["run_id"] == run_b and latest["X role"]["score"] == 7
        assert "Y role" in latest

    def test_the_screen_does_not_move_when_a_later_publish_rewrites_the_job(self, world):
        store, search_id, run_a, run_b, (x, _, _) = world
        renamed = job(
            "X role",
            description="A completely rewritten, much longer description of the X role "
            "and everything the team expects from it.",
        )
        renamed.title = "X role (renamed)"
        store.upsert_job(renamed, TOMORROW + timedelta(days=1))
        frozen = {r["title"] for r in store.results(search_id, run_id=run_a)}
        assert frozen == {"X role", "Y role"}
        row = next(r for r in store.results(search_id, run_id=run_a) if r["title"] == "X role")
        assert row["changed_since"] == 1
        current = {r["title"] for r in store.results(search_id)}
        assert "X role (renamed)" in current

    def test_changed_since_is_clear_until_the_job_changes(self, world):
        store, search_id, run_a, _, _ = world
        assert all(r["changed_since"] == 0 for r in store.results(search_id, run_id=run_a))

    @pytest.mark.parametrize("order", ["score", "date", "company", "title"])
    def test_pages_cover_every_row_once_under_every_sort(self, store, order):
        search_id = store.save_spec("p", "name: p")
        run_id = completed_run(store, search_id, NOW)
        for n in range(7):
            verdict(store, job(f"Same {n}", company="Same Co"), search_id, run_id, score=1.0)
        seen: list[int] = []
        offset = 0
        while True:
            page = store.results(search_id, run_id=run_id, limit=3, offset=offset, order=order)
            if not page:
                break
            seen.extend(r["id"] for r in page)
            offset += 3
        assert len(seen) == 7 and len(set(seen)) == 7
        assert store.count_results(search_id, run_id=run_id) == 7

    def test_explanation_is_per_run(self, world):
        store, search_id, run_a, run_b, (x, _, _) = world
        job_id = store.get_job(store.results(search_id, run_id=run_b)[0]["id"])["id"]
        assert store.explanation_for(job_id, run_a) is not None
        assert store.explanation_for(job_id, run_b) is not None
        assert store.explanation_for(job_id, 999) is None

    def test_an_unfinished_run_is_invisible_to_the_historical_view(self, store):
        search_id = store.save_spec("h", "name: h")
        done = completed_run(store, search_id, NOW)
        pending = store.start_run(search_id, TOMORROW, spec_yaml="name: h")
        item = job("Pending")
        job_id = verdict(store, item, search_id, done, score=1)
        store.record_match(
            job_id, search_id, pending, MatchResult(decision=Decision.REJECTED), False, job=item
        )
        latest = store.results(search_id)
        assert [r["run_id"] for r in latest] == [done]
        assert store.latest_explanation(job_id, search_id) == store.explanation_for(job_id, done)
        assert store.job_seen_by_search(job_id, search_id)


class TestAvailability:
    def test_only_the_latest_runs_are_available(self, store):
        search_id = store.save_spec("a", "name: a")
        ids = [completed_run(store, search_id, NOW + timedelta(days=n)) for n in range(5)]
        assert [r["id"] for r in store.available_runs(search_id)] == ids[-AVAILABLE_RUNS:][::-1]
        assert store.run_availability(search_id, ids[-1]) == "available"
        assert store.run_availability(search_id, ids[0]) == "expired"
        assert store.run_availability(search_id, 999) == "unknown"
        pending = store.start_run(search_id, TOMORROW, spec_yaml="name: a")
        assert store.run_availability(search_id, pending) == "unknown"

    def test_a_run_of_another_search_is_unknown_here(self, store):
        a = store.save_spec("a", "name: a")
        b = store.save_spec("b", "name: b")
        run_b = completed_run(store, b, NOW)
        assert store.run_availability(a, run_b) == "unknown"

    def test_available_runs_of_every_search(self, store):
        a = store.save_spec("a", "name: a")
        b = store.save_spec("b", "name: b")
        runs_a = [completed_run(store, a, NOW + timedelta(days=n)) for n in range(4)]
        runs_b = [completed_run(store, b, NOW + timedelta(days=n)) for n in range(2)]
        assert set(store.available_run_ids_all()) == set(runs_a[-3:]) | set(runs_b)


class TestRetention:
    def test_pruning_never_resurfaces_an_old_match(self, store):
        """A match in run A, a rejection in B, absent from C onwards: the rejection is
        the latest verdict and must survive, even after B expires."""
        search_id = store.save_spec("r", "name: r")
        lease = store.take_lease(NOW)
        x, y = job("X role"), job("Y role")
        runs = [completed_run(store, search_id, NOW + timedelta(days=n)) for n in range(5)]
        a, b, c, d, e = runs
        verdict(store, x, search_id, a, score=1)
        verdict(store, y, search_id, a, Decision.REJECTED)
        verdict(store, x, search_id, b, Decision.REJECTED)
        verdict(store, y, search_id, b, Decision.REJECTED)
        for later in (c, d, e):
            verdict(store, y, search_id, later, Decision.REJECTED)

        deleted = store.expire_runs(search_id, lease, NOW)
        # A and B have expired (C, D, E are available). Y was re-encountered, so its old
        # rejections go; X was not, so B's rejection -- its latest verdict -- stays.
        assert deleted == 2
        remaining = rows(
            store,
            "SELECT run_id, title, decision FROM job_search_matches ORDER BY run_id, title",
        )
        assert (a, "X role", "match") in remaining
        assert (b, "X role", "rejected") in remaining
        assert (a, "Y role", "rejected") not in remaining
        assert (b, "Y role", "rejected") not in remaining
        assert "X role" not in {r["title"] for r in store.results(search_id)}

    def test_available_runs_are_never_pruned(self, store):
        search_id = store.save_spec("r", "name: r")
        lease = store.take_lease(NOW)
        runs = [completed_run(store, search_id, NOW + timedelta(days=n)) for n in range(3)]
        y = job("Y role")
        for run_id in runs:
            verdict(store, y, search_id, run_id, Decision.REJECTED)
        assert store.expire_runs(search_id, lease, NOW) == 0
        kept = store.conn.execute("SELECT COUNT(*) AS n FROM job_search_matches").fetchone()
        assert kept["n"] == 3

    def test_stale_jobs_are_removed_unless_someone_kept_them(self, store):
        search_id = store.save_spec("r", "name: r")
        lease = store.take_lease(NOW)
        old = NOW - timedelta(days=61)
        plain = store.upsert_job(job("Plain"), old)
        saved = store.upsert_job(job("Saved"), old)
        store.set_user_status(saved, "saved")
        noted = store.upsert_job(job("Noted"), old)
        store.record_feedback(noted, search_id, "not for me", [])
        shown = store.upsert_job(job("Shown"), old)
        run_id = completed_run(store, search_id, NOW)
        store.record_match(
            shown, search_id, run_id, MatchResult(decision=Decision.MATCH, score=1), True,
            job=job("Shown"),
        )
        fresh = store.upsert_job(job("Fresh"), NOW)

        assert store.delete_stale_jobs(lease, NOW) == 1
        assert store.get_job(plain) is None
        for kept in (saved, noted, shown, fresh):
            assert store.get_job(kept) is not None


class TestSavedJobs:
    def test_lists_kept_jobs_across_searches_newest_first(self, store):
        first = store.upsert_job(job("First"), NOW)
        second = store.upsert_job(job("Second"), NOW)
        seen = store.upsert_job(job("Seen"), NOW)
        store.set_user_status(first, "saved")
        store.set_user_status(second, "applied")
        store.set_user_status(seen, "seen")
        kept = store.saved_jobs()
        assert [r["title"] for r in kept] == ["Second", "First"]
        assert kept[0]["user_status"] == "applied"


class TestMigrations:
    def test_a_failing_migration_records_nothing(self, monkeypatch):
        good = MIGRATION_ONE = schema.MIGRATIONS[0]
        bad = (99, "CREATE TABLE ok (x);\nCREATE TABLE nope (")
        monkeypatch.setattr(schema, "MIGRATIONS", [good, bad])
        monkeypatch.setattr("jobagent.infra.store.MIGRATIONS", [MIGRATION_ONE, bad])
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        store = Store.from_connection(conn)
        with pytest.raises(sqlite3.OperationalError):
            store.migrate()
        versions = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        assert versions == {1}
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "ok" not in tables and "searches" in tables
