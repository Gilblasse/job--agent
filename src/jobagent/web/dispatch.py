"""Waking the runner.

A request to run the searches is a row in the database (``run_requests``); the runner
consumes it whenever it starts. The dispatcher only makes it start sooner, by triggering
the GitHub workflow. It carries no work of its own, which is why a wake-up that GitHub
replaces with a scheduled run loses nothing.

Not a job source: this reaches the user's own CI, so it does not go through the fetcher.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx


class Dispatcher(Protocol):
    def wake(self) -> None: ...


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
    """No credentials configured: the request waits for the scheduled run."""

    def wake(self) -> None:
        return None


def dispatcher_from_env() -> Dispatcher:
    token = os.environ.get("JOBAGENT_GITHUB_TOKEN", "").strip()
    repo = os.environ.get("JOBAGENT_GITHUB_REPO", "").strip()
    if not token or not repo:
        return NullDispatcher()
    return GitHubDispatcher(
        token, repo,
        workflow=os.environ.get("JOBAGENT_GITHUB_WORKFLOW", "run.yml"),
        ref=os.environ.get("JOBAGENT_GITHUB_REF", "main"),
    )
