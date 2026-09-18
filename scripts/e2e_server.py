#!/usr/bin/env python3
"""The web app on a local port with no cloud and no GitHub.

Seeds a demo database from the test fixtures (two example searches, each already run
once), serves the built SPA and the API from one process, and answers "Search jobs" with
a dispatcher that runs the real runner path -- lease, queue, engine, publish -- in a
thread against the same SQLite file, over the fixtures. That is what the acceptance test
drives, and it is also the quickest way to try the app:

    cd web && npm run build && cd ..
    python scripts/e2e_server.py --port 8000
    # open http://127.0.0.1:8000  (password: e2e)

Test-only hooks live under /e2e: POST /e2e/outcome {"outcome": "succeed"|"fail"} decides
what the next "Search jobs" does, and POST /e2e/advance {"runs": N} completes N further
runs at once, which is how the acceptance test expires the run on screen.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import APIRouter  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from jobagent.domain.spec import SearchSpec  # noqa: E402
from jobagent.engine.cloud_run import run_cloud  # noqa: E402
from jobagent.engine.orchestrator import run_search  # noqa: E402
from jobagent.infra.store import Store  # noqa: E402
from jobagent.web import app as web  # noqa: E402
from tests.fakes import FakeFetcher  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
EXAMPLES = ROOT / "examples"
SEED_DAY = datetime(2026, 9, 14, 12, 0, 0)


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def routes() -> dict:
    return {
        "boards-api.greenhouse.io": fixture("greenhouse_board"),
        "api.lever.co": fixture("lever_board"),
        "api.ashbyhq.com": fixture("ashby_board"),
    }


def seed(path: Path) -> None:
    store = Store(path)
    store.add_company("Acme Corp", "greenhouse", "acme", domain="acme.com", us_signal=True)
    store.add_company("Beta Inc", "lever", "beta", domain="beta.com", us_signal=True)
    store.add_company("Gamma LLC", "ashby", "gamma", domain="gamma.com", us_signal=True)
    fetcher = FakeFetcher(routes=routes())
    # The accounting search is saved last so it is the most recently updated -- the one
    # the screen opens on -- and it is the one the fixtures have matches for.
    for name in ("react-remote.yml", "benchmark-accounting.yml"):
        spec = SearchSpec.from_yaml((EXAMPLES / name).read_text())
        search_id = store.save_spec(spec.name, spec.to_yaml())
        run_search(
            spec, store, fetcher, search_id=search_id, today=SEED_DAY.date(), now=SEED_DAY,
            only_sources=["greenhouse", "lever", "ashby"],
        )
    store.close()


class LocalDispatcher:
    """Runs the runner's own path in a thread, over the fixtures, against ``path``."""

    def __init__(self, path: Path):
        self.path = path
        self.outcome = "succeed"
        self.lock = threading.Lock()

    def wake(self) -> None:
        threading.Thread(target=self.run_once, daemon=True).start()

    def run_once(self) -> None:
        # A short pause so a screen that just queued the request can see it waiting.
        time.sleep(1.0)
        with self.lock:
            fail = self.outcome == "fail"
            fetcher = FakeFetcher(routes={} if fail else routes())
            run_cloud(
                Store(self.path), lambda: Store(self.path), lambda: fetcher,
                origin="local", doctor=fail, heartbeat_seconds=5, log=print,
            )


class Outcome(BaseModel):
    outcome: str


class Advance(BaseModel):
    runs: int = 1


def hooks(dispatcher: LocalDispatcher) -> APIRouter:
    router = APIRouter(prefix="/e2e")

    @router.post("/outcome")
    def set_outcome(body: Outcome) -> dict:
        dispatcher.outcome = body.outcome
        return {"outcome": dispatcher.outcome}

    @router.post("/advance")
    def advance(body: Advance) -> dict:
        for _ in range(body.runs):
            store = Store(dispatcher.path)
            store.create_queued("local", None, datetime.now())
            with dispatcher.lock:
                run_cloud(
                    store, lambda: Store(dispatcher.path), lambda: FakeFetcher(routes=routes()),
                    origin="local", doctor=False, heartbeat_seconds=5, log=lambda _l: None,
                )
        return {"advanced": body.runs, "today": date.today().isoformat()}

    return router


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="Database path; a temporary one by default.")
    parser.add_argument("--password", default=os.environ.get("JOBAGENT_WEB_PASSWORD") or "e2e")
    args = parser.parse_args()

    path = (
        Path(args.db) if args.db
        else Path(tempfile.mkdtemp(prefix="jobagent-e2e-")) / "e2e.sqlite3"
    )
    if not path.exists():
        seed(path)
    os.environ["JOBAGENT_DB"] = str(path)
    os.environ["JOBAGENT_WEB_PASSWORD"] = args.password
    os.environ.pop("TURSO_DATABASE_URL", None)

    dispatcher = LocalDispatcher(path)
    web.app.state.dispatcher = dispatcher
    web.app.include_router(hooks(dispatcher))
    web.serve_static()  # last: a mount at / shadows anything registered after it

    print(f"jobagent e2e server: http://127.0.0.1:{args.port}  db={path}  password={args.password}")
    uvicorn.run(web.app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
