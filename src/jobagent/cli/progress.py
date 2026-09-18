"""The progress display for ``jobagent run``.

A run has two phases and the display says so. While sources are being read, the bar
counts boards and the line beneath counts postings found; only once every source has
returned can jobs be judged -- cross-source duplicates have to be merged first -- so the
matches count appears in the second phase, rising as the evaluation runs.

Everything is transient: when the run ends the display is cleared and the tables that
follow are the record. Under a non-terminal console (tests, pipes) nothing is drawn.
"""

from __future__ import annotations

import threading

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from ..domain.models import SourceReport


class ConsoleProgress:
    """A ``RunProgress`` drawn with rich.

    Hooks arrive from the source worker threads; one lock guards the counters and each
    update hands the live display a freshly built line rather than mutating one that
    another thread may be rendering.
    """

    def __init__(self, console: Console):
        self._console = console
        self._lock = threading.Lock()
        self._progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        self._live = Live(
            self._render(""), console=console, transient=True, refresh_per_second=8
        )
        self._boards: TaskID | None = None
        self._jobs: TaskID | None = None
        self._planned: dict[str, int] = {}
        self._done: dict[str, int] = {}
        self._found = 0
        self._sources_done = 0
        self._matched = 0
        self._new = 0

    # -- context -------------------------------------------------------------------

    def __enter__(self) -> ConsoleProgress:
        self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._live.stop()

    # -- RunProgress ---------------------------------------------------------------

    def discovery_started(self, units: dict[str, int]) -> None:
        with self._lock:
            self._planned = dict(units)
            self._done = dict.fromkeys(units, 0)
            total = sum(units.values())
            self._boards = self._progress.add_task("Reading boards", total=max(total, 1))
            if total == 0:
                # Nothing to read (a zero budget, an empty registry): a bar with no
                # total would pulse forever, so it is completed at once.
                self._progress.update(self._boards, completed=1)
            self._refresh()

    def unit_done(self, source: str, found: int) -> None:
        with self._lock:
            self._found += found
            self._done[source] = self._done.get(source, 0) + 1
            if self._boards is not None and self._done[source] <= self._planned.get(source, 0):
                self._progress.advance(self._boards, 1)
            self._refresh()

    def source_done(self, source: str, report: SourceReport) -> None:
        with self._lock:
            self._sources_done += 1
            # Whatever the source did not reach -- budget stop, host block, missing
            # credentials -- is completed here, so the bar reflects work that will not
            # happen rather than waiting for it.
            remaining = self._planned.get(source, 0) - self._done.get(source, 0)
            if self._boards is not None and remaining > 0:
                self._progress.advance(self._boards, remaining)
                self._done[source] = self._planned[source]
            self._refresh()

    def evaluation_started(self, total: int) -> None:
        with self._lock:
            if self._boards is not None:
                self._progress.update(self._boards, visible=False)
            self._jobs = self._progress.add_task("Evaluating postings", total=max(total, 1))
            if total == 0:
                self._progress.update(self._jobs, completed=1)
            self._refresh()

    def job_evaluated(self, matched: bool, is_new: bool) -> None:
        with self._lock:
            self._matched += int(matched)
            self._new += int(matched and is_new)
            if self._jobs is not None:
                self._progress.advance(self._jobs, 1)
            self._refresh()

    # -- rendering -----------------------------------------------------------------

    def _detail(self) -> str:
        if self._jobs is not None:
            return f"matches: {self._matched}  ·  new: {self._new}"
        sources = len(self._planned)
        return (
            f"postings found: {self._found}  ·  sources done {self._sources_done}/{sources}"
        )

    def _render(self, detail: str) -> Group:
        return Group(self._progress, Text(detail, style="dim"))

    def _refresh(self) -> None:
        self._live.update(self._render(self._detail()))
