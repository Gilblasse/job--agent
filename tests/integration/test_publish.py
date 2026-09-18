"""A run travels from the scratch database to the cloud and back into a results screen.

The cloud here is a Store over the HTTP connection, talking to the in-process Hrana
server, so every publish goes through the same batches, guards and transactions it
would against Turso -- including the one that dies halfway.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jobagent.domain.spec import SearchSpec
from jobagent.engine.cloud_run import run_cloud
from jobagent.engine.orchestrator import run_search
from jobagent.infra.publish import publish_run, pull
from jobagent.infra.store import LEASE_SECONDS, LeaseLost, Store
from jobagent.infra.turso import TursoConnection, TursoError
from tests.fakes import FakeFetcher, FakeHrana

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T0 = datetime(2026, 9, 14, 12, 0, 0)
SOURCES = ["greenhouse", "lever", "ashby"]


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def spec_yaml(name: str = "accounting", **overrides) -> str:
    data = {
        "name": name,
        "titles": ["Accounts Payable", "Junior Accountant", "Accountant", "Cash Management"],
        "excluded_titles": ["Staff Accountant"],
        "seniority_exclude": ["senior", "staff", "management"],
        "workplace": ["remote"],
        "countries": ["US"],
        "source_budget": 10,
    }
    data.update(overrides)
    return SearchSpec(**data).to_yaml()


class Clock:
    """A clock that ticks forward one second per reading, so ordering is stable."""

    def __init__(self, start: datetime = T0):
        self.moment = start

    def __call__(self) -> datetime:
        self.moment += timedelta(seconds=1)
        return self.moment


@pytest.fixture
def fake(tmp_path) -> FakeHrana:
    return FakeHrana(str(tmp_path / "cloud.sqlite3"))


@pytest.fixture
def open_cloud(fake):
    def opener() -> Store:
        conn = TursoConnection(
            "https://db.example.turso.io", "t", client=httpx.Client(transport=fake.transport())
        )
        return Store.from_connection(conn)

    return opener


@pytest.fixture
def cloud(open_cloud) -> Store:
    store = open_cloud()
    store.migrate()
    store.save_spec("accounting", spec_yaml())
    store.add_company("Acme Corp", "greenhouse", "acme", domain="acme.com", us_signal=True)
    store.add_company("Beta Inc", "lever", "beta", domain="beta.com", us_signal=True)
    store.add_company("Gamma LLC", "ashby", "gamma", domain="gamma.com", us_signal=True)
    return store


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher(routes={
        "boards-api.greenhouse.io": fixture("greenhouse_board"),
        "api.lever.co": fixture("lever_board"),
        "api.ashbyhq.com": fixture("ashby_board"),
    })


def run_locally(local: Store, fetcher: FakeFetcher, name: str = "accounting", when=T0) -> int:
    search_id, yaml = local.get_spec(name)
    spec = SearchSpec.from_yaml(yaml)
    outcome = run_search(
        spec, local, fetcher, search_id=search_id, today=when.date(), now=when,
        only_sources=SOURCES,
    )
    return outcome.run_id


def leased(cloud: Store, when: datetime):
    lease = cloud.take_lease(when)
    assert lease is not None
    return cloud.consume_request(lease, when, "local")


def verdicts(store: Store, search_id: int, run_id: int) -> set[tuple]:
    return {
        (row["identity"], row["decision"], round(row["score"], 3), row["title"])
        for row in store.results(search_id, run_id=run_id, decision=None, limit=1000)
    }


class TestPullAndPublish:
    def test_pull_copies_searches_with_identity_and_the_registry(self, cloud):
        local = Store(":memory:")
        report = pull(cloud, local)
        assert report.searches == 1 and report.boards == 3
        cloud_search = cloud.get_search(name="accounting")
        local_search = local.get_search(name="accounting")
        assert local_search["uid"] == cloud_search["uid"]
        assert local_search["revision"] == cloud_search["revision"] == 0
        assert local.registry_counts() == {"greenhouse": 1, "lever": 1, "ashby": 1}

    def test_a_published_run_is_the_local_run(self, cloud, fetcher):
        local = Store(":memory:")
        pull(cloud, local)
        run_id = run_locally(local, fetcher)
        lease = leased(cloud, T0)

        published = publish_run(local, cloud, run_id, lease, T0 + timedelta(minutes=1))

        assert not published.skipped and published.cloud_run_id
        assert published.matched > 0 and published.new == published.matched
        search = cloud.get_search(name="accounting")
        cloud_run = cloud.latest_completed_run(search["id"])
        assert cloud_run["id"] == published.cloud_run_id
        assert cloud_run["spec_revision"] == 0 and "titles:" in cloud_run["spec_yaml"]
        assert verdicts(cloud, search["id"], cloud_run["id"]) == verdicts(
            local, local.get_search(name="accounting")["id"], run_id
        )
        assert cloud.count_results(search["id"], run_id=cloud_run["id"]) == published.matched
        assert cloud.run_sources(cloud_run["id"])
        assert cloud.get_request(lease.request_id)["status"] == "running"

    def test_a_second_run_from_a_fresh_scratch_finds_nothing_new(self, cloud, fetcher):
        for offset in (0, 1):
            local = Store(":memory:")
            pull(cloud, local)
            when = T0 + timedelta(days=offset)
            run_id = run_locally(local, fetcher, when=when)
            lease = leased(cloud, when)
            published = publish_run(local, cloud, run_id, lease, when + timedelta(minutes=1))
            assert cloud.finish_request(lease, "succeeded", "", 0, when + timedelta(minutes=2))
        search = cloud.get_search(name="accounting")
        runs = cloud.available_runs(search["id"])
        assert len(runs) == 2
        assert runs[1]["new_count"] == published.matched  # the first run
        assert runs[0]["new_count"] == published.new == 0  # the second: all seen before
        assert cloud.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] > 0

    def test_frozen_columns_survive_a_later_change_to_the_job(self, cloud, fetcher):
        local = Store(":memory:")
        pull(cloud, local)
        run_id = run_locally(local, fetcher)
        lease = leased(cloud, T0)
        published = publish_run(local, cloud, run_id, lease, T0)
        search = cloud.get_search(name="accounting")
        before = cloud.results(search["id"], run_id=published.cloud_run_id, limit=1)[0]
        assert before["changed_since"] == 0

        job = next(j for j in local.jobs_for_run(run_id) if j.identity == before["identity"])
        job.description_text = job.description_text + "\n\nUpdate: the role now includes more."
        cloud.upsert_job(job, T0 + timedelta(days=1))

        after = cloud.results(search["id"], run_id=published.cloud_run_id, limit=1)[0]
        assert after["title"] == before["title"] and after["changed_since"] == 1


class TestFailureHalfway:
    def test_a_publish_that_dies_leaves_the_previous_run_intact_and_the_new_one_invisible(
        self, cloud, fetcher, fake
    ):
        local = Store(":memory:")
        pull(cloud, local)
        first = publish_run(local, cloud, run_locally(local, fetcher), leased(cloud, T0), T0)
        search_id = cloud.get_search(name="accounting")["id"]
        baseline = verdicts(cloud, search_id, first.cloud_run_id)

        later = T0 + timedelta(days=1)
        lease = leased(cloud, later)
        second_local = Store(":memory:")
        pull(cloud, second_local)
        run_id = run_locally(second_local, fetcher, when=later)
        fake.fail_on = "INSERT INTO job_search_matches"
        with pytest.raises(TursoError):
            publish_run(second_local, cloud, run_id, lease, later)

        # The screen still reads the first run, and nothing of the second shows.
        assert cloud.latest_completed_run(search_id)["id"] == first.cloud_run_id
        assert [r["id"] for r in cloud.available_runs(search_id)] == [first.cloud_run_id]
        assert verdicts(cloud, search_id, first.cloud_run_id) == baseline
        running = cloud.conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'running'"
        ).fetchone()["n"]
        assert running == 1
        assert cloud.conn.execute(
            "SELECT COUNT(*) AS n FROM job_search_matches WHERE run_id != ?",
            (first.cloud_run_id,),
        ).fetchone()["n"] == 0

        # The next publish clears the wreck and lands the run.
        fake.fail_on = None
        again = T0 + timedelta(days=2)
        lease = leased(cloud, again + timedelta(seconds=LEASE_SECONDS + 1))
        retried = publish_run(second_local, cloud, run_id, lease, again + timedelta(seconds=400))
        assert retried.abandoned == 1 and retried.cloud_run_id
        assert cloud.conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'running'"
        ).fetchone()["n"] == 0
        assert cloud.latest_completed_run(search_id)["id"] == retried.cloud_run_id


class TestSearchIdentity:
    def test_a_deleted_and_recreated_search_receives_nothing(self, cloud, fetcher):
        local = Store(":memory:")
        pull(cloud, local)
        run_id = run_locally(local, fetcher)
        old_uid = cloud.get_search(name="accounting")["uid"]
        cloud.delete_search(old_uid)
        cloud.save_spec("accounting", spec_yaml())
        new = cloud.get_search(name="accounting")
        assert new["uid"] != old_uid

        published = publish_run(local, cloud, run_id, leased(cloud, T0), T0)
        assert published.skipped and "no longer exists" in published.skipped
        assert cloud.runs_for(new["id"]) == []

    def test_an_edit_during_the_run_is_recorded_on_the_run(self, cloud, fetcher):
        local = Store(":memory:")
        pull(cloud, local)
        cloud.save_spec("accounting", spec_yaml(excluded_titles=["Staff Accountant", "Clerk"]))
        run_id = run_locally(local, fetcher)
        published = publish_run(local, cloud, run_id, leased(cloud, T0), T0)
        assert published.revision_moved and not published.skipped
        run = cloud.get_run(published.cloud_run_id)
        assert run["spec_revision"] == 0
        assert cloud.get_search(name="accounting")["revision"] == 1
        assert "Clerk" not in run["spec_yaml"]


class TestLeaseGuardOverHttp:
    def test_a_stale_lease_cannot_publish_anything(self, cloud, fetcher):
        local = Store(":memory:")
        pull(cloud, local)
        run_id = run_locally(local, fetcher)
        lease = leased(cloud, T0)
        stale = T0 + timedelta(seconds=LEASE_SECONDS + 60)
        with pytest.raises(LeaseLost):
            publish_run(local, cloud, run_id, lease, stale)
        assert cloud.conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
        assert cloud.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


class TestRunCloud:
    def test_the_runner_consumes_the_queued_request_and_publishes_its_search_first(
        self, cloud, open_cloud, fetcher
    ):
        cloud.save_spec("second", spec_yaml("second", titles=["Controller"]))
        priority = cloud.get_search(name="second")["uid"]
        request_id, _ = cloud.create_queued("web", priority, T0)
        lines: list[str] = []

        outcome = run_cloud(
            cloud, open_cloud, lambda: fetcher, origin="schedule", doctor=False,
            clock=Clock(), heartbeat_seconds=0.05, log=lines.append,
        )

        assert outcome.exit_code == 0 and outcome.request_id == request_id
        request = cloud.get_request(request_id)
        assert request["status"] == "succeeded" and request["rows_written"] > 0
        assert "rows written" in request["note"]
        first = cloud.latest_completed_run(cloud.get_search(name="second")["id"])
        second = cloud.latest_completed_run(cloud.get_search(name="accounting")["id"])
        assert first["id"] < second["id"]  # the requester's search was published first
        assert first["request_id"] == second["request_id"] == request_id
        assert any("accounting:" in line for line in lines)

    def test_a_runner_that_loses_its_lease_stops_and_never_reports_success(
        self, cloud, open_cloud, fetcher
    ):
        clock = Clock()
        thief = open_cloud()

        class StealingFetcher(FakeFetcher):
            """Takes the lease away during the fan-out, as a second runner would after
            this one's heartbeat had gone quiet for too long."""

            def get(self, url, *, params=None, headers=None):
                if not getattr(self, "stolen", False):
                    self.stolen = True
                    assert thief.take_lease(clock.moment + timedelta(seconds=LEASE_SECONDS + 5))
                return super().get(url, params=params, headers=headers)

        stealing = StealingFetcher(routes=fetcher.routes)
        outcome = run_cloud(
            cloud, open_cloud, lambda: stealing, origin="schedule", doctor=False,
            clock=clock, heartbeat_seconds=0.05, log=lambda _line: None,
        )
        assert outcome.exit_code == 1
        request = cloud.get_request(outcome.request_id)
        assert request["status"] == "failed" and "taken over" in request["note"]
        assert cloud.conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'completed'"
        ).fetchone()["n"] == 0
