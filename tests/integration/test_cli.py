"""The CLI surface, driven through Typer's runner.

These exist because the command layer is where SQL typos and shape mismatches hide: the
domain can be perfectly tested and `jobagent show` still crash on a bad column name. Every
command that reads the database is invoked at least once here.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobagent.cli.app import app
from jobagent.domain.spec import SearchSpec
from jobagent.engine.orchestrator import run_search
from jobagent.infra.store import Store
from tests.fakes import FakeFetcher

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
runner = CliRunner()


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    """Render at a fixed wide width.

    Rich wraps to the terminal, so at the runner's default 80 columns a long job title
    is split across lines and a substring assertion fails for reasons that have nothing
    to do with the command under test.
    """
    monkeypatch.setenv("COLUMNS", "200")


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def db(tmp_path) -> str:
    """A database with one search already run against fixtures."""
    path = str(tmp_path / "test.sqlite3")
    store = Store(path)
    spec = SearchSpec.from_yaml((EXAMPLES / "benchmark-accounting.yml").read_text())
    search_id = store.save_spec(spec.name, spec.to_yaml())
    store.add_company("Acme Corp", "greenhouse", "acme", domain="acme.com", us_signal=True)
    store.add_company("Beta Inc", "lever", "beta", domain="beta.com", us_signal=True)
    store.add_company("Gamma LLC", "ashby", "gamma", domain="gamma.com", us_signal=True)
    fetcher = FakeFetcher(routes={
        "boards-api.greenhouse.io": fixture("greenhouse_board"),
        "api.lever.co": fixture("lever_board"),
        "api.ashbyhq.com": fixture("ashby_board"),
    })
    run_search(
        spec, store, fetcher, search_id=search_id, today=date(2026, 9, 14),
        now=datetime(2026, 9, 14, 12, 0, 0),
        only_sources=["greenhouse", "lever", "ashby"],
    )
    store.close()
    return path


def invoke(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f"{args} failed:\n{result.output}\n{result.exception}"
    return result.output


class TestSearchCommands:
    def test_create_from_a_yaml_spec(self, tmp_path):
        path = str(tmp_path / "new.sqlite3")
        out = invoke(
            "search", "create", "--from", str(EXAMPLES / "benchmark-accounting.yml"),
            "--db", path,
        )
        assert "Saved search" in out
        assert "accounting-remote" in invoke("search", "list", "--db", path)

    def test_show_prints_the_spec(self, db):
        assert "titles" in invoke("search", "show", "accounting-remote", "--db", db)

    def test_export_round_trips_through_yaml(self, db, tmp_path):
        out_file = tmp_path / "spec.yml"
        invoke("search", "export", "accounting-remote", "--db", db, "--out", str(out_file))
        assert SearchSpec.from_yaml(out_file.read_text()).name == "accounting-remote"

    def test_an_unknown_search_exits_non_zero_with_a_hint(self, db):
        result = runner.invoke(app, ["search", "show", "nope", "--db", db])
        assert result.exit_code == 1
        assert "No search named" in result.output

    def test_delete_removes_the_search(self, db):
        invoke("search", "delete", "accounting-remote", "--db", db, "--yes")
        assert "No searches yet" in invoke("search", "list", "--db", db)


class TestResults:
    def test_matches_are_listed(self, db):
        out = invoke("results", "accounting-remote", "--db", db)
        assert "Accounts Payable Specialist" in out
        assert "Senior Accountant" not in out

    def test_rejected_view_shows_the_excluded_jobs(self, db):
        out = invoke("results", "accounting-remote", "--db", db, "--rejected")
        assert "Senior Accountant" in out

    def test_explain_states_the_rule_and_the_evidence(self, db):
        out = invoke("results", "accounting-remote", "--db", db, "--rejected", "--explain")
        assert "Rejected because" in out
        assert "seniority_excludes" in out

    def test_new_filter_works(self, db):
        assert "Accounts Payable Specialist" in invoke(
            "results", "accounting-remote", "--db", db, "--new"
        )

    def test_sorting_options_are_accepted(self, db):
        for order in ("score", "date", "company", "title"):
            invoke("results", "accounting-remote", "--db", db, "--sort", order)

    def test_min_score_filters(self, db):
        out = invoke("results", "accounting-remote", "--db", db, "--min-score", "1000")
        assert "Nothing to show" in out


class TestShowAndMark:
    def test_show_renders_a_job_without_crashing(self, db):
        """Regression: this command once died on a mis-named SQL column."""
        out = invoke("show", "1", "--db", db)
        assert "Apply at" in out
        assert "Employer" in out

    def test_show_rejects_an_unknown_id(self, db):
        result = runner.invoke(app, ["show", "9999", "--db", db])
        assert result.exit_code == 1

    def test_mark_then_filter_by_status(self, db):
        invoke("mark", "1", "saved", "--db", db)
        assert "Nothing to show" not in invoke(
            "results", "accounting-remote", "--db", db, "--saved"
        )

    def test_mark_rejects_an_invalid_status(self, db):
        result = runner.invoke(app, ["mark", "1", "banana", "--db", db])
        assert result.exit_code == 1
        assert "Status must be one of" in result.output


class TestCoverageAndExport:
    def test_coverage_lists_every_source(self, db):
        out = invoke("coverage", "accounting-remote", "--db", db)
        assert "greenhouse" in out and "lever" in out

    @pytest.mark.parametrize("fmt", ["csv", "json", "md"])
    def test_export_formats(self, db, tmp_path, fmt):
        out_file = tmp_path / f"results.{fmt}"
        invoke("export", "accounting-remote", "--db", db, "--format", fmt,
               "--out", str(out_file))
        text = out_file.read_text()
        assert "Accounts Payable Specialist" in text

    def test_export_includes_the_reasoning(self, db, tmp_path):
        """An export has to stand on its own once it leaves this tool."""
        out_file = tmp_path / "results.csv"
        invoke("export", "accounting-remote", "--db", db, "--format", "csv",
               "--out", str(out_file))
        assert "why" in out_file.read_text().splitlines()[0]

    def test_export_rejects_an_unknown_format(self, db):
        result = runner.invoke(app, ["export", "accounting-remote", "--db", db,
                                     "--format", "pdf"])
        assert result.exit_code == 1


class TestSourcesAndCompany:
    def test_sources_list_names_what_is_used_and_what_is_not(self):
        out = invoke("sources", "list")
        assert "greenhouse" in out and "usajobs" in out
        assert "Not used" in out

    def test_company_seed_loads_the_bundled_registry(self, tmp_path):
        path = str(tmp_path / "seed.sqlite3")
        out = invoke("company", "seed", "--db", path)
        assert "added" in out
        listing = invoke("company", "list", "--db", path)
        assert "greenhouse" in listing

    def test_company_add_registers_a_direct_board_url(self, tmp_path):
        path = str(tmp_path / "add.sqlite3")
        out = invoke("company", "add", "https://boards.greenhouse.io/acme",
                     "--db", path, "--name", "Acme")
        assert "Registered" in out

    def test_company_import_reads_a_csv(self, tmp_path):
        csv_file = tmp_path / "companies.csv"
        csv_file.write_text(
            "company,url\n"
            "Acme,https://boards.greenhouse.io/acme\n"
            "Beta,https://jobs.lever.co/beta\n"
            "Bad,https://example.com/careers\n"
        )
        path = str(tmp_path / "import.sqlite3")
        out = invoke("company", "import", str(csv_file), "--db", path)
        assert "Imported 2 boards" in out
        assert "no recognizable ATS board" in out


class TestDoctorTreatsUsajobsAsOptional:
    """Regression: the gate failed without USAJOBS credentials, while the README calls
    them optional -- so the check the README tells users to run first could never pass
    for an ATS-only user."""

    def _result(self, tmp_path, monkeypatch, configured: bool):
        from jobagent.engine.doctor import run_doctor
        from jobagent.infra.store import Store
        from jobagent.sources.registry import seed_registry
        from tests.fakes import FakeFetcher

        if configured:
            monkeypatch.setenv("JOBAGENT_USAJOBS_KEY", "k")
            monkeypatch.setenv("JOBAGENT_USAJOBS_EMAIL", "u@example.com")
        else:
            monkeypatch.delenv("JOBAGENT_USAJOBS_KEY", raising=False)
            monkeypatch.delenv("JOBAGENT_USAJOBS_EMAIL", raising=False)

        store = Store(str(tmp_path / "doc.sqlite3"))
        seed_registry(store)
        return run_doctor(store, FakeFetcher(default_status=500))

    def test_unconfigured_credentials_are_a_note_not_a_failure(self, tmp_path, monkeypatch):
        result = self._result(tmp_path, monkeypatch, configured=False)
        assert not any("USAJOBS" in f for f in result.failures)
        assert any("not configured" in n for n in result.notes)

    def test_configured_but_broken_credentials_do_fail(self, tmp_path, monkeypatch):
        result = self._result(tmp_path, monkeypatch, configured=True)
        assert any("USAJOBS is configured but not returning" in f for f in result.failures)

    def test_the_note_is_rendered_for_the_user(self, tmp_path, monkeypatch):
        from jobagent.engine.doctor import describe

        result = self._result(tmp_path, monkeypatch, configured=False)
        assert any("NOTE:" in line for line in describe(result))


class TestBudgetOverride:
    def test_an_explicit_zero_budget_is_honoured(self, db):
        """Regression: a truthiness check silently substituted the saved budget."""
        out = invoke("run", "accounting-remote", "--db", db, "--budget", "0", "--quiet")
        assert "0 postings" in out

    def test_a_negative_budget_is_rejected(self, db):
        result = runner.invoke(app, ["run", "accounting-remote", "--db", db, "--budget", "-1"])
        assert result.exit_code == 1
