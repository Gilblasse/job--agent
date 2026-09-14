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

There is no response caching. Conditional requests would cut bandwidth on repeat runs,
but returning a cached body means storing every board body, and that is not built. A run
re-fetches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..domain.spec import SearchSpec
from ..infra.store import Store
from ..ports import DiscoveryRequest, Fetcher
from ..sources.ats.workday import TERM_SEPARATOR
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
    available = store.registry_counts()
    counts = {p: available.get(p, 0) for p in platforms}
    total = sum(counts.values())
    if not total:
        return dict.fromkeys(platforms, 0)

    # Largest-remainder, so the parts sum to the budget. The previous max(1, ...) floor
    # gave every non-empty platform at least one board, so a budget of 1 across four
    # platforms scheduled four reads and the documented total meant nothing.
    exact = {p: spec.source_budget * c / total for p, c in counts.items() if c}
    allocation = {p: int(v) for p, v in exact.items()}
    remaining = spec.source_budget - sum(allocation.values())
    for platform, _ in sorted(exact.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True):
        if remaining <= 0:
            break
        allocation[platform] += 1
        remaining -= 1
    return {p: allocation.get(p, 0) for p in platforms}


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
            # Still planned, with no boards. Dropping it here removed the platform from
            # the coverage table altogether, so a fresh install reported "No matches"
            # while silently never having searched anything.
            plans.append(
                SourcePlan(
                    source=platform, boards=0,
                    request=DiscoveryRequest(
                        terms=terms, countries=spec.countries, locations=spec.locations,
                        fetcher=fetcher, budget=0, since=since, today=today, targets=[],
                    ),
                )
            )
            continue
        # Only Workday offers real server-side search, so only Workday gets the query
        # pushed down; the rest return whole boards and are filtered locally. It takes one
        # string per query, so every term is passed and the adapter queries each in turn --
        # sending only the first would narrow discovery server-side, where no local filter
        # can recover the jobs that were never retrieved.
        search_text = TERM_SEPARATOR.join(terms) if (platform == "workday" and terms) else ""
        targets = targets_for(store, platform, limit=limit, search_text=search_text)
        plans.append(
            SourcePlan(
                source=platform,
                boards=len(targets),
                request=DiscoveryRequest(
                    terms=terms, countries=spec.countries, locations=spec.locations,
                    fetcher=fetcher, budget=limit, since=since, today=today,
                    targets=list(targets),
                ),
            )
        )

    if not wanted or "usajobs" in wanted:
        plans.append(
            SourcePlan(
                source="usajobs",
                request=DiscoveryRequest(
                    terms=terms, countries=spec.countries, locations=spec.locations,
                    fetcher=fetcher, budget=spec.source_budget, since=since, today=today,
                ),
            )
        )
    return plans
