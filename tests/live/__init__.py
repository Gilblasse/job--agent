"""Tests that hit real third-party endpoints.

Excluded from the default run by pyproject's ``addopts``, so CI never depends on somebody
else's uptime. Run them deliberately:

    pytest -m live

These could not be run during development -- every job-source host is refused at this
environment's egress proxy -- so they are written against the documented contracts and
are part of the handoff, not part of what has been verified.
"""
