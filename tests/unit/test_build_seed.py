"""The seed builder: three open lists become one row per board."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_seed.py"
_spec = importlib.util.spec_from_file_location("build_seed", _SCRIPT)
build_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_seed)


def test_sources_merge_into_one_row_per_board():
    """Three lists spell the same board three ways, and the registry's UNIQUE(ats, token)
    must see it once -- with the richest name and any US evidence kept."""
    rows = build_seed.build(
        {
            "companies_v2.json": [
                {
                    "name": "Acme Corp",
                    "website": "https://www.acme.com",
                    "countries": ["United States"],
                    "ats_links": ["https://boards.greenhouse.io/acme"],
                },
            ],
            "greenhouse.csv": [
                {"name": "acme", "slug": "acme", "url": "https://boards.greenhouse.io/acme"},
                {"name": "Beta Labs", "slug": "beta", "url": "https://job-boards.greenhouse.io/beta"},
            ],
            "workday.csv": [
                {"name": "Foo", "slug": "foo/external_careers",
                 "url": "https://foo.wd5.myworkdayjobs.com/External_Careers"},
                {"name": "Baz", "slug": "baz", "url": "https://baz.wd5.myworkdayjobs.com"},
            ],
            "icims.csv": [
                {"name": "acme", "slug": "careers-acme", "url": "https://careers-acme.icims.com",
                 "company_name": "Acme Corp"},
            ],
            "slugs.json": {
                "ats": {
                    "greenhouse": ["acme", "pocket%20worlds"],
                    "workday": ["bar.wd1.myworkdayjobs.com"],
                }
            },
        }
    )
    by_key = {(r["ats"], r["token"]): r for r in rows}
    assert len(by_key) == len(rows) == 5

    acme = by_key[("greenhouse", "acme")]
    assert acme["company"] == "Acme Corp"
    assert acme["us_signal"] is True
    assert acme["domain"] == "acme.com"
    assert "extra" not in acme

    beta = by_key[("greenhouse", "beta")]
    assert beta["company"] == "Beta Labs"
    assert beta["us_signal"] is False
    assert "domain" not in beta

    assert by_key[("greenhouse", "pocket worlds")]["company"] == "pocket worlds"

    # Workday: the site travels in the token, lowercased like every other identifier here;
    # a row without a site is dropped whatever list it came from.
    foo = by_key[("workday", "foo:5:external_careers")]
    assert foo["extra"] == {"wd": "5", "site": "external_careers"}
    assert not any(r["ats"] == "workday" and r["token"] != "foo:5:external_careers" for r in rows)

    assert by_key[("icims", "careers-acme")]["company"] == "Acme Corp"
