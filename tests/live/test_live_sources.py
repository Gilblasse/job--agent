"""Live checks against real ATS endpoints.

These answer the question fixtures cannot: do the live endpoints still behave the way
their documentation says? A failure here is information, not necessarily a bug -- a
vendor changing a field name is exactly what this is for.

Run with: pytest -m live
"""

from __future__ import annotations

import os

import pytest

from jobagent.domain.models import SourceStatus
from jobagent.infra.http import HttpFetcher
from jobagent.infra.store import Store
from jobagent.ports import DiscoveryRequest
from jobagent.sources.ats.ashby import AshbyAdapter
from jobagent.sources.ats.base import BoardTarget
from jobagent.sources.ats.greenhouse import GreenhouseAdapter
from jobagent.sources.ats.lever import LeverAdapter
from jobagent.sources.ats.workable import WorkableAdapter
from jobagent.sources.ats.workday import WorkdayAdapter
from jobagent.sources.registry import seed_registry
from jobagent.sources.usajobs import UsaJobsAdapter

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def fetcher():
    with HttpFetcher() as client:
        yield client


ADAPTERS = [
    pytest.param(GreenhouseAdapter(), id="greenhouse"),
    pytest.param(LeverAdapter(), id="lever"),
    pytest.param(AshbyAdapter(), id="ashby"),
    pytest.param(WorkdayAdapter(), id="workday"),
    pytest.param(WorkableAdapter(), id="workable"),
]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_adapter_returns_real_postings(adapter, fetcher):
    """Each P1 adapter must reach a real board and parse it.

    Judged on content, never on status: several platforms answer 200 with a vendor
    placeholder for tenants that do not exist.
    """
    report = adapter.check(fetcher)
    assert report.status is SourceStatus.OK, f"{adapter.name}: {report.note}"
    assert report.found > 0, f"{adapter.name} was reachable but returned nothing"


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_documented_fields_are_still_present(adapter, fetcher):
    """Guards against silent vendor schema drift, the known failure mode here."""
    for target in adapter.probe_targets():
        try:
            postings = adapter.fetch_board(fetcher, target)
        except Exception:  # noqa: BLE001 - try the next probe board
            continue
        if not postings:
            continue
        posting = postings[0]
        assert posting.title, f"{adapter.name}: no title"
        assert posting.external_id, f"{adapter.name}: no id"
        assert posting.url.startswith("http"), f"{adapter.name}: no absolute url"
        return
    pytest.fail(f"{adapter.name}: no probe board returned postings")


def test_robots_policy_resolves_for_every_source_host(fetcher):
    """Every shipped source's host must still permit us.

    A host that disallows us is a real answer, and the right response is to stop
    shipping that adapter -- which is why SmartRecruiters is held back: its API host
    says ``Disallow: /`` for everyone but LinkedInBot. Workable was held on the same
    question until its host was read on 2026-09-15 and found to disallow nothing; this
    test is what notices if that changes.
    """
    hosts = [
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs",
        "https://api.lever.co/v0/postings/netflix",
        "https://api.ashbyhq.com/posting-api/job-board/linear",
        "https://apply.workable.com/api/v1/widget/accounts/huggingface",
        "https://data.usajobs.gov/api/search",
    ]
    verdicts = {}
    for url in hosts:
        allowed, note = fetcher.robots.allows(url)
        verdicts[url] = (allowed, note)
    for url, (allowed, note) in verdicts.items():
        print(f"{'ALLOW' if allowed else 'DENY '}  {url}  ({note})")
    assert all(allowed for allowed, _ in verdicts.values()), (
        "a source host disallows this crawler; stop shipping that adapter rather than "
        f"ignoring it: {verdicts}"
    )


@pytest.mark.skipif(
    not (os.environ.get("JOBAGENT_USAJOBS_KEY") and os.environ.get("JOBAGENT_USAJOBS_EMAIL")),
    reason="USAJOBS credentials not set",
)
def test_usajobs_search_returns_results(fetcher):
    result = UsaJobsAdapter(max_pages=1).discover(
        DiscoveryRequest(terms=["analyst"], locations=["Dallas, Texas"], fetcher=fetcher)
    )
    assert result.report.status in (SourceStatus.OK, SourceStatus.PARTIAL)
    assert result.postings, "USAJOBS returned no postings for a common term"
    assert all(p.country_hint == "US" for p in result.postings)


def test_the_go_no_go_gate_passes(tmp_path, fetcher):
    """The gate that decides whether any of this is usable from here."""
    from jobagent.engine.doctor import describe, run_doctor

    store = Store(str(tmp_path / "live.sqlite3"))
    seed_registry(store)
    result = run_doctor(store, fetcher)
    print("\n".join(describe(result)))
    assert result.passed, "; ".join(result.failures)


def test_a_seeded_board_is_still_alive(fetcher):
    """Registry rot is the documented long-term failure mode; this samples for it."""
    from jobagent.sources.registry import load_seed_file

    companies = [c for c in load_seed_file()["companies"] if c["ats"] == "greenhouse"][:5]
    assert companies, "seed file has no Greenhouse boards"

    adapter = GreenhouseAdapter()
    alive = 0
    for row in companies:
        target = BoardTarget(company=row["company"], token=row["token"])
        try:
            if adapter.fetch_board(fetcher, target):
                alive += 1
        except Exception:  # noqa: BLE001
            continue
    assert alive, f"none of {len(companies)} sampled seeded boards returned postings"
