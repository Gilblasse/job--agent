"""Checking whether a job still exists.

A tracker that shows filled roles wastes the user's time, but a tracker that marks a live
role dead because a request timed out is worse. So verification only ever moves a job to
GONE on a definite answer from the server, and leaves it UNVERIFIED otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain.models import VerificationState
from ..infra.store import Store
from ..ports import Fetcher, FetchError


@dataclass
class VerificationSummary:
    checked: int = 0
    live: int = 0
    gone: int = 0
    inconclusive: int = 0

    def describe(self) -> str:
        return (
            f"checked {self.checked}: {self.live} still live, {self.gone} closed, "
            f"{self.inconclusive} could not be confirmed"
        )


def verify_jobs(
    store: Store, fetcher: Fetcher, job_ids: list[int], *, now: datetime | None = None
) -> VerificationSummary:
    """Re-check a set of jobs and record what was learned."""
    now = now or datetime.now()
    summary = VerificationSummary()

    for job_id in job_ids:
        row = store.get_job(job_id)
        if row is None or not row["url"]:
            continue
        summary.checked += 1

        try:
            response = fetcher.get(row["url"])
        except FetchError:
            # Blocked or unreachable says something about us, not about the posting.
            summary.inconclusive += 1
            continue

        if response.status in (404, 410):
            store.mark_verification(job_id, VerificationState.GONE, now)
            summary.gone += 1
        elif response.ok and _redirected_away(row["url"], response.url):
            # Several ATS platforms answer a removed posting with a 200 redirect to the
            # board root. Trusting the status alone marked those roles LIVE forever --
            # exactly the failure this function exists to prevent. It is not proof the
            # job is gone either, so it is reported as unconfirmed.
            summary.inconclusive += 1
        elif response.ok:
            store.mark_verification(job_id, VerificationState.LIVE, now)
            summary.live += 1
        else:
            summary.inconclusive += 1

    return summary


def _redirected_away(requested: str, final: str) -> bool:
    """True when a response came back from a meaningfully different place.

    A posting URL carries an id; a board root does not. Landing somewhere shorter than
    where we asked is the signature of "this listing is gone, here is the board instead".
    """
    if not final or final == requested:
        return False
    from urllib.parse import urlsplit

    asked, got = urlsplit(requested), urlsplit(final)
    if asked.hostname != got.hostname:
        return True
    asked_path = asked.path.rstrip("/")
    got_path = got.path.rstrip("/")
    # Any path change is inconclusive: landing on an ancestor means the specific posting
    # is gone, and landing elsewhere tells us nothing about this one either.
    return got_path != asked_path
