"""The acceptance test: a person sets preferences, searches, browses every match, reads
several postings and opens one to apply, without ever losing their place.

Drives the built web app in Chromium against ``scripts/e2e_server.py`` -- a demo
database, a local dispatcher that runs the real runner path over the fixtures, no GitHub
and no cloud. Excluded from the default run (``-m e2e``); CI's ``web`` job runs it.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "e2e"
RUN_TIMEOUT = 90_000


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server() -> str:
    built = ROOT / "web" / "dist" / "index.html"
    assert built.exists(), "build the web app first: cd web && npm run build"
    port = free_port()
    script = str(ROOT / "scripts" / "e2e_server.py")
    process = subprocess.Popen(
        [sys.executable, script, "--port", str(port), "--password", PASSWORD], cwd=ROOT
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if process.poll() is not None:
            raise RuntimeError("the e2e server exited early")
        time.sleep(0.5)
    else:
        process.kill()
        raise RuntimeError("the e2e server did not come up")
    yield base
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture
def app(page: Page, server: str) -> Page:
    page.goto(f"{server}/")
    page.fill("#password", PASSWORD)
    page.click("text=Sign in")
    page.wait_for_selector('[data-testid="search-jobs"]')
    page.select_option("#saved-search", label="accounting-remote")
    page.wait_for_selector('[data-testid="job-list"] [role="option"]')
    return page


def card_ids(page: Page) -> list[str]:
    return [c.get_attribute("data-job-id") or "" for c in page.locator('[role="option"]').all()]


def card_titles(page: Page) -> list[str]:
    titles = page.locator('[role="option"] [data-testid="card-title"]').all_inner_texts()
    return [t.strip() for t in titles]


def list_scroll(page: Page) -> float:
    return page.locator('[data-testid="job-list"]').evaluate("el => el.scrollTop")


def results_line(page: Page) -> str:
    return page.locator('[data-testid="results-line"]').inner_text()


def test_set_preferences_search_browse_select_read_apply_and_keep_your_place(
    app: Page, server: str
):
    page = app
    excluded_title = card_titles(page)[0]
    before = results_line(page)
    assert "Results from" in before

    # 1. Preferences: exclude the first result's title, pick Remote. Chips appear.
    page.click('[data-testid="more-filters"]')
    page.get_by_label("Titles to exclude outright").fill(excluded_title)
    page.click("text=Done")
    page.get_by_role("button", name=re.compile(r"^Remote")).click()
    page.get_by_label("Remote", exact=True).check()
    page.keyboard.press("Escape")
    chips = page.locator('[data-testid="filter-chip"]')
    expect(chips.filter(has_text=excluded_title)).to_have_count(1)
    expect(chips.filter(has_text="Remote")).to_have_count(1)

    # 2. Search jobs: the request waits, then runs, while the previous results stay put.
    page.click('[data-testid="search-jobs"]')
    page.get_by_text("Waiting for an available runner").wait_for(timeout=15_000)
    assert results_line(page) == before
    assert len(card_ids(page)) > 0

    # 3. Newer results are offered, never swapped under the user; accepting them shows
    #    the new rules applied.
    page.get_by_role("button", name="Newer results are ready — Show").wait_for(timeout=RUN_TIMEOUT)
    assert results_line(page) == before
    page.get_by_role("button", name="Newer results are ready — Show").click()
    page.wait_for_function(
        "(before) => document.querySelector('[data-testid=results-line]').innerText !== before",
        arg=before,
    )
    page.wait_for_selector('[data-testid="job-list"] [role="option"]')
    assert excluded_title not in card_titles(page)
    after = results_line(page)

    # 4. Every match is reachable.
    while page.get_by_role("button", name=re.compile("^Load more")).count():
        page.get_by_role("button", name=re.compile("^Load more")).click()
        page.wait_for_timeout(300)
    showing = page.locator('[data-testid="showing"]').inner_text()
    shown, total = re.findall(r"\d+", showing)[:2]
    assert shown == total, showing
    assert len(card_ids(page)) == int(total)

    # 5. Select several jobs: the detail changes, the selection is marked, the list does
    #    not move.
    page.locator('[data-testid="job-list"]').evaluate("el => { el.scrollTop = 80 }")
    page.wait_for_timeout(250)
    position = list_scroll(page)
    ids = card_ids(page)
    titles = card_titles(page)
    for index in range(min(3, len(ids))):
        card = page.locator(f'[data-job-id="{ids[index]}"]')
        card.click()
        expect(page.locator("#job-title")).to_have_text(titles[index], timeout=10_000)
        expect(card).to_have_attribute("aria-selected", "true")
        assert abs(list_scroll(page) - position) < 2
    chosen = ids[min(2, len(ids) - 1)]
    assert page.locator(".posting, .detail-pane .muted").count() > 0
    expect(page.locator(".why")).to_be_visible()

    # 6. Open the employer's posting in a new tab and come back to the same place.
    with page.expect_popup() as popup_info:
        page.click('[data-testid="apply-link"]')
    popup_info.value.close()
    expect(page.locator(f'[data-job-id="{chosen}"]')).to_have_attribute("aria-selected", "true")
    expect(chips.filter(has_text=excluded_title)).to_have_count(1)
    assert abs(list_scroll(page) - position) < 2

    # 7. Reload: the same search, run, job and filters come back.
    url_before = page.url
    assert "run=" in url_before and f"job={chosen}" in url_before
    page.reload()
    page.wait_for_selector(f'[data-job-id="{chosen}"][aria-selected="true"]', timeout=15_000)
    assert results_line(page) == after
    expect(page.locator('[data-testid="filter-chip"]').filter(has_text=excluded_title)).to_have_count(1)
    assert len(card_ids(page)) == int(total)

    # 8. A run that fails says so, offers a retry, and leaves the results alone.
    httpx.post(f"{server}/e2e/outcome", json={"outcome": "fail"}, timeout=5)
    page.click('[data-testid="search-jobs"]')
    page.get_by_text(re.compile("^Search failed")).wait_for(timeout=RUN_TIMEOUT)
    expect(page.get_by_role("button", name="Retry")).to_be_visible()
    assert results_line(page) == after
    httpx.post(f"{server}/e2e/outcome", json={"outcome": "succeed"}, timeout=5)

    # 9. When the run on screen expires, the next request says so and offers the latest.
    pinned = int(re.search(r"run=(\d+)", page.url).group(1))
    httpx.post(f"{server}/e2e/advance", json={"runs": 3}, timeout=120)
    page.locator("#sort").select_option("title")
    page.get_by_role("button", name="Results expired — show latest").wait_for(timeout=15_000)
    page.get_by_role("button", name="Results expired — show latest").click()
    page.wait_for_selector('[data-testid="job-list"] [role="option"]', timeout=15_000)
    latest = int(re.search(r"run=(\d+)", page.url).group(1))
    assert latest > pinned
    assert "Results from" in results_line(page)
