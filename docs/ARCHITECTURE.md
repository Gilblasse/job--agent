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

Version 1 is local-first and free, and stays that way. The seams for anything larger were
in place from the start; the web deployment below used them:

| Step | What changes | What does not | Status |
|---|---|---|---|
| Scheduled runs | a GitHub Actions workflow calling `jobagent cloud run` | nothing | built |
| Serverless execution | the API runs on Vercel; the fan-out runs on GitHub Actions | the engine, the gates, the ranking | built |
| Cloud database | the same `Store` over `infra/turso.py` | `domain/` and `engine/` | built |
| Web UI | `web/` and `src/jobagent/web/`, a second adapter over the same engine | everything below `cli/` | built |
| Multiple users | a user column on `searches` and `user_job_status` | the search logic | not built |
| Distributed source workers | `SourceAdapter.discover` is already an isolated unit of work | the orchestrator's contract | not built |
| Notifications | a hook on the `is_new` computation, which already exists | the tracking model | not built |

The load-bearing property is that `run_search` has no global state, no CLI dependency and
no direct I/O — it receives a store, a fetcher and a clock. Moving it elsewhere was a
deployment question, not a rewrite.

## The web deployment

```
 browser  ──►  Vercel Hobby ────────────────────────────►  Turso (libSQL, free)
 React SPA     app.py → FastAPI (src/jobagent/web/)          POST /v2/pipeline (Hrana)
 (web/dist)    Store over infra/turso.py (baton transactions)        ▲
               POST /api/runs → queued row (unique) + wake-up         │ publish: atomic batches,
                                                                      │   lease-guarded
              GitHub Actions  .github/workflows/run.yml  ─────────────┘ pull: searches, registry
              cron daily + wake-ups · `jobagent cloud run` (no inputs)
              take lease → consume the queued request → pull → doctor
              → per search: run_search → publish → retention
```

The measured facts that shaped it: a run touches ~20k jobs and writes ~20k verdict rows of
~1.7 KB each in ~120k statements, so the engine cannot run statement-by-statement over
HTTP, and page-based replica sync would cost ~150–200 MB per run against a 3 GB monthly
quota. So the runner keeps nothing between runs: it pulls the searches and the registry
into a scratch SQLite file, runs the unchanged engine, and publishes each run in batches
of a few hundred statements.

Seven invariants, each enforced in code and pinned by a test:

1. **One writer per table.** The API writes `searches`, `user_job_status`,
   `job_feedback`, `company_registry` inserts and the request queue; the runner writes
   `runs`, `run_sources`, `jobs`, `job_sources`, `job_search_matches`, registry health,
   the lease and the guard. — `infra/publish.py`
2. **Exactly one publisher, and takeover invalidates the previous owner atomically.**
   `publish_lease` is a single row; taking it overwrites the token in the same
   transaction that closes the old owner's request. From then on the old runner's
   heartbeat, every guard and its `finish_request` match zero rows. No reconciliation is
   involved in correctness. — `Store.take_lease`, `tests/integration/test_lease.py`
3. **Every cloud write group is atomic and lease-proven.** A publish batch is one Hrana
   `batch` request whose steps are conditioned on the previous step's success, `COMMIT`
   on the last and `ROLLBACK` on its negation; its first statements insert the number of
   valid leases held by the writer's token into `publish_guard(ok CHECK (ok = 1))`. Zero
   fails the CHECK and the whole batch rolls back. Short API-side groups (a dismissal:
   rule, status and reason) are baton-based interactive transactions. —
   `infra/turso.py`, `Store._tx`, `Store.guard_statements`
4. **At most one queued request, by constraint.** A partial unique index on
   `run_requests(status) WHERE status = 'queued'` makes "create or attach" atomic under
   concurrent clicks. The workflow carries no inputs: every run consumes whatever is
   queued, so a woken run that GitHub replaces with a scheduled one strands nothing. —
   `Store.create_queued`, `Store.consume_request`
5. **A results screen is pinned to one available run and cannot move.** Every read is
   scoped to `m.run_id`; cards and ordering use fields frozen on the verdict row at
   publish (never `jobs.*`), and every order ends with `m.id`. A search's latest three
   completed runs are available and untouched by retention; an older one answers
   `410 {expired, latest_run}`. The posting body is the current text, with a
   "changed since it was evaluated" notice when the hash differs. — `Store.results`,
   `Store.expire_runs`, `web/app.py`
6. **Search identity cannot be reused.** `searches.uid` (uuid4, unique, immutable) is
   what runs, requests and the runner carry; SQLite reuses row ids, so `id` is a local
   join key only. Every edit bumps `revision`, and an update with a stale revision is
   refused. — `Store.save_spec`, `Store.update_search`
7. **Writes are counted, not assumed.** Every Hrana result carries `rows_written`; the
   publish report sums them onto the request row, so the free-tier budget is measured on
   the first live run rather than estimated.

Retention: rejected verdicts of runs older than the available window are pruned, but
only for jobs that have a verdict in a newer run, so a job not encountered again keeps
its latest verdict and an old match can never resurface because a newer rejection was
pruned. Match verdicts are never pruned. Jobs unseen for 60 days with no saved, applied
or dismissed status, no feedback and no verdict in an available run are deleted.

Who runs a queued request is the dispatcher's choice (`web/dispatch.py`): in-process, in
a background thread with the real fetcher, when the app runs on a local SQLite database
— the website and the command line are then one system; GitHub Actions when deployed,
because a Vercel function cannot host a minutes-long fan-out; nobody, until the schedule,
when the cloud database is configured without GitHub credentials. The runner path is the
same in every case, and it seeds the registry from the bundled file when it is empty.

### The search screen

"Search jobs" saves the rules (with the revision) and queues a request; it does not
evaluate anything. The screen keeps showing one completed run, with its timestamp, until
the user accepts "Newer results are ready". The URL carries the search, run, job, sort,
decision and the number of rows loaded, so refresh, Back and returning from the employer's
tab land in the same place. The top bar's controls are spec fields (`titles`,
`locations`, `salary_min`, `workplace`, `employment_types`); "More filters" holds the
rest in the wizard's sections and wording; "Active filters" are the non-default fields.
Postings are rendered from the text the engine stored, cut at the posting's own headings
by the same rule the gates use (`split_sections` in `domain/gates.py`); nothing is
summarised or invented. Runs from an older revision of the search are flagged.

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
