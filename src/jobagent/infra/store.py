"""SQLite persistence.

Local, free, and part of the standard library -- which is the whole requirement. The
interesting logic here is not storage but memory: deciding whether a job is genuinely new
to a search or something the user was already shown days ago.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..domain.models import (
    AuthorityTier,
    Coverage,
    Job,
    Location,
    MatchResult,
    SalaryRange,
    UserStatus,
    VerificationState,
    WorkplaceType,
)
from .schema import MIGRATIONS

DEFAULT_DB_DIR = Path.home() / ".jobagent"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "jobagent.sqlite3"


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


class Store:
    """Owns the database connection and every statement run against it."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # ------------------------------------------------------------------ schema

    def migrate(self) -> None:
        """Bring the database up to the current schema version.

        Each migration runs inside a transaction and records its version, so an
        interrupted upgrade is retried rather than half-applied.
        """
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row["version"] for row in self.conn.execute("SELECT version FROM schema_migrations")
        }
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            with self.conn:
                self.conn.executescript(sql)
                self.conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now().isoformat()),
                )

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------------- searches

    def save_spec(self, name: str, spec_yaml: str) -> int:
        now = datetime.now().isoformat()
        with self.conn:
            self.conn.execute(
                """INSERT INTO searches (name, spec_yaml, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET spec_yaml=excluded.spec_yaml,
                                                   updated_at=excluded.updated_at""",
                (name, spec_yaml, now, now),
            )
        # Read the id back rather than trusting lastrowid, which is not meaningful after
        # an upsert that took the UPDATE branch.
        row = self.conn.execute("SELECT id FROM searches WHERE name = ?", (name,)).fetchone()
        return int(row["id"])

    def get_spec(self, name: str) -> tuple[int, str] | None:
        row = self.conn.execute(
            "SELECT id, spec_yaml FROM searches WHERE name = ?", (name,)
        ).fetchone()
        return (int(row["id"]), row["spec_yaml"]) if row else None

    def list_specs(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT s.id, s.name, s.updated_at,
                          (SELECT COUNT(*) FROM runs r WHERE r.search_id = s.id) AS runs,
                          (SELECT MAX(started_at) FROM runs r WHERE r.search_id = s.id) AS last_run
                   FROM searches s ORDER BY s.name"""
            )
        )

    def delete_spec(self, name: str) -> bool:
        with self.conn:
            cursor = self.conn.execute("DELETE FROM searches WHERE name = ?", (name,))
        return cursor.rowcount > 0

    # --------------------------------------------------------------------- runs

    def start_run(self, search_id: int, started: datetime) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (search_id, started_at) VALUES (?, ?)",
                (search_id, started.isoformat()),
            )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self, run_id: int, finished: datetime, coverage: Coverage,
        found: int, matched: int, rejected: int, new_count: int, status: str = "completed",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE runs SET finished_at=?, status=?, found=?, matched=?,
                                   rejected=?, new_count=? WHERE id=?""",
                (finished.isoformat(), status, found, matched, rejected, new_count, run_id),
            )
            for report in coverage.reports:
                self.conn.execute(
                    """INSERT INTO run_sources
                       (run_id, source, status, found, requests, duration_ms, note)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        run_id, report.source, report.status.value, report.found,
                        report.requests, report.duration_ms, report.note,
                    ),
                )

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def latest_run(self, search_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE search_id = ? ORDER BY started_at DESC LIMIT 1",
            (search_id,),
        ).fetchone()

    def run_sources(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM run_sources WHERE run_id = ? ORDER BY source", (run_id,)
            )
        )

    # --------------------------------------------------------------------- jobs

    def upsert_job(self, job: Job, seen: datetime) -> int:
        """Insert or refresh a job, returning its row id.

        On a repeat sighting the canonical row is upgraded only when the new record is
        *more* authoritative. A later aggregator-tier sighting must never overwrite an
        employer-tier URL that an earlier run resolved.
        """
        existing = self.conn.execute(
            "SELECT id, authority FROM jobs WHERE identity = ?", (job.identity,)
        ).fetchone()

        lo = job.salary.minimum if job.salary else None
        hi = job.salary.maximum if job.salary else None
        cur = job.salary.currency if job.salary else None
        per = job.salary.period if job.salary else None

        with self.conn:
            if existing is None:
                cursor = self.conn.execute(
                    """INSERT INTO jobs (identity, title, company, url, description_text,
                            location_raw, city, region, country, workplace, employment_type,
                            department, salary_min, salary_max, salary_currency, salary_period,
                            posted_at, authority, first_seen, last_seen, verification, verified_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job.identity, job.title, job.company, job.url, job.description_text,
                        job.location.raw, job.location.city, job.location.region,
                        job.location.country, job.workplace.value, job.employment_type,
                        job.department, lo, hi, cur, per, _iso(job.posted_at),
                        int(job.authority), _iso(seen), _iso(seen),
                        job.verification.value, _iso(job.verified_at),
                    ),
                )
                job_id = int(cursor.lastrowid or 0)
            else:
                job_id = int(existing["id"])
                if int(job.authority) >= int(existing["authority"]):
                    self.conn.execute(
                        """UPDATE jobs SET title=?, company=?, url=?, description_text=?,
                               location_raw=?, city=?, region=?, country=?, workplace=?,
                               employment_type=?, department=?, salary_min=?, salary_max=?,
                               salary_currency=?, salary_period=?, posted_at=?, authority=?,
                               last_seen=? WHERE id=?""",
                        (
                            job.title, job.company, job.url, job.description_text,
                            job.location.raw, job.location.city, job.location.region,
                            job.location.country, job.workplace.value, job.employment_type,
                            job.department, lo, hi, cur, per, _iso(job.posted_at),
                            int(job.authority), _iso(seen), job_id,
                        ),
                    )
                else:
                    self.conn.execute(
                        "UPDATE jobs SET last_seen=? WHERE id=?", (_iso(seen), job_id)
                    )

            for ref in job.sources:
                self.conn.execute(
                    """INSERT INTO job_sources
                       (job_id, source, url, external_id, authority, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(job_id, url) DO UPDATE SET last_seen=excluded.last_seen""",
                    (
                        job_id, ref.source, ref.url, ref.external_id, int(ref.authority),
                        _iso(ref.first_seen or seen), _iso(ref.last_seen or seen),
                    ),
                )
        return job_id

    def job_seen_by_search(self, job_id: int, search_id: int) -> bool:
        """Has this job ever been reported for this search before?

        This single question is what separates "I found this today" from "I showed you
        this three days ago", and it is asked before the current run's match is written.
        """
        row = self.conn.execute(
            "SELECT 1 FROM job_search_matches WHERE job_id = ? AND search_id = ? LIMIT 1",
            (job_id, search_id),
        ).fetchone()
        return row is not None

    def record_match(
        self, job_id: int, search_id: int, run_id: int, result: MatchResult, is_new: bool
    ) -> None:
        explanation = {
            "gates": [
                {
                    "gate": g.gate, "outcome": g.outcome.value, "rule": g.rule,
                    "evidence": g.evidence, "detail": g.detail,
                }
                for g in result.gates
            ],
            "signals": [
                {"name": s.name, "points": s.points, "evidence": s.evidence}
                for s in result.signals
            ],
        }
        with self.conn:
            self.conn.execute(
                """INSERT INTO job_search_matches
                   (job_id, search_id, run_id, decision, score, uncertain, explanation,
                    is_new, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, search_id, run_id, result.decision.value, result.score,
                    int(result.is_uncertain), json.dumps(explanation), int(is_new),
                    datetime.now().isoformat(),
                ),
            )
            if is_new:
                self.conn.execute(
                    """INSERT INTO user_job_status (job_id, status, changed_at)
                       VALUES (?, ?, ?) ON CONFLICT(job_id) DO NOTHING""",
                    (job_id, UserStatus.NEW.value, datetime.now().isoformat()),
                )

    # ------------------------------------------------------------- user status

    def set_user_status(self, job_id: int, status: str, note: str = "") -> None:
        now = datetime.now().isoformat()
        with self.conn:
            self.conn.execute(
                """INSERT INTO user_job_status (job_id, status, note, changed_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
                        note=excluded.note, changed_at=excluded.changed_at""",
                (job_id, status, note, now),
            )
            self.conn.execute(
                """INSERT INTO user_job_status_history (job_id, status, note, changed_at)
                   VALUES (?,?,?,?)""",
                (job_id, status, note, now),
            )

    def mark_verification(self, job_id: int, state: VerificationState, when: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET verification=?, verified_at=? WHERE id=?",
                (state.value, when.isoformat(), job_id),
            )
            if state is VerificationState.GONE:
                self.conn.execute(
                    """INSERT INTO user_job_status (job_id, status, changed_at)
                       VALUES (?,?,?)
                       ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
                            changed_at=excluded.changed_at
                       WHERE user_job_status.status NOT IN ('applied','saved')""",
                    (job_id, UserStatus.CLOSED.value, when.isoformat()),
                )

    # ------------------------------------------------------------------ results

    def results(
        self, search_id: int, *, only_new: bool = False, decision: str | None = "match",
        statuses: list[str] | None = None, run_id: int | None = None,
        min_score: float | None = None, limit: int = 100, order: str = "score",
    ) -> list[sqlite3.Row]:
        """Fetch the latest verdict per job for a search, with filters applied.

        Only the most recent match row per job is considered: re-running a search should
        revise what the user sees, not stack a new copy beside the old one.
        """
        clauses = ["m.search_id = ?"]
        params: list[Any] = [search_id]

        if decision:
            clauses.append("m.decision = ?")
            params.append(decision)
        if only_new:
            clauses.append("m.is_new = 1")
        if run_id:
            clauses.append("m.run_id = ?")
            params.append(run_id)
        if min_score is not None:
            clauses.append("m.score >= ?")
            params.append(min_score)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"COALESCE(u.status, 'new') IN ({placeholders})")
            params.extend(statuses)

        order_sql = {
            "score": "m.score DESC, j.last_seen DESC",
            "date": "COALESCE(j.posted_at, j.first_seen) DESC",
            "company": "j.company ASC, m.score DESC",
            "title": "j.title ASC",
        }.get(order, "m.score DESC")

        sql = f"""
            SELECT j.*, m.decision, m.score, m.uncertain, m.explanation, m.is_new,
                   m.run_id, COALESCE(u.status, 'new') AS user_status
            FROM job_search_matches m
            JOIN jobs j ON j.id = m.job_id
            LEFT JOIN user_job_status u ON u.job_id = j.id
            WHERE m.id IN (
                SELECT MAX(id) FROM job_search_matches WHERE search_id = ? GROUP BY job_id
            ) AND {' AND '.join(clauses)}
            ORDER BY {order_sql}
            LIMIT ?
        """
        return list(self.conn.execute(sql, [search_id, *params, limit]))

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def job_source_refs(self, job_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM job_sources WHERE job_id = ? ORDER BY authority DESC", (job_id,)
            )
        )

    def latest_explanation(self, job_id: int, search_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT explanation FROM job_search_matches
               WHERE job_id=? AND search_id=? ORDER BY id DESC LIMIT 1""",
            (job_id, search_id),
        ).fetchone()
        return json.loads(row["explanation"]) if row else {}

    # ----------------------------------------------------------------- registry

    def add_company(
        self, company: str, ats: str, token: str, *, domain: str | None = None,
        board_url: str = "", source: str = "manual", us_signal: bool = False,
        notes: str = "",
    ) -> bool:
        """Register a company board. Returns True when the row is new."""
        now = datetime.now().isoformat()
        # SQLite reports rowcount 1 for an ON CONFLICT UPDATE as well as an INSERT, so
        # existence is checked first. Without this, re-seeding claimed every row was new.
        existed = self.conn.execute(
            "SELECT 1 FROM company_registry WHERE ats = ? AND token = ?", (ats, token)
        ).fetchone() is not None
        with self.conn:
            self.conn.execute(
                """INSERT INTO company_registry
                   (company, domain, ats, token, board_url, source, us_signal, first_seen, notes)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ats, token) DO UPDATE SET
                       company=excluded.company,
                       domain=COALESCE(excluded.domain, company_registry.domain),
                       board_url=CASE WHEN excluded.board_url != '' THEN excluded.board_url
                                      ELSE company_registry.board_url END,
                       -- Refreshed when the incoming value is non-empty: notes carry
                       -- routing metadata, and keeping a stale copy meant continuing to
                       -- query an endpoint the employer had moved away from.
                       notes=CASE WHEN excluded.notes != '' THEN excluded.notes
                                  ELSE company_registry.notes END,
                       us_signal=MAX(excluded.us_signal, company_registry.us_signal)""",
                (company, domain, ats, token, board_url, source, int(us_signal), now, notes),
            )
        return not existed

    def registry_targets(
        self, *, platforms: list[str] | None = None, limit: int = 500,
        prefer_us: bool = True, max_failures: int = 5,
    ) -> list[sqlite3.Row]:
        """Pick which boards to fan out to this run.

        Ordering is the rate budget in disguise. US-signalled boards go first because the
        product is US-scoped, then boards that succeeded recently, then everything else.
        Boards that have failed repeatedly sink, and past ``max_failures`` drop out
        entirely so a run is not spent re-probing dead tenants.
        """
        clauses = ["consecutive_failures < ?"]
        params: list[Any] = [max_failures]
        if platforms:
            placeholders = ",".join("?" for _ in platforms)
            clauses.append(f"ats IN ({placeholders})")
            params.extend(platforms)

        order = "consecutive_failures ASC, last_success DESC NULLS LAST, company ASC"
        if prefer_us:
            order = "us_signal DESC, " + order

        return list(
            self.conn.execute(
                f"SELECT * FROM company_registry WHERE {' AND '.join(clauses)} "
                f"ORDER BY {order} LIMIT ?",
                [*params, limit],
            )
        )

    def registry_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT ats, COUNT(*) AS n FROM company_registry "
            "WHERE consecutive_failures < 5 GROUP BY ats"
        )
        return {row["ats"]: int(row["n"]) for row in rows}

    def record_board_outcome(self, registry_id: int, *, ok: bool, kind: str = "") -> None:
        """Update a board's health after a fan-out attempt.

        ``kind`` is kept because "rate limited" and "tenant is gone" must not be
        conflated: treating a 429 as a dead board is how a registry quietly rots away.
        """
        now = datetime.now().isoformat()
        with self.conn:
            if ok:
                self.conn.execute(
                    """UPDATE company_registry SET consecutive_failures=0, last_success=?,
                           last_verified=?, last_failure_kind='' WHERE id=?""",
                    (now, now, registry_id),
                )
            elif kind == "rate_limited":
                # Not the board's fault; record it without counting it against the board.
                self.conn.execute(
                    "UPDATE company_registry SET last_verified=?, last_failure_kind=? WHERE id=?",
                    (now, kind, registry_id),
                )
            else:
                self.conn.execute(
                    """UPDATE company_registry
                       SET consecutive_failures=consecutive_failures+1,
                           last_verified=?, last_failure_kind=? WHERE id=?""",
                    (now, kind, registry_id),
                )


def row_to_job(row: Any) -> Job:
    """Rebuild a Job from a database row."""
    salary = None
    if row["salary_min"] is not None or row["salary_max"] is not None:
        salary = SalaryRange(
            minimum=row["salary_min"], maximum=row["salary_max"],
            currency=row["salary_currency"] or "USD", period=row["salary_period"] or "year",
        )
    return Job(
        identity=row["identity"], title=row["title"], company=row["company"], url=row["url"],
        description_text=row["description_text"],
        location=Location(
            raw=row["location_raw"], city=row["city"], region=row["region"], country=row["country"]
        ),
        workplace=WorkplaceType(row["workplace"]),
        employment_type=row["employment_type"], department=row["department"], salary=salary,
        posted_at=date.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        authority=AuthorityTier(row["authority"]),
        sources=[],
        first_seen=datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None,
        last_seen=datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None,
        verification=VerificationState(row["verification"]),
    )
