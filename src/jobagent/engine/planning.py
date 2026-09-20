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
finite budget, ordering *is* the search strategy. The budget is a request allowance, and
platforms differ by an order of magnitude in what one board costs, so a share of it is
reserved for the costly ones (``RESERVED_SHARE``): they are the only free source for
onsite, non-tech US employers, and a split by board count alone read about one of their
boards per run.

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
from ..sources.ats.base import TERM_SEPARATOR, AtsAdapter
from ..sources.catalog import ats_adapters
from ..sources.registry import targets_for

# The platforms that cover onsite non-tech employers cost 20-plus requests per board (list
# pages plus one per description). A proportional split by board count starved them to
# about one board per run; reserving this share of the budget for them was the user's call.
RESERVED_SHARE = 0.40


@dataclass
class SourcePlan:
    """One source's share of the run."""

    source: str
    request: DiscoveryRequest
    boards: int = 0


def since_date(spec: SearchSpec, today: date) -> date | None:
    return today - timedelta(days=spec.max_age_days) if spec.max_age_days else None


def _split(budget: int, counts: dict[str, int]) -> dict[str, int]:
    """Divide ``budget`` in proportion to ``counts``; the parts sum exactly to ``budget``.

    Largest-remainder rounding. The previous max(1, ...) floor gave every non-empty
    platform at least one board, so a budget of 1 across four platforms scheduled four
    reads and the documented total meant nothing.
    """
    total = sum(counts.values())
    if not total:
        return dict.fromkeys(counts, 0)
    exact = {p: budget * c / total for p, c in counts.items() if c}
    allocation = {p: int(v) for p, v in exact.items()}
    remaining = budget - sum(allocation.values())
    for platform, _ in sorted(exact.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True):
        if remaining <= 0:
            break
        allocation[platform] += 1
        remaining -= 1
    return {p: allocation.get(p, 0) for p in counts}


def allocate(
    spec: SearchSpec, store: Store, platforms: list[str],
    adapters: dict[str, AtsAdapter] | None = None,
) -> dict[str, int]:
    """Split the request budget across platforms.

    Proportional to registered board count, so a platform with 600 boards is not given
    the same slice as one with 140 -- except that the platforms flagged ``costly`` on
    their adapter are guaranteed ``RESERVED_SHARE`` of the budget between them, split by
    board count. Under a plain proportional split, 155 boards on a costly platform drew
    47 of 400 requests: about one board read per run, on the only free source for onsite
    non-tech employers. Each costly platform gets the larger of its reserved and its
    proportional share, and the cheap platforms split what is left, so the parts still
    sum to ``spec.source_budget``. When only one group has boards, it takes the whole
    budget proportionally.

    Known gap, unchanged here: ``targets_for(limit=limit)`` still selects ``limit`` boards
    for what is a request allowance, so a costly platform's coverage note may say "N more
    not reached" about boards the allowance could never have paid for.
    """
    adapters = adapters or ats_adapters()
    available = store.registry_counts()
    counts = {p: available.get(p, 0) for p in platforms}
    if not sum(counts.values()):
        return dict.fromkeys(platforms, 0)

    costly = {
        p: c for p, c in counts.items() if c and getattr(adapters.get(p), "costly", False)
    }
    cheap = {p: c for p, c in counts.items() if c and p not in costly}
    if not costly or not cheap:
        return _split(spec.source_budget, counts)

    proportional = _split(spec.source_budget, counts)
    reserved = _split(int(spec.source_budget * RESERVED_SHARE), costly)
    allocation = {p: max(reserved[p], proportional[p]) for p in costly}
    allocation.update(_split(spec.source_budget - sum(allocation.values()), cheap))
    return {p: allocation.get(p, 0) for p in platforms}


def build_plans(
    spec: SearchSpec, store: Store, fetcher: Fetcher, today: date,
    only_sources: list[str] | None = None,
) -> list[SourcePlan]:
    """Produce one request per source for this run."""
    terms = spec.discovery_terms()
    since = since_date(spec, today)
    wanted = set(only_sources) if only_sources else None

    adapters = ats_adapters()
    platforms = [p for p in adapters if not wanted or p in wanted]
    allocation = allocate(spec, store, platforms, adapters)
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
        # Only platforms whose adapter offers real server-side search get the query
        # pushed down; the rest return whole boards and are filtered locally. Such a
        # platform takes one string per query, so every term is passed and the adapter
        # queries each in turn -- sending only the first would narrow discovery
        # server-side, where no local filter can recover the jobs that were never
        # retrieved.
        pushdown = adapters[platform].server_search and terms
        search_text = TERM_SEPARATOR.join(terms) if pushdown else ""
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
