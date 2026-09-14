#!/usr/bin/env python3
"""Populate a database from test fixtures, with no network.

Two uses. It lets someone see what the tool does before they have API keys or a reachable
network, and it is how the CLI's read paths were exercised during development in an
environment where every job-source host is blocked at the proxy.

The postings are fixtures, not live data. Anything produced from this database is a
demonstration of the machinery, never a real job.

    python scripts/demo.py --db /tmp/demo.sqlite3
    jobagent results accounting-remote --db /tmp/demo.sqlite3 --explain
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from jobagent.domain.spec import SearchSpec  # noqa: E402
from jobagent.engine.orchestrator import run_search  # noqa: E402
from jobagent.infra.store import Store  # noqa: E402
from tests.fakes import FakeFetcher  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
TODAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 12, 0, 0)


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--spec", default=str(ROOT / "examples" / "benchmark-accounting.yml"))
    args = parser.parse_args()

    store = Store(args.db)
    spec = SearchSpec.from_yaml(Path(args.spec).read_text())
    search_id = store.save_spec(spec.name, spec.to_yaml())

    # A handful of demo boards rather than the full seeded registry.
    store.add_company("Acme Corp", "greenhouse", "acme", domain="acme.com", us_signal=True)
    store.add_company("Beta Inc", "lever", "beta", domain="beta.com", us_signal=True)
    store.add_company("Gamma LLC", "ashby", "gamma", domain="gamma.com", us_signal=True)

    fetcher = FakeFetcher(routes={
        "boards-api.greenhouse.io": fixture("greenhouse_board"),
        "api.lever.co": fixture("lever_board"),
        "api.ashbyhq.com": fixture("ashby_board"),
    })

    outcome = run_search(
        spec, store, fetcher, search_id=search_id, today=TODAY, now=NOW,
        only_sources=["greenhouse", "lever", "ashby"],
    )
    print(f"demo database ready: {args.db}")
    print(f"  search: {spec.name}")
    print(f"  {outcome.summary()}")
    print()
    print(f"  jobagent results {spec.name} --db {args.db} --explain")
    print(f"  jobagent results {spec.name} --db {args.db} --rejected --explain")
    print(f"  jobagent coverage {spec.name} --db {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
