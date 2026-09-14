"""What every source has in common.

An adapter's contract is narrow on purpose: map a platform's shape onto ``RawPosting``,
and report honestly what happened. It must not raise for an ordinary failure. A dead
board, a rate limit or an unreachable host is a status on a report, because one bad
source must never take down a whole search.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..domain.models import RawPosting, SourceReport, SourceStatus
from ..ports import DiscoveryRequest, DiscoveryResult, Fetcher, FetchError


@dataclass
class SourceAdapter:
    """Base class for every source."""

    name: str = "source"

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        raise NotImplementedError

    def check(self, fetcher: Fetcher) -> SourceReport:
        """Probe the source for the go/no-go gate.

        Liveness is judged on CONTENT, never on status code. Several ATS platforms return
        HTTP 200 with a vendor placeholder page for tenants that do not exist, and one
        redirects rather than 404s, so a status-only check reports healthy sources that
        return nothing.
        """
        raise NotImplementedError

    # -- helpers shared by adapters ------------------------------------------------

    @staticmethod
    def _timer() -> float:
        return time.monotonic()

    def _report(
        self, status: SourceStatus, *, found: int = 0, requests: int = 0,
        started: float = 0.0, note: str = "",
    ) -> SourceReport:
        return SourceReport(
            source=self.name,
            status=status,
            found=found,
            requests=requests,
            duration_ms=int((time.monotonic() - started) * 1000) if started else 0,
            note=note,
        )

    @staticmethod
    def classify_failure(error: Exception) -> tuple[SourceStatus, str]:
        """Turn an exception into a reportable status.

        Being blocked is kept distinct from being broken. The two call for different
        responses -- one means stop asking, the other means try again -- and merging them
        is how a registry silently rots: a rate-limited board looks exactly like a dead
        one unless the difference is recorded.
        """
        if isinstance(error, FetchError) and error.blocked:
            return SourceStatus.BLOCKED, str(error)
        if isinstance(error, FetchError):
            return SourceStatus.UNAVAILABLE, str(error)
        return SourceStatus.FAILED, f"{type(error).__name__}: {error}"


def dedupe_postings(postings: list[RawPosting]) -> list[RawPosting]:
    """Drop exact repeats within one source's own output."""
    seen: set[tuple[str, str]] = set()
    unique: list[RawPosting] = []
    for posting in postings:
        key = (posting.source, posting.external_id or posting.url)
        if key not in seen:
            seen.add(key)
            unique.append(posting)
    return unique
