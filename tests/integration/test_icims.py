"""The iCIMS list and detail readers, driven by fixtures trimmed from live captures of
2026-09-20.

The list fixture keeps the real wrapper markup (the job table, the header's "Page 1 of N",
the bottom paginator) around three invented cards for an invented tenant; the detail
fixture keeps the real page skeleton around one JSON-LD JobPosting for the first card.
Synthetic pages for the paging and multi-term cases are built here from one card template.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from jobagent.domain.models import AuthorityTier, WorkplaceType
from jobagent.ports import FetchError, FetchResponse
from jobagent.sources.ats.base import TERM_SEPARATOR, BoardTarget
from jobagent.sources.ats.icims import PAGE_SIZE, IcimsAdapter
from jobagent.sources.catalog import CATALOG, EXCLUDED, all_adapters, ats_adapters
from tests.fakes import FakeFetcher, blocked, unavailable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = (FIXTURES / "icims_search.html").read_text(encoding="utf-8")
DETAIL = (FIXTURES / "icims_detail.html").read_text(encoding="utf-8")
SEARCH = "careers-acme.icims.com/jobs/search"
JOB_5819 = "/jobs/5819/"


def acme(**extra: str) -> BoardTarget:
    return BoardTarget(company="Acme Corp", token="careers-acme", domain="acme.com", extra=extra)


def searches(fetcher: FakeFetcher) -> list[str]:
    """The list requests only; the detail pass is counted by its own tests."""
    return [url for url in fetcher.calls if "/jobs/search" in url]


def details(fetcher: FakeFetcher) -> list[str]:
    return [url for url in fetcher.calls if "/jobs/search" not in url]


def detail(**overrides: object) -> str:
    """A job page whose JSON-LD is the fixture's with some keys replaced."""
    start = DETAIL.index('<script type="application/ld+json">') + len(
        '<script type="application/ld+json">'
    )
    data = json.loads(DETAIL[start:DETAIL.index("</script>", start)])
    data.update(overrides)
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def card(job_id: int, title: str = "Warehouse Associate", remote: str = "No") -> str:
    """One job card in the live layout, for pages the fixture cannot express."""
    return f"""<li class="iCIMS_JobCardItem"><div class="row">
<div class="col-xs-6 header left"><span class="sr-only field-label">Location</span>
<span > US-FL-Bartow</span></div>
<div class="col-xs-6 header right"><span class="sr-only field-label">ID</span>
<span > 2026-{job_id}</span></div>
<div class="col-xs-12 title"><a href="https://careers-acme.icims.com/jobs/{job_id}/x/job?in_iframe=1"
 class="iCIMS_Anchor" title="{job_id} - {title}"><span class="sr-only field-label">Title</span>
<h3 > {title}</h3></a></div>
<div class="col-xs-12 description">Teaser.</div>
<div class="col-xs-12 additionalFields"><dl class="iCIMS_JobHeaderGroup">
<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Remote</dt>
<dd class="iCIMS_JobHeaderData"><span > {remote}</span></dd>
</div></dl></div></div></li>"""


def page(cards: list[str], number: int = 1, total: int = 1) -> str:
    return (
        f'<h2 class="iCIMS_SubHeader">Search Results Page {number} of {total}</h2>'
        f'<ul class="container-fluid iCIMS_JobsTable">{"".join(cards)}</ul>'
    )


class TestIcims:
    def setup_method(self):
        self.fetcher = FakeFetcher(routes={SEARCH: FIXTURE})
        self.postings = IcimsAdapter().fetch_board(self.fetcher, acme())
        self.by_id = {p.external_id: p for p in self.postings}

    def test_cards_become_postings(self):
        """Every field the gates and the display read is lifted from the card, not guessed."""
        assert len(self.postings) == 3
        first = self.by_id["careers-acme:5819"]
        assert first.title == "Warehouse Associate"
        assert first.url == "https://careers-acme.icims.com/jobs/5819/warehouse-associate/job"
        assert first.extra["requisition"] == "2026-5819"
        assert first.extra["detail_url"].endswith("?in_iframe=1")
        assert first.location_raw == "US-FL-Bartow"
        assert first.country_hint == "US"
        assert first.workplace_hint is WorkplaceType.UNKNOWN
        assert first.employment_type == "Full-Time"
        assert first.department == "Plant Operations"
        assert "$17.00/hr" in first.description_text
        assert first.posted_at is None
        assert first.apply_url is None
        assert first.authority is AuthorityTier.OFFICIAL_ATS

        payroll = self.by_id["careers-acme:5817"]
        assert payroll.workplace_hint is WorkplaceType.REMOTE
        assert payroll.employment_type == "Part-Time"
        assert payroll.department == "Finance"

    def test_remote_no_is_unknown_not_onsite(self):
        """"Remote: No" means not marked remote, which is not a claim of onsite work."""
        assert self.by_id["careers-acme:5819"].workplace_hint is WorkplaceType.UNKNOWN
        assert self.by_id["careers-acme:5818"].workplace_hint is WorkplaceType.UNKNOWN
        assert self.by_id["careers-acme:5817"].workplace_hint is WorkplaceType.REMOTE

    def test_every_term_is_queried_and_duplicates_collapse(self):
        """One keyword per query means one request per term, and a job answering two
        terms must still be one posting."""

        class ByTerm(FakeFetcher):
            def get(self, url, *, params=None, headers=None):
                self.calls.append(url)
                cards = [card(1)] if "searchKeyword=alpha" in url else [card(1), card(2)]
                return FetchResponse(url=url, status=200, text=page(cards))

        fetcher = ByTerm()
        target = acme(search_text=TERM_SEPARATOR.join(["alpha", "beta"]))
        postings = IcimsAdapter().fetch_board(fetcher, target)

        assert len(searches(fetcher)) == 2
        assert any("searchKeyword=alpha" in url for url in fetcher.calls)
        assert any("searchKeyword=beta" in url for url in fetcher.calls)
        assert sorted(p.external_id for p in postings) == ["careers-acme:1", "careers-acme:2"]

    def test_the_last_page_stops_pagination(self):
        """A short page or the header's own "Page N of N" ends a query; a full page that
        is not the last buys exactly one more request."""
        assert len(searches(self.fetcher)) == 1  # fixture: 3 cards, "Page 1 of 1"

        full = [card(i) for i in range(1, PAGE_SIZE + 1)]
        fetcher = FakeFetcher(routes={"pr=0": page(full, number=1, total=1)})
        IcimsAdapter().fetch_board(fetcher, acme())
        assert len(searches(fetcher)) == 1  # full page, but the header says it is the last

        fetcher = FakeFetcher(routes={
            "pr=0": page(full, number=1, total=2),
            "pr=1": page([card(i) for i in range(PAGE_SIZE + 1, PAGE_SIZE + 4)], 2, 2),
        })
        postings = IcimsAdapter().fetch_board(fetcher, acme())
        assert len(searches(fetcher)) == 2
        assert len(postings) == PAGE_SIZE + 3

    def test_a_missing_tenant_is_gone_not_empty(self):
        """A 404 is a dead tenant and counts against the board; it is never an empty one."""
        with pytest.raises(FetchError) as info:
            IcimsAdapter().fetch_board(FakeFetcher(), acme())
        assert info.value.status == 404

    def test_a_table_with_no_cards_is_a_parse_error(self):
        """A job table whose cards no longer parse is a changed layout and must surface as
        a parse error, never as a healthy empty board; no table at all is empty."""
        broken = FakeFetcher(routes={SEARCH: '<ul class="container-fluid iCIMS_JobsTable"></ul>'})
        with pytest.raises(ValueError):
            IcimsAdapter().fetch_board(broken, acme())

        empty = FakeFetcher(routes={SEARCH: "<html><body>No jobs match.</body></html>"})
        assert IcimsAdapter().fetch_board(empty, acme()) == []


class TestIcimsDetails:
    """The job page's JSON-LD JobPosting, read for the first card only (5819)."""

    def enriched(self, page: str = DETAIL, **extra: str):
        fetcher = FakeFetcher(routes={SEARCH: FIXTURE, JOB_5819: page})
        postings = IcimsAdapter().fetch_board(fetcher, acme(**extra))
        return {p.external_id: p for p in postings}["careers-acme:5819"]

    def test_title_matched_postings_get_details_first(self):
        """A detail budget of one must buy the description of the job the search named,
        not of whatever the board listed first."""
        fetcher = FakeFetcher(routes={SEARCH: FIXTURE})
        IcimsAdapter(max_details=1).fetch_board(fetcher, acme(search_text="Payroll Coordinator"))
        assert len(details(fetcher)) == 1
        assert "/jobs/5817/" in details(fetcher)[0]  # listed third, matched on title

        fetcher = FakeFetcher(routes={SEARCH: FIXTURE})
        IcimsAdapter(max_details=1).fetch_board(fetcher, acme())
        assert len(details(fetcher)) == 1
        assert JOB_5819 in details(fetcher)[0]  # nothing asked for, so listing order

    def test_detail_json_ld_supplies_description_date_type_and_location(self):
        """The gates judge description_text first, so the teaser must leave both text and
        html; the card's own Position Type wins over the schema.org code."""
        teaser = {p.external_id: p for p in IcimsAdapter().fetch_board(
            FakeFetcher(routes={SEARCH: FIXTURE}), acme()
        )}["careers-acme:5819"].description_text
        posting = self.enriched()

        assert "<h2>Overview</h2>" in posting.description_html
        assert posting.description_text != teaser
        assert "Stack finished product by hand" in posting.description_text
        assert posting.posted_at == date(2026, 9, 19)
        assert posting.employment_type == "Full-Time"
        assert posting.country_hint == "US"
        assert posting.location_raw == "Bartow, FL, US"

    def test_json_ld_employment_type_fills_in_when_the_card_has_none(self):
        """"FULL_TIME" never matches the taxonomy; the mapped value must be a phrase it knows."""
        fetcher = FakeFetcher(routes={SEARCH: page([card(5819)]), JOB_5819: DETAIL})
        (posting,) = IcimsAdapter().fetch_board(fetcher, acme())
        assert posting.employment_type == "full time"

    def test_unit_less_pay_is_read_by_magnitude_never_guessed_yearly(self):
        """iCIMS states no unit, so 17-25 must be hourly and 60000-80000 yearly; a range
        between is read from the description's words or not at all."""
        posting = self.enriched()
        assert (posting.salary.minimum, posting.salary.maximum) == (17, 25)
        assert posting.salary.period == "hour"
        assert posting.salary.currency == "USD"

        yearly = self.enriched(detail(baseSalary={"minValue": 60000, "maxValue": 80000}))
        assert (yearly.salary.minimum, yearly.salary.maximum) == (60000, 80000)
        assert yearly.salary.period == "year"

        stated = self.enriched(detail(baseSalary={
            "value": {"minValue": 30, "maxValue": 40, "unitText": "HOUR"}, "currency": "USD",
        }))
        assert (stated.salary.minimum, stated.salary.maximum) == (30, 40)
        assert stated.salary.period == "hour"

        weekly = self.enriched(detail(
            baseSalary={"minValue": 900, "maxValue": 1200},
            description="<p>Pay: $900 - $1,200 per week, paid every Friday.</p>",
        ))
        assert (weekly.salary.minimum, weekly.salary.maximum) == (900, 1200)
        assert weekly.salary.period == "week"

        silent = self.enriched(detail(
            baseSalary={"minValue": 900, "maxValue": 1200}, description="<p>Join us.</p>",
        ))
        assert silent.salary is None  # an unexplained figure is left unread, not guessed

    def test_a_blocked_detail_fetch_raises_and_a_500_leaves_the_posting_intact(self):
        """A refusal on the job page must surface as a blocked board, not as a healthy run
        of teaser-only postings; an ordinary failure just leaves that posting as listed."""
        refused = FakeFetcher(routes={SEARCH: FIXTURE}, failures={JOB_5819: blocked()})
        with pytest.raises(FetchError) as info:
            IcimsAdapter().fetch_board(refused, acme())
        assert info.value.blocked

        for fetcher in (
            FakeFetcher(routes={SEARCH: FIXTURE}, default_status=500),
            FakeFetcher(routes={SEARCH: FIXTURE}, failures={JOB_5819: unavailable()}),
        ):
            postings = {p.external_id: p for p in IcimsAdapter().fetch_board(fetcher, acme())}
            posting = postings["careers-acme:5819"]
            assert "$17.00/hr" in posting.description_text  # the card's teaser
            assert posting.description_html == ""
            assert posting.posted_at is None
            assert posting.salary is None


def test_the_catalog_ships_icims():
    """Registered, planned as a costly server-search platform, and not held back."""
    adapters = all_adapters()
    assert len(adapters) == 7
    assert isinstance(adapters["icims"], IcimsAdapter)
    assert (CATALOG["icims"].kind, CATALOG["icims"].priority) == ("ats", "P1")
    assert "icims" not in EXCLUDED
    assert ats_adapters()["icims"].costly and ats_adapters()["icims"].server_search
