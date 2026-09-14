"""The requirement gate: the brief's hardest filtering case.

"Exclude CPA-required roles" must not reject "CPA preferred" or "CPA a plus". The whole
point of these tests is that a keyword match would fail every case in the KEEP column,
and those are the majority of real postings.
"""

from __future__ import annotations

import pytest

from jobagent.domain.gates import RequirementContext, RequirementGate, classify_requirement
from jobagent.domain.models import GateOutcome
from tests.conftest import TODAY, make_job

REQUIRED_PHRASINGS = [
    "CPA required",
    "CPA certification is required",
    "Active CPA license required",
    "Must have a CPA",
    "Must possess a current and valid CPA",
    "CPA is mandatory for this role",
    "Requirements:\n- Bachelor's degree in Accounting\n- CPA\n- 3 years of experience",
    "Minimum Qualifications:\n- CPA",
]

PREFERRED_PHRASINGS = [
    "CPA preferred",
    "CPA a plus",
    "CPA is a plus",
    "CPA nice to have",
    "No CPA required",
    "CPA not required",
    "Working toward CPA",
    "Pursuing CPA certification",
    "CPA or equivalent experience",
    "Preferred Qualifications:\n- CPA\n- Big 4 background",
    "Nice to have:\n- CPA",
]


@pytest.mark.parametrize("text", REQUIRED_PHRASINGS)
def test_demanded_credential_reads_as_required(text, taxonomy):
    assert classify_requirement(text, "CPA", taxonomy).context is RequirementContext.REQUIRED


@pytest.mark.parametrize("text", PREFERRED_PHRASINGS)
def test_optional_credential_reads_as_preferred(text, taxonomy):
    assert classify_requirement(text, "CPA", taxonomy).context is RequirementContext.PREFERRED


def test_unmentioned_credential_is_absent(taxonomy):
    text = "We are hiring an accountant to own the monthly close."
    assert classify_requirement(text, "CPA", taxonomy).context is RequirementContext.ABSENT


def test_required_wins_when_a_posting_says_both(taxonomy):
    text = "Requirements:\n- CPA required\n\nPreferred:\n- CPA a plus"
    assert classify_requirement(text, "CPA", taxonomy).context is RequirementContext.REQUIRED


def test_adjacent_bullets_do_not_contaminate_each_other(taxonomy):
    """A "required" three lines away must not make a "preferred" bullet read as required."""
    text = "- Excel required\n- CPA a plus\n- MBA preferred"
    assert classify_requirement(text, "CPA", taxonomy).context is RequirementContext.PREFERRED
    assert classify_requirement(text, "Excel", taxonomy).context is RequirementContext.REQUIRED


# The same machinery, no code changes, different professions. This is the genericity
# proof for requirement detection: nothing in it knows what a CPA is.
@pytest.mark.parametrize("term", ["CPA", "PMP", "RN license", "security clearance", "Series 7"])
def test_requirement_detection_is_profession_agnostic(term, taxonomy):
    assert classify_requirement(f"{term} required", term, taxonomy).context \
        is RequirementContext.REQUIRED
    assert classify_requirement(f"{term} preferred", term, taxonomy).context \
        is RequirementContext.PREFERRED
    assert classify_requirement(f"{term} a plus", term, taxonomy).context \
        is RequirementContext.PREFERRED


class TestRequirementGate:
    def test_rejects_when_required(self, taxonomy):
        job = make_job(description="Active CPA license required.")
        result = RequirementGate(term="CPA").evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert "CPA" in result.evidence

    def test_keeps_when_merely_preferred(self, taxonomy):
        job = make_job(description="CPA a plus but not necessary.")
        result = RequirementGate(term="CPA").evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.PASS

    def test_keeps_when_not_mentioned(self, taxonomy):
        job = make_job(description="Own the monthly close.")
        result = RequirementGate(term="CPA").evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.PASS

    def test_mentioned_mode_rejects_any_mention(self, taxonomy):
        job = make_job(description="CPA a plus.")
        gate = RequirementGate(term="CPA", when="mentioned")
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_preferred_or_required_mode_rejects_preference(self, taxonomy):
        job = make_job(description="CPA preferred.")
        gate = RequirementGate(term="CPA", when="preferred_or_required")
        assert gate.evaluate(job, taxonomy, TODAY).outcome is GateOutcome.FAIL

    def test_bare_mention_is_unverifiable_not_a_rejection(self, taxonomy):
        job = make_job(description="Our team includes a CPA and two analysts.")
        result = RequirementGate(term="CPA").evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.UNVERIFIABLE

    def test_rejection_quotes_the_triggering_text(self, taxonomy):
        job = make_job(description="You will close the books. CPA required. Great benefits.")
        result = RequirementGate(term="CPA").evaluate(job, taxonomy, TODAY)
        assert result.outcome is GateOutcome.FAIL
        assert "CPA required" in result.detail
