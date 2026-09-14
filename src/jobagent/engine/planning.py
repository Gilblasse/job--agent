"""Deciding how to execute a search.

The user says what they want; this module decides how to go and get it. That division is
the point -- nobody should have to know what an ATS is, which platforms support
server-side search, or how to spend a request budget, in order to look for a job.

Two rules shape every plan:

Only the positive side of the spec is ever sent to a source. Exclusions are applied after
retrieval, so a job is dropped by a rule the user can see rather than by a query that
never asked for it.

The budget is spent where it is most likely to pay. Fan-out ordering favours boards with
a US signal and a recent success, because with thousands of registered boards and a
finite budget, ordering *is* the search strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..domain.spec import SearchSpec
from ..infra.store import Store
from ..ports import DiscoveryRequest, Fetcher
from ..sources.catalog import ats_adapters
from ..sources.registry import targets_for


@dataclass
class SourcePlan:
    """One source's share of the run."""

    source: str
    request: DiscoveryRequest
    boards: int = 0


def since_date(spec: SearchSpec, today: date) -> date | None:
    return today - timedelta(days=spec.max_age_days) if spec.max_age_days else None


def allocate(spec: SearchSpec, store: Store, platforms: list[str]) -> dict[str, int]:
    """Split the board budget across platforms in proportion to what is registered.

    Proportional rather than equal: giving a platform with 600 boards the same slice as
    one with 140 wastes budget on the smaller and starves the larger.
    """
    counts = {p: store.registry_counts().get(p, 0) for p in platforms}
    total = sum(counts.values())
    if not total:
        return dict.fromkeys(platforms, 0)

    allocation: dict[str, int] = {}
    for platform, count in counts.items():
        allocation[platform] = max(1, round(spec.source_budget * count / total)) if count else 0
    return allocation


def build_plans(
    spec: SearchSpec, store: Store, fetcher: Fetcher, today: date,
    only_sources: list[str] | None = None,
) -> list[SourcePlan]:
    """Produce one request per source for this run."""
    terms = spec.discovery_terms()
    since = since_date(spec, today)
    wanted = set(only_sources) if only_sources else None

    platforms = [p for p in ats_adapters() if not wanted or p in wanted]
    allocation = allocate(spec, store, platforms)
    plans: list[SourcePlan] = []

    for platform in platforms:
        limit = allocation.get(platform, 0)
        if not limit:
            continue
        # Only Workday offers real server-side search, so only Workday gets the query
        # pushed down; the rest return whole boards and are filtered locally.
        search_text = terms[0] if (platform == "workday" and terms) else ""
        targets = targets_for(store, platform, limit=limit, search_text=search_text)
        plans.append(
            SourcePlan(
                source=platform,
                boards=len(targets),
                request=DiscoveryRequest(
                    terms=terms, countries=spec.countries, locations=spec.locations,
                    fetcher=fetcher, budget=limit, since=since, targets=list(targets),
                ),
            )
        )

    if not wanted or "usajobs" in wanted:
        plans.append(
            SourcePlan(
                source="usajobs",
                request=DiscoveryRequest(
                    terms=terms, countries=spec.countries, locations=spec.locations,
                    fetcher=fetcher, budget=spec.source_budget, since=since,
                ),
            )
        )
    return plans
