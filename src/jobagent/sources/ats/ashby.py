"""Ashby job boards.

One request returns the whole board with descriptions, a real ``isRemote`` boolean and a
three-state ``workplaceType``. Compensation is only included when asked for, so it is
always requested; it arrives nested under each job's ``compensation`` object.

Ashby's API host is reported to answer 401 for /robots.txt. Under RFC 9309 a 4xx means no
robots file is published, which permits access; that behaviour is implemented in
RobotsPolicy rather than special-cased here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ...domain.models import RawPosting, SalaryRange, WorkplaceType
from ...domain.normalize import parse_salary
from ...ports import Fetcher
from .base import AtsAdapter, BoardTarget, ats_authority, require_ok

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"

WORKPLACE = {
    "remote": WorkplaceType.REMOTE,
    "onsite": WorkplaceType.ONSITE,
    "on-site": WorkplaceType.ONSITE,
    "hybrid": WorkplaceType.HYBRID,
}

# Ashby states the pay interval on each compensation component, so it is mapped rather
# than guessed.
INTERVAL = {
    "1 YEAR": "year", "1 MONTH": "month", "1 WEEK": "week", "1 DAY": "day", "1 HOUR": "hour",
}


@dataclass
class AshbyAdapter(AtsAdapter):
    name: str = "ashby"
    probe_tokens: tuple[str, ...] = ("linear", "ramp", "notion")

    def fetch_board(
        self, fetcher: Fetcher, target: BoardTarget, today: date | None = None
    ) -> list[RawPosting]:
        response = fetcher.get(
            API.format(token=target.token), params={"includeCompensation": "true"}
        )
        require_ok(response, f"ashby board {target.token!r}")
        payload = response.json() or {}
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []

        postings: list[RawPosting] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            # Unlisted postings are drafts or internal; surfacing them wastes the user's
            # attention on roles they cannot apply to.
            if job.get("isListed") is False:
                continue

            url = job.get("jobUrl") or job.get("applyUrl") or ""
            workplace = WORKPLACE.get(
                str(job.get("workplaceType") or "").lower().replace(" ", ""),
                WorkplaceType.REMOTE if job.get("isRemote") else WorkplaceType.UNKNOWN,
            )
            # secondaryLocations is documented both as a list of strings and as a list
            # of {"location": ...} objects depending on which reference you read, so both
            # are accepted. Guessing one and being wrong costs every secondary location.
            locations = [job.get("location")]
            for entry in job.get("secondaryLocations") or []:
                if isinstance(entry, str):
                    locations.append(entry)
                elif isinstance(entry, dict):
                    locations.append(entry.get("location") or entry.get("name"))
            location_text = ", ".join(
                str(loc) for loc in locations if isinstance(loc, str) and loc
            )

            postings.append(
                RawPosting(
                    source=self.name,
                    external_id=str(job.get("id") or ""),
                    title=str(job.get("title") or ""),
                    company=target.company,
                    url=url,
                    apply_url=job.get("applyUrl") or url,
                    description_text=str(job.get("descriptionPlain") or ""),
                    description_html=str(job.get("descriptionHtml") or ""),
                    location_raw=location_text,
                    workplace_hint=workplace,
                    employment_type=job.get("employmentType"),
                    # Same reason: sources disagree on departmentName/department.
                    department=(
                        job.get("departmentName") or job.get("department")
                        or job.get("teamName") or job.get("team")
                    ),
                    posted_at=_parse_date(job.get("publishedAt") or job.get("updatedAt")),
                    authority=ats_authority(url, target.domain),
                    # Compensation lives under job["compensation"], never at the top
                    # level; reading it there left salary empty on every live board.
                    salary=_salary(job.get("compensation")),
                )
            )
        return postings


def _salary(compensation: object) -> SalaryRange | None:
    """Structured Salary component first (tiers, then the summary), else the summary text."""
    if not isinstance(compensation, dict):
        return None
    components = [
        component
        for tier in compensation.get("compensationTiers") or []
        if isinstance(tier, dict)
        for component in tier.get("components") or []
    ]
    components += compensation.get("summaryComponents") or []
    for component in components:
        if not isinstance(component, dict) or component.get("compensationType") != "Salary":
            continue
        minimum, maximum = _number(component.get("minValue")), _number(component.get("maxValue"))
        if minimum is None and maximum is None:
            continue
        return SalaryRange(
            minimum=minimum,
            maximum=maximum,
            currency=str(component.get("currencyCode") or "USD"),
            period=INTERVAL.get(str(component.get("interval") or ""), "year"),
        )
    return parse_salary(
        str(
            compensation.get("compensationTierSummary")
            or compensation.get("scrapeableCompensationSalarySummary")
            or ""
        )
    )


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _parse_date(value: object):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None
