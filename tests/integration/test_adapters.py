"""Adapters, driven entirely by replayed fixtures.

These prove each adapter maps its platform's documented fields onto RawPosting correctly,
and that failures degrade into reports rather than exceptions. They cannot prove the live
endpoints still behave as documented -- see tests/fixtures/__init__.py.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from jobagent.domain.models import AuthorityTier, SourceStatus, WorkplaceType
from jobagent.ports import DiscoveryRequest
from jobagent.sources.ats.ashby import AshbyAdapter
from jobagent.sources.ats.base import BoardTarget
from jobagent.sources.ats.greenhouse import GreenhouseAdapter
from jobagent.sources.ats.lever import LeverAdapter
from jobagent.sources.ats.workday import WorkdayAdapter
from jobagent.sources.usajobs import UsaJobsAdapter
from tests.fakes import FakeFetcher, blocked, unavailable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def acme() -> BoardTarget:
    return BoardTarget(company="Acme Corp", token="acme", domain="acme.com")


class TestGreenhouse:
    def setup_method(self):
        self.fetcher = FakeFetcher(routes={"boards-api.greenhouse.io": fixture("greenhouse_board")})
        self.postings = GreenhouseAdapter().fetch_board(self.fetcher, acme())

    def test_all_postings_are_returned(self):
        assert len(self.postings) == 3

    def test_double_escaped_html_becomes_readable_text(self):
        from jobagent.domain.text import html_to_text

        text = html_to_text(self.postings[0].description_html)
        assert "accounts payable" in text.lower()
        assert "&lt;" not in text and "<p>" not in text

    def test_absolute_url_is_used_and_ranked_as_official_ats(self):
        assert self.postings[0].url.startswith("https://boards.greenhouse.io/")
        assert self.postings[0].authority is AuthorityTier.OFFICIAL_ATS

    def test_office_location_is_used_when_the_job_has_none(self):
        assert "Canada" in self.postings[2].location_raw

    def test_greenhouse_publishes_no_workplace_or_salary(self):
        """Recorded as a fact about the source, so gates report unverifiable honestly."""
        assert self.postings[0].workplace_hint is WorkplaceType.UNKNOWN
        assert self.postings[0].salary is None


class TestLever:
    def setup_method(self):
        self.fetcher = FakeFetcher(routes={"api.lever.co": fixture("lever_board")})
        self.postings = LeverAdapter().fetch_board(self.fetcher, acme())

    def test_structured_workplace_type_is_carried_through(self):
        assert self.postings[0].workplace_hint is WorkplaceType.REMOTE
        assert self.postings[1].workplace_hint is WorkplaceType.HYBRID

    def test_structured_salary_is_parsed_with_its_interval(self):
        salary = self.postings[0].salary
        assert (salary.minimum, salary.maximum, salary.period) == (70000.0, 90000.0, "year")

    def test_iso_country_is_carried_through(self):
        assert self.postings[0].country_hint == "US"

    def test_epoch_milliseconds_become_a_date(self):
        assert isinstance(self.postings[0].posted_at, date)


class TestAshby:
    def setup_method(self):
        self.fetcher = FakeFetcher(routes={"api.ashbyhq.com": fixture("ashby_board")})
        self.postings = AshbyAdapter().fetch_board(self.fetcher, acme())

    def test_unlisted_postings_are_skipped(self):
        """Unpublished drafts cannot be applied to, so surfacing them wastes attention."""
        assert len(self.postings) == 2
        assert all("a-3" not in p.external_id for p in self.postings)

    def test_workplace_type_is_carried_through(self):
        assert self.postings[0].workplace_hint is WorkplaceType.REMOTE

    def test_apply_url_is_preferred_over_the_listing_url(self):
        assert self.postings[0].apply_url.endswith("/application")


class TestWorkday:
    def setup_method(self):
        self.fetcher = FakeFetcher(routes={
            "/jobs#offset=0": fixture("workday_list"),
            "Project-Manager_R-1": fixture("workday_detail_1"),
            "AP-Clerk_R-2": fixture("workday_detail_2"),
        })
        target = BoardTarget(
            company="Acme Corp", token="acme", domain="acme.com",
            extra={"wd": "5", "site": "External"},
        )
        self.postings = WorkdayAdapter(today=date(2026, 9, 14)).fetch_board(self.fetcher, target)

    def test_listings_are_returned(self):
        assert len(self.postings) == 2

    def test_prose_dates_are_parsed(self):
        """Workday says "Posted 5 Days Ago"; a freshness filter is useless without this."""
        assert self.postings[0].posted_at == date(2026, 9, 9)

    def test_detail_requests_supply_the_description_and_workplace(self):
        assert "hybrid" in self.postings[0].description_html.lower()
        assert self.postings[0].workplace_hint is WorkplaceType.HYBRID
        assert self.postings[0].country_hint == "US"

    def test_page_size_never_exceeds_the_platform_cap(self):
        """Asking for more than 20 returns an empty list with no error."""
        from jobagent.sources.ats.workday import PAGE_SIZE

        assert PAGE_SIZE == 20


class TestUsaJobs:
    def test_skipped_without_credentials(self, monkeypatch):
        monkeypatch.delenv("JOBAGENT_USAJOBS_KEY", raising=False)
        monkeypatch.delenv("JOBAGENT_USAJOBS_EMAIL", raising=False)
        result = UsaJobsAdapter().discover(DiscoveryRequest(fetcher=FakeFetcher()))
        assert result.report.status is SourceStatus.SKIPPED
        assert "JOBAGENT_USAJOBS_KEY" in result.report.note

    def test_federal_postings_are_mapped(self, monkeypatch):
        monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "test-key")
        monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "user@example.com")
        fetcher = FakeFetcher(routes={"data.usajobs.gov": fixture("usajobs_search")})
        result = UsaJobsAdapter(max_pages=1).discover(
            DiscoveryRequest(terms=["accountant"], fetcher=fetcher)
        )
        posting = result.postings[0]
        assert posting.title == "Accountant"
        assert posting.country_hint == "US"
        assert posting.salary.minimum == 72553.0
        assert "accounts payable" in posting.description_text.lower()

    def test_telework_eligible_is_not_reported_as_fully_remote(self, monkeypatch):
        """Telework-eligible and remote are different promises to a candidate."""
        monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "k")
        monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "u@example.com")
        fetcher = FakeFetcher(routes={"data.usajobs.gov": fixture("usajobs_search")})
        result = UsaJobsAdapter(max_pages=1).discover(DiscoveryRequest(fetcher=fetcher))
        assert result.postings[0].workplace_hint is WorkplaceType.HYBRID


class TestFailureHandling:
    """One bad board must never take down a run."""

    def _request(self, fetcher, count=3):
        return DiscoveryRequest(
            fetcher=fetcher, budget=50,
            targets=[BoardTarget(company=f"C{i}", token=f"t{i}") for i in range(count)],
        )

    def test_a_blocked_host_stops_that_platform_and_is_reported(self):
        fetcher = FakeFetcher(failures={"greenhouse": blocked("boards-api.greenhouse.io")})
        result = GreenhouseAdapter().discover(self._request(fetcher))
        assert result.report.status is SourceStatus.BLOCKED
        assert result.postings == []

    def test_unreachable_boards_report_unavailable_without_raising(self):
        fetcher = FakeFetcher(failures={"greenhouse": unavailable()})
        result = GreenhouseAdapter().discover(self._request(fetcher))
        assert result.report.status is SourceStatus.UNAVAILABLE

    def test_a_partial_failure_still_returns_what_worked(self):
        fetcher = FakeFetcher(
            routes={"boards/t0": fixture("greenhouse_board")},
            failures={"boards/t1": unavailable(), "boards/t2": unavailable()},
        )
        result = GreenhouseAdapter().discover(self._request(fetcher))
        assert result.report.status is SourceStatus.PARTIAL
        assert len(result.postings) == 3
        assert "2 unavailable" in result.report.note

    def test_an_empty_board_is_success_not_failure(self):
        """A real board with nothing open is healthy; counting it as failure rots the registry."""
        fetcher = FakeFetcher(routes={"boards-api.greenhouse.io": {"jobs": []}})
        result = GreenhouseAdapter().discover(self._request(fetcher, count=1))
        assert result.report.status is SourceStatus.OK

    def test_malformed_payload_does_not_crash_the_run(self):
        fetcher = FakeFetcher(routes={"boards-api.greenhouse.io": {"jobs": "not-a-list"}})
        result = GreenhouseAdapter().discover(self._request(fetcher, count=1))
        assert result.report.status is SourceStatus.OK
        assert result.postings == []

    def test_no_registered_boards_is_skipped_not_failed(self):
        result = GreenhouseAdapter().discover(DiscoveryRequest(fetcher=FakeFetcher(), targets=[]))
        assert result.report.status is SourceStatus.SKIPPED


class TestDoctorGate:
    def test_gate_fails_when_nothing_is_reachable(self):
        from jobagent.engine.doctor import run_doctor
        from jobagent.infra.store import Store
        from jobagent.sources.registry import seed_registry

        store = Store(":memory:")
        seed_registry(store)
        fetcher = FakeFetcher(failures={"https://": unavailable("egress blocked")})
        result = run_doctor(store, fetcher)

        assert not result.passed
        assert result.routable == 0
        assert any("ATS adapters" in f for f in result.failures)

    def test_gate_reports_registry_size_even_when_sources_fail(self):
        """The registry is the discovery surface, so its size is always worth reporting."""
        from jobagent.engine.doctor import run_doctor
        from jobagent.infra.store import Store
        from jobagent.sources.registry import seed_registry

        store = Store(":memory:")
        seed_registry(store)
        result = run_doctor(store, FakeFetcher(failures={"https://": unavailable()}))
        assert sum(result.registry_counts.values()) > 1000
