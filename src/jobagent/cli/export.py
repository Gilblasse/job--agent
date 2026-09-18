"""Getting results out of the tool.

A job search runs for weeks across spreadsheets, notes and other people's advice, so the
data has to leave here in ordinary formats rather than being trapped behind this CLI.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any


def load_explanation(row: Any) -> dict[str, Any]:
    """The stored verdict as a dict; empty when a row has none."""
    try:
        return json.loads(row["explanation"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {}


COLUMNS = [
    "id", "title", "company", "location", "workplace", "employment_type",
    "salary_min", "salary_max", "salary_period", "posted_at", "first_seen", "last_seen",
    "score", "decision", "uncertain", "is_new", "user_status", "verification", "url",
    "why",
]


def _why(row: Any) -> str:
    """A one-line answer to "why is this here", so an export stands on its own."""
    explanation = load_explanation(row)
    failed = [g for g in explanation.get("gates", []) if g["outcome"] == "fail"]
    if failed:
        return "; ".join(
            f"{g['gate']}: {g['rule']}" + (f" (matched {g['evidence']!r})" if g["evidence"] else "")
            for g in failed
        )
    signals = explanation.get("signals", [])
    return "; ".join(f"{s['name']} {s['points']:+g}" for s in signals)


def _record(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "company": row["company"],
        "location": row["location_raw"], "workplace": row["workplace"],
        "employment_type": row["employment_type"], "salary_min": row["salary_min"],
        "salary_max": row["salary_max"], "salary_period": row["salary_period"],
        "posted_at": row["posted_at"], "first_seen": row["first_seen"],
        "last_seen": row["last_seen"], "score": row["score"], "decision": row["decision"],
        "uncertain": bool(row["uncertain"]), "is_new": bool(row["is_new"]),
        "user_status": row["user_status"], "verification": row["verification"],
        "url": row["url"], "why": _why(row),
    }


def to_csv(rows: list[Any]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(_record(row))
    return buffer.getvalue()


def to_json(rows: list[Any]) -> str:
    return json.dumps([_record(r) for r in rows], indent=2) + "\n"


def to_markdown(rows: list[Any]) -> str:
    """A shareable summary: one section per job, with the reasoning kept."""
    lines = ["# Job search results", ""]
    for row in rows:
        flags = []
        if row["is_new"]:
            flags.append("**NEW**")
        if row["uncertain"]:
            flags.append("_unconfirmed requirements_")
        heading = f"## {row['title']} — {row['company']}"
        if flags:
            heading += "  " + " ".join(flags)
        lines.append(heading)
        lines.append("")
        lines.append(f"- **Where:** {row['location_raw'] or 'not stated'} ({row['workplace']})")
        if row["salary_min"] or row["salary_max"]:
            lines.append(
                f"- **Pay:** {row['salary_min'] or '?'}-{row['salary_max'] or '?'} "
                f"{row['salary_currency'] or ''}/{row['salary_period'] or ''}"
            )
        lines.append(f"- **Posted:** {row['posted_at'] or 'not stated'}")
        lines.append(f"- **Status:** {row['user_status']}")
        lines.append(f"- **Score:** {row['score']:.0f}")
        lines.append(f"- **Apply:** {row['url']}")
        lines.append(f"- **Why:** {_why(row)}")
        lines.append("")
    return "\n".join(lines)


FORMATS = {"csv": to_csv, "json": to_json, "md": to_markdown}
