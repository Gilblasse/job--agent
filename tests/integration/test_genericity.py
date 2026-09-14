"""The genericity proof.

The brief's central requirement is that the same binary serves any profession, and that
its accounting benchmark is a test case rather than product logic. That claim is only
worth anything if it is tested the hard way: ONE corpus of postings, THREE unrelated
searches, and the assertion that each returns the right jobs for the right stated reasons
with no code change between them.

If any of this needed a branch in src/, the product would not be generic.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from jobagent.domain.models import AuthorityTier, Decision, RawPosting, WorkplaceType
from jobagent.domain.spec import SearchSpec
from jobagent.engine.orchestrator import run_search
from jobagent.infra.store import Store
from jobagent.ports import DiscoveryRequest, DiscoveryResult
from jobagent.sources.base import SourceAdapter

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
TODAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 12, 0, 0)


def posting(title, company, description, location, workplace=WorkplaceType.UNKNOWN, ext="x"):
    return RawPosting(
        source="corpus", external_id=ext, title=title, company=company,
        url=f"https://boards.greenhouse.io/{company.lower()}/jobs/{ext}",
        description_text=description, location_raw=location, workplace_hint=workplace,
        authority=AuthorityTier.OFFICIAL_ATS, posted_at=date(2026, 9, 10),
    )


# One mixed corpus. Every spec below sees exactly these postings.
CORPUS = [
    # --- accounting ---------------------------------------------------------------
    posting("Accounts Payable Specialist", "Acme",
            "Own accounts payable and invoice processing. CPA a plus.",
            "Remote - US", WorkplaceType.REMOTE, "a1"),
    posting("Senior Accountant", "Acme",
            "Lead the monthly close. Active CPA license required.",
            "Remote - US", WorkplaceType.REMOTE, "a2"),
    posting("Staff Accountant", "Beta",
            "General ledger and reconciliations.", "Austin, TX", WorkplaceType.REMOTE, "a3"),
    posting("Junior Accountant", "Beta",
            "Support accounts payable and bank reconciliation. CPA preferred.",
            "Remote - US", WorkplaceType.REMOTE, "a4"),
    posting("Internal Auditor", "Gamma",
            "Lead auditing engagements and forecasting.",
            "Remote - US", WorkplaceType.REMOTE, "a5"),
    posting("Accountant", "Delta",
            "Accounts payable duties.", "Toronto, Canada", WorkplaceType.REMOTE, "a6"),

    # --- software ------------------------------------------------------------------
    posting("Frontend Engineer", "Cassini",
            "Build React components and user interface work.",
            "Remote - US", WorkplaceType.REMOTE, "s1"),
    posting("Senior Frontend Engineer", "Cassini",
            "Lead React architecture and frontend strategy.",
            "Remote - US", WorkplaceType.REMOTE, "s2"),
    posting("Web Developer", "Dyad",
            "Maintain Angular and Drupal sites.", "Remote - US", WorkplaceType.REMOTE, "s3"),

    # --- project management, DFW ----------------------------------------------------
    posting("Project Manager", "Metroplex Health",
            "Own the project plan, schedule and stakeholder comms. Hybrid, 3 days in office. "
            "PMP preferred.",
            "Dallas, TX", WorkplaceType.HYBRID, "p1"),
    posting("Project Manager", "Lone Star Logistics",
            "Manage schedules and budget tracking. Active PMP certification required.",
            "Fort Worth, TX", WorkplaceType.ONSITE, "p2"),
    posting("Program Manager", "Bay Systems",
            "Stakeholder management and project plan ownership.",
            "San Francisco, CA", WorkplaceType.HYBRID, "p3"),
]


class CorpusAdapter(SourceAdapter):
    """Serves the fixed corpus, so the only variable between runs is the spec."""

    name: str = "greenhouse"

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        from jobagent.domain.models import SourceReport, SourceStatus

        return DiscoveryResult(
            postings=list(CORPUS),
            report=SourceReport(source=self.name, status=SourceStatus.OK, found=len(CORPUS)),
        )


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(str(tmp_path / "generic.sqlite3"))
    store.add_company("Corpus", "greenhouse", "corpus", us_signal=True)
    return store


def execute(spec: SearchSpec, store: Store, monkeypatch):
    """Run a spec over the corpus and return (matched titles, rejections by title)."""
    monkeypatch.setattr(
        "jobagent.engine.orchestrator.all_adapters", lambda: {"greenhouse": CorpusAdapter()}
    )
    search_id = store.save_spec(spec.name, spec.to_yaml())
    outcome = run_search(
        spec, store, None, search_id=search_id, today=TODAY, now=NOW,
        only_sources=["greenhouse"],
    )
    matched = {job.title for job, _, _ in outcome.matches}
    rejected = {
        row["title"]: row for row in store.results(search_id, decision="rejected", limit=50)
    }
    return matched, rejected, outcome


def verdict_rows(store: Store, search_id: int) -> tuple[list, list]:
    """Every recorded verdict, as rows.

    Counted by row rather than by title: two distinct postings can legitimately share a
    title, and keying by title would merge them and hide a job that went missing.
    """
    return (
        store.results(search_id, decision="match", limit=100),
        store.results(search_id, decision="rejected", limit=100),
    )


def load(name: str) -> SearchSpec:
    return SearchSpec.from_yaml((EXAMPLES / f"{name}.yml").read_text())


class TestShippedSpecsAreExpressibleAsData:
    """Each shipped example must load and compile without any code knowing its field."""

    @pytest.mark.parametrize(
        "name", ["benchmark-accounting", "react-remote", "pm-dfw-hybrid"]
    )
    def test_example_specs_load_and_compile(self, name):
        from jobagent.domain.spec import compile_gates

        spec = load(name)
        assert compile_gates(spec)

    def test_no_profession_appears_anywhere_in_the_source(self):
        """The strongest form of the claim: grep src/ and find nothing domain-specific.

        Words like "CPA", "accountant" or "React" in src/ would mean the benchmark had
        leaked into the product. They are allowed in comments explaining the machinery,
        so only executable lines are checked.
        """
        import ast

        banned = [
            "accountant", "accounts payable", "cpa", "react", "pmp",
            "bookkeep", "nurse", "paralegal", "medical billing",
        ]
        offenders: list[str] = []
        for path in (Path(__file__).resolve().parents[2] / "src").rglob("*.py"):
            tree = ast.parse(path.read_text())
            # Docstrings are stripped before checking. Prose explaining *why* the
            # requirement gate exists may name a credential as an example; executable
            # code may not, because that is where a benchmark would actually leak in.
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                    docstring = ast.get_docstring(node, clean=False)
                    if docstring and node.body and isinstance(node.body[0], ast.Expr):
                        node.body = node.body[1:] or [ast.Pass()]
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                lowered = node.value.lower()
                for word in banned:
                    if word in lowered:
                        offenders.append(
                            f"{path.name}:{node.lineno}: {node.value[:60]!r}"
                        )
        assert not offenders, "profession-specific terms in executable code:\n" + "\n".join(
            offenders
        )


class TestOneCorpusThreeSearches:
    def test_accounting_benchmark(self, store, monkeypatch):
        matched, rejected, _ = execute(load("benchmark-accounting"), store, monkeypatch)

        assert "Accounts Payable Specialist" in matched
        assert "Junior Accountant" in matched

        # Each exclusion in the brief, asserted by its stated reason rather than by absence.
        assert "Senior Accountant" in rejected
        assert "Staff Accountant" in rejected
        assert "Internal Auditor" in rejected
        assert "Accountant" in rejected  # Toronto

        # No software or PM role leaks into an accounting search.
        assert not {"Frontend Engineer", "Project Manager", "Web Developer"} & matched

    def test_accounting_rejections_state_the_right_reason(self, store, monkeypatch):
        import json

        _, rejected, _ = execute(load("benchmark-accounting"), store, monkeypatch)

        def reasons(title: str) -> set[str]:
            gates = json.loads(rejected[title]["explanation"])["gates"]
            return {g["gate"] for g in gates if g["outcome"] == "fail"}

        assert "seniority_excludes" in reasons("Senior Accountant")
        assert "requirement:CPA" in reasons("Senior Accountant")
        assert "title_excludes" in reasons("Staff Accountant")
        assert "excluded_responsibilities" in reasons("Internal Auditor")
        assert "country" in reasons("Accountant")

    def test_cpa_preferred_survives_while_cpa_required_does_not(self, store, monkeypatch):
        """The brief's hardest case, end to end rather than at unit level."""
        matched, rejected, _ = execute(load("benchmark-accounting"), store, monkeypatch)
        assert "Junior Accountant" in matched      # "CPA preferred"
        assert "Senior Accountant" in rejected     # "Active CPA license required"

    def test_react_search_over_the_same_corpus(self, store, monkeypatch):
        matched, rejected, _ = execute(load("react-remote"), store, monkeypatch)

        assert "Frontend Engineer" in matched
        assert "Senior Frontend Engineer" in rejected   # seniority
        assert "Web Developer" in rejected              # excluded skills
        assert not {"Accounts Payable Specialist", "Project Manager"} & matched

    def test_dfw_project_manager_search_over_the_same_corpus(self, store, monkeypatch):
        matched, rejected, _ = execute(load("pm-dfw-hybrid"), store, monkeypatch)

        assert "Project Manager" in matched             # Dallas, hybrid, PMP preferred
        assert "Program Manager" in rejected            # San Francisco
        assert not {"Accounts Payable Specialist", "Frontend Engineer"} & matched

    def test_pmp_required_is_excluded_but_pmp_preferred_is_kept(self, store, monkeypatch):
        """The CPA machinery, unchanged, applied to a different profession's credential."""
        import json

        _, rejected, outcome = execute(load("pm-dfw-hybrid"), store, monkeypatch)
        kept = {(job.title, job.company) for job, _, _ in outcome.matches}
        assert ("Project Manager", "Metroplex Health") in kept   # PMP preferred

        lone_star = [
            row for title, row in rejected.items() if title == "Project Manager"
        ]
        assert lone_star, "the PMP-required role should have been rejected"
        gates = json.loads(lone_star[0]["explanation"])["gates"]
        assert any(
            g["gate"] == "requirement:PMP" and g["outcome"] == "fail" for g in gates
        )

    def test_the_three_searches_disagree_about_the_same_corpus(self, tmp_path, monkeypatch):
        """The clearest statement of genericity: same input, three different outputs."""
        results = {}
        for name in ("benchmark-accounting", "react-remote", "pm-dfw-hybrid"):
            store = Store(str(tmp_path / f"{name}.sqlite3"))
            store.add_company("Corpus", "greenhouse", "corpus", us_signal=True)
            matched, _, _ = execute(load(name), store, monkeypatch)
            results[name] = matched

        assert results["benchmark-accounting"] != results["react-remote"]
        assert results["react-remote"] != results["pm-dfw-hybrid"]
        assert not results["benchmark-accounting"] & results["react-remote"]
        assert all(results.values()), "every search should match something"


class TestHardGatesAreAbsolute:
    def test_a_strong_match_cannot_buy_its_way_past_an_exclusion(self, store, monkeypatch):
        """A job that is otherwise a perfect fit still loses to one failed gate."""
        spec = load("benchmark-accounting")
        _, rejected, _ = execute(spec, store, monkeypatch)
        senior = rejected["Senior Accountant"]
        assert senior["decision"] == "rejected"
        assert senior["score"] == 0

    def test_relaxing_one_rule_admits_the_job(self, tmp_path, monkeypatch):
        """Proof the exclusion, not something incidental, was doing the work."""
        spec = load("benchmark-accounting")
        relaxed = spec.model_copy(update={"seniority_exclude": [], "excluded_requirements": []})
        store = Store(str(tmp_path / "relaxed.sqlite3"))
        store.add_company("Corpus", "greenhouse", "corpus", us_signal=True)
        matched, _, _ = execute(relaxed, store, monkeypatch)
        assert "Senior Accountant" in matched


class TestDecisionCoverage:
    def test_every_posting_gets_a_recorded_verdict(self, store, monkeypatch):
        """Nothing may vanish silently between discovery and results."""
        _, _, outcome = execute(load("benchmark-accounting"), store, monkeypatch)
        assert outcome.matched + outcome.rejected == len(CORPUS)

        search_id = store.get_spec("accounting-remote")[0]
        matched_rows, rejected_rows = verdict_rows(store, search_id)
        assert len(matched_rows) + len(rejected_rows) == len(CORPUS)

    def test_results_are_ordered_by_score(self, store, monkeypatch):
        _, _, outcome = execute(load("benchmark-accounting"), store, monkeypatch)
        scores = [result.score for _, result, _ in outcome.matches]
        assert scores == sorted(scores, reverse=True)
        assert all(r.decision is Decision.MATCH for _, r, _ in outcome.matches)
