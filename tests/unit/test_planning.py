"""How a request budget is split across platforms, and where the query is pushed down.

The budget is a request allowance, not a board count, and a board on a detail-costly
platform costs an order of magnitude more requests than one on the others. A split by
board count alone gave the costly platform 47 of 400 and read about one of its boards per
run -- on the only free source for onsite, non-tech US employers. These tests pin the
reserved share, and pin that pushdown follows the adapter's flag rather than its name.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from jobagent.domain.spec import SearchSpec
from jobagent.engine import planning
from jobagent.engine.planning import allocate, build_plans
from jobagent.infra.store import Store
from jobagent.sources.ats.base import TERM_SEPARATOR
from tests.fakes import FakeFetcher


class _Registry:
    """The only thing allocate() reads from a store: live boards per platform."""

    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    def registry_counts(self) -> dict[str, int]:
        return dict(self.counts)


def _adapters(costly: set[str] = frozenset(), **counts: int) -> dict:
    return {p: SimpleNamespace(costly=p in costly, server_search=False) for p in counts}


def _allocate(budget: int, costly: set[str] = frozenset(), **counts: int) -> dict[str, int]:
    spec = SearchSpec(name="t", titles=["x"], source_budget=budget)
    return allocate(spec, _Registry(counts), list(counts), _adapters(costly, **counts))


def test_costly_platforms_get_the_reserved_share():
    """Regression: the seed proportions at budget 400 gave the costly platform 47."""
    alloc = _allocate(400, {"workday"}, greenhouse=618, lever=307, ashby=227, workday=155)
    assert alloc["workday"] >= 160
    assert sum(alloc.values()) == 400


def test_zero_budget_is_all_zeros():
    """A zero budget must schedule nothing; a reserved share is a share of nothing."""
    alloc = _allocate(0, {"workday"}, greenhouse=618, lever=307, ashby=227, workday=155)
    assert all(v == 0 for v in alloc.values())
    assert set(alloc) == {"greenhouse", "lever", "ashby", "workday"}


def test_no_costly_platforms_split_everything():
    """Without a costly platform the split is plain proportional and sums exactly."""
    alloc = _allocate(7, greenhouse=600, lever=300, ashby=100)
    assert sum(alloc.values()) == 7
    assert alloc["greenhouse"] == max(alloc.values())

    assert _allocate(10, greenhouse=60, lever=30, ashby=10) == {
        "greenhouse": 6, "lever": 3, "ashby": 1,
    }


def test_costly_share_never_drops_below_proportional():
    """The reserve is a floor, not a cap: a costly platform with most of the boards keeps them."""
    alloc = _allocate(100, {"workday"}, workday=900, greenhouse=100)
    assert alloc["workday"] >= 90
    assert sum(alloc.values()) == 100


def test_terms_are_pushed_down_only_to_server_search_adapters(monkeypatch):
    """Pushdown used to be keyed on one platform's name; it now follows the adapter's flag."""
    store = Store(":memory:")
    store.add_company("Acme", "greenhouse", "acme", us_signal=True)
    store.add_company("Beta", "lever", "beta", us_signal=True)
    monkeypatch.setattr(planning, "ats_adapters", lambda: {
        "greenhouse": SimpleNamespace(costly=False, server_search=True),
        "lever": SimpleNamespace(costly=False, server_search=False),
    })
    spec = SearchSpec(name="t", titles=["x", "y"], source_budget=10)

    plans = {p.source: p for p in build_plans(spec, store, FakeFetcher(), date(2026, 9, 14))}

    sent = plans["greenhouse"].request.targets[0].extra["search_text"]
    assert sent == TERM_SEPARATOR.join(["x", "y"])
    assert "search_text" not in plans["lever"].request.targets[0].extra
