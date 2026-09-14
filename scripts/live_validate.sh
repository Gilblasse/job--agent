#!/usr/bin/env bash
# Live validation, to be run on an unrestricted network.
#
# This could not be run during development: the environment the tool was built in refuses
# every job-source host at its egress proxy, so nothing below has ever executed against a
# live endpoint. Everything up to this point is verified against replayed fixtures built
# from vendor-documented schemas.
#
# Run this first on a real network. It checks the go/no-go gate, then runs the three
# shipped example searches, then re-runs one to prove the new-versus-seen tracking works
# against real data.
#
#   ./scripts/live_validate.sh [output-file]
#
# USAJOBS needs a free, instantly-issued key (developer.usajobs.gov/apirequest):
#   export JOBAGENT_USAJOBS_KEY=...
#   export JOBAGENT_USAJOBS_EMAIL=...   # must be the address the key is registered to

set -uo pipefail

REPORT="${1:-live-validation-$(date +%Y%m%d-%H%M%S).txt}"
DB="${JOBAGENT_DB:-$(mktemp -d)/live.sqlite3}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -d .venv ]; then . .venv/bin/activate; fi

log() { echo "$@" | tee -a "$REPORT"; }
run() {
  log ""
  log "=============================================================="
  log "\$ $*"
  log "=============================================================="
  "$@" 2>&1 | tee -a "$REPORT"
  return "${PIPESTATUS[0]}"
}

log "jobagent live validation"
log "date:      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "database:  $DB"
log "version:   $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
# Only whether it is set, never the value: ${VAR:-default} expands to the VALUE when the
# variable is set, so the previous form wrote the API key straight into this report -- a
# file that is saved to disk and meant to be pasted back.
if [ -n "${JOBAGENT_USAJOBS_KEY:-}" ]; then
  log "usajobs:   configured"
else
  log "usajobs:   NOT CONFIGURED"
fi

run jobagent company seed --db "$DB"

# The gate. Everything after this is only meaningful if it passes.
run jobagent sources doctor --db "$DB"
GATE=$?
if [ $GATE -ne 0 ]; then
  log ""
  log "GATE FAILED (exit $GATE). The searches below will run anyway so their output is"
  log "on the record, but treat any empty result as a consequence of the gate, not as a"
  log "statement about the job market."
fi

for spec in examples/benchmark-accounting.yml examples/react-remote.yml \
            examples/pm-dfw-hybrid.yml; do
  name="$(python -c "import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))['name'])" "$spec")"
  run jobagent search create --from "$spec" --db "$DB"
  run jobagent run "$name" --db "$DB"
  run jobagent results "$name" --db "$DB" --limit 15
  run jobagent results "$name" --db "$DB" --rejected --limit 10 --explain
  run jobagent coverage "$name" --db "$DB"
done

# The second run should report zero new for postings that have not changed.
log ""
log "### Re-running the first search: new count should be 0 ###"
run jobagent run accounting-remote --db "$DB"

log ""
log "=============================================================="
log "Validation complete. Report written to: $REPORT"
log ""
log "Please check and report back:"
log "  1. Did the gate pass? Which sources answered, and which did not?"
log "  2. How many boards were actually read per platform?"
log "  3. Did the accounting search reject Senior / CPA-required / Staff roles,"
log "     and did it KEEP any that merely say 'CPA preferred'?"
log "  4. How thin were the results for pm-dfw-hybrid? That is the honest stress test."
log "  5. Did the re-run report 0 new?"
