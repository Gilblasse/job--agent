"""Test doubles for the ports.

The whole suite runs with no network. A ``FakeFetcher`` satisfies the ``Fetcher``
protocol and replays canned payloads, so the orchestrator, adapters and gates are
exercised exactly as they would be against a live host -- including the failure paths,
which are otherwise nearly impossible to trigger on demand.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from jobagent.ports import FetchError, FetchResponse


@dataclass
class FakeFetcher:
    """Replays responses keyed by URL substring.

    Routes are matched by substring rather than exact URL so a test can stub "any
    Greenhouse board" without enumerating tenants.
    """

    routes: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, Exception] = field(default_factory=dict)
    default_status: int = 404
    calls: list[str] = field(default_factory=list)
    _requests: int = 0

    @property
    def requests_made(self) -> int:
        return self._requests

    def _match(self, url: str) -> tuple[str, Any] | None:
        for pattern, payload in self.routes.items():
            if pattern in url:
                return pattern, payload
        return None

    def _respond(self, url: str) -> FetchResponse:
        self.calls.append(url)
        self._requests += 1

        for pattern, error in self.failures.items():
            if pattern in url:
                raise error

        matched = self._match(url)
        if matched is None:
            return FetchResponse(url=url, status=self.default_status, text="")
        _, payload = matched
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return FetchResponse(url=url, status=200, text=text)

    def get(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        return self._respond(url)

    def post_json(
        self, url: str, *, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> FetchResponse:
        # Workday paginates by POST body, so the offset has to reach the route key.
        offset = payload.get("offset", 0) if isinstance(payload, dict) else 0
        return self._respond(f"{url}#offset={offset}")


@dataclass
class FixedClock:
    """A clock that does not move, so freshness assertions are stable."""

    moment: datetime = datetime(2026, 9, 14, 12, 0, 0)

    def now(self) -> datetime:
        return self.moment

    def today(self) -> date:
        return self.moment.date()


def blocked(host: str = "example.com", status: int = 429) -> FetchError:
    return FetchError(f"{host} rate limited", blocked=True, status=status)


def unavailable(message: str = "ConnectError: refused") -> FetchError:
    return FetchError(message)


class FakeHrana:
    """An in-process Hrana v3 HTTP server over sqlite3, for tests of ``infra/turso.py``.

    It implements the parts of the protocol the client relies on, as the specification
    states them:

    * A batch is "a list of steps (statements) which are always executed sequentially".
      A step's ``condition`` may be ``ok``, ``error``, ``not``, ``and``, ``or`` or
      ``is_autocommit``; ``ok`` "evaluates true if the referenced step executed
      successfully". When a condition is false, that step's result and error are both
      null. "If a statement fails, the error is returned inside the BatchResult
      structure" -- and later steps still run unless their own condition stops them.
    * "The server returns a baton in every response to a request on the stream, and the
      client then needs to include the baton in the subsequent request." A ``close``
      request ends the stream. Batons are rotated on every response so a client that
      reuses a stale one is caught.

    Each stream is its own sqlite3 connection in autocommit mode, exactly as each server
    stream is its own connection. ``fail_on`` injects a failure into any statement whose
    SQL contains that text, which is how a publish is made to die halfway.
    """

    def __init__(self, path: str):
        import threading

        self.path = path
        self._streams: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.fail_on: str | None = None
        self.requests: list[dict[str, Any]] = []

    # -- transport --------------------------------------------------------------

    def transport(self):
        import httpx

        return httpx.MockTransport(self.handle)

    def handle(self, request):
        with self._lock:
            return self._handle(request)

    def _handle(self, request):
        import httpx

        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        baton = body.get("baton")
        if baton is None:
            conn = self._open()
        elif baton in self._streams:
            conn = self._streams.pop(baton)
        else:
            return httpx.Response(400, json={"error": "unknown baton"})

        results = []
        closed = False
        for item in body.get("requests", []):
            kind = item.get("type")
            if kind == "close":
                closed = True
                results.append({"type": "ok", "response": {"type": "close"}})
            elif kind == "execute":
                try:
                    payload = self._run(conn, item["stmt"])
                    results.append(
                        {"type": "ok", "response": {"type": "execute", "result": payload}}
                    )
                except Exception as error:  # noqa: BLE001 - reported as the server would
                    results.append({"type": "error", "error": {"message": str(error)}})
            elif kind == "batch":
                results.append(
                    {"type": "ok", "response": {"type": "batch", "result": self._batch(conn, item)}}
                )
            else:
                results.append({"type": "error", "error": {"message": f"unknown request {kind}"}})

        if closed:
            conn.close()
            new_baton = None
        else:
            new_baton = uuid.uuid4().hex
            self._streams[new_baton] = conn
        return httpx.Response(200, json={"baton": new_baton, "base_url": None, "results": results})

    # -- execution --------------------------------------------------------------

    def _open(self):
        import sqlite3

        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        return conn

    def _run(self, conn, stmt: dict[str, Any]) -> dict[str, Any]:
        from jobagent.infra.turso import decode, encode

        sql = stmt["sql"]
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"injected failure: {self.fail_on}")
        args = [decode(value) for value in stmt.get("args", [])]
        before = conn.total_changes
        cursor = conn.execute(sql, args)
        rows = cursor.fetchall()
        cols = [{"name": column[0], "decltype": None} for column in (cursor.description or [])]
        return {
            "cols": cols,
            "rows": [[encode(value) for value in row] for row in rows],
            "affected_row_count": max(cursor.rowcount, 0),
            "last_insert_rowid": str(cursor.lastrowid) if cursor.lastrowid else None,
            "rows_read": 0,
            "rows_written": conn.total_changes - before,
            "query_duration_ms": 0.0,
        }

    def _batch(self, conn, item: dict[str, Any]) -> dict[str, Any]:
        outcomes: list[bool | None] = []
        step_results: list[Any] = []
        step_errors: list[Any] = []
        for step in item["batch"]["steps"]:
            condition = step.get("condition")
            if condition is not None and not self._holds(condition, outcomes, conn):
                outcomes.append(None)
                step_results.append(None)
                step_errors.append(None)
                continue
            try:
                step_results.append(self._run(conn, step["stmt"]))
                step_errors.append(None)
                outcomes.append(True)
            except Exception as error:  # noqa: BLE001 - reported as the server would
                step_results.append(None)
                step_errors.append({"message": str(error)})
                outcomes.append(False)
        return {"step_results": step_results, "step_errors": step_errors}

    def _holds(self, condition: dict[str, Any], outcomes: list[bool | None], conn) -> bool:
        kind = condition["type"]
        if kind == "ok":
            return outcomes[condition["step"]] is True
        if kind == "error":
            return outcomes[condition["step"]] is False
        if kind == "not":
            return not self._holds(condition["cond"], outcomes, conn)
        if kind == "and":
            return all(self._holds(c, outcomes, conn) for c in condition["conds"])
        if kind == "or":
            return any(self._holds(c, outcomes, conn) for c in condition["conds"])
        if kind == "is_autocommit":
            return not conn.in_transaction
        raise ValueError(f"unknown condition {kind}")
