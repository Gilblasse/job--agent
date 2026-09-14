"""The single point of network egress.

Every rule about how this tool touches other people's servers lives here: robots.txt,
per-host rate limiting, backoff, the user agent, and what "blocked" means. Source
adapters receive one of these and have no other way to reach the network, so none of them
can forget a rule or decide to route around one.
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..ports import FetchError, FetchResponse

USER_AGENT = (
    "jobagent/0.1 (+https://github.com/Gilblasse/job--agent; "
    "personal job search tool; contact via repository issues)"
)

# Deliberately modest. These are documented public JSON endpoints, but several platforms
# host thousands of tenants behind one hostname, so the limit is per HOST rather than per
# company -- the mistake that turns a fan-out into a self-inflicted rate-limit storm.
DEFAULT_MIN_INTERVAL = 0.25
DEFAULT_TIMEOUT = 20.0


class HostBlocked(FetchError):
    """This host has stopped serving us for the rest of this run."""


@dataclass
class _HostState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_allowed: float = 0.0
    blocked_reason: str = ""
    retry_waits_used: int = 0


class RobotsPolicy:
    """robots.txt lookups, cached per host.

    Follows RFC 9309 on unavailability: a 4xx for robots.txt means "no robots file",
    which permits access, while a 5xx means the server is unhealthy and is treated as a
    full disallow. This matters concretely -- one ATS returns 401 for its robots.txt
    while documenting the endpoint beneath it as a public read-only API.
    """

    def __init__(self, client: httpx.Client, enabled: bool = True):
        self._client = client
        self._enabled = enabled
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allows(self, url: str, user_agent: str = USER_AGENT) -> tuple[bool, str]:
        if not self._enabled:
            return True, "robots checking disabled"
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        with self._lock:
            if origin not in self._cache:
                self._cache[origin] = self._load(origin)
            parser = self._cache[origin]

        if parser is None:
            return True, "no robots.txt published"
        allowed = parser.can_fetch(user_agent, url)
        return allowed, "allowed by robots.txt" if allowed else "disallowed by robots.txt"

    def _load(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        parser = urllib.robotparser.RobotFileParser()
        try:
            response = self._client.get(f"{origin}/robots.txt", timeout=10.0)
        except httpx.HTTPError:
            return None  # unreachable robots is treated as absent, not as a prohibition
        if response.status_code >= 500:
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        if response.status_code >= 400:
            return None
        parser.parse(response.text.splitlines())
        return parser


class HttpFetcher:
    """A rate-limited, robots-respecting HTTP client.

    Thread-safe: the orchestrator fans out across hosts concurrently, while the per-host
    interval keeps any single server from seeing a burst.
    """

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        respect_robots: bool = True,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retry_waits: int = 1,
        client: httpx.Client | None = None,
    ):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_retry_waits = max_retry_waits
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent}
        )
        self.robots = RobotsPolicy(self._client, enabled=respect_robots)
        self._hosts: dict[str, _HostState] = {}
        self._hosts_lock = threading.Lock()
        self._requests = 0
        self._counter_lock = threading.Lock()

    @property
    def requests_made(self) -> int:
        return self._requests

    def blocked_hosts(self) -> dict[str, str]:
        return {host: state.blocked_reason for host, state in self._hosts.items()
                if state.blocked_reason}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ requests

    def get(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        return self._request("GET", url, params=params, headers=headers)

    def post_json(
        self, url: str, *, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> FetchResponse:
        return self._request("POST", url, json=payload, headers=headers)

    def _state(self, host: str) -> _HostState:
        with self._hosts_lock:
            return self._hosts.setdefault(host, _HostState())

    def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        host = urlsplit(url).netloc
        state = self._state(host)

        if state.blocked_reason:
            raise HostBlocked(f"{host} is blocked for this run: {state.blocked_reason}",
                              blocked=True)

        allowed, note = self.robots.allows(url, self.user_agent)
        if not allowed:
            state.blocked_reason = note
            raise HostBlocked(f"{host}: {note}", blocked=True)

        response = self._send(method, url, state, params=params, json=json, headers=headers)

        # 403 is a refusal, not a hiccup. Retrying it is how a polite client turns into
        # an impolite one, so the host is set aside for the rest of the run.
        if response.status_code == 403:
            state.blocked_reason = "host returned 403"
            raise HostBlocked(f"{host} returned 403", blocked=True, status=403)

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            may_wait = (
                state.retry_waits_used < self.max_retry_waits
                and retry_after is not None
                and retry_after <= 30
            )
            if may_wait:
                state.retry_waits_used += 1
                time.sleep(retry_after)
                response = self._send(
                    method, url, state, params=params, json=json, headers=headers
                )
                if response.status_code != 429:
                    return _to_response(url, response)
            # One considered wait, then stop. Per run only -- a shared host that
            # rate-limits us today must not be struck off permanently.
            state.blocked_reason = "host returned 429 (rate limited)"
            raise HostBlocked(f"{host} rate limited", blocked=True, status=429)

        return _to_response(url, response)

    def _send(
        self, method: str, url: str, state: _HostState, *,
        params: dict[str, Any] | None, json: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        with state.lock:
            now = time.monotonic()
            if now < state.next_allowed:
                time.sleep(state.next_allowed - now)
            state.next_allowed = time.monotonic() + self.min_interval

        with self._counter_lock:
            self._requests += 1

        merged = {"Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
        merged.update(headers or {})
        try:
            return self._client.request(method, url, params=params, json=json, headers=merged)
        except httpx.HTTPError as exc:
            # Network-level failure, including this environment's egress policy. Reported
            # as a source-level problem so one unreachable host cannot fail a whole run.
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; not worth honouring for a one-shot wait


def _to_response(url: str, response: httpx.Response) -> FetchResponse:
    return FetchResponse(
        url=str(response.url) or url,
        status=response.status_code,
        text=response.text,
        headers={k.lower(): v for k, v in response.headers.items()},
    )


def body_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]
