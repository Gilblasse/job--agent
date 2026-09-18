"""The web API, driven through FastAPI's test client over a seeded SQLite store.

Every endpoint is called at least once, because the API is where a bad column name or
a wrong status code hides. The rules that matter most get their own tests: a screen
pinned to one run, an expired run refused, a dismissal that is all-or-nothing, and one
queued request however many times the button is pressed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobagent.domain.spec import SearchSpec
from jobagent.engine.orchestrator import run_search
from jobagent.infra.store import Store
from jobagent.web import app as web
from tests.conftest import make_job
from tests.fakes import FakeFetcher

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
PASSWORD = "open-sesame"
SOURCES = ["greenhouse", "lever", "ashby"]
T0 = datetime(2026, 9, 14, 12, 0, 0)


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class CountingDispatcher:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


class FailingDispatcher:
    def wake(self) -> None:
        raise RuntimeError("github is down")


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher(routes={
        "boards-api.greenhouse.io": fixture("greenhouse_board"),
        "api.lever.co": fixture("lever_board"),
        "api.ashbyhq.com": fixture("ashby_board"),
    })


def run_once(store: Store, fetcher: FakeFetcher, name: str, when: datetime) -> int:
    search_id, yaml = store.get_spec(name)
    spec = SearchSpec.from_yaml(yaml)
    return run_search(
        spec, store, fetcher, search_id=search_id, today=when.date(), now=when,
        only_sources=SOURCES,
    ).run_id


@pytest.fixture
def store(tmp_path, fetcher) -> Store:
    store = Store(tmp_path / "web.sqlite3")
    spec = SearchSpec.from_yaml((EXAMPLES / "benchmark-accounting.yml").read_text())
    store.save_spec(spec.name, spec.to_yaml())
    store.add_company("Acme Corp", "greenhouse", "acme", domain="acme.com", us_signal=True)
    store.add_company("Beta Inc", "lever", "beta", domain="beta.com", us_signal=True)
    store.add_company("Gamma LLC", "ashby", "gamma", domain="gamma.com", us_signal=True)
    run_once(store, fetcher, spec.name, T0)
    return store


@pytest.fixture
def client(store, monkeypatch) -> TestClient:
    monkeypatch.setenv("JOBAGENT_WEB_PASSWORD", PASSWORD)
    web.app.state.store_factory = lambda: store
    web.app.state.dispatcher = CountingDispatcher()
    client = TestClient(web.app)
    client.headers["Authorization"] = f"Bearer {PASSWORD}"
    yield client
    web.app.state.store_factory = web.store_from_env
    web.app.state.dispatcher = None


def search_uid(client: TestClient) -> str:
    return client.get("/api/searches").json()[0]["uid"]


class TestAuth:
    def test_health_needs_no_password(self, client):
        assert client.get("/api/health", headers={"Authorization": ""}).status_code == 200

    def test_missing_or_wrong_password_is_refused(self, client):
        assert client.get("/api/searches", headers={"Authorization": ""}).status_code == 401
        wrong = client.get("/api/searches", headers={"Authorization": "Bearer no"})
        assert wrong.status_code == 401

    def test_an_unconfigured_password_fails_closed(self, client, monkeypatch):
        monkeypatch.delenv("JOBAGENT_WEB_PASSWORD")
        assert client.get("/api/searches").status_code == 503


class TestSearches:
    def test_list_and_get(self, client):
        listed = client.get("/api/searches").json()
        assert listed[0]["name"] == "accounting-remote" and listed[0]["revision"] == 0
        assert listed[0]["runs"] == 1
        detail = client.get(f"/api/searches/{listed[0]['uid']}").json()
        assert "Accounts Payable" in detail["spec"]["titles"]
        assert detail["revision"] == 0

    def test_update_bumps_the_revision_and_a_stale_update_is_refused(self, client):
        uid = search_uid(client)
        detail = client.get(f"/api/searches/{uid}").json()
        spec = detail["spec"]
        spec["excluded_titles"].append("Clerk")
        saved = client.put(f"/api/searches/{uid}", json={"spec": spec, "revision": 0})
        assert saved.status_code == 200 and saved.json()["revision"] == 1
        stale = client.put(f"/api/searches/{uid}", json={"spec": spec, "revision": 0})
        assert stale.status_code == 409 and stale.json()["detail"]["revision"] == 1
        assert "Clerk" in client.get(f"/api/searches/{uid}").json()["spec"]["excluded_titles"]

    def test_create_import_yaml_and_delete(self, client):
        created = client.post("/api/searches", json={"name": "second", "titles": ["Controller"]})
        assert created.status_code == 201
        duplicate = client.post("/api/searches", json={"name": "second", "titles": ["X"]})
        assert duplicate.status_code == 409
        uid = created.json()["uid"]
        yaml = client.get(f"/api/searches/{uid}/yaml")
        assert yaml.status_code == 200 and "titles:" in yaml.text
        imported = client.post(
            "/api/searches/import", json={"yaml": "name: third\ntitles: [Analyst]\n"}
        )
        assert imported.status_code == 201 and imported.json()["name"] == "third"
        bad = client.post("/api/searches/import", json={"yaml": "countries: [FR]\nname: x"})
        assert bad.status_code == 422
        assert client.delete(f"/api/searches/{uid}").status_code == 204
        assert client.get(f"/api/searches/{uid}").status_code == 404

    def test_a_spec_that_fails_validation_is_a_422(self, client):
        uid = search_uid(client)
        response = client.put(
            f"/api/searches/{uid}", json={"spec": {"name": "x", "countries": ["FR"]}, "revision": 0}
        )
        assert response.status_code == 422


class TestResults:
    def test_one_run_paged_with_previews(self, client, store):
        uid = search_uid(client)
        first = client.get(f"/api/searches/{uid}/results", params={"limit": 2}).json()
        assert first["run"]["id"] and first["run"]["stale"] is False
        assert first["total"] == store.get_run(first["run"]["id"])["matched"]
        assert len(first["items"]) == min(2, first["total"])
        item = first["items"][0]
        assert "description_text" not in item and "preview" in item
        assert item["explanation"]["gates"]
        assert item["is_new"] is True

        seen: list[int] = []
        offset = 0
        while True:
            page = client.get(
                f"/api/searches/{uid}/results", params={"limit": 2, "offset": offset}
            ).json()
            if not page["items"]:
                break
            seen.extend(i["id"] for i in page["items"])
            offset += 2
        assert len(seen) == len(set(seen)) == first["total"]

    def test_the_screen_stays_on_its_run_while_a_newer_one_completes(self, client, store, fetcher):
        uid = search_uid(client)
        pinned = client.get(f"/api/searches/{uid}/results").json()["run"]["id"]
        newer = run_once(store, fetcher, "accounting-remote", T0 + timedelta(days=1))
        assert newer != pinned
        still = client.get(f"/api/searches/{uid}/results", params={"run": pinned}).json()
        assert still["run"]["id"] == pinned
        assert all(item["run_id"] == pinned for item in still["items"])
        assert client.get(f"/api/searches/{uid}/results").json()["run"]["id"] == newer

    def test_rejections_and_sorts(self, client):
        uid = search_uid(client)
        rejected = client.get(
            f"/api/searches/{uid}/results", params={"decision": "rejected", "sort": "title"}
        ).json()
        assert rejected["total"] > 0
        titles = [item["title"] for item in rejected["items"]]
        assert titles == sorted(titles)
        assert all(item["explanation"]["gates"] for item in rejected["items"])

    def test_an_expired_run_answers_410_everywhere(self, client, store, fetcher):
        uid = search_uid(client)
        first = client.get(f"/api/searches/{uid}/results").json()["run"]["id"]
        for day in range(1, 4):
            run_once(store, fetcher, "accounting-remote", T0 + timedelta(days=day))
        gone = client.get(f"/api/searches/{uid}/results", params={"run": first})
        assert gone.status_code == 410
        assert gone.json()["detail"]["expired"] is True
        assert gone.json()["detail"]["latest_run"]["id"] > first
        assert client.get(f"/api/searches/{uid}/export", params={"run": first}).status_code == 410
        job_id = client.get(f"/api/searches/{uid}/results").json()["items"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}", params={"search": uid, "run": first})
        assert detail.status_code == 410
        assert client.get(f"/api/searches/{uid}/results", params={"run": 999}).status_code == 404

    def test_export_formats(self, client):
        uid = search_uid(client)
        for fmt, marker in (("csv", "id,title"), ("json", "["), ("md", "# Job search results")):
            response = client.get(f"/api/searches/{uid}/export", params={"format": fmt})
            assert response.status_code == 200 and marker in response.text
        unknown = client.get(f"/api/searches/{uid}/export", params={"format": "xml"})
        assert unknown.status_code == 422

    def test_a_search_that_never_ran_has_no_run(self, client):
        uid = client.post("/api/searches", json={"name": "fresh", "titles": ["X"]}).json()["uid"]
        assert client.get(f"/api/searches/{uid}/results").json() == {
            "run": None, "total": 0, "offset": 0, "items": [],
        }


class TestStatus:
    def test_reports_the_latest_run_coverage_and_requests(self, client):
        uid = search_uid(client)
        status = client.get(f"/api/searches/{uid}/status").json()
        assert status["latest_run"]["id"] and status["coverage"]
        assert status["active_request"] is None and status["queued_request"] is None
        assert status["stale"] is False
        client.post("/api/runs", json={"priority_uid": uid})
        status = client.get(f"/api/searches/{uid}/status").json()
        assert status["queued_request"]["priority_uid"] == uid

    def test_editing_the_rules_marks_the_results_stale(self, client):
        uid = search_uid(client)
        spec = client.get(f"/api/searches/{uid}").json()["spec"]
        spec["keywords"].append("ledger")
        client.put(f"/api/searches/{uid}", json={"spec": spec, "revision": 0})
        assert client.get(f"/api/searches/{uid}/status").json()["stale"] is True
        runs = client.get(f"/api/searches/{uid}/runs").json()
        assert runs[0]["stale"] is True

    def test_a_search_that_never_ran(self, client):
        uid = client.post("/api/searches", json={"name": "fresh", "titles": ["X"]}).json()["uid"]
        status = client.get(f"/api/searches/{uid}/status").json()
        assert status["latest_run"] is None and status["available_runs"] == []


class TestJobs:
    def test_detail_in_context_carries_the_runs_verdict_and_the_postings_sections(self, client):
        uid = search_uid(client)
        results = client.get(f"/api/searches/{uid}/results").json()
        job_id = results["items"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}", params={"search": uid}).json()
        assert detail["verdict"]["decision"] == "match"
        assert detail["explanation"]["gates"]
        assert detail["run"]["id"] == results["run"]["id"]
        assert detail["sections"] and isinstance(detail["sections"][0]["lines"], list)
        assert detail["changed_since"] is False
        assert detail["status"] == "new"
        assert any(r["kind"] == "employer" for r in detail["suggested_rules"])
        assert detail["sources"]

    def test_detail_without_context_has_facts_only(self, client):
        uid = search_uid(client)
        job_id = client.get(f"/api/searches/{uid}/results").json()["items"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["verdict"] is None and detail["run"] is None
        assert client.get("/api/jobs/999999").status_code == 404

    def test_a_job_without_a_verdict_in_that_run_is_a_404(self, client, store):
        uid = search_uid(client)
        stray = store.upsert_job(make_job(title="Stray Role"), T0)
        assert client.get(f"/api/jobs/{stray}", params={"search": uid}).status_code == 404

    def test_status_and_saved(self, client):
        uid = search_uid(client)
        job_id = client.get(f"/api/searches/{uid}/results").json()["items"][0]["id"]
        saved_ok = client.post(f"/api/jobs/{job_id}/status", json={"status": "saved"})
        assert saved_ok.status_code == 200
        bad = client.post(f"/api/jobs/{job_id}/status", json={"status": "nope"})
        assert bad.status_code == 422
        saved = client.get("/api/saved").json()
        assert [item["id"] for item in saved] == [job_id]
        assert saved[0]["user_status"] == "saved"


class TestDismiss:
    def test_a_dismissal_teaches_the_search_in_one_step(self, client):
        uid = search_uid(client)
        results = client.get(f"/api/searches/{uid}/results").json()
        job = results["items"][0]
        response = client.post(f"/api/jobs/{job['id']}/dismiss", json={
            "search_uid": uid, "revision": 0, "reason": "agency work",
            "rules": [{"kind": "employer"}, {"kind": "duty", "value": "cold calling"}],
        })
        assert response.status_code == 200
        body = response.json()
        assert body["spec_changed"] and body["revision"] == 1
        spec = client.get(f"/api/searches/{uid}").json()["spec"]
        assert job["company"] in spec["excluded_companies"]
        assert "cold calling" in spec["responsibilities_exclude"]
        detail = client.get(f"/api/jobs/{job['id']}", params={"search": uid}).json()
        assert detail["status"] == "dismissed" and detail["note"] == "agency work"
        dismissed = client.get(f"/api/searches/{uid}/dismissed").json()
        assert dismissed[0]["job_id"] == job["id"] and len(dismissed[0]["rules"]) == 2
        hidden = client.get(f"/api/searches/{uid}/results").json()
        assert job["id"] not in [item["id"] for item in hidden["items"]]
        shown = client.get(f"/api/searches/{uid}/results", params={"all": "true"}).json()
        assert job["id"] in [item["id"] for item in shown["items"]]

    def test_a_conflicting_title_rule_writes_nothing(self, client):
        uid = search_uid(client)
        job = client.get(f"/api/searches/{uid}/results").json()["items"][0]
        response = client.post(f"/api/jobs/{job['id']}/dismiss", json={
            "search_uid": uid, "revision": 0, "reason": "x",
            "rules": [{"kind": "title", "value": "Accountant"}],
        })
        assert response.status_code == 400 and "also appears" in response.json()["detail"]
        assert client.get(f"/api/searches/{uid}").json()["revision"] == 0
        assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "new"

    def test_a_stale_revision_writes_nothing(self, client):
        uid = search_uid(client)
        job = client.get(f"/api/searches/{uid}/results").json()["items"][0]
        response = client.post(f"/api/jobs/{job['id']}/dismiss", json={
            "search_uid": uid, "revision": 5, "reason": "x", "rules": [{"kind": "employer"}],
        })
        assert response.status_code == 409
        assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "new"
        assert client.get(f"/api/searches/{uid}/dismissed").json() == []

    def test_a_failure_halfway_leaves_spec_and_status_untouched(self, client, store, monkeypatch):
        uid = search_uid(client)
        job = client.get(f"/api/searches/{uid}/results").json()["items"][0]

        def explode(*args, **kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(store, "record_feedback", explode)
        with pytest.raises(RuntimeError):
            client.post(f"/api/jobs/{job['id']}/dismiss", json={
                "search_uid": uid, "revision": 0, "reason": "x", "rules": [{"kind": "employer"}],
            })
        assert client.get(f"/api/searches/{uid}").json()["revision"] == 0
        assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "new"


class TestRunRequests:
    def test_one_queued_request_and_one_wake_up_however_often_it_is_pressed(self, client):
        uid = search_uid(client)
        first = client.post("/api/runs", json={"priority_uid": uid})
        assert first.status_code == 202 and first.json()["attached"] is False
        second = client.post("/api/runs", json={})
        assert second.json()["attached"] is True and second.json()["id"] == first.json()["id"]
        assert web.app.state.dispatcher.wakes == 1
        listed = client.get("/api/runs").json()
        assert [r["status"] for r in listed] == ["queued"]
        assert client.get(f"/api/runs/{first.json()['id']}").json()["status"] == "queued"

    def test_cancel_frees_the_queue(self, client):
        request_id = client.post("/api/runs", json={}).json()["id"]
        assert client.post(f"/api/runs/{request_id}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/api/runs/{request_id}/cancel").status_code == 409
        fresh = client.post("/api/runs", json={}).json()
        assert fresh["id"] != request_id and fresh["attached"] is False

    def test_a_failed_wake_up_is_recorded_and_does_not_block_the_queue(self, client):
        web.app.state.dispatcher = FailingDispatcher()
        response = client.post("/api/runs", json={})
        assert response.status_code == 502
        listed = client.get("/api/runs").json()
        assert listed[0]["status"] == "failed" and "wake" in listed[0]["note"]
        web.app.state.dispatcher = CountingDispatcher()
        assert client.post("/api/runs", json={}).json()["attached"] is False

    def test_unknown_request(self, client):
        assert client.get("/api/runs/nope").status_code == 404


class TestSourcesAndRegistry:
    def test_sources(self, client):
        body = client.get("/api/sources").json()
        assert any(s["name"] == "greenhouse" for s in body["sources"])
        assert any(e["name"] == "smartrecruiters" for e in body["excluded"])

    def test_registry_lists_and_adds(self, client):
        body = client.get("/api/registry").json()
        assert body["counts"] == {"greenhouse": 1, "lever": 1, "ashby": 1}
        added = client.post(
            "/api/registry", json={"url": "https://boards.greenhouse.io/newco", "name": "NewCo"}
        )
        assert added.status_code == 201
        assert client.get("/api/registry").json()["counts"]["greenhouse"] == 2
