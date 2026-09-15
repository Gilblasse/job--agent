# Draft: question for the Common Crawl public group

Written 2026-09-15 for next action 4 in `state.md`. Not sent — that is the user's call.
Post to the Common Crawl Google Group (groups.google.com/g/common-crawl) or open a
discussion on github.com/commoncrawl/cc-index-server; both are public and both are read by
the maintainers. Decision 26 stays in force until there is an answer in writing.

---

**Subject:** Does `Disallow: /` on index.commoncrawl.org apply to CDX API clients?

Hello,

I maintain a small open-source job-search tool that wants to use the CDX index server to
enumerate employer career-site hostnames by URL pattern — for example
`*.myworkdayjobs.com/` and `jobs.lever.co/*` — at one request per second, a bounded number
of pages per run, and a saved cursor so a sweep resumes rather than restarts. It reads
`collinfo.json` and then `/<CC-MAIN-…>-index?url=…&output=json&fl=url&page=N`, exactly as
the cc-index-server README documents.

Before sending a single query I fetch the host's robots.txt, and as of today
`https://index.commoncrawl.org/robots.txt` reads:

```
User-agent: *
Disallow: /

Allow: /$
Allow: /index.html$
Allow: /web-graphs-index.html$
Allow: /collinfo.json$
Allow: /graphinfo.json$
Allow: /ccbot.json$
Allow: /.well-known/*.txt$
```

`collinfo.json` is explicitly allowed; the per-crawl `-index` query paths are not. The
tool honours robots.txt for every host it touches and has no override, so at the moment it
refuses to query the index at all.

My reading is that the file is aimed at web spiders wandering into the query endpoints,
not at clients of the documented API — but I would rather ask than guess. Two questions:

1. Is a client that follows the documented CDX API, at ~1 request/second with a
   descriptive User-Agent and contact URL, within the intended use of the index server
   despite the `Disallow: /`?
2. If so, would you consider adding an `Allow:` for the `-index` paths (or a note in the
   cc-index-server README) so robots-respecting clients can tell?

If the answer is that programmatic clients should not be querying the index server this
way, I will leave the feature disabled — I would just like to know which it is.

Thank you for running the index; it is the only free, keyless way to enumerate those
hostnames that I have found.

User-Agent the tool sends:
`jobagent/0.1 (+https://github.com/Gilblasse/job--agent; personal job search tool; contact via repository issues)`
