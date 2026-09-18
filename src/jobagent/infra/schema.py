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


MIGRATIONS.append(
    (
        4,
        """
        -- The web deployment. A search has an identity that outlives its row id (SQLite
        -- reuses ids), a revision that every edit bumps, and each run records which
        -- revision of which search it evaluated.
        ALTER TABLE searches ADD COLUMN uid TEXT NOT NULL DEFAULT '';
        UPDATE searches SET uid = lower(hex(randomblob(16))) WHERE uid = '';
        CREATE UNIQUE INDEX idx_searches_uid ON searches(uid);
        ALTER TABLE searches ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE runs ADD COLUMN search_uid TEXT;
        ALTER TABLE runs ADD COLUMN spec_revision INTEGER;
        ALTER TABLE runs ADD COLUMN spec_yaml TEXT NOT NULL DEFAULT '';
        ALTER TABLE runs ADD COLUMN request_id TEXT;
        CREATE INDEX idx_runs_request ON runs(request_id);

        -- What the run saw, frozen on the verdict: the fields a results screen lists
        -- and sorts by, and the hash of the text that was judged. A later run may
        -- rewrite the job row; the screen showing this run does not move.
        ALTER TABLE job_search_matches ADD COLUMN title TEXT NOT NULL DEFAULT '';
        ALTER TABLE job_search_matches ADD COLUMN company TEXT NOT NULL DEFAULT '';
        ALTER TABLE job_search_matches ADD COLUMN location_raw TEXT NOT NULL DEFAULT '';
        ALTER TABLE job_search_matches ADD COLUMN workplace TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE job_search_matches ADD COLUMN employment_type TEXT;
        ALTER TABLE job_search_matches ADD COLUMN salary_min REAL;
        ALTER TABLE job_search_matches ADD COLUMN salary_max REAL;
        ALTER TABLE job_search_matches ADD COLUMN salary_currency TEXT;
        ALTER TABLE job_search_matches ADD COLUMN salary_period TEXT;
        ALTER TABLE job_search_matches ADD COLUMN posted_at TEXT;
        ALTER TABLE job_search_matches ADD COLUMN url TEXT NOT NULL DEFAULT '';
        ALTER TABLE job_search_matches ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';

        -- One row per request to run the searches. Also the queue: the partial unique
        -- index means at most one request is ever waiting, whatever two browser tabs do.
        CREATE TABLE run_requests (
            id            TEXT PRIMARY KEY,
            priority_uid  TEXT,
            status        TEXT NOT NULL,
            origin        TEXT NOT NULL,
            lease_token   TEXT,
            requested_at  TEXT NOT NULL,
            started_at    TEXT,
            finished_at   TEXT,
            rows_written  INTEGER NOT NULL DEFAULT 0,
            note          TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX idx_one_queued ON run_requests(status) WHERE status = 'queued';

        -- The single publisher lease. Taking it overwrites the token, which is what makes
        -- a takeover invalidate the previous owner: its heartbeat, its guard and its
        -- finish all look for a token that is no longer there.
        CREATE TABLE publish_lease (
            singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
            token       TEXT,
            request_id  TEXT,
            expires_at  TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO publish_lease (singleton) VALUES (1);

        -- Written as the first step of every publish batch with the number of valid
        -- leases held by the writer's token. Zero violates the CHECK, and the whole
        -- batch rolls back. A lost lease cannot write; that is the entire table.
        CREATE TABLE publish_guard (ok INTEGER NOT NULL CHECK (ok = 1));
        """,
    )
)

SCHEMA_VERSION = MIGRATIONS[-1][0]
