"""Persistence: SQLite locally, the same SQL over HTTP in the cloud.

Local, free, and part of the standard library -- which is the whole requirement for the
command line. The interesting logic here is not storage but memory: deciding whether a
job is genuinely new to a search or something the user was already shown days ago.

The web deployment adds a second connection (``infra/turso.py``) and three rules that
this file enforces for both:

* Every write group is a transaction (``_tx``), nesting-safe, so a dismissal's spec
  change, status and feedback land together or not at all.
* A results screen reads ONE completed run, with the fields it lists and sorts by
  frozen on the verdict row, so nothing moves under an open page.
* A publisher proves it still holds the lease inside every batch it writes
  (``guard_statements``): the first statement of each batch inserts the number of valid
  leases held by its token into a table whose CHECK constraint refuses zero.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..domain.dedup import job_content_hash
from ..domain.models import (
    AuthorityTier,
    Coverage,
    Job,
    JobSourceRef,
    Location,
    MatchResult,
    SalaryRange,
    UserStatus,
    VerificationState,
    WorkplaceType,
)
from .schema import MIGRATIONS
from .turso import MAX_BATCH, TursoError, split_statements

# Failure kinds that say something about the HOST or the run, not about the board.
# Only "gone" and "parse_error" are evidence the board itself is a dead end.
NOT_THE_BOARDS_FAULT = frozenset({"rate_limited", "blocked", "host_blocked", "unavailable"})

DEFAULT_DB_DIR = Path.home() / ".jobagent"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "jobagent.sqlite3"

# A search's latest completed runs that a results screen may still be reading. Older
# runs expire: their rejected verdicts are pruned and the API answers "expired".
AVAILABLE_RUNS = 3
# A publisher renews its lease every minute; one that has not for five is dead.
LEASE_SECONDS = 300
# Jobs unseen this long, and untouched by the user, are removed.
RETENTION_DAYS = 60
# Existing rows are read in groups of this many identities.
LOOKUP_CHUNK = 250

Statement = tuple[str, Sequence[Any]]

_COMPLETED = "run_id IN (SELECT id FROM runs WHERE status = 'completed')"


class RevisionConflict(Exception):
    """The search was edited by someone else since this caller read it."""

    def __init__(self, current: int):
        super().__init__(f"search revision is now {current}")
        self.current = current


class LeaseLost(Exception):
    """This publisher no longer holds the lease; nothing it tried to write was kept."""


class SearchGone(Exception):
    """The search a run was evaluated for no longer exists in the cloud."""


@dataclass(frozen=True)
class Lease:
    """Proof of being the one publisher. ``request_id`` is set once a request is consumed."""

    token: str
    request_id: str | None = None


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


class Store:
    """Owns the database connection and every statement run against it."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path: Path | None = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Transactions are explicit here (``_tx``), so the module's implicit ones are off.
        # check_same_thread is off because the web app serves requests from a thread
        # pool; the store is still used by one request at a time.
        self.conn: Any = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0
        self.migrate()

    @classmethod
    def from_connection(cls, conn: Any) -> Store:
        """A store over an already-open connection: the cloud, or a test double.

        No path, no pragmas, no migration -- the cloud is migrated once by
        ``jobagent cloud init``, and a connection over HTTP sets its own pragmas.
        """
        store = cls.__new__(cls)
        store.path = None
        store.conn = conn
        store._depth = 0
        return store

    # ------------------------------------------------------------ transactions

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """One transaction around a group of writes, whatever the connection.

        Depth-counted: only the outermost block begins and ends the transaction, so a
        store method's own block inside a caller's block never commits early.
        """
        transaction = getattr(self.conn, "transaction", None)
        if transaction is not None:
            with transaction():
                yield
            return
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth = 0
            self.conn.execute("COMMIT")

    def _batch(self, stmts: Sequence[Statement]) -> int:
        """Run ``stmts`` as one atomic unit; return rows written where the backend counts.

        Over HTTP this is one request; locally it is one transaction. A statement
        against ``publish_guard`` that fails is the lease saying no.
        """
        if not stmts:
            return 0
        batch = getattr(self.conn, "batch", None)
        if batch is not None:
            try:
                return int(batch(stmts).rows_written)
            except TursoError as error:
                if "publish_guard" in (error.sql or ""):
                    raise LeaseLost(str(error)) from error
                raise
        current = ""
        try:
            with self._tx():
                for current, params in stmts:
                    self.conn.execute(current, params)
        except sqlite3.IntegrityError as error:
            if "publish_guard" in current:
                raise LeaseLost(str(error)) from error
            raise
        return 0

    # ------------------------------------------------------------------ schema

    def migrate(self) -> None:
        """Bring the database up to the current schema version.

        Each migration and its version row are one transaction, so an interrupted
        upgrade is retried rather than recorded half-applied.
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
            stmts: list[Statement] = [(statement, ()) for statement in split_statements(sql)]
            stmts.append((
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now().isoformat()),
            ))
            self._batch(stmts)

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------------- searches

    def save_spec(
        self, name: str, spec_yaml: str, *, expected_revision: int | None = None
    ) -> int:
        """Create or update a search; every update bumps its revision.

        With ``expected_revision`` the update is refused when someone else has edited
        the search since the caller read it, rather than silently overwriting them.
        """
        now = datetime.now().isoformat()
        with self._tx():
            row = self.conn.execute(
                "SELECT id, revision FROM searches WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO searches (uid, name, spec_yaml, created_at, updated_at, revision)
                       VALUES (?, ?, ?, ?, ?, 0)""",
                    (uuid.uuid4().hex, name, spec_yaml, now, now),
                )
                created = self.conn.execute(
                    "SELECT id FROM searches WHERE name = ?", (name,)
                ).fetchone()
                return int(created["id"])
            current = int(row["revision"])
            if expected_revision is not None and expected_revision != current:
                raise RevisionConflict(current)
            updated = self.conn.execute(
                """UPDATE searches SET spec_yaml = ?, updated_at = ?, revision = revision + 1
                   WHERE id = ? AND revision = ?""",
                (spec_yaml, now, int(row["id"]), current),
            )
            if updated.rowcount != 1:
                raise RevisionConflict(current + 1)
            return int(row["id"])

    def update_search(
        self, uid: str, *, name: str, spec_yaml: str, expected_revision: int
    ) -> int:
        """Edit a search by identity; returns the new revision.

        Refused (``RevisionConflict``) when the revision moved since the caller read
        it, so two tabs cannot silently overwrite each other.
        """
        now = datetime.now().isoformat()
        with self._tx():
            row = self.conn.execute(
                "SELECT revision FROM searches WHERE uid = ?", (uid,)
            ).fetchone()
            if row is None:
                raise SearchGone(uid)
            current = int(row["revision"])
            if current != expected_revision:
                raise RevisionConflict(current)
            updated = self.conn.execute(
                """UPDATE searches SET name = ?, spec_yaml = ?, updated_at = ?,
                       revision = revision + 1
                   WHERE uid = ? AND revision = ?""",
                (name, spec_yaml, now, uid, current),
            )
            if updated.rowcount != 1:
                raise RevisionConflict(current + 1)
            return current + 1

    def import_search(self, uid: str, name: str, spec_yaml: str, revision: int) -> int:
        """A cloud search copied into a scratch database, identity and revision intact."""
        now = datetime.now().isoformat()
        with self._tx():
            self.conn.execute(
                """INSERT INTO searches (uid, name, spec_yaml, created_at, updated_at, revision)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uid, name, spec_yaml, now, now, revision),
            )
        row = self.conn.execute("SELECT id FROM searches WHERE uid = ?", (uid,)).fetchone()
        return int(row["id"])

    def get_spec(self, name: str) -> tuple[int, str] | None:
        row = self.conn.execute(
            "SELECT id, spec_yaml FROM searches WHERE name = ?", (name,)
        ).fetchone()
        return (int(row["id"]), row["spec_yaml"]) if row else None

    def get_spec_by_id(self, search_id: int) -> tuple[str, str] | None:
        """(name, spec_yaml) for a search id, or None if it was deleted."""
        row = self.conn.execute(
            "SELECT name, spec_yaml FROM searches WHERE id = ?", (search_id,)
        ).fetchone()
        return (row["name"], row["spec_yaml"]) if row else None

    def get_search(
        self, *, name: str | None = None, uid: str | None = None, search_id: int | None = None
    ) -> Any:
        """The full search row (id, uid, name, revision, spec_yaml, timestamps)."""
        if uid is not None:
            clause, value = "uid = ?", uid
        elif name is not None:
            clause, value = "name = ?", name
        elif search_id is not None:
            clause, value = "id = ?", search_id
        else:
            raise ValueError("name, uid or search_id is required")
        return self.conn.execute(f"SELECT * FROM searches WHERE {clause}", (value,)).fetchone()

    def list_specs(self) -> list[Any]:
        return list(
            self.conn.execute(
                """SELECT s.id, s.uid, s.name, s.revision, s.updated_at,
                          (SELECT COUNT(*) FROM runs r WHERE r.search_id = s.id) AS runs,
                          (SELECT MAX(started_at) FROM runs r WHERE r.search_id = s.id) AS last_run
                   FROM searches s ORDER BY s.name"""
            )
        )

    def search_index(self) -> dict[str, Any]:
        """Every search by uid, for a publisher deciding where a run belongs."""
        return {
            row["uid"]: row
            for row in self.conn.execute("SELECT id, uid, name, revision FROM searches")
        }

    def delete_spec(self, name: str) -> bool:
        with self._tx():
            cursor = self.conn.execute("DELETE FROM searches WHERE name = ?", (name,))
            return cursor.rowcount > 0

    def delete_search(self, uid: str) -> bool:
        with self._tx():
            cursor = self.conn.execute("DELETE FROM searches WHERE uid = ?", (uid,))
            return cursor.rowcount > 0

    # --------------------------------------------------------------------- runs

    def start_run(
        self, search_id: int, started: datetime, *, spec_yaml: str = "",
        request_id: str | None = None,
    ) -> int:
        """Open a run, recording which revision of which search it evaluates."""
        with self._tx():
            search = self.conn.execute(
                "SELECT uid, revision FROM searches WHERE id = ?", (search_id,)
            ).fetchone()
            cursor = self.conn.execute(
                """INSERT INTO runs (search_id, search_uid, spec_revision, spec_yaml,
                                     request_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    search_id, search["uid"] if search else None,
                    int(search["revision"]) if search else None, spec_yaml, request_id,
                    started.isoformat(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def finish_run(
        self, run_id: int, finished: datetime, coverage: Coverage,
        found: int, matched: int, rejected: int, new_count: int, status: str = "completed",
    ) -> None:
        with self._tx():
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

    def get_run(self, run_id: int) -> Any:
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def latest_run(self, search_id: int) -> Any:
        return self.conn.execute(
            "SELECT * FROM runs WHERE search_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
            (search_id,),
        ).fetchone()

    def runs_for(self, search_id: int, limit: int = 20) -> list[Any]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs WHERE search_id = ? ORDER BY id DESC LIMIT ?",
                (search_id, limit),
            )
        )

    def run_sources(self, run_id: int) -> list[Any]:
        return list(
            self.conn.execute(
                "SELECT * FROM run_sources WHERE run_id = ? ORDER BY source", (run_id,)
            )
        )

    def latest_completed_run(self, search_id: int) -> Any:
        return self.conn.execute(
            """SELECT * FROM runs WHERE search_id = ? AND status = 'completed'
               ORDER BY id DESC LIMIT 1""",
            (search_id,),
        ).fetchone()

    def available_runs(self, search_id: int) -> list[Any]:
        """The completed runs a results screen may still read, newest first."""
        return list(
            self.conn.execute(
                """SELECT * FROM runs WHERE search_id = ? AND status = 'completed'
                   ORDER BY id DESC LIMIT ?""",
                (search_id, AVAILABLE_RUNS),
            )
        )

    def available_run_ids_all(self) -> list[int]:
        """The available runs of every search, for retention to keep its hands off."""
        return [
            int(row["id"])
            for row in self.conn.execute(
                """SELECT id FROM runs r WHERE status = 'completed'
                   AND (SELECT COUNT(*) FROM runs n WHERE n.search_id = r.search_id
                        AND n.status = 'completed' AND n.id > r.id) < ?""",
                (AVAILABLE_RUNS,),
            )
        ]

    def run_availability(self, search_id: int, run_id: int) -> str:
        """'available', 'expired' (completed, but retention has visited it), or 'unknown'."""
        row = self.get_run(run_id)
        if row is None or int(row["search_id"]) != search_id or row["status"] != "completed":
            return "unknown"
        available = {int(r["id"]) for r in self.available_runs(search_id)}
        return "available" if run_id in available else "expired"

    # --------------------------------------------------------------------- jobs

    _EXISTING_JOB = """SELECT id, identity, authority, content_hash,
                              length(description_text) AS description_len, workplace,
                              salary_min, posted_at, last_seen
                       FROM jobs WHERE identity IN ({})"""

    def upsert_job(self, job: Job, seen: datetime) -> int:
        """Insert or refresh a job, returning its row id.

        On a repeat sighting the canonical row is upgraded only when the new record is
        *more* authoritative, or as authoritative and at least as informative -- and
        only when its content actually differs. An unchanged posting is touched once a
        day and otherwise costs nothing.
        """
        existing = self.conn.execute(
            self._EXISTING_JOB.format("?"), (job.identity,)
        ).fetchone()
        sources: dict[str, str | None] = {}
        if existing is not None:
            sources = {
                row["url"]: row["last_seen"]
                for row in self.conn.execute(
                    "SELECT url, last_seen FROM job_sources WHERE job_id = ?",
                    (int(existing["id"]),),
                )
            }
        with self._tx():
            for sql, params in self._job_statements(job, existing, sources, seen):
                self.conn.execute(sql, params)
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE identity = ?", (job.identity,)
        ).fetchone()
        return int(row["id"])

    def _job_statements(
        self, job: Job, existing: Any, sources: dict[str, str | None], seen: datetime
    ) -> list[Statement]:
        """The writes one sighting of a job needs, given what is already stored.

        Shared by the local path and the cloud publisher, so both apply the same rule.
        Sources are decided one URL at a time: a new URL for an unchanged job is still
        inserted, and each URL's ``last_seen`` moves on its own day.
        """
        stmts: list[Statement] = []
        lo = job.salary.minimum if job.salary else None
        hi = job.salary.maximum if job.salary else None
        cur = job.salary.currency if job.salary else None
        per = job.salary.period if job.salary else None
        digest = job_content_hash(job)
        day = seen.date().isoformat()

        if existing is None:
            stmts.append((
                """INSERT INTO jobs (identity, title, company, url, description_text,
                        location_raw, city, region, country, workplace, employment_type,
                        department, salary_min, salary_max, salary_currency, salary_period,
                        posted_at, authority, first_seen, last_seen, verification, verified_at,
                        content_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.identity, job.title, job.company, job.url, job.description_text,
                    job.location.raw, job.location.city, job.location.region,
                    job.location.country, job.workplace.value, job.employment_type,
                    job.department, lo, hi, cur, per, _iso(job.posted_at),
                    int(job.authority), _iso(job.first_seen or seen), _iso(seen),
                    job.verification.value, _iso(job.verified_at), digest,
                ),
            ))
        else:
            # Equal authority is not licence to overwrite. A later sparse sighting --
            # exactly what a failed Workday enrichment produces -- could replace a
            # stored description, workplace and salary with blanks, after which every
            # text gate went unverifiable on evidence already persisted.
            upgrade = int(job.authority) > int(existing["authority"]) or (
                int(job.authority) == int(existing["authority"])
                and _at_least_as_informative(job, existing)
            )
            if upgrade and digest != (existing["content_hash"] or ""):
                stmts.append((
                    """UPDATE jobs SET title=?, company=?, url=?, description_text=?,
                           location_raw=?, city=?, region=?, country=?, workplace=?,
                           employment_type=?, department=?, salary_min=?, salary_max=?,
                           salary_currency=?, salary_period=?, posted_at=?, authority=?,
                           last_seen=?, content_hash=? WHERE identity=?""",
                    (
                        job.title, job.company, job.url, job.description_text,
                        job.location.raw, job.location.city, job.location.region,
                        job.location.country, job.workplace.value, job.employment_type,
                        job.department, lo, hi, cur, per, _iso(job.posted_at),
                        int(job.authority), _iso(seen), digest, job.identity,
                    ),
                ))
            elif (existing["last_seen"] or "")[:10] != day:
                stmts.append((
                    "UPDATE jobs SET last_seen=? WHERE identity=?", (_iso(seen), job.identity)
                ))

        for ref in job.sources:
            if ref.url not in sources:
                # The job row exists by now: it was inserted earlier in this same
                # transaction or was already stored. OR IGNORE covers a URL two refs
                # share within one sighting.
                stmts.append((
                    """INSERT OR IGNORE INTO job_sources
                       (job_id, source, url, external_id, authority, first_seen, last_seen)
                       SELECT id, ?, ?, ?, ?, ?, ? FROM jobs WHERE identity = ?""",
                    (
                        ref.source, ref.url, ref.external_id, int(ref.authority),
                        _iso(ref.first_seen or seen), _iso(ref.last_seen or seen), job.identity,
                    ),
                ))
                sources[ref.url] = _iso(seen)
            elif (sources[ref.url] or "")[:10] != day:
                stmts.append((
                    """UPDATE job_sources SET last_seen = ?
                       WHERE url = ? AND job_id = (SELECT id FROM jobs WHERE identity = ?)""",
                    (_iso(seen), ref.url, job.identity),
                ))
                sources[ref.url] = _iso(seen)
        return stmts

    def upsert_jobs(
        self, jobs: Sequence[Job], lease: Lease | None = None, now: datetime | None = None
    ) -> tuple[dict[str, int], int]:
        """The batch form of ``upsert_job``: (identity -> row id, rows written).

        Existing rows and their URLs are read in groups, the statements each job needs
        are gathered under the lease guard, and the ids are read back afterwards. A
        job whose id is not found later is a loud error, never a dropped verdict.
        """
        ids: dict[str, int] = {}
        written = 0
        now = now or datetime.now()
        for chunk in _chunks(list(jobs), LOOKUP_CHUNK):
            identities = [job.identity for job in chunk]
            marks = _placeholders(len(identities))
            existing = {
                row["identity"]: row
                for row in self.conn.execute(self._EXISTING_JOB.format(marks), identities)
            }
            sources: dict[str, dict[str, str | None]] = {}
            for row in self.conn.execute(
                f"""SELECT j.identity, s.url, s.last_seen FROM job_sources s
                    JOIN jobs j ON j.id = s.job_id WHERE j.identity IN ({marks})""",
                identities,
            ):
                sources.setdefault(row["identity"], {})[row["url"]] = row["last_seen"]

            pending: list[Statement] = []
            prefix = self.guard_statements(lease, now) if lease else []
            for job in chunk:
                stmts = self._job_statements(
                    job, existing.get(job.identity), sources.get(job.identity, {}),
                    job.last_seen or now,
                )
                if pending and len(prefix) + len(pending) + len(stmts) > MAX_BATCH:
                    written += self._batch([*prefix, *pending])
                    pending = []
                pending.extend(stmts)
            if pending:
                written += self._batch([*prefix, *pending])

            for row in self.conn.execute(
                f"SELECT id, identity FROM jobs WHERE identity IN ({marks})", identities
            ):
                ids[row["identity"]] = int(row["id"])
        return ids, written

    def job_seen_by_search(self, job_id: int, search_id: int) -> bool:
        """Has this job ever been SHOWN for this search before?

        This single question separates "I found this today" from "I showed you this three
        days ago", and it is asked before the current run's verdict is written.

        Only prior MATCHES from completed runs count. A job that was previously rejected
        was never put in front of the user, so when a search is later relaxed and that
        job qualifies, it is genuinely new to them -- counting the old rejection hid it
        from --new.
        """
        row = self.conn.execute(
            f"""SELECT 1 FROM job_search_matches
                WHERE job_id = ? AND search_id = ? AND decision = 'match' AND {_COMPLETED}
                LIMIT 1""",
            (job_id, search_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def _frozen(job: Job) -> tuple[Any, ...]:
        """The card and ordering fields a verdict row carries, as the run saw them."""
        salary = job.salary
        return (
            job.title, job.company, job.location.raw, job.workplace.value,
            job.employment_type,
            salary.minimum if salary else None, salary.maximum if salary else None,
            salary.currency if salary else None, salary.period if salary else None,
            _iso(job.posted_at), job.url, job_content_hash(job),
        )

    _MATCH_COLUMNS = (
        "job_id, search_id, run_id, decision, score, uncertain, explanation, is_new, "
        "created_at, title, company, location_raw, workplace, employment_type, salary_min, "
        "salary_max, salary_currency, salary_period, posted_at, url, content_hash"
    )

    def record_match(
        self, job_id: int, search_id: int, run_id: int, result: MatchResult, is_new: bool,
        *, job: Job,
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
        now = datetime.now().isoformat()
        with self._tx():
            self.conn.execute(
                f"INSERT INTO job_search_matches ({self._MATCH_COLUMNS}) "
                f"VALUES ({_placeholders(21)})",
                (
                    job_id, search_id, run_id, result.decision.value, result.score,
                    int(result.is_uncertain), json.dumps(explanation), int(is_new), now,
                    *self._frozen(job),
                ),
            )
            if is_new:
                self.conn.execute(
                    """INSERT INTO user_job_status (job_id, status, changed_at)
                       VALUES (?, ?, ?) ON CONFLICT(job_id) DO NOTHING""",
                    (job_id, UserStatus.NEW.value, now),
                )

    def record_matches(
        self, run_id: int, search_id: int, rows: Sequence[Any], ids: dict[str, int],
        lease: Lease, now: datetime,
    ) -> int:
        """Publish a run's verdicts, with ``is_new`` decided against the cloud's history.

        Explicit ids only: a job whose identity has no id is a KeyError here, never a
        zero-row insert nobody notices.
        """
        written = 0
        prefix = self.guard_statements(lease, now)
        sql = (
            f"INSERT INTO job_search_matches ({self._MATCH_COLUMNS}) VALUES ("
            "?, ?, ?, ?, ?, ?, ?, "
            "NOT EXISTS (SELECT 1 FROM job_search_matches m JOIN runs r ON r.id = m.run_id "
            "AND r.status = 'completed' WHERE m.job_id = ? AND m.search_id = ? "
            "AND m.decision = 'match'), "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        for chunk in _chunks(list(rows), MAX_BATCH - len(prefix)):
            stmts: list[Statement] = list(prefix)
            for row in chunk:
                job_id = ids[row["identity"]]
                stmts.append((sql, (
                    job_id, search_id, run_id, row["decision"], row["score"],
                    row["uncertain"], row["explanation"], job_id, search_id,
                    row["created_at"], row["title"], row["company"], row["location_raw"],
                    row["workplace"], row["employment_type"], row["salary_min"],
                    row["salary_max"], row["salary_currency"], row["salary_period"],
                    row["posted_at"], row["url"], row["content_hash"],
                )))
            written += self._batch(stmts)
        return written

    # ------------------------------------------------------------- user status

    def set_user_status(self, job_id: int, status: str, note: str = "") -> None:
        now = datetime.now().isoformat()
        with self._tx():
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
        with self._tx():
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
                       WHERE user_job_status.status NOT IN ('applied','saved','dismissed')""",
                    (job_id, UserStatus.CLOSED.value, when.isoformat()),
                )

    # ------------------------------------------------------------------ results

    _RUN_COLUMNS = """
        j.id, j.identity, j.first_seen, j.last_seen, j.verification, j.description_text,
        j.department, j.city, j.region, j.country, j.authority,
        m.title, m.company, m.location_raw, m.workplace, m.employment_type,
        m.salary_min, m.salary_max, m.salary_currency, m.salary_period, m.posted_at, m.url,
        m.decision, m.score, m.uncertain, m.explanation, m.is_new, m.run_id, m.search_id,
        m.id AS match_id, m.content_hash AS evaluated_hash,
        (j.content_hash != m.content_hash) AS changed_since,
        COALESCE(u.status, 'new') AS user_status
    """

    _RUN_ORDERS = {
        "score": "m.score DESC, m.id ASC",
        "date": "COALESCE(m.posted_at, m.created_at) DESC, m.id ASC",
        "company": "m.company ASC, m.score DESC, m.id ASC",
        "title": "m.title ASC, m.id ASC",
    }
    _HISTORY_ORDERS = {
        "score": "m.score DESC, j.last_seen DESC, m.id ASC",
        "date": "COALESCE(j.posted_at, j.first_seen) DESC, m.id ASC",
        "company": "j.company ASC, m.score DESC, m.id ASC",
        "title": "j.title ASC, m.id ASC",
    }

    @staticmethod
    def _result_clauses(
        *, only_new: bool, decision: str | None, statuses: list[str] | None,
        min_score: float | None, include_dismissed: bool,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_dismissed:
            clauses.append("COALESCE(u.status, 'new') != ?")
            params.append(UserStatus.DISMISSED.value)
        if decision:
            clauses.append("m.decision = ?")
            params.append(decision)
        if only_new:
            clauses.append("m.is_new = 1")
        if min_score is not None:
            clauses.append("m.score >= ?")
            params.append(min_score)
        if statuses:
            clauses.append(f"COALESCE(u.status, 'new') IN ({_placeholders(len(statuses))})")
            params.extend(statuses)
        return clauses, params

    def results(
        self, search_id: int, *, only_new: bool = False, decision: str | None = "match",
        statuses: list[str] | None = None, run_id: int | None = None,
        min_score: float | None = None, limit: int = 100, order: str = "score",
        include_dismissed: bool = True, offset: int = 0,
    ) -> list[Any]:
        """Verdicts for a search, with filters applied.

        With ``run_id`` this is ONE run's verdicts, listed and ordered by the fields
        frozen on each verdict row, so a screen reading a run never sees it move. It is
        what the search screen and the post-run table use.

        Without ``run_id`` it is the historical view: each job's latest verdict across
        completed runs. Re-running a search revises what the user sees rather than
        stacking a new copy beside the old one.

        ``include_dismissed`` defaults to True so exports and engine callers see
        everything; the results screens pass False, because a job the user turned away
        must not come back on the next run.
        """
        clauses, params = self._result_clauses(
            only_new=only_new, decision=decision, statuses=statuses, min_score=min_score,
            include_dismissed=include_dismissed,
        )
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        if run_id is not None:
            order_sql = self._RUN_ORDERS.get(order, self._RUN_ORDERS["score"])
            sql = f"""
                SELECT {self._RUN_COLUMNS}
                FROM job_search_matches m
                JOIN jobs j ON j.id = m.job_id
                LEFT JOIN user_job_status u ON u.job_id = j.id
                WHERE m.search_id = ? AND m.run_id = ?{where}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
            """
            return list(self.conn.execute(sql, [search_id, run_id, *params, limit, offset]))

        order_sql = self._HISTORY_ORDERS.get(order, self._HISTORY_ORDERS["score"])
        sql = f"""
            SELECT j.*, m.decision, m.score, m.uncertain, m.explanation, m.is_new,
                   m.run_id, m.search_id, m.id AS match_id,
                   COALESCE(u.status, 'new') AS user_status
            FROM job_search_matches m
            JOIN jobs j ON j.id = m.job_id
            LEFT JOIN user_job_status u ON u.job_id = j.id
            WHERE m.id IN (
                SELECT MAX(m2.id) FROM job_search_matches m2
                JOIN runs r ON r.id = m2.run_id AND r.status = 'completed'
                WHERE m2.search_id = ? GROUP BY m2.job_id
            ){where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        return list(self.conn.execute(sql, [search_id, *params, limit, offset]))

    def count_results(
        self, search_id: int, *, run_id: int, only_new: bool = False,
        decision: str | None = "match", statuses: list[str] | None = None,
        min_score: float | None = None, include_dismissed: bool = True,
    ) -> int:
        """How many verdicts ``results`` would list for one run under the same filters."""
        clauses, params = self._result_clauses(
            only_new=only_new, decision=decision, statuses=statuses, min_score=min_score,
            include_dismissed=include_dismissed,
        )
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        row = self.conn.execute(
            f"""SELECT COUNT(*) AS n FROM job_search_matches m
                LEFT JOIN user_job_status u ON u.job_id = m.job_id
                WHERE m.search_id = ? AND m.run_id = ?{where}""",
            [search_id, run_id, *params],
        ).fetchone()
        return int(row["n"])

    def explanation_for(self, job_id: int, run_id: int) -> dict[str, Any] | None:
        """The verdict one run gave one job, or None if that run never judged it."""
        row = self.conn.execute(
            "SELECT explanation FROM job_search_matches WHERE job_id = ? AND run_id = ?",
            (job_id, run_id),
        ).fetchone()
        return json.loads(row["explanation"]) if row else None

    def match_row(self, job_id: int, run_id: int) -> Any:
        """The frozen verdict row for one job in one run."""
        return self.conn.execute(
            "SELECT * FROM job_search_matches WHERE job_id = ? AND run_id = ?",
            (job_id, run_id),
        ).fetchone()

    def get_job(self, job_id: int) -> Any:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def latest_search_for_job(self, job_id: int) -> int | None:
        """The search that most recently judged this job in a completed run, if any."""
        found = self.conn.execute(
            f"""SELECT search_id FROM job_search_matches
                WHERE job_id = ? AND {_COMPLETED} ORDER BY id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        return int(found["search_id"]) if found else None

    def latest_explanation(self, job_id: int, search_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            f"""SELECT explanation FROM job_search_matches
                WHERE job_id = ? AND search_id = ? AND {_COMPLETED} ORDER BY id DESC LIMIT 1""",
            (job_id, search_id),
        ).fetchone()
        return json.loads(row["explanation"]) if row else {}

    def saved_jobs(self, statuses: Sequence[str] = ("saved", "applied")) -> list[Any]:
        """Jobs the user has kept, across every search, most recently changed first."""
        return list(
            self.conn.execute(
                f"""SELECT j.*, u.status AS user_status, u.note, u.changed_at
                    FROM user_job_status u JOIN jobs j ON j.id = u.job_id
                    WHERE u.status IN ({_placeholders(len(statuses))})
                    ORDER BY u.changed_at DESC, j.id DESC""",
                list(statuses),
            )
        )

    # ----------------------------------------------------------------- feedback

    def record_feedback(
        self, job_id: int, search_id: int | None, reason: str, rules: list[dict[str, str]]
    ) -> int:
        """Keep why a job was dismissed, with the rules the reason became."""
        with self._tx():
            cursor = self.conn.execute(
                """INSERT INTO job_feedback (job_id, search_id, reason, rules_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (job_id, search_id, reason, json.dumps(rules), datetime.now().isoformat()),
            )
            return int(cursor.lastrowid or 0)

    def feedback(self, search_id: int) -> list[Any]:
        """Every dismissal recorded against a search, newest first."""
        return list(
            self.conn.execute(
                """SELECT f.*, j.title, j.company
                   FROM job_feedback f JOIN jobs j ON j.id = f.job_id
                   WHERE f.search_id = ?
                   ORDER BY f.created_at DESC, f.id DESC""",
                (search_id,),
            )
        )

    def job_source_refs(self, job_id: int) -> list[Any]:
        return list(
            self.conn.execute(
                "SELECT * FROM job_sources WHERE job_id = ? ORDER BY authority DESC", (job_id,)
            )
        )

    # ---------------------------------------------------------------- discovery

    def discovery_state(self, backend: str, crawl: str, pattern: str) -> Any:
        return self.conn.execute(
            """SELECT * FROM discovery_progress
               WHERE backend=? AND crawl=? AND pattern=?""",
            (backend, crawl, pattern),
        ).fetchone()

    def record_discovery(
        self, backend: str, crawl: str, pattern: str, *, next_page: int,
        total_pages: int | None, urls_seen: int, boards_found: int, boards_new: int,
        completed: bool, note: str = "",
    ) -> None:
        """Advance a sweep's cursor. Counters accumulate across resumptions."""
        now = datetime.now().isoformat()
        with self._tx():
            self.conn.execute(
                """INSERT INTO discovery_progress
                   (backend, crawl, pattern, next_page, total_pages, urls_seen,
                    boards_found, boards_new, completed, last_note, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(backend, crawl, pattern) DO UPDATE SET
                       next_page=excluded.next_page,
                       total_pages=COALESCE(excluded.total_pages,
                                            discovery_progress.total_pages),
                       urls_seen=discovery_progress.urls_seen + excluded.urls_seen,
                       boards_found=discovery_progress.boards_found + excluded.boards_found,
                       boards_new=discovery_progress.boards_new + excluded.boards_new,
                       completed=excluded.completed,
                       last_note=excluded.last_note,
                       updated_at=excluded.updated_at""",
                (backend, crawl, pattern, next_page, total_pages, urls_seen, boards_found,
                 boards_new, int(completed), note, now),
            )

    def discovery_report(self, backend: str | None = None) -> list[Any]:
        sql = "SELECT * FROM discovery_progress"
        params: list[Any] = []
        if backend:
            sql += " WHERE backend = ?"
            params.append(backend)
        return list(self.conn.execute(sql + " ORDER BY crawl DESC, pattern", params))

    def known_board_keys(self) -> set[tuple[str, str]]:
        """Every (platform, token) already registered, for new-versus-known counting."""
        return {
            (row["ats"], row["token"])
            for row in self.conn.execute("SELECT ats, token FROM company_registry")
        }

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
        with self._tx():
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

    def registry_rows(self) -> list[Any]:
        return list(self.conn.execute("SELECT * FROM company_registry ORDER BY id"))

    def insert_registry_rows(self, rows: Sequence[Any]) -> int:
        """The cloud's registry copied into a scratch database, health included."""
        stmts: list[Statement] = [
            (
                """INSERT OR IGNORE INTO company_registry
                   (company, domain, ats, token, board_url, source, us_signal, first_seen,
                    last_verified, last_success, consecutive_failures, last_failure_kind, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["company"], row["domain"], row["ats"], row["token"], row["board_url"],
                    row["source"], row["us_signal"], row["first_seen"], row["last_verified"],
                    row["last_success"], row["consecutive_failures"], row["last_failure_kind"],
                    row["notes"],
                ),
            )
            for row in rows
        ]
        for chunk in _chunks(stmts, MAX_BATCH):
            self._batch(chunk)
        return len(stmts)

    def registry_health_rows(self, since: datetime) -> list[Any]:
        """Boards whose health changed since ``since``, for publishing back."""
        return list(
            self.conn.execute(
                """SELECT ats, token, consecutive_failures, last_success, last_verified,
                          last_failure_kind
                   FROM company_registry WHERE last_verified >= ?""",
                (since.isoformat(),),
            )
        )

    def update_registry_health(self, rows: Sequence[Any], lease: Lease, now: datetime) -> int:
        written = 0
        prefix = self.guard_statements(lease, now)
        for chunk in _chunks(list(rows), MAX_BATCH - len(prefix)):
            stmts: list[Statement] = list(prefix)
            for row in chunk:
                stmts.append((
                    """UPDATE company_registry
                       SET consecutive_failures=?, last_success=?, last_verified=?,
                           last_failure_kind=?
                       WHERE ats=? AND token=?""",
                    (
                        row["consecutive_failures"], row["last_success"], row["last_verified"],
                        row["last_failure_kind"], row["ats"], row["token"],
                    ),
                ))
            written += self._batch(stmts)
        return written

    def registry_targets(
        self, *, platforms: list[str] | None = None, limit: int = 500,
        prefer_us: bool = True, max_failures: int = 5,
    ) -> list[Any]:
        """Pick which boards to fan out to this run.

        Ordering is the rate budget in disguise. US-signalled boards go first because the
        product is US-scoped, then boards that succeeded recently, then everything else.
        Boards that have failed repeatedly sink, and past ``max_failures`` drop out
        entirely so a run is not spent re-probing dead tenants.
        """
        clauses = ["consecutive_failures < ?"]
        params: list[Any] = [max_failures]
        if platforms:
            clauses.append(f"ats IN ({_placeholders(len(platforms))})")
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
        with self._tx():
            if ok:
                self.conn.execute(
                    """UPDATE company_registry SET consecutive_failures=0, last_success=?,
                           last_verified=?, last_failure_kind='' WHERE id=?""",
                    (now, now, registry_id),
                )
            elif kind in NOT_THE_BOARDS_FAULT:
                # Recorded, but never counted against the board. None of these are
                # evidence about the BOARD: a shared host throttling or refusing us says
                # nothing about the employer behind it, an unattempted board was never
                # tried, and a 5xx is a server having a bad day. Counting them evicted
                # healthy boards after a handful of transient outages -- the exact
                # failure the separate failure_kind column exists to prevent.
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

    # -------------------------------------------------------- publishing a run

    @staticmethod
    def guard_statements(lease: Lease, now: datetime) -> list[Statement]:
        """The two statements every publish batch starts with.

        The insert carries the number of valid leases held by this token; zero fails
        the CHECK, the statement errors, and the batch rolls back.
        """
        return [
            ("DELETE FROM publish_guard", ()),
            (
                """INSERT INTO publish_guard (ok)
                   SELECT COUNT(*) FROM publish_lease WHERE token = ? AND expires_at >= ?""",
                (lease.token, now.isoformat()),
            ),
        ]

    def _guarded(self, lease: Lease, now: datetime) -> None:
        """The guard, executed inside the current transaction; LeaseLost when it fails."""
        try:
            for sql, params in self.guard_statements(lease, now):
                self.conn.execute(sql, params)
        except (sqlite3.IntegrityError, TursoError) as error:
            raise LeaseLost(str(error)) from error

    def jobs_for_run(self, run_id: int) -> list[Job]:
        """The jobs a run judged, rebuilt with their provenance, for publishing."""
        jobs: list[Job] = []
        for row in self.conn.execute(
            """SELECT * FROM jobs
               WHERE id IN (SELECT job_id FROM job_search_matches WHERE run_id = ?)
               ORDER BY id""",
            (run_id,),
        ):
            job = row_to_job(row)
            job.sources = [
                JobSourceRef(
                    source=ref["source"], url=ref["url"], external_id=ref["external_id"],
                    authority=AuthorityTier(ref["authority"]),
                    first_seen=_from_iso(ref["first_seen"]),
                    last_seen=_from_iso(ref["last_seen"]),
                )
                for ref in self.job_source_refs(int(row["id"]))
            ]
            jobs.append(job)
        return jobs

    def matches_for_run(self, run_id: int) -> list[Any]:
        return list(
            self.conn.execute(
                """SELECT m.*, j.identity FROM job_search_matches m
                   JOIN jobs j ON j.id = m.job_id WHERE m.run_id = ? ORDER BY m.id""",
                (run_id,),
            )
        )

    def insert_run(self, row: Any, lease: Lease, now: datetime) -> int:
        """Open a published run against the search's cloud id. SearchGone if it left."""
        with self._tx():
            self._guarded(lease, now)
            search = self.conn.execute(
                "SELECT id FROM searches WHERE uid = ?", (row["search_uid"],)
            ).fetchone()
            if search is None:
                raise SearchGone(row["search_uid"])
            cursor = self.conn.execute(
                """INSERT INTO runs (search_id, search_uid, spec_revision, spec_yaml,
                                     request_id, started_at, finished_at, status, found,
                                     matched, rejected, new_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, 0)""",
                (
                    int(search["id"]), row["search_uid"], row["spec_revision"],
                    row["spec_yaml"], lease.request_id, row["started_at"],
                    row["finished_at"], row["found"], row["matched"], row["rejected"],
                ),
            )
            if cursor.rowcount != 1 or not cursor.lastrowid:
                raise RuntimeError("the run row was not inserted")
            return int(cursor.lastrowid)

    def copy_run_sources(
        self, run_id: int, rows: Sequence[Any], lease: Lease, now: datetime
    ) -> int:
        stmts: list[Statement] = self.guard_statements(lease, now)
        for row in rows:
            stmts.append((
                """INSERT INTO run_sources
                   (run_id, source, status, found, requests, duration_ms, note)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    run_id, row["source"], row["status"], row["found"], row["requests"],
                    row["duration_ms"], row["note"],
                ),
            ))
        return self._batch(stmts)

    def update_new_count(self, run_id: int, lease: Lease, now: datetime) -> int:
        with self._tx():
            self._guarded(lease, now)
            self.conn.execute(
                """UPDATE runs SET new_count = (
                       SELECT COUNT(*) FROM job_search_matches
                       WHERE run_id = ? AND is_new = 1 AND decision = 'match')
                   WHERE id = ?""",
                (run_id, run_id),
            )
            return int(self.get_run(run_id)["new_count"])

    def complete_run(self, run_id: int, finished: datetime, lease: Lease, now: datetime) -> None:
        """The visibility flip: from here on the run is one a screen may select."""
        with self._tx():
            self._guarded(lease, now)
            self.conn.execute(
                "UPDATE runs SET status = 'completed', finished_at = ? WHERE id = ?",
                (finished.isoformat(), run_id),
            )

    def abandon_stale_runs(self, lease: Lease, now: datetime) -> int:
        """Remove runs a previous publisher left unfinished. Never the lease holder's own."""
        with self._tx():
            self._guarded(lease, now)
            self.conn.execute(
                """DELETE FROM job_search_matches WHERE run_id IN (
                       SELECT id FROM runs WHERE status = 'running'
                       AND (request_id IS NULL OR request_id != ?))""",
                (lease.request_id or "",),
            )
            cursor = self.conn.execute(
                """DELETE FROM runs WHERE status = 'running'
                   AND (request_id IS NULL OR request_id != ?)""",
                (lease.request_id or "",),
            )
            return int(cursor.rowcount)

    def expire_runs(self, search_id: int, lease: Lease, now: datetime) -> int:
        """Prune rejected verdicts of runs older than the available window.

        Only for jobs that have a verdict in a newer run: a job not encountered again
        keeps its latest verdict, whatever it was, so an old match can never resurface
        because a newer rejection was pruned. Match verdicts are never pruned.
        """
        available = [int(row["id"]) for row in self.available_runs(search_id)]
        if len(available) < AVAILABLE_RUNS:
            return 0
        with self._tx():
            self._guarded(lease, now)
            cursor = self.conn.execute(
                """DELETE FROM job_search_matches WHERE id IN (
                       SELECT m.id FROM job_search_matches m
                       WHERE m.search_id = ? AND m.decision = 'rejected' AND m.run_id < ?
                       AND EXISTS (SELECT 1 FROM job_search_matches n
                                   WHERE n.job_id = m.job_id AND n.search_id = m.search_id
                                   AND n.run_id > m.run_id))""",
                (search_id, min(available)),
            )
            return int(cursor.rowcount)

    def delete_stale_jobs(
        self, lease: Lease, now: datetime, *, days: int = RETENTION_DAYS
    ) -> int:
        """Remove jobs unseen for ``days`` that nobody kept and no available run shows."""
        cutoff = (now - timedelta(days=days)).isoformat()
        protected = self.available_run_ids_all()
        shield = (
            f" AND id NOT IN (SELECT job_id FROM job_search_matches WHERE run_id IN "
            f"({_placeholders(len(protected))}))" if protected else ""
        )
        with self._tx():
            self._guarded(lease, now)
            cursor = self.conn.execute(
                f"""DELETE FROM jobs WHERE last_seen < ?
                    AND id NOT IN (SELECT job_id FROM user_job_status
                                   WHERE status IN ('saved', 'applied', 'dismissed'))
                    AND id NOT IN (SELECT job_id FROM job_feedback){shield}""",
                [cutoff, *protected],
            )
            return int(cursor.rowcount)

    # ---------------------------------------------------------- lease and queue

    def take_lease(self, now: datetime, token: str | None = None) -> Lease | None:
        """Become the one publisher, or learn that someone else still is.

        One transaction: overwrite the lease row if it has expired, prove the overwrite
        stuck (the guard), and close any running request whose token is no longer the
        lease's -- that is the previous owner, and this is how it learns.
        """
        token = token or uuid.uuid4().hex
        expires = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
        lease = Lease(token)
        try:
            with self._tx():
                self.conn.execute(
                    """UPDATE publish_lease SET token = ?, request_id = NULL, expires_at = ?
                       WHERE singleton = 1 AND expires_at < ?""",
                    (token, expires, now.isoformat()),
                )
                self._guarded(lease, now)
                self.conn.execute(
                    """UPDATE run_requests SET status = 'failed', finished_at = ?,
                           note = 'lease taken over by another runner'
                       WHERE status = 'running' AND lease_token IS NOT NULL
                       AND lease_token != ?""",
                    (now.isoformat(), token),
                )
        except LeaseLost:
            return None
        return lease

    def consume_request(
        self, lease: Lease, now: datetime, origin: str, priority_uid: str | None = None
    ) -> Lease:
        """Claim the waiting request, or open one of ``origin`` when none is waiting."""
        with self._tx():
            self._guarded(lease, now)
            request_id: str | None = None
            queued = self.conn.execute(
                """SELECT id FROM run_requests WHERE status = 'queued'
                   ORDER BY requested_at, id LIMIT 1"""
            ).fetchone()
            if queued is not None:
                claimed = self.conn.execute(
                    """UPDATE run_requests SET status = 'running', lease_token = ?, started_at = ?
                       WHERE id = ? AND status = 'queued'""",
                    (lease.token, now.isoformat(), queued["id"]),
                )
                if claimed.rowcount == 1:
                    request_id = str(queued["id"])
            if request_id is None:
                request_id = uuid.uuid4().hex
                self.conn.execute(
                    """INSERT INTO run_requests
                       (id, priority_uid, status, origin, lease_token, requested_at, started_at)
                       VALUES (?, ?, 'running', ?, ?, ?, ?)""",
                    (
                        request_id, priority_uid, origin, lease.token, now.isoformat(),
                        now.isoformat(),
                    ),
                )
            self.conn.execute(
                "UPDATE publish_lease SET request_id = ? WHERE token = ?", (request_id, lease.token)
            )
        return replace(lease, request_id=request_id)

    def renew_lease(self, lease: Lease, now: datetime) -> bool:
        """Push the expiry out; False means the lease is no longer this publisher's."""
        expires = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
        cursor = self.conn.execute(
            "UPDATE publish_lease SET expires_at = ? WHERE token = ? AND expires_at >= ?",
            (expires, lease.token, now.isoformat()),
        )
        return cursor.rowcount == 1

    def finish_request(
        self, lease: Lease, status: str, note: str, rows_written: int, now: datetime
    ) -> bool:
        """Report the outcome and release the lease. False means the lease was lost, in
        which case nothing is reported: a replaced runner does not get to claim success."""
        try:
            with self._tx():
                done = self.conn.execute(
                    """UPDATE run_requests
                       SET status = ?, note = ?, rows_written = ?, finished_at = ?
                       WHERE id = ? AND lease_token = ?
                       AND EXISTS (SELECT 1 FROM publish_lease
                                   WHERE token = ? AND expires_at >= ?)""",
                    (
                        status, note, rows_written, now.isoformat(), lease.request_id,
                        lease.token, lease.token, now.isoformat(),
                    ),
                )
                if done.rowcount != 1:
                    raise LeaseLost("the lease was taken over or expired")
                self.conn.execute(
                    "UPDATE publish_lease SET expires_at = '' WHERE token = ?", (lease.token,)
                )
        except LeaseLost:
            return False
        return True

    def create_queued(
        self, origin: str, priority_uid: str | None, now: datetime
    ) -> tuple[str, bool]:
        """Queue a request, or attach to the one already waiting: (id, attached).

        The partial unique index on ``status = 'queued'`` is what makes this atomic
        under two simultaneous clicks; the second insert fails and reads the first.
        """
        request_id = uuid.uuid4().hex
        try:
            with self._tx():
                self.conn.execute(
                    """INSERT INTO run_requests (id, priority_uid, status, origin, requested_at)
                       VALUES (?, ?, 'queued', ?, ?)""",
                    (request_id, priority_uid, origin, now.isoformat()),
                )
        except (sqlite3.IntegrityError, TursoError):
            waiting = self.queued_request()
            if waiting is None:
                raise
            return str(waiting["id"]), True
        return request_id, False

    def fail_request(self, request_id: str, note: str, now: datetime) -> None:
        """A request that cannot proceed -- the wake-up failed -- and must not block
        the queue."""
        with self._tx():
            self.conn.execute(
                """UPDATE run_requests SET status = 'failed', finished_at = ?, note = ?
                   WHERE id = ? AND status = 'queued'""",
                (now.isoformat(), note, request_id),
            )

    def cancel_queued(self, request_id: str, now: datetime) -> bool:
        with self._tx():
            cursor = self.conn.execute(
                """UPDATE run_requests SET status = 'cancelled', finished_at = ?
                   WHERE id = ? AND status = 'queued'""",
                (now.isoformat(), request_id),
            )
            return cursor.rowcount == 1

    def get_request(self, request_id: str) -> Any:
        return self.conn.execute(
            "SELECT * FROM run_requests WHERE id = ?", (request_id,)
        ).fetchone()

    def list_requests(self, limit: int = 20) -> list[Any]:
        return list(
            self.conn.execute(
                "SELECT * FROM run_requests ORDER BY requested_at DESC, id DESC LIMIT ?", (limit,)
            )
        )

    def queued_request(self) -> Any:
        return self.conn.execute(
            "SELECT * FROM run_requests WHERE status = 'queued' ORDER BY requested_at LIMIT 1"
        ).fetchone()

    def active_request(self, now: datetime) -> Any:
        """The request whose runner holds a valid lease right now, if any."""
        return self.conn.execute(
            """SELECT r.*, l.expires_at AS lease_expires_at FROM run_requests r
               JOIN publish_lease l ON l.request_id = r.id
               WHERE r.status = 'running' AND l.expires_at >= ?""",
            (now.isoformat(),),
        ).fetchone()

    def lease_row(self) -> Any:
        return self.conn.execute("SELECT * FROM publish_lease WHERE singleton = 1").fetchone()

    def reconcile_requests(self, now: datetime, *, queued_after_hours: float = 2.0) -> None:
        """Mark what the display can already tell is over. Display only: a runner learns
        it has lost its lease from its own writes, never from this."""
        stale_queue = (now - timedelta(hours=queued_after_hours)).isoformat()
        with self._tx():
            self.conn.execute(
                """UPDATE run_requests SET status = 'failed', finished_at = ?,
                       note = 'no runner picked this up'
                   WHERE status = 'queued' AND requested_at < ?""",
                (now.isoformat(), stale_queue),
            )
            self.conn.execute(
                """UPDATE run_requests SET status = 'failed', finished_at = ?,
                       note = 'runner stopped responding'
                   WHERE status = 'running' AND id NOT IN (
                       SELECT request_id FROM publish_lease
                       WHERE request_id IS NOT NULL AND expires_at >= ?)""",
                (now.isoformat(), now.isoformat()),
            )


def _at_least_as_informative(job: Job, existing: Any) -> bool:
    """True when ``job`` does not lose information the stored row already has."""
    if len(job.description_text or "") < int(existing["description_len"] or 0):
        return False
    if job.workplace.value == "unknown" and (existing["workplace"] or "unknown") != "unknown":
        return False
    if job.salary is None and existing["salary_min"] is not None:
        return False
    return not (job.posted_at is None and existing["posted_at"])


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
