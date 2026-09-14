"""Workday career sites.

The most expensive adapter here, and the one that matters most for this product's scope.
With aggregators out of scope, Workday is where large non-tech employers post the onsite
and hybrid US roles that no other free source covers -- so its cost is accepted
deliberately rather than reluctantly.

Four quirks drive the implementation:

- The page size is hard-capped at 20. Asking for more returns an empty list with no
  error, which reads exactly like a dead board.
- Descriptions are not in the list response; each one costs a second request. That is
  budgeted rather than unlimited.
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
from .base import AtsAdapter, BoardTarget, ats_authority, require_ok

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
    max_pages: int = 3
    max_details: int = 20
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
        base = self._base(target)
        # The run's date, threaded from DiscoveryRequest. Relative "Posted 5 Days Ago"
        # values otherwise shift at a date boundary and freshness stops being
        # deterministic unless a caller hand-builds the adapter.
        as_of = today or self.today or date.today()
        # Workday accepts a single search string, so a multi-title spec pushes only its
        # first term down here; the rest are applied locally after retrieval.
        search_text = target.extra.get("search_text", "")
        postings: list[RawPosting] = []
        seen_paths: list[str] = []

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
                break

            for item in listings:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("externalPath") or "")
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
                    )
                )
                seen_paths.append(path)

            if len(listings) < PAGE_SIZE:
                break

        # Descriptions come one request at a time, so they are capped. Without them the
        # workplace and requirement gates cannot reach a verdict, which is precisely the
        # information this product's hardest search needs -- hence a budget, not zero.
        for posting, path in list(zip(postings, seen_paths, strict=False))[: self.max_details]:
            self._enrich(fetcher, base, posting, path)

        return postings

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
