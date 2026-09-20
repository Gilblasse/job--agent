"""Text utilities shared by normalization and gate evaluation.

Job descriptions arrive as HTML of wildly varying quality. Everything that has to read
that text -- phrase gates, requirement detection, evidence quoting -- goes through here so
the quirks are handled in exactly one place.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Tags whose content is not prose and would pollute phrase matching.
_SKIP_CONTENT = {"script", "style", "head", "title"}

# Tags that imply a line break when flattened to text.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}


class _TextExtractor(HTMLParser):
    """Flattens HTML to text, preserving block structure as newlines.

    Bullet structure is preserved as ``\n- `` because requirement detection reads the
    enclosing bullet, and losing list boundaries would merge a "required" bullet into an
    adjacent "preferred" one.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        # True from a bullet marker until its first text. Lever and Greenhouse publish
        # ``<li><p>text</p></li>``; letting that ``<p>`` break the line left a lone "-"
        # and put the bullet text on a line of its own, where it read as a heading.
        self._fresh_bullet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
        elif tag == "li":
            self._parts.append("\n- ")
            self._fresh_bullet = True
        elif tag in _BLOCK_TAGS and not self._fresh_bullet:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
            self._fresh_bullet = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth or (self._fresh_bullet and not data.strip()):
            return
        self._parts.append(data)
        self._fresh_bullet = False

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(raw: str) -> str:
    """Convert a description from HTML to readable plain text.

    Unescapes twice by design: Greenhouse returns HTML whose entities are themselves
    entity-encoded, so a single pass leaves ``&lt;p&gt;`` visible in the output.
    """
    if not raw:
        return ""
    if "&lt;" in raw or "&amp;" in raw:
        raw = html.unescape(raw)
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # Malformed markup should degrade to stripped text, never crash a whole run.
        return collapse_whitespace(re.sub(r"<[^>]+>", " ", raw))
    return collapse_whitespace(parser.text())


def collapse_whitespace(text: str) -> str:
    """Normalize runs of whitespace while keeping paragraph and bullet breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Endings stripped to find a word's stem, longest first so "auditing" loses "ing" rather
# than just "g".
_INFLECTIONS = ("ing", "ed", "es", "s")
_MIN_WORD_FOR_STEMMING = 5
_MIN_STEM = 4


def _inflected(word: str) -> str:
    """Build a pattern matching a word and its ordinary English inflections.

    A user who excludes "auditing" means to exclude "the annual audit" too. Requiring an
    exact form makes exclusions quietly porous in exactly the cases they were written
    for, so the word is reduced to a stem and matched with an optional ending.

    Short words are left alone. Stemming "plus" to "plu" or "ads" to "ad" buys nothing
    and risks matching things the user never asked about.
    """
    escaped = re.escape(word)
    if len(word) < _MIN_WORD_FOR_STEMMING:
        # Too short to stem safely, but a trailing plural is still safe to allow, and
        # short words here are usually acronyms: "Licensed CPAs required" must not read
        # as no mention of a CPA at all. Only the suffix is added, never removed, so
        # "plus" cannot become "plu".
        return escaped + "s?" if word[-1:].isalpha() else escaped
    lowered = word.lower()
    stem = lowered
    for ending in _INFLECTIONS:
        if lowered.endswith(ending) and len(lowered) - len(ending) >= _MIN_STEM:
            stem = lowered[: -len(ending)]
            break
    if len(stem) < _MIN_STEM:
        return escaped
    return re.escape(stem) + r"(?:s|es|ed|ing)?"


# Where one word ends and the next begins inside a typed phrase. A hyphen counts, so the
# user's "front-end" and "front end" compile to the same pattern.
_WORD_JOIN = re.compile(r"[\s\-]+")


def phrase_pattern(
    phrase: str, *, inflect: bool = False, match_case: bool = False
) -> re.Pattern[str]:
    """Build a word-boundary-aware pattern for a phrase.

    Word boundaries matter more than they look: a bare substring search for "AP" hits
    "apply", "application" and "capacity", which would make an Accounts Payable search
    match essentially every posting ever written.

    Word joins are flexible in both directions: the phrase is split on whitespace AND
    hyphens, and the words may be joined in the text by whitespace, a hyphen, a slash, or
    nothing at all. So "front-end", "front end" and "frontend" are one phrase however the
    user typed it, "cash management" still matches across a line break, and "e-commerce"
    finds "ecommerce". A leading or trailing non-word character (as in "C++") drops the
    boundary on that side, where a word boundary could never match.

    With ``inflect``, the final word also matches its common inflections. Only the final
    word is stemmed: in a phrase like "budget creation" it is the head noun that varies,
    while the modifier does not.

    With ``match_case``, a phrase the user wrote with a capital letter is matched exactly
    as written. This exists for skills: "React" the library is not "react" the verb, and
    on the first live run a trading role passed a React search on "react to volatility
    shifts". A lower-case phrase is still matched case-insensitively, so the user's own
    casing decides, and nothing changes for anyone who did not ask.
    """
    words = [w for w in _WORD_JOIN.split(phrase) if w]
    tokens = [re.escape(t) for t in words]
    if inflect and tokens:
        tokens[-1] = _inflected(words[-1])
    core = r"[\s\-/]*".join(tokens)
    left = r"\b" if re.match(r"\w", phrase) else ""
    right = r"\b" if re.search(r"\w$", phrase) else ""
    flags = 0 if match_case and any(c.isupper() for c in phrase) else re.IGNORECASE
    return re.compile(left + core + right, flags)


def find_phrase(
    text: str, phrase: str, *, inflect: bool = False, match_case: bool = False
) -> tuple[int, int] | None:
    """Return the span of the first occurrence of ``phrase``, or None."""
    if not phrase or not text:
        return None
    match = phrase_pattern(phrase, inflect=inflect, match_case=match_case).search(text)
    return match.span() if match else None


def find_all_phrases(text: str, phrase: str, *, inflect: bool = False) -> list[tuple[int, int]]:
    """Return spans of every occurrence of ``phrase``."""
    if not phrase or not text:
        return []
    return [m.span() for m in phrase_pattern(phrase, inflect=inflect).finditer(text)]


def contains_phrase(text: str, phrase: str, *, inflect: bool = False) -> bool:
    return find_phrase(text, phrase, inflect=inflect) is not None


def first_matching_phrase(
    text: str, phrases: list[str], *, inflect: bool = False, match_case: bool = False
) -> tuple[str, tuple[int, int]] | None:
    """Return the first phrase from ``phrases`` that occurs in ``text``, with its span."""
    for phrase in phrases:
        span = find_phrase(text, phrase, inflect=inflect, match_case=match_case)
        if span:
            return phrase, span
    return None



def snippet(text: str, span: tuple[int, int], width: int = 60) -> str:
    """Quote the text around a match, for use as evidence in an explanation."""
    start, end = span
    left = max(0, start - width)
    right = min(len(text), end + width)
    fragment = collapse_whitespace(text[left:right]).replace("\n", " ")
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


# A clause ends at a sentence terminator, a newline, a bullet, or a semicolon. Requirement
# context is read within one clause, because "CPA required" and "MBA preferred" routinely
# sit in adjacent bullets and must not contaminate each other.
_CLAUSE_BOUNDARY = re.compile(r"(?<=[.;!?])\s+|\n+")


def clause_around(text: str, span: tuple[int, int]) -> str:
    """Return the clause containing ``span``.

    This is the window requirement detection classifies. Keeping it tight is what stops
    "CPA preferred" in one bullet from being read as required because a different bullet
    three lines down says "required".
    """
    start, end = span
    left = 0
    for match in _CLAUSE_BOUNDARY.finditer(text, 0, start):
        left = match.end()
    right_match = _CLAUSE_BOUNDARY.search(text, end)
    right = right_match.start() if right_match else len(text)
    return text[left:right].strip()


_WORD = re.compile(r"[a-z0-9]+(?:\+\+)?")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, used for overlap-style ranking signals."""
    return _WORD.findall(text.lower())
