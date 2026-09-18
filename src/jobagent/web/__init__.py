"""The web adapter: a FastAPI API over the same engine, and the runner's wake-up call.

Imports ``domain/``, ``engine/`` and ``infra/`` only -- never ``cli/app`` -- and renders
postings as text sections, never as HTML.
"""
