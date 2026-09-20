"""The iCIMS list reader, driven by a fixture trimmed from a live capture of 2026-09-20.

The fixture keeps the real wrapper markup (the job table, the header's "Page 1 of N", the
bottom paginator) around three invented cards for an invented tenant. Synthetic pages for
the paging and multi-term cases are built here from one card template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.domain.models import AuthorityTier, WorkplaceType
from jobagent.ports import FetchError, FetchResponse
from jobagent.sources.ats.base import TERM_SEPARATOR, BoardTarget
from jobagent.sources.ats.icims import PAGE_SIZE, IcimsAdapter
from tests.fakes import FakeFetcher

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "icims_search.html").read_text(
    encoding="utf-8"
)
SEARCH = "careers-acme.icims.com/jobs/search"


def acme(**extra: str) -> BoardTarget:
    return BoardTarget(company="Acme Corp", token="careers-acme", domain="acme.com", extra=extra)


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

        assert len(fetcher.calls) == 2
        assert any("searchKeyword=alpha" in url for url in fetcher.calls)
        assert any("searchKeyword=beta" in url for url in fetcher.calls)
        assert sorted(p.external_id for p in postings) == ["careers-acme:1", "careers-acme:2"]

    def test_the_last_page_stops_pagination(self):
        """A short page or the header's own "Page N of N" ends a query; a full page that
        is not the last buys exactly one more request."""
        assert len(self.fetcher.calls) == 1  # fixture: 3 cards, "Page 1 of 1"

        full = [card(i) for i in range(1, PAGE_SIZE + 1)]
        fetcher = FakeFetcher(routes={"pr=0": page(full, number=1, total=1)})
        IcimsAdapter().fetch_board(fetcher, acme())
        assert len(fetcher.calls) == 1  # full page, but the header says it is the last

        fetcher = FakeFetcher(routes={
            "pr=0": page(full, number=1, total=2),
            "pr=1": page([card(i) for i in range(PAGE_SIZE + 1, PAGE_SIZE + 4)], 2, 2),
        })
        postings = IcimsAdapter().fetch_board(fetcher, acme())
        assert len(fetcher.calls) == 2
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
