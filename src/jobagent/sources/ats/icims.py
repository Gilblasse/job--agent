"""iCIMS career portals.

Read from the public search page every iCIMS tenant serves at
``https://{token}.icims.com/jobs/search``: one HTML page of up to fifty job cards per
request, filtered server-side by ``searchKeyword`` and paged by a zero-based ``pr``.
There is no public JSON API -- the vendor's Job Portal API needs partner credentials -- so
the cards are read from the markup. robots.txt was checked live on 2026-09-20 on three
tenants (``careers-48forty``, ``external-92y``, ``resume-chesterton``); each disallows
only ``/jobs/*referral``, ``/jobs/*login``, ``/jobs/*candidate``, ``/jobs/reminder`` and
``/connect*``. The search and job pages are allowed.

With aggregators out of scope, iCIMS is where many large non-tech US employers post the
onsite roles no other free source covers, so its cost -- several list pages per board --
is accepted deliberately.

Three things about the shape that are not obvious from one card:

- A card carries the title, a ``US-FL-Bartow`` style location, the displayed requisition
  ID, a teaser paragraph and a few labelled fields (Category, Position Type, Remote). It
  carries NO date; the job page does, and a later pass reads it.
- The numeric id in the job URL is the stable identity. The displayed ID ("2026-5819") is
  the tenant's own numbering and is kept as evidence, not used as the key.
- ``Remote`` is Yes or No. Yes means remote; No only means the tenant did not mark it
  remote, which is not the same as onsite, so it maps to UNKNOWN and the description
  decides.

What the card does not publish: pay, and the full description. Pay is inferred from the
teaser downstream when the tenant writes it there.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from ...domain.models import RawPosting, WorkplaceType
from ...domain.text import html_to_text
from ...ports import Fetcher, FetchError
from .base import TERM_SEPARATOR, AtsAdapter, BoardTarget, ats_authority, require_ok

API = "https://{token}.icims.com/jobs/search"
PAGE_SIZE = 50

_TABLE = "iCIMS_JobsTable"
_CARD = 'class="iCIMS_JobCardItem"'
_ANCHOR = re.compile(r"<a\b[^>]*\biCIMS_Anchor\b[^>]*>")
_HREF = re.compile(r'href="([^"]*)"')
_JOB_ID = re.compile(r"/jobs/(\d+)/")
_TITLE = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.S)
# The header row and the field list both spell a field as a label then a value span.
_HEADER = re.compile(r'field-label">([^<]*)</span>\s*<span\b[^>]*>([^<]*)</span>')
_FIELD = re.compile(r"<dt\b[^>]*>([^<]*)</dt>\s*<dd\b[^>]*>\s*<span\b[^>]*>([^<]*)</span>")
_TEASER = re.compile(r'<div class="[^"]*\bdescription\b[^"]*">(.*?)</div>', re.S)
_PAGE_OF = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")


@dataclass
class IcimsAdapter(AtsAdapter):
    name: str = "icims"
    probe_tokens: tuple[str, ...] = ("careers-48forty", "external-92y", "resume-chesterton")
    costly: bool = True
    server_search: bool = True
    max_pages: int = 3
    max_terms: int = 4
    max_details: int = 20  # job-page reads per board, spent by the detail pass

    def fetch_board(
        self, fetcher: Fetcher, target: BoardTarget, today: date | None = None
    ) -> list[RawPosting]:
        # One keyword per query, so each wanted term is its own search: sending only the
        # first narrowed discovery server-side, where no local filter can recover a job
        # that was never retrieved.
        raw_terms = target.extra.get("search_text", "")
        terms = [t for t in raw_terms.split(TERM_SEPARATOR) if t][: self.max_terms]

        postings: list[RawPosting] = []
        seen: set[str] = set()
        for term in terms or [""]:
            for page in range(self.max_pages):
                response = fetcher.get(_search_url(target.token, term, page))
                if page == 0:
                    require_ok(response, f"icims board {target.token!r}")
                elif not response.ok:
                    # A later page failing is not end-of-results; reporting page one as
                    # the whole board would present partial discovery as full coverage.
                    raise FetchError(
                        f"icims board {target.token!r}: HTTP {response.status} on page "
                        f"{page + 1} of pagination",
                        status=response.status,
                    )

                cards = _cards(response.text)
                for card in cards:
                    if card["id"] in seen:
                        continue  # one job can answer several terms
                    seen.add(card["id"])
                    postings.append(self._posting(card, target))

                # A short page ends the query, and so does the page header's own count:
                # a full last page otherwise costs one more request that comes back empty.
                if len(cards) < PAGE_SIZE or _is_last_page(response.text):
                    break
        return postings

    def _posting(self, card: dict[str, str], target: BoardTarget) -> RawPosting:
        href = card["href"]
        url = href.split("?", 1)[0]  # the job page without its iframe flag
        location = card.get("Location", "")
        return RawPosting(
            source=self.name,
            # Namespaced by tenant: the path id is unique only within one portal.
            external_id=f"{target.token}:{card['id']}",
            title=card["title"],
            company=target.company,
            url=url,
            description_text=card["teaser"],
            location_raw=location,
            country_hint="US" if location.startswith("US-") else None,
            workplace_hint=(
                WorkplaceType.REMOTE if card.get("Remote", "").lower() == "yes"
                else WorkplaceType.UNKNOWN
            ),
            employment_type=card.get("Position Type") or None,
            department=card.get("Category") or None,
            authority=ats_authority(url, target.domain),
            extra={"detail_url": href, "requisition": card.get("ID", "")},
        )


def _search_url(token: str, term: str, page: int) -> str:
    """The search page for one term and one zero-based page.

    The query is folded into the URL rather than passed as params: the fetcher's robots
    check reads the URL as given, so this way it judges the exact request being made.
    """
    query = {"ss": "1", "in_iframe": "1", "searchRelation": "keyword_all"}
    if term:
        query["searchKeyword"] = term
    query["pr"] = str(page)
    return f"{API.format(token=token)}?{urlencode(query)}"


def _cards(page: str) -> list[dict[str, str]]:
    """Each job card as a flat dict: id, href, title, teaser, and every labelled field.

    A page that has the job table but yields no cards is a layout change, not an empty
    board, and is raised as such so the board is reported as a parse error.
    """
    cards: list[dict[str, str]] = []
    # ponytail: cards are sibling <li> with no nesting, so a split on the class marker and
    # a cut at the first </li> isolates each one; a nested <li> would break it. Upgrade
    # path: an html.parser state machine.
    for chunk in page.split(_CARD)[1:]:
        chunk = chunk.split("</li>", 1)[0]
        anchor = _ANCHOR.search(chunk)
        href = _HREF.search(anchor.group(0)) if anchor else None
        job_id = _JOB_ID.search(href.group(1)) if href else None
        title = _TITLE.search(chunk)
        if not job_id or not title:
            continue
        card = {_clean(k): _clean(v) for k, v in _HEADER.findall(chunk) + _FIELD.findall(chunk)}
        teaser = _TEASER.search(chunk)
        card.update(
            id=job_id.group(1),
            href=html.unescape(href.group(1)),
            title=_clean(title.group(1)),
            teaser=html_to_text(teaser.group(1)) if teaser else "",
        )
        cards.append(card)
    if not cards and _TABLE in page:
        raise ValueError("icims: job table present but no cards parsed")
    return cards


def _is_last_page(page: str) -> bool:
    """The results header says "Page 1 of 7"; a page at or past its total is the last."""
    match = _PAGE_OF.search(page)
    return bool(match) and int(match.group(1)) >= int(match.group(2))


def _clean(fragment: str) -> str:
    """Text of a small markup fragment: tags dropped, entities decoded, spaces folded."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())
