#!/usr/bin/env bash
# The daily Watchlist job on this Mac, for days GitHub's runner cannot do it (Google News or Polymarket
# refusing the runner, while Actions itself still works): the same steps as .github/workflows/watchlist.yml
# in "full" mode, with the same data: the job's memory, the lists, the news cache and the added markets are
# all in the site's database (watchlist/state.py, watchlist/blobs.py), never in git. Then it starts the
# site's deploy on GitHub, which publishes them; with Actions turned off the data is saved but the site
# stays as last deployed. See docs/DEPLOY.md.
#
#   scripts/watchlist-mac.sh
#
# It adds the markets sent to the main page too (built in a scratch folder, saved in the database: nothing
# in your working tree changes). It reads the Gemini key and the API settings the way the Python modules do
# (the environment, else ../.env); it never prints them. A log of the run goes to
# data/watchlist/logs/mac-<UTC stamp>.log.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")/.."

# keep the Mac awake for the whole run (a sleeping Mac stalls the lists for hours)
if [ -z "${WATCHLIST_AWAKE:-}" ] && command -v caffeinate >/dev/null 2>&1; then
  WATCHLIST_AWAKE=1 exec caffeinate -is "$SELF" "$@"
fi

mkdir -p data/watchlist/logs
LOG="data/watchlist/logs/mac-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1
export PYTHONUNBUFFERED=1

# never alongside the GitHub job, running or about to (queued, waiting): both would save the job's memory
# (the second save would be refused anyway)
if command -v gh >/dev/null 2>&1; then
  running=$(gh run list --workflow watchlist.yml --limit 20 --json status \
    --jq '[.[] | select(.status != "completed")] | length' 2>/dev/null || echo 0)
  if [ "${running:-0}" != "0" ]; then
    echo "The Watchlist job is running or waiting to run on GitHub right now. Run this again once it has finished."
    exit 1
  fi
fi

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

step python3 -m watchlist.sync add
# a market saved this run is published even when the state's save failed
ADDED=$(python3 -c 'import json, os, sys
from watchlist.common import JOB_DIR
try:
    d = json.load(open(os.path.join(JOB_DIR, "add_results.json")))
except (OSError, ValueError):
    d = {}
print("yes" if d.get("at", 0) >= int(sys.argv[1]) and any(r.get("saved") for r in d.get("results") or []) else "no")' "$STARTED")

python3 -m watchlist.sync push --mode local --status "$STATUS" --started-at "$STARTED" || true

if [ "$SAVED" = yes ] || [ "$ADDED" = yes ]; then
  if command -v gh >/dev/null 2>&1; then
    echo "── gh workflow run deploy.yml --ref main"
    gh workflow run deploy.yml --ref main || echo "  ! the deploy could not be started; start it from the Actions tab"
  else
    echo "Start the site's deploy from the Actions tab (Deploy dashboard to GitHub Pages) to publish the lists"
    echo "and the added markets."
  fi
fi

echo "Done ($STATUS). Log: $LOG"
[ "$STATUS" = success ]
