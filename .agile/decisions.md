# Decisions

Recorded so they are not re-litigated. Rationale lives here; the trade-offs a reader meets
in the code are also noted at their site.

| # | Decision | Why |
|---|---|---|
| 1 | Python 3.11 | stdlib `sqlite3` satisfies "free local persistence" with no dependency; mature prompt and table libraries; user-selected. |
| 2 | Ports and adapters, pure domain | Makes the whole pipeline testable with no network, which is the only reason this was verifiable at all in an egress-blocked environment. Also makes the serverless path a swap rather than a rewrite. |
| 3 | Three gate outcomes, not two | Most postings omit pay, many omit workplace, remote postings often name no country. Collapsing "does not say" either hides jobs or misrepresents them. |
| 4 | Requirement-context detection over keyword matching | "Exclude CPA-required" must keep "CPA preferred" — otherwise the filter discards the jobs it was written to find. |
| 5 | Registry seeded from outscal/OpenJobs, filtered | With no cross-company search anywhere, an empty registry means an empty results table. 1,766 boards, 1,307 on P1 platforms. |
| 6 | Workday as P1 despite its cost | With aggregators out of scope, it is the only source covering onsite non-tech US metro roles. Accepting 20-per-page and a request per description is the price of that coverage. |
| 7 | Per-host rate limiting, not per company | Several platforms host thousands of tenants behind one name; per-tenant limiting would 429-storm them. |
| 8 | Blocked is per-run, never permanent | A shared host that throttles today must not be struck off forever. |
| 9 | Rate-limited tracked separately from gone | The boards most worth reading are the likeliest to throttle; conflating them rots the registry. |
| 10 | Identical titles at one employer and location collapse | Splitting needs a requisition id most sources do not publish; showing the same job twice is worse. |
| 11 | "Manager" is not a seniority level | Rank in "Engineering Manager", job family in "Project Manager". Reading it as rank silently deletes a whole profession; not reading it as rank shows a few roles the user can exclude by title. The recoverable error wins. |
| 12 | Named locations are a hard gate | A search for Dallas returning San Francisco is a wrong answer, not a ranking flaw. Remote roles are exempt when remote is acceptable. |
| 13 | Inflection-tolerant phrase matching, final word only, 5+ characters | Excluding "auditing" must catch "audit"; stemming "plus" to "plu" would match "plush". |
| 14 | Fixtures from documented schemas, clearly labelled | No live capture was possible. Labelling them is the difference between a limitation and a misrepresentation. |
| 15 | SmartRecruiters and Workable held at P2 | Unresolved robots.txt conflicts. Shipping them would contradict the legitimate-access constraint on an unverified assumption. |
| 16 | Registry growth via the Common Crawl index, not a search API | Every free whole-web search API is gone: Bing Search retired, Google CSE closed to new signups and ending, the rest paid or aggregator-backed. Common Crawl is free, keyless, non-profit, and exists to be queried programmatically — so it satisfies "free forever" and "legitimate access" together. |
| 17 | Common Crawl is the only route to Workday tenants | Each tenant is its own hostname and the (tenant, instance, site) triple is not guessable from a company name, so no `site:` query on any engine can enumerate them. A subdomain wildcard on the index can. That is the exact gap in coverage: onsite, non-tech, US. |
| 18 | Sweeps are bounded, paced and resumable | The index is volunteer-funded. One request per second, a page budget per run, and a saved cursor make discovery a background chore rather than a load spike on somebody else's free service. |
| 19 | A sweep that stops early is never recorded complete | Parking the cursor as finished after an error leaves the rest of the pattern unread for the life of the crawl — a silent hole in reach, which is the one thing the registry cannot afford. |
| 20 | Discovered boards carry no US signal | An index entry proves a board exists, not where it hires. Marking it US would push it ahead of boards with real evidence in the fan-out order. |
