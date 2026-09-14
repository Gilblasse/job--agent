"""Recognizing that two postings are the same opportunity.

The same job reaches us through an employer's careers page and its ATS board, often with
different URLs, differently-worded titles and differently-spelled company names. Showing
it twice -- worse, showing it again next week as "new" -- is the fastest way to make a
job tracker useless.

Matching runs cheapest-first: an exact platform id, then a canonicalized URL, then a
normalized identity built from employer, title and location.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import AuthorityTier, Job, RawPosting
from .normalize import normalize_company, normalize_title, parse_location

# Parameters that identify a referrer or a session, never the job. Left in place they
# make one posting look like several.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gh_src", "gh_jid", "ref", "referrer", "source", "src", "trk", "trackingid",
    "lever-source", "lever-origin", "session", "sessionid", "sid", "fbclid", "gclid",
    "mc_cid", "mc_eid", "recruiter", "campaign", "from", "utm_referrer",
}

# Hosts known to host employer-run boards on behalf of the employer. Used to decide that
# a URL is an official ATS posting rather than an unverified third party.
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com",
    "workable.com", "recruitee.com", "bamboohr.com", "breezy.hr", "applytojob.com",
    "teamtailor.com", "personio.de", "rippling.com", "usajobs.gov", "jobs.polymer.co",
)


def canonical_url(url: str) -> str:
    """Strip a URL down to what actually identifies the posting.

    Removes tracking parameters, normalizes host casing, drops fragments and trailing
    slashes. Two links that differ only by campaign tag collapse to one key.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(sorted(query)), ""))


def url_authority(url: str, employer_domain: str | None = None) -> AuthorityTier:
    """Rank how authoritative a URL is as a place to apply.

    An employer's own domain outranks its ATS, which outranks anything else. This is the
    ordering that decides which URL becomes the canonical record.
    """
    if not url:
        return AuthorityTier.UNVERIFIED
    host = (urlsplit(url).hostname or "").lower()
    if employer_domain and host.endswith(employer_domain.lower().lstrip(".")):
        return AuthorityTier.EMPLOYER_SITE
    if any(host.endswith(ats) or ats in host for ats in ATS_HOSTS):
        return AuthorityTier.OFFICIAL_ATS
    return AuthorityTier.UNVERIFIED


def location_bucket(raw: str, country: str | None = None) -> str:
    """Coarse location key for identity.

    Deliberately coarse: "Dallas, TX", "Dallas-Fort Worth" and "Dallas, Texas, United
    States" all describe one place, and a stricter key would split one job into three.
    """
    location = parse_location(raw, country)
    if location.city:
        # Split on the separator BEFORE stripping punctuation: a metro written
        # "Dallas-Fort Worth" must reduce to "dallas", and removing the hyphen first
        # would leave "dallasfort worth", which matches nothing.
        city = location.city.lower().split("-")[0].split("/")[0].split(",")[0]
        city = re.sub(r"[^a-z ]", "", city).strip()
        return f"{city}|{location.region or location.country or ''}".strip("|")
    if location.region:
        return location.region.lower()
    if location.country:
        return location.country.lower()
    return "unspecified"


def identity_for(company: str, title: str, location_raw: str, country: str | None = None) -> str:
    """The cross-source identity of an opportunity.

    Built from normalized employer, normalized title and a coarse location so that the
    same role recognizes itself whichever platform it arrived through.

    Known limitation, accepted deliberately: two separate requisitions with identical
    titles at the same employer and location collapse into one record. Both URLs are kept
    on that record. Splitting them would need a requisition id that most sources do not
    publish, and the alternative -- showing the user the same job twice -- is worse.
    """
    key = "|".join(
        [normalize_company(company), normalize_title(title), location_bucket(location_raw, country)]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def posting_identity(posting: RawPosting) -> str:
    return identity_for(
        posting.company, posting.title, posting.location_raw, posting.country_hint
    )


def content_hash(text: str) -> str:
    """Stable hash of description text, used to notice a posting has changed."""
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def cluster_postings(postings: list[RawPosting]) -> dict[str, list[RawPosting]]:
    """Group raw postings into one bucket per opportunity.

    Exact signals are applied first and can merge buckets that the normalized identity
    would have kept apart -- two differently-titled records sharing a canonical URL are
    the same job, whatever they call themselves.
    """
    by_identity: dict[str, list[RawPosting]] = {}
    url_index: dict[str, str] = {}
    external_index: dict[tuple[str, str], str] = {}

    for posting in postings:
        identity = posting_identity(posting)

        url_key = canonical_url(posting.apply_url or posting.url)
        ext_key = (posting.source, posting.external_id) if posting.external_id else None

        if url_key and url_key in url_index:
            identity = url_index[url_key]
        elif ext_key and ext_key in external_index:
            identity = external_index[ext_key]

        by_identity.setdefault(identity, []).append(posting)
        if url_key:
            url_index[url_key] = identity
        if ext_key:
            external_index[ext_key] = identity

    return by_identity


def choose_canonical(postings: list[RawPosting]) -> RawPosting:
    """Pick the record that should represent a cluster.

    Highest authority wins, so the user is sent to the employer or its official ATS
    rather than an intermediary. Among equals, the record carrying a real description
    wins, because that is what the gates need to read.
    """
    def rank(posting: RawPosting) -> tuple[int, int, int]:
        description = posting.description_text or posting.description_html
        return (int(posting.authority), 1 if description else 0, len(description or ""))

    return max(postings, key=rank)


def merge_job_sources(job: Job, others: list[Job]) -> Job:
    """Fold duplicate jobs' provenance into the canonical job."""
    seen = {canonical_url(ref.url) for ref in job.sources}
    for other in others:
        for ref in other.sources:
            key = canonical_url(ref.url)
            if key and key not in seen:
                job.sources.append(ref)
                seen.add(key)
    return job
