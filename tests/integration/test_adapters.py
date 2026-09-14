"""Adapters, driven entirely by replayed fixtures.

These prove each adapter maps its platform's documented fields onto RawPosting correctly,
and that failures degrade into reports rather than exceptions. They cannot prove the live
endpoints still behave as documented -- see tests/fixtures/__init__.py.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

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


class TestHttpStatusIsNotSilentlySuccess:
    """Regression: a non-2xx response was indistinguishable from an empty board.

    404, 500 and 401 all produced `status: ok, found: 0, note: "3 boards read"`. The user
    saw "No matches" over a coverage table claiming every source had been searched, and
    the registry reset each board's failure count on every 404, so dead tenants were
    never evicted.
    """

    def _request(self, fetcher, count=3):
        return DiscoveryRequest(
            fetcher=fetcher, budget=50,
            targets=[BoardTarget(company=f"C{i}", token=f"t{i}") for i in range(count)],
        )

    @pytest.mark.parametrize(
        "adapter",
        [GreenhouseAdapter(), LeverAdapter(), AshbyAdapter()],
        ids=["greenhouse", "lever", "ashby"],
    )
    @pytest.mark.parametrize("status", [404, 500, 502])
    def test_error_statuses_are_reported_as_unavailable(self, adapter, status):
        fetcher = FakeFetcher(default_status=status)
        result = adapter.discover(self._request(fetcher))
        assert result.report.status is SourceStatus.UNAVAILABLE, (
            f"HTTP {status} reported as {result.report.status.value}"
        )
        assert result.postings == []

    @pytest.mark.parametrize(
        "adapter",
        [GreenhouseAdapter(), LeverAdapter(), AshbyAdapter()],
        ids=["greenhouse", "lever", "ashby"],
    )
    @pytest.mark.parametrize("status", [401, 403])
    def test_refusals_block_the_host(self, adapter, status):
        fetcher = FakeFetcher(default_status=status)
        result = adapter.discover(self._request(fetcher))
        assert result.report.status is SourceStatus.BLOCKED

    def test_a_404_counts_against_the_board_but_a_500_does_not(self):
        """Registry health must tell a removed tenant from a bad afternoon."""
        gone = GreenhouseAdapter().discover(self._request(FakeFetcher(default_status=404)))
        unwell = GreenhouseAdapter().discover(self._request(FakeFetcher(default_status=500)))
        assert {o.failure_kind for o in gone.report.__dict__["outcomes"]} == {"gone"}
        assert {o.failure_kind for o in unwell.report.__dict__["outcomes"]} == {"unavailable"}

    def test_a_mistyped_usajobs_key_is_blocked_not_empty(self, monkeypatch):
        """The likeliest real occurrence: a green, permanently empty source."""
        monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "wrong")
        monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "user@example.com")
        fetcher = FakeFetcher(default_status=401)
        result = UsaJobsAdapter(max_pages=1).discover(
            DiscoveryRequest(terms=["analyst"], fetcher=fetcher)
        )
        assert result.report.status is SourceStatus.BLOCKED
        assert "credentials" in result.report.note.lower()


class TestWorkdayEnrichmentFailures:
    def test_a_blocked_host_during_enrichment_is_not_swallowed(self):
        """Regression: a bare except caught HostBlocked and reported the run healthy.

        Descriptions are what the workplace, requirement and salary gates read, so
        returning postings without them and calling the run OK presents degradation as
        health.
        """
        from jobagent.ports import FetchError

        fetcher = FakeFetcher(
            routes={"/jobs#offset=0": fixture("workday_list")},
            failures={"/job/": FetchError("rate limited", blocked=True, status=429)},
        )
        target = BoardTarget(company="Acme", token="acme", extra={"wd": "5", "site": "External"})
        result = WorkdayAdapter(today=date(2026, 9, 14)).discover(
            DiscoveryRequest(fetcher=fetcher, budget=5, targets=[target])
        )
        assert result.report.status is SourceStatus.BLOCKED

    def test_a_missing_description_alone_does_not_fail_the_board(self):
        from jobagent.ports import FetchError

        fetcher = FakeFetcher(
            routes={"/jobs#offset=0": fixture("workday_list")},
            failures={"/job/": FetchError("connection reset")},
        )
        target = BoardTarget(company="Acme", token="acme", extra={"wd": "5", "site": "External"})
        result = WorkdayAdapter(today=date(2026, 9, 14)).discover(
            DiscoveryRequest(fetcher=fetcher, budget=5, targets=[target])
        )
        assert result.report.status is SourceStatus.OK
        assert len(result.postings) == 2


class TestUsaJobsWorkplaceIsNotGuessed:
    def test_an_unstated_arrangement_stays_unknown(self, monkeypatch):
        """Regression: the adapter asserted ONSITE, so remote searches rejected everything."""
        monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "k")
        monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "u@example.com")
        payload = fixture("usajobs_search")
        details = payload["SearchResult"]["SearchResultItems"][0]["MatchedObjectDescriptor"]
        details["UserArea"]["Details"].pop("TeleworkEligible")

        fetcher = FakeFetcher(routes={"data.usajobs.gov": payload})
        result = UsaJobsAdapter(max_pages=1).discover(DiscoveryRequest(fetcher=fetcher))
        assert result.postings[0].workplace_hint is WorkplaceType.UNKNOWN

    def test_an_explicit_no_is_still_onsite(self, monkeypatch):
        monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "k")
        monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "u@example.com")
        payload = fixture("usajobs_search")
        details = payload["SearchResult"]["SearchResultItems"][0]["MatchedObjectDescriptor"]
        details["UserArea"]["Details"]["TeleworkEligible"] = "false"

        fetcher = FakeFetcher(routes={"data.usajobs.gov": payload})
        result = UsaJobsAdapter(max_pages=1).discover(DiscoveryRequest(fetcher=fetcher))
        assert result.postings[0].workplace_hint is WorkplaceType.ONSITE


class TestRegistryUpsertReporting:
    def test_reseeding_reports_existing_rows_as_existing(self):
        """Regression: SQLite reports rowcount 1 for an upsert, so every row looked new."""
        from jobagent.infra.store import Store
        from jobagent.sources.registry import seed_registry

        store = Store(":memory:")
        first = seed_registry(store)
        second = seed_registry(store)
        assert first.added > 0 and first.existing == 0
        assert second.added == 0
        assert second.existing == first.added

    def test_add_company_returns_false_for_an_update(self):
        from jobagent.infra.store import Store

        store = Store(":memory:")
        assert store.add_company("Acme", "greenhouse", "acme") is True
        assert store.add_company("Acme Corp", "greenhouse", "acme") is False


class TestWorkdayIdentityIsNamespaced:
    """Regression: a Workday requisition id is unique only within a tenant and site.

    The bare tail ("R-1") was used both for within-source deduplication and for
    cross-board clustering, so two employers sharing one could drop a posting or merge
    two unrelated jobs into a single record.
    """

    def _board(self, token: str, wd: str, site: str, details: bool = True):
        fetcher = FakeFetcher(routes={
            "/jobs#offset=0": fixture("workday_list"),
            "Project-Manager_R-1": fixture("workday_detail_1"),
            "AP-Clerk_R-2": fixture("workday_detail_2"),
        })
        target = BoardTarget(company=token, token=token, extra={"wd": wd, "site": site})
        adapter = WorkdayAdapter(today=date(2026, 9, 14), max_details=20 if details else 0)
        return adapter.fetch_board(fetcher, target)

    def test_two_tenants_sharing_a_requisition_id_stay_distinct(self):
        # Enrichment is off here so the test isolates the id. The shared fixture hands
        # both boards the same externalUrl, which the clusterer correctly treats as one
        # job -- true of the fixture, not of two real employers.
        first = self._board("acme", "5", "External", details=False)
        second = self._board("globex", "1", "Careers", details=False)
        assert first[0].external_id != second[0].external_id

        from jobagent.domain.dedup import cluster_postings

        assert len(cluster_postings(first + second)) == 4

    def test_the_id_carries_the_tenancy(self):
        posting = self._board("acme", "5", "External")[0]
        assert posting.external_id.startswith("acme:5:External:")

    def test_two_sites_of_one_tenant_are_two_boards(self):
        """One careers page can link to several sites; keying on the tenant lost all but one."""
        from jobagent.sources.discovery import extract_board

        a = extract_board("https://acme.wd5.myworkdayjobs.com/en-US/External")
        b = extract_board("https://acme.wd5.myworkdayjobs.com/en-US/Campus")
        assert a.key() != b.key()

    def test_the_run_date_reaches_the_adapter(self):
        """Relative dates otherwise shift at a date boundary."""
        fetcher = FakeFetcher(routes={"/jobs#offset=0": fixture("workday_list")})
        target = BoardTarget(company="Acme", token="acme", extra={"wd": "5", "site": "External"})
        result = WorkdayAdapter().discover(
            DiscoveryRequest(fetcher=fetcher, budget=5, targets=[target],
                             today=date(2026, 9, 14))
        )
        assert result.postings[0].posted_at == date(2026, 9, 9)


class TestPaginationFailuresAreNotEndOfResults:
    def test_a_workday_page_two_failure_is_reported(self):
        """Regression: a board with >20 postings returned page one and reported OK."""
        full_page = {"total": 40, "jobPostings": [
            {"title": f"Role {i}", "externalPath": f"/job/X/Role_{i}",
             "locationsText": "Dallas, TX", "postedOn": "Posted 1 Day Ago"}
            for i in range(20)
        ]}
        fetcher = FakeFetcher(routes={"/jobs#offset=0": full_page}, default_status=500)
        target = BoardTarget(company="Acme", token="acme", extra={"wd": "5", "site": "External"})
        result = WorkdayAdapter(today=date(2026, 9, 14), max_details=0).discover(
            DiscoveryRequest(fetcher=fetcher, budget=5, targets=[target])
        )
        assert result.report.status is not SourceStatus.OK

    def test_a_usajobs_first_page_failure_is_not_a_healthy_empty_search(self, monkeypatch):
        monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "k")
        monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "u@example.com")
        fetcher = FakeFetcher(default_status=500)
        result = UsaJobsAdapter(max_pages=2).discover(
            DiscoveryRequest(terms=["analyst"], fetcher=fetcher, budget=10)
        )
        assert result.report.status is not SourceStatus.OK
        assert result.postings == []


class TestWorkdayQueriesEveryTerm:
    """Regression: only the first search term was pushed down.

    Workday is the one platform with real server-side search, so narrowing it to one term
    discards jobs matching any other title before they are ever retrieved -- and no local
    filter can recover what was never fetched.
    """

    def _fetcher(self):
        accountant = {"total": 1, "jobPostings": [
            {"title": "Accountant", "externalPath": "/job/Dallas/Accountant_R-9",
             "locationsText": "Dallas, TX", "postedOn": "Posted 2 Days Ago"}]}
        clerk = {"total": 1, "jobPostings": [
            {"title": "Accounts Payable Clerk", "externalPath": "/job/Dallas/AP_R-8",
             "locationsText": "Dallas, TX", "postedOn": "Posted 3 Days Ago"}]}

        class TermAware(FakeFetcher):
            def post_json(self, url, *, payload, headers=None):
                self.calls.append(url)
                self._requests += 1
                term = (payload or {}).get("searchText", "")
                offset = (payload or {}).get("offset", 0)
                body = {"total": 0, "jobPostings": []}
                if offset == 0:
                    body = accountant if term == "Accountant" else (
                        clerk if term == "Accounts Payable" else body
                    )
                from jobagent.ports import FetchResponse

                return FetchResponse(url=url, status=200, text=json.dumps(body))

        return TermAware()

    def _run(self, terms: str):
        target = BoardTarget(company="Acme", token="acme",
                             extra={"wd": "5", "site": "External", "search_text": terms})
        return WorkdayAdapter(today=date(2026, 9, 14), max_details=0).fetch_board(
            self._fetcher(), target
        )

    def test_a_second_term_still_finds_its_jobs(self):
        from jobagent.sources.ats.workday import TERM_SEPARATOR

        titles = {p.title for p in self._run(TERM_SEPARATOR.join(["Accountant",
                                                                 "Accounts Payable"]))}
        assert titles == {"Accountant", "Accounts Payable Clerk"}

    def test_one_term_alone_finds_only_its_own(self):
        assert {p.title for p in self._run("Accountant")} == {"Accountant"}

    def test_a_job_answering_two_terms_is_collected_once(self):
        from jobagent.sources.ats.workday import TERM_SEPARATOR

        postings = self._run(TERM_SEPARATOR.join(["Accountant", "Accountant"]))
        assert len(postings) == 1

    def test_planning_sends_every_term(self):
        from jobagent.domain.spec import SearchSpec
        from jobagent.engine.planning import build_plans
        from jobagent.infra.store import Store
        from jobagent.sources.ats.workday import TERM_SEPARATOR

        store = Store(":memory:")
        store.add_company("Acme", "workday", "acme:5:External", us_signal=True)
        spec = SearchSpec(name="t", titles=["Accountant", "Bookkeeper"], source_budget=10)
        plans = build_plans(spec, store, FakeFetcher(), date(2026, 9, 14),
                            only_sources=["workday"])
        sent = plans[0].request.targets[0].extra["search_text"]
        assert TERM_SEPARATOR.join(["Accountant", "Bookkeeper"]) == sent
