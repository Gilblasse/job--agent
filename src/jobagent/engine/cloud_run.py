"""Running every search for the web, from a runner that keeps nothing.

One invocation: take the publisher lease, consume the waiting request (or open a
scheduled one), pull the searches and the registry into a scratch database, pass the
doctor gate, run each search with the unchanged engine and publish it as soon as it
finishes, then report. The lease is renewed from a thread every minute; if it is ever
lost, the guard inside every publish batch refuses the next write and the run stops
without claiming success.
"""

from __future__ import annotations

import contextlib
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..domain.spec import SearchSpec
from ..infra.publish import PublishReport, finish_publish, publish_run, pull
from ..infra.store import Lease, LeaseLost, Store
from .doctor import describe as describe_doctor
from .doctor import run_doctor
from .orchestrator import run_search


@dataclass
class CloudRunOutcome:
    exit_code: int
    request_id: str | None = None
    lines: list[str] = field(default_factory=list)
    report: PublishReport | None = None


class Heartbeat:
    """Renews the lease from its own connection until stopped, or until it cannot.

    ``lost`` is set the moment a renewal matches no row: the lease was taken over or
    expired, and this runner must not write again. The guard enforces that inside each
    batch regardless; the flag lets the main loop stop early and say why.
    """

    def __init__(
        self, open_store: Callable[[], Store], lease: Lease, interval: float,
        clock: Callable[[], datetime],
    ):
        self._open = open_store
        self._lease = lease
        self._interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="lease-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                store = self._open()
                renewed = store.renew_lease(self._lease, self._clock())
            except Exception:  # noqa: BLE001 - a transient failure is retried next tick
                continue
            if not renewed:
                self.lost.set()
                return


def run_cloud(
    cloud: Store,
    open_cloud: Callable[[], Store],
    open_fetcher: Callable[[], Any],
    *,
    origin: str,
    search: str | None = None,
    budget: int | None = None,
    doctor: bool = True,
    clock: Callable[[], datetime] = datetime.now,
    heartbeat_seconds: float = 60.0,
    log: Callable[[str], None] = print,
) -> CloudRunOutcome:
    """The whole runner, start to finish. Raises after recording a failure so the
    caller (a workflow, a CLI) sees the traceback; returns an exit code otherwise."""
    now = clock()
    lease = cloud.take_lease(now)
    if lease is None:
        log("another runner holds the lease; nothing done")
        return CloudRunOutcome(exit_code=0)
    lease = cloud.consume_request(lease, now, origin)
    request = cloud.get_request(lease.request_id or "")
    priority = request["priority_uid"] if request else None
    log(f"request {lease.request_id} ({request['origin'] if request else origin})")
    outcome = CloudRunOutcome(exit_code=0, request_id=lease.request_id)

    heartbeat = Heartbeat(open_cloud, lease, heartbeat_seconds, clock)
    heartbeat.start()
    report = PublishReport()
    started = now
    try:
        with tempfile.TemporaryDirectory() as scratch_dir, contextlib.ExitStack() as stack:
            local = Store(Path(scratch_dir) / "scratch.sqlite3")
            stack.callback(local.close)
            pulled = pull(cloud, local)
            log(f"pulled {pulled.searches} searches and {pulled.boards} boards")

            fetcher = open_fetcher()
            if hasattr(fetcher, "__enter__"):
                fetcher = stack.enter_context(fetcher)

            if doctor:
                verdict = run_doctor(local, fetcher)
                if not verdict.passed:
                    lines = describe_doctor(verdict)
                    for line in lines:
                        log(line)
                    cloud.finish_request(
                        lease, "failed", "doctor gate failed: " + verdict.summary(), 0, clock()
                    )
                    outcome.exit_code = 1
                    outcome.lines = lines
                    return outcome

            for row in _ordered(local.list_specs(), priority, search):
                if heartbeat.lost.is_set():
                    raise LeaseLost("the lease was lost during the run")
                spec = SearchSpec.from_yaml(local.get_spec(row["name"])[1])
                if budget is not None:
                    spec = spec.model_copy(update={"source_budget": budget})
                moment = clock()
                run = run_search(
                    spec, local, fetcher, search_id=int(row["id"]), today=moment.date(),
                    now=moment, request_id=lease.request_id,
                )
                published = publish_run(local, cloud, run.run_id, lease, clock())
                report.runs.append(published)
                log(published.summary())

            finish_publish(local, cloud, lease, clock(), started, report)
    except LeaseLost as error:
        heartbeat.stop()
        log(f"lease lost: {error}; stopping without reporting success")
        outcome.exit_code = 1
        outcome.lines.append(str(error))
        return outcome
    except Exception as error:
        heartbeat.stop()
        cloud.finish_request(lease, "failed", f"{type(error).__name__}: {error}", 0, clock())
        raise
    heartbeat.stop()

    outcome.report = report
    outcome.lines = report.summary().splitlines()
    for line in outcome.lines:
        log(line)
    if not cloud.finish_request(lease, "succeeded", report.summary(), report.rows_written, clock()):
        log("lease lost before the outcome could be reported")
        outcome.exit_code = 1
    return outcome


def _ordered(rows: list[Any], priority_uid: str | None, only: str | None) -> list[Any]:
    """The requester's search first, then the rest; or just the one named."""
    chosen = [row for row in rows if only is None or row["name"] == only]
    return sorted(chosen, key=lambda row: (row["uid"] != priority_uid, row["name"]))
