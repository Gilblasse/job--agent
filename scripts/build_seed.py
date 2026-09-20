#!/usr/bin/env python3
"""Build the bundled company registry seed.

Discovery in this tool is fan-out over known employer boards, so the registry is the
product's entire reach. An empty registry means an empty results table, which is why a
seed ships rather than asking every new user to build one by hand.

Three open lists are merged, one row per (ats, registry token), for the platforms this
tool routes to:

- outscal/OpenJobs `data/companies_v2.json` (MIT). The only list with a website and a
  country per company, so the only source of `domain` and of the US signal that orders
  fan-out and spends the request budget on the scope the product serves.
- kalil0321/ats-scrapers `ats-companies/<platform>.csv` (MIT): name, slug and board URL.
- elliottdehn/open-jobs `slugs.json` (CC0): bare slugs per platform. Its Workday entries
  are hosts without a site and are skipped -- a Workday board is a (tenant, instance,
  site) triple, and defaulting the site mints a plausible board that fetches nothing.

The seed ships gzip-compressed: ~28,000 rows cost the repository about a megabyte.

Run:  python scripts/build_seed.py [--offline DIR]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobagent.sources.discovery import extract_board  # noqa: E402

OUTPUT = (
    Path(__file__).resolve().parents[1] / "src" / "jobagent" / "data" / "companies.seed.json.gz"
)

# Platforms this project can route to. A seeded board is not necessarily a searchable one
# (`jobagent company seed` reports the split), but every platform here has a case-
# insensitive identifier the registry can normalise. SmartRecruiters is out: its API is
# disallowed by robots.txt for this fetcher, and its identifiers are case-sensitive.
SUPPORTED = {"greenhouse", "lever", "ashby", "workday", "workable", "icims"}

RAW = "https://raw.githubusercontent.com"
# basename -> (url, licence). `--offline DIR` reads the same basenames from a directory.
SOURCES: dict[str, tuple[str, str]] = {
    "companies_v2.json": (
        f"{RAW}/outscal/OpenJobs/main/data/companies_v2.json",
        "MIT (outscal/OpenJobs, fork of santifer/career-ops)",
    ),
    **{
        f"{platform}.csv": (
            f"{RAW}/kalil0321/ats-scrapers/main/ats-companies/{platform}.csv",
            "MIT (kalil0321/ats-scrapers)",
        )
        for platform in sorted(SUPPORTED)
    },
    "slugs.json": (f"{RAW}/elliottdehn/open-jobs/main/slugs.json", "CC0 (elliottdehn/open-jobs)"),
}

# How a bare slug becomes a URL `extract_board` recognises. Workday is absent on purpose:
# a slug list carries no site, and a Workday board without its site is unreachable.
SLUG_URL = {
    "greenhouse": "https://boards.greenhouse.io/{}",
    "lever": "https://jobs.lever.co/{}",
    "ashby": "https://jobs.ashbyhq.com/{}",
    "workable": "https://apply.workable.com/{}",
    "icims": "https://{}.icims.com",
}

US_NAMES = {"united states", "usa", "us", "united states of america"}


def load(offline: str | None) -> dict[str, Any]:
    """Read every upstream list, keyed by basename and parsed (JSON, or CSV rows).

    Downloads go through the project's own fetcher rather than urllib, so this build-time
    script cannot become a second egress path with its own timeout, user agent and
    rate-limit behaviour -- the whole point of routing everything through one place.
    """
    texts: dict[str, str] = {}
    if offline:
        for name in SOURCES:
            texts[name] = (Path(offline) / name).read_text(encoding="utf-8")
    else:
        from jobagent.infra.http import HttpFetcher

        with HttpFetcher() as fetcher:
            for name, (url, _) in SOURCES.items():
                response = fetcher.get(url)
                if not response.ok:
                    raise SystemExit(
                        f"could not fetch {name} (HTTP {response.status}). "
                        f"Download the lists manually and pass --offline DIR."
                    )
                texts[name] = response.text
    return {name: _parse(name, text) for name, text in texts.items()}


def _parse(name: str, text: str) -> Any:
    if name.endswith(".csv"):
        return list(csv.DictReader(io.StringIO(text)))
    return json.loads(text)


def domain_of(website: str | None) -> str | None:
    if not website:
        return None
    host = website.split("//")[-1].split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host or None


def build(payloads: dict[str, Any]) -> list[dict]:
    """Merge every list into one row per board.

    Lists are visited richest-first, so first-write-wins keeps the best company name and
    the only domain; the US signal upgrades whenever any list carries it.
    """
    rows: dict[tuple[str, str], dict] = {}

    def add(url: str, company: str, *, us_signal: bool = False, domain: str | None = None) -> None:
        board = extract_board(url)
        if board is None or board.platform not in SUPPORTED:
            return
        # One spelling per board: slugs arrive percent-encoded and in mixed case, and the
        # registry's UNIQUE(ats, token) would otherwise hold the same board twice.
        token = unquote(board.registry_token()).lower()
        key = (board.platform, token)
        if key in rows:
            rows[key]["us_signal"] = rows[key]["us_signal"] or us_signal
            return
        row: dict[str, Any] = {
            "company": company or token, "ats": board.platform, "token": token,
            "board_url": board.url, "us_signal": us_signal,
        }
        if domain:
            row["domain"] = domain
        if board.extra:
            row["extra"] = {k: v.lower() for k, v in board.extra.items()}
        rows[key] = row

    for record in payloads.get("companies_v2.json") or []:
        name = (record.get("name") or "").strip()
        if not name:
            continue
        countries = [str(c).strip().lower() for c in (record.get("countries") or [])]
        us_signal = any(c in US_NAMES for c in countries)
        domain = domain_of(record.get("website"))
        for link in record.get("ats_links") or []:
            if isinstance(link, str):
                add(link, name, us_signal=us_signal, domain=domain)

    for platform in sorted(SUPPORTED):
        for row in payloads.get(f"{platform}.csv") or []:
            name = (row.get("company_name") or row.get("name") or "").strip()
            add(row.get("url") or "", name)

    slugs = (payloads.get("slugs.json") or {}).get("ats") or {}
    for platform, template in SLUG_URL.items():
        for slug in slugs.get(platform) or []:
            add(template.format(slug), "")

    return sorted(rows.values(), key=lambda r: (r["ats"], r["company"].lower(), r["token"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", help="directory holding previously downloaded lists")
    args = parser.parse_args()

    rows = build(load(args.offline))

    document = {
        "sources": [{"url": url, "license": licence} for url, licence in SOURCES.values()],
        "companies": rows,
    }
    buffer = io.BytesIO()
    # mtime=0: the same input yields the same bytes, so a rebuild that changes nothing is
    # not a diff.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as archive:
        archive.write((json.dumps(document, indent=1) + "\n").encode("utf-8"))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(buffer.getvalue())

    by_platform: dict[str, int] = {}
    for row in rows:
        by_platform[row["ats"]] = by_platform.get(row["ats"], 0) + 1
    us = sum(1 for r in rows if r["us_signal"])
    size = len(buffer.getvalue())
    print(f"wrote {len(rows)} boards to {OUTPUT.relative_to(Path.cwd())} ({size:,} bytes gzip)")
    print(f"  US-signalled: {us}")
    for platform, count in sorted(by_platform.items(), key=lambda kv: -kv[1]):
        print(f"  {platform:16} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
