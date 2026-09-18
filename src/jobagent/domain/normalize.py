"""Turning raw postings into comparable, normalized jobs.

Sources disagree about everything: location is a free-text blob on one platform and a
structured object on another; remote status is a boolean, a tri-state enum, or nothing at
all. Normalization is where that variance is absorbed, so gates and ranking see one shape.

Judgements made here are conservative. Where a signal is genuinely absent the result is
``None`` or ``UNKNOWN`` rather than a guess, because a guess turns into a silent
rejection two layers down.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .models import (
    Job,
    JobSourceRef,
    Location,
    RawPosting,
    SalaryRange,
    WorkplaceType,
)
from .places import (
    AMBIGUOUS_PREFIXES,
    ISO_CODES,
    NON_US_VOCAB,
    PREFIX_ALIASES,
    US_CITY_VOCAB,
)
from .taxonomy import Taxonomy
from .text import collapse_whitespace, contains_phrase, find_phrase, html_to_text, snippet

US_STATES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}

_STATE_ABBREVS = set(US_STATES.values())

# Phrases that positively establish a posting is US-scoped, including the remote-but-US
# forms that a bare "Remote" would otherwise leave unverifiable.
US_PHRASES: list[str] = [
    "united states", "usa", "u.s.a.", "u.s.", "us based", "us-based", "united states of america",
    "remote us", "remote - us", "remote (us)", "us remote", "anywhere in the us",
    "anywhere in the united states", "us only", "domestic us",
    "authorized to work in the united states", "must reside in the us",
    "us - remote", "us-remote", "guam", "u.s. virgin islands",
]

# The country, region and city vocabularies live in ``places``; they are large and they
# are data. What stays here is the resolution order.
_PREFIXED = re.compile(r"(?<![A-Za-z])([A-Z]{2})\s*[-\u2013\u2014]\s*([A-Za-z][^,;|]*)")

_CITY_STATE = re.compile(r"([A-Za-z .'-]+),\s*([A-Z]{2})\b")


def detect_country(text: str, *, prefixes: bool = True) -> tuple[str | None, str]:
    """Infer an ISO country code from free text.

    Returns ``(code, evidence)``; ``code`` is None when nothing decisive was found, which
    the US gate reports as UNVERIFIABLE rather than guessing.

    Resolution order, and why:

    1. US phrases, then "City, ST", then spelled-out states. US signals are checked first
       and win ties: "Remote (US) or Canada" is open to US applicants, and "Paris, TX"
       is Texas however famous the other Paris is.
    2. Every non-US country, region and city, as one leftmost-longest search.
    3. US cities by bare name. After the non-US pass, so "Paris or Austin" fails the
       US-only gate -- the safe error.
    4. A two-letter prefix ("FR - Sophia Antipolis") when nothing above recognised the
       city. A code that is both a state and a country ("CA", "PA", "IN") with an
       unrecognised city is reported as unknown rather than guessed either way.

    ``prefixes`` is off when the text is a job title: "HR - Business Partner" is not a
    Croatian posting, and "QA - Remote" is not in Qatar.
    """
    if not text:
        return None, ""
    lowered = text.lower()

    for phrase in US_PHRASES:
        span = find_phrase(lowered, phrase)
        if span:
            return "US", snippet(text, span, 24)

    match = _CITY_STATE.search(text)
    if match and match.group(2) in _STATE_ABBREVS:
        return "US", match.group(0)

    for name in US_STATES:
        span = find_phrase(lowered, name)
        if span:
            return "US", snippet(text, span, 24)

    found = NON_US_VOCAB.find(text)
    if found:
        return found[0], snippet(text, found[1], 24)

    found = US_CITY_VOCAB.find(text)
    if found:
        return "US", snippet(text, found[1], 24)

    if prefixes:
        for match in _PREFIXED.finditer(text):
            code = PREFIX_ALIASES.get(match.group(1), match.group(1))
            rest = match.group(2).strip().lower()
            if rest in {"remote", "hybrid", "onsite", "on-site", "virtual", ""}:
                continue
            if code == "US":
                return "US", match.group(0)
            if code in AMBIGUOUS_PREFIXES:
                return None, ""
            if code in _STATE_ABBREVS:
                return "US", match.group(0)
            if code in ISO_CODES:
                return code, match.group(0)

    return None, ""


def parse_location(raw: str, country_hint: str | None = None) -> Location:
    """Parse a free-text location string into a structured Location."""
    raw = collapse_whitespace(raw or "")
    if not raw:
        return Location(raw="", country=(country_hint or None))

    # A posting open in several places lists them semicolon-separated. The first is the
    # one that becomes the structured city and region; scanning the whole list found
    # whichever state sorts first alphabetically and took everything before it as the
    # city. ``raw`` keeps every place, which is what the location gate reads.
    primary = raw.split(";")[0].strip()

    city = region = None
    match = _CITY_STATE.search(primary)
    if match and match.group(2) in _STATE_ABBREVS:
        city = match.group(1).strip()
        region = match.group(2)

    if region is None:
        # Spelled-out state names are folded to their abbreviation so that "Dallas, TX"
        # and "Dallas, Texas" produce the same region and therefore the same identity.
        for name, abbrev in US_STATES.items():
            if find_phrase(primary.lower(), name):
                region = abbrev
                head = primary.lower().split(name)[0].strip(" ,")
                if not city and head and len(head) < 60:
                    city = head.title()
                break

    country = (country_hint or "").upper() or None
    if not country:
        country, _ = detect_country(raw)
    if region and not country:
        country = "US"

    if not city and not region:
        head = primary.split(",")[0].strip()
        # "Remote" is a workplace arrangement, not a city; recording it as one would make
        # a city-level location gate match on the word "Remote".
        if head and not contains_phrase(head, "remote") and len(head) < 60:
            city = head

    return Location(raw=raw, city=city, region=region, country=country)


def infer_workplace(
    text: str, taxonomy: Taxonomy, hint: WorkplaceType = WorkplaceType.UNKNOWN
) -> tuple[WorkplaceType, str]:
    """Infer onsite/hybrid/remote from text, returning the deciding evidence.

    A structured ``hint`` from the source is trusted over text inference -- Lever and
    Ashby publish a real tri-state field, and second-guessing it with regex would be
    strictly worse.

    Order matters among the text checks. "Hybrid" is tested before "remote" because
    hybrid postings almost always contain the word "remote"; negations are tested before
    the positive remote phrases so "not a remote role" does not read as remote.
    """
    if hint is not WorkplaceType.UNKNOWN:
        return hint, "source field"
    if not text:
        return WorkplaceType.UNKNOWN, ""

    for phrase in taxonomy.hybrid_phrases:
        span = find_phrase(text, phrase)
        if span:
            return WorkplaceType.HYBRID, snippet(text, span, 40)

    for phrase in taxonomy.onsite_phrases:
        span = find_phrase(text, phrase)
        if span:
            return WorkplaceType.ONSITE, snippet(text, span, 40)

    for phrase in taxonomy.remote_phrases:
        span = find_phrase(text, phrase)
        if span:
            return WorkplaceType.REMOTE, snippet(text, span, 40)

    return WorkplaceType.UNKNOWN, ""


# Cues that a number nearby is actually pay. Without one, a "$25 lunch allowance",
# a "401k match" and a "$5,000,000 annual budget" all read as compensation -- and the
# first two are far more common in postings than a stated salary is.
_PAY_CUES = re.compile(
    r"""(salary|salaries|compensation|pay\s*(?:range|rate|band)?|base\s+pay|base\s+salary
        |wage|hourly\s*(?:rate)?|annual(?:ized)?\s+(?:pay|salary|compensation)
        |per\s+(?:hour|year|annum|month|week)|/\s*(?:hr|hour|yr|year|mo|month)
        |starting\s+at|range\s+of|earn(?:s|ing)?\s+up\s+to|\bDOE\b)""",
    re.IGNORECASE | re.VERBOSE,
)

# Retirement-plan names are numerals followed by a letter and would otherwise parse as
# money. "401k" appears in a large share of US postings and became $401,000.
_NOT_MONEY = re.compile(r"\b(401\s*[kK]|403\s*[bB]|457\s*[bB]|529)\b")

_MONEY = re.compile(
    r"""(?P<cur>[$€£])?\s*
        (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
        \s*(?P<suffix>k\b|m\b)?""",
    re.IGNORECASE | re.VERBOSE,
)

# A stated range: two figures joined by a dash, "to", or "up to". This is what a real
# pay disclosure looks like, and preferring it avoids pairing a salary with an unrelated
# number elsewhere in the text.
_RANGE = re.compile(
    r"""(?P<cur>[$€£])?\s*(?P<low>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*
        (?P<lowsuf>[kKmM])?\s*(?:-|–|—|to|through|up\s+to)\s*
        (?P<cur2>[$€£])?\s*(?P<high>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*
        (?P<highsuf>[kKmM])?""",
    re.IGNORECASE | re.VERBOSE,
)

_PERIOD_HINTS = [
    (["per hour", "/hour", "/hr", "hourly", "an hour", "per hr"], "hour"),
    (["per day", "/day", "daily"], "day"),
    (["per week", "/week", "weekly"], "week"),
    (["per month", "/month", "monthly"], "month"),
    (["per year", "/year", "annually", "per annum", "annual", "/yr"], "year"),
]

_PAY_WINDOW = 120  # characters either side of a figure that may carry the pay cue


def _scale(value: str, suffix: str | None) -> float | None:
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    if suffix and suffix.lower() == "k":
        number *= 1_000
    elif suffix and suffix.lower() == "m":
        number *= 1_000_000
    return number


def _period_for(window: str, default: str = "year") -> str:
    lowered = window.lower()
    for needles, name in _PERIOD_HINTS:
        if any(needle in lowered for needle in needles):
            return name
    return default


def parse_salary(text: str) -> SalaryRange | None:
    """Extract an advertised pay range from free text.

    Returns None unless a figure sits near an explicit pay cue. That bar is deliberately
    high: most postings state no pay, and a parser that guesses turns a wellness stipend
    into a salary and then lets a pay-floor gate reject the job over it -- an invented
    number causing a real rejection.
    """
    if not text:
        return None

    masked = _NOT_MONEY.sub(" ", text)

    # A stated range near a pay cue is the strongest signal, so it wins outright.
    for match in _RANGE.finditer(masked):
        window = masked[max(0, match.start() - _PAY_WINDOW):match.end() + _PAY_WINDOW]
        has_currency = bool(match.group("cur") or match.group("cur2"))
        if not (_PAY_CUES.search(window) or has_currency):
            continue
        low = _scale(match.group("low"), match.group("lowsuf"))
        high = _scale(match.group("high"), match.group("highsuf"))
        if low is None or high is None or high < low:
            continue
        if not has_currency and not (match.group("lowsuf") or "," in match.group("low")):
            continue  # bare "3 to 5" is years of experience, not money
        return SalaryRange(
            minimum=low, maximum=high if high != low else None,
            currency=_currency(match.group("cur") or match.group("cur2")),
            period=_period_for(window),
        )

    # Otherwise accept a single figure, and only when a pay cue is genuinely nearby.
    for match in _MONEY.finditer(masked):
        suffix = match.group("suffix")
        number_text = match.group("num")
        if not match.group("cur") and not suffix and "," not in number_text:
            continue
        window = masked[max(0, match.start() - _PAY_WINDOW):match.end() + _PAY_WINDOW]
        if not _PAY_CUES.search(window):
            continue
        value = _scale(number_text, suffix)
        if value is None:
            continue
        return SalaryRange(
            minimum=value, maximum=None, currency=_currency(match.group("cur")),
            period=_period_for(window),
        )

    return None


def _currency(symbol: str | None) -> str:
    return {"€": "EUR", "£": "GBP"}.get(symbol or "", "USD")


def detect_seniority_levels(title: str, taxonomy: Taxonomy) -> list[tuple[str, str]]:
    """Every seniority level named in a title, with the word that named it.

    A title can name two: "Senior Staff Accountant" and "Senior Director of Finance" both
    do. Returning only the highest meant a search excluding "senior" let both through,
    because the title reported "staff" and "management" respectively.

    Reads the title only. Descriptions routinely mention reporting lines ("partners with
    senior leadership") that say nothing about the level of the advertised role.
    """
    if not title:
        return []
    found: list[tuple[str, str]] = []
    for label in taxonomy.seniority:
        for term in taxonomy.seniority[label]:
            # Short tokens are noisy, so only the unambiguous ones are matched.
            if len(term) <= 2 and term not in {"sr", "jr", "ii", "iv", "vp"}:
                continue
            span = find_phrase(title, term)
            if span:
                found.append((label, title[span[0]:span[1]]))
                break
    return found


def detect_seniority(title: str, taxonomy: Taxonomy) -> tuple[str | None, str]:
    """The highest seniority level named in a title, for ranking and display.

    Filtering uses ``detect_seniority_levels`` instead: for an exclusion, every level
    named matters, not just the top one.
    """
    levels = detect_seniority_levels(title, taxonomy)
    if not levels:
        return None, ""
    order = list(taxonomy.seniority)
    highest = max(levels, key=lambda item: order.index(item[0]))
    return highest


def detect_employment_type(text: str, taxonomy: Taxonomy) -> str | None:
    """Identify full-time/part-time/contract and similar from text."""
    if not text:
        return None
    for label, terms in taxonomy.employment_types.items():
        for term in terms:
            if contains_phrase(text, term):
                return label
    return None


def normalize_company(name: str) -> str:
    """Canonical form of a company name, for duplicate detection.

    Strips legal suffixes and punctuation so "Acme, Inc." and "Acme Incorporated" collapse
    to the same key.
    """
    if not name:
        return ""
    text = name.lower().strip()
    text = re.sub(r"[^\w\s&]", " ", text)
    suffixes = {
        "inc", "incorporated", "llc", "l l c", "ltd", "limited", "corp", "corporation",
        "co", "company", "plc", "gmbh", "sa", "ag", "nv", "bv", "srl", "pty", "holdings",
        "group", "the",
    }
    words = [w for w in text.split() if w not in suffixes]
    return " ".join(words) or text.strip()


def normalize_title(title: str) -> str:
    """Canonical form of a job title, for duplicate detection.

    Drops requisition ids, bracketed qualifiers and trailing location suffixes, which are
    the usual reason one role looks like two postings. The location suffix is genuinely
    removed rather than merely flattened: leaving it turned "Accountant - Dallas, TX" and
    "Accountant" into different identities, so the same job was shown twice.
    """
    if not title:
        return ""
    text = title_core(title).lower()
    text = re.sub(r"[^\w\s+#]", " ", text)
    return collapse_whitespace(text)


def title_core(title: str) -> str:
    """The title without its qualifiers, in the user's own casing.

    Drops bracketed asides, requisition ids and one trailing place or arrangement
    suffix, and keeps everything else as written. This is what the tool offers back when
    a user wants to exclude "titles like this one": the identity form from
    ``normalize_title`` is lowercased and stripped of punctuation, which reads badly as
    a rule.
    """
    if not title:
        return ""
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title)
    text = re.sub(
        r"\b(?:req|requisition|job|id)[\s#:-]*\w*\d+\w*\b", " ", text, flags=re.IGNORECASE
    )
    text = _strip_location_suffix(text)
    return collapse_whitespace(text).strip(" -–—|,:")


# "Accountant - Dallas, TX", "Accountant | Remote", "Accountant, Austin TX"
_TITLE_SEPARATOR = re.compile(r"\s*[-–—|,]\s*")
_PLACE_SHAPE = re.compile(r"^[A-Za-z .']{2,30}(?:,\s*[A-Za-z]{2}|,\s*[A-Za-z .']{4,20})?$")
# "Dallas, TX" and "Austin TX": a place followed by a state abbreviation.
_CITY_STATE_PAIR = re.compile(r"^[A-Za-z .']{2,30}?[,\s]\s*([A-Za-z]{2})$")
_ARRANGEMENTS = {"remote", "hybrid", "onsite", "on-site"}


def _strip_location_suffix(text: str) -> str:
    """Remove one trailing place or work-arrangement qualifier from a title.

    Separators are tried from the right, so the qualifier is the LAST segment that
    names a place. Matching from the left cut a hyphenated title at its own hyphen:
    "Front-End Developer, Austin TX" became "Front", and every "Front-..." role at one
    employer and place collapsed into a single identity.
    """
    separators = list(_TITLE_SEPARATOR.finditer(text))
    for index in range(len(separators) - 1, -1, -1):
        head = text[: separators[index].start()].strip()
        tail = text[separators[index].end():].strip()
        if not head or not tail or not _names_a_place(tail):
            continue
        # "London, United Kingdom" and "Dallas, TX" are one place in two segments. Take
        # the segment before as well, but only when it is itself a place: "End
        # Developer" before "Austin TX" is the role, not the city.
        if index > 0:
            previous = separators[index - 1]
            between = text[previous.end(): separators[index].start()].strip()
            wider = text[previous.end():].strip()
            pair = _CITY_STATE_PAIR.match(wider)
            if _names_a_place(between) or (pair and pair.group(1).upper() in _STATE_ABBREVS):
                head = text[: previous.start()].strip()
        return head or text
    return text


def _names_a_place(tail: str) -> bool:
    """Whether a title's trailing segment is a place or arrangement, not part of the role.

    "Accountant - Payroll" must keep the part that distinguishes the role, so a tail
    counts only when it is an arrangement word, a "City, ST" pair, or something the
    place vocabulary recognises.
    """
    lowered = tail.lower()
    if lowered in _ARRANGEMENTS or lowered in {"us", "usa", "united states"}:
        return True
    if not _PLACE_SHAPE.match(tail):
        return False
    pair = _CITY_STATE_PAIR.match(tail)
    if pair and pair.group(1).upper() in _STATE_ABBREVS:
        return True
    if lowered in US_STATES:
        return True
    # The whole tail must be the place, not merely contain one: "Georgia Operations" is
    # a business unit that happens to name a state, and stripping it would offer
    # "Analyst" as the title to exclude when the user dismisses that job.
    for vocabulary in (US_CITY_VOCAB, NON_US_VOCAB):
        hit = vocabulary.find(tail)
        if hit and hit[1] == (0, len(tail)):
            return True
    return False


def build_job(
    posting: RawPosting, taxonomy: Taxonomy, identity: str, seen_at: datetime | None = None
) -> Job:
    """Normalize one raw posting into a Job.

    ``identity`` comes from the dedup layer, which needs normalized fields to compute it;
    it is passed in rather than derived here to keep that dependency one-directional.
    """
    description = posting.description_text or html_to_text(posting.description_html)
    location = parse_location(posting.location_raw, posting.country_hint)

    # Workplace evidence is drawn from title and location first: those are short and
    # deliberate, while descriptions mention remote work for all sorts of incidental
    # reasons ("our remote-friendly culture") that do not describe this role.
    headline = " ".join(filter(None, [posting.title, posting.location_raw]))
    workplace, _ = infer_workplace(headline, taxonomy, posting.workplace_hint)
    if workplace is WorkplaceType.UNKNOWN:
        workplace, _ = infer_workplace(description[:4000], taxonomy)

    if location.country is None:
        # Title and location only. Scanning the description let "we are a United States
        # company" in the boilerplate of a Toronto posting resolve the job to the US and
        # slip past a US-only gate.
        # The location alone was already tried by parse_location; this adds the title.
        # Prefix parsing is off for titles: "HR - Business Partner" is not Croatian.
        country, _ = detect_country(posting.title, prefixes=False)
        if country:
            location = Location(
                raw=location.raw, city=location.city, region=location.region, country=country
            )

    salary = posting.salary or parse_salary(description[:6000])
    employment = posting.employment_type or detect_employment_type(
        f"{posting.title}\n{description[:2000]}", taxonomy
    )

    ref = JobSourceRef(
        source=posting.source,
        url=posting.url,
        external_id=posting.external_id,
        authority=posting.authority,
        first_seen=seen_at,
        last_seen=seen_at,
    )

    return Job(
        identity=identity,
        title=collapse_whitespace(posting.title),
        company=collapse_whitespace(posting.company),
        url=posting.apply_url or posting.url,
        description_text=description,
        location=location,
        workplace=workplace,
        employment_type=employment,
        department=posting.department,
        salary=salary,
        posted_at=posting.posted_at,
        authority=posting.authority,
        sources=[ref],
        first_seen=seen_at,
        last_seen=seen_at,
    )


def parse_relative_date(text: str, today: date) -> date | None:
    """Parse human-written posting dates such as "Posted 5 Days Ago".

    Workday publishes this instead of a timestamp, and a freshness filter is useless
    without it.
    """
    if not text:
        return None
    lowered = text.lower()
    if "today" in lowered or "just posted" in lowered:
        return today
    if "yesterday" in lowered:
        return date.fromordinal(today.toordinal() - 1)
    match = re.search(r"(\d+)\+?\s*(day|week|month|year)s?\s*ago", lowered)
    if not match:
        return None
    amount = int(match.group(1))
    unit_days = {"day": 1, "week": 7, "month": 30, "year": 365}[match.group(2)]
    return date.fromordinal(max(1, today.toordinal() - amount * unit_days))
