"""Shared fixtures. Everything here is offline by construction."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from jobagent.domain.dedup import identity_for
from jobagent.domain.models import AuthorityTier, RawPosting, WorkplaceType
from jobagent.domain.normalize import build_job
from jobagent.domain.taxonomy import Taxonomy

TODAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 12, 0, 0)


@pytest.fixture
def taxonomy() -> Taxonomy:
    return Taxonomy.default()


def make_posting(
    title: str = "Accountant",
    company: str = "Acme Corp",
    description: str = "",
    location: str = "Dallas, TX",
    *,
    source: str = "greenhouse",
    external_id: str = "1",
    url: str = "",
    workplace: WorkplaceType = WorkplaceType.UNKNOWN,
    authority: AuthorityTier = AuthorityTier.OFFICIAL_ATS,
    posted: date | None = None,
    country: str | None = None,
) -> RawPosting:
    return RawPosting(
        source=source,
        external_id=external_id,
        title=title,
        company=company,
        url=url or f"https://boards.greenhouse.io/acme/jobs/{external_id}",
        description_text=description,
        location_raw=location,
        workplace_hint=workplace,
        authority=authority,
        posted_at=posted,
        country_hint=country,
    )


def make_job(**kwargs):
    """Build a fully normalized Job, the way the engine would."""
    posting = make_posting(**kwargs)
    return build_job(
        posting, Taxonomy.default(), identity_for(
            posting.company, posting.title, posting.location_raw, posting.country_hint
        ), NOW
    )
