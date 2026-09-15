"""Workable job boards.

Read through the widget API Workable publishes for embedding a company's open roles on
its own website: one keyless request per tenant, descriptions included when asked for.
Every tenant lives behind ``apply.workable.com``, whose robots.txt was checked live on
2026-09-15 and disallows nothing; the older ``www.workable.com/api/accounts`` form is a
redirect to the same place, so the apply host is used directly.

Two things about the shape that are not obvious from a single entry:

- A job open in several cities comes back once PER CITY, every copy sharing the same
  shortcode and URL. Emitting each copy as its own posting would hand the deduper four
  postings with one identity, and whichever city it kept would decide whether a metro
  search saw the job. They are folded into one posting carrying every location.
- ``telecommuting`` is the only workplace signal, and it is a boolean. True means the
  job is remote; false only means it was not marked remote, which is not the same as
  onsite, so it maps to UNKNOWN and the description decides.

What it does not publish: pay. That is inferred from the description downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...domain.models import RawPosting, WorkplaceType
from ...ports import Fetcher
from .base import AtsAdapter, BoardTarget, as_text, ats_authority, require_ok

API = "https://apply.workable.com/api/v1/widget/accounts/{token}"

# Several cities for one job are joined with a semicolon rather than a comma, because
# each city already contains commas ("Dallas, Texas") and the location parser reads the
# first comma-separated pair as city and region.
LOCATION_SEPARATOR = "; "


@dataclass
class WorkableAdapter(AtsAdapter):
    name: str = "workable"
    probe_tokens: tuple[str, ...] = ("huggingface", "vizrt", "1000heads")

    def fetch_board(
        self, fetcher: Fetcher, target: BoardTarget, today: date | None = None
    ) -> list[RawPosting]:
        response = fetcher.get(API.format(token=target.token), params={"details": "true"})
        require_ok(response, f"workable board {target.token!r}")
        payload = response.json() or {}
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []

        # Insertion-ordered, so the board's own ordering survives the fold.
        postings: dict[str, RawPosting] = {}
        countries: dict[str, list[str | None]] = {}
        for job in jobs:
            if not isinstance(job, dict):
                continue
            shortcode = as_text(job.get("shortcode"))
            url = as_text(job.get("url")) or as_text(job.get("shortlink"))
            key = shortcode or url
            if not key:
                continue

            place, code = _location(job)
            if key in postings:
                existing = postings[key]
                if place and place not in existing.location_raw.split(LOCATION_SEPARATOR):
                    existing.location_raw = LOCATION_SEPARATOR.join(
                        filter(None, [existing.location_raw, place])
                    )
                countries[key].append(code)
                continue

            postings[key] = RawPosting(
                source=self.name,
                external_id=shortcode,
                title=as_text(job.get("title")),
                company=target.company,
                url=url,
                apply_url=as_text(job.get("application_url")) or url,
                description_html=_description(job),
                location_raw=place,
                workplace_hint=(
                    WorkplaceType.REMOTE if job.get("telecommuting") is True
                    else WorkplaceType.UNKNOWN
                ),
                employment_type=as_text(job.get("employment_type")) or None,
                department=as_text(job.get("department")) or None,
                posted_at=_parse_date(job.get("published_on") or job.get("created_at")),
                authority=ats_authority(url, target.domain),
            )
            countries[key] = [code]

        for key, posting in postings.items():
            posting.country_hint = _country_hint(countries[key])
        return list(postings.values())


def _location(job: dict) -> tuple[str, str | None]:
    """One entry's place as "City, Region, Country", and its ISO country code.

    The structured ``locations`` list carries the ISO code; the flat ``country``,
    ``city`` and ``state`` fields carry the same place spelled out. Both are read so a
    tenant that publishes only one form still yields a usable location.
    """
    structured = job.get("locations")
    first = structured[0] if isinstance(structured, list) and structured else None
    if not isinstance(first, dict):
        first = {}
    city = as_text(first.get("city")) or as_text(job.get("city"))
    region = as_text(first.get("region")) or as_text(job.get("state"))
    country = as_text(first.get("country")) or as_text(job.get("country"))
    code = as_text(first.get("countryCode")).upper() or None
    place = ", ".join(p for p in (city, region, country) if p)
    return place, code if code and len(code) == 2 else None


def _country_hint(codes: list[str | None]) -> str | None:
    """The country a posting is in, when its locations agree on one.

    A job open in the US and Germany is open in the US, which is what a US-only search
    asks; mixed locations with no US among them are left for text detection, which
    reads the first country named and fails a US-only search honestly.
    """
    known = {c for c in codes if c}
    if len(known) == 1:
        return known.pop()
    if "US" in known:
        return "US"
    return None


def _description(job: dict) -> str:
    """The description, with the requirements and benefits sections when published.

    ``details=true`` returns them as separate fields on some tenants and folded into
    the description on others; concatenating whatever is present is correct either way.
    """
    parts = [as_text(job.get(field)) for field in ("description", "requirements", "benefits")]
    return "".join(p for p in parts if p)


def _parse_date(value: object) -> date | None:
    """Workable publishes plain ISO dates ("2026-07-28")."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
