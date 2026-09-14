"""Defects found by the first live run, 2026-09-14.

Every location string and every title below was returned by a real employer board on the
first run against the live network. Until that run, every test in this suite used inputs
someone imagined; these are the ones nobody imagined. They are kept together so the shape
of what live data looks like is not lost in the general tests.
"""

from __future__ import annotations

import pytest

from jobagent.domain.gates import RelevanceGate, ResponsibilityExcludesGate
from jobagent.domain.models import GateOutcome
from jobagent.domain.normalize import detect_country
from tests.conftest import TODAY, make_job


class TestCountryDetectionOnLiveLocations:
    """Eight of the eighteen survivors of a US-only search had the wrong country verdict.

    The vocabulary was sixty hand-picked country names and nothing else: no cities, no
    ISO prefixes, no regions. "Croatia" -- a country -- was simply not in the list, so a
    Croatian posting was reported as "does not state a country" and surfaced as a flag
    rather than rejected.
    """

    @pytest.mark.parametrize(
        ("location", "code"),
        [
            ("Croatia", "HR"),
            ("Hong Kong, Hong Kong", "HK"),
            ("Mumbai", "IN"),
            ("FR - Paris", "FR"),
            ("AU - Melbourne, AU - Sydney", "AU"),
            ("Remote-Iberia", "ES"),
            ("Taiwan", "TW"),
            ("Serbia", "RS"),
            ("Luxembourg", "LU"),
            ("Lithuania", "LT"),
            ("London", "GB"),
            ("Toronto, ON", "CA"),
            ("Bengaluru, Karnataka", "IN"),
            ("Remote - Europe", "EU"),
            ("Remote, LATAM", "LATAM"),
            ("UK", "GB"),
        ],
    )
    def test_non_us_is_detected(self, location, code):
        assert detect_country(location)[0] == code

    @pytest.mark.parametrize(
        "location",
        [
            "San Francisco, Remote",
            "PA-Philadelphia (160/90)",
            "Chicago",
            "Remote - Seattle",
            "NYC",
            "Boston or Remote",
        ],
    )
    def test_us_cities_without_a_state_are_still_us(self, location):
        # Both directions matter. "San Francisco, Remote" was reported as unknown, which
        # under a strict policy would have dropped a perfectly good US job.
        assert detect_country(location)[0] == "US"

    @pytest.mark.parametrize(
        ("location", "code"),
        [
            # A US state abbreviation before a dash is a state, even where the same two
            # letters are also a country code. Workday emits this form constantly.
            ("PA-Philadelphia", "US"),
            ("CA - Los Angeles", "US"),
            ("IN - Indianapolis", "US"),
            # But the same ambiguous code followed by a foreign city reads the other way.
            ("CA - Toronto", "CA"),
            ("IN - Bangalore", "IN"),
            ("DE - Berlin", "DE"),
        ],
    )
    def test_ambiguous_two_letter_prefixes_are_resolved_by_the_city(self, location, code):
        assert detect_country(location)[0] == code

    @pytest.mark.parametrize(
        "location",
        ["Paris, TX", "London, KY", "Melbourne, FL", "Dublin, OH", "Berlin, CT"],
    )
    def test_a_us_state_still_wins_over_a_famous_foreign_city_name(self, location):
        assert detect_country(location)[0] == "US"

    @pytest.mark.parametrize("location", ["Georgia", "Atlanta, Georgia", "Jersey City"])
    def test_names_that_collide_with_us_places_are_not_read_as_countries(self, location):
        # Georgia the country and Jersey the island exist, but in a job posting the US
        # reading is overwhelmingly the intended one and a wrong FAIL drops real jobs.
        assert detect_country(location)[0] == "US"

    def test_bare_remote_is_still_unknown(self):
        # Widening the vocabulary must not make the detector start guessing.
        assert detect_country("Remote")[0] is None

    def test_a_person_name_is_not_a_country(self):
        assert detect_country("Reports to Chad Jordan")[0] is None


class TestRelevanceIsNotSatisfiedByPassingMention:
    """Seven of eighteen survivors of an accounting search were not accounting jobs.

    The gate pooled title phrases with responsibility phrases and searched the whole
    posting for any of them. Live descriptions defeated that immediately: a marketing
    role passed on "liaise with our accountant", and a sales role at a company that
    sells accounts-payable software passed on its own product blurb.
    """

    TITLES = ["Accountant", "Accounts Payable"]
    DUTIES = ["accounts payable", "invoice processing"]

    def _gate(self):
        return RelevanceGate(title_phrases=self.TITLES, responsibility_phrases=self.DUTIES)

    def test_a_wanted_title_passes(self, taxonomy):
        job = make_job(title="Staff Accountant", description="Nothing relevant here.")
        assert self._gate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_a_title_phrase_in_the_body_alone_does_not_pass(self, taxonomy):
        # "Marketing Coordinator" at Betson Group, live.
        job = make_job(
            title="Marketing Coordinator",
            description="You will liaise with our accountant on campaign budgets.",
        )
        result = self._gate().evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL

    def test_a_responsibility_under_a_duties_heading_passes(self, taxonomy):
        # The case the pooled design was built for: right work, unlisted title.
        job = make_job(
            title="Finance Operations Specialist",
            description=(
                "About us\nWe make widgets.\n\n"
                "What you'll do\n- Own accounts payable end to end\n- Close the books\n\n"
                "Requirements\n- 2 years experience\n"
            ),
        )
        result = self._gate().evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.PASS
        assert "accounts payable" in result.evidence.lower()

    def test_a_responsibility_phrase_in_the_company_blurb_does_not_pass(self, taxonomy):
        # "Sales Development Representative" at AirWallex and "Product Owner" at GHX,
        # live. Both companies sell accounts-payable software and say so up top.
        job = make_job(
            title="Sales Development Representative",
            description=(
                "About us\nWe build the leading accounts payable automation platform.\n\n"
                "What you'll do\n- Prospect new SME customers\n- Book demos\n\n"
                "Requirements\n- 1 year in sales\n"
            ),
        )
        result = self._gate().evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL

    def test_without_any_headings_the_whole_body_counts(self, taxonomy):
        # Many postings are one unstructured paragraph. Refusing to read them would
        # discard real matches for the sake of a heuristic.
        job = make_job(
            title="Finance Associate",
            description="Join us to run invoice processing and vendor payments daily.",
        )
        assert self._gate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_no_title_hit_and_no_body_fails(self, taxonomy):
        # First written the other way round -- "no description means it did not say" --
        # and a Workday board whose descriptions were over budget put "Software Engineer
        # 4" and "Machine Learning Engineer" into an accounting search as flags. The
        # title is always present and always reaches a verdict; unverifiable is for when
        # no branch can.
        job = make_job(title="Software Engineer 4, Backend Services", description="")
        result = self._gate().evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert "no description was retrieved" in result.detail

    def test_no_phrases_configured_passes(self, taxonomy):
        job = make_job(title="Anything")
        gate = RelevanceGate(title_phrases=[], responsibility_phrases=[])
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_inflection_still_applies_to_titles(self, taxonomy):
        job = make_job(title="Accountants Wanted", description="")
        assert self._gate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS


class TestExcludedResponsibilitiesAreScopedToTheWork:
    """"Manager, Accounts Payable and Expenses", San Ramon, was rejected on the first live
    run for one reason: excluding "auditing" matched "audit" somewhere in the posting.
    An accounts-payable job that mentions audit support is not an auditing job."""

    def _gate(self):
        return ResponsibilityExcludesGate(phrases=["auditing"])

    def test_a_barred_responsibility_in_the_duties_section_fails(self, taxonomy):
        job = make_job(
            title="Finance Associate",
            description=(
                "What you'll do\n- Lead internal audits across the group\n\n"
                "Requirements\n- CPA preferred\n"
            ),
        )
        result = self._gate().evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert "audit" in result.evidence.lower()

    def test_a_mention_outside_the_duties_section_does_not_fail(self, taxonomy):
        job = make_job(
            title="Manager, Accounts Payable and Expenses",
            description=(
                "What you'll do\n- Own the AP cycle\n- Approve vendor payments\n\n"
                "Requirements\n- Attention to the audit trail\n- 5 years in AP\n"
            ),
        )
        assert self._gate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.PASS

    def test_a_barred_responsibility_in_the_title_fails(self, taxonomy):
        # "Audit" is caught by inflection of "auditing"; "Auditor" is an agent noun and
        # deliberately is not (see the stemming rule in domain/text.py).
        job = make_job(title="Internal Audit Lead", description="What you'll do\n- Things\n")
        assert self._gate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_without_headings_the_whole_body_counts(self, taxonomy):
        job = make_job(
            title="Finance Associate",
            description="You will run our quarterly auditing programme.",
        )
        assert self._gate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_no_text_is_unverifiable_not_clear(self, taxonomy):
        # The mirror image of the relevance rule: an exclusion that could not be checked
        # has not been passed. Silence is not a clean bill.
        job = make_job(title="Finance Associate", description="")
        assert self._gate().evaluate(job, taxonomy, TODAY).outcome is GateOutcome.UNVERIFIABLE
