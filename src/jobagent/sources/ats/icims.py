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
  carries NO date and NO pay field, and its teaser is a few lines, not the description.
- The numeric id in the job URL is the stable identity. The displayed ID ("2026-5819") is
  the tenant's own numbering and is kept as evidence, not used as the key.
- ``Remote`` is Yes or No. Yes means remote; No only means the tenant did not mark it
  remote, which is not the same as onsite, so it maps to UNKNOWN and the description
  decides.

The job page fills the gaps. It embeds one ``<script type="application/ld+json">``
schema.org ``JobPosting`` (verified live 2026-09-20) carrying the full description as
HTML, ``datePosted``, ``employmentType``, ``jobLocation`` with a postal address, and
``baseSalary``. The description replaces the teaser in BOTH ``description_text`` and
``description_html``: the gates judge the text field first, so a teaser left there would
be what gets judged. Each page costs a request, so at most ``max_details`` are read per
board and title matches go first (``prioritize_for_details``); a posting the budget did
not reach stays as the card listed it.

Pay: iCIMS writes ``baseSalary`` with no ``unitText``, so a stated unit is honoured when
present and otherwise the magnitude decides -- a maximum under 500 is hourly, a minimum
of 10,000 or more is yearly, and anything between is read from the description's own
words or left unread. A range is never assumed yearly: 17-25 read as dollars a year would
fail every pay floor.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlencode

from ...domain.models import RawPosting, SalaryRange, WorkplaceType
from ...domain.normalize import parse_salary
from ...domain.text import html_to_text
from ...ports import Fetcher, FetchError
from .base import (
    TERM_SEPARATOR,
    AtsAdapter,
    BoardTarget,
    ats_authority,
    prioritize_for_details,
    require_ok,
)

API = "https://{token}.icims.com/jobs/search"
PAGE_SIZE = 50
_JSON_LD = re.compile(r"<script\b[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)
_PERIODS = ("hour", "day", "week", "month", "year")

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

        # Job pages cost one request each and are capped; title matches go first so the
        # budget buys descriptions for the jobs asked about, not whatever was listed first.
        for posting in prioritize_for_details(postings, terms, self.max_details):
            self._enrich(fetcher, posting)
        return postings

    def _enrich(self, fetcher: Fetcher, posting: RawPosting) -> None:
        try:
            response = fetcher.get(posting.extra["detail_url"])
        except FetchError as error:
            # A blocked host must NOT be swallowed here. Descriptions are what the
            # workplace, requirement and salary gates read, so quietly returning postings
            # without them presents a degraded run as a healthy one.
            if error.blocked:
                raise
            return
        except Exception:  # noqa: BLE001 - a missing job page is not a failed board
            return
        if not response.ok:
            return
        data = _job_posting(response.text)
        if data is None:
            return

        description = data.get("description")
        if isinstance(description, str) and description:
            # Both fields: the gates judge description_text first, and the card's teaser
            # left there would be what gets judged.
            posting.description_html = description
            posting.description_text = html_to_text(description)
        posted = data.get("datePosted")
        if isinstance(posted, str):
            try:
                posting.posted_at = date.fromisoformat(posted[:10])
            except ValueError:
                pass
        kind = data.get("employmentType")
        if not posting.employment_type and isinstance(kind, str) and kind:
            # "FULL_TIME" as the taxonomy spells it: "full time".
            posting.employment_type = kind.replace("_", " ").lower()

        places = data.get("jobLocation")
        names: list[str] = []
        country = None
        for place in places if isinstance(places, list) else [places]:
            address = place.get("address") if isinstance(place, dict) else None
            if not isinstance(address, dict):
                continue
            parts = [address.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            name = ", ".join(p for p in parts if isinstance(p, str) and p)
            if name:
                names.append(name)
                country = country or address.get("addressCountry")
        if names:
            posting.location_raw = "; ".join(names)  # the separator parse_location reads
            if isinstance(country, str) and len(country) == 2:
                posting.country_hint = country.upper()

        posting.salary = (
            _salary(data.get("baseSalary"), data.get("salaryCurrency"), posting.description_text)
            or posting.salary
        )

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


def _job_posting(page: str) -> dict[str, Any] | None:
    """The job page's schema.org JobPosting, or None when it is missing or unreadable.

    The first JSON-LD block is read; it may hold one object or a list of them.
    """
    match = _JSON_LD.search(page)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict) and item.get("@type") == "JobPosting":
            return item
    return None


def _salary(base: Any, currency: Any, text: str) -> SalaryRange | None:
    """``baseSalary`` as a range, read in both schema.org shapes.

    The figures sit either directly on the MonetaryAmount or on its ``value``
    QuantitativeValue. A posting with no ``unitText`` is the norm on iCIMS, so when the
    unit is missing the magnitude decides: a maximum under 500 is an hourly rate, a
    minimum of 10,000 or more is yearly, and anything between is left to the words of the
    description (``parse_salary``), which may find nothing. Never a guess of "year": 17-25
    read as dollars a year would fail every pay floor.
    """
    if not isinstance(base, dict):
        return None
    value = base["value"] if isinstance(base.get("value"), dict) else base
    low, high = value.get("minValue"), value.get("maxValue", value.get("minValue"))
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    unit = str(value.get("unitText") or base.get("unitText") or "").lower()
    if unit in _PERIODS:
        period = unit
    elif high < 500:
        period = "hour"
    elif low >= 10_000:
        period = "year"
    else:
        return parse_salary(text)
    return SalaryRange(
        minimum=float(low), maximum=float(high) if high != low else None,
        currency=str(base.get("currency") or currency or "USD"), period=period,
    )


def _clean(fragment: str) -> str:
    """Text of a small markup fragment: tags dropped, entities decoded, spaces folded."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())
