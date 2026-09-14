"""Discovering employer boards from the Common Crawl index.

The registry is this product's entire reach, and until now it grew only from a vendored
seed, a careers-page scrape, or the user typing a URL. That caps discovery at companies
somebody already knew about.

Common Crawl closes that gap. It publishes an index of the URLs it has crawled, queryable
by domain pattern, free, with no key, no account and no query budget -- and it permits
programmatic querying because that is what it exists for. Ask it for everything under
``jobs.lever.co`` and it answers with the boards, which is precisely the cross-company
index no ATS will provide.

One capability here has no substitute. Every Workday tenant is its own hostname, so no
``site:`` query on any search engine can enumerate them; a Common Crawl subdomain wildcard
(``*.myworkdayjobs.com/``) can. Workday is where large non-tech employers post the onsite
US roles this tool covers worst, and its tenancy triple is not guessable from a company
name -- so this is the only practical route to those boards at scale.

What it is not: fresh. Crawls run every month or two, so a board that appeared last week
is missing. That matters far less than it sounds, because what is being harvested here is
*which employers have boards* -- a durable fact -- while the live postings still come from
the ATS APIs. A company that adopted Greenhouse six weeks ago is a perfectly good addition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..ports import Fetcher, FetchError
from .discovery import BoardRef, extract_board

INDEX_HOST = "https://index.commoncrawl.org"
COLLECTIONS_URL = f"{INDEX_HOST}/collinfo.json"

# Common Crawl asks callers not to overload the index server, and it is a volunteer-funded
# non-profit. A full sweep is a background chore, not something to race.
POLITE_INTERVAL = 1.0

# URL patterns per platform. A trailing "/*" matches paths under one host; a leading "*."
# matches subdomains, which is the form that enumerates Workday tenants.
PLATFORM_PATTERNS: dict[str, list[str]] = {
    "greenhouse": ["job-boards.greenhouse.io/*", "boards.greenhouse.io/*"],
    "lever": ["jobs.lever.co/*"],
    "ashby": ["jobs.ashbyhq.com/*"],
    "workday": ["*.myworkdayjobs.com/"],
    "smartrecruiters": ["careers.smartrecruiters.com/*", "jobs.smartrecruiters.com/*"],
    "workable": ["apply.workable.com/*"],
}


@dataclass
class DiscoveryOutcome:
    """What one sweep found."""

    pattern: str
    crawl: str
    start_page: int = 0
    urls_seen: int = 0
    boards_found: int = 0
    boards_new: int = 0
    pages_read: int = 0
    total_pages: int | None = None
    stopped_early: bool = False
    note: str = ""
    boards: list[BoardRef] = field(default_factory=list)

    @property
    def next_page(self) -> int:
        """Where the next sweep should resume.

        Counted from where this sweep started, not from zero: a resumed sweep reads only
        the pages that remain, so ``pages_read`` alone would rewind the cursor.
        """
        return self.start_page + self.pages_read

    @property
    def complete(self) -> bool:
        """True only when the cursor reached the end of the index for this pattern.

        A sweep that stopped on an error has not finished the pattern, however far it
        got. Saying otherwise would park the cursor as done and leave the remaining pages
        unread for the life of the crawl.
        """
        if self.stopped_early or self.total_pages is None:
            return False
        return self.next_page >= self.total_pages

    def describe(self) -> str:
        scope = f"{self.next_page}/{self.total_pages}" if self.total_pages else \
            str(self.next_page)
        return (
            f"{self.pattern}: {self.urls_seen} urls through page {scope}, "
            f"{self.boards_found} boards ({self.boards_new} new)"
        )


@dataclass
class CommonCrawlDiscovery:
    """Queries the Common Crawl CDX index for employer board URLs."""

    fetcher: Fetcher
    crawl: str | None = None

    def available_crawls(self) -> list[str]:
        """Crawl ids, newest first.

        Fetched rather than hardcoded: crawl ids carry a year and week, so any constant
        would start silently pointing at stale data within months.
        """
        response = self.fetcher.get(COLLECTIONS_URL)
        if not response.ok:
            raise FetchError(
                f"could not list Common Crawl collections (HTTP {response.status})",
                status=response.status,
            )
        payload = response.json() or []
        if not isinstance(payload, list):
            return []
        return [str(entry.get("id")) for entry in payload if isinstance(entry, dict)
                and entry.get("id")]

    def latest_crawl(self) -> str:
        if self.crawl:
            return self.crawl
        crawls = self.available_crawls()
        if not crawls:
            raise FetchError("Common Crawl published no collections")
        self.crawl = crawls[0]
        return self.crawl

    def _index_url(self, crawl: str) -> str:
        return f"{INDEX_HOST}/{crawl}-index"

    def page_count(self, pattern: str, crawl: str, page_size: int = 5) -> int | None:
        """How many pages the index holds for a pattern.

        Asked up front so a sweep can report progress and resume, rather than discovering
        the end by walking into it.
        """
        response = self.fetcher.get(
            self._index_url(crawl),
            params={"url": pattern, "output": "json", "showNumPages": "true",
                    "pageSize": page_size},
        )
        if not response.ok:
            return None
        try:
            payload = json.loads(response.text.strip().splitlines()[0])
        except (ValueError, IndexError):
            return None
        pages = payload.get("pages") if isinstance(payload, dict) else None
        return int(pages) if isinstance(pages, int) else None

    def sweep(
        self, pattern: str, *, crawl: str | None = None, start_page: int = 0,
        max_pages: int = 5, page_size: int = 5, known: set[tuple[str, str]] | None = None,
    ) -> DiscoveryOutcome:
        """Read pages of the index for one pattern and extract board references.

        Bounded by ``max_pages`` so a sweep is a resumable chore rather than an
        open-ended crawl of a shared public service.
        """
        crawl = crawl or self.latest_crawl()
        known = known if known is not None else set()
        outcome = DiscoveryOutcome(pattern=pattern, crawl=crawl, start_page=start_page)
        outcome.total_pages = self.page_count(pattern, crawl, page_size)

        seen_keys: set[tuple[str, str]] = set()
        for offset in range(max_pages):
            page = start_page + offset
            if outcome.total_pages is not None and page >= outcome.total_pages:
                break
            try:
                response = self.fetcher.get(
                    self._index_url(crawl),
                    params={"url": pattern, "output": "json", "page": page,
                            "pageSize": page_size, "filter": "=status:200"},
                )
            except FetchError as error:
                outcome.note = f"stopped at page {page}: {error}"
                outcome.stopped_early = True
                break

            if response.status == 404:
                # The index answers 404 when a pattern has no captures at all, which is
                # an empty result rather than a failure.
                outcome.note = f"no captures for this pattern (HTTP 404 at page {page})"
                outcome.stopped_early = True
                break
            if not response.ok:
                outcome.note = f"stopped at page {page}: HTTP {response.status}"
                outcome.stopped_early = True
                break

            rows = _parse_cdx(response.text)
            if not rows:
                # Silence here would be indistinguishable from "swept and found nothing",
                # and the progress table would show a stalled cursor with no reason.
                outcome.note = f"page {page} came back empty"
                outcome.stopped_early = True
                break
            outcome.pages_read += 1

            for url in rows:
                outcome.urls_seen += 1
                board = extract_board(url)
                if board is None:
                    continue
                key = board.key()
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                outcome.boards_found += 1
                if key not in known:
                    outcome.boards_new += 1
                    outcome.boards.append(board)

        return outcome


def _parse_cdx(text: str) -> list[str]:
    """Pull URLs out of a CDXJ response.

    Each line is its own JSON object. A malformed line is skipped rather than failing the
    page: the index is a public service and one bad record should not cost a whole sweep.
    """
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        url = record.get("url") if isinstance(record, dict) else None
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


def patterns_for(platforms: list[str] | None = None) -> list[tuple[str, str]]:
    """(platform, pattern) pairs to sweep."""
    wanted = platforms or list(PLATFORM_PATTERNS)
    pairs: list[tuple[str, str]] = []
    for platform in wanted:
        for pattern in PLATFORM_PATTERNS.get(platform, []):
            pairs.append((platform, pattern))
    return pairs
