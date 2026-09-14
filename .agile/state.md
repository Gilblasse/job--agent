# State

**Product Goal:** a profession-agnostic CLI that finds jobs on employer ATS boards,
filters them hard with stated reasons, and remembers what it has shown.

**Status:** implemented and locally verified. **Not** validated against any live endpoint.

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

## Verification evidence

- 240 tests pass (`pytest -q`), `ruff check src tests scripts` clean.
- Genericity proven the hard way: one corpus, three unrelated searches, different correct
  answers, no code change. A test parses `src/` and fails on profession-specific terms in
  executable code.
- Failure paths exercised: blocked host, partial source failure, malformed payload, empty
  board, missing credentials.
- `sources doctor` was run against the real network here and failed honestly, naming the
  egress refusal per source rather than implying the sources were broken.

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

## Next actions

1. Run `./scripts/live_validate.sh` on an unrestricted network; read the gate result first.
2. Resolve the two robots conflicts; ship SmartRecruiters and Workable only if clean.
3. Record the real coverage of `pm-dfw-hybrid` in the README, whatever it turns out to be.

## Retrospective

What worked: writing the requirement classifier test table before the classifier. It
caught the "Preferred Qualifications" heading bug immediately, which would have been
invisible in manual testing and would have wrongly excluded a large share of postings.

What to change: two real product bugs — seniority misreading "Project Manager", and
locations being ranking-only — were found only when the genericity proof forced a
non-tech, non-remote search through the full pipeline. Those searches should have been
written at M2, when the gates were, rather than at M6. **Improvement for next iteration:
write the hardest acceptance case first, not last.**
