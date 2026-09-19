"""Waking the runner.

A request to run the searches is a row in the database (``run_requests``); a runner
consumes it whenever it starts. The dispatcher decides who that runner is:

* ``ThreadDispatcher`` -- this process, in a background thread, with the real fetcher.
  The default whenever the app runs on a local SQLite database: the website and the
  command line are then the same system, one engine reading the same boards under the
  same robots and rate-limit policy, one writing to a terminal and one to a screen.
* ``GitHubDispatcher`` -- ``workflow_dispatch`` on the run workflow, with no inputs. The
  deployed case: Vercel's function cannot host a minutes-long fan-out, so the runner is
  a GitHub Actions job that consumes the queued request from the cloud database.
* ``NullDispatcher`` -- nobody is woken; the request waits for the scheduled run.

``JOBAGENT_DISPATCHER`` (``thread`` | ``github`` | ``none``) overrides the choice.

The GitHub call is not a job source: it reaches the user's own CI, so it does not go
through the fetcher. The thread dispatcher's fan-out does, exactly as the CLI's.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any, Protocol

import httpx


class Dispatcher(Protocol):
    def wake(self) -> None: ...


class ThreadDispatcher:
    """Run the runner's whole path here, in a daemon thread, one at a time.

    ``open_store`` opens a fresh connection to the same database each time it is called
    (the runner, its heartbeat and each API request each want their own);
    ``open_fetcher`` is the real fetcher unless a test says otherwise.
    """

    def __init__(
        self, open_store: Callable[[], Any], open_fetcher: Callable[[], Any], *,
        doctor: bool = True, heartbeat_seconds: float = 60.0,
        log: Callable[[str], None] = lambda line: print(line, flush=True),
    ):
        self._open_store = open_store
        self._open_fetcher = open_fetcher
        self._doctor = doctor
        self._heartbeat = heartbeat_seconds
        self._log = log
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def wake(self) -> None:
        thread = threading.Thread(target=self.run_once, name="jobagent-runner", daemon=True)
        self.threads.append(thread)
        thread.start()

    def run_once(self) -> None:
        from ..engine.cloud_run import run_cloud

        # One runner at a time in this process; the lease guards against any other.
        with self._lock:
            try:
                run_cloud(
                    self._open_store(), self._open_store, self._open_fetcher,
                    origin="web", doctor=self._doctor, heartbeat_seconds=self._heartbeat,
                    log=self._log,
                )
            except Exception as error:  # noqa: BLE001 - recorded on the request by run_cloud
                self._log(f"runner failed: {type(error).__name__}: {error}")

    def join(self, timeout: float | None = None) -> None:
        for thread in list(self.threads):
            thread.join(timeout)


class GitHubDispatcher:
    """``workflow_dispatch`` on the run workflow, with no inputs."""

    def __init__(
        self, token: str, repo: str, *, workflow: str = "run.yml", ref: str = "main",
        client: httpx.Client | None = None,
    ):
        self._token = token
        self._repo = repo
        self._workflow = workflow
        self._ref = ref
        self._client = client or httpx.Client(timeout=20.0)

    def wake(self) -> None:
        response = self._client.post(
            f"https://api.github.com/repos/{self._repo}/actions/workflows/"
            f"{self._workflow}/dispatches",
            json={"ref": self._ref},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code != 204:
            raise RuntimeError(
                f"GitHub refused the wake-up: {response.status_code} {response.text[:200]}"
            )


class NullDispatcher:
    """No runner to wake: the request waits for the scheduled run."""

    def wake(self) -> None:
        return None


def dispatcher_from_env(open_store: Callable[[], Any]) -> Dispatcher:
    choice = os.environ.get("JOBAGENT_DISPATCHER", "").strip().lower()
    token = os.environ.get("JOBAGENT_GITHUB_TOKEN", "").strip()
    repo = os.environ.get("JOBAGENT_GITHUB_REPO", "").strip()
    cloud = bool(os.environ.get("TURSO_DATABASE_URL", "").strip())

    if choice == "none":
        return NullDispatcher()
    if choice == "github" or (not choice and token and repo):
        if not (token and repo):
            raise RuntimeError("JOBAGENT_DISPATCHER=github needs JOBAGENT_GITHUB_TOKEN and _REPO")
        return GitHubDispatcher(
            token, repo,
            workflow=os.environ.get("JOBAGENT_GITHUB_WORKFLOW", "run.yml"),
            ref=os.environ.get("JOBAGENT_GITHUB_REF", "main"),
        )
    if choice == "thread" or not cloud:
        from ..infra.http import HttpFetcher

        return ThreadDispatcher(open_store, HttpFetcher)
    return NullDispatcher()
