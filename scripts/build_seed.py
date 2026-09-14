#!/usr/bin/env python3
"""Build the bundled company registry seed.

Discovery in this tool is fan-out over known employer boards, so the registry is the
product's entire reach. An empty registry means an empty results table, which is why a
seed ships rather than asking every new user to build one by hand.

Source: outscal/OpenJobs `data/companies_v2.json` (MIT). Filtered here to companies whose
ATS is one this tool can actually read, and tagged with a US signal so fan-out ordering
and the request budget favour the scope the product serves.

Run:  python scripts/build_seed.py [--offline PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobagent.sources.discovery import extract_board  # noqa: E402

SOURCE_URL = (
    "https://raw.githubusercontent.com/outscal/OpenJobs/main/data/companies_v2.json"
)
OUTPUT = Path(__file__).resolve().parents[1] / "src" / "jobagent" / "data" / "companies.seed.json"

# Platforms this project can route to. The first four have shipped adapters; the last two
# are seeded ahead of theirs, which are held back pending a robots.txt question, so they
# sit in the registry unread. `jobagent company seed` reports the split rather than
# implying every seeded board is searchable.
SUPPORTED = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable"}

US_NAMES = {"united states", "usa", "us", "united states of america"}


def load(offline: str | None) -> list[dict]:
    """Read the upstream company list.

    The download goes through the project's own fetcher rather than urllib, so this
    build-time script cannot become a second egress path with its own timeout, user
    agent and rate-limit behaviour -- the whole point of routing everything through one
    place.
    """
    if offline:
        return json.loads(Path(offline).read_text())

    from jobagent.infra.http import HttpFetcher

    with HttpFetcher() as fetcher:
        response = fetcher.get(SOURCE_URL)
    if not response.ok:
        raise SystemExit(
            f"could not fetch the company list (HTTP {response.status}). "
            f"Download it manually and pass --offline."
        )
    return json.loads(response.text)


def domain_of(website: str | None) -> str | None:
    if not website:
        return None
    host = website.split("//")[-1].split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host or None


def build(records: list[dict]) -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    for record in records:
        name = (record.get("name") or "").strip()
        if not name:
            continue
        countries = [str(c).strip().lower() for c in (record.get("countries") or [])]
        us_signal = any(c in US_NAMES for c in countries)

        for link in record.get("ats_links") or []:
            if not isinstance(link, str):
                continue
            board = extract_board(link)
            if board is None or board.platform not in SUPPORTED:
                continue
            row = {
                "company": name,
                "domain": domain_of(record.get("website")),
                "ats": board.platform,
                # Composite for platforms whose identity is more than a name, so two
                # sites of one Workday tenant stay two boards.
                "token": board.registry_token(),
                "board_url": board.url,
                "us_signal": us_signal,
                "extra": board.extra,
            }
            # First write wins, except that a US signal always upgrades an existing row:
            # the same company can appear more than once with different metadata.
            key = board.key()
            if key in rows:
                rows[key]["us_signal"] = rows[key]["us_signal"] or us_signal
            else:
                rows[key] = row
    return sorted(rows.values(), key=lambda r: (r["ats"], r["company"].lower()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", help="path to a previously downloaded companies_v2.json")
    args = parser.parse_args()

    records = load(args.offline)
    rows = build(records)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "source": SOURCE_URL,
                "license": "MIT (outscal/OpenJobs, fork of santifer/career-ops)",
                "companies": rows,
            },
            indent=1,
        )
        + "\n"
    )

    by_platform: dict[str, int] = {}
    for row in rows:
        by_platform[row["ats"]] = by_platform.get(row["ats"], 0) + 1
    us = sum(1 for r in rows if r["us_signal"])
    print(f"wrote {len(rows)} boards to {OUTPUT.relative_to(Path.cwd())}")
    print(f"  US-signalled: {us}")
    for platform, count in sorted(by_platform.items(), key=lambda kv: -kv[1]):
        print(f"  {platform:16} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
