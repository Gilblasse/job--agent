# Working on jobagent

## The constraint that explains the design

No ATS offers cross-company search (Lever documents this explicitly). Discovery is
**fan-out over a registry of known employer boards**, so the registry is the product's
reach, and the request budget is the search strategy. Before changing discovery, read
`docs/ARCHITECTURE.md`.

## Scope — do not widen without being asked

- **US only.**
- **Employer ATS boards and official employer systems only.** No job-board aggregators
  (no Indeed, LinkedIn, RemoteOK, Remotive, Adzuna). USAJOBS is in scope because it is the
  federal employer's own system.
- **Free forever.** No paid APIs, databases, proxies, or trials that expire.
- **Legitimate access only.** robots.txt respected, rate limits honoured, nothing
  bypassed. A source that cannot be read legitimately is a recorded coverage gap.

## Rules that the code enforces structurally

- `domain/` is pure: no I/O, no network, no sqlite. Everything environmental goes behind a
  Protocol in `ports.py`.
- **All network traffic goes through `infra/http.py`.** Adapters receive a `Fetcher`. Never
  add a direct `httpx` call in an adapter; that would bypass robots and rate limiting.
- **No profession may appear in executable code.** A test parses `src/` and fails on it.
  Docstrings may name a credential as an example; code may not.

## Conventions

- Run `pytest -q` and `ruff check src tests scripts` before committing. Both must be clean.
- The suite is offline. Adapter fixtures live in `tests/fixtures/` and come from
  vendor-documented schemas; anything hitting the network is marked `@pytest.mark.live`
  and excluded by default.
- Gates return three outcomes. If you add one, decide deliberately what `UNVERIFIABLE`
  means for it — "the posting did not say" is not the same as "the posting disqualifies".
- Every rejection must name its rule and quote the triggering text. A gate that fails
  without evidence is incomplete.

## Before trusting any result

No adapter in this repository has ever run against a live endpoint, and no robots.txt has
ever been fetched — the environment it was built in blocks every job-source host. Run
`./scripts/live_validate.sh` on a real network first, and read `jobagent sources doctor`
before believing an empty result means an empty market.
