"""The full pipeline, offline: discovery through persistence.

Everything here runs against replayed fixtures, so it exercises the same code paths a
live run would -- including the ones that only appear when a source misbehaves.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from jobagent.domain.models import SourceStatus, UserStatus
from jobagent.domain.spec import SearchSpec
from jobagent.engine.orchestrator import run_search
from jobagent.engine.verify import verify_jobs
from jobagent.infra.store import Store
from tests.fakes import FakeFetcher, unavailable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TODAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 12, 0, 0)


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def seeded(store: Store) -> Store:
    """A small registry, so tests are not fanning out over 1,755 real boards."""
    store.add_company("Acme Corp", "greenhouse", "acme", domain="acme.com", us_signal=True)
    store.add_company("Beta Inc", "lever", "beta", domain="beta.com", us_signal=True)
    store.add_company("Gamma LLC", "ashby", "gamma", domain="gamma.com", us_signal=True)
    return store


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher(routes={
        "boards-api.greenhouse.io": fixture("greenhouse_board"),
        "api.lever.co": fixture("lever_board"),
        "api.ashbyhq.com": fixture("ashby_board"),
    })


def accounting_spec(**overrides) -> SearchSpec:
    data = {
        "name": "accounting",
        "titles": ["Accounts Payable", "Junior Accountant", "Accountant", "Cash Management"],
        "excluded_titles": ["Staff Accountant"],
        "excluded_keywords": ["auditing", "budget creation", "forecasting"],
        "seniority_exclude": ["senior", "staff", "management"],
        "excluded_requirements": [{"term": "CPA"}],
        "workplace": ["remote"],
        "countries": ["US"],
        "source_budget": 10,
    }
    data.update(overrides)
    return SearchSpec(**data)


class TestEndToEnd:
    def test_a_run_discovers_filters_and_persists(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        outcome = run_search(
            spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
            only_sources=["greenhouse", "lever", "ashby"],
        )
        assert outcome.found > 0
        assert outcome.matched > 0
        assert outcome.matched + outcome.rejected > 0

    def test_the_benchmark_exclusions_actually_bite(self, seeded, fetcher):
        """Senior, CPA-required, Staff and non-US postings are all in the fixtures."""
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        outcome = run_search(
            spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
            only_sources=["greenhouse", "lever", "ashby"],
        )
        matched = {job.title for job, _, _ in outcome.matches}
        assert "Senior Accountant" not in matched       # CPA required AND senior
        assert "Staff Accountant" not in matched         # excluded title
        assert "Internal Auditor" not in matched         # excluded content

    def test_rejections_carry_a_stated_reason(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse", "lever", "ashby"])
        rejected = seeded.results(search_id, decision="rejected", limit=50)
        assert rejected
        for row in rejected:
            explanation = json.loads(row["explanation"])
            failing = [g for g in explanation["gates"] if g["outcome"] == "fail"]
            assert failing, f"{row['title']} was rejected with no failing gate"
            assert failing[0]["rule"]


class TestNewVersusSeen:
    """"I found this today" must be distinguishable from "I showed you this before"."""

    def test_first_run_marks_everything_new(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        outcome = run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                             only_sources=["greenhouse", "lever", "ashby"])
        assert outcome.new == outcome.matched
        assert outcome.new > 0

    def test_second_run_over_the_same_data_finds_nothing_new(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        first = run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                           only_sources=["greenhouse", "lever", "ashby"])
        second = run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                            only_sources=["greenhouse", "lever", "ashby"])
        assert first.new > 0
        assert second.new == 0
        assert second.matched == first.matched

    def test_a_genuinely_new_posting_is_reported_as_new(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse"])

        board = fixture("greenhouse_board")
        board["jobs"].append({
            "id": 4099, "title": "Accounts Payable Analyst",
            "first_published": "2026-09-13T10:00:00-04:00",
            "updated_at": "2026-09-13T10:00:00-04:00",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4099",
            "content": "&lt;p&gt;Accounts payable analysis. Fully remote, US.&lt;/p&gt;",
            "departments": [], "offices": [],
        })
        fetcher.routes["boards-api.greenhouse.io"] = board

        second = run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                            only_sources=["greenhouse"])
        assert second.new == 1
        assert [j.title for j, _, is_new in second.matches if is_new] == \
               ["Accounts Payable Analyst"]

    def test_only_new_filter_returns_the_new_row(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse", "lever", "ashby"])
        assert seeded.results(search_id, only_new=True, limit=50)


class TestCoverageReporting:
    def test_every_source_is_accounted_for(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        outcome = run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW)
        sources = {r.source for r in outcome.coverage.reports}
        assert {"greenhouse", "lever", "ashby", "usajobs"} <= sources

    def test_one_dead_source_does_not_fail_the_run(self, seeded):
        """The core promise of the coverage model."""
        partial = FakeFetcher(
            routes={"api.lever.co": fixture("lever_board")},
            failures={"greenhouse": unavailable("ConnectError"),
                      "ashbyhq": unavailable("ConnectError")},
        )
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        outcome = run_search(spec, seeded, partial, search_id=search_id, today=TODAY, now=NOW,
                             only_sources=["greenhouse", "lever", "ashby"])

        assert outcome.found > 0
        degraded = {r.source for r in outcome.coverage.degraded}
        assert "greenhouse" in degraded and "ashby" in degraded
        assert any(r.source == "lever" and r.status is SourceStatus.OK
                   for r in outcome.coverage.reports)

    def test_coverage_is_persisted_for_later_inspection(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        outcome = run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW)
        rows = seeded.run_sources(outcome.run_id)
        assert rows and {r["source"] for r in rows}

    def test_usajobs_is_skipped_not_failed_without_credentials(self, seeded, fetcher, monkeypatch):
        monkeypatch.delenv("JOBAGENT_USAJOBS_KEY", raising=False)
        monkeypatch.delenv("JOBAGENT_USAJOBS_EMAIL", raising=False)
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        outcome = run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                             only_sources=["usajobs"])
        report = outcome.coverage.reports[0]
        assert report.status is SourceStatus.SKIPPED


class TestAuthorityResolution:
    def test_the_employer_url_wins_over_the_ats_url(self, store):
        """The user should apply through the employer, not an intermediary."""
        from jobagent.domain.models import AuthorityTier, RawPosting
        from jobagent.domain.taxonomy import Taxonomy
        from jobagent.engine.resolve import resolve

        ats = RawPosting(
            source="greenhouse", external_id="1", title="Accountant", company="Acme",
            url="https://boards.greenhouse.io/acme/jobs/1", location_raw="Dallas, TX",
            description_text="Accounts payable.", authority=AuthorityTier.OFFICIAL_ATS,
        )
        site = RawPosting(
            source="careersite", external_id="1", title="Accountant", company="Acme",
            url="https://careers.acme.com/jobs/1", location_raw="Dallas, TX",
            description_text="Accounts payable.", authority=AuthorityTier.EMPLOYER_SITE,
        )
        jobs = resolve([ats, site], Taxonomy.default(), NOW)
        assert len(jobs) == 1
        assert jobs[0].url == "https://careers.acme.com/jobs/1"
        assert len(jobs[0].sources) == 2  # provenance is kept, not discarded

    def test_a_description_is_borrowed_from_a_lesser_record_when_missing(self, store):
        from jobagent.domain.models import AuthorityTier, RawPosting
        from jobagent.domain.taxonomy import Taxonomy
        from jobagent.engine.resolve import resolve

        bare = RawPosting(
            source="careersite", external_id="1", title="Accountant", company="Acme",
            url="https://careers.acme.com/jobs/1", location_raw="Dallas, TX",
            description_text="", authority=AuthorityTier.EMPLOYER_SITE,
        )
        rich = RawPosting(
            source="greenhouse", external_id="1", title="Accountant", company="Acme",
            url="https://boards.greenhouse.io/acme/jobs/1", location_raw="Dallas, TX",
            description_text="Own accounts payable.", authority=AuthorityTier.OFFICIAL_ATS,
        )
        job = resolve([bare, rich], Taxonomy.default(), NOW)[0]
        assert job.url.startswith("https://careers.acme.com")
        assert "accounts payable" in job.description_text.lower()


class TestVerification:
    def test_a_404_closes_the_job(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse"])
        rows = seeded.results(search_id, limit=5)
        job_id = rows[0]["id"]

        gone = FakeFetcher(default_status=404)
        summary = verify_jobs(seeded, gone, [job_id], now=NOW)
        assert summary.gone == 1
        assert seeded.get_job(job_id)["verification"] == "gone"

    def test_an_unreachable_host_leaves_the_job_unverified(self, seeded, fetcher):
        """A network problem says something about us, not about the posting."""
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse"])
        job_id = seeded.results(search_id, limit=5)[0]["id"]

        broken = FakeFetcher(failures={"https://": unavailable()})
        summary = verify_jobs(seeded, broken, [job_id], now=NOW)
        assert summary.inconclusive == 1
        assert seeded.get_job(job_id)["verification"] == "unverified"

    def test_closing_a_job_does_not_overwrite_an_applied_status(self, seeded, fetcher):
        """The user's record of having applied outranks our liveness guess."""
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse"])
        job_id = seeded.results(search_id, limit=5)[0]["id"]
        seeded.set_user_status(job_id, UserStatus.APPLIED.value)

        verify_jobs(seeded, FakeFetcher(default_status=404), [job_id], now=NOW)
        row = seeded.results(search_id, decision=None, limit=50)
        status = next(r["user_status"] for r in row if r["id"] == job_id)
        assert status == UserStatus.APPLIED.value


class TestUserStatus:
    def test_status_transitions_are_recorded_with_history(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse"])
        job_id = seeded.results(search_id, limit=5)[0]["id"]

        seeded.set_user_status(job_id, UserStatus.SAVED.value)
        seeded.set_user_status(job_id, UserStatus.APPLIED.value, note="submitted")
        history = list(seeded.conn.execute(
            "SELECT status FROM user_job_status_history WHERE job_id=? ORDER BY id", (job_id,)
        ))
        assert [r["status"] for r in history] == ["saved", "applied"]

    def test_results_can_be_filtered_by_status(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse", "lever", "ashby"])
        job_id = seeded.results(search_id, limit=5)[0]["id"]
        seeded.set_user_status(job_id, UserStatus.SAVED.value)

        saved = seeded.results(search_id, statuses=["saved"], limit=50)
        assert [r["id"] for r in saved] == [job_id]


class TestVerificationDoesNotTrustStatusAlone:
    """Regression: a removed posting that redirects to the board root was marked LIVE.

    Several ATS platforms answer a deleted listing with 200 at a different URL. Trusting
    the status made filled roles look open indefinitely, which is the exact failure
    verify.py exists to prevent.
    """

    def _job(self, seeded, fetcher):
        spec = accounting_spec()
        search_id = seeded.save_spec(spec.name, spec.to_yaml())
        run_search(spec, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                   only_sources=["greenhouse"])
        return seeded.results(search_id, limit=5)[0]["id"]

    def test_a_redirect_to_the_board_root_is_inconclusive(self, seeded, fetcher):
        job_id = self._job(seeded, fetcher)

        class Redirecting(FakeFetcher):
            def get(self, url, *, params=None, headers=None):
                response = super().get(url, params=params, headers=headers)
                response.status = 200
                response.url = "https://boards.greenhouse.io/acme"  # the board, not the job
                return response

        summary = verify_jobs(seeded, Redirecting(), [job_id], now=NOW)
        assert summary.inconclusive == 1
        assert summary.live == 0
        assert seeded.get_job(job_id)["verification"] == "unverified"

    def test_the_same_url_answering_200_is_live(self, seeded, fetcher):
        job_id = self._job(seeded, fetcher)
        url = seeded.get_job(job_id)["url"]

        class SameUrl(FakeFetcher):
            def get(self, inner, *, params=None, headers=None):
                response = super().get(inner, params=params, headers=headers)
                response.status = 200
                response.url = url
                return response

        summary = verify_jobs(seeded, SameUrl(), [job_id], now=NOW)
        assert summary.live == 1


class TestRegistryHealthOnlyCountsBoardEvidence:
    """Regression: transient and host-wide problems evicted healthy boards.

    The schema separates failure kinds precisely so throttling is not mistaken for a dead
    tenant, and then every kind except rate limiting incremented the counter anyway. Five
    outages behind a shared host removed a board no one had shown to be gone.
    """

    @pytest.mark.parametrize("kind", ["rate_limited", "blocked", "host_blocked", "unavailable"])
    def test_host_and_transient_problems_do_not_count(self, store, kind):
        store.add_company("Acme", "greenhouse", "acme")
        row_id = store.registry_targets(limit=1)[0]["id"]
        for _ in range(6):
            store.record_board_outcome(row_id, ok=False, kind=kind)
        assert store.registry_targets(limit=5), f"{kind!r} evicted a board"

    def test_a_board_that_is_actually_gone_is_evicted(self):
        from jobagent.infra.store import Store

        store = Store(":memory:")
        store.add_company("Acme", "greenhouse", "acme")
        row_id = store.registry_targets(limit=1)[0]["id"]
        for _ in range(6):
            store.record_board_outcome(row_id, ok=False, kind="gone")
        assert not store.registry_targets(limit=5)

    def test_a_success_clears_the_counter(self, store):
        store.add_company("Acme", "greenhouse", "acme")
        row_id = store.registry_targets(limit=1)[0]["id"]
        for _ in range(3):
            store.record_board_outcome(row_id, ok=False, kind="gone")
        store.record_board_outcome(row_id, ok=True)
        assert store.registry_targets(limit=1)[0]["consecutive_failures"] == 0


class TestSparseRecordsDoNotEraseRichOnes:
    """Regression: an equal-authority update overwrote stored evidence with blanks."""

    def _posting(self, description: str, workplace=None):
        from jobagent.domain.models import AuthorityTier, RawPosting, WorkplaceType

        return RawPosting(
            source="greenhouse", external_id="1", title="Accountant", company="Acme",
            url="https://boards.greenhouse.io/acme/jobs/1", location_raw="Dallas, TX",
            description_text=description, authority=AuthorityTier.OFFICIAL_ATS,
            workplace_hint=workplace or WorkplaceType.UNKNOWN,
        )

    def test_a_later_blank_record_does_not_wipe_the_description(self, store):
        from jobagent.domain.models import WorkplaceType
        from jobagent.domain.taxonomy import Taxonomy
        from jobagent.engine.resolve import resolve

        rich = resolve([self._posting("Own accounts payable.", WorkplaceType.REMOTE)],
                       Taxonomy.default(), NOW)[0]
        job_id = store.upsert_job(rich, NOW)

        sparse = resolve([self._posting("")], Taxonomy.default(), NOW)[0]
        store.upsert_job(sparse, NOW)

        stored = store.get_job(job_id)
        assert "accounts payable" in stored["description_text"].lower()
        assert stored["workplace"] == "remote"

    def test_a_richer_record_still_updates(self, store):
        from jobagent.domain.taxonomy import Taxonomy
        from jobagent.engine.resolve import resolve

        thin = resolve([self._posting("Short.")], Taxonomy.default(), NOW)[0]
        job_id = store.upsert_job(thin, NOW)
        full = resolve([self._posting("A much longer and more useful description.")],
                       Taxonomy.default(), NOW)[0]
        store.upsert_job(full, NOW)
        assert "much longer" in store.get_job(job_id)["description_text"]


class TestRelaxingASearchSurfacesPreviouslyRejectedJobs:
    def test_a_job_rejected_before_counts_as_new_once_it_matches(self, seeded, fetcher):
        """Regression: any prior verdict marked a job seen, including a rejection.

        A rejected job was never put in front of the user, so when the search is relaxed
        and it qualifies, hiding it from --new hides it entirely.
        """
        strict = accounting_spec()
        search_id = seeded.save_spec(strict.name, strict.to_yaml())
        first = run_search(strict, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                           only_sources=["greenhouse"])

        relaxed = strict.model_copy(
            update={"seniority_exclude": [], "excluded_requirements": []}
        )
        seeded.save_spec(relaxed.name, relaxed.to_yaml())
        second = run_search(relaxed, seeded, fetcher, search_id=search_id, today=TODAY, now=NOW,
                            only_sources=["greenhouse"])

        assert second.matched > first.matched
        newly = {job.title for job, _, is_new in second.matches if is_new}
        assert "Senior Accountant" in newly


class TestDescriptionBorrowHandlesHtml:
    def test_an_html_only_record_supplies_the_missing_text(self, store):
        """Greenhouse and Workday send HTML and no plain text."""
        from jobagent.domain.models import AuthorityTier, RawPosting
        from jobagent.domain.taxonomy import Taxonomy
        from jobagent.engine.resolve import resolve

        bare = RawPosting(
            source="careersite", external_id="1", title="Accountant", company="Acme",
            url="https://careers.acme.com/jobs/1", location_raw="Dallas, TX",
            description_text="", authority=AuthorityTier.EMPLOYER_SITE,
        )
        html_only = RawPosting(
            source="greenhouse", external_id="1", title="Accountant", company="Acme",
            url="https://boards.greenhouse.io/acme/jobs/1", location_raw="Dallas, TX",
            description_html="<p>Own accounts payable and the close.</p>",
            authority=AuthorityTier.OFFICIAL_ATS,
        )
        job = resolve([bare, html_only], Taxonomy.default(), NOW)[0]
        assert "accounts payable" in job.description_text.lower()
