"""Vercel's entrypoint: the FastAPI app, with the src/ layout on the path.

Locally: ``uvicorn app:app --reload`` with ``JOBAGENT_DB`` pointing at a SQLite file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from jobagent.web.app import app, serve_static  # noqa: E402

serve_static(app)

__all__ = ["app"]
