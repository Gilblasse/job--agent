"""USAJOBS -- the US federal government's own applicant system.

Not a job board: this is the employer's own system, which is why it survives a scope that
excludes aggregators. It is also the only source in that scope with genuine cross-company
keyword search and real coverage of onsite roles in arbitrary US metros, across every
occupation the federal government employs.

Its ceiling is equally clear and belongs in any honest coverage report: federal only. No
state, municipal or private-sector employers appear here.

The API key is free, instantly issued, and never becomes paid. Its one trap is that the
``User-Agent`` header must carry the registered email address -- the usual cause of
otherwise inexplicable authentication failures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime

from ..domain.models import (
    AuthorityTier,
    RawPosting,
    SalaryRange,
    SourceReport,
    SourceStatus,
    WorkplaceType,
)
from ..ports import DiscoveryRequest, DiscoveryResult, Fetcher, FetchError
from .base import SourceAdapter, dedupe_postings

API = "https://data.usajobs.gov/api/search"
KEY_ENV = "JOBAGENT_USAJOBS_KEY"
EMAIL_ENV = "JOBAGENT_USAJOBS_EMAIL"

RATE_INTERVAL = {
    "PA": "year", "PH": "hour", "PD": "day", "PW": "week", "PM": "month", "BW": "week",
}


@dataclass
class UsaJobsAdapter(SourceAdapter):
    name: str = "usajobs"
    results_per_page: int = 50
    max_pages: int = 4
    _page_notes: list[str] = field(default_factory=list)

    def credentials(self) -> tuple[str, str] | None:
        key, email = os.environ.get(KEY_ENV, ""), os.environ.get(EMAIL_ENV, "")
        return (key, email) if key and email else None

    def _headers(self, key: str, email: str) -> dict[str, str]:
        return {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        started = self._timer()
        fetcher = request.fetcher
        if fetcher is None:
            return DiscoveryResult(report=self._report(SourceStatus.SKIPPED, note="no fetcher"))

        credentials = self.credentials()
        if credentials is None:
            return DiscoveryResult(
                report=self._report(
                    SourceStatus.SKIPPED, started=started,
                    note=f"set {KEY_ENV} and {EMAIL_ENV} to enable (free, instant, no card)",
                )
            )
        key, email = credentials

        before = fetcher.requests_made
        postings: list[RawPosting] = []
        failures: list[str] = []
        terms = request.terms or [""]

        for term in terms[:4]:
            for location in (request.locations or [""])[:3]:
                # The run's budget is a cap on requests, not a suggestion. Without this
                # a four-term, three-location search issued up to 48 requests whatever
                # the caller asked for.
                if fetcher.requests_made - before >= request.budget:
                    failures.append("stopped at this run's request budget")
                    break
                try:
                    postings.extend(
                        self._search(
                            fetcher, key, email, term, location, request.since,
                            request.today or date.today(),
                        )
                    )
                except FetchError as error:
                    status, note = self.classify_failure(error)
                    if status is SourceStatus.BLOCKED:
                        return DiscoveryResult(
                            postings=dedupe_postings(postings),
                            report=self._report(
                                SourceStatus.BLOCKED, found=len(postings),
                                requests=fetcher.requests_made - before, started=started, note=note,
                            ),
                        )
                    failures.append(note)

        status = SourceStatus.OK if not failures else (
            SourceStatus.PARTIAL if postings else SourceStatus.UNAVAILABLE
        )
        return DiscoveryResult(
            postings=dedupe_postings(postings),
            report=self._report(
                status, found=len(postings), requests=fetcher.requests_made - before,
                started=started, note="; ".join(failures[:2]) or "federal postings only",
            ),
        )

    def _search(
        self, fetcher: Fetcher, key: str, email: str, term: str, location: str,
        since: date | None, today: date,
    ) -> list[RawPosting]:
        postings: list[RawPosting] = []
        for page in range(1, self.max_pages + 1):
            params: dict[str, str | int] = {
                "ResultsPerPage": self.results_per_page,
                "Page": page,
            }
            if term:
                params["Keyword"] = term
            if location:
                params["LocationName"] = location
            if since:
                params["DatePosted"] = max(1, min(60, (today - since).days))

            response = fetcher.get(API, params=params, headers=self._headers(key, email))
            if response.status in (401, 403):
                # Almost always a mistyped key, or a User-Agent that is not the address
                # the key was registered to. Reporting this as "no results" would leave
                # the user with a permanently green, permanently empty source.
                raise FetchError(
                    f"USAJOBS rejected the credentials (HTTP {response.status}); check "
                    f"{KEY_ENV} and {EMAIL_ENV}",
                    blocked=True, status=response.status,
                )
            if not response.ok:
                if page == 1:
                    # A failure on the FIRST page is not end-of-results; leaving it here
                    # made an outage or a dead endpoint look like a healthy, empty
                    # federal search.
                    raise FetchError(
                        f"USAJOBS returned HTTP {response.status}", status=response.status
                    )
                failures_note = f"stopped after page {page - 1}: HTTP {response.status}"
                self._page_notes.append(failures_note)
                break
            payload = response.json() or {}
            result = payload.get("SearchResult", {}) if isinstance(payload, dict) else {}
            items = result.get("SearchResultItems") or []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                posting = _to_posting(item, self.name)
                if posting:
                    postings.append(posting)
            if len(items) < self.results_per_page:
                break
        return postings

    def check(self, fetcher: Fetcher) -> SourceReport:  # type: ignore[override]
        started = self._timer()
        credentials = self.credentials()
        if credentials is None:
            return self._report(
                SourceStatus.SKIPPED, started=started,
                note=f"no credentials; set {KEY_ENV} and {EMAIL_ENV} (free, instant)",
            )
        key, email = credentials
        before = fetcher.requests_made
        try:
            # No keyword: the probe asks "is this source answering at all", and any
            # occupation used here would bake one profession into the health check.
            response = fetcher.get(
                API, params={"ResultsPerPage": 5},
                headers=self._headers(key, email),
            )
        except Exception as error:  # noqa: BLE001
            status, note = self.classify_failure(error)
            return self._report(
                status, requests=fetcher.requests_made - before, started=started, note=note
            )

        count = 0
        if response.ok:
            payload = response.json() or {}
            count = len(
                (payload.get("SearchResult") or {}).get("SearchResultItems") or []
            )
        elif response.status in (401, 403):
            return self._report(
                SourceStatus.BLOCKED, requests=fetcher.requests_made - before, started=started,
                note=f"credentials rejected (HTTP {response.status}); check {KEY_ENV}/{EMAIL_ENV}",
            )
        status = SourceStatus.OK if count else SourceStatus.UNAVAILABLE
        return self._report(
            status, found=count, requests=fetcher.requests_made - before, started=started,
            note=f"HTTP {response.status}, {count} postings",
        )


def _to_posting(item: object, source: str) -> RawPosting | None:
    if not isinstance(item, dict):
        return None
    d = item.get("MatchedObjectDescriptor")
    if not isinstance(d, dict):
        return None

    locations = d.get("PositionLocation") or []
    location_text = "; ".join(
        str(loc.get("LocationName") or "") for loc in locations if isinstance(loc, dict)
    )
    details = ((d.get("UserArea") or {}).get("Details") or {}) if isinstance(
        d.get("UserArea"), dict
    ) else {}
    description = "\n\n".join(
        str(details.get(field) or "")
        for field in ("JobSummary", "MajorDuties", "Requirements", "Qualifications")
    ).strip()

    telework = str(details.get("TeleworkEligible") or "").lower()
    remote = str(details.get("RemoteIndicator") or "").lower()
    if remote in ("true", "yes"):
        workplace = WorkplaceType.REMOTE
    elif telework in ("true", "yes"):
        workplace = WorkplaceType.HYBRID  # telework-eligible is not fully remote
    elif telework in ("false", "no") or remote in ("false", "no"):
        workplace = WorkplaceType.ONSITE
    else:
        # Neither field present. Guessing ONSITE here made a remote-only search hard
        # reject every federal posting with a terse UserArea -- an adapter inventing a
        # fact the posting never stated.
        workplace = WorkplaceType.UNKNOWN

    apply_uris = d.get("ApplyURI") or []
    apply_url = apply_uris[0] if isinstance(apply_uris, list) and apply_uris else None

    return RawPosting(
        source=source,
        external_id=str(d.get("PositionID") or d.get("MatchedObjectId") or ""),
        title=str(d.get("PositionTitle") or ""),
        company=str(d.get("OrganizationName") or d.get("DepartmentName") or "US Government"),
        url=str(d.get("PositionURI") or ""),
        apply_url=str(apply_url) if apply_url else None,
        description_text=description,
        location_raw=location_text,
        country_hint="US",  # federal postings are US by definition
        workplace_hint=workplace,
        employment_type=_schedule(d.get("PositionSchedule")),
        department=str(d.get("DepartmentName") or "") or None,
        salary=_remuneration(d.get("PositionRemuneration")),
        posted_at=_parse_date(d.get("PublicationStartDate")),
        authority=AuthorityTier.OFFICIAL_ATS,
    )


def _schedule(value: object) -> str | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return str(value[0].get("Name") or "") or None
    return None


def _remuneration(value: object) -> SalaryRange | None:
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        return None
    entry = value[0]
    try:
        minimum = float(entry.get("MinimumRange") or 0) or None
        maximum = float(entry.get("MaximumRange") or 0) or None
    except (TypeError, ValueError):
        return None
    if minimum is None and maximum is None:
        return None
    return SalaryRange(
        minimum=minimum, maximum=maximum, currency="USD",
        period=RATE_INTERVAL.get(str(entry.get("RateIntervalCode") or ""), "year"),
    )


def _parse_date(value: object):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None
