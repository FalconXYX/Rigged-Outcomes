#!/usr/bin/env bash
# The daily Watchlist job on this Mac, for days the backend's Vercel steps cannot do it (Google News or
# Polymarket refusing Vercel's addresses): the same work as the daily run (docs/BACKEND.md) in one go, with the
# same data: the job's memory, the lists and the added markets are all in the site's database
# (watchlist/state.py, watchlist/blobs.py, backend/site.py), never in git. The Worker serves what it saves at
# once, so no site build is needed. See docs/DEPLOY.md.
#
#   scripts/watchlist-mac.sh
#
# It adds the markets sent to the main page too, and each watched market that has closed with a result (built
# in a scratch folder, saved in the database: nothing in your working tree changes). It reads the Gemini key
# and the API settings the way the Python modules do (the environment, else ../.env); it never prints them. A
# log of the run goes to data/watchlist/logs/mac-<Toronto stamp>.log.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")/.."

# keep the Mac awake for the whole run (a sleeping Mac stalls the lists for hours)
if [ -z "${WATCHLIST_AWAKE:-}" ] && command -v caffeinate >/dev/null 2>&1; then
  WATCHLIST_AWAKE=1 exec caffeinate -is "$SELF" "$@"
fi

mkdir -p data/watchlist/logs
LOG="data/watchlist/logs/mac-$(TZ=America/Toronto date +%Y%m%dT%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
export PYTHONUNBUFFERED=1

# alongside the backend's own daily run, whichever saves the job's memory second is refused (the save checks the
# version it read), so nothing is overwritten
STARTED=$(date +%s)
STATUS=success
step() {   # run one step; a failure makes the run "partial" and the next step still runs
  echo "── $*"
  if ! "$@"; then
    echo "  ! failed: $*"
    STATUS=partial
  fi
}

echo "── python3 -m watchlist.state pull"
if ! python3 -m watchlist.state pull; then
  echo "The saved state could not be read from the database (see above); nothing was changed."
  python3 -m watchlist.sync push --mode local --status failure --started-at "$STARTED" || true
  exit 1
fi

step python3 -m watchlist.sync pull
step python3 -m watchlist.sync decide --mode local
step python3 -m watchlist.upsets --days 2 --watched data/watchlist/work.json
step python3 -m watchlist.opennow --report data/watchlist/open_report.json

SAVED=yes
echo "── python3 -m watchlist.state push --by mac"
if ! python3 -m watchlist.state push --by mac; then
  STATUS=failure
  SAVED=no
fi

step python3 -m watchlist.sync add --watched
# a market saved this run is published even when the state's save failed
ADDED=$(python3 -c 'import json, os, sys
from watchlist.common import JOB_DIR
try:
    d = json.load(open(os.path.join(JOB_DIR, "add_results.json")))
except (OSError, ValueError):
    d = {}
print("yes" if d.get("at", 0) >= int(sys.argv[1]) and any(r.get("saved") for r in d.get("results") or []) else "no")' "$STARTED")

python3 -m watchlist.sync push --mode local --status "$STATUS" --started-at "$STARTED" || true

# the markets added here get their pages and their place in the added markets' index, which the Worker serves
if [ "$ADDED" = yes ]; then
  step python3 -m backend.site migrate
fi

echo "Done ($STATUS). Log: $LOG"
[ "$STATUS" = success ]
