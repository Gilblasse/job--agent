"""The set of sources this build can read."""

from __future__ import annotations

from dataclasses import dataclass

from .ats.ashby import AshbyAdapter
from .ats.base import AtsAdapter
from .ats.greenhouse import GreenhouseAdapter
from .ats.lever import LeverAdapter
from .ats.workday import WorkdayAdapter
from .base import SourceAdapter
from .usajobs import UsaJobsAdapter


@dataclass(frozen=True)
class SourceInfo:
    """What a source is, and why it is in or out of the priority set."""

    name: str
    kind: str           # "ats" | "employer"
    priority: str       # "P1" | "P2"
    note: str


CATALOG: dict[str, SourceInfo] = {
    "greenhouse": SourceInfo(
        "greenhouse", "ats", "P1",
        "Documented, auth-free. Whole board with descriptions in one request. "
        "Publishes no remote flag, employment type or pay.",
    ),
    "lever": SourceInfo(
        "lever", "ats", "P1",
        "Documented, auth-free. Richest fields: real three-state workplace type, "
        "structured salary, ISO country.",
    ),
    "ashby": SourceInfo(
        "ashby", "ats", "P1",
        "Auth-free. Whole board with descriptions, isRemote plus a three-state "
        "workplace type. Compensation on request.",
    ),
    "workday": SourceInfo(
        "workday", "ats", "P1",
        "Undocumented but keyless. Expensive: 20 per page, a second request per "
        "description. Where most large non-tech US employers post onsite roles.",
    ),
    "usajobs": SourceInfo(
        "usajobs", "employer", "P1",
        "The federal government's own applicant system. Free instant key. Real "
        "cross-agency keyword and location search across every occupation. Federal only.",
    ),
}

# Deliberately not built. Recorded so the reasons survive, rather than looking like
# oversights to whoever reads this next.
EXCLUDED: dict[str, str] = {
    "aggregators": (
        "Out of scope: this tool searches employer ATS boards and official employer "
        "systems only."
    ),
    "smartrecruiters": (
        "P2, pending a robots.txt check: its API host is reported to disallow all "
        "crawlers while the vendor documents the endpoint beneath it as a public "
        "read-only API. Unresolved, so unshipped."
    ),
    "workable": "P2, pending a robots.txt check. Real-world 429s observed at fan-out scale.",
    "jobvite": "No usable public feed; detail-page JSON-LD only, at one request per posting.",
    "join.com": "Public API requires a paid-plan token.",
}


def ats_adapters() -> dict[str, AtsAdapter]:
    return {
        "greenhouse": GreenhouseAdapter(),
        "lever": LeverAdapter(),
        "ashby": AshbyAdapter(),
        "workday": WorkdayAdapter(),
    }


def all_adapters() -> dict[str, SourceAdapter]:
    adapters: dict[str, SourceAdapter] = dict(ats_adapters())
    adapters["usajobs"] = UsaJobsAdapter()
    return adapters
