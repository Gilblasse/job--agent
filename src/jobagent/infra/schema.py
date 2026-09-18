"""Database schema, expressed as ordered migrations.

Migrations rather than a single CREATE script because the database lives in the user's
home directory and outlives any one version of this tool. A schema change must upgrade an
existing search history, never require deleting it.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE searches (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            spec_yaml   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE runs (
            id          INTEGER PRIMARY KEY,
            search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT NOT NULL DEFAULT 'running',
            found       INTEGER NOT NULL DEFAULT 0,
            matched     INTEGER NOT NULL DEFAULT 0,
            rejected    INTEGER NOT NULL DEFAULT 0,
            new_count   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_runs_search ON runs(search_id, started_at DESC);

        -- One row per source per run. This is what makes coverage reportable: a run that
        -- reached four of seven sources can say so instead of implying completeness.
        CREATE TABLE run_sources (
            id             INTEGER PRIMARY KEY,
            run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            source         TEXT NOT NULL,
            status         TEXT NOT NULL,
            found          INTEGER NOT NULL DEFAULT 0,
            requests       INTEGER NOT NULL DEFAULT 0,
            duration_ms    INTEGER NOT NULL DEFAULT 0,
            note           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_run_sources_run ON run_sources(run_id);

        -- The canonical record for an opportunity, after deduplication.
        CREATE TABLE jobs (
            id               INTEGER PRIMARY KEY,
            identity         TEXT NOT NULL UNIQUE,
            title            TEXT NOT NULL,
            company          TEXT NOT NULL,
            url              TEXT NOT NULL,
            description_text TEXT NOT NULL DEFAULT '',
            location_raw     TEXT NOT NULL DEFAULT '',
            city             TEXT,
            region           TEXT,
            country          TEXT,
            workplace        TEXT NOT NULL DEFAULT 'unknown',
            employment_type  TEXT,
            department       TEXT,
            salary_min       REAL,
            salary_max       REAL,
            salary_currency  TEXT,
            salary_period    TEXT,
            posted_at        TEXT,
            authority        INTEGER NOT NULL DEFAULT 1,
            first_seen       TEXT NOT NULL,
            last_seen        TEXT NOT NULL,
            verification     TEXT NOT NULL DEFAULT 'unverified',
            verified_at      TEXT,
            content_hash     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_jobs_company ON jobs(company);
        CREATE INDEX idx_jobs_last_seen ON jobs(last_seen DESC);

        -- Every URL a job was seen at. Deduplication collapses records; this keeps the
        -- provenance that collapsing would otherwise destroy.
        CREATE TABLE job_sources (
            id          INTEGER PRIMARY KEY,
            job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            source      TEXT NOT NULL,
            url         TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            authority   INTEGER NOT NULL DEFAULT 1,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            UNIQUE(job_id, url)
        );

        -- The engine's verdict for one job under one search, with the full explanation.
        CREATE TABLE job_search_matches (
            id          INTEGER PRIMARY KEY,
            job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
            run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            decision    TEXT NOT NULL,
            score       REAL NOT NULL DEFAULT 0,
            uncertain   INTEGER NOT NULL DEFAULT 0,
            explanation TEXT NOT NULL DEFAULT '{}',
            is_new      INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX idx_matches_search ON job_search_matches(search_id, run_id);
        CREATE INDEX idx_matches_job ON job_search_matches(job_id);

        -- Current user status per job, plus an append-only history of transitions.
        CREATE TABLE user_job_status (
            job_id     INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            status     TEXT NOT NULL,
            note       TEXT NOT NULL DEFAULT '',
            changed_at TEXT NOT NULL
        );

        CREATE TABLE user_job_status_history (
            id         INTEGER PRIMARY KEY,
            job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            status     TEXT NOT NULL,
            note       TEXT NOT NULL DEFAULT '',
            changed_at TEXT NOT NULL
        );

        -- The discovery surface. With no cross-company search available anywhere, this
        -- table IS the reach of the product, which is why it is seeded, grown and
        -- health-tracked rather than treated as a cache.
        CREATE TABLE company_registry (
            id                  INTEGER PRIMARY KEY,
            company             TEXT NOT NULL,
            domain              TEXT,
            ats                 TEXT NOT NULL,
            token               TEXT NOT NULL,
            board_url           TEXT NOT NULL DEFAULT '',
            source              TEXT NOT NULL DEFAULT 'seed',
            us_signal           INTEGER NOT NULL DEFAULT 0,
            first_seen          TEXT NOT NULL,
            last_verified       TEXT,
            last_success        TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_failure_kind   TEXT NOT NULL DEFAULT '',
            notes               TEXT NOT NULL DEFAULT '',
            UNIQUE(ats, token)
        );
        CREATE INDEX idx_registry_ats ON company_registry(ats);
        CREATE INDEX idx_registry_us ON company_registry(us_signal DESC, consecutive_failures ASC);

        """,
    ),
]

MIGRATIONS.append(
    (
        2,
        """
        -- Where a Common Crawl sweep got to, so it resumes instead of restarting.
        -- The index is a free public service run by a non-profit; re-reading pages we
        -- already read is exactly the load they ask callers not to create.
        CREATE TABLE discovery_progress (
            id            INTEGER PRIMARY KEY,
            backend       TEXT NOT NULL,
            crawl         TEXT NOT NULL,
            pattern       TEXT NOT NULL,
            next_page     INTEGER NOT NULL DEFAULT 0,
            total_pages   INTEGER,
            urls_seen     INTEGER NOT NULL DEFAULT 0,
            boards_found  INTEGER NOT NULL DEFAULT 0,
            boards_new    INTEGER NOT NULL DEFAULT 0,
            completed     INTEGER NOT NULL DEFAULT 0,
            last_note     TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL,
            UNIQUE(backend, crawl, pattern)
        );
        """,
    )
)

MIGRATIONS.append(
    (
        3,
        """
        -- Why the user turned a job away, kept with the rules it produced, so a search's
        -- exclusions stay explainable after the wizard's original answers are long gone.
        CREATE TABLE job_feedback (
            id          INTEGER PRIMARY KEY,
            job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            search_id   INTEGER REFERENCES searches(id) ON DELETE SET NULL,
            reason      TEXT NOT NULL,
            rules_json  TEXT NOT NULL DEFAULT '[]',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX idx_feedback_search ON job_feedback(search_id, created_at DESC);
        """,
    )
)

SCHEMA_VERSION = MIGRATIONS[-1][0]
