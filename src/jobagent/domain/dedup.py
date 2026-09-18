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
    if not host:
        return AuthorityTier.UNVERIFIED

    # Matched on domain boundaries, not substrings. `ats in host` promoted
    # "greenhouse.io.evil.example" to OFFICIAL_ATS, and a bare endswith made
    # "evilacme.com" an employer site for "acme.com". This decides which link the user is
    # sent to, so it is the one place a lookalike host must not slip through.
    if employer_domain:
        domain = employer_domain.lower().strip().lstrip(".")
        if domain and (host == domain or host.endswith(f".{domain}")):
            return AuthorityTier.EMPLOYER_SITE
    if any(host == ats or host.endswith(f".{ats}") for ats in ATS_HOSTS):
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

    Three signals can merge two postings: the normalized identity, a shared canonical
    URL, and a shared platform id. They are applied as a union-find rather than by
    reassigning postings one at a time, because merging is transitive and order must not
    matter: if A and B share an identity while B and C share a URL, all three are one job
    however they happen to arrive.

    An earlier version reassigned each posting to the identity it collided with, which
    quietly split a group whenever the colliding posting had been seen first.
    """
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    # Every signal becomes a node; a posting unions the nodes it carries.
    keys: list[str] = []
    for posting in postings:
        identity = posting_identity(posting)
        keys.append(identity)
        find(identity)

        url_key = canonical_url(posting.apply_url or posting.url)
        if url_key:
            union(identity, f"url:{url_key}")
        if posting.external_id:
            union(identity, f"ext:{posting.source}:{posting.external_id}")

    grouped: dict[str, list[RawPosting]] = {}
    for posting, identity in zip(postings, keys, strict=True):
        grouped.setdefault(find(identity), []).append(posting)

    # The representative may be a url: or ext: node, which is an implementation detail.
    # Each cluster is named by the identity of the record that will represent it, so the
    # key stays stable across runs and matches what gets persisted.
    return {
        posting_identity(choose_canonical(group)): group for group in grouped.values()
    }


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


def job_content_hash(job: Job) -> str:
    """The hash of everything a stored job row carries about the posting.

    Compared against the stored row's hash to tell "this posting changed" from "this
    posting was read again": an unchanged posting costs no rewrite, and a verdict records
    which version of the text it judged.
    """
    salary = job.salary
    parts = [
        job.title, job.company, job.url, job.description_text,
        job.location.raw, job.location.city or "", job.location.region or "",
        job.location.country or "", job.workplace.value, job.employment_type or "",
        job.department or "",
        "" if salary is None else
        f"{salary.minimum}|{salary.maximum}|{salary.currency}|{salary.period}",
        job.posted_at.isoformat() if job.posted_at else "", str(int(job.authority)),
    ]
    return content_hash("\x1f".join(parts))
