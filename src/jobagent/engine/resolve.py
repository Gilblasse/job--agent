"""Choosing the record a user should actually apply through.

The same opportunity arrives from several places. Whichever we keep becomes the link the
user clicks, so the rule is fixed: the employer's own domain beats its official ATS, which
beats anything unverified. Every other URL is kept as provenance rather than discarded.
"""

from __future__ import annotations

from datetime import datetime

from ..domain.dedup import choose_canonical, cluster_postings, posting_identity
from ..domain.models import Job, JobSourceRef, RawPosting
from ..domain.normalize import build_job
from ..domain.taxonomy import Taxonomy
from ..domain.text import html_to_text


def resolve(
    postings: list[RawPosting], taxonomy: Taxonomy, seen_at: datetime
) -> list[Job]:
    """Cluster raw postings and build one canonical Job per opportunity."""
    jobs: list[Job] = []

    for identity, group in cluster_postings(postings).items():
        canonical = choose_canonical(group)
        job = build_job(canonical, taxonomy, identity, seen_at)

        # Keep every URL the job was seen at. Deduplication is about what the user is
        # shown, not about forgetting where the information came from.
        known = {job.sources[0].url} if job.sources else set()
        for posting in group:
            if posting is canonical:
                continue
            url = posting.apply_url or posting.url
            if url and url not in known:
                known.add(url)
                job.sources.append(
                    JobSourceRef(
                        source=posting.source, url=url, external_id=posting.external_id,
                        authority=posting.authority, first_seen=seen_at, last_seen=seen_at,
                    )
                )

        # A description can be missing from the most authoritative record while a
        # lesser one carries it. Gates read description text, so borrowing it turns
        # several unverifiable verdicts into real ones.
        if not job.description_text:
            for posting in group:
                # Greenhouse and Workday supply description_html and no plain text, so
                # checking only description_text left the record textless and every
                # text-based gate unverifiable.
                text = posting.description_text or html_to_text(posting.description_html)
                if text:
                    job.description_text = text
                    break

        jobs.append(job)

    return jobs


def identity_of(posting: RawPosting) -> str:
    return posting_identity(posting)
