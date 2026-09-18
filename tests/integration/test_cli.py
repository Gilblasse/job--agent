"""The CLI surface, driven through Typer's runner.

These exist because the command layer is where SQL typos and shape mismatches hide: the
domain can be perfectly tested and `jobagent show` still crash on a bad column name. Every
command that reads the database is invoked at least once here.
"""

from __future__ import annotations

import json
import re
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


class TestResultsIdsAndLinks:
    def test_the_first_column_is_the_job_id_the_other_commands_take(self, db):
        """The row number that used to sit there sent users to the wrong job."""
        out = invoke("results", "accounting-remote", "--db", db)
        assert "ID" in out and "jobagent show <id>" in out
        row = next(line for line in out.splitlines() if "Accounts Payable Specialist" in line)
        assert re.match(r"^\W\s*1\s*\W", row), row

    def test_links_are_shown_by_default_and_hidden_on_request(self, db):
        assert "boards.greenhouse.io" in invoke("results", "accounting-remote", "--db", db)
        assert "boards.greenhouse.io" not in invoke(
            "results", "accounting-remote", "--db", db, "--no-links"
        )


class TestDismiss:
    """Turn a job away, say why, and the next run applies it."""

    def _spec(self, db):
        from jobagent.domain.spec import SearchSpec
        from jobagent.infra.store import Store

        return SearchSpec.from_yaml(Store(db).get_spec("accounting-remote")[1])

    def _status(self, db, job_id):
        from jobagent.infra.store import Store

        row = Store(db).conn.execute(
            "SELECT status, note FROM user_job_status WHERE job_id=?", (job_id,)
        ).fetchone()
        return (row["status"], row["note"]) if row else None

    def test_a_reason_and_an_employer_rule_teach_the_search(self, db):
        out = invoke("dismiss", "1", "--db", db, "--reason", "agency work", "--rule", "employer")
        assert "Dismissed:" in out and "agency work" in out
        assert "EMPLOYER: exclude 'Acme Corp'" in out
        assert "rejected" not in out
        assert self._spec(db).excluded_companies == ["Acme Corp"]
        assert self._status(db, 1) == ("dismissed", "agency work")

    def test_a_dismissed_job_leaves_the_results_until_asked_for(self, db):
        invoke("dismiss", "1", "--db", db, "--reason", "no", "--rule", "hide")
        assert "Accounts Payable Specialist" not in invoke(
            "results", "accounting-remote", "--db", db
        )
        shown = invoke("results", "accounting-remote", "--db", db, "--all")
        assert "Accounts Payable Specialist" in shown and "dismissed" in shown

    def test_a_rule_without_a_reason_is_refused(self, db):
        result = runner.invoke(app, ["dismiss", "1", "--db", db, "--rule", "employer"])
        assert result.exit_code == 1
        assert "reason is required" in result.output
        assert self._status(db, 1) != ("dismissed", "")

    def test_a_reason_alone_hides_and_leaves_the_search_alone(self, db):
        before = self._spec(db)
        out = invoke("dismiss", "1", "--db", db, "--reason", "not for me")
        assert "no rule added" in out
        assert self._spec(db) == before
        assert self._status(db, 1) == ("dismissed", "not for me")

    def test_a_reason_only_dismissal_survives_the_next_run(self, db, monkeypatch):
        """Re-matching a dismissed job must not bring it back or reset the dismissal.

        record_match writes 'new' with ON CONFLICT DO NOTHING; this pins that so a
        change to that INSERT cannot silently undo what the user said.
        """
        from jobagent.cli import app as cli

        invoke("dismiss", "1", "--db", db, "--reason", "not for me")

        class Fetcher(FakeFetcher):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(
            cli, "HttpFetcher",
            lambda *a, **k: Fetcher(routes={
                "boards-api.greenhouse.io": fixture("greenhouse_board"),
                "api.lever.co": fixture("lever_board"),
                "api.ashbyhq.com": fixture("ashby_board"),
            }),
        )
        invoke("run", "accounting-remote", "--db", db, "--quiet")

        assert "Accounts Payable Specialist" not in invoke(
            "results", "accounting-remote", "--db", db
        )
        assert self._status(db, 1) == ("dismissed", "not for me")

    def test_a_title_rule_that_would_exclude_a_wanted_title_is_refused(self, db):
        result = runner.invoke(
            app, ["dismiss", "1", "--db", db, "--reason", "r", "--rule", "title=Accountant"]
        )
        assert result.exit_code == 1
        assert "Junior Accountant" in result.output
        assert "Accountant" not in self._spec(db).excluded_titles

    def test_a_bare_seniority_rule_needs_a_level_in_the_title(self, db):
        result = runner.invoke(
            app, ["dismiss", "1", "--db", db, "--reason", "r", "--rule", "seniority"]
        )
        assert result.exit_code == 1
        assert "states no seniority level" in result.output

    def test_the_next_run_rejects_on_the_new_rule(self, db, monkeypatch):
        from jobagent.cli import app as cli
        from jobagent.infra.store import Store

        invoke("dismiss", "1", "--db", db, "--reason", "agency", "--rule", "employer")

        class Fetcher(FakeFetcher):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(
            cli, "HttpFetcher",
            lambda *a, **k: Fetcher(
                routes={"boards-api.greenhouse.io": fixture("greenhouse_board")}
            ),
        )
        invoke("run", "accounting-remote", "--db", db, "--quiet")

        store = Store(db)
        row = store.conn.execute(
            "SELECT decision, explanation FROM job_search_matches WHERE job_id=1 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["decision"] == "rejected"
        assert "company_excludes" in row["explanation"]

    def test_the_interactive_path_asks_and_applies(self, db, monkeypatch):
        from jobagent.cli import dismiss as prompts

        class _Answer:
            def __init__(self, value):
                self.value = value

            def ask(self):
                return self.value

        def text(message, **kw):
            if message.startswith("Why"):
                return _Answer("too much travel")
            return _Answer("cold calling")

        def checkbox(message, choices, **kw):
            picked = [c.value for c in choices if c.value.kind in ("employer", "duty")]
            return _Answer(picked)

        monkeypatch.setattr(prompts.questionary, "text", text)
        monkeypatch.setattr(prompts.questionary, "checkbox", checkbox)
        monkeypatch.setattr(prompts.questionary, "print", lambda *a, **k: None)

        out = invoke("dismiss", "1", "--db", db)
        assert "too much travel" in out
        spec = self._spec(db)
        assert spec.excluded_companies == ["Acme Corp"]
        assert spec.responsibilities_exclude[-1] == "cold calling"

    def test_dismissed_lists_reasons_and_rules(self, db):
        invoke("dismiss", "1", "--db", db, "--reason", "agency work", "--rule", "employer")
        out = invoke("dismissed", "accounting-remote", "--db", db)
        assert "agency work" in out and "EMPLOYER" in out and "Acme Corp" in out
        assert "rejected" not in out

    def test_mark_dismissed_hides_but_mark_rejected_does_not(self, db):
        invoke("mark", "4", "dismissed", "--db", db)
        invoke("mark", "6", "rejected", "--db", db)
        out = invoke("results", "accounting-remote", "--db", db)
        assert "Junior Accountant" not in out
        assert "Cash Management Analyst" in out

    def test_a_gone_posting_keeps_its_dismissal(self, db):
        from datetime import datetime

        from jobagent.domain.models import VerificationState
        from jobagent.infra.store import Store

        invoke("dismiss", "1", "--db", db, "--reason", "no")
        Store(db).mark_verification(1, VerificationState.GONE, datetime(2026, 9, 15))
        assert self._status(db, 1)[0] == "dismissed"


class TestWizardPreview:
    """The wizard's last step, which had no test and crashed on every run.

    A user answered every question, and the preview raised AttributeError on the
    country gate before the search was saved: each rule's description was an f-string
    in a dict literal, so the workplace line's ``.value`` ran against every gate.
    """

    def _lines(self, monkeypatch, spec) -> list[str]:
        from jobagent.cli import wizard

        printed: list[str] = []
        monkeypatch.setattr(
            wizard.questionary, "print", lambda text, **kw: printed.append(str(text))
        )
        wizard._preview(spec)
        return printed

    def test_every_gate_is_described_in_words(self, monkeypatch):
        from jobagent.domain.spec import SearchSpec, compile_gates

        spec = SearchSpec.model_validate({
            "name": "everything",
            "titles": ["Front End Developer"],
            "excluded_titles": ["Angular developer"],
            "responsibilities_include": ["React"],
            "responsibilities_exclude": ["rails"],
            "required_skills": ["react"],
            "excluded_skills": ["angular"],
            "required_keywords": ["equity"],
            "seniority_exclude": ["senior", "staff", "management"],
            "workplace": ["remote", "hybrid"],
            "locations": ["Dallas"],
            "countries": ["US"],
            "employment_types": ["full_time", "contract"],
            "salary_min": 110000,
            "salary_period": "year",
            "max_age_days": 3,
            "excluded_requirements": [{"term": "CPA", "when": "required"}],
            "required_credentials": ["PMP"],
            "needs_visa_sponsorship": True,
            "exclude_security_clearance": True,
            "deal_breakers": ["Vue"],
            "excluded_companies": ["Take2"],
            "unverifiable_policy": "flag",
        })
        lines = self._lines(monkeypatch, spec)
        rules = [line for line in lines if line.startswith("  - ")]
        assert len(rules) == len(compile_gates(spec)) + 1  # plus the policy line

        # No rule falls through to its internal name.
        names = {gate.name for gate in compile_gates(spec)}
        assert not any(rule.strip("- ").strip() in names for rule in rules)
        assert any("only ['remote', 'hybrid'] roles" in r for r in rules)
        assert any("only jobs in ['US']" in r for r in rules)
        assert any("REQUIRE CPA" in r for r in rules)
        assert any("110,000 per year" in r for r in rules)


class TestWizardClarifiesShortEntries:
    """A one-letter language and a two-letter one went into a rule-out list unasked."""

    class _Answer:
        def __init__(self, value):
            self.value = value

        def ask(self):
            return self.value

    def _drive(self, monkeypatch, *, typed: str, keep: list[str], replacements: dict):
        from jobagent.cli import wizard

        asked: list[str] = []

        def text(message, **kw):
            asked.append(message)
            if message.startswith("Replace '"):
                entry = message.split("'")[1]
                return self._Answer(replacements[entry])
            return self._Answer(typed)

        def checkbox(message, choices, **kw):
            asked.append(f"{message} {[c.title for c in choices]}")
            return self._Answer(keep)

        monkeypatch.setattr(wizard.questionary, "text", text)
        monkeypatch.setattr(wizard.questionary, "checkbox", checkbox)
        monkeypatch.setattr(wizard.questionary, "print", lambda *a, **k: None)
        result = wizard._list("Duties you do not want", clarify="in the duties section")
        return result, asked

    def test_short_entries_are_confirmed_rewritten_or_dropped(self, monkeypatch):
        result, asked = self._drive(
            monkeypatch, typed="rails, go, c, sql, wordpress",
            keep=["sql"], replacements={"go": "golang", "c": ""},
        )
        assert result == ["rails", "golang", "sql", "wordpress"]
        # Only the short ones were put to the user, in one checkbox, in order.
        assert any("['go', 'c', 'sql']" in line for line in asked)
        assert not any("Replace 'sql'" in line for line in asked)

    def test_a_replacement_may_be_several_entries(self, monkeypatch):
        result, _ = self._drive(
            monkeypatch, typed="c", keep=[], replacements={"c": "C++, C programming"},
        )
        assert result == ["C++", "C programming"]

    def test_lists_without_short_entries_are_not_interrupted(self, monkeypatch):
        result, asked = self._drive(
            monkeypatch, typed="rails, wordpress", keep=[], replacements={},
        )
        assert result == ["rails", "wordpress"]
        assert len(asked) == 1  # the list prompt itself, nothing more

    def test_lists_that_do_not_scan_prose_are_never_interrupted(self, monkeypatch):
        """Credentials such as a two-letter licence are names, read in context."""
        from jobagent.cli import wizard

        monkeypatch.setattr(
            wizard.questionary, "text", lambda *a, **k: self._Answer("RN, CPA")
        )
        monkeypatch.setattr(
            wizard.questionary, "checkbox",
            lambda *a, **k: pytest.fail("clarification asked without clarify="),
        )
        assert wizard._list("Credentials you hold") == ["RN", "CPA"]


class TestPostRunTable:
    def test_new_matches_after_a_run_are_that_runs_new_matches(self, db, monkeypatch):
        """Live, a re-run headed "New matches (2)" listed three jobs.

        The third had been new in the first run and was simply not re-read in the
        second, because dead boards found in run one sank in the fan-out order and
        different boards took their places. Its latest verdict was still flagged new,
        so an unscoped query listed it under this run's heading.
        """
        from jobagent.cli import app as cli

        board = fixture("greenhouse_board")
        board["jobs"].append({
            **board["jobs"][0], "id": 999, "title": "Accounts Payable Analyst",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/999",
        })

        class Fetcher(FakeFetcher):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        # Only Greenhouse answers this time; Beta Inc's and Gamma LLC's boards are not
        # read, so their run-one matches are neither new nor stale -- just not re-seen.
        monkeypatch.setattr(
            cli, "HttpFetcher",
            lambda *a, **k: Fetcher(routes={"boards-api.greenhouse.io": board}),
        )
        out = invoke("run", "accounting-remote", "--db", db, "--quiet")
        assert "New matches (1)" in out
        assert "Accounts Payable Analyst" in out
        assert "Junior Accountant" not in out
        assert "Cash Management Analyst" not in out


class TestBudgetOverride:
    def test_an_explicit_zero_budget_is_honoured(self, db):
        """Regression: a truthiness check silently substituted the saved budget."""
        out = invoke("run", "accounting-remote", "--db", db, "--budget", "0", "--quiet")
        assert "0 postings" in out

    def test_a_negative_budget_is_rejected(self, db):
        result = runner.invoke(app, ["run", "accounting-remote", "--db", db, "--budget", "-1"])
        assert result.exit_code == 1


class TestProgressDisplay:
    def test_a_non_terminal_console_draws_nothing_and_never_raises(self):
        """Under CliRunner or a pipe the display is silent; the tables are the record."""
        import threading
        from io import StringIO

        from rich.console import Console

        from jobagent.cli.progress import ConsoleProgress
        from jobagent.domain.models import SourceReport, SourceStatus

        buffer = StringIO()
        console = Console(file=buffer, force_terminal=False, width=100)
        with ConsoleProgress(console) as progress:
            progress.discovery_started({"a": 2, "b": 1, "usajobs": 0})

            def worker():
                progress.unit_done("a", 5)
                progress.unit_done("a", 0)
                progress.source_done("a", SourceReport(source="a", status=SourceStatus.OK))

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
            # b stops early: its remainder is completed on source_done.
            progress.source_done("b", SourceReport(source="b", status=SourceStatus.BLOCKED))
            progress.source_done(
                "usajobs", SourceReport(source="usajobs", status=SourceStatus.SKIPPED)
            )
            progress.evaluation_started(3)
            for matched, is_new in ((True, True), (False, False), (True, False)):
                progress.job_evaluated(matched, is_new)
            assert progress._detail() == "matches: 2  ·  new: 1"
            assert progress._done == {"a": 2, "b": 1, "usajobs": 0}

        assert buffer.getvalue() == ""

    def test_an_empty_plan_completes_at_once(self):
        from io import StringIO

        from rich.console import Console

        from jobagent.cli.progress import ConsoleProgress

        with ConsoleProgress(Console(file=StringIO(), force_terminal=False)) as progress:
            progress.discovery_started({})
            progress.evaluation_started(0)
            assert all(task.finished for task in progress._progress.tasks)
