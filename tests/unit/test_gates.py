"""Hard gates other than the requirement gate."""

from __future__ import annotations

from datetime import date

import pytest

from jobagent.domain.gates import (
    CountryGate,
    FreshnessGate,
    PhraseExcludesGate,
    PhraseRequiresGate,
    SalaryFloorGate,
    SeniorityExcludeGate,
    TitleExcludesGate,
    UnverifiablePolicy,
    WorkplaceGate,
    responsibility_text,
)
from jobagent.domain.models import GateOutcome, WorkplaceType
from tests.conftest import TODAY, make_job


class TestCountryGate:
    """US-only scope. The remote cases are the ones that matter.

    A posting saying only "Remote" genuinely has not told us which country it is open to.
    Assuming it is domestic would quietly surface jobs the user cannot take; rejecting it
    would discard many they can. It is reported as unverifiable, and the search's policy
    decides.
    """

    @pytest.mark.parametrize(
        "location",
        ["Dallas, TX", "Dallas, Texas", "Remote - US", "US-based", "Remote (US)",
         "New York, NY", "United States", "Remote, United States"],
    )
    def test_us_locations_pass(self, location, taxonomy):
        job = make_job(location=location)
        assert CountryGate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    @pytest.mark.parametrize(
        "location", ["Toronto, Canada", "Berlin, Germany", "London, United Kingdom",
                     "Remote - EMEA", "Bangalore, India"],
    )
    def test_non_us_locations_fail(self, location, taxonomy):
        job = make_job(location=location)
        result = CountryGate().evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL

    def test_bare_remote_is_unverifiable(self, taxonomy):
        job = make_job(location="Remote", workplace=WorkplaceType.REMOTE)
        result = CountryGate().evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.UNVERIFIABLE
        assert "no country" in result.detail

    def test_strict_policy_turns_unverifiable_into_exclusion_at_match_time(self, taxonomy):
        """The gate still reports UNVERIFIABLE; policy is applied by the matcher.

        Keeping the distinction in the record is what lets the UI say "excluded because we
        could not confirm" rather than the misleading "excluded because it is not US".
        """
        job = make_job(location="Remote", workplace=WorkplaceType.REMOTE)
        gate = CountryGate(policy=UnverifiablePolicy.STRICT)
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE
        assert gate.policy is UnverifiablePolicy.STRICT

    def test_georgia_reads_as_the_us_state(self, taxonomy):
        """Ambiguous names resolve to the US reading, which is overwhelmingly correct."""
        job = make_job(location="Atlanta, Georgia")
        assert CountryGate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS


class TestWorkplaceGate:
    def test_hybrid_does_not_survive_a_remote_only_filter(self, taxonomy):
        job = make_job(title="Project Manager", location="Dallas, TX",
                       description="This is a hybrid role, 3 days onsite.")
        gate = WorkplaceGate(allowed=[WorkplaceType.REMOTE])
        result = gate.evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert result.evidence == "hybrid"

    def test_remote_passes_a_remote_only_filter(self, taxonomy):
        job = make_job(description="This is a 100% remote position.")
        gate = WorkplaceGate(allowed=[WorkplaceType.REMOTE])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_unstated_arrangement_is_unverifiable(self, taxonomy):
        job = make_job(description="Join our finance team.", location="Dallas, TX")
        gate = WorkplaceGate(allowed=[WorkplaceType.REMOTE])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE


class TestSeniorityGate:
    @pytest.mark.parametrize(
        "title", ["Senior Accountant", "Sr. Accountant", "Senior Software Engineer"]
    )
    def test_senior_titles_are_excluded(self, title, taxonomy):
        job = make_job(title=title)
        gate = SeniorityExcludeGate(levels=["senior"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_junior_survives_a_senior_exclusion(self, taxonomy):
        job = make_job(title="Junior Accountant")
        gate = SeniorityExcludeGate(levels=["senior"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_senior_staff_reads_as_staff(self, taxonomy):
        """Highest level named in the title wins, so the ladder does not mis-rank."""
        job = make_job(title="Senior Staff Engineer")
        assert SeniorityExcludeGate(levels=["staff"]).evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.FAIL


class TestPhraseGates:
    def test_excluded_phrase_rejects_and_quotes_evidence(self, taxonomy):
        job = make_job(description="You will lead the annual audit and forecasting cycle.")
        gate = PhraseExcludesGate(phrases=["auditing", "audit", "forecasting"])
        result = gate.evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert result.evidence == "audit"

    def test_word_boundaries_prevent_absurd_matches(self, taxonomy):
        """"AP" must not match "apply" -- otherwise it matches nearly every posting."""
        job = make_job(description="Please apply through our careers page.")
        gate = PhraseRequiresGate(phrases=["AP"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_phrase_matches_across_a_line_break(self, taxonomy):
        job = make_job(description="Daily cash\nmanagement and reconciliations.")
        gate = PhraseRequiresGate(phrases=["cash management"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_empty_posting_text_is_unverifiable(self, taxonomy):
        job = make_job(title="", company="", description="", location="")
        gate = PhraseExcludesGate(phrases=["audit"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE


class TestTitleExcludes:
    def test_excluded_title_phrase_rejects(self, taxonomy):
        job = make_job(title="Staff Accountant")
        gate = TitleExcludesGate(phrases=["Staff Accountant"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_description_mention_does_not_trip_a_title_gate(self, taxonomy):
        job = make_job(title="Accountant", description="You report to the Staff Accountant.")
        gate = TitleExcludesGate(phrases=["Staff Accountant"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS


class TestSalaryAndFreshness:
    def test_missing_pay_is_unverifiable_not_a_rejection(self, taxonomy):
        job = make_job(description="Competitive compensation.")
        gate = SalaryFloorGate(minimum=80_000)
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE

    def test_pay_below_floor_is_rejected(self, taxonomy):
        job = make_job(description="Salary: $50,000 - $60,000 per year.")
        assert SalaryFloorGate(minimum=80_000).evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.FAIL

    def test_hourly_pay_is_annualized_before_comparison(self, taxonomy):
        job = make_job(description="Pay: $60.00 per hour.")
        assert SalaryFloorGate(minimum=80_000).evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.PASS

    def test_stale_posting_is_rejected(self, taxonomy):
        job = make_job(posted=date(2026, 1, 1))
        assert FreshnessGate(max_age_days=30).evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.FAIL

    def test_undated_posting_is_unverifiable(self, taxonomy):
        job = make_job(posted=None)
        assert FreshnessGate(max_age_days=30).evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.UNVERIFIABLE


class TestInflectedMatching:
    """Exclusions must survive ordinary English word endings.

    A user who excludes "auditing" and then sees a job whose description says "lead the
    annual audit" has been let down by the filter, not served by it.
    """

    def test_excluding_auditing_also_catches_audit(self, taxonomy):
        job = make_job(description="You will lead the annual audit.")
        gate = PhraseExcludesGate(phrases=["auditing"])
        result = gate.evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert result.evidence.lower() == "audit"

    def test_excluding_audit_also_catches_auditing(self, taxonomy):
        job = make_job(description="Auditing experience essential.")
        gate = PhraseExcludesGate(phrases=["audit"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_excluding_forecasting_catches_forecast(self, taxonomy):
        job = make_job(description="Owns the revenue forecast.")
        gate = PhraseExcludesGate(phrases=["forecasting"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_short_words_are_not_stemmed_into_false_matches(self, taxonomy):
        """Stemming "plus" to "plu" would start matching unrelated words."""
        job = make_job(description="We sell plush toys.")
        gate = PhraseExcludesGate(phrases=["plus"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_inflection_does_not_break_word_boundaries(self, taxonomy):
        job = make_job(description="Please apply through our careers page.")
        gate = PhraseExcludesGate(phrases=["AP"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS


class TestLocationGate:
    """A named place is a requirement, not a hint.

    Regression: before this gate existed, `locations` only fed the ranking, so a search
    for hybrid work in Dallas returned San Francisco roles near the top.
    """

    def test_a_job_in_the_named_metro_passes(self, taxonomy):
        from jobagent.domain.gates import LocationGate

        job = make_job(title="Project Manager", location="Dallas, TX")
        gate = LocationGate(places=["Dallas", "Fort Worth"], remote_exempt=False)
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_a_job_in_another_metro_is_rejected(self, taxonomy):
        from jobagent.domain.gates import LocationGate

        job = make_job(title="Project Manager", location="San Francisco, CA")
        gate = LocationGate(places=["Dallas", "Fort Worth"], remote_exempt=False)
        result = gate.evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert "San Francisco" in result.evidence

    def test_remote_roles_are_exempt_when_remote_is_acceptable(self, taxonomy):
        """A fully remote job is not tied to a metro; failing it would be nonsense."""
        from jobagent.domain.gates import LocationGate

        job = make_job(title="Project Manager", location="Remote - US",
                       description="100% remote position.")
        gate = LocationGate(places=["Dallas"], remote_exempt=True)
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_remote_roles_are_not_exempt_when_the_user_wants_onsite_only(self, taxonomy):
        from jobagent.domain.gates import LocationGate

        job = make_job(title="Project Manager", location="Remote - US",
                       description="100% remote position.")
        gate = LocationGate(places=["Dallas"], remote_exempt=False)
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_a_posting_with_no_location_is_unverifiable(self, taxonomy):
        from jobagent.domain.gates import LocationGate

        job = make_job(title="Project Manager", location="")
        gate = LocationGate(places=["Dallas"], remote_exempt=False)
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE


class TestSeniorityIsNotConfusedWithJobFamily:
    """"Manager" is a job-family noun in much of the labour market, not a rank.

    Regression: Project Manager, Program Manager and Case Manager were all being read as
    management-level and deleted by any search excluding management.
    """

    @pytest.mark.parametrize(
        "title", ["Project Manager", "Program Manager", "Account Manager", "Case Manager"]
    )
    def test_manager_titles_survive_a_management_exclusion(self, title, taxonomy):
        job = make_job(title=title)
        gate = SeniorityExcludeGate(levels=["management"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is not GateOutcome.FAIL

    @pytest.mark.parametrize("title", ["Director of Finance", "VP of Engineering", "Head of Ops"])
    def test_unambiguous_leadership_titles_are_still_excluded(self, title, taxonomy):
        job = make_job(title=title)
        gate = SeniorityExcludeGate(levels=["management"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL


class TestSalaryFloorRespectsItsPeriod:
    """Regression: the floor was compared against annualized pay without being annualized.

    A user asking for "$30 an hour" had that 30 compared against ~$22,880, so an $11/hour
    role passed by a factor of two thousand -- and the explanation asserted the pay rule
    had been satisfied.
    """

    def test_an_hourly_floor_rejects_a_lower_hourly_rate(self, taxonomy):
        from jobagent.domain.gates import SalaryFloorGate

        job = make_job(description="Pay rate: $11.00 per hour.")
        gate = SalaryFloorGate(minimum=30, period="hour")
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_an_hourly_floor_accepts_a_higher_hourly_rate(self, taxonomy):
        from jobagent.domain.gates import SalaryFloorGate

        job = make_job(description="Pay rate: $45.00 per hour.")
        gate = SalaryFloorGate(minimum=30, period="hour")
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_an_hourly_floor_compares_correctly_against_an_annual_salary(self, taxonomy):
        from jobagent.domain.gates import SalaryFloorGate

        job = make_job(description="Salary range: $40,000 - $45,000 per year.")
        assert SalaryFloorGate(minimum=30, period="hour").evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.FAIL
        assert SalaryFloorGate(minimum=15, period="hour").evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.PASS

    def test_the_rule_text_states_both_figures(self, taxonomy):
        from jobagent.domain.gates import SalaryFloorGate

        job = make_job(description="Pay rate: $45.00 per hour.")
        result = SalaryFloorGate(minimum=30, period="hour").evaluate(job, taxonomy, TODAY)
        assert "30 per hour" in result.rule and "62,400/year" in result.rule


class TestEmploymentTypeGate:
    """Regression: the wizard collected employment types and nothing ever read them."""

    def test_a_contract_role_fails_a_full_time_only_search(self, taxonomy):
        from jobagent.domain.gates import EmploymentTypeGate

        job = make_job(title="Accountant")
        job.employment_type = "contract"
        gate = EmploymentTypeGate(allowed=["full_time"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_a_full_time_role_passes(self, taxonomy):
        from jobagent.domain.gates import EmploymentTypeGate

        job = make_job(title="Accountant")
        job.employment_type = "Full-time"
        gate = EmploymentTypeGate(allowed=["full_time"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_an_unstated_type_is_unverifiable(self, taxonomy):
        from jobagent.domain.gates import EmploymentTypeGate

        job = make_job(title="Accountant")
        job.employment_type = None
        gate = EmploymentTypeGate(allowed=["full_time"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE

    def test_the_gate_is_compiled_from_the_spec(self):
        """The bug was not in the gate but in it never being built."""
        from jobagent.domain.spec import SearchSpec, compile_gates

        spec = SearchSpec(name="t", employment_types=["full_time"], salary_min=30,
                          salary_period="hour")
        names = [g.name for g in compile_gates(spec)]
        assert "employment_type" in names and "salary_floor" in names


class TestCompoundSeniorityTitles:
    """Regression: only the highest level named was reported, so the others escaped."""

    @pytest.mark.parametrize(
        "title", ["Senior Staff Accountant", "Senior Director of Finance",
                  "Senior Principal Engineer"],
    )
    def test_excluding_senior_catches_a_compound_title(self, title, taxonomy):
        job = make_job(title=title)
        gate = SeniorityExcludeGate(levels=["senior"])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_the_higher_level_is_still_caught_too(self, taxonomy):
        job = make_job(title="Senior Staff Accountant")
        assert SeniorityExcludeGate(levels=["staff"]).evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.FAIL


class TestSalaryFloorJudgesTheMinimum:
    """Regression: the floor was compared against the TOP of the advertised range.

    A floor is a guarantee. Comparing the upper bound let "$50k - $100k" clear an $80k
    floor even though the employer may pay $50k.
    """

    def test_a_range_straddling_the_floor_is_rejected(self, taxonomy):
        from jobagent.domain.gates import SalaryFloorGate

        job = make_job(description="Salary range: $50,000 - $100,000 per year.")
        result = SalaryFloorGate(minimum=80_000).evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL

    def test_a_range_entirely_above_the_floor_passes(self, taxonomy):
        from jobagent.domain.gates import SalaryFloorGate

        job = make_job(description="Salary range: $90,000 - $120,000 per year.")
        assert SalaryFloorGate(minimum=80_000).evaluate(
            job, taxonomy, TODAY
        ).outcome is GateOutcome.PASS

    def test_evidence_quotes_the_posting_not_a_computed_number(self, taxonomy):
        """A bare "22,880/year" is this tool's arithmetic, not evidence from the posting."""
        from jobagent.domain.gates import SalaryFloorGate

        job = make_job(description="Salary range: $50,000 - $100,000 per year.")
        result = SalaryFloorGate(minimum=80_000).evaluate(job, taxonomy, TODAY)
        assert "50,000-100,000" in result.evidence

    def test_the_ranking_ledger_agrees_with_the_gate(self, taxonomy):
        """The ledger must never award "pay above floor" to a job the gate rejects."""
        from jobagent.domain.matching import evaluate
        from jobagent.domain.spec import SearchSpec

        spec = SearchSpec(name="t", salary_min=30, salary_period="hour",
                          unverifiable_policy="flag")
        job = make_job(description="Pay rate: $11.00 per hour.")
        result = evaluate(job, spec, taxonomy, TODAY)
        assert not any("above floor" in s.name for s in result.signals)


class TestSponsorshipNeedsEvidence:
    """Regression: a plain exclusion made silence a confident match.

    Most postings say nothing about sponsorship. Treating that as a pass showed a user who
    needs sponsorship jobs they cannot take.
    """

    def test_an_explicit_refusal_rejects(self, taxonomy):
        from jobagent.domain.gates import SponsorshipGate

        job = make_job(description="We are unable to sponsor visas for this role.")
        assert SponsorshipGate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_an_explicit_offer_passes(self, taxonomy):
        from jobagent.domain.gates import SponsorshipGate

        job = make_job(description="Visa sponsorship available for the right candidate.")
        assert SponsorshipGate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_silence_is_unverifiable(self, taxonomy):
        from jobagent.domain.gates import SponsorshipGate

        job = make_job(description="Own the monthly close. Great benefits.")
        assert SponsorshipGate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE


class TestClearanceUsesRequirementContext:
    """Regression: the clearance gate rejected "Security clearance not required"."""

    def test_a_posting_that_does_not_require_clearance_is_kept(self, taxonomy):
        from jobagent.domain.matching import evaluate
        from jobagent.domain.models import Decision
        from jobagent.domain.spec import SearchSpec, compile_gates

        spec = SearchSpec(name="t", exclude_security_clearance=True)
        job = make_job(description="Security clearance not required for this position.")
        assert evaluate(job, spec, taxonomy, TODAY, compile_gates(spec)).decision is Decision.MATCH

    def test_a_posting_that_requires_clearance_is_rejected(self, taxonomy):
        from jobagent.domain.matching import evaluate
        from jobagent.domain.models import Decision
        from jobagent.domain.spec import SearchSpec, compile_gates

        spec = SearchSpec(name="t", exclude_security_clearance=True)
        job = make_job(description="An active security clearance is required.")
        result = evaluate(job, spec, taxonomy, TODAY, compile_gates(spec))
        assert result.decision is Decision.REJECTED


class TestSpecStaysInScope:
    def test_a_non_us_country_is_rejected(self):
        """A YAML file could otherwise ask for jobs no configured source can supply."""
        import pytest as _pytest

        from jobagent.domain.spec import SearchSpec

        with _pytest.raises(ValueError, match="US sources only"):
            SearchSpec(name="t", countries=["CA"])

    def test_an_empty_country_list_does_not_disable_the_gate(self):
        from jobagent.domain.spec import SearchSpec

        assert SearchSpec(name="t", countries=[]).countries == ["US"]


class TestSectionHeadings:
    def test_curly_apostrophe_headings_are_recognised(self, taxonomy):
        """Employers write "What You’ll Do" with U+2019; compared against the taxonomy's
        ASCII apostrophe it never matched, so the whole body was read as the work."""
        text = "What You’ll Do\n- Own AP\n- Close books\n\nWho You Are\n- Careful\n"
        work = responsibility_text(text, taxonomy)
        assert "Own AP" in work and "Close books" in work
        assert "Careful" not in work
