"""The cloud database, reached over HTTP.

Turso speaks Hrana over HTTP: one ``POST /v2/pipeline`` carries a list of requests and
returns a list of results. This module wraps that in the slice of ``sqlite3.Connection``
that ``Store`` uses -- ``execute``, ``executescript``, ``lastrowid``, ``rowcount``, rows
addressable by column name -- plus two things a plain connection does not need to think
about and a remote one must:

* ``transaction()``: an interactive transaction spanning several requests, held together
  by the server's ``baton``. Turso closes a transaction after five seconds, so this is
  for the API's short write groups, never for bulk.
* ``batch(stmts)``: many statements in ONE request, executed as ONE transaction. Hrana
  keeps executing after a failed statement unless told otherwise, so every step here is
  conditioned on the previous step having succeeded, ``COMMIT`` on the last one, and
  ``ROLLBACK`` on its negation. A failure anywhere leaves nothing of the batch behind.

This is not a job source. The robots and rate-limit policy in ``infra/http.py`` governs
how the tool reads other people's servers; this talks to the user's own database.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

PIPELINE = "/v2/pipeline"
# Turso caps a transaction at five seconds; five hundred small statements execute
# server-side in well under one. Raised only with a measurement to justify it.
MAX_BATCH = 500

Statement = tuple[str, Sequence[Any]]


class TursoError(Exception):
    """A statement the server refused, with the step and SQL that failed."""

    def __init__(self, message: str, *, step: int | None = None, sql: str = ""):
        super().__init__(message)
        self.step = step
        self.sql = sql


@dataclass
class Result:
    """One statement's outcome, shaped like a ``sqlite3.Cursor`` that has already run."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    rowcount: int = 0
    lastrowid: int | None = None
    rows_read: int = 0
    rows_written: int = 0

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)


@dataclass
class BatchResult:
    results: list[Result]
    rows_written: int


def encode(value: Any) -> dict[str, Any]:
    """A Python value as a Hrana value. Integers travel as strings to keep 64 bits."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    return {"type": "text", "value": str(value)}


def decode(value: dict[str, Any]) -> Any:
    kind = value.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    if kind == "blob":
        return base64.b64decode(value["base64"])
    return value.get("value")


def split_statements(script: str) -> list[str]:
    """The statements of a migration script, comments dropped.

    The scripts in ``schema.py`` are plain DDL with ``--`` comments and no string literal
    containing a semicolon, so splitting on ``;`` is exact for them.
    """
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def _stmt(sql: str, params: Sequence[Any] = ()) -> dict[str, Any]:
    return {"sql": sql, "args": [encode(p) for p in params], "want_rows": True}


def _pragma() -> dict[str, Any]:
    # Foreign keys are a per-connection setting in SQLite, and every stream is a fresh
    # connection. Without this a search delete would leave its verdicts behind.
    return {"type": "execute", "stmt": _stmt("PRAGMA foreign_keys = ON")}


def _to_result(payload: dict[str, Any]) -> Result:
    names = [col.get("name") or f"column{i}" for i, col in enumerate(payload.get("cols", []))]
    rows = [
        {name: decode(cell) for name, cell in zip(names, row, strict=False)}
        for row in payload.get("rows", [])
    ]
    last = payload.get("last_insert_rowid")
    return Result(
        rows=rows,
        rowcount=int(payload.get("affected_row_count") or 0),
        lastrowid=int(last) if last is not None else None,
        rows_read=int(payload.get("rows_read") or 0),
        rows_written=int(payload.get("rows_written") or 0),
    )


class TursoConnection:
    """One database, one thread. The heartbeat thread opens its own."""

    def __init__(
        self, url: str, token: str, *, client: httpx.Client | None = None,
        timeout: float = 60.0,
    ):
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._baton: str | None = None
        self._base_url: str | None = None
        self._depth = 0
        self._failed = False

    # ------------------------------------------------------------------ plumbing

    def _pipeline(
        self, requests: list[dict[str, Any]], *, on_stream: bool = False, close: bool = True
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"requests": [*requests, *([{"type": "close"}] if close else [])]}
        if on_stream and self._baton:
            body["baton"] = self._baton
        base = (self._base_url if on_stream and self._base_url else self.url).rstrip("/")
        response = self._client.post(base + PIPELINE, json=body, headers=self._headers)
        if response.status_code >= 400:
            raise TursoError(f"HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if on_stream:
            self._baton = payload.get("baton")
            self._base_url = payload.get("base_url") or self._base_url
        return payload.get("results", [])

    @staticmethod
    def _unwrap(entry: dict[str, Any], sql: str = "") -> dict[str, Any]:
        if entry.get("type") == "error":
            message = (entry.get("error") or {}).get("message", "unknown error")
            raise TursoError(message, sql=sql)
        return entry.get("response", {})

    # ------------------------------------------------------------------ statements

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        request = {"type": "execute", "stmt": _stmt(sql, params)}
        if self._depth > 0:
            if self._baton is None:
                raise TursoError("the transaction's stream was closed by the server", sql=sql)
            try:
                entries = self._pipeline([request], on_stream=True, close=False)
                return _to_result(self._unwrap(entries[0], sql).get("result", {}))
            except TursoError:
                self._failed = True
                raise
        # Autocommit: a fresh stream, foreign keys on, the statement, close.
        entries = self._pipeline([_pragma(), request])
        self._unwrap(entries[0], "PRAGMA foreign_keys = ON")
        return _to_result(self._unwrap(entries[1], sql).get("result", {}))

    def batch(self, stmts: Sequence[Statement]) -> BatchResult:
        """Execute ``stmts`` as one atomic transaction in one request."""
        if self._depth > 0:
            raise TursoError("batch() cannot run inside an interactive transaction")
        if len(stmts) > MAX_BATCH:
            raise TursoError(f"a batch holds at most {MAX_BATCH} statements, got {len(stmts)}")
        if not stmts:
            return BatchResult(results=[], rows_written=0)

        steps: list[dict[str, Any]] = [{"stmt": _stmt("BEGIN")}]
        for index, (sql, params) in enumerate(stmts):
            steps.append({"condition": {"type": "ok", "step": index}, "stmt": _stmt(sql, params)})
        last = len(stmts)  # the step index of the final user statement
        steps.append({"condition": {"type": "ok", "step": last}, "stmt": _stmt("COMMIT")})
        steps.append({
            "condition": {"type": "not", "cond": {"type": "ok", "step": last}},
            "stmt": _stmt("ROLLBACK"),
        })

        entries = self._pipeline([_pragma(), {"type": "batch", "batch": {"steps": steps}}])
        self._unwrap(entries[0], "PRAGMA foreign_keys = ON")
        outcome = self._unwrap(entries[1]).get("result", {})
        step_results = outcome.get("step_results") or []
        step_errors = outcome.get("step_errors") or []

        def error_at(step: int) -> dict[str, Any] | None:
            return step_errors[step] if step < len(step_errors) else None

        if error_at(0):
            raise TursoError(f"BEGIN failed: {error_at(0)['message']}", step=0, sql="BEGIN")
        for index, (sql, _params) in enumerate(stmts):
            failure = error_at(index + 1)
            if failure:
                raise TursoError(
                    f"statement {index} failed: {failure.get('message')}", step=index, sql=sql
                )
        commit_failure = error_at(last + 1)
        if commit_failure:
            raise TursoError(f"COMMIT failed: {commit_failure.get('message')}", sql="COMMIT")

        results = [
            _to_result((step_results[i + 1] or {}) if i + 1 < len(step_results) else {})
            for i in range(len(stmts))
        ]
        return BatchResult(results=results, rows_written=sum(r.rows_written for r in results))

    def executescript(self, script: str) -> None:
        self.batch([(sql, ()) for sql in split_statements(script)])

    # ------------------------------------------------------------------ transactions

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """``BEGIN`` on entry, ``COMMIT`` on exit, ``ROLLBACK`` on an exception.

        Depth-counted, so a Store method's own block inside an API handler's block does
        not commit early. Everything inside runs on one server stream via the baton.
        """
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return

        self._baton = None
        self._base_url = None
        self._failed = False
        entries = self._pipeline(
            [_pragma(), {"type": "execute", "stmt": _stmt("BEGIN")}], on_stream=True, close=False
        )
        self._unwrap(entries[0], "PRAGMA foreign_keys = ON")
        self._unwrap(entries[1], "BEGIN")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._end("ROLLBACK")
            raise
        else:
            self._end("COMMIT" if not self._failed else "ROLLBACK")

    def _end(self, verb: str) -> None:
        self._depth = 0
        if self._baton is None:
            return  # the server already closed the stream; nothing was kept
        try:
            entries = self._pipeline(
                [{"type": "execute", "stmt": _stmt(verb)}], on_stream=True, close=True
            )
            if verb == "COMMIT":
                self._unwrap(entries[0], verb)
        finally:
            self._baton = None
            self._base_url = None

    def __enter__(self) -> TursoConnection:
        self._cm = self.transaction()
        self._cm.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._cm.__exit__(*exc)

    def commit(self) -> None:
        """Statements outside a transaction are already committed."""

    def close(self) -> None:
        self._client.close()
