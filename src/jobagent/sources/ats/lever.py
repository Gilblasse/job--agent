"""Lever job boards.

The richest of the free ATS feeds: a real three-state ``workplaceType``, a structured
salary range, and an ISO country code. Because those are published rather than inferred,
Lever postings resolve gates that stay UNVERIFIABLE elsewhere.

Lever's own documentation is unusually explicit that this is public: published postings
"may be scraped by third parties".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from ...domain.models import RawPosting, SalaryRange, WorkplaceType
from ...ports import Fetcher
from .base import AtsAdapter, BoardTarget, ats_authority, require_ok

API = "https://api.lever.co/v0/postings/{token}"

WORKPLACE = {
    "remote": WorkplaceType.REMOTE,
    "on-site": WorkplaceType.ONSITE,
    "onsite": WorkplaceType.ONSITE,
    "hybrid": WorkplaceType.HYBRID,
    "unspecified": WorkplaceType.UNKNOWN,
}

# Lever states the pay interval explicitly, so it is mapped rather than guessed.
INTERVAL = {
    "per-year-salary": "year", "per-month-salary": "month", "per-week-salary": "week",
    "per-day-wage": "day", "per-hour-wage": "hour",
}


@dataclass
class LeverAdapter(AtsAdapter):
    name: str = "lever"
    probe_tokens: tuple[str, ...] = ("netflix", "plaid", "spotify")

    def fetch_board(
        self, fetcher: Fetcher, target: BoardTarget, today: date | None = None
    ) -> list[RawPosting]:
        response = fetcher.get(API.format(token=target.token), params={"mode": "json"})
        require_ok(response, f"lever board {target.token!r}")
        jobs = response.json()
        if not isinstance(jobs, list):
            return []

        postings: list[RawPosting] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            categories = job.get("categories") or {}
            url = job.get("hostedUrl") or job.get("applyUrl") or ""
            workplace = WORKPLACE.get(
                str(job.get("workplaceType") or "").lower(), WorkplaceType.UNKNOWN
            )

            postings.append(
                RawPosting(
                    source=self.name,
                    external_id=str(job.get("id") or ""),
                    title=str(job.get("text") or ""),
                    company=target.company,
                    url=url,
                    apply_url=job.get("applyUrl") or url,
                    description_text=str(job.get("descriptionPlain") or ""),
                    description_html=str(job.get("description") or ""),
                    location_raw=str(categories.get("location") or ""),
                    country_hint=_country(job.get("country")),
                    workplace_hint=workplace,
                    employment_type=categories.get("commitment"),
                    department=categories.get("team") or categories.get("department"),
                    salary=_salary(job.get("salaryRange")),
                    posted_at=_epoch_date(job.get("createdAt")),
                    authority=ats_authority(url, target.domain),
                )
            )
        return postings


def _country(value: object) -> str | None:
    return value.upper() if isinstance(value, str) and len(value) == 2 else None


def _salary(raw: object) -> SalaryRange | None:
    if not isinstance(raw, dict):
        return None
    minimum, maximum = raw.get("min"), raw.get("max")
    if minimum is None and maximum is None:
        return None
    return SalaryRange(
        minimum=float(minimum) if minimum is not None else None,
        maximum=float(maximum) if maximum is not None else None,
        currency=str(raw.get("currency") or "USD").upper(),
        period=INTERVAL.get(str(raw.get("interval") or ""), "year"),
    )


def _epoch_date(value: object):
    """Lever publishes creation time as epoch milliseconds."""
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return None
