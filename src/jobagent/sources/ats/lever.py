"""Lever job boards.

The richest of the free ATS feeds: a real three-state ``workplaceType``, a structured
salary range, and an ISO country code. Because those are published rather than inferred,
Lever postings resolve gates that stay UNVERIFIABLE elsewhere. The body is split across
``description`` (the opening), ``lists`` (one heading plus HTML list per section) and
``additional`` (the closing); ``_body`` joins them so the gates read the whole posting.

Lever's own documentation is unusually explicit that this is public: published postings
"may be scraped by third parties".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from ...domain.models import RawPosting, SalaryRange, WorkplaceType
from ...domain.text import html_to_text
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
    probe_tokens: tuple[str, ...] = ("spotify", "palantir", "ans")

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
            text, html = _body(job)

            postings.append(
                RawPosting(
                    source=self.name,
                    external_id=str(job.get("id") or ""),
                    title=str(job.get("text") or ""),
                    company=target.company,
                    url=url,
                    apply_url=job.get("applyUrl") or url,
                    description_text=text,
                    description_html=html,
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


def _body(job: dict) -> tuple[str, str]:
    """The whole posting as text and as HTML.

    Requirements and responsibilities live in ``lists``, not in ``description``. Reading
    only the opening, as the adapter used to, meant a credential demanded in a bullet under
    "Requirements" never reached the requirement gate, and the responsibilities gates fell
    back to the opening paragraph. Each list is emitted under its heading so the section
    detector can cut the text where the employer did.
    """
    lists = [d for d in job.get("lists") or [] if isinstance(d, dict) and d.get("text")]
    parts = [
        str(job.get("descriptionPlain") or "") or html_to_text(str(job.get("description") or "")),
        *(f"{d['text']}\n{html_to_text(str(d.get('content') or ''))}" for d in lists),
        str(job.get("additionalPlain") or "") or html_to_text(str(job.get("additional") or "")),
    ]
    html = (
        str(job.get("description") or "")
        + "".join(f"<h3>{d['text']}</h3>{d.get('content') or ''}" for d in lists)
        + str(job.get("additional") or "")
    )
    return "\n\n".join(p for p in parts if p.strip()), html


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
