"""Fixture payloads.

IMPORTANT: most of these are built from each vendor's *documented* response schema, not
recorded from live traffic. The environment this was developed in blocks every job-source
host at the proxy, so no live capture was possible.

The exception is ``workable_board.json``: its field names, value forms and the
one-entry-per-city repetition are copied from a live widget response read on
2026-09-15, with the employer and postings replaced by invented ones.

They are therefore correct about field names and shapes, and they prove the adapters map
those fields properly -- but they cannot prove the live endpoints still behave this way.
That is what `jobagent sources doctor` and `pytest -m live` are for, and why the README
says so plainly.
"""
