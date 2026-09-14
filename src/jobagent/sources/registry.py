"""The company registry: this product's discovery surface.

No ATS offers cross-company search, so a search reaches exactly as far as the list of
boards it knows about. That makes the registry a first-class feature rather than a cache:
it ships seeded, it grows from what the user finds, and its health is tracked so a run is
not spent re-probing boards that are gone.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from ..infra.store import Store
from ..sources.ats.base import BoardTarget
from .discovery import extract_board, split_token

SEED_PACKAGE = "jobagent.data"
SEED_FILENAME = "companies.seed.json"


@dataclass
class SeedReport:
    added: int = 0
    existing: int = 0
    total: int = 0
    by_platform: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_platform is None:
            self.by_platform = {}


def load_seed_file() -> dict[str, Any]:
    """Read the bundled seed, wherever the package is installed."""
    try:
        text = resources.files(SEED_PACKAGE).joinpath(SEED_FILENAME).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        local = Path(__file__).resolve().parents[1] / "data" / SEED_FILENAME
        if not local.exists():
            return {"companies": []}
        text = local.read_text(encoding="utf-8")
    return json.loads(text)


def seed_registry(store: Store, *, only_us: bool = False) -> SeedReport:
    """Load the bundled boards into the user's registry.

    Idempotent: re-seeding after an upgrade adds newly-known boards and leaves the health
    history of existing ones intact.
    """
    data = load_seed_file()
    companies = data.get("companies") or []
    report = SeedReport()

    for row in companies:
        if only_us and not row.get("us_signal"):
            continue
        added = store.add_company(
            company=row["company"],
            ats=row["ats"],
            token=row["token"],
            domain=row.get("domain"),
            board_url=row.get("board_url", ""),
            source="seed",
            us_signal=bool(row.get("us_signal")),
            notes=json.dumps(row.get("extra") or {}) if row.get("extra") else "",
        )
        report.added += int(added)
        report.existing += int(not added)
        report.total += 1
        report.by_platform[row["ats"]] = report.by_platform.get(row["ats"], 0) + 1

    return report


def add_from_url(
    store: Store, url: str, *, company: str | None = None, us_signal: bool = True
) -> list[BoardTarget]:
    """Register whatever board a URL points at."""
    board = extract_board(url)
    if board is None:
        return []
    name = company or board.token
    store.add_company(
        company=name, ats=board.platform, token=board.registry_token(), board_url=url,
        source="user", us_signal=us_signal,
        notes=json.dumps(board.extra) if board.extra else "",
    )
    return [BoardTarget(company=name, token=board.token, board_url=url, extra=dict(board.extra))]


def import_csv(store: Store, path: Path) -> tuple[int, list[str]]:
    """Bulk-load boards from a CSV.

    Accepts either a ``url`` column (any board or careers URL) or explicit ``ats`` and
    ``token`` columns. With only two growth paths left, loading a list someone already has
    should not be a thousand separate commands.
    """
    added = 0
    problems: list[str] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            keys = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            company = keys.get("company") or keys.get("name") or ""
            url = keys.get("url") or keys.get("board_url") or keys.get("careers_url") or ""
            ats, token = keys.get("ats", ""), keys.get("token", "")

            if url and not (ats and token):
                board = extract_board(url)
                if board is None:
                    problems.append(f"row {index}: no recognizable ATS board in {url!r}")
                    continue
                ats, token = board.platform, board.registry_token()
                extra = board.extra
            else:
                extra = {}

            if not (ats and token):
                problems.append(f"row {index}: needs a url, or both ats and token")
                continue

            store.add_company(
                company=company or token, ats=ats, token=token, board_url=url,
                source="import", us_signal=_truthy(keys.get("us", "1")),
                notes=json.dumps(extra) if extra else "",
            )
            added += 1
    return added, problems


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "us", "united states"}


def targets_for(
    store: Store, platform: str, *, limit: int, prefer_us: bool = True,
    search_text: str = "",
) -> list[BoardTarget]:
    """Select this run's boards for one platform, in fan-out order."""
    rows = store.registry_targets(platforms=[platform], limit=limit, prefer_us=prefer_us)
    targets: list[BoardTarget] = []
    for row in rows:
        # The stored token carries the tenancy for platforms that need it; split it back
        # into the tenant plus its routing metadata before building a request.
        token, extra = split_token(platform, row["token"])
        if row["notes"]:
            try:
                loaded = json.loads(row["notes"])
                if isinstance(loaded, dict):
                    extra = {**{str(k): str(v) for k, v in loaded.items()}, **extra}
            except json.JSONDecodeError:
                pass
        if search_text:
            # Workday supports real server-side search, so the query is pushed down
            # rather than pulling whole boards back to filter locally.
            extra["search_text"] = search_text
        targets.append(
            BoardTarget(
                company=row["company"], token=token, registry_id=int(row["id"]),
                board_url=row["board_url"], domain=row["domain"], extra=extra,
            )
        )
    return targets
