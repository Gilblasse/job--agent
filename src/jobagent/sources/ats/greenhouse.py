"""Greenhouse job boards.

Officially documented and explicitly auth-free: "Job Board data is publicly available, so
authentication is not required for any GET endpoints." The whole board, descriptions
included, comes back in one request -- the cheapest source in the set.

What it does not publish: any remote flag, employment type, or pay. Those are inferred
from text downstream, which is why several gates will report UNVERIFIABLE on Greenhouse
postings rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ...domain.models import RawPosting
from ...ports import Fetcher
from .base import AtsAdapter, BoardTarget, ats_authority, require_ok

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


@dataclass
class GreenhouseAdapter(AtsAdapter):
    name: str = "greenhouse"
    probe_tokens: tuple[str, ...] = ("stripe", "figma", "airbnb")

    def fetch_board(self, fetcher: Fetcher, target: BoardTarget) -> list[RawPosting]:
        response = fetcher.get(API.format(token=target.token), params={"content": "true"})
        require_ok(response, f"greenhouse board {target.token!r}")
        payload = response.json() or {}
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []

        postings: list[RawPosting] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            url = job.get("absolute_url") or ""
            offices = job.get("offices") or []
            office_text = ", ".join(
                str(o.get("location") or o.get("name") or "")
                for o in offices if isinstance(o, dict)
            )
            location = (job.get("location") or {}).get("name") if isinstance(
                job.get("location"), dict
            ) else ""
            departments = job.get("departments") or []
            department = next(
                (d.get("name") for d in departments if isinstance(d, dict) and d.get("name")), None
            )

            postings.append(
                RawPosting(
                    source=self.name,
                    external_id=str(job.get("id") or ""),
                    title=str(job.get("title") or ""),
                    company=target.company,
                    url=url,
                    # Greenhouse entity-encodes its HTML; html_to_text unescapes twice.
                    description_html=str(job.get("content") or ""),
                    location_raw=str(location or office_text or ""),
                    department=department,
                    posted_at=_parse_date(job.get("first_published") or job.get("updated_at")),
                    authority=ats_authority(url, target.domain),
                    extra={"requisition_id": str(job.get("requisition_id") or "")},
                )
            )
        return postings


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None
