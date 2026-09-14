"""Test doubles for the ports.

The whole suite runs with no network. A ``FakeFetcher`` satisfies the ``Fetcher``
protocol and replays canned payloads, so the orchestrator, adapters and gates are
exercised exactly as they would be against a live host -- including the failure paths,
which are otherwise nearly impossible to trigger on demand.
"""

from __future__ import annotations

import json
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
