"""The go/no-go gate.

Discovery here is registry fan-out, not keyword search, so "does this tool actually work
on your network" is a question about three things at once: whether the federal source is
reachable, whether enough ATS adapters return real data, and whether the registry has
enough boards routed to those working adapters to be worth fanning out over.

An engine over an empty or unreachable registry is not a product, so this is a gate rather
than a diagnostic: it is meant to be run first, and to fail loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import SourceReport, SourceStatus
from ..infra.store import Store
from ..ports import Fetcher
from ..sources.catalog import CATALOG, all_adapters

# Thresholds. The registry number is the one that matters most: with no cross-company
# search available, boards ARE the search corpus.
MIN_ATS_ADAPTERS = 3
MIN_ROUTABLE_BOARDS = 500


@dataclass
class DoctorResult:
    reports: list[SourceReport] = field(default_factory=list)
    registry_counts: dict[str, int] = field(default_factory=dict)
    routable: int = 0
    working_ats: list[str] = field(default_factory=list)
    usajobs_ok: bool = False
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        return (
            f"{len(self.working_ats)} ATS adapters working, "
            f"{self.routable} boards routable, "
            f"USAJOBS {'reachable' if self.usajobs_ok else 'not reachable'}"
        )


def run_doctor(store: Store, fetcher: Fetcher) -> DoctorResult:
    """Probe every source and apply the three thresholds."""
    result = DoctorResult()
    adapters = all_adapters()

    for name, adapter in adapters.items():
        try:
            report = adapter.check(fetcher)
        except Exception as error:  # noqa: BLE001 - a broken probe is a failed probe
            status, note = adapter.classify_failure(error)
            report = SourceReport(source=name, status=status, note=note)
        result.reports.append(report)

        # Content, not status code: several platforms answer 200 with a placeholder for
        # tenants that do not exist, so "reachable" alone proves nothing.
        working = report.status is SourceStatus.OK and report.found > 0
        if name == "usajobs":
            result.usajobs_ok = working
        elif working:
            result.working_ats.append(name)

    result.registry_counts = store.registry_counts()
    result.routable = sum(
        count for platform, count in result.registry_counts.items()
        if platform in result.working_ats
    )

    if not result.usajobs_ok:
        usajobs = next((r for r in result.reports if r.source == "usajobs"), None)
        detail = usajobs.note if usajobs else "no report"
        result.failures.append(f"USAJOBS is not returning results ({detail})")
    if len(result.working_ats) < MIN_ATS_ADAPTERS:
        result.failures.append(
            f"only {len(result.working_ats)} of {MIN_ATS_ADAPTERS} required ATS adapters "
            f"returned postings ({', '.join(result.working_ats) or 'none'})"
        )
    if result.routable < MIN_ROUTABLE_BOARDS:
        result.failures.append(
            f"only {result.routable} registry boards route to a working adapter; "
            f"{MIN_ROUTABLE_BOARDS} needed. Seed the registry or add companies."
        )

    return result


def describe(result: DoctorResult) -> list[str]:
    """Plain-text rendering, for the CLI and for the live-validation handoff."""
    lines = ["Source check", "=" * 60]
    for report in sorted(result.reports, key=lambda r: r.source):
        info = CATALOG.get(report.source)
        mark = "ok  " if report.status is SourceStatus.OK and report.found else "FAIL"
        lines.append(
            f"{mark} {report.source:16} {report.status.value:12} "
            f"found={report.found:<5} {report.note[:70]}"
        )
        if info:
            lines.append(f"       {info.priority} {info.kind}: {info.note}")

    lines.append("")
    lines.append("Registry")
    lines.append("-" * 60)
    for platform, count in sorted(result.registry_counts.items(), key=lambda kv: -kv[1]):
        routed = " (routable)" if platform in result.working_ats else ""
        lines.append(f"  {platform:16} {count:>6}{routed}")
    lines.append(f"  {'routable total':16} {result.routable:>6}")

    lines.append("")
    if result.passed:
        lines.append(f"GATE PASSED: {result.summary()}")
    else:
        lines.append(f"GATE FAILED: {result.summary()}")
        lines.extend(f"  - {failure}" for failure in result.failures)
    return lines
