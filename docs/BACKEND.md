# Backend

A Cloudflare Worker queues and schedules the work; a Vercel function does it in short steps. Both are on free plans with no card. Setup is in [DEPLOY.md](DEPLOY.md); why it is built this way is in [DECISIONS.md](DECISIONS.md). Every number the two sides share (lanes, caps, timeouts, retry limits) lives in one `CONTRACT` object, kept the same in [`backend/contract.py`](../backend/contract.py) and [`worker/src/contract.ts`](../worker/src/contract.ts).

## What runs where

| Piece | Runs on | Does |
|---|---|---|
| Site | GitHub Pages | The app; rebuilt only on a push to `main`. Every market and list comes from the Worker |
| Worker (`worker/`) | Cloudflare Workers Free | The API, the job queue, the every-minute schedule, and serving what the backend publishes |
| D1 | Cloudflare D1 Free | All storage: the queue, results, news searches, the budget ledger, the lists, every market (the 7 original ones and the added ones) |
| Step function (`api/step.py`) | Vercel Hobby, Python | All the compute (Polymarket, Google News, Gemini, pandas), in calls under 300 s |

- The Worker writes a run and its jobs to D1, then wakes Vercel with a signed POST.
- The step function claims a job, works until its deadline, reports back, and claims again while time is left.
- Two secrets, one per direction: `WAKE_SECRET` signs Worker → Vercel wake-ups; `JOB_TOKEN` is the bearer token for Vercel → Worker calls.

## The life of an add

1. A visitor presses "+ Add to main page". The Worker checks Turnstile and the daily limits, records the request as approved and creates an `add` run, in one batch. (A watched market that closes with a result is sent the same way by the daily run.)
2. It wakes the `add` lane on Vercel.
3. `add.fetch` checks the market, sums its trades page by page, cleans them, lists the whales and spawns `add.whales` batches.
4. Each `add.whales` batch fetches those wallets' earlier trades.
5. `add.export` scores the whales, publishes the market's page and the new index to D1, and marks the request `added`.
6. The open page sees `added` and reads "On the main page →". No site rebuild.
7. `wallets.score` gives every whale its insider score (Gemini's Decisions API, [WALLET_RUBRIC.md](WALLET_RUBRIC.md)) and publishes the page again with it. It runs where Vercel's settings have `WALLET_SCORES=on`.

## The life of the daily run

1. From 04:00, the cron creates the day's `daily` run (15 jobs).
2. `daily.begin` loads the state and applies the owner's decisions; `watch.check` sends watched markets that closed to the main page.
3. **Recently closed:** scan closed markets, label them, search the news, judge, then `closed.publish` puts the day's 3 picks.
4. **Open now:** walk the open markets, name the stories, search, read each for a step and for "already out", then `open.publish` puts the list.
5. `daily.end` updates the review queue and the catalog, and reports the run.
6. When work was left, a `catchup` run from 10:00 finishes the Open now readings.

"Apply decisions now" is a one-job `apply` run: it rebuilds today's Open now from stored searches, with no model. A run past its deadline starts nothing new; its final jobs publish what is there, so a day always has lists.

## Jobs and lanes

A job goes `queued` → `leased` → `done`, `failed` or `cancelled`. Each step ends with one outcome:

| Outcome | Means | The Worker |
|---|---|---|
| `done` | Finished, maybe with results and child jobs | Marks it done, wakes lanes that are now ready |
| `partial` | Out of time; sends a cursor | Queues it again to go on from the cursor |
| `later` | Must wait (model busy, quota used, news limited, budget) | Queues it for later, maybe pausing its lane |
| `error` | Something broke | Retries with growing back-off, then `failed` |
| Lease expired | The call died | Counts it as an `error` |

| Lane | Carries | Why it is separate |
|---|---|---|
| `main` | Every job that saves the state, scans and publishing | One at a time, so saves never race |
| `news` | Google News searches | One a second; pauses on a run of 429s |
| `model` | Gemini and Gemma requests | Free-tier spacing; pauses while the model is busy |
| `add` | Adding markets | An add never waits for the daily run |

- One live job per lane, and one memory-heavy job across all lanes.
- A job goes only to a call with time left for its longest operation.
- A job waits for the jobs it depends on; failed and cancelled ones count as finished, so nothing waits forever.
- Steps checkpoint each whale, search or reading as it finishes: a step cut off loses at most one item.

## Schedule (Toronto time)

One Cron Trigger runs every minute. Each tick works out Toronto time and does what is due, so a missed tick is caught by the next. Every day the backend keeps is a Toronto calendar day. A day with no run leaves a `missed` row, and a new daily run cancels older unfinished ones.

| When | What |
|---|---|
| Every minute | The watchdog: expired leases, run deadlines, stuck adds, wake-ups |
| Every 10 minutes | The budget level is recomputed |
| 03:30 | Old rows are pruned |
| 04:00 | The daily run (deadline 09:30) |
| 10:00 to 12:00 | A catch-up, only when the daily run left work |
| Any time | An add or an "Apply decisions now" press starts at once |

## Limits and budget

| Free-plan limit | How the backend stays under it |
|---|---|
| Vercel: 300 s a call, 2 GB | Steps stop 20 s early; one heavy job at a time |
| Vercel: 4 CPU-hours, 360 GB-hours, 1M calls a month, or a 30-day pause | A rolling 30-day ledger and the brakes below |
| Workers: 100,000 requests a day, 10 ms CPU each | Small D1 batches; ETags; wake-ups back off when unanswered |
| D1: 500 MB | A daily prune; at most 200 markets |

| Budget level | From (share of the month) | What changes |
|---|---|---|
| `ok` | — | Everything runs |
| `tight` | 50% | Fewer adds and Open now readings a day; no catch-up |
| `low` | 65% | Adds wait (never lost); no new step readings; current cards still checked |
| `stop` | 80%, or a heavy day | Nothing is claimed or woken; due runs are `skipped` |

## Where data lives (D1)

| Table | Holds |
|---|---|
| `runs`, `jobs` | The queue: each run's kind, state and deadline; each job's lane, lease and cursor |
| `job_results`, `news_cache` | What a step hands to later steps of its run; the latest Google News search per query |
| `usage`, `lanes` | The ledger (CPU, GB-ms and calls per Toronto day and job kind); each lane's pause and wake-up back-off |
| `job_blobs`, `job_blob_parts`, `job_blob_versions` | Files: the state, the lists, the markets and their index, each market's insider scores (`rubric:<name>`), gzipped |
| `requests` | "Add to main page" requests and their status |
| `watches`, `markers` | The site's watchlist and visitors' timeline markers |
| `open_queue`, `open_decisions` | The Open now review queue and the owner's decisions |
| `catalog`, `quota` | What visitors may write to, and the daily limits |
| `job_runs`, `settings` | Run reports for the review page; the pause switch, the budget and small flags |

## Endpoints

| Path | Called by | Purpose |
|---|---|---|
| `/api/data/...` | The site | The markets' index, the two lists and each market's page |
| `/api/markers`, `/api/watch`, `/api/request` | Visitors, with Turnstile | Timeline markers, the watchlist, Add to main page |
| `/api/admin/...` | The owner (`OWNER_TOKEN`) | The review page: overview, decisions, settings, Apply decisions now |
| `/api/job/claim`, `checkpoint`, `complete` | The step function (`JOB_TOKEN`) | Take a job, save progress, report the outcome |
| `/api/job/results`, `news/get`, `run` | The step function | A run's results, stored searches, a run's jobs |
| `/api/job/work`, `queue`, `catalog`, `requests`, `done` | The step function | The daily run's inputs and reports |
| `/api/job/blobs/...` | The step function | Read and write files |
| `/api/dev/clock`, `/api/dev/tick` | Tests (`DEV=on` only) | Move the Worker's clock; run a tick now |
| `/api/step` (on Vercel) | The Worker | A signed wake-up for one lane |

## Run and test it locally

- **Worker:** `npm --prefix worker run dev` serves the API at http://127.0.0.1:8788 on a local database (first-time setup: [`worker/README.md`](../worker/README.md)).
- **Step function:** `python3 -m backend.dev_server` serves `api/step.py` locally the way Vercel does; point `STEP_URL` in `worker/.dev.vars` at it.
- **Quick tests:** `python3 tests/test_<name>.py` for the Python side (seconds) and `npm --prefix worker test` for the Worker (about 15 s).
- **End to end:** the whole path in a browser, faults included: [`tests/end-to-end/README.md`](../tests/end-to-end/README.md).
- No test calls Gemini, deploys, reads `.env`, or uses ports 8788 or 5173.
