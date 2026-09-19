"""The web API.

A second adapter over the same engine: the search screen reads one available run of a
search, verdicts frozen; edits carry a revision; a dismissal is one transaction; and
"Search jobs" queues a request that the runner consumes. Nothing here evaluates a
posting -- that is the runner's job -- and nothing renders a posting as HTML.

The database is Turso when ``TURSO_DATABASE_URL`` is set, else a local SQLite file
(``JOBAGENT_DB``), which is how tests, local development and the acceptance test run
with no cloud at all.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from ..cli.export import FORMATS, load_explanation
from ..domain.feedback import DismissRule, apply_rules, complete_rules, describe, suggest_rules
from ..domain.gates import split_sections
from ..domain.models import UserStatus
from ..domain.spec import SearchSpec
from ..domain.taxonomy import Taxonomy
from ..engine.verify import verify_jobs
from ..infra.http import HttpFetcher
from ..infra.store import DEFAULT_DB_PATH, RevisionConflict, SearchGone, Store
from ..infra.turso import TursoConnection
from ..sources.catalog import CATALOG, EXCLUDED
from ..sources.registry import add_from_url
from .dispatch import Dispatcher, dispatcher_from_env

PREVIEW_CHARS = 200
PAGE_MAX = 100
WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


# ------------------------------------------------------------------ wiring


def store_from_env() -> Store:
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if url and token:
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        return Store.from_connection(TursoConnection(url, token))
    return Store(os.environ.get("JOBAGENT_DB") or DEFAULT_DB_PATH)


app = FastAPI(title="jobagent", docs_url=None, redoc_url=None)
app.state.store_factory = store_from_env
app.state.dispatcher = None  # resolved lazily so tests and the e2e server can replace it


def get_store(request: Request) -> Store:
    return request.app.state.store_factory()


def get_dispatcher(request: Request) -> Dispatcher:
    if request.app.state.dispatcher is None:
        request.app.state.dispatcher = dispatcher_from_env(request.app.state.store_factory)
    return request.app.state.dispatcher


def require_password(request: Request) -> None:
    expected = os.environ.get("JOBAGENT_WEB_PASSWORD", "")
    if not expected:
        # Fail closed: an unconfigured deployment is not an open one.
        raise HTTPException(503, "JOBAGENT_WEB_PASSWORD is not configured")
    header = request.headers.get("authorization", "")
    given = header[7:] if header.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "invalid password")


api = APIRouter(prefix="/api", dependencies=[Depends(require_password)])


# ------------------------------------------------------------------ bodies


class SpecUpdate(BaseModel):
    spec: SearchSpec
    revision: int


class ImportBody(BaseModel):
    yaml: str


class StatusBody(BaseModel):
    status: str
    note: str = ""


class RuleBody(BaseModel):
    kind: str
    value: str = ""


class DismissBody(BaseModel):
    search_uid: str
    revision: int
    reason: str = Field(min_length=1)
    rules: list[RuleBody] = Field(default_factory=list)


class RunBody(BaseModel):
    priority_uid: str | None = None


class RegistryBody(BaseModel):
    url: str
    name: str | None = None


# ------------------------------------------------------------------ helpers


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _preview(text: str) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= PREVIEW_CHARS:
        return flat
    cut = flat[:PREVIEW_CHARS]
    return cut[: cut.rfind(" ")] + "…" if " " in cut else cut + "…"


def _item(row: Any) -> dict[str, Any]:
    data = _row(row)
    description = data.pop("description_text", "") or ""
    data["preview"] = _preview(description)
    data["explanation"] = load_explanation(row)
    data["is_new"] = bool(data.get("is_new"))
    data["uncertain"] = bool(data.get("uncertain"))
    data["changed_since"] = bool(data.get("changed_since"))
    return data


def _search_or_404(store: Store, uid: str) -> Any:
    search = store.get_search(uid=uid)
    if search is None:
        raise HTTPException(404, "no such search")
    return search


def _search_summary(search: Any) -> dict[str, Any]:
    return {
        "uid": search["uid"], "name": search["name"], "revision": int(search["revision"]),
        "updated_at": search["updated_at"],
    }


def _run_summary(run: Any, search: Any) -> dict[str, Any] | None:
    if run is None:
        return None
    data = _row(run)
    data.pop("spec_yaml", None)
    data["stale"] = (
        run["spec_revision"] is not None and int(run["spec_revision"]) != int(search["revision"])
    )
    return data


def _request_summary(row: Any) -> dict[str, Any] | None:
    return _row(row) if row is not None else None


def _resolve_run(store: Store, search: Any, run: int | None) -> Any:
    """The run a screen may read, or the right refusal."""
    search_id = int(search["id"])
    if run is None:
        return store.latest_completed_run(search_id)
    availability = store.run_availability(search_id, run)
    if availability == "available":
        return store.get_run(run)
    if availability == "expired":
        latest = store.latest_completed_run(search_id)
        raise HTTPException(
            410, {"expired": True, "latest_run": _run_summary(latest, search)}
        )
    raise HTTPException(404, "no such run for this search")


def _statuses(status: str | None) -> list[str] | None:
    if not status:
        return None
    return [part.strip() for part in status.split(",") if part.strip()]


# ------------------------------------------------------------------ searches


@api.get("/searches")
def list_searches(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return [_row(row) for row in store.list_specs()]


@api.post("/searches", status_code=201)
def create_search(spec: SearchSpec, store: Store = Depends(get_store)) -> dict[str, Any]:
    if store.get_search(name=spec.name) is not None:
        raise HTTPException(409, f"a search named {spec.name!r} already exists")
    store.save_spec(spec.name, spec.to_yaml())
    return _search_summary(store.get_search(name=spec.name))


@api.post("/searches/import", status_code=201)
def import_search(body: ImportBody, store: Store = Depends(get_store)) -> dict[str, Any]:
    try:
        spec = SearchSpec.from_yaml(body.yaml)
    except (ValueError, ValidationError) as error:
        raise HTTPException(422, str(error)) from None
    store.save_spec(spec.name, spec.to_yaml())
    return _search_summary(store.get_search(name=spec.name))


@api.get("/searches/{uid}")
def get_search(uid: str, store: Store = Depends(get_store)) -> dict[str, Any]:
    search = _search_or_404(store, uid)
    spec = SearchSpec.from_yaml(search["spec_yaml"])
    return {**_search_summary(search), "spec": spec.model_dump(mode="json")}


@api.put("/searches/{uid}")
def update_search(uid: str, body: SpecUpdate, store: Store = Depends(get_store)) -> dict[str, Any]:
    _search_or_404(store, uid)
    try:
        revision = store.update_search(
            uid, name=body.spec.name, spec_yaml=body.spec.to_yaml(),
            expected_revision=body.revision,
        )
    except RevisionConflict as conflict:
        raise HTTPException(409, {"revision": conflict.current}) from None
    except SearchGone:
        raise HTTPException(404, "no such search") from None
    except Exception as error:  # noqa: BLE001 - a name collision is the only other cause
        if "UNIQUE" in str(error).upper():
            raise HTTPException(409, f"a search named {body.spec.name!r} already exists") from None
        raise
    return {"uid": uid, "name": body.spec.name, "revision": revision}


@api.delete("/searches/{uid}", status_code=204)
def delete_search(uid: str, store: Store = Depends(get_store)) -> Response:
    if not store.delete_search(uid):
        raise HTTPException(404, "no such search")
    return Response(status_code=204)


@api.get("/searches/{uid}/yaml")
def search_yaml(uid: str, store: Store = Depends(get_store)) -> PlainTextResponse:
    search = _search_or_404(store, uid)
    return PlainTextResponse(SearchSpec.from_yaml(search["spec_yaml"]).to_yaml())


@api.get("/searches/{uid}/results")
def results(
    uid: str,
    run: int | None = Query(None),
    sort: str = Query("score"),
    decision: str = Query("match"),
    status: str | None = Query(None),
    min_score: float | None = Query(None),
    all: bool = Query(False),
    limit: int = Query(25, ge=1, le=PAGE_MAX),
    offset: int = Query(0, ge=0),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    search = _search_or_404(store, uid)
    run_row = _resolve_run(store, search, run)
    if run_row is None:
        return {"run": None, "total": 0, "offset": offset, "items": []}
    filters = dict(
        decision=decision, statuses=_statuses(status), min_score=min_score,
        include_dismissed=all,
    )
    rows = store.results(
        int(search["id"]), run_id=int(run_row["id"]), limit=limit, offset=offset, order=sort,
        **filters,
    )
    total = store.count_results(int(search["id"]), run_id=int(run_row["id"]), **filters)
    # The coverage of THIS run, so the screen never describes one run with another's.
    summary = _run_summary(run_row, search) or {}
    summary["coverage"] = [_row(r) for r in store.run_sources(int(run_row["id"]))]
    return {"run": summary, "total": total, "offset": offset, "items": [_item(row) for row in rows]}


@api.get("/searches/{uid}/export")
def export(
    uid: str,
    run: int | None = Query(None),
    format: str = Query("csv"),
    decision: str = Query("match"),
    store: Store = Depends(get_store),
) -> Response:
    if format not in FORMATS:
        raise HTTPException(422, f"format must be one of {', '.join(FORMATS)}")
    search = _search_or_404(store, uid)
    run_row = _resolve_run(store, search, run)
    rows = [] if run_row is None else store.results(
        int(search["id"]), run_id=int(run_row["id"]), decision=decision, limit=5000
    )
    media = {"csv": "text/csv", "json": "application/json", "md": "text/markdown"}[format]
    name = f"{search['name']}-{decision}.{format}"
    return Response(
        FORMATS[format](rows), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@api.get("/searches/{uid}/status")
def status(uid: str, store: Store = Depends(get_store)) -> dict[str, Any]:
    search = _search_or_404(store, uid)
    now = datetime.now()
    store.reconcile_requests(now)
    latest = store.latest_completed_run(int(search["id"]))
    requests = store.list_requests(1)
    return {
        "search": _search_summary(search),
        "latest_run": _run_summary(latest, search),
        "coverage": [_row(r) for r in store.run_sources(int(latest["id"]))] if latest else [],
        "available_runs": [
            _run_summary(r, search) for r in store.available_runs(int(search["id"]))
        ],
        "active_request": _request_summary(store.active_request(now)),
        "queued_request": _request_summary(store.queued_request()),
        "latest_request": _request_summary(requests[0]) if requests else None,
        "stale": bool(latest and _run_summary(latest, search)["stale"]),
    }


@api.get("/searches/{uid}/runs")
def runs(uid: str, store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    search = _search_or_404(store, uid)
    return [_run_summary(r, search) for r in store.runs_for(int(search["id"]))]


@api.get("/searches/{uid}/dismissed")
def dismissed(uid: str, store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    search = _search_or_404(store, uid)
    out = []
    for row in store.feedback(int(search["id"])):
        data = _row(row)
        rules = [DismissRule(r["kind"], r["value"]) for r in json.loads(data.pop("rules_json"))]
        data["rules"] = [{"kind": r.kind, "value": r.value, "label": describe(r)} for r in rules]
        out.append(data)
    return out


@api.post("/searches/{uid}/verify")
def verify(
    uid: str, limit: int = Query(25, ge=1, le=100), store: Store = Depends(get_store)
) -> dict[str, Any]:
    search = _search_or_404(store, uid)
    rows = store.results(int(search["id"]), limit=limit)
    with HttpFetcher() as fetcher:
        summary = verify_jobs(store, fetcher, [int(r["id"]) for r in rows])
    return {
        "checked": summary.checked, "live": summary.live, "gone": summary.gone,
        "inconclusive": summary.inconclusive, "text": summary.describe(),
    }


# ------------------------------------------------------------------ jobs


@api.get("/jobs/{job_id}")
def job_detail(
    job_id: int,
    search: str | None = Query(None),
    run: int | None = Query(None),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    taxonomy = Taxonomy.default()
    facts = _row(job)
    description = facts.pop("description_text", "") or ""
    out: dict[str, Any] = {
        "job": facts,
        "sections": [
            {"heading": section.heading, "lines": list(section.lines)}
            for section in split_sections(description, taxonomy)
        ],
        "sources": [_row(r) for r in store.job_source_refs(job_id)],
        "suggested_rules": [
            {"kind": r.kind, "value": r.value, "label": describe(r)}
            for r in suggest_rules(job["title"], job["company"], taxonomy)
        ],
        "explanation": None,
        "verdict": None,
        "run": None,
        "changed_since": False,
    }
    status_row = store.conn.execute(
        "SELECT status, note FROM user_job_status WHERE job_id = ?", (job_id,)
    ).fetchone()
    out["status"] = status_row["status"] if status_row else "new"
    out["note"] = status_row["note"] if status_row else ""

    if search is not None:
        search_row = _search_or_404(store, search)
        run_row = _resolve_run(store, search_row, run)
        if run_row is None:
            raise HTTPException(404, "this search has no completed run")
        match = store.match_row(job_id, int(run_row["id"]))
        if match is None:
            raise HTTPException(404, "this job has no verdict in that run")
        verdict = _row(match)
        out["explanation"] = load_explanation(match)
        verdict.pop("explanation", None)
        out["verdict"] = verdict
        out["run"] = _run_summary(run_row, search_row)
        out["changed_since"] = (job["content_hash"] or "") != (match["content_hash"] or "")
        out["search"] = _search_summary(search_row)
    return out


@api.post("/jobs/{job_id}/status")
def set_status(job_id: int, body: StatusBody, store: Store = Depends(get_store)) -> dict[str, Any]:
    allowed = {s.value for s in UserStatus}
    if body.status not in allowed:
        raise HTTPException(422, f"status must be one of {', '.join(sorted(allowed))}")
    if store.get_job(job_id) is None:
        raise HTTPException(404, "no such job")
    store.set_user_status(job_id, body.status, body.note)
    return {"job_id": job_id, "status": body.status}


@api.post("/jobs/{job_id}/dismiss")
def dismiss(job_id: int, body: DismissBody, store: Store = Depends(get_store)) -> dict[str, Any]:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    search = _search_or_404(store, body.search_uid)
    spec = SearchSpec.from_yaml(search["spec_yaml"])
    taxonomy = Taxonomy.default()
    try:
        rules = complete_rules(
            [DismissRule(r.kind, r.value) for r in body.rules],  # type: ignore[arg-type]
            title=job["title"], company=job["company"], spec=spec, taxonomy=taxonomy,
        )
        updated = apply_rules(spec, rules, taxonomy)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None

    revision = int(search["revision"])
    try:
        with store._tx():
            if updated != spec:
                revision = store.update_search(
                    body.search_uid, name=search["name"], spec_yaml=updated.to_yaml(),
                    expected_revision=body.revision,
                )
            store.set_user_status(job_id, UserStatus.DISMISSED.value, body.reason)
            store.record_feedback(
                job_id, int(search["id"]), body.reason, [r.as_dict() for r in rules]
            )
    except RevisionConflict as conflict:
        raise HTTPException(409, {"revision": conflict.current}) from None
    return {
        "job_id": job_id, "reason": body.reason, "rules": [describe(r) for r in rules],
        "revision": revision, "spec_changed": updated != spec,
    }


@api.get("/saved")
def saved(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return [_item(row) for row in store.saved_jobs()]


# ------------------------------------------------------------------ runs


@api.post("/runs", status_code=202)
def request_run(
    body: RunBody, store: Store = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> dict[str, Any]:
    now = datetime.now()
    request_id, attached = store.create_queued("web", body.priority_uid, now)
    if not attached:
        try:
            dispatcher.wake()
        except Exception as error:  # noqa: BLE001 - recorded on the request, never swallowed
            store.fail_request(request_id, f"could not wake the runner: {error}", now)
            raise HTTPException(502, f"could not wake the runner: {error}") from None
    return {**_row(store.get_request(request_id)), "attached": attached}


@api.get("/runs")
def list_runs(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    store.reconcile_requests(datetime.now())
    return [_row(r) for r in store.list_requests(20)]


@api.get("/runs/{request_id}")
def get_run_request(request_id: str, store: Store = Depends(get_store)) -> dict[str, Any]:
    store.reconcile_requests(datetime.now())
    row = store.get_request(request_id)
    if row is None:
        raise HTTPException(404, "no such request")
    return _row(row)


@api.post("/runs/{request_id}/cancel")
def cancel_run_request(request_id: str, store: Store = Depends(get_store)) -> dict[str, Any]:
    if not store.cancel_queued(request_id, datetime.now()):
        raise HTTPException(409, "only a queued request can be cancelled")
    return _row(store.get_request(request_id))


# ------------------------------------------------------------------ sources


@api.get("/sources")
def sources() -> dict[str, Any]:
    return {
        "sources": [
            {"name": i.name, "kind": i.kind, "priority": i.priority, "note": i.note}
            for i in CATALOG.values()
        ],
        "excluded": [{"name": name, "reason": reason} for name, reason in EXCLUDED.items()],
    }


@api.get("/registry")
def registry(
    platform: str | None = Query(None), limit: int = Query(50, ge=1, le=500),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    rows = store.registry_targets(
        platforms=[platform] if platform else None, limit=limit, max_failures=99
    )
    return {"counts": store.registry_counts(), "boards": [_row(r) for r in rows]}


@api.post("/registry", status_code=201)
def add_board(body: RegistryBody, store: Store = Depends(get_store)) -> dict[str, Any]:
    added = add_from_url(store, body.url, company=body.name)
    if added:
        board = added[0]
        return {"boards": [{"platform": "board", "token": board.token, "company": board.company}]}
    from ..sources.discovery import discover_boards

    with HttpFetcher() as fetcher:
        try:
            boards = discover_boards(fetcher, body.url)
        except Exception as error:  # noqa: BLE001 - reported to the user as the CLI does
            raise HTTPException(502, f"could not read that page: {error}") from None
    if not boards:
        raise HTTPException(404, "no ATS board found on that page")
    registered = []
    for board in boards:
        store.add_company(
            company=body.name or board.token, ats=board.platform, token=board.registry_token(),
            board_url=board.url, source="careers-page", us_signal=True,
            notes=json.dumps(board.extra) if board.extra else "",
        )
        registered.append({
            "platform": board.platform, "token": board.registry_token(),
            "company": body.name or board.token,
        })
    return {"boards": registered}


@app.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    backend = "turso" if os.environ.get("TURSO_DATABASE_URL") else "sqlite"
    return {"ok": True, "backend": backend}


app.include_router(api)


def serve_static(target: FastAPI = app) -> bool:
    """Serve the built SPA from ``web/dist`` at ``/``. Called by the entrypoints, last,
    because a mount at the root shadows every route registered after it. The SPA uses
    hash routing, so no history fallback is needed."""
    if not WEB_DIST.is_dir():
        return False
    target.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
    return True
