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

from jobagent.domain.dedup import identity_for
from jobagent.domain.gates import RequirementGate
from jobagent.domain.models import (
    AuthorityTier,
    GateOutcome,
    RawPosting,
    SourceStatus,
    WorkplaceType,
)
from jobagent.domain.normalize import build_job
from jobagent.domain.taxonomy import Taxonomy
from jobagent.ports import DiscoveryRequest
from jobagent.sources.ats.ashby import AshbyAdapter
from jobagent.sources.ats.base import BoardTarget, prioritize_for_details
from jobagent.sources.ats.greenhouse import GreenhouseAdapter
from jobagent.sources.ats.lever import LeverAdapter
from jobagent.sources.ats.workable import WorkableAdapter
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

    def test_lists_join_the_body_under_their_headings(self):
        """Lever splits a posting across ``description``, ``lists`` and ``additional``;
        reading only the first meant duties and requirements never reached the gates."""
        text = self.postings[2].description_text
        assert "What You’ll Do\n- Own the AP cycle" in text
        assert "Requirements\n- Active CPA license required" in text
        assert text.endswith("Equal opportunity employer.")
        html = self.postings[2].description_html
        assert "<h3>Requirements</h3><div><li>Active CPA license required</li>" in html

    def test_a_requirement_stated_only_in_a_list_is_detected(self):
        """A "CPA required" bullet under Requirements lives in ``lists``, so the
        requirement gate never saw it and let the posting through."""
        posting = self.postings[2]
        job = build_job(posting, Taxonomy.default(), identity_for(
            posting.company, posting.title, posting.location_raw, posting.country_hint
        ))
        gate = RequirementGate(term="CPA", when="required")
        result = gate.evaluate(job, Taxonomy.default(), date(2026, 9, 14))
        assert result.outcome is GateOutcome.FAIL
        assert "CPA license required" in result.detail

    def test_a_posting_without_lists_is_unchanged(self):
        """A board that publishes only the opening paragraph reads exactly as before."""
        raw = fixture("lever_board")[0]
        assert self.postings[0].description_text == raw["descriptionPlain"]
        assert self.postings[0].description_html == raw["description"]


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

    def test_structured_salary_comes_from_the_tier_components(self):
        """Compensation is nested under ``compensation``; reading it at the top level of
        the job, as the adapter used to, left salary empty on every live board."""
        salary = self.postings[0].salary
        assert (salary.minimum, salary.maximum, salary.currency, salary.period) == (
            80000.0, 100000.0, "USD", "year",
        )

    def test_summary_string_is_the_fallback_when_tiers_are_empty(self):
        """Some boards publish only the summary text, with an en dash, not a hyphen."""
        salary = self.postings[1].salary
        assert (salary.minimum, salary.maximum) == (90000.0, 110000.0)

    def test_missing_or_equity_only_compensation_yields_no_salary(self):
        """Live boards omit the key, publish null, or publish only an equity component;
        none of those is a salary and none may raise."""
        equity_only = {
            "compensationTierSummary": "Offers Equity",
            "scrapeableCompensationSalarySummary": None,
            "compensationTiers": [{
                "id": "t", "tierSummary": "Offers Equity", "title": None,
                "additionalInformation": None,
                "components": [{
                    "id": "c", "summary": "Offers Equity",
                    "compensationType": "EquityPercentage", "interval": "NONE",
                    "currencyCode": None, "minValue": None, "maxValue": None,
                }],
            }],
            "summaryComponents": [{
                "compensationType": "EquityPercentage", "interval": "NONE",
                "currencyCode": None, "minValue": None, "maxValue": None,
            }],
        }
        payload = {"jobs": [
            {"id": "n-1", "title": "Role One", "jobUrl": "https://jobs.ashbyhq.com/acme/n-1"},
            {"id": "n-2", "title": "Role Two", "jobUrl": "https://jobs.ashbyhq.com/acme/n-2",
             "compensation": None},
            {"id": "n-3", "title": "Role Three", "jobUrl": "https://jobs.ashbyhq.com/acme/n-3",
             "compensation": equity_only},
        ]}
        fetcher = FakeFetcher(routes={"api.ashbyhq.com": payload})
        postings = AshbyAdapter().fetch_board(fetcher, acme())
        assert [p.salary for p in postings] == [None, None, None]


class TestWorkable:
    """The fixture's shape is copied from a live capture, 2026-09-15 (see fixtures)."""

    def setup_method(self):
        self.fetcher = FakeFetcher(routes={"apply.workable.com": fixture("workable_board")})
        self.postings = WorkableAdapter().fetch_board(self.fetcher, acme())
        self.by_id = {p.external_id: p for p in self.postings}

    def test_details_are_requested_in_one_call_per_board(self):
        assert len(self.fetcher.calls) == 1
        assert "/api/v1/widget/accounts/acme" in self.fetcher.calls[0]

    def test_one_job_in_several_cities_is_one_posting_with_every_city(self):
        """The widget repeats a multi-city job once per city under one shortcode.

        Keeping the copies separate gave the deduper four postings with one identity,
        and whichever city survived decided whether a metro search saw the job.
        """
        assert len(self.postings) == 4
        folded = self.by_id["W1AAAA0001"].location_raw
        assert folded == "Dallas, Texas, United States; New York, New York, United States"

    def test_a_folded_posting_still_matches_a_metro_search(self):
        from jobagent.domain.normalize import parse_location

        location = parse_location(self.by_id["W1AAAA0001"].location_raw, "US")
        assert (location.city, location.region, location.country) == ("Dallas", "TX", "US")
        assert "New York" in location.raw

    def test_country_comes_from_the_iso_code_when_locations_agree(self):
        assert self.by_id["W1AAAA0001"].country_hint == "US"
        assert self.by_id["W3CCCC0003"].country_hint == "US"

    def test_mixed_non_us_locations_leave_the_country_to_text_detection(self):
        assert self.by_id["W2BBBB0002"].country_hint is None
        assert "Madrid" in self.by_id["W2BBBB0002"].location_raw

    def test_telecommuting_is_remote_and_its_absence_is_unknown_not_onsite(self):
        assert self.by_id["W2BBBB0002"].workplace_hint is WorkplaceType.REMOTE
        assert self.by_id["W1AAAA0001"].workplace_hint is WorkplaceType.UNKNOWN

    def test_requirements_and_benefits_join_the_description_when_published(self):
        html = self.by_id["W3CCCC0003"].description_html
        assert "Run client projects" in html and "PMP preferred" in html
        assert "401k" in html

    def test_apply_url_dates_and_authority(self):
        posting = self.by_id["W1AAAA0001"]
        assert posting.apply_url.endswith("/apply")
        assert posting.posted_at == date(2026, 7, 28)
        assert posting.authority is AuthorityTier.OFFICIAL_ATS
        assert posting.employment_type == "Full-time"

    def test_entries_with_no_identity_and_non_dict_entries_are_skipped(self):
        """A board with a stray string and an id-less draft must not raise or leak."""
        assert "" not in self.by_id
        assert all(p.title != "Untitled draft with no identity" for p in self.postings)

    def test_flat_location_fields_are_used_when_the_structured_list_is_absent(self):
        posting = self.by_id["W4DDDD0004"]
        assert posting.location_raw == "Plano, Texas, United States"
        # No ISO code without the structured list; text detection takes over.
        assert posting.country_hint is None

    def test_workable_publishes_no_pay(self):
        """Recorded as a fact about the source, so the pay gate reports unverifiable."""
        assert all(p.salary is None for p in self.postings)

    def test_a_missing_tenant_is_gone_not_empty(self):
        from jobagent.ports import FetchError

        with pytest.raises(FetchError) as info:
            WorkableAdapter().fetch_board(FakeFetcher(), acme())
        assert info.value.status == 404


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


class TestWorkdayDetailBudget:
    """Two live defects in how a Workday board's request budget was spent.

    Details went to the first ``max_details`` postings in listing order regardless of
    title, so the budget bought descriptions for unrelated jobs while the one the user
    asked about stayed blind; and paging ignored the response's ``total``, costing a board
    with exactly one full page a second request that could only come back empty.
    """

    def _target(self, search_text: str = "") -> BoardTarget:
        extra = {"wd": "5", "site": "External"}
        if search_text:
            extra["search_text"] = search_text
        return BoardTarget(company="Acme", token="acme", extra=extra)

    def test_title_matched_postings_are_enriched_first(self):
        """The list puts the unrelated job first; the one detail request must skip it."""
        fetcher = FakeFetcher(routes={
            "/jobs#offset=0": fixture("workday_list"),
            "Project-Manager_R-1": fixture("workday_detail_1"),
            "AP-Clerk_R-2": fixture("workday_detail_2"),
        })
        WorkdayAdapter(today=date(2026, 9, 14), max_details=1).fetch_board(
            fetcher, self._target("Accounts Payable")
        )
        details = [call.rsplit("/", 1)[-1] for call in fetcher.calls if "/job/" in call]
        assert details == ["AP-Clerk_R-2"]

    def test_total_ends_pagination_without_an_extra_request(self):
        """A full page whose ``total`` equals the page size is the whole board."""
        full_page = {"total": 20, "jobPostings": [
            {"title": f"Role {i}", "externalPath": f"/job/X/Role_{i}",
             "locationsText": "Dallas, TX", "postedOn": "Posted 1 Day Ago"}
            for i in range(20)
        ]}
        fetcher = FakeFetcher(routes={"/jobs#offset=0": full_page})
        postings = WorkdayAdapter(today=date(2026, 9, 14), max_details=0).fetch_board(
            fetcher, self._target()
        )
        assert len(postings) == 20
        assert not [call for call in fetcher.calls if "#offset=20" in call]

    def test_total_is_per_query_not_cumulative(self):
        """``total`` describes one query; the first term's total must not end the second."""
        from jobagent.ports import FetchResponse
        from jobagent.sources.ats.workday import TERM_SEPARATOR

        answers = {
            "Accountant": {"total": 1, "jobPostings": [
                {"title": "Accountant", "externalPath": "/job/Dallas/Accountant_R-9",
                 "locationsText": "Dallas, TX", "postedOn": "Posted 2 Days Ago"}]},
            "Accounts Payable": {"total": 1, "jobPostings": [
                {"title": "Accounts Payable Clerk", "externalPath": "/job/Dallas/AP_R-8",
                 "locationsText": "Dallas, TX", "postedOn": "Posted 3 Days Ago"}]},
        }

        class TermAware(FakeFetcher):
            def post_json(self, url, *, payload, headers=None):
                self.calls.append(url)
                body = {"total": 0, "jobPostings": []}
                if payload["offset"] == 0:
                    body = answers.get(payload["searchText"], body)
                return FetchResponse(url=url, status=200, text=json.dumps(body))

        fetcher = TermAware()
        terms = TERM_SEPARATOR.join(["Accountant", "Accounts Payable"])
        postings = WorkdayAdapter(today=date(2026, 9, 14), max_details=0).fetch_board(
            fetcher, self._target(terms)
        )
        assert {p.title for p in postings} == {"Accountant", "Accounts Payable Clerk"}
        assert len(fetcher.calls) == 2  # one list request per term, no second page

    def test_prioritize_for_details_is_stable_and_capped(self):
        """Matches first, then the rest, each in listing order; word-bounded; capped."""
        def posting(title: str) -> RawPosting:
            return RawPosting(source="workday", external_id=title, title=title,
                              company="Acme", url="https://acme.example/" + title)

        postings = [posting(t) for t in ("Project Manager", "Graphic Designer",
                                          "Analyst", "Senior Project Manager")]

        def titles(terms: list[str], cap: int) -> list[str]:
            return [p.title for p in prioritize_for_details(postings, terms, cap)]

        assert titles(["Project Manager"], 4) == [
            "Project Manager", "Senior Project Manager", "Graphic Designer", "Analyst",
        ]
        assert titles(["Project Manager"], 2) == ["Project Manager", "Senior Project Manager"]
        assert titles(["Project Manager"], 0) == []
        assert titles([], 2) == ["Project Manager", "Graphic Designer"]
        # Word boundaries: a bare substring search would move "Graphic Designer" first.
        assert titles(["AP"], 4) == [p.title for p in postings]


class TestProgressUnits:
    """One call per unit of work actually done, so a bar can move as boards are read."""

    def _request(self, fetcher, count, seen):
        return DiscoveryRequest(
            fetcher=fetcher, budget=50, on_unit=seen.append,
            targets=[BoardTarget(company=f"C{i}", token=f"t{i}") for i in range(count)],
        )

    def test_each_board_read_reports_what_it_found(self):
        seen: list[int] = []
        fetcher = FakeFetcher(routes={"boards-api.greenhouse.io": fixture("greenhouse_board")})
        GreenhouseAdapter().discover(self._request(fetcher, 2, seen))
        assert seen == [3, 3]

    def test_a_dead_board_is_still_a_unit_done(self):
        seen: list[int] = []
        GreenhouseAdapter().discover(self._request(FakeFetcher(), 2, seen))  # 404s
        assert seen == [0, 0]

    def test_boards_skipped_after_a_block_are_not_units(self):
        """The display completes the remainder itself when the source finishes."""
        seen: list[int] = []
        fetcher = FakeFetcher(failures={"greenhouse": blocked("boards-api.greenhouse.io")})
        GreenhouseAdapter().discover(self._request(fetcher, 3, seen))
        assert seen == [0]

    def test_usajobs_reports_one_unit_per_request(self, monkeypatch):
        monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "k")
        monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "u@example.com")
        seen: list[int] = []
        fetcher = FakeFetcher(routes={"data.usajobs.gov": fixture("usajobs_search")})
        UsaJobsAdapter(max_pages=1).discover(
            DiscoveryRequest(
                terms=["a", "b"], locations=["x"], fetcher=fetcher, on_unit=seen.append
            )
        )
        assert len(seen) == 2 and all(n > 0 for n in seen)
