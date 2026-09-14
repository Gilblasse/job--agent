"""Turning a careers URL into a board this tool can read.

With no cross-company search anywhere in scope, the registry is the product's reach. This
module is how that registry grows: it recognizes an ATS board from its URL, and it can
read an employer's own careers page to find the board embedded in it.

Every pattern here was derived from a real board URL. The parsing is deliberately strict
-- a wrong token produces a board that returns nothing, which looks exactly like a company
with no openings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..ports import Fetcher

# Path segments that are part of the platform's own routing, never a tenant name.
RESERVED = {
    "api", "v1", "v2", "v3", "accounts", "jobs", "job", "j", "embed", "search", "careers",
    "en-us", "en", "widget", "board", "boards", "www", "static", "assets",
}


@dataclass(frozen=True)
class BoardRef:
    """A board identified from a URL."""

    platform: str
    token: str
    url: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return (self.platform, self.token)


def _clean(token: str) -> str:
    return token.strip().strip("/").split("?")[0].split("#")[0]


_WORKDAY = re.compile(
    r"^(?P<tenant>[\w-]+)\.wd(?P<wd>\d+)\.myworkdayjobs\.com$", re.IGNORECASE
)


def extract_board(url: str) -> BoardRef | None:
    """Identify the ATS board a URL points at, if any."""
    if not url or "://" not in url:
        url = f"https://{url}" if url else ""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [_clean(s) for s in parts.path.split("/") if _clean(s)]
    # Case is kept alongside the lowercased form: most platforms use lowercase slugs, but
    # SmartRecruiters identifiers are case-sensitive, and lowercasing one yields a token
    # that fetches nothing while looking perfectly valid.
    first_raw = segments[0] if segments else ""
    first = first_raw.lower()

    # Workday's identity is a triple, not a name: the tenant, the numbered instance it
    # lives on, and the site. Any one of them missing makes the board unreachable.
    workday = _WORKDAY.match(host)
    if workday:
        usable = [s for s in segments if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        site = next((s for s in usable if s.lower() not in RESERVED), usable[0] if usable else "")
        if site:
            return BoardRef(
                "workday", workday.group("tenant").lower(), url,
                {"wd": workday.group("wd"), "site": site},
            )
        return None

    if host.endswith("greenhouse.io"):
        if first and first not in RESERVED:
            return BoardRef("greenhouse", first, url)
        subdomain = host.split(".")[0]
        if subdomain not in {"boards", "job-boards", "api", "boards-api", "my"}:
            return BoardRef("greenhouse", subdomain, url)
        return None

    if host.endswith("lever.co"):
        return BoardRef("lever", first, url) if first and first not in RESERVED else None

    if host.endswith("ashbyhq.com"):
        if first and first not in RESERVED:
            return BoardRef("ashby", first, url)
        subdomain = host.split(".")[0]
        return BoardRef("ashby", subdomain, url) if subdomain not in {"jobs", "api"} else None

    if host.endswith("smartrecruiters.com"):
        return (
            BoardRef("smartrecruiters", first_raw, url)
            if first and first not in RESERVED
            else None
        )

    if host.endswith("workable.com"):
        return BoardRef("workable", first, url) if first and first not in RESERVED else None

    for suffix, platform in (
        (".recruitee.com", "recruitee"),
        (".bamboohr.com", "bamboohr"),
        (".breezy.hr", "breezy"),
        (".applytojob.com", "jazzhr"),
        (".teamtailor.com", "teamtailor"),
    ):
        if host.endswith(suffix):
            token = host[: -len(suffix)]
            return BoardRef(platform, token, url) if token else None

    if ".jobs.personio." in host:
        return BoardRef("personio", host.split(".")[0], url)

    return None


# Matches board URLs wherever they appear in a page: links, iframes, and the script tags
# that embed a board into an employer's own branded careers page -- which is the common
# case, and invisible to a link-only scan.
_EMBED = re.compile(
    r"""(?:href|src|data-src|action)\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_BARE = re.compile(
    r"""https?://[\w.-]*(?:greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com"""
    r"""|smartrecruiters\.com|workable\.com|recruitee\.com|bamboohr\.com|breezy\.hr"""
    r"""|applytojob\.com|teamtailor\.com)[^\s"'<>]*""",
    re.IGNORECASE,
)
# Greenhouse embeds identify the tenant in a query parameter rather than the path.
_GH_EMBED = re.compile(r"""boards\.greenhouse\.io/embed/job_board[^"']*?for=([\w-]+)""", re.I)


def boards_in_html(html: str, page_url: str = "") -> list[BoardRef]:
    """Find every ATS board referenced by a careers page.

    Reported as the most common way to route a company: employers overwhelmingly keep
    their own careers URL and embed the ATS inside it, so the board is in the markup
    rather than in the address bar.
    """
    if not html:
        return []
    found: dict[tuple[str, str], BoardRef] = {}

    for token in _GH_EMBED.findall(html):
        ref = BoardRef("greenhouse", token.lower(), page_url)
        found.setdefault(ref.key(), ref)

    candidates = set(_EMBED.findall(html)) | set(_BARE.findall(html))
    for candidate in candidates:
        ref = extract_board(candidate)
        if ref:
            found.setdefault(ref.key(), ref)
    return list(found.values())


def discover_boards(fetcher: Fetcher, careers_url: str) -> list[BoardRef]:
    """Resolve a careers URL to boards, following an embed if necessary.

    A direct board URL is answered without a request at all; only a branded careers page
    costs a fetch.
    """
    direct = extract_board(careers_url)
    if direct:
        return [direct]
    response = fetcher.get(careers_url)
    if not response.ok:
        return []
    return boards_in_html(response.text, careers_url)
