"""Board discovery from the Common Crawl index.

These tests never touch the network. The index is a free public service, and a suite that
hammered it would be both rude and flaky; every response here is a recorded CDXJ shape.

The behaviour that matters most is not "does it parse JSON". It is that a sweep is
*honest* about where it stopped -- a bounded, resumable chore that never reports a
partial pass as a completed one -- and that it can enumerate Workday tenants, which is
the one thing no other discovery route in this codebase can do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from jobagent.infra.store import Store
from jobagent.ports import FetchError, FetchResponse
from jobagent.sources.commoncrawl import (
    COLLECTIONS_URL,
    PLATFORM_PATTERNS,
    CommonCrawlDiscovery,
    _parse_cdx,
    patterns_for,
)
from jobagent.sources.registry import register_boards


def cdxj(*urls: str, status: str = "200") -> str:
    """A CDXJ page: one JSON object per line, as the index actually answers."""
    return "\n".join(
        json.dumps({"urlkey": "com,example)/", "timestamp": "20260801000000",
                    "url": url, "status": status, "mime": "text/html"})
        for url in urls
    )


@dataclass
class CdxFetcher:
    """A fetcher that answers the index, keyed on the page it was asked for.

    ``FakeFetcher`` matches on URL substring, but every page of a CDX sweep is the same
    URL and differs only in ``params``, so pagination would be invisible to it.
    """

    pages: list[str] = field(default_factory=list)
    collections: Any = None
    num_pages: int | None = None
    page_status: dict[int, int] = field(default_factory=dict)
    page_errors: dict[int, Exception] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _requests: int = 0

    @property
    def requests_made(self) -> int:
        return self._requests

    def get(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self._requests += 1
        params = params or {}
        self.calls.append({"url": url, **params})

        if url == COLLECTIONS_URL:
            body = self.collections if self.collections is not None else [
                {"id": "CC-MAIN-2026-33"}, {"id": "CC-MAIN-2026-26"},
            ]
            return FetchResponse(url=url, status=200, text=json.dumps(body))

        if params.get("showNumPages"):
            if self.num_pages is None:
                return FetchResponse(url=url, status=404, text="")
            return FetchResponse(
                url=url, status=200,
                text=json.dumps({"pages": self.num_pages, "pageSize": 5, "blocks": 7}),
            )

        page = int(params.get("page", 0))
        if page in self.page_errors:
            raise self.page_errors[page]
        if page in self.page_status:
            return FetchResponse(url=url, status=self.page_status[page], text="")
        if page >= len(self.pages):
            return FetchResponse(url=url, status=200, text="")
        return FetchResponse(url=url, status=200, text=self.pages[page])

    def post_json(self, url: str, *, payload, headers=None) -> FetchResponse:
        raise AssertionError("the index is read with GET")


GREENHOUSE_PAGE = cdxj(
    "https://job-boards.greenhouse.io/acmeanvil/jobs/4001",
    "https://job-boards.greenhouse.io/acmeanvil/jobs/4002",
    "https://job-boards.greenhouse.io/northwindledger",
    "https://job-boards.greenhouse.io/embed/job_app?token=99",
)


class TestParseCdx:
    def test_reads_one_json_object_per_line(self):
        assert _parse_cdx(cdxj("https://jobs.lever.co/acme/1")) == [
            "https://jobs.lever.co/acme/1"
        ]

    def test_a_malformed_line_does_not_cost_the_page(self):
        # The index is a shared public service; re-reading a whole page because one
        # record is truncated wastes their bandwidth as well as ours.
        text = "\n".join([
            json.dumps({"url": "https://jobs.lever.co/acme/1"}),
            '{"url": "https://jobs.lever.co/brok',
            json.dumps({"url": "https://jobs.lever.co/beta/2"}),
        ])
        assert _parse_cdx(text) == [
            "https://jobs.lever.co/acme/1", "https://jobs.lever.co/beta/2"
        ]

    def test_ignores_blank_and_non_json_lines(self):
        text = f"\n  \nnot json at all\n{json.dumps({'url': 'https://jobs.lever.co/a/1'})}\n"
        assert _parse_cdx(text) == ["https://jobs.lever.co/a/1"]

    def test_a_record_without_a_url_is_skipped(self):
        assert _parse_cdx(json.dumps({"urlkey": "com,x)/", "status": "200"})) == []


class TestPatterns:
    def test_defaults_to_every_supported_platform(self):
        platforms = {platform for platform, _ in patterns_for()}
        assert platforms == set(PLATFORM_PATTERNS)

    def test_narrows_to_the_platforms_asked_for(self):
        assert patterns_for(["lever"]) == [("lever", "jobs.lever.co/*")]

    def test_an_unknown_platform_contributes_nothing(self):
        assert patterns_for(["indeed"]) == []

    def test_workday_is_swept_by_subdomain_wildcard(self):
        # The whole reason this module exists: tenants are hostnames, so only a subdomain
        # wildcard can enumerate them.
        assert PLATFORM_PATTERNS["workday"] == ["*.myworkdayjobs.com/"]


class TestCrawlSelection:
    def test_uses_the_newest_published_crawl(self):
        discovery = CommonCrawlDiscovery(fetcher=CdxFetcher())
        assert discovery.latest_crawl() == "CC-MAIN-2026-33"

    def test_an_explicit_crawl_is_not_overridden(self):
        fetcher = CdxFetcher()
        discovery = CommonCrawlDiscovery(fetcher=fetcher, crawl="CC-MAIN-2025-05")
        assert discovery.latest_crawl() == "CC-MAIN-2025-05"
        assert fetcher.requests_made == 0

    def test_the_crawl_list_is_fetched_once(self):
        fetcher = CdxFetcher()
        discovery = CommonCrawlDiscovery(fetcher=fetcher)
        discovery.latest_crawl()
        discovery.latest_crawl()
        assert sum(1 for call in fetcher.calls if call["url"] == COLLECTIONS_URL) == 1

    def test_no_published_collections_is_an_error_not_a_silent_empty_sweep(self):
        discovery = CommonCrawlDiscovery(fetcher=CdxFetcher(collections=[]))
        with pytest.raises(FetchError):
            discovery.latest_crawl()


class TestSweep:
    def _discovery(self, **kwargs) -> tuple[CommonCrawlDiscovery, CdxFetcher]:
        fetcher = CdxFetcher(**kwargs)
        return CommonCrawlDiscovery(fetcher=fetcher, crawl="CC-MAIN-2026-33"), fetcher

    def test_extracts_boards_and_ignores_platform_routing_urls(self):
        discovery, _ = self._discovery(pages=[GREENHOUSE_PAGE], num_pages=1)
        outcome = discovery.sweep("job-boards.greenhouse.io/*")

        assert outcome.urls_seen == 4
        # Two job URLs collapse to one board; "embed" is platform routing, not a tenant.
        assert {board.token for board in outcome.boards} == {
            "acmeanvil", "northwindledger"
        }
        assert outcome.boards_found == 2

    def test_counts_a_board_as_new_only_when_the_registry_lacks_it(self):
        discovery, _ = self._discovery(pages=[GREENHOUSE_PAGE], num_pages=1)
        outcome = discovery.sweep(
            "job-boards.greenhouse.io/*", known={("greenhouse", "acmeanvil")}
        )

        assert outcome.boards_found == 2
        assert outcome.boards_new == 1
        assert [board.token for board in outcome.boards] == ["northwindledger"]

    def test_reads_no_more_pages_than_asked_for(self):
        pages = [cdxj(f"https://jobs.lever.co/tenant{n}/1") for n in range(10)]
        discovery, fetcher = self._discovery(pages=pages, num_pages=10)
        outcome = discovery.sweep("jobs.lever.co/*", max_pages=3)

        assert outcome.pages_read == 3
        assert [call.get("page") for call in fetcher.calls if "page" in call] == [0, 1, 2]
        assert outcome.complete is False

    def test_resumes_from_the_page_it_was_given(self):
        pages = [cdxj(f"https://jobs.lever.co/tenant{n}/1") for n in range(10)]
        discovery, fetcher = self._discovery(pages=pages, num_pages=10)
        outcome = discovery.sweep("jobs.lever.co/*", start_page=4, max_pages=2)

        assert [call.get("page") for call in fetcher.calls if "page" in call] == [4, 5]
        assert {board.token for board in outcome.boards} == {"tenant4", "tenant5"}

    def test_stops_at_the_end_of_the_index_and_says_so(self):
        pages = [cdxj(f"https://jobs.lever.co/tenant{n}/1") for n in range(3)]
        discovery, _ = self._discovery(pages=pages, num_pages=3)
        outcome = discovery.sweep("jobs.lever.co/*", start_page=1, max_pages=9)

        assert outcome.pages_read == 2
        assert outcome.complete is True

    def test_a_pattern_with_no_captures_is_empty_not_broken(self):
        # The index answers 404 for a pattern it has never seen. That is a legitimate
        # empty result and must not look like an outage.
        discovery, _ = self._discovery(pages=[], num_pages=2, page_status={0: 404})
        outcome = discovery.sweep("jobs.lever.co/*")

        assert outcome.boards == []
        assert "no captures" in outcome.note
        # Not marked complete: the index also 404s when it is unwell, and one wasted
        # request per run is cheaper than silently skipping a platform for a whole crawl.
        assert outcome.complete is False

    def test_a_failed_page_is_recorded_not_swallowed(self):
        pages = [cdxj("https://jobs.lever.co/acme/1")]
        discovery, _ = self._discovery(pages=pages, num_pages=5, page_status={1: 503})
        outcome = discovery.sweep("jobs.lever.co/*", max_pages=5)

        assert outcome.pages_read == 1
        assert [board.token for board in outcome.boards] == ["acme"]
        assert "503" in outcome.note and "page 1" in outcome.note
        # A sweep that stopped early must never claim the pattern is finished, or the
        # cursor is saved as complete and the remaining pages are never read.
        assert outcome.complete is False

    def test_a_transport_failure_keeps_what_was_already_found(self):
        pages = [cdxj("https://jobs.lever.co/acme/1")]
        discovery, _ = self._discovery(
            pages=pages, num_pages=5, page_errors={1: FetchError("ConnectError: refused")}
        )
        outcome = discovery.sweep("jobs.lever.co/*", max_pages=5)

        assert [board.token for board in outcome.boards] == ["acme"]
        assert "refused" in outcome.note
        assert outcome.complete is False

    def test_an_unexpectedly_empty_page_is_reported(self):
        # Without a note this looks identical to "swept and found nothing", and the
        # progress table would show a stalled sweep with no reason given.
        discovery, _ = self._discovery(pages=[""], num_pages=5)
        outcome = discovery.sweep("jobs.lever.co/*", max_pages=5)

        assert outcome.pages_read == 0
        assert outcome.note
        assert outcome.complete is False

    def test_an_unknown_page_count_still_sweeps(self):
        # showNumPages can fail on its own; that costs progress reporting, not the sweep.
        discovery, _ = self._discovery(pages=[GREENHOUSE_PAGE], num_pages=None)
        outcome = discovery.sweep("job-boards.greenhouse.io/*", max_pages=1)

        assert outcome.total_pages is None
        assert outcome.boards_found == 2
        assert outcome.complete is False

    def test_asks_the_index_only_for_successful_captures(self):
        # A 404 capture is a page that no longer exists; registering a board from one
        # produces a token that fetches nothing while looking perfectly valid.
        discovery, fetcher = self._discovery(pages=[GREENHOUSE_PAGE], num_pages=1)
        discovery.sweep("job-boards.greenhouse.io/*")

        page_calls = [call for call in fetcher.calls if "page" in call]
        assert all(call.get("filter") == "=status:200" for call in page_calls)

    def test_enumerates_workday_tenants_with_their_full_tenancy(self):
        # Workday's identity is (tenant, instance, site). A tenant name alone is not a
        # reachable board, and two sites on one tenant are two boards.
        page = cdxj(
            "https://acmeanvil.wd5.myworkdayjobs.com/en-US/External",
            "https://acmeanvil.wd5.myworkdayjobs.com/en-US/Campus",
            "https://northwind.wd103.myworkdayjobs.com/Careers",
        )
        discovery, _ = self._discovery(pages=[page], num_pages=1)
        outcome = discovery.sweep("*.myworkdayjobs.com/")

        assert {board.registry_token() for board in outcome.boards} == {
            "acmeanvil:5:External", "acmeanvil:5:Campus", "northwind:103:Careers",
        }

    def test_the_same_board_seen_twice_in_one_sweep_is_one_board(self):
        page = cdxj(*[f"https://jobs.lever.co/acme/{n}" for n in range(6)])
        discovery, _ = self._discovery(pages=[page], num_pages=1)
        outcome = discovery.sweep("jobs.lever.co/*")

        assert outcome.urls_seen == 6
        assert outcome.boards_found == 1


class TestProgressPersistence:
    """A sweep is only resumable if the cursor survives the process."""

    def test_a_sweep_resumes_where_it_stopped(self, tmp_path):
        store = Store(tmp_path / "jobs.db")
        store.record_discovery(
            "commoncrawl", "CC-MAIN-2026-33", "jobs.lever.co/*", next_page=3,
            total_pages=40, urls_seen=120, boards_found=30, boards_new=30,
            completed=False,
        )
        state = store.discovery_state("commoncrawl", "CC-MAIN-2026-33", "jobs.lever.co/*")
        assert state["next_page"] == 3
        assert state["completed"] == 0

    def test_counters_accumulate_across_resumptions(self, tmp_path):
        store = Store(tmp_path / "jobs.db")
        for page in (0, 5):
            store.record_discovery(
                "commoncrawl", "CC-MAIN-2026-33", "jobs.lever.co/*", next_page=page + 5,
                total_pages=40, urls_seen=100, boards_found=20, boards_new=8,
                completed=False,
            )
        state = store.discovery_state("commoncrawl", "CC-MAIN-2026-33", "jobs.lever.co/*")
        assert state["urls_seen"] == 200
        assert state["boards_new"] == 16
        assert state["next_page"] == 10

    def test_progress_is_tracked_per_crawl_so_a_new_crawl_starts_fresh(self, tmp_path):
        store = Store(tmp_path / "jobs.db")
        store.record_discovery(
            "commoncrawl", "CC-MAIN-2026-26", "jobs.lever.co/*", next_page=40,
            total_pages=40, urls_seen=900, boards_found=200, boards_new=200,
            completed=True,
        )
        assert store.discovery_state(
            "commoncrawl", "CC-MAIN-2026-33", "jobs.lever.co/*"
        ) is None
        assert len(store.discovery_report()) == 1


class TestRegistration:
    def test_discovered_boards_land_in_the_registry(self, tmp_path):
        store = Store(tmp_path / "jobs.db")
        discovery = CommonCrawlDiscovery(
            fetcher=CdxFetcher(pages=[GREENHOUSE_PAGE], num_pages=1),
            crawl="CC-MAIN-2026-33",
        )
        outcome = discovery.sweep("job-boards.greenhouse.io/*")

        assert register_boards(store, outcome.boards) == 2
        assert store.known_board_keys() == {
            ("greenhouse", "acmeanvil"), ("greenhouse", "northwindledger"),
        }

    def test_registering_the_same_board_again_adds_nothing(self, tmp_path):
        store = Store(tmp_path / "jobs.db")
        discovery = CommonCrawlDiscovery(
            fetcher=CdxFetcher(pages=[GREENHOUSE_PAGE], num_pages=1),
            crawl="CC-MAIN-2026-33",
        )
        boards = discovery.sweep("job-boards.greenhouse.io/*").boards

        register_boards(store, boards)
        assert register_boards(store, boards) == 0

    def test_a_discovered_board_carries_no_us_claim(self, tmp_path):
        # Sweeping an index tells us a board exists, not where it hires. Marking it
        # US-signal would push it ahead of boards there is real evidence for.
        store = Store(tmp_path / "jobs.db")
        discovery = CommonCrawlDiscovery(
            fetcher=CdxFetcher(pages=[GREENHOUSE_PAGE], num_pages=1),
            crawl="CC-MAIN-2026-33",
        )
        register_boards(store, discovery.sweep("job-boards.greenhouse.io/*").boards)

        rows = store.registry_targets(platforms=["greenhouse"], limit=10)
        assert rows and not any(row["us_signal"] for row in rows)

    def test_workday_tenancy_survives_the_round_trip(self, tmp_path):
        # If the registry stored only the tenant, the fan-out would query wd5/External
        # for a board that lives at wd103/Careers and report it as empty.
        store = Store(tmp_path / "jobs.db")
        discovery = CommonCrawlDiscovery(
            fetcher=CdxFetcher(
                pages=[cdxj("https://northwind.wd103.myworkdayjobs.com/Careers")],
                num_pages=1,
            ),
            crawl="CC-MAIN-2026-33",
        )
        register_boards(store, discovery.sweep("*.myworkdayjobs.com/").boards)

        assert ("workday", "northwind:103:Careers") in store.known_board_keys()


def page_reads(state: dict[str, Any]) -> int:
    """Index pages actually read, ignoring the crawl-list lookup."""
    return sum(1 for call in state["fetcher"].calls if "page" in call)


class TestDiscoverCommand:
    """The command layer, where a shape mismatch hides behind a green unit suite."""

    @pytest.fixture(autouse=True)
    def wide_terminal(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")

    @pytest.fixture
    def run(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from jobagent.cli import app as cli

        db_path = str(tmp_path / "discover.sqlite3")
        runner = CliRunner()
        state: dict[str, Any] = {}

        def install(**fetcher_kwargs):
            fetcher = CdxFetcher(**fetcher_kwargs)
            state["fetcher"] = fetcher

            class Managed:
                def __init__(self, *args, **kwargs):
                    pass

                def __enter__(self):
                    return fetcher

                def __exit__(self, *exc):
                    return False

            monkeypatch.setattr(cli, "HttpFetcher", Managed)

        def invoke(*args):
            return runner.invoke(cli.app, [*args, "--db", db_path])

        return install, invoke, db_path, state

    def test_a_dry_run_reports_boards_without_registering_them(self, run):
        install, invoke, db_path, _ = run
        install(pages=[GREENHOUSE_PAGE], num_pages=1)

        result = invoke("company", "discover", "--platform", "greenhouse", "--dry-run")

        assert result.exit_code == 0, result.output
        assert "NOT registered" in result.output
        assert Store(db_path).known_board_keys() == set()

    def test_discovered_boards_reach_the_registry(self, run):
        install, invoke, db_path, _ = run
        install(pages=[GREENHOUSE_PAGE], num_pages=1)

        result = invoke("company", "discover", "--platform", "greenhouse")

        assert result.exit_code == 0, result.output
        assert Store(db_path).known_board_keys() == {
            ("greenhouse", "acmeanvil"), ("greenhouse", "northwindledger"),
        }

    def test_a_finished_sweep_is_not_swept_again(self, run):
        install, invoke, _, state = run
        install(pages=[GREENHOUSE_PAGE], num_pages=1)
        invoke("company", "discover", "--platform", "greenhouse")
        before = page_reads(state)

        result = invoke("company", "discover", "--platform", "greenhouse")

        assert "done" in result.output
        # The cursor is honoured: a completed pattern reads no index pages on a re-run.
        # One request still goes out -- the crawl list, which is how the command knows
        # which crawl's cursor to consult.
        assert page_reads(state) == before

    def test_restart_sweeps_a_finished_pattern_again(self, run):
        install, invoke, _, state = run
        install(pages=[GREENHOUSE_PAGE], num_pages=1)
        invoke("company", "discover", "--platform", "greenhouse")
        before = page_reads(state)

        invoke("company", "discover", "--platform", "greenhouse", "--restart")

        assert page_reads(state) > before

    def test_an_unknown_platform_fails_rather_than_sweeping_nothing(self, run):
        install, invoke, _, _ = run
        install()

        result = invoke("company", "discover", "--platform", "indeed")

        assert result.exit_code == 1
        assert "No patterns" in result.output

    def test_status_says_so_when_nothing_has_been_swept(self, run):
        _, invoke, _, _ = run
        result = invoke("company", "discovery-status")

        assert result.exit_code == 0
        assert "No sweeps yet" in result.output

    def test_status_reports_the_saved_cursor(self, run):
        install, invoke, _, _ = run
        install(pages=[GREENHOUSE_PAGE], num_pages=1)
        invoke("company", "discover", "--platform", "greenhouse")

        result = invoke("company", "discovery-status")

        assert "job-boards.greenhouse.io/*" in result.output
        assert "complete" in result.output


class TestMigrationFromAnExistingInstall:
    """Every other test creates a fresh database, so the upgrade path is otherwise
    untested -- and it is the one an existing user actually takes."""

    def _v1_database(self, path) -> None:
        """A database as it stood before discovery existed."""
        import sqlite3
        from datetime import datetime

        from jobagent.infra import schema

        conn = sqlite3.connect(str(path))
        version, sql = schema.MIGRATIONS[0]
        conn.executescript(sql)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now().isoformat()),
        )
        conn.execute(
            "INSERT INTO company_registry (company, ats, token, first_seen) "
            "VALUES ('Legacy Co', 'lever', 'legacyco', '2026-01-01')"
        )
        conn.commit()
        conn.close()

    def test_an_existing_registry_survives_the_upgrade(self, tmp_path):
        path = tmp_path / "existing.sqlite3"
        self._v1_database(path)

        store = Store(path)

        assert store.known_board_keys() == {("lever", "legacyco")}

    def test_discovery_works_on_an_upgraded_database(self, tmp_path):
        path = tmp_path / "existing.sqlite3"
        self._v1_database(path)
        store = Store(path)

        store.record_discovery(
            "commoncrawl", "CC-MAIN-2026-33", "jobs.lever.co/*", next_page=2,
            total_pages=9, urls_seen=5, boards_found=1, boards_new=1, completed=False,
        )

        state = store.discovery_state("commoncrawl", "CC-MAIN-2026-33", "jobs.lever.co/*")
        assert state["next_page"] == 2

    def test_the_upgrade_is_not_reapplied_on_reopen(self, tmp_path):
        path = tmp_path / "existing.sqlite3"
        self._v1_database(path)
        Store(path).close()

        store = Store(path)  # would raise "table already exists" if migrations re-ran

        assert [
            row["version"]
            for row in store.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2]


class TestNewBoardCounting:
    """Greenhouse is swept under two patterns, so a board can be seen twice in one run."""

    @pytest.fixture(autouse=True)
    def wide_terminal(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")

    def _invoke(self, tmp_path, monkeypatch, *args):
        from typer.testing import CliRunner

        from jobagent.cli import app as cli

        # The same board reachable under both Greenhouse hostnames.
        pages = [cdxj("https://job-boards.greenhouse.io/acmeanvil/jobs/1"),
                 cdxj("https://boards.greenhouse.io/acmeanvil")]
        calls = {"n": 0}

        class Managed:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                # One fetcher per pattern, each serving its own page.
                fetcher = CdxFetcher(pages=[pages[min(calls["n"], 1)]], num_pages=1)
                calls["n"] += 1
                return fetcher

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(cli, "HttpFetcher", Managed)
        db_path = str(tmp_path / "count.sqlite3")
        result = CliRunner().invoke(
            cli.app, ["company", "discover", *args, "--db", db_path]
        )
        return result, db_path

    def test_a_dry_run_does_not_count_the_same_board_twice(self, tmp_path, monkeypatch):
        result, _ = self._invoke(tmp_path, monkeypatch, "--platform", "greenhouse", "--dry-run")

        assert result.exit_code == 0, result.output
        assert "1 new boards NOT registered" in result.output

    def test_a_real_run_agrees_with_the_dry_run(self, tmp_path, monkeypatch):
        result, db_path = self._invoke(tmp_path, monkeypatch, "--platform", "greenhouse")

        assert result.exit_code == 0, result.output
        assert Store(db_path).known_board_keys() == {("greenhouse", "acmeanvil")}
        assert "1 new boards registered" in result.output
