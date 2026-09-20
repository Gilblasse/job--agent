"""Workday career sites.

The most expensive adapter here, and the one that matters most for this product's scope.
With aggregators out of scope, Workday is where large non-tech employers post the onsite
and hybrid US roles that no other free source covers -- so its cost is accepted
deliberately rather than reluctantly.

Four quirks drive the implementation:

- The page size is hard-capped at 20. Asking for more returns an empty list with no
  error, which reads exactly like a dead board. The response does carry the query's
  ``total``, so paging stops there instead of guessing from a short page alone.
- Descriptions are not in the list response; each one costs a second request. That is
  budgeted rather than unlimited, and the budget goes to title-matched postings first:
  spent in listing order it bought descriptions for whatever Workday listed first while
  the job the user asked about went without the text the gates read.
- Tenancy is a non-guessable ``(tenant, wd number, site)`` triple, parsed from a real
  careers URL and stored as a composite registry token.
- ``postedOn`` is prose ("Posted 5 Days Ago"), not a timestamp.

In exchange, ``searchText`` is genuine server-side search, so the user's query is pushed
down to the tenant instead of pulling whole boards back to filter locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...domain.models import RawPosting, WorkplaceType
from ...domain.normalize import parse_relative_date
from ...ports import Fetcher, FetchError
from .base import (
    TERM_SEPARATOR,
    AtsAdapter,
    BoardTarget,
    ats_authority,
    prioritize_for_details,
    require_ok,
)

PAGE_SIZE = 20  # hard platform limit; larger values silently return nothing

WORKPLACE = {
    "remote": WorkplaceType.REMOTE,
    "fully remote": WorkplaceType.REMOTE,
    "hybrid": WorkplaceType.HYBRID,
    "on-site": WorkplaceType.ONSITE,
    "onsite": WorkplaceType.ONSITE,
    "in office": WorkplaceType.ONSITE,
}


@dataclass
class WorkdayAdapter(AtsAdapter):
    name: str = "workday"
    probe_tokens: tuple[str, ...] = ()
    costly: bool = True
    server_search: bool = True
    tenant_hosts: bool = True
    max_pages: int = 3
    max_details: int = 20
    max_terms: int = 4
    today: date | None = None

    def probe_targets(self) -> list[BoardTarget]:
        """Real tenancy triples, since a Workday board cannot be named by slug alone."""
        return [
            BoardTarget(company="Adobe", token="adobe",
                        extra={"wd": "5", "site": "external_experienced"}),
            BoardTarget(company="Activision", token="activision",
                        extra={"wd": "1", "site": "External"}),
        ]

    def _base(self, target: BoardTarget) -> str:
        wd = target.extra.get("wd", "5")
        site = target.extra.get("site", "External")
        return (
            f"https://{target.token}.wd{wd}.myworkdayjobs.com"
            f"/wday/cxs/{target.token}/{site}"
        )

    def fetch_board(
        self, fetcher: Fetcher, target: BoardTarget, today: date | None = None
    ) -> list[RawPosting]:
        # The run's date, threaded from DiscoveryRequest. Relative "Posted 5 Days Ago"
        # values otherwise shift at a date boundary and freshness stops being
        # deterministic unless a caller hand-builds the adapter.
        as_of = today or self.today or date.today()
        base = self._base(target)

        # Workday takes ONE search string per query, so each wanted term gets its own
        # query. Sending only the first narrowed discovery server-side, where no amount
        # of local filtering can recover a job that was never retrieved.
        raw_terms = target.extra.get("search_text", "")
        terms = [t for t in raw_terms.split(TERM_SEPARATOR) if t][: self.max_terms]

        postings: list[RawPosting] = []
        seen: set[str] = set()

        for search_text in terms or [""]:
            self._collect(fetcher, base, target, search_text, as_of, postings, seen)

        # Descriptions cost one request each, so they are capped. Without them the
        # workplace and requirement gates cannot reach a verdict, which is precisely the
        # information this product's hardest search needs -- hence a budget, not zero.
        # Title matches go first: spent in listing order, the budget bought descriptions
        # for whatever Workday listed first and left the matching job blind.
        for posting in prioritize_for_details(postings, terms, self.max_details):
            self._enrich(fetcher, base, posting, posting.extra["path"])

        return postings

    def _collect(
        self, fetcher: Fetcher, base: str, target: BoardTarget, search_text: str,
        as_of: date, postings: list[RawPosting], seen: set[str],
    ) -> None:
        """Run one query against one board, appending postings not already collected."""
        fetched = 0  # rows THIS query returned, duplicates included: ``total`` is per query
        for page in range(self.max_pages):
            response = fetcher.post_json(
                f"{base}/jobs",
                payload={
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "searchText": search_text,
                },
            )
            if page == 0:
                require_ok(response, f"workday board {target.token!r}")
            elif not response.ok:
                # A later page failing is not end-of-results either. For a board with
                # more than 20 postings this returned page one and reported the board
                # healthy, presenting partial discovery as full coverage.
                raise FetchError(
                    f"workday board {target.token!r}: HTTP {response.status} on page "
                    f"{page + 1} of pagination",
                    status=response.status,
                )

            payload = response.json() or {}
            listings = payload.get("jobPostings") if isinstance(payload, dict) else None
            if not isinstance(listings, list) or not listings:
                return
            fetched += len(listings)

            for item in listings:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("externalPath") or "")
                if not path or path in seen:
                    continue  # one job can answer several terms
                seen.add(path)

                public_url = _public_url(target, path)
                requisition = path.rsplit("/", 1)[-1] or path
                postings.append(
                    RawPosting(
                        source=self.name,
                        # Namespaced by tenancy. A bare "R-1" is unique only within one
                        # tenant and site, and this value is used both for within-source
                        # deduplication and for cross-board clustering -- so two
                        # employers sharing a tail could drop a posting or, worse, merge
                        # two unrelated jobs into one record.
                        external_id=(
                            f"{target.token}:{target.extra.get('wd', '5')}:"
                            f"{target.extra.get('site', 'External')}:{requisition}"
                        ),
                        title=str(item.get("title") or ""),
                        company=target.company,
                        url=public_url,
                        location_raw=str(item.get("locationsText") or ""),
                        posted_at=parse_relative_date(str(item.get("postedOn") or ""), as_of),
                        authority=ats_authority(public_url, target.domain),
                        extra={"path": path},
                    )
                )

            # A short page ends the query, and so does reaching its own ``total``: a full
            # page holding exactly ``total`` rows otherwise cost one more request that
            # could only come back empty, or 500 and fail the board.
            total = payload.get("total")
            if len(listings) < PAGE_SIZE or (isinstance(total, int) and fetched >= total):
                return

    def _enrich(self, fetcher: Fetcher, base: str, posting: RawPosting, path: str) -> None:
        tail = path.rsplit("/", 1)[-1]
        if not tail:
            return
        try:
            response = fetcher.get(f"{base}/job/{tail}")
        except FetchError as error:
            # A blocked host must NOT be swallowed here. Descriptions are what the
            # workplace, requirement and salary gates read, so quietly returning postings
            # without them presents a degraded run as a healthy one.
            if error.blocked:
                raise
            return
        except Exception:  # noqa: BLE001 - a missing description is not a failed board
            return
        if not response.ok:
            return
        payload = response.json() or {}
        info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            return

        posting.description_html = str(info.get("jobDescription") or "")
        posting.employment_type = info.get("timeType") or posting.employment_type
        posting.location_raw = str(info.get("location") or posting.location_raw)
        remote = str(info.get("remoteType") or "").lower()
        if remote:
            posting.workplace_hint = WORKPLACE.get(remote, posting.workplace_hint)
        country = info.get("country")
        if isinstance(country, dict):
            code = country.get("alpha2Code") or country.get("descriptor")
            if isinstance(code, str) and len(code) == 2:
                posting.country_hint = code.upper()
        if info.get("externalUrl"):
            posting.apply_url = str(info["externalUrl"])


def _public_url(target: BoardTarget, path: str) -> str:
    wd = target.extra.get("wd", "5")
    site = target.extra.get("site", "External")
    return (
        f"https://{target.token}.wd{wd}.myworkdayjobs.com/en-US/{site}{path}"
        if path.startswith("/")
        else f"https://{target.token}.wd{wd}.myworkdayjobs.com/en-US/{site}/{path}"
    )
