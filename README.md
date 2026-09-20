# jobagent

A command-line job search engine. You describe the work you want; it searches employer
applicant-tracking boards, filters the results hard against your rules, tells you *why*
each job passed or failed, and remembers what it has already shown you.

It is deliberately not built for any one profession. The accounting search in
`examples/` is a test case, not the product — the same binary serves a medical biller, a
project manager or a frontend developer by swapping a YAML file.

```
BROAD DISCOVERY  →  STRICT FILTERING  →  AUTHORITATIVE SOURCE  →  TRACKING
```

## Read this first: what it does not cover

Free job data is narrower than people expect, and a tool that hides that is worse than
one that says so.

**This searches employer ATS boards and official employer systems. It does not search job
boards or aggregators** — not Indeed, LinkedIn, ZipRecruiter, RemoteOK or Remotive. That
is a deliberate scope choice: the payoff is that when it finds a job, the link goes to the
employer's own posting, and the drawback is that its reach is exactly the set of companies
in its registry.

| Kind of search | Realistic coverage |
|---|---|
| Remote roles at tech and scale-up companies | **Good.** These employers live on Greenhouse, Lever and Ashby. |
| Non-tech roles at those same employers (finance, HR, support, ops) | **Decent.** Their ATS boards carry every department, not just engineering. |
| US federal roles, any occupation, any metro | **Good** via USAJOBS — but federal only. No state, city or private employers. |
| Onsite or hybrid roles in a specific metro | **Uneven.** Thousands of Workday and iCIMS employers are now registered — the platforms where large non-tech US employers post onsite roles — plus the Greenhouse/Lever/Ashby/Workable boards and whatever USAJOBS covers federally. What a metro search returns still depends on who among them is hiring there this week, and a run reads a share of the registry, not all of it. |
| Small local employers, agencies, hourly and shift work | **Poor.** These employers mostly do not run a public ATS board. |

`examples/pm-dfw-hybrid.yml` is shipped precisely because it is the hardest case. Expect
few results. That is a fact about free job data, not a defect in the filter.

**The registry is the reach.** No ATS offers cross-company search — Lever says so in its
own documentation — so discovery is fan-out over known employer boards. If a company is
not in the registry, its jobs are not found.

It ships seeded with 28,562 boards, **all of them searchable**: every seeded board sits on
one of the six platforms with a shipped adapter (Workable 8,522, Greenhouse 7,310, Ashby
3,856, Workday 3,572, Lever 2,764, iCIMS 2,538). 693 carry a US flag from the source lists
— only one of the three lists records a country — and the rest learn their US share as
they are read: each board's share of US-located postings is recorded on the first read and
orders the fan-out from then on, so measured-US boards come first, untried boards next and
measured non-US boards last. SmartRecruiters is no longer seeded. Its API host answers
`robots.txt` with `Disallow: /` for every agent — and an explicit `Allow` for one named
bot, which makes the refusal deliberate — so its boards were seeded but never read, and
its identifiers are case-sensitive where every other platform's fold to one spelling. It
stays listed under `jobagent sources list` as excluded, with that reason.
`jobagent sources doctor` counts only what a working adapter can actually read.

### Growing the registry

Because reach *is* the registry, growing it is the highest-leverage thing you can do:

```bash
jobagent company add https://boards.greenhouse.io/acme     # one board, from its URL
jobagent company add https://acme.com/careers              # or from a careers page
jobagent company import companies.csv                      # many at once
```

The bundled seed is a merge of three open lists, keyed by platform and board token:
[outscal/OpenJobs](https://github.com/outscal/OpenJobs) (MIT; the only one with a
website and a country per company, hence the only source of the US flag),
[kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT; name, slug and
board URL per platform) and [elliottdehn/open-jobs](https://github.com/elliottdehn/open-jobs)
(CC0; bare slugs). It ships gzip-compressed (`companies.seed.json.gz`, about 530 KB,
written with a fixed timestamp so a rebuild that changes nothing is not a diff), because
28,000 rows of indented JSON would add roughly 10 MB of repository history per refresh.
`python scripts/build_seed.py` rebuilds it from the lists; `--offline DIR` reads the same
files from a directory when the network is not available. A Workday row is accepted only
from a URL that carries its site, because a (tenant, instance, site) triple with a
defaulted site is a plausible board that fetches nothing; Workday tokens are stored
lowercased, which Workday accepts, and `jobagent company seed` on a registry from an
older version treats an existing mixed-case token as the same board rather than
registering it twice.

**`company discover` is built but held.** On its first live run, both Common Crawl hosts
answered `robots.txt` with `Disallow: /`. That is almost certainly aimed at web spiders
rather than at the query API Common Crawl documents for programmatic use — but this tool
does not guess at intent, and it has no flag to override a robots file. It refuses, says
why, and carries on. The feature stays in the codebase, tested, until Common Crawl says
in so many words that API clients are not what the file means. That is the same posture
that holds SmartRecruiters unshipped. (Workable was held on the same question until its
hosts were read live on 2026-09-15: nothing disallowed, so it now ships.) What it would
do, when allowed:

```bash
jobagent company discover                  # sweep the Common Crawl index for boards
jobagent company discover --platform workday --pages 20
jobagent company discovery-status          # how far each sweep has got
```

[Common Crawl](https://commoncrawl.org) publishes a free, keyless index of the URLs it has
crawled, queryable by domain pattern. Asking it for everything under `jobs.lever.co` gives
back the boards — the cross-company index no ATS will provide. It is the only route that
can enumerate **Workday** employers at all, because each tenant is its own hostname
(`acme.wd5.myworkdayjobs.com`) and the tenancy triple is not guessable from a company
name. That matters: Workday is where the large non-tech employers post the onsite US roles
this tool covers worst.

Two honest caveats. The index is a snapshot, refreshed every month or two, so a board
opened last week is missing — which costs little, because what is harvested is *which
employers have boards*, a durable fact, while live postings still come from the ATS APIs.
And sweeps are deliberately slow and bounded: the index is run by a non-profit, so requests
are paced a second apart and each run reads a few pages and saves its cursor. Run it
repeatedly rather than in one long pass; `--restart` re-reads a pattern from the beginning.

Expect a **low yield per page**, especially on Workday. The index is keyed by URL rather
than by host, and Common Crawl ignores the path component once a subdomain wildcard is
used, so a sweep reads many job URLs to harvest comparatively few hostnames. That is
inherent to the index — there is no "list the distinct hosts" query — which is why the
sweep is cursored and run repeatedly rather than expected to pay off in one pass.

A discovered board is registered with no US signal attached. Finding a board in an index
says the employer exists, not where it hires, and claiming otherwise would push it ahead of
boards there is real evidence for.

## Install

```bash
git clone https://github.com/Gilblasse/job--agent
cd job--agent
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

Optional, and free — it adds every US federal job to your searches. Get a key instantly at
[developer.usajobs.gov/apirequest](https://developer.usajobs.gov/apirequest):

```bash
export JOBAGENT_USAJOBS_KEY=...
export JOBAGENT_USAJOBS_EMAIL=...   # must be the address the key is registered to
```

## Start here

```bash
jobagent company seed        # load the bundled registry of employer boards
jobagent sources doctor      # check this actually works from your network
```

`sources doctor` is a gate, not a diagnostic. It requires at least three ATS adapters
returning real postings and at least 500 registered boards routed to a working adapter.
If it fails, fix that before trusting any search results.

USAJOBS credentials are optional, so leaving them unset is reported as a coverage note
rather than a failure — you simply get no federal roles. Credentials that are set but not
working *do* fail the gate, because that is a broken source rather than an absent one.

```bash
jobagent search create       # interactive
jobagent run my-search       # a progress bar: boards read, postings found, then matches
jobagent results my-search --new
```

Every results table starts with the job's **ID** and ends with its **link** (clickable in
terminals that support hyperlinks, the plain URL everywhere; `--no-links` hides the
column). The ID is what the other commands take:

```bash
jobagent show 1616                 # the whole job, every URL it was seen at, why it matched
jobagent mark 1616 saved
jobagent dismiss 1616              # "not jobs like this" -- asks why, then what to do about it
```

`dismiss` asks for your reason in your own words, keeps it with the job, and then asks
what the reason means for the next run: never show this employer, exclude titles worded
like this, exclude a duty, a skill, a phrase anywhere, or a seniority level -- or just
hide the job. The rules you pick are added to the saved search and apply on the next
`run`; the job itself stays hidden from `results` (use `--all` to see it), and
`jobagent dismissed my-search` lists every reason you have given. The same thing without
the questions, for scripts:

```bash
jobagent dismiss 1616 --reason "agency work" --rule employer --rule "duty=cold calling"
```

No network, no keys, just to see what it does:

```bash
python scripts/demo.py --db /tmp/demo.sqlite3
jobagent results accounting-remote --db /tmp/demo.sqlite3 --rejected --explain
```

## Why a job matched, or didn't

Filtering happens in two stages that never blend.

**Hard gates** decide admission. A job that fails one is out, whatever else it has going
for it — that is what "exclude Senior roles" has to mean to be worth setting.

**Ranking** orders what survived, as a ledger of named contributions that sum to the
score. There is no mystery number.

```
$ jobagent results accounting-remote --rejected --explain

╭───────────────── Senior Accountant — Acme Corp ─────────────────╮
│ Rejected because                                                │
│   x seniority_excludes: seniority must not be ['senior', ...]   │
│       matched 'Senior'                                          │
│   x requirement:CPA: reject when 'CPA' is required              │
│       matched 'CPA'                                             │
│       stated as required via 'active': Lead the close.          │
│       Active CPA license required.                              │
│                                                                 │
│ Rules satisfied                                                 │
│   ok workplace: remote                                          │
│   ok country: US                                                │
╰─────────────────────────────────────────────────────────────────╯
```

### "CPA preferred" is not "CPA required"

Excluding a credential cannot be a keyword match. Most postings that mention a CPA say
"CPA preferred" or "CPA a plus", and rejecting those throws away the jobs you wanted.

So each mention is read in context — the clause around it is weighed for requirement
versus preference wording, negations are resolved first, and a genuine tie is reported as
unconfirmed rather than guessed:

| Posting says | Result |
|---|---|
| "CPA required", "Active CPA license required", "Must have a CPA" | **excluded** |
| "CPA preferred", "CPA a plus", "Working toward CPA", "No CPA required" | **kept** |
| "Our team includes a CPA" | **kept, flagged** as unconfirmed |

Nothing in the code knows what a CPA is. `PMP`, `RN license`, `Series 7` and
`security clearance` all work the same way, because the vocabulary being matched is
ordinary English.

### When a posting doesn't say

Most postings omit pay, and many omit location or working arrangement. "Does not say" is
tracked separately from "says something disqualifying" — those are different facts, and
merging them either hides jobs or misrepresents them.

By default such jobs are **kept, marked `?`, and ranked lower**. Set
`unverifiable_policy: strict` to exclude them instead.

## Commands

| Command | |
|---|---|
| `search create [--from spec.yml]` | build a search, interactively or from YAML |
| `search list / show / edit / export / delete` | manage saved searches |
| `run <name> [--sources] [--budget]` | execute a search |
| `results <name> [--new\|--rejected\|--saved\|--applied] [--explain] [--all] [--no-links]` | review |
| `show <job-id>` | one job in full, with every URL it was seen at |
| `mark <job-id> saved\|applied\|rejected\|dismissed` | track where you stand |
| `dismiss <job-id> [--reason] [--rule ...]` | say why not jobs like this; the search learns |
| `dismissed <name>` | every reason you have given, and the rules it produced |
| `coverage <name>` | which sources answered, and which did not |
| `export <name> --format csv\|json\|md` | get the data out, reasoning included |
| `verify <name>` | re-check whether saved jobs are still open |
| `sources list / doctor` | what it reads; whether it works from here |
| `company seed / add <url> / import <csv> / list` | grow the registry |
| `company discover [--platform] [--pages] [--dry-run]` | find new boards in the Common Crawl index |
| `company discovery-status` | how far each discovery sweep has got |

## Searches are just files

Everything the wizard asks can be written as YAML and run headlessly:

```yaml
name: accounting-remote
titles: [Accounts Payable, Junior Accountant, Accountant, Cash Management]
excluded_titles: [Staff Accountant]
responsibilities_include: [accounts payable, cash management, bank reconciliation]
responsibilities_exclude: [auditing, budget creation, forecasting]
seniority_exclude: [senior, staff, management]
excluded_requirements:
  - term: CPA
    when: required          # "CPA preferred" still gets through
workplace: [remote]
countries: [US]
```

The wizard also asks about shift patterns, licensure, travel, sponsorship and pay basis —
the things that decide whether a job is viable outside tech, and that a tech-shaped
questionnaire leaves out.

## How it plays with other people's servers

All network traffic goes through one chokepoint, so these are properties of the system
rather than promises:

- **robots.txt is respected** (RFC 9309: a 4xx means no file is published and access is
  allowed; a 5xx is treated as a full disallow), **including its `Crawl-delay`**: a host
  that asks for a pause gets it, up to 30 seconds, and the pause only ever widens the
  per-host pace. `api.lever.co` asks for one second, so a Lever fan-out runs at one
  request a second.
- **Rate limiting is per host**, because several ATS platforms put thousands of employers
  behind one hostname.
- **403 stops that host for the run.** A 429 earns one wait when `Retry-After` asks for a
  short one, then stops. Both are per-run, never permanent.
- **Nothing is bypassed** — no CAPTCHA solving, no auth circumvention, no proxy rotation,
  no headless browser, and deliberately **no flag to turn robots checking off**. A source
  that cannot be read legitimately is recorded as a coverage gap.
- **Redirects are re-checked**, so a redirect cannot walk the crawler into a disallowed
  path.
- Only documented public endpoints are used. `jobagent sources list` shows what is read
  and, with reasons, what is deliberately not.
- **Board discovery reads a public dataset, not search engines.** The Common Crawl index
  is a free service whose stated purpose is programmatic querying; sweeps use a one-second
  interval, stop at a page budget, and resume rather than restart. No search engine is
  scraped, and no result page is parsed.

## Verification status

774 tests, all offline, run with `pytest`.

**Live-verified on 2026-09-14** from an ordinary home network, after being built in an
environment that refused every job-source host:

- `sources doctor` **passed**: Greenhouse, Lever, Ashby and Workday all returned real
  postings on their first live call, and none of their `robots.txt` files disallows the
  documented API paths.
- The accounting benchmark read **18,238 live postings** from ~280 boards in about three
  minutes and returned 10 matches, all US, the top one a remote Technical Accountant.

The first live run also found four real defects that no fixture had exercised, all fixed
the same day and pinned in `tests/unit/test_live_findings.py`: country detection missed
"Croatia", "Mumbai" and "FR - Paris" (the vocabulary was sixty hand-picked names; it is
now every country, plus regions and major cities); relevance was satisfied by a title
word anywhere in the body ("liaise with our accountant"); a Workday board over its
description budget flooded the results with unverifiable flags; and a barred
responsibility fired on "audit trail" in a requirements list. The retrospective lesson
stands: the inputs nobody imagined are where the bugs were.

All three example searches have now run live. The React search first matched an options
trader on "react to volatility shifts" — skills are now matched as names, with the user's
casing — and the Dallas–Fort Worth search returned four hybrid/onsite project-management
roles in the metro, all four defensible, from 1,307 boards. Two example specs were
tightened along the way: a bare "component" or "stakeholder" as a responsibility phrase
matches nearly everything.

**Second live pass, 2026-09-15**, with Workable added after its `robots.txt` was read
clean. `scripts/live_validate.sh` ran end to end: the gate passed with five adapters and
1,479 routable boards, and all three example searches ran again: accounting 16,751
postings and 9 matches; React 19,567 and 35; Dallas–Fort Worth 20,162 and the same four
hybrid/onsite roles in the metro as the day before. Workable's 727 postings from 47 boards
matched none of the three — the seeded Workable employers are mostly agencies, studios and
game companies, and the rejections say why, one rule and one quote at a time.

That pass found two more defects, both fixed and pinned. A job open in several cities
had its structured city read as "Dallas, Texas, United States;" because the parser
scanned the whole list for a spelled-out state and found "New York" first — USAJOBS
multi-location postings had the same problem. And the `robots.txt` read was being
charged against each source's board allowance, so the last planned board on every
platform was never reached, and every coverage table said so.

One thing to know about budgets: `source_budget` in a spec is a request allowance, and
platforms differ by an order of magnitude in what one board costs. A Greenhouse, Lever,
Ashby or Workable board is one request; a Workday or iCIMS board is several list pages
plus one request per description. Under the original split — by board count alone —
adding Workable's 172 boards to the registry did not add reach to a 400-request run, it
re-sliced it (the same script at the previous commit read 17,715 / 20,119 / 20,407
postings for the three searches; with Workable in the split it read 16,751 / 19,567 /
20,162, for the same 9 / 35 / 4 matches), and Workday drew 47 requests, about one board
per run. So 40% of the budget is now reserved for the costly platforms, divided between
them by board count, each taking the larger of its reserved and its proportional share;
the cheap platforms split the rest. The default is now 1,500 requests. Platforms are read
in parallel at the per-host pace — a quarter of a second, or one second on Lever by its
robots `Crawl-delay` — so the slowest platform's share sets the time, not the sum, and a
run takes several minutes rather than the roughly three of the 400-request runs above.
Growing the registry no longer only re-slices a fixed pie between platforms, but within a
platform it still does: more boards than a run can reach means each run reads a slice,
ordered by measured US share and then least-recently-tried first, so successive runs walk
the registry rather than re-reading the same few hundred boards.

Not yet run live: USAJOBS (needs a free key) — and `data.usajobs.gov` publishes
`Disallow: /`, read on 2026-09-15 and again on 2026-09-20, so even with a key the tool
will currently refuse it, exactly as it refuses SmartRecruiters and Common Crawl. The live
robots test now records that hold rather than failing on it: every shipped host must
still permit this crawler, and the held host must still deny it, so a change in either
direction is noticed and acted on deliberately. Whether a keyed, documented API is what
that file means is a question for USAJOBS; until then the "Good" federal coverage in the
table above is unverified. `company discover` ran and was refused by Common Crawl's
`robots.txt`; see above.

**Third live pass, 2026-09-20**, the data-sourcing changes: iCIMS added as a sixth
adapter after its tenant `robots.txt` files were read (`careers-48forty`, `external-92y`,
`resume-chesterton`: only the referral, login, candidate and connect paths are
disallowed); the live probe reached `careers-48forty` and parsed 150 postings from three
list requests. The robots sweep from this network answered ALLOW for the Greenhouse API
(`Disallow: /embed/` only), the Lever API, the Ashby API (a 401 on `robots.txt`, which
under RFC 9309 means no file is published), Workable and iCIMS, and DENY for
`data.usajobs.gov`. The same pass found that Ashby pay had been read from a key the live
API never sends, so it was empty on every Ashby board; that Lever's headed `lists`
sections were not part of the posting text, so a credential demanded in a Requirements
bullet never reached the requirement gate; that a heading written with a curly apostrophe
("What You’ll Do") never matched the section taxonomy; and that a `<li><p>` bullet lost
its marker to a line break, so a short capitalised bullet read as a heading. All four are
fixed and pinned in the fixtures. The first run of `pm-dfw-hybrid` at the 1,500 budget
over the grown registry then read 0/249 iCIMS boards: the alphabetically first tenant's
`robots.txt` disallows us, and the fan-out loop took that one refusal for the platform's
host stopping. Tenants are their own hosts on Workday and iCIMS, so a refusal now ends
only that tenant. The rerun read greenhouse 235/293, workable 341/342, ashby 145/154,
lever 69/111, workday 16/351 and icims 26/249 boards — 27,767 postings, 7 matches for
the hardest search — with Lever paced at 1 s by its `Crawl-delay` and nothing throttled.

`pytest -m live` holds tests that hit real endpoints. They are excluded by default so CI
never depends on third-party uptime.

## Web version

The same engine behind a search screen: filters on top, matching jobs on the left, the
selected posting on the right, with "Why this matches your preferences" under it. It
runs on free tiers only.

```
 browser ──► Vercel Hobby (React SPA + FastAPI, app.py) ──► Turso (libSQL, free)
                 "Search jobs" queues a request and wakes the runner    ▲
                                                                        │ publish
             GitHub Actions (.github/workflows/run.yml) ────────────────┘
             daily, plus wake-ups · `jobagent cloud run`
```

What to know before trusting it:

- **"Search jobs" saves your rules and queues a run.** A search here is not a query
  against an index; it is a rule set that a run evaluates over minutes of board fan-out.
  The screen keeps showing one completed run, with its timestamp, until you accept
  "Newer results are ready". "Waiting for an available runner" means exactly that.
- **Who runs it depends on where the app is.** On a local SQLite database the web
  process runs the search itself, in a background thread, with the same fetcher and the
  same robots and rate-limit policy as the command line — the website and the CLI are
  one system. Deployed, the runner is the GitHub Actions workflow, woken by the site.
  `JOBAGENT_DISPATCHER=thread|github|none` overrides the choice.
- **One runner at a time, proven per write.** The runner holds a lease in the database
  and every batch it publishes checks it; a runner that lost its lease cannot write or
  report success. Requests, runs and verdicts are only ever visible once complete.
- **A screen is pinned to one run.** The fields it lists and sorts by are frozen on the
  verdict, so a later run cannot move the page under you. A search's latest three runs
  stay readable; older ones answer "Results expired — show latest". The posting body is
  the current text, and the detail says so when the evaluated version differed.
- **Rejected verdicts are kept for the latest three runs;** match verdicts are kept for
  good (that is what "new" is measured against). Jobs unseen for 60 days that you never
  saved, applied to or dismissed are removed.
- **Editing a search bumps its revision.** Two tabs cannot silently overwrite each
  other, and results show "Rules changed since these results" when they were.

Setup, once:

1. Turso: `turso db create jobagent`, then `turso db show jobagent --url` and
   `turso db tokens create jobagent`.
2. `TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... jobagent cloud init` — schema plus the
   bundled registry.
3. GitHub: repository secrets `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` (and the
   optional USAJOBS pair). A fine-grained token with *Actions: read and write* on this
   repository lets the site wake the runner; without it, requests wait for the daily run.
4. Vercel: import the repository (the FastAPI preset finds `app.py`; the build runs
   `npm run build` in `web/`). Environment: `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`,
   `JOBAGENT_WEB_PASSWORD`, `JOBAGENT_GITHUB_TOKEN`, `JOBAGENT_GITHUB_REPO`.

Two facts about GitHub: it disables a schedule after 60 days without repository
activity, and Actions runners use shared IP ranges — the doctor gate inside
`cloud run` says whether the boards can be read from there, every run.

Upgrading an existing deployment: run `jobagent cloud init` again before deploying a
runner built from this version. It applies the schema migrations (this version adds the
registry's `us_share` column) and tops the cloud registry up from the bundled seed, and
it is safe to repeat. A runner against an un-migrated cloud database fails at `pull`,
where it copies the registry and finds the column missing, rather than running degraded.

Locally, with no cloud at all: `cd web && npm ci && npm run build && cd ..`, then

```bash
JOBAGENT_DB=~/.jobagent/jobagent.sqlite3 JOBAGENT_WEB_PASSWORD=choose-one \
  uvicorn app:app --port 8010
```

and open http://127.0.0.1:8010. That is your command-line database in the browser, and
"Search jobs" there reads the real boards. `python scripts/e2e_server.py` is the same
app over a demo database seeded from the test fixtures (password `e2e`), with a runner
that reads fixtures instead of the network; that is what the acceptance test
(`pytest -m e2e`) drives.

## Data and privacy

The command line keeps everything local: one SQLite file at
`~/.jobagent/jobagent.sqlite3`. Nothing is uploaded, no account is needed, and no
telemetry is sent. The web version keeps the same tables in your own Turso database,
behind a password you set; nothing else sees them.

## Attribution

The bundled registry is merged from three open lists, filtered to platforms this tool can
read: [outscal/OpenJobs](https://github.com/outscal/OpenJobs) (MIT, itself a fork of
santifer/career-ops), [kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers)
(MIT) and [elliottdehn/open-jobs](https://github.com/elliottdehn/open-jobs) (CC0).
`scripts/build_seed.py` records which file came from which list, under which licence.

## License

MIT.
