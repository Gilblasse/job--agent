"""End-to-end matching: gates plus the ranking ledger."""

from __future__ import annotations

from jobagent.domain.matching import evaluate, explain
from jobagent.domain.models import Decision
from jobagent.domain.spec import SearchSpec
from tests.conftest import TODAY, make_job


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
    }
    data.update(overrides)
    return SearchSpec(**data)


class TestHardGatesBeatGoodScores:
    def test_a_perfect_match_still_loses_to_one_failed_gate(self, taxonomy):
        """This is the whole point of hard gates: relevance cannot buy an exemption."""
        job = make_job(
            title="Senior Accountant",
            description="Accounts payable, cash management, 100% remote. Great fit.",
        )
        result = evaluate(job, accounting_spec(), taxonomy, TODAY)
        assert result.decision is Decision.REJECTED
        assert any(g.gate == "seniority_excludes" for g in result.failed_gates)

    def test_hybrid_does_not_survive_remote_only(self, taxonomy):
        job = make_job(
            title="Accountant",
            description="Accounts payable duties. This is a hybrid role, 3 days onsite.",
        )
        result = evaluate(job, accounting_spec(), taxonomy, TODAY)
        assert result.decision is Decision.REJECTED
        assert any(g.gate == "workplace" for g in result.failed_gates)

    def test_cpa_required_is_rejected_but_cpa_preferred_is_not(self, taxonomy):
        base = "Accounts payable and cash management. 100% remote, US."
        required = make_job(title="Accountant", description=base + " Active CPA required.")
        preferred = make_job(title="Accountant", description=base + " CPA a plus.")
        spec = accounting_spec()
        assert evaluate(required, spec, taxonomy, TODAY).decision is Decision.REJECTED
        assert evaluate(preferred, spec, taxonomy, TODAY).decision is Decision.MATCH

    def test_every_failing_reason_is_reported_not_just_the_first(self, taxonomy):
        job = make_job(
            title="Senior Staff Accountant",
            description="Auditing and forecasting. Hybrid role in Toronto, Canada.",
            location="Toronto, Canada",
        )
        result = evaluate(job, accounting_spec(), taxonomy, TODAY)
        gates = {g.gate for g in result.failed_gates}
        assert {"title_excludes", "seniority_excludes", "country"} <= gates


class TestExplainability:
    def test_a_rejection_names_the_rule_and_quotes_the_evidence(self, taxonomy):
        job = make_job(title="Accountant",
                       description="Accounts payable. Remote US. Lead the annual audit.")
        result = evaluate(job, accounting_spec(), taxonomy, TODAY)
        assert result.decision is Decision.REJECTED
        reason = result.rejection_reason()
        assert "excluded_content" in reason and "auditing" in reason

    def test_a_match_carries_a_ledger_that_sums_to_the_score(self, taxonomy):
        job = make_job(
            title="Accounts Payable Specialist",
            description="Accounts payable and cash management. 100% remote, US-based.",
        )
        result = evaluate(job, accounting_spec(), taxonomy, TODAY)
        assert result.decision is Decision.MATCH
        assert result.signals
        assert round(sum(s.points for s in result.signals), 2) == result.score

    def test_explain_produces_readable_lines(self, taxonomy):
        job = make_job(title="Accountant",
                       description="Accounts payable. 100% remote, US-based.")
        lines = explain(evaluate(job, accounting_spec(), taxonomy, TODAY))
        assert any("Matched" in line for line in lines)


class TestUncertainty:
    def test_unconfirmed_requirements_are_flagged_not_hidden(self, taxonomy):
        """A remote job with no country stated is kept, marked, and ranked down."""
        job = make_job(
            title="Accountant", location="Remote",
            description="Accounts payable duties. Fully remote position.",
        )
        result = evaluate(job, accounting_spec(), taxonomy, TODAY)
        assert result.decision is Decision.MATCH
        assert result.is_uncertain
        assert any("unconfirmed" in s.name for s in result.signals)

    def test_strict_policy_excludes_what_cannot_be_confirmed(self, taxonomy):
        job = make_job(
            title="Accountant", location="Remote",
            description="Accounts payable duties. Fully remote position.",
        )
        strict = accounting_spec(unverifiable_policy="strict")
        assert evaluate(job, strict, taxonomy, TODAY).decision is Decision.REJECTED


class TestGenericity:
    """The same code, three professions, no changes. This is the core claim.

    If any of these needed a branch in ``src/``, the product would not be generic.
    """

    def test_software_search(self, taxonomy):
        spec = SearchSpec(
            name="react", titles=["React Developer", "Frontend Engineer"],
            seniority_exclude=["senior", "staff"], workplace=["remote"], countries=["US"],
            required_skills=["React"],
        )
        good = make_job(title="Frontend Engineer",
                        description="Build React applications. 100% remote, US-based.")
        senior = make_job(title="Senior Frontend Engineer",
                          description="Build React applications. 100% remote, US.")
        assert evaluate(good, spec, taxonomy, TODAY).decision is Decision.MATCH
        assert evaluate(senior, spec, taxonomy, TODAY).decision is Decision.REJECTED

    def test_healthcare_search(self, taxonomy):
        spec = SearchSpec(
            name="billing", titles=["Medical Biller", "Medical Billing Specialist"],
            excluded_requirements=[{"term": "RN license"}],
            workplace=["remote"], countries=["US"],
        )
        good = make_job(title="Medical Billing Specialist",
                        description="Remote US role. RN license a plus.")
        barred = make_job(title="Medical Billing Specialist",
                          description="Remote US role. RN license required.")
        assert evaluate(good, spec, taxonomy, TODAY).decision is Decision.MATCH
        assert evaluate(barred, spec, taxonomy, TODAY).decision is Decision.REJECTED

    def test_onsite_metro_search(self, taxonomy):
        """The Dallas hybrid case: hybrid is wanted here, so remote-only is not assumed."""
        from jobagent.domain.models import WorkplaceType  # noqa: PLC0415

        spec = SearchSpec(
            name="pm-dfw", titles=["Project Manager"], workplace=["hybrid", "onsite"],
            countries=["US"], locations=["Dallas"], excluded_requirements=[{"term": "PMP"}],
        )
        job = make_job(
            title="Project Manager", location="Dallas, TX",
            description="Hybrid role, 3 days in office. PMP preferred.",
            workplace=WorkplaceType.HYBRID,
        )
        result = evaluate(job, spec, taxonomy, TODAY)
        assert result.decision is Decision.MATCH
        assert any("location" in s.name for s in result.signals)

    def test_one_corpus_two_specs_give_different_answers(self, taxonomy):
        """Same jobs in, different results out, driven entirely by the spec."""
        corpus = [
            make_job(title="Accountant", description="Accounts payable. Remote, US-based."),
            make_job(title="Frontend Engineer", description="React work. Remote, US-based."),
        ]
        accounting = accounting_spec()
        software = SearchSpec(
            name="react", titles=["Frontend Engineer"], workplace=["remote"], countries=["US"]
        )
        acc = [evaluate(j, accounting, taxonomy, TODAY).decision for j in corpus]
        sw = [evaluate(j, software, taxonomy, TODAY).decision for j in corpus]
        assert acc == [Decision.MATCH, Decision.REJECTED]
        assert sw == [Decision.REJECTED, Decision.MATCH]
