"""The egress chokepoint: robots, rate limiting, and what "blocked" means.

This is the code that keeps the tool on the right side of other people's servers, and it
had no tests at all -- while the README claimed the policy was tested. `HttpFetcher`
takes an injectable client, so every branch is reachable offline.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from jobagent.infra.http import HttpFetcher, RobotsPolicy, _parse_retry_after
from jobagent.ports import FetchError


class StubClient:
    """Stands in for httpx.Client, returning scripted responses per URL substring."""

    def __init__(self, routes: dict[str, tuple[int, str, dict[str, str]]] | None = None,
                 raise_for: str | None = None):
        self.routes = routes or {}
        self.raise_for = raise_for
        self.calls: list[tuple[str, str, float]] = []

    def _response(self, method: str, url: str) -> httpx.Response:
        self.calls.append((method, url, time.monotonic()))
        if self.raise_for and self.raise_for in url:
            raise httpx.ConnectError("refused")
        for pattern, (status, text, headers) in self.routes.items():
            if pattern in url:
                return httpx.Response(
                    status_code=status, text=text, headers=headers,
                    request=httpx.Request(method, url),
                )
        return httpx.Response(status_code=404, text="", request=httpx.Request(method, url))

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self._response("GET", url)

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return self._response(method, url)

    def close(self) -> None:
        pass


def fetcher_with(routes, *, robots=True, interval=0.0, raise_for=None) -> HttpFetcher:
    return HttpFetcher(
        respect_robots=robots, min_interval=interval,
        client=StubClient(routes, raise_for=raise_for),
    )


ALLOW_ALL = {"/robots.txt": (200, "User-agent: *\nAllow: /", {})}


class TestRobotsPolicy:
    """RFC 9309 on unavailability, which is not a detail here.

    One ATS answers 401 for its robots.txt while documenting the endpoint beneath it as a
    public read-only API. Treating that 401 as a prohibition would remove a source the
    vendor publishes deliberately.
    """

    def test_a_disallow_blocks_the_request(self):
        fetcher = fetcher_with({
            "/robots.txt": (200, "User-agent: *\nDisallow: /api/", {}),
            "/api/jobs": (200, "{}", {}),
        })
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api/jobs")
        assert caught.value.blocked
        assert "robots" in str(caught.value)

    def test_an_allowed_path_proceeds(self):
        fetcher = fetcher_with({
            "/robots.txt": (200, "User-agent: *\nDisallow: /private/", {}),
            "/api/jobs": (200, '{"jobs":[]}', {}),
        })
        assert fetcher.get("https://example.com/api/jobs").ok

    @pytest.mark.parametrize("status", [401, 403, 404, 410])
    def test_a_4xx_robots_means_no_file_and_permits_access(self, status):
        fetcher = fetcher_with({
            "/robots.txt": (status, "", {}),
            "/api/jobs": (200, '{"jobs":[]}', {}),
        })
        assert fetcher.get("https://example.com/api/jobs").ok

    @pytest.mark.parametrize("status", [500, 503])
    def test_a_5xx_robots_is_treated_as_a_full_disallow(self, status):
        """An unhealthy server is not an invitation to crawl it."""
        fetcher = fetcher_with({
            "/robots.txt": (status, "", {}),
            "/api/jobs": (200, '{"jobs":[]}', {}),
        })
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api/jobs")
        assert caught.value.blocked

    def test_an_unreadable_robots_fails_closed(self):
        """Not being able to read the policy is not the same as there being no policy.

        This previously cached a transport failure as "no robots.txt" and permitted the
        request, so a transient network error let the tool crawl a source whose rules it
        had never seen.
        """
        policy = RobotsPolicy(StubClient(raise_for="robots"), enabled=True)
        allowed, note = policy.allows("https://example.com/api/jobs")
        assert not allowed
        assert "could not be fetched" in note

    def test_the_transport_error_is_named_so_the_user_can_act_on_it(self):
        """"robots.txt could not be fetched" alone points the blame at the host.

        A host refusing us is the host's decision; a proxy or a dead connection is the
        user's own network and fixable. Reporting both identically sent people to
        complain to the wrong party.
        """
        policy = RobotsPolicy(StubClient(raise_for="robots"), enabled=True)
        _, note = policy.allows("https://example.com/api/jobs")
        assert "refused" in note

    def test_naming_the_cause_does_not_reopen_the_door(self):
        # The diagnosis changed; the posture must not. Still closed.
        policy = RobotsPolicy(StubClient(raise_for="robots"), enabled=True)
        allowed, _ = policy.allows("https://example.com/api/jobs")
        assert allowed is False

    def test_an_error_with_no_message_still_names_its_kind(self):
        class Silent(StubClient):
            def _response(self, method, url):
                if "robots" in url:
                    raise httpx.ConnectTimeout("")
                return super()._response(method, url)

        policy = RobotsPolicy(Silent(), enabled=True)
        allowed, note = policy.allows("https://example.com/api/jobs")
        assert not allowed
        assert "ConnectTimeout" in note

    def test_a_published_empty_robots_still_permits(self):
        """Distinguished from the above: a host that publishes no rules allows access."""
        policy = RobotsPolicy(StubClient({"/robots.txt": (404, "", {})}), enabled=True)
        allowed, note = policy.allows("https://example.com/api/jobs")
        assert allowed and "no robots.txt" in note

    def test_robots_is_fetched_once_per_host(self):
        client = StubClient({**ALLOW_ALL, "/api": (200, "{}", {})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        for _ in range(3):
            fetcher.get("https://example.com/api/jobs")
        assert sum(1 for _, url, _ in client.calls if "robots" in url) == 1

    def test_disabling_the_check_skips_it_entirely(self):
        client = StubClient({"/api": (200, "{}", {})})
        HttpFetcher(respect_robots=False, min_interval=0.0, client=client).get(
            "https://example.com/api"
        )
        assert not any("robots" in url for _, url, _ in client.calls)


class TestBlockedSemantics:
    """403 stops; 429 earns one considered wait; both are per-run, never permanent."""

    def test_a_403_blocks_the_host_without_retrying(self):
        client = StubClient({**ALLOW_ALL, "/api": (403, "", {})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api")
        assert caught.value.blocked and caught.value.status == 403
        assert sum(1 for _, url, _ in client.calls if "/api" in url) == 1

    def test_a_blocked_host_refuses_subsequent_requests_without_a_call(self):
        client = StubClient({**ALLOW_ALL, "/api": (403, "", {})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError):
            fetcher.get("https://example.com/api")
        before = len(client.calls)
        with pytest.raises(FetchError):
            fetcher.get("https://example.com/api/other")
        assert len(client.calls) == before, "a blocked host was contacted again"

    def test_a_429_with_a_short_retry_after_is_waited_out_once(self):
        client = StubClient({**ALLOW_ALL, "/api": (429, "", {"retry-after": "0"})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api")
        assert caught.value.status == 429
        # One wait, then it stops: the original attempt plus exactly one retry.
        assert sum(1 for _, url, _ in client.calls if "/api" in url) == 2

    def test_a_429_without_retry_after_does_not_retry(self):
        client = StubClient({**ALLOW_ALL, "/api": (429, "", {})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError):
            fetcher.get("https://example.com/api")
        assert sum(1 for _, url, _ in client.calls if "/api" in url) == 1

    def test_a_long_retry_after_is_not_waited_out(self):
        """Sleeping for minutes inside a run is worse than reporting the source blocked."""
        client = StubClient({**ALLOW_ALL, "/api": (429, "", {"retry-after": "600"})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError):
            fetcher.get("https://example.com/api")
        assert sum(1 for _, url, _ in client.calls if "/api" in url) == 1

    def test_blocking_one_host_does_not_block_another(self):
        client = StubClient({
            **ALLOW_ALL, "blocked.example": (403, "", {}), "ok.example": (200, "{}", {}),
        })
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError):
            fetcher.get("https://blocked.example/api")
        assert fetcher.get("https://ok.example/api").ok
        assert list(fetcher.blocked_hosts()) == ["blocked.example"]

    @pytest.mark.parametrize(
        "value,expected", [("0", 0.0), ("12", 12.0), (None, None), ("", None),
                           ("Wed, 21 Oct 2026 07:28:00 GMT", None)],
    )
    def test_retry_after_parsing(self, value, expected):
        assert _parse_retry_after(value) == expected


class TestRateLimiting:
    def test_requests_to_one_host_are_spaced(self):
        client = StubClient({**ALLOW_ALL, "/api": (200, "{}", {})})
        fetcher = HttpFetcher(min_interval=0.05, client=client)
        start = time.monotonic()
        for _ in range(3):
            fetcher.get("https://example.com/api")
        assert time.monotonic() - start >= 0.10

    def test_the_limit_is_per_host_not_global(self):
        """Several platforms host thousands of tenants behind one name, so the limit must
        follow the host -- but two different hosts must not queue behind each other.

        Robots checking is off here so the measurement is of the limiter alone; with it
        on, each host also pays for its one robots fetch.
        """
        client = StubClient({"/api": (200, "{}", {})})
        fetcher = HttpFetcher(respect_robots=False, min_interval=0.05, client=client)
        start = time.monotonic()
        fetcher.get("https://a.example/api")
        fetcher.get("https://b.example/api")
        fetcher.get("https://c.example/api")
        assert time.monotonic() - start < 0.10

    def test_concurrent_callers_share_the_host_limit(self):
        client = StubClient({**ALLOW_ALL, "/api": (200, "{}", {})})
        fetcher = HttpFetcher(min_interval=0.05, client=client)

        def hit() -> None:
            fetcher.get("https://example.com/api")

        start = time.monotonic()
        threads = [threading.Thread(target=hit) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert time.monotonic() - start >= 0.15

    def test_every_request_is_counted_including_the_robots_fetch(self):
        """robots.txt is a request to someone else's server and is counted as one.

        It is also rate limited: fetching it outside the limiter was a small hole in the
        one guarantee this module exists to make.
        """
        fetcher = fetcher_with({**ALLOW_ALL, "/api": (200, "{}", {})})
        for _ in range(3):
            fetcher.get("https://example.com/api")
        assert fetcher.requests_made == 4  # three API calls plus one robots.txt

    def test_the_robots_fetch_is_itself_paced(self):
        client = StubClient({**ALLOW_ALL, "/api": (200, "{}", {})})
        fetcher = HttpFetcher(min_interval=0.05, client=client)
        start = time.monotonic()
        fetcher.get("https://example.com/api")
        assert time.monotonic() - start >= 0.05


class TestTransportFailures:
    def test_a_connection_error_becomes_a_non_blocking_fetch_error(self):
        """This is the path the egress policy takes in a restricted environment."""
        fetcher = fetcher_with(ALLOW_ALL, raise_for="/api")
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api")
        assert not caught.value.blocked
        assert "ConnectError" in str(caught.value)


class TestAdaptersCannotBypassTheChokepoint:
    def test_no_adapter_imports_an_http_library(self):
        """The structural half of the access guarantee.

        Every network rule lives in infra/http.py. An adapter reaching for httpx directly
        would silently opt out of robots, rate limiting and the blocked-host logic.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "src" / "jobagent"
        offenders = [
            path.relative_to(root)
            for path in root.rglob("*.py")
            if path.name != "http.py"
            and any(
                line.startswith(("import httpx", "import requests", "import urllib.request"))
                or line.startswith(("from httpx", "from requests", "from urllib.request"))
                for line in (ln.strip() for ln in path.read_text().splitlines())
            )
        ]
        assert not offenders, f"modules bypassing the fetcher: {offenders}"


class TestRedirectsAreRechecked:
    def test_a_redirect_into_a_disallowed_path_is_blocked(self):
        """Checking only the requested URL let a redirect walk past robots."""
        client = StubClient({
            "/robots.txt": (200, "User-agent: *\nDisallow: /private/", {}),
            "/api/jobs": (200, "{}", {}),
        })

        original = client._response

        def redirecting(method: str, url: str):
            response = original(method, url)
            if "/api/jobs" in url:
                response._request = httpx.Request(method, "https://example.com/private/x")
                response.request = httpx.Request(method, "https://example.com/private/x")
            return response

        client._response = redirecting
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api/jobs")
        assert caught.value.blocked
        assert "disallowed" in str(caught.value)


class TestTheRetryGetsTheSameChecks:
    """Regression: the 429 retry returned early, skipping redirect and 403 handling.

    A 429 followed by a redirect into a disallowed path, or by a 403, came back as an
    ordinary response with none of the protections the first attempt got.
    """

    def test_a_429_then_403_is_still_blocked(self):
        statuses = iter([429, 403])
        client = StubClient({**ALLOW_ALL, "/api": (429, "", {"retry-after": "0"})})
        original = client._response

        def sequenced(method: str, url: str):
            response = original(method, url)
            if "/api" in url:
                response.status_code = next(statuses, 403)
            return response

        client._response = sequenced
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api")
        assert caught.value.blocked
        assert caught.value.status == 403

    def test_a_429_then_a_disallowed_redirect_is_still_blocked(self):
        statuses = iter([429, 200])
        client = StubClient({
            "/robots.txt": (200, "User-agent: *\nDisallow: /private/", {}),
            "/api": (429, "", {"retry-after": "0"}),
        })
        original = client._response

        def sequenced(method: str, url: str):
            response = original(method, url)
            if "/api" in url:
                code = next(statuses, 200)
                response.status_code = code
                if code == 200:
                    response.request = httpx.Request(method, "https://example.com/private/x")
            return response

        client._response = sequenced
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with pytest.raises(FetchError) as caught:
            fetcher.get("https://example.com/api")
        assert "disallowed" in str(caught.value)

    def test_a_429_then_success_still_returns_the_response(self):
        statuses = iter([429, 200])
        client = StubClient({**ALLOW_ALL, "/api": (429, '{"ok":true}', {"retry-after": "0"})})
        original = client._response

        def sequenced(method: str, url: str):
            response = original(method, url)
            if "/api" in url:
                response.status_code = next(statuses, 200)
            return response

        client._response = sequenced
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        assert fetcher.get("https://example.com/api").ok


class TestPerSourceRequestMetering:
    """Regression: budgets were measured from a counter shared by concurrent sources.

    One source could spend another's allowance, and coverage reported request counts
    inflated by whatever ran alongside it.
    """

    def test_a_meter_counts_only_its_own_thread(self):
        client = StubClient({**ALLOW_ALL, "/api": (200, "{}", {})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        seen: dict[str, int] = {}

        def work(name: str, calls: int) -> None:
            with fetcher.usage() as meter:
                for _ in range(calls):
                    fetcher.get(f"https://{name}.example/api")
                seen[name] = meter.used

        threads = [
            threading.Thread(target=work, args=("a", 2)),
            threading.Thread(target=work, args=("b", 5)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Each source's own traffic plus its own robots fetch, and nobody else's.
        assert seen["a"] == 3
        assert seen["b"] == 6

    def test_the_global_counter_still_sees_everything(self):
        client = StubClient({**ALLOW_ALL, "/api": (200, "{}", {})})
        fetcher = HttpFetcher(min_interval=0.0, client=client)
        with fetcher.usage():
            fetcher.get("https://example.com/api")
        fetcher.get("https://example.com/api")
        assert fetcher.requests_made == 3  # robots + two calls
