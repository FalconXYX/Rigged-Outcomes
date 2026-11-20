# End-to-end harness (`tests/end-to-end/`)

Run it before every push to main, all scenarios side by side (each on its own ports).

Tests what a person does on the site through the real backend with real timing: browser click, the Worker and its D1 queue, the cron, the Vercel step function, what the Worker publishes from D1, and what an open page shows afterwards. Everything runs on this Mac and nothing reaches production.

```bash
node tests/end-to-end/scenarios/smoke.mjs                       # "+ Add to main page" until the open page reads "On the main page →" (a few minutes)
node tests/end-to-end/scenarios/add.mjs --only main             # every way an add can go: main, botfail, faults, daily (one process per group runs them side by side)
node tests/end-to-end/scenarios/watch.mjs                       # "Watch this market", and the daily run adding a watched market
node tests/end-to-end/scenarios/markers.mjs                     # timeline markers (again with --bot-fail --port-base 9660)
node tests/end-to-end/scenarios/lists.mjs                       # 3 a day, Toronto time with no UTC
node tests/end-to-end/scenarios/schedule.mjs                    # 04:00 Toronto across daylight saving, a missed day, the catch-up, the budget ledger
node tests/end-to-end/stack.mjs --port-base 9700                # a stack to click around in by hand (Ctrl-C stops it)
```

| Scenario | Default ports | Roughly |
|---|---|---|
| smoke | 9500–9503 | 1 min |
| add (main, botfail, faults, daily) | 9520–9535 | 1 to 10 min a group |
| lists | 9560–9563 | the daily run, 20 to 40 min |
| schedule | 9580–9583 | 10 min |
| watch | 9600–9603 | the daily run plus 15 min |
| markers | 9640–9643 | 10 min |

## What a stack is (`stack.mjs`)

Four ports from `--port-base`: the API, Vercel, the site, wrangler's inspector.

- **Code:** a snapshot of the working tree (tracked and new files, uncommitted changes included, ignored ones left out), taken at start. node_modules are linked from the repo.
- **API:** `wrangler dev --test-scheduled` with its own D1 folder; every setting is passed with `--var`, so `.dev.vars` is never read. Today's and yesterday's daily runs are recorded as done before it starts, so the first tick starts no full daily run (`daily: true` lets it).
- **Cron:** `lib/cron.mjs` fires the Worker's `* * * * *` trigger at the start of every minute through wrangler's local scheduled path (`GET /cdn-cgi/handler/scheduled`), as Cloudflare does. The Worker decides what is due in Toronto time on its own clock; scenarios move that clock with `POST /api/dev/clock`.
- **Vercel:** `python3 -m backend.dev_server --control` serves `api/step.py`'s own handler (`lib/vercel.mjs`). Calls share instance processes (Fluid compute), a call still going at 300 s is killed with its instance, and the Worker wakes it with signed wake-ups.
- **Site:** `npm run build` (as `deploy.yml` builds it), served at `/Rigged-Outcomes/` with Pages-style caching. It reads every market and the lists from the Worker.
- **Originals:** `python3 -m backend.site originals` saves the 7 original markets in the stack's D1, their CSVs read from this repository's `original-project` branch.
- **Seed** (`seed.py`): real Polymarket markets closed in the last three Toronto days (at most 3 a day, at the closed list's own $10,000 floor), live open cards with Google News headlines, saved the way the backend saves them (`watchlist.state push --seed`, `watchlist.sync catalog`). `--big N` adds larger markets, `--crowd N` a day published with N picks.

## For scenarios (`startStack()`)

- **Backend:** `status()` (`GET /api/job/status`), `run(id)`, `runOf(cid)`, `waitRun(id)`, `clock(epoch | null)`, `workerNow()`, `tick()` (the scheduled path), `devTick()` (`POST /api/dev/tick`, which answers what it made), `api()` for the job endpoints.
- **Faults:** `vercel.fault({status: 503, error: "DEPLOYMENT_PAUSED"})`, `vercel.fault({drop: n})` (lost wake-ups), `vercel.kill()` (every instance, mid-step), `vercel.maxSeconds(s)`, `vercel.calls()`; `stopApi()`/`startApi()`; `sql(statements)` runs SQL on the stack's D1 with the Worker stopped for it (the budget ledger's rows).
- **Shared helpers:** `lib/scenario.mjs` (the results table, card faces, watchers, hedging words, run lines) and `lib/outcome.mjs` (stop a wait as soon as an add clearly went wrong).
- Each stack stops only the processes it started, empties its state folder at start, and removes it when it stops cleanly; a scenario keeps the folder of a stack whose checks failed, and `--keep` keeps it always.

## Left out on purpose

- **Gemini is never called.** `py/sitecustomize.py` answers the key check and answers every model request with "daily quota used up" (`E2E_GEMINI=busy`: "overloaded"). Python processes refuse production hosts.
- **Package installs:** this Mac's `python3` with the repo's requirements and the repo's node_modules are used.
- **Requirements:** `npm install` in `web/` and `worker/`, and playwright-core with Chromium (paths in `lib/browser.mjs`, or set `E2E_PLAYWRIGHT` and `E2E_CHROME`).

Logs: `<state>/logs` (stack, Worker, Vercel, build, Python). Python's bytecode goes to `<state>/pycache` and each step's `/tmp` folder to `<state>/tmp`, so nothing is written into the repository.
