# State

**Product Goal:** a profession-agnostic CLI that finds jobs on employer ATS boards,
filters them hard with stated reasons, and remembers what it has shown.

**Status:** implemented, locally verified, and live-validated on a home network
(2026-09-14 and 2026-09-15). Not yet validated through real user outcomes.

**Depth:** Full — multi-component, external dependencies, new subsystem.
**Mode:** autonomous scope; all six milestones delivered in one pass.

## Milestones

| | Deliverable | Status |
|---|---|---|
| M1 | Domain models, gate vocabulary, SearchSpec, SQLite schema | done |
| M2 | Normalization, explainable matching, deduplication | done |
| M3 | HTTP egress, P1 adapters, seeded registry, go/no-go gate | done |
| M4 | Orchestrator, authority resolution, verification, new-vs-seen | done |
| M5 | CLI, wizard, results, export | done |
| M6 | Genericity proof, docs, live-validation handoff | done |
| M7 | Registry growth: Common Crawl board discovery | done |
| M8 | First live validation, and the defects it found | done |
| M9 | Resolve the P2 robots holds: Workable ships, SmartRecruiters stays out | done |
| M10 | Wizard: preview crash, scope-labelled prompts, flexible compounds, short-entry check | done, uncommitted |
| M11 | Run progress bar, job ids and links in results, `dismiss` with reasons that teach the search | done, uncommitted |

## Verification evidence

- 604 tests pass (`pytest -q`), plus 14 live tests deselected by default.
  `ruff check src tests scripts` clean.
- Genericity proven the hard way: one corpus, three unrelated searches, different correct
  answers, no code change. A test parses `src/` and fails on profession-specific terms in
  executable code.
- Failure paths exercised: blocked host, partial source failure, malformed payload, empty
  board, missing credentials.
- `sources doctor` was run against the real network here and failed honestly, naming the
  egress refusal per source rather than implying the sources were broken.

## M11 — progress, links, dismiss, 2026-09-15

Depth: Standard (plan approved with three review notes, all folded in). Plan file:
`~/.claude/plans/no-but-instead-lets-vivid-lobster.md`.

- **Progress** (`ports.RunProgress`, `cli/progress.py`): two phases by design
  (decision 35). Hooks are called from worker threads, guarded in the orchestrator, and
  `as_completed` drives the display while results are gathered in plan order. Verified
  offline (hook counts agree with the outcome; plan order holds when the first source
  finishes last; a broken display leaves every source OK) and live on a scratch DB
  through the real `run` command under a non-TTY console.
- **Results** (`cli/render.py`): ID column, hyperlinked title, folded Link column,
  `--no-links`, hint line, Status widened to fit "dismissed", `-0` scores gone.
- **Dismiss** (`domain/feedback.py`, `cli/dismiss.py`, `dismiss`, `dismissed`,
  `results --all`, migration 3 `job_feedback`, `UserStatus.DISMISSED`): reason required,
  rule chosen; title rules refused when they would also exclude a wanted title; bare
  `seniority` takes the title's highest level. Verified offline (13 CLI tests including
  the survive-a-rerun test the user asked for) and live: a real match dismissed with an
  employer and a duty rule, listed by `dismissed`, hidden from `results`.
- **Found on the way:** `normalize_title` cut hyphenated titles at their own hyphen when a
  location suffix followed (decision 38). Fixed and pinned.

Independent review (`feature-dev:code-reviewer`), collected. It reported one BLOCKER and
one MAJOR, both in the rewritten `_strip_location_suffix`: "- US" / "- Remote - US"
suffixes not stripped, and "Analyst - Georgia Operations" stripped to "Analyst". Both
were checked against the pre-change code (`git show HEAD:` run side by side) and were
**pre-existing behaviour, not regressions**: old and new normalised every named case
identically, and the only behavioural change in that helper was the hyphenated-title
fix. Reclassified MINOR (both cheap, and the second now feeds the dismiss title
suggestion) and fixed: a tail is a place only when it is wholly an arrangement word,
"US"/"United States", a state, a "City ST" pair, or an exact vocabulary entry. Eight
cases added to `TestTitleLocationSuffixes`. Everything else the reviewer checked --
display thread-safety, the guarded hooks, bar completion under early exits, SQL
parameters, genericity, the absence of "rejected" from dismiss output -- was clean.

Not verified: the bar's rendering in a real terminal (this session has no TTY). The user
sees it on the next `run`.

## M10 — the first real wizard session, 2026-09-15

Depth: Light. The user ran `search create` for the first time and pasted the transcript.

1. **The preview crashed after the last question, before saving.** `_describe_gate`
   built one dict of f-strings for every gate, so the workplace line's `.value` ran
   against the country gate's plain strings. Every wizard run hit it; no test covered
   the wizard at all. Now lazy, one description per gate, every compiled gate described
   in words (six had fallen through to their internal names), and a test that runs the
   preview over a spec with every gate populated -- confirmed red on the old code.
2. **Prompts now say where they look** -- TITLE, DUTIES or ANYWHERE -- with a legend at
   the top of the role section. The user's transcript had backend stacks under "duties
   you do not want" (right) and a frontend framework under "anything else that rules a
   job out" (posting-wide; one blurb mention would lose the job). The genericity test
   rejected the first draft for naming a library in a help string; the shipped wording
   is neutral.
3. **Compounds: "front-end", "front end" and "frontend" are one phrase** (decision 32).
   The transcript listed six spellings of two titles.
4. **Short entries are confirmed** (decision 33): the transcript had "go" and "c" in a
   rule-out list. `tests/unit/test_text.py` is new and pins both the join behaviour and
   why short words are wide.

Self-reviewed only (Light depth); 538 tests, ruff clean. Not committed: the user has
not asked. Two things the user should know before re-entering that search: "posted
within 3 days" will be very thin, and the language names still need spelling out.

## M9 — the P2 robots question, answered live, 2026-09-15

Depth: Standard. Mode: autonomous, USAJOBS excluded by the user.

Both P2 hosts' robots.txt were read from this network with the tool's own User-Agent:

- `api.smartrecruiters.com`: `User-agent: *` / `Disallow: /`, with `Allow: /v1/companies/`
  for `LinkedInBot` alone. The carve-out makes the refusal deliberate. **Stays unread**
  (decision 28); its 287 boards remain seeded so `company list` shows the split.
- `apply.workable.com`: `Disallow:` (empty), i.e. everything allowed. `www.workable.com`
  disallows only `/admin`, `/auth/google`, `/user_password_resets`, `/j/`, and redirects
  its API path to the apply host. **Workable ships** (decision 27).

The adapter reads the widget API (`/api/v1/widget/accounts/{token}?details=true`), one
request per tenant. Its fixture is the first in the suite shaped from a live capture
rather than a documented schema. Two facts about the live shape drove the design:

1. A job open in N cities comes back N times under one shortcode and URL. The adapter
   folds them into one posting whose `location_raw` lists every city. Emitting them
   separately would have given the deduper N postings with one identity, and whichever
   city survived would decide whether a metro search saw the job.
2. `telecommuting` is the only workplace field and it is a boolean: true → REMOTE, false
   → UNKNOWN (not ONSITE; the description decides hybrid versus onsite).

The fold exposed a `parse_location` defect that USAJOBS multi-location postings already
had: scanning a semicolon-separated list for a spelled-out state found whichever state
sorts first alphabetically and took everything before it as the city. It now structures
the first place and keeps the whole list in `raw` (decision 29), pinned in
`tests/unit/test_normalize.py`.

Live, from this machine: `sources doctor` passed with five adapters and **1,479 routable
boards** (was 1,307). The DFW search fanned out to all 172 Workable boards in 43 s, 162
read, 10 dead tenants (404), 1,141 postings, no 429 — the "429s at fan-out scale" note in
the old catalog entry did not reproduce at the standard per-host pace.

Also this iteration: `scripts/live_validate.sh` finds the venv on Windows (`Scripts/`)
as well as POSIX (`bin/`), and its header no longer claims it has never run. The Common
Crawl question is drafted in `.agile/commoncrawl-question.md`, not sent — sending it is
the user's call. Re-read today, `index.commoncrawl.org/robots.txt` still says
`Disallow: /` but now lists explicit `Allow`s for `collinfo.json` and a few top-level
files, none of them the `-index` query paths; decision 26 stands.

Full live validation with the five adapters (`scripts/live_validate.sh`, report in the
session scratchpad, not committed): gate passed; accounting 16,751 postings / 9 matches
; React 19,567 / 35; DFW 20,162 / 4, the same four Dallas roles as M8. A baseline run of
the same script at `2d153ef` the same morning (`live-validation-20260915-004603.txt`
in the repo root, untracked, not mine) read 17,715 / 20,119 / 20,407 for the same
9 / 35 / 4 matches: the fixed 400-request budget re-sliced across five platforms costs
about 5% of postings read and no matches. Not a regression in matching. Workable: 727 postings from 47 boards, no matches in any of the three
— agencies, studios and game companies, rejected with rule and quote.

The DFW run, the only one that started after the budget fix landed, shows every
platform reading exactly its planned board count ("61 boards read", "47 boards read")
and no phantom shortfall; the two earlier runs in the same report still show it, having
started before the edit. The script's final re-run of the accounting search reported
10 matched, 2 new: dead boards found in run 1 sank in the fan-out order and live boards
took their places (20,306 postings read against 16,751), so two matches came from boards
never read before -- correct new-versus-seen behaviour, not a tracking defect. The
baseline run's re-run showed the same board churn (19,108 against 17,715) and 0 new.

That re-run exposed a third defect: the post-run "New matches (2)" table listed three
jobs. It queried every job whose latest verdict was still flagged new, unscoped to the
run, so a job first seen in run 1 and simply not re-read in run 4 appeared under run 4's
heading. Fixed by scoping the query to the run; the regression test drives the CLI
offline and was confirmed red without the fix.

## Independent review — M9

One pass, `feature-dev:code-reviewer`, collected. No blockers. Two MAJOR findings, both
verification completeness rather than behaviour, both fixed: the live robots test still
described Workable as held back and did not check `apply.workable.com` (now checked, and
the docstring states the SmartRecruiters finding); and the fold's defensive branches
(non-dict entry, entry with no identity, entry with no structured locations) had no
fixture coverage (three entries and two tests added). The reviewer had no shell and
reviewed from file state rather than `git diff`; it named the files it read.

Running the live tests after that fix found something outside this iteration's scope:
`data.usajobs.gov` publishes `Disallow: /`. Recorded under next actions; USAJOBS was
excluded from this iteration by the user.

## M8 — first live run, 2026-09-14

Cloned to a home network. `sources doctor` passed on the first attempt: all four P1
adapters returned real postings, and no robots.txt blocks the documented API paths. The
accounting benchmark read 18,238 live postings in about three minutes.

The first pass surfaced 18 matches, and 15 of them were wrong. Four defects, none of
which any fixture had exercised, each now pinned in `tests/unit/test_live_findings.py`
with the real input that exposed it:

1. **Country detection was a list of sixty names.** "Croatia", "Hong Kong", "Mumbai",
   "FR - Paris", "AU - Melbourne" and "Remote-Iberia" all read as "does not state a
   country" and surfaced as flags on a US-only search. "San Francisco, Remote" read as
   unknown in the other direction. Replaced with `domain/places.py`: every country,
   regions, sub-national regions, ~330 non-US and ~200 US cities, compiled once.
2. **Relevance was satisfied by a passing mention.** Title phrases were searched in the
   body; "Marketing Coordinator" passed on "liaise with our accountant". Now title
   phrases match the title, responsibility phrases match the duties section.
3. **An over-budget Workday board flooded the results.** With no description fetched, the
   first fix reported relevance as unverifiable, and twenty Adobe engineering and sales
   roles appeared as flags. The title always reaches a verdict; it now fails.
4. **A barred responsibility fired anywhere in the posting.** "Manager, Accounts Payable
   and Expenses" in San Ramon was rejected on "audit trail" in its requirements. Barred
   responsibilities are now scoped to the duties section, as wanted ones are.

Third run: 18,238 postings, 10 matches, all US; the top result a remote Technical
Accountant. The remaining weak matches are the known limit of a phrase gate -- it cannot
tell *doing* accounts payable from *selling* accounts-payable software -- and the ranking
scores them accordingly.

**React search, live.** 43 matches on the first pass, two of them an options trader and
a macro analyst. Two causes: the example spec's `component` matched "salary is one
component of total compensation", and `React` as a required skill matched "react to
volatility shifts". The spec is fixed; the engine now matches skills as names -- no
inflection, and with the user's casing (decision 25). Second pass: 35 matches, all
software or UI roles, the top seven remote frontend roles in the US.

**DFW search, live.** The case the README called hardest. 13 matches, all US, all
hybrid or onsite, all in the metro; four after tightening `stakeholder` and `schedule`
in the example spec. Top: Project Manager, Brillio, Dallas, hybrid. That is a fact about
the market across 1,307 ATS boards, and a better one than predicted.

**`company discover`, live.** Refused. Both Common Crawl hosts answer `Disallow: /`
(decision 26). The feature stays built and tested; the CLI now explains the refusal.

Not yet run live: USAJOBS (needs a free key).

## M7 — board discovery from the Common Crawl index

Added because reach *is* the registry, and the registry only grew from a vendored seed or
the user typing a URL. Every free whole-web search API is gone as of 2026, so the index of
a public crawl is what remains — free, keyless, non-profit, and meant to be queried.

Its one irreplaceable capability: Workday tenants are hostnames, so a subdomain wildcard
enumerates them where no `site:` query on any engine can. That is the exact gap in
coverage — onsite, non-tech, US.

Three defects found by the tests written for it, all now fixed and pinned:

- `complete` compared pages read against total pages, so a resumed sweep could never be
  marked finished, and a sweep that stopped on an error was marked finished if it happened
  to have read enough pages. It now compares the cursor, and a sweep that stopped early is
  never complete.
- An empty index page ended a sweep with no note, indistinguishable in the progress table
  from "swept and found nothing".
- A dry run counted a board once per pattern, so Greenhouse's two hostnames reported twice
  the boards the real run would register.

Also fixed here, carried over: a robots.txt transport failure reported only "could not be
fetched", which reads as the host refusing us. It now names the cause, so a proxy 403 is
distinguishable from a host's decision. The fail-closed posture is unchanged and tested.

## Second review pass (GitHub Copilot, on PR #1)

CI was green, but the reviewer returned "Changes recommended": 16 posted comments and 22
suppressed. All 38 were triaged and fixed, each with a regression test.

The worst was a credential leak: `live_validate.sh` used `${VAR:-default}`, which expands
to the *value* when the variable is set, so the line meant to print "configured" wrote the
USAJOBS API key into a report saved to disk and meant to be shared.

Also serious: a pay floor judged on the top of a range, so "$50k-$100k" cleared an $80k
floor; two ways past the HTTP protections (an unreadable robots.txt read as permission, and
a 429 retry that skipped the redirect and 403 checks); and Workday requisition ids that are
unique only within a tenant being used for cross-board clustering — rebuilding the seed with
namespaced ids found 11 boards the old key had been collapsing.

## Independent review

One independent review pass was run and collected. It found 4 blocker and 7 major issues;
all were fixed, each with a regression test. The three most serious were a pay floor that
ignored its own period (an $11/hour role cleared a $30/hour floor while the explanation
asserted the rule was satisfied), a salary parser that read "401k" as $401,000 and let an
invented number cause a real rejection, and adapters reporting HTTP 404/500/401 as a
healthy empty board, which made a mistyped API key look like an empty job market forever.

Two findings were already fixed independently before the review landed: non-transitive
deduplication and dead caching scaffolding.

## Not verified — carried into the handoff

1. **No adapter has ever run against a live endpoint.** Every job-source host is refused at
   this environment's egress proxy. Fixtures are built from vendor-documented schemas, so
   field mapping is proven but live behaviour is not.
2. **No robots.txt was ever fetched.** The policy code is tested; the actual postures of
   the hosts involved are unverified. Two are known conflicts: `api.smartrecruiters.com`
   is reported to disallow all crawlers while the vendor documents the endpoint beneath it
   as a public read-only API, and `api.ashbyhq.com` reportedly 401s on `/robots.txt`.
   Both platforms are held at P2 and unshipped pending that check.
3. **The three live searches have not run.** `scripts/live_validate.sh` is the handoff.
4. ~~No adapter has ever run against a live endpoint.~~ **Resolved 2026-09-14**: see M8.
   Items 1 and 2 above are superseded for the four P1 adapters; SmartRecruiters and
   Workable remain unverified and unshipped.
5. ~~No Common Crawl sweep has run against the live index.~~ **Ran 2026-09-14 and was
   refused by robots.txt** — see M8 and decision 26. The paragraph below describes what
   was verified from documentation before that. Four contract details were
   since checked against Common Crawl's published documentation and confirmed correct:
   the `showNumPages` response shape (`{pageSize, blocks, pages}`), NDJSON one record per
   line under `output=json`, the record's field being `url`, and pages being numbered `0`
   to `pages-1`. The same check found two things worth fixing, both now done: `fl` can
   restrict the returned fields, and a subdomain wildcard makes the index ignore the path,
   so a Workday sweep is high-volume and low-yield by nature. Still unproven end to end: `index.commoncrawl.org` is
   refused at this environment's egress proxy, same as every other host. The CDX response
   shape is taken from Common Crawl's documented CDXJ format, and the parser, pagination,
   cursor and registration path are tested against it end to end — but no real index page
   has ever been read. The first live run is the proof.

## Next actions

1. **User:** send `.agile/commoncrawl-question.md` to the Common Crawl group. Their answer
   decides whether `company discover` ships. No bypass in the meantime.
2. **User, deferred by choice on 2026-09-15:** USAJOBS. Before getting a key, note that
   `pytest -m live -k robots` found `data.usajobs.gov/robots.txt` says `Disallow: /`
   (read 2026-09-15). The fetcher will refuse the API even with a key. Same question as
   Common Crawl (decision 26): ask USAJOBS whether the file covers clients of the
   documented, keyed API. No code was changed; the README's federal-coverage claim is
   annotated as unverified.
3. Registry growth is now the only lever left for reach. With Common Crawl held, the
   routes are `company add` / `company import` from the user's own lists; nothing in the
   backlog changes that without a cross-company index.
4. Observation, not an action: SmartRecruiters' `Allow` for LinkedInBot suggests they
   would consider named exceptions. Asking them is an external communication and the
   user's call.

## Retrospective — M9

What worked: reading the robots files before writing a line of adapter code. The whole
question that had held two platforms for a week was settled by two HTTP requests, and
the answer was different for each — which the rumour-based notes had not predicted.

What was found only by running live, again: the multi-city repetition in Workable's
widget (no documentation mentions it), and the budget off-by-one, which had been in every
coverage table since M8 and read as normal. **Improvement carried forward:** when a
coverage table says the same odd thing for every source, that is a defect in the
accounting, not a fact about the sources. Read the numbers, not just the verdict.

Process note: a grep that dropped table continuation lines briefly made a still-present
defect look fixed. Verifying against the raw report caught it; the lesson is to read the
unfiltered evidence before claiming a fix is confirmed.

## Retrospective — M1–M8

What worked: writing the requirement classifier test table before the classifier. It
caught the "Preferred Qualifications" heading bug immediately, which would have been
invisible in manual testing and would have wrongly excluded a large share of postings.

What to change: two real product bugs — seniority misreading "Project Manager", and
locations being ranking-only — were found only when the genericity proof forced a
non-tech, non-remote search through the full pipeline. Those searches should have been
written at M2, when the gates were, rather than at M6. **Improvement for next iteration:
write the hardest acceptance case first, not last.**

The review reinforced it from a different angle: every blocker it found was in a path no
test exercised — an hourly pay floor, a non-2xx response, a plural credential. The gaps
were in the inputs never tried, not in the logic reasoned about.
