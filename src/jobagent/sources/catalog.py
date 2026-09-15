"""The set of sources this build can read."""

from __future__ import annotations

from dataclasses import dataclass

from .ats.ashby import AshbyAdapter
from .ats.base import AtsAdapter
from .ats.greenhouse import GreenhouseAdapter
from .ats.lever import LeverAdapter
from .ats.workable import WorkableAdapter
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
    "workable": SourceInfo(
        "workable", "ats", "P1",
        "Keyless widget API, one request per tenant with descriptions. Publishes a "
        "remote flag but not onsite-versus-hybrid, and no pay. Host robots.txt checked "
        "live 2026-09-15: nothing disallowed.",
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
        "Its API host's robots.txt, read live 2026-09-15, says 'Disallow: /' for every "
        "agent and grants '/v1/companies/' to LinkedInBot alone. A carve-out for one "
        "named bot makes the general refusal deliberate, so the boards stay seeded but "
        "unread."
    ),
    "jobvite": "No usable public feed; detail-page JSON-LD only, at one request per posting.",
    "join.com": "Public API requires a paid-plan token.",
}


def ats_adapters() -> dict[str, AtsAdapter]:
    return {
        "greenhouse": GreenhouseAdapter(),
        "lever": LeverAdapter(),
        "ashby": AshbyAdapter(),
        "workday": WorkdayAdapter(),
        "workable": WorkableAdapter(),
    }


def all_adapters() -> dict[str, SourceAdapter]:
    adapters: dict[str, SourceAdapter] = dict(ats_adapters())
    adapters["usajobs"] = UsaJobsAdapter()
    return adapters
