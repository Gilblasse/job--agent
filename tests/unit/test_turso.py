"""The cloud connection, driven through an in-process Hrana server over sqlite3.

What matters here is not that JSON round-trips but that the transactional promises the
publisher relies on actually hold: a batch that fails halfway leaves nothing behind, a
guard row that fails its CHECK rolls the whole chunk back, and an interactive transaction
commits once at the outermost level or not at all.
"""

from __future__ import annotations

import httpx
import pytest

from jobagent.infra.turso import (
    MAX_BATCH,
    TursoConnection,
    TursoError,
    decode,
    encode,
    split_statements,
)
from tests.fakes import FakeHrana


@pytest.fixture
def fake(tmp_path) -> FakeHrana:
    return FakeHrana(str(tmp_path / "cloud.sqlite3"))


@pytest.fixture
def conn(fake) -> TursoConnection:
    return TursoConnection(
        "https://db.example.turso.io", "token", client=httpx.Client(transport=fake.transport())
    )


@pytest.fixture
def table(conn) -> TursoConnection:
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
    return conn


class TestCodec:
    @pytest.mark.parametrize(
        "value",
        [None, 0, 1, -7, 2**62, 1.5, "text", "", b"\x00\xff", True, False],
    )
    def test_round_trip(self, value):
        decoded = decode(encode(value))
        if isinstance(value, bool):
            assert decoded == int(value)  # SQLite has no boolean; the integer is the value
        else:
            assert decoded == value

    def test_integers_travel_as_strings(self):
        assert encode(2**62) == {"type": "integer", "value": str(2**62)}

    def test_blobs_are_base64(self):
        assert encode(b"hi")["base64"] == "aGk="

    def test_scripts_split_on_semicolons_and_drop_comments(self):
        script = """
        -- a comment; with a semicolon
        CREATE TABLE a (x);
        CREATE INDEX ix ON a(x);
        """
        assert split_statements(script) == ["CREATE TABLE a (x)", "CREATE INDEX ix ON a(x)"]


class TestExecute:
    def test_rows_are_dicts_with_rowcount_and_lastrowid(self, table):
        inserted = table.execute("INSERT INTO t (v) VALUES (?)", ("a",))
        assert inserted.rowcount == 1
        assert inserted.lastrowid == 1
        rows = table.execute("SELECT id, v FROM t").fetchall()
        assert rows == [{"id": 1, "v": "a"}]
        assert rows[0]["v"] == "a" and list(rows[0].keys()) == ["id", "v"]

    def test_every_stream_turns_foreign_keys_on(self, fake, conn):
        conn.execute("SELECT 1")
        first = fake.requests[-1]["requests"][0]
        assert first["stmt"]["sql"] == "PRAGMA foreign_keys = ON"

    def test_a_refused_statement_raises_with_its_sql(self, table):
        with pytest.raises(TursoError) as error:
            table.execute("INSERT INTO nope VALUES (1)")
        assert "nope" in error.value.sql

    def test_http_errors_surface(self, fake):
        def deny(request):
            return httpx.Response(401, text="bad token")

        client = httpx.Client(transport=httpx.MockTransport(deny))
        conn = TursoConnection("https://x", "t", client=client)
        with pytest.raises(TursoError, match="401"):
            conn.execute("SELECT 1")


class TestBatch:
    def test_a_batch_is_one_request_wrapped_in_a_transaction(self, fake, table):
        result = table.batch([
            ("INSERT INTO t (v) VALUES (?)", ("a",)), ("INSERT INTO t (v) VALUES (?)", ("b",)),
        ])
        assert result.rows_written == 2
        body = fake.requests[-1]["requests"]
        assert body[0]["stmt"]["sql"] == "PRAGMA foreign_keys = ON"
        steps = body[1]["batch"]["steps"]
        sqls = [step["stmt"]["sql"] for step in steps]
        assert sqls[0] == "BEGIN" and sqls[-2] == "COMMIT" and sqls[-1] == "ROLLBACK"
        # Every user statement is conditioned on the previous step; COMMIT on the last
        # user statement; ROLLBACK on its negation.
        assert steps[1]["condition"] == {"type": "ok", "step": 0}
        assert steps[2]["condition"] == {"type": "ok", "step": 1}
        assert steps[-2]["condition"] == {"type": "ok", "step": 2}
        assert steps[-1]["condition"] == {"type": "not", "cond": {"type": "ok", "step": 2}}

    def test_a_failure_at_step_k_persists_nothing_from_earlier_steps(self, table):
        stmts = [
            ("INSERT INTO t (v) VALUES (?)", ("a",)),
            ("INSERT INTO t (v) VALUES (?)", ("b",)),
            ("INSERT INTO t (v) VALUES (?)", ("a",)),  # UNIQUE violation
            ("INSERT INTO t (v) VALUES (?)", ("c",)),
        ]
        with pytest.raises(TursoError) as error:
            table.batch(stmts)
        assert error.value.step == 2
        assert "VALUES" in error.value.sql
        assert table.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 0

    def test_a_guard_row_that_fails_its_check_rolls_the_chunk_back(self, table):
        table.execute("CREATE TABLE guard (ok INTEGER NOT NULL CHECK (ok = 1))")
        with pytest.raises(TursoError) as error:
            table.batch([
                ("INSERT INTO guard (ok) SELECT 0", ()),
                ("INSERT INTO t (v) VALUES (?)", ("a",)),
            ])
        assert error.value.step == 0 and "guard" in error.value.sql
        assert table.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 0

    def test_results_come_back_per_statement(self, table):
        result = table.batch([
            ("INSERT INTO t (v) VALUES (?)", ("a",)),
            ("SELECT v FROM t", ()),
        ])
        assert result.results[0].lastrowid == 1
        assert result.results[1].fetchall() == [{"v": "a"}]

    def test_an_oversized_batch_is_refused_before_it_is_sent(self, fake, table):
        sent = len(fake.requests)
        with pytest.raises(TursoError, match="at most"):
            table.batch([("SELECT 1", ())] * (MAX_BATCH + 1))
        assert len(fake.requests) == sent

    def test_executescript_is_one_atomic_batch(self, fake, conn):
        conn.executescript("CREATE TABLE a (x);\n-- note\nCREATE TABLE b (y);")
        assert fake.requests[-1]["requests"][1]["type"] == "batch"
        tables = conn.execute("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'")
        assert tables.fetchone()["n"] == 2


class TestTransaction:
    def test_commits_on_exit(self, table):
        with table.transaction():
            table.execute("INSERT INTO t (v) VALUES (?)", ("a",))
            table.execute("INSERT INTO t (v) VALUES (?)", ("b",))
        assert table.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 2

    def test_rolls_back_on_an_exception(self, table):
        with pytest.raises(RuntimeError), table.transaction():
            table.execute("INSERT INTO t (v) VALUES (?)", ("a",))
            raise RuntimeError("boom")
        assert table.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 0

    def test_a_failed_statement_inside_rolls_back_the_whole_block(self, table):
        with pytest.raises(TursoError), table.transaction():
            table.execute("INSERT INTO t (v) VALUES (?)", ("a",))
            table.execute("INSERT INTO t (v) VALUES (?)", ("a",))  # UNIQUE violation
        assert table.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 0

    def test_nested_blocks_commit_once_at_the_outermost_level(self, table):
        with pytest.raises(RuntimeError), table.transaction():
            with table.transaction():
                table.execute("INSERT INTO t (v) VALUES (?)", ("inner",))
            # The inner block has exited; without depth counting this would be committed.
            raise RuntimeError("outer fails")
        assert table.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 0

    def test_statements_inside_share_one_stream(self, fake, table):
        with table.transaction():
            table.execute("INSERT INTO t (v) VALUES (?)", ("a",))
            batons = [body.get("baton") for body in fake.requests[-2:]]
        # BEGIN opened the stream (no baton); the insert carried the baton it returned.
        assert batons[0] is None and batons[1] is not None

    def test_reads_see_the_blocks_own_writes(self, table):
        with table.transaction():
            table.execute("INSERT INTO t (v) VALUES (?)", ("a",))
            assert table.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"] == 1

    def test_batch_is_refused_inside_a_transaction(self, table):
        with pytest.raises(TursoError, match="inside"), table.transaction():
            table.batch([("SELECT 1", ())])
