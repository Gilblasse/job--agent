"""Who runs the searches when "Search jobs" is pressed.

Locally the website is the runner: the in-process dispatcher runs the same path the
command line does -- lease, queue, engine, publish -- so the two are one system. On the
cloud it is GitHub, or nobody until the schedule. The choice is by environment and it
must be the right one, because a wrong choice is a request that waits forever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobagent.domain.spec import SearchSpec
from jobagent.infra.store import Store
from jobagent.web import app as web
from jobagent.web.dispatch import (
    GitHubDispatcher,
    NullDispatcher,
    ThreadDispatcher,
    dispatcher_from_env,
)
from tests.fakes import FakeFetcher

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PASSWORD = "pw"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fetcher() -> FakeFetcher:
    return FakeFetcher(routes={
        "boards-api.greenhouse.io": fixture("greenhouse_board"),
        "api.lever.co": fixture("lever_board"),
        "api.ashbyhq.com": fixture("ashby_board"),
    })


class TestChoosingTheRunner:
    def test_local_sqlite_runs_in_process(self, monkeypatch):
        for name in ("TURSO_DATABASE_URL", "JOBAGENT_GITHUB_TOKEN", "JOBAGENT_DISPATCHER"):
            monkeypatch.delenv(name, raising=False)
        assert isinstance(dispatcher_from_env(lambda: None), ThreadDispatcher)

    def test_github_when_credentials_are_present(self, monkeypatch):
        monkeypatch.setenv("JOBAGENT_GITHUB_TOKEN", "t")
        monkeypatch.setenv("JOBAGENT_GITHUB_REPO", "o/r")
        monkeypatch.delenv("JOBAGENT_DISPATCHER", raising=False)
        assert isinstance(dispatcher_from_env(lambda: None), GitHubDispatcher)

    def test_cloud_without_github_waits_for_the_schedule(self, monkeypatch):
        monkeypatch.setenv("TURSO_DATABASE_URL", "https://x.turso.io")
        for name in ("JOBAGENT_GITHUB_TOKEN", "JOBAGENT_GITHUB_REPO", "JOBAGENT_DISPATCHER"):
            monkeypatch.delenv(name, raising=False)
        assert isinstance(dispatcher_from_env(lambda: None), NullDispatcher)

    def test_the_override_wins(self, monkeypatch):
        monkeypatch.setenv("TURSO_DATABASE_URL", "https://x.turso.io")
        monkeypatch.setenv("JOBAGENT_DISPATCHER", "thread")
        assert isinstance(dispatcher_from_env(lambda: None), ThreadDispatcher)
        monkeypatch.setenv("JOBAGENT_DISPATCHER", "none")
        assert isinstance(dispatcher_from_env(lambda: None), NullDispatcher)
        monkeypatch.setenv("JOBAGENT_DISPATCHER", "github")
        with pytest.raises(RuntimeError, match="JOBAGENT_GITHUB_TOKEN"):
            dispatcher_from_env(lambda: None)


class TestSearchJobsRunsTheSearch:
    def test_pressing_search_jobs_produces_a_completed_run_with_results(
        self, tmp_path, monkeypatch
    ):
        """The whole loop through the API: create a search, press Search jobs, and the
        in-process runner seeds the registry, runs the engine and publishes -- the
        website did what the command line does."""
        path = tmp_path / "site.sqlite3"
        seed = Store(path)
        seed.add_company("Acme Corp", "greenhouse", "acme", domain="acme.com", us_signal=True)
        seed.add_company("Beta Inc", "lever", "beta", domain="beta.com", us_signal=True)
        seed.close()
        monkeypatch.setenv("JOBAGENT_WEB_PASSWORD", PASSWORD)
        dispatcher = ThreadDispatcher(
            lambda: Store(path), fetcher, doctor=False, heartbeat_seconds=0.05,
            log=lambda _line: None,
        )
        web.app.state.store_factory = lambda: Store(path)
        web.app.state.dispatcher = dispatcher
        try:
            client = TestClient(web.app)
            client.headers["Authorization"] = f"Bearer {PASSWORD}"
            spec = SearchSpec(name="ap", titles=["Accounts Payable", "Accountant"], source_budget=5)
            uid = client.post("/api/searches", json=spec.model_dump(mode="json")).json()["uid"]
            assert client.get(f"/api/searches/{uid}/results").json()["run"] is None

            request = client.post("/api/runs", json={"priority_uid": uid}).json()
            assert request["status"] == "queued"
            dispatcher.join(timeout=60)

            done = client.get(f"/api/runs/{request['id']}").json()
            assert done["status"] == "succeeded", done["note"]
            results = client.get(f"/api/searches/{uid}/results").json()
            assert results["run"]["status"] == "completed" and results["total"] > 0
            assert results["items"][0]["title"]
            status = client.get(f"/api/searches/{uid}/status").json()
            assert status["queued_request"] is None and status["active_request"] is None
        finally:
            web.app.state.store_factory = web.store_from_env
            web.app.state.dispatcher = None

    def test_an_empty_registry_is_seeded_before_the_run(self, tmp_path):
        from jobagent.engine.cloud_run import run_cloud

        path = tmp_path / "fresh.sqlite3"
        Store(path).save_spec("ap", SearchSpec(name="ap", titles=["Accountant"]).to_yaml())
        assert Store(path).registry_counts() == {}
        outcome = run_cloud(
            Store(path), lambda: Store(path), fetcher, origin="web", doctor=False,
            heartbeat_seconds=0.05, log=lambda _line: None,
        )
        assert outcome.exit_code == 0
        counts = Store(path).registry_counts()
        assert sum(counts.values()) > 1000 and "greenhouse" in counts
