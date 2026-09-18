# Architecture

## The constraint that shapes everything

**No applicant-tracking system offers cross-company search.** Lever states it plainly in
its own documentation — "The API does not: Let you do full-text searches over open jobs" —
and Greenhouse, Ashby, Recruitee, Breezy, BambooHR, Rippling and Personio expose no search
parameter at all. SmartRecruiters' `?q=` is scoped to one company.

Discovery is therefore **fan-out over a registry of known employer boards**, not a query.
Three consequences run through the whole design:

1. **The registry is the product's reach.** It ships seeded, it is ordered deliberately,
   and its health is tracked. It is a feature, not a cache.
2. **The request budget is the search strategy.** With thousands of boards and a finite
   budget, what you fetch *first* determines what you find.
3. **Filtering happens after retrieval, never in the query.** Folding a user's exclusions
   into a discovery query destroys recall silently — the job is missed because nothing
   asked for it, not because it failed a rule the user could see.

## Layers

```
      CLI  ──────────────────────────────────────────────┐
       │  typer + rich + questionary                     │
       ▼                                                 │
   engine/  planning → orchestrator → resolve → verify   │  depends inward only
       │                                                 │
       ▼                                                 │
   domain/  models · spec · gates · matching · dedup  ◄──┘  PURE. no I/O.
       ▲
       │  Protocols (ports.py)
   ┌───┴──────────────────────────────┐
   │                                  │
 sources/  ats · usajobs · registry   infra/  http · store · schema
```

`domain/` imports nothing from `infra/` or `sources/` and performs no I/O. Everything
environmental — network, clock, database — is declared as a Protocol in `ports.py`.

That is not decoration. It is why the entire pipeline is tested offline: the suite injects
a fetcher that replays recorded payloads, and the engine cannot tell the difference. It is
also what makes the cloud evolution below a swap rather than a rewrite.

## Module map

| Module | Responsibility |
|---|---|
| `domain/models.py` | Job, RawPosting, MatchResult, Coverage, and the enums |
| `domain/spec.py` | `SearchSpec` — the user's search as validated data; compiles to gates |
| `domain/gates.py` | The hard-gate vocabulary, including requirement-context detection |
| `domain/matching.py` | Gate evaluation plus the ranking ledger |
| `domain/normalize.py` | Location, workplace, salary, seniority, title and company normalization |
| `domain/places.py` | Country, region and city vocabulary for country detection; pure data |
| `domain/dedup.py` | Identity keys, URL canonicalization, authority tiers |
| `domain/taxonomy.py` | Profession-neutral vocabulary, overridable as data |
| `ports.py` | `Fetcher`, `Clock`, `SourceAdapter`, `JobRepository` |
| `infra/http.py` | The only network egress: robots, rate limiting, backoff |
| `infra/store.py` | SQLite persistence and the new-versus-seen question |
| `sources/ats/*` | Per-platform adapters |
| `sources/registry.py` | Seeding, growth, and fan-out ordering |
| `sources/discovery.py` | Careers URL → ATS board |
| `sources/commoncrawl.py` | Common Crawl index → employer boards; bounded, resumable sweeps |
| `engine/planning.py` | Spec → per-source requests and budget allocation |
| `engine/orchestrator.py` | The run: fan out, resolve, match, persist, report |
| `engine/doctor.py` | The go/no-go gate |

## Five decisions worth knowing

### Three gate outcomes, not two

A gate returns `PASS`, `FAIL` or `UNVERIFIABLE`. The third exists because most postings
omit pay, many omit working arrangement, and a remote posting frequently names no country
at all. Collapsing "does not say" into "does not qualify" discards jobs whose only flaw is
a terse description; collapsing it into "qualifies" surfaces jobs the user cannot take.

So it is kept as a third state, carried into the record, shown in the UI, and resolved by
a per-gate policy (`flag` keeps and marks; `strict` excludes).

### Requirement context, not keyword matching

`classify_requirement` in `domain/gates.py` is the least obvious code in the project and
the most important to the product.

Excluding "CPA-required" roles by matching the string "CPA" rejects every posting saying
"CPA a plus" — which is most of them. Instead each mention is located, the clause around
it is isolated, and requirement markers are weighed against preference markers by
proximity. Negations resolve first, so "no CPA required" counts *against* a requirement.
Section headings are consulted when no inline marker exists, ranked so "Preferred
Qualifications" beats the bare "qualifications" nested inside it. A genuine tie yields
`AMBIGUOUS` rather than a guess.

None of it knows what a CPA is; the vocabulary is ordinary English, kept in
`taxonomy.py`.

### Authority tiers decide the link

`employer career domain > official ATS > unverified`. The highest tier in a duplicate
cluster becomes the canonical record, and every other URL is retained as provenance. If
the most authoritative record has no description — common for a branded careers page that
embeds its ATS — the text is borrowed from a lesser record in the same cluster, because
gates read description text and a bare record would make half of them unverifiable.

### One egress chokepoint

Adapters receive a `Fetcher` and have no other way to reach the network. robots.txt,
per-host rate limiting, backoff and the user agent live in `infra/http.py` alone, which
makes the access policy a structural property rather than fourteen adapters each
remembering to be polite.

Rate limiting is per **host**, not per company, because several platforms concentrate
thousands of tenants behind one hostname; a per-tenant model turns a fan-out into a
self-inflicted rate-limit storm.

### Rate-limited is not gone

`company_registry` tracks `consecutive_failures` and `last_failure_kind` separately. A
throttled board is recorded but never counted against, because the boards most worth
reading are the ones most likely to throttle, and conflating the two is how a registry
quietly loses its best sources.

## Known trade-offs

These are decisions, not oversights.

**Identical titles collapse.** Two separate requisitions with the same title at the same
employer and location become one record carrying both URLs. Splitting them needs a
requisition id most sources do not publish, and showing the user the same job twice is
worse. — `domain/dedup.py`

**"Manager" is not a seniority level.** It is a rank in "Engineering Manager" and a job
family in "Project Manager", and nothing profession-neutral distinguishes them. Reading it
as a rank silently deletes every project-management role from a search excluding
management; not reading it as one merely shows some roles the user can exclude by title.
The recoverable error is the better one. — `domain/taxonomy.py`

**Workday costs a request per description.** Capped rather than unlimited. Without
descriptions the workplace and requirement gates cannot reach a verdict, which is exactly
what the onsite-metro case needs — so a budget, not zero. — `sources/ats/workday.py`

**Inflection is stemmed on the final word only.** Excluding "auditing" also catches
"audit"; words under five characters are left alone so "plus" does not start matching
"plush". — `domain/text.py`

**Word joins are flexible both ways.** A phrase is split on spaces and hyphens, and the
words may be joined in the text by a space, a hyphen, a slash or nothing, so "front-end",
"front end" and "frontend" are one phrase however the user typed the first two. The
closed form typed cannot be split, so it matches only itself. — `domain/text.py`

**A run reports progress in two phases.** While sources are read the bar counts boards
and the line beneath counts postings found; the matches count appears only once every
source has returned, because cross-source duplicates are merged before any job is judged
and judging early would either double the work or produce a count that falls after the
merge. The display hears about the run through `RunProgress` in `ports.py`, from worker
threads, and is guarded so a display bug can never read as a source failure. Results are
gathered in plan order whatever the completion order, so job ids and the coverage table
are the same from run to run. — `engine/orchestrator.py`, `cli/progress.py`

**Dismissal is a status of its own, and the reason is the record.** A dismissed job
carries `dismissed`, never `rejected` -- a gate rejects, and `results --rejected` shows
those -- and `record_match` never overwrites an existing status, so a dismissed job that
matches again stays hidden. The reason is stored verbatim in `job_feedback`; the rule the
user picked is what changes the search, because there is no model here to read the
reason and guessing would delete wanted jobs. — `domain/feedback.py`, `cli/dismiss.py`

**The wizard confirms short entries.** Anything three characters or under in a list that
scans prose is put back to the user: a two-letter language is also a verb, and a
one-letter one appears in "Plan C", and such a list rules a job out on a single hit.
Lists read in context — credentials, companies, places — are not interrupted.
— `cli/wizard.py`

**Discovered boards are a month or two stale.** The Common Crawl index is a periodic
snapshot, so a board opened last week is not in it. Accepted because the durable fact being
harvested is *which employers have boards*, while live postings still come from the ATS
APIs — and because the alternative, a search-engine API, no longer exists for free.
— `sources/commoncrawl.py`

**A sweep is a chore, not a crawl.** Each run reads a handful of index pages at one request
per second and saves its cursor. A full sweep of Workday therefore takes many invocations.
The index is run by a non-profit; pacing is the price of using it at all.
— `sources/commoncrawl.py`

**There is no response caching.** Conditional requests would cut bandwidth noticeably on
repeat fan-outs, but honouring a 304 means storing every board body, which is not built.
A run re-fetches. This is the most obvious efficiency work left. — `engine/planning.py`

## Evolving beyond one machine

Version 1 is local-first and free, and stays that way. The seams for anything larger are
already in place, and none of it is built:

| Step | What changes | What does not |
|---|---|---|
| Scheduled runs | a cron entry calling `run_search` | nothing |
| Serverless execution | `run_search` takes its store, fetcher and clock as arguments already | the engine, the gates, the ranking |
| Cloud database | a second `JobRepository` implementation | `domain/` and `engine/` |
| Multiple users | a user column on `searches` and `user_job_status` | the search logic |
| Web or mobile UI | a new adapter over the same engine | everything below `cli/` |
| Distributed source workers | `SourceAdapter.discover` is already an isolated unit of work | the orchestrator's contract |
| Notifications | a hook on the `is_new` computation, which already exists | the tracking model |

The load-bearing property is that `run_search` has no global state, no CLI dependency and
no direct I/O — it receives a store, a fetcher and a clock. Moving it into a Lambda is a
deployment question, not a rewrite.

## Testing

| Layer | Approach |
|---|---|
| `domain/` | Pure unit tests. Every gate, the requirement table, the US-gate table, ranking, dedup. |
| Adapters | Fixtures built from vendor-documented schemas, replayed through a fake fetcher. |
| Engine | Full pipeline offline, including failure paths: blocked hosts, partial failures, malformed payloads. |
| CLI | Typer's runner over every command that reads the database. |
| Genericity | One corpus, three unrelated searches, asserting different correct answers with no code change. |
| Live | `pytest -m live`, excluded by default. |

The genericity suite also parses `src/` and fails if a profession-specific term appears in
executable code. Docstrings may name a credential as an example; code may not.
