# Rigged Outcomes API (`worker/`)

A small Cloudflare Worker (`rigged-outcomes-api`) with one D1 database (`rigged-outcomes`), both on Cloudflare's free plan with no card. It keeps what the static site can't: timeline markers that visitors add, the site's one watchlist ("Watch" puts an open market on it and "Stop watching" takes it off, for everyone), the markets sent with "Add to main page" (recorded as approved at once; the job's next run, daily, adds each under a page name made from its slug), the owner's decisions on the open list's review queue, the job's reports, and everything the daily job keeps (its memory between runs, the two lists, the news cache and the added markets' pages; nothing of it is in git). When the API is down the site still works: the lists and added markets are built into it at deploy time, and only the visitor controls wait.

Setting it up on the real account (creating the database, secrets, Turnstile, deploying) is in [`docs/DEPLOY.md`](../docs/DEPLOY.md). Nothing in this folder deploys anything by itself.

## Files

| File | What it is |
|---|---|
| `wrangler.toml` | The Worker, its D1 binding (the live database's `database_id`) and the production vars |
| `migrations/` | The D1 schema: `0001_init.sql`, then `0002_site_watchlist.sql` (one watchlist for the site; adds need no review), then `0003_job_blobs.sql` (the daily job's data) |
| `src/index.ts` | Routes, CORS (only the origins in `ALLOWED_ORIGINS`) and the error wrapper |
| `src/lib.ts` | Shared pieces: bodies, the visitor hash, token checks, daily limits, Turnstile, the label rules |
| `src/public.ts` | Visitor endpoints: markers, the watchlist, Add to main page |
| `src/owner.ts` | Owner endpoints for the review page (`#/review`), behind `OWNER_TOKEN` |
| `src/job.ts` | Daily job endpoints (`watchlist/sync.py`), behind `JOB_TOKEN` |
| `src/blobs.ts` | The job's data endpoints (`watchlist/blobs.py`), behind `JOB_TOKEN`: save, read, list, restore and delete blobs |
| `test/smoke.mjs` | Checks every endpoint against a running local Worker |
| `test/client.py` | The job's own Python client (`watchlist/blobs.py`, `watchlist/state.py`) against the Worker `npm test` starts |
| `test/run.mjs` | `npm test`: the smoke test, the client check and the prune check on a throwaway local database, start to finish |
| `.dev.vars.example` | Local values (test Turnstile secret, dev tokens); copy it to `.dev.vars` |

## Running it locally

Local only: `wrangler dev` runs the Worker and a local SQLite copy of D1 on this Mac and never talks to the Cloudflare account.

```bash
cd worker
npm install
cp .dev.vars.example .dev.vars
npm run migrate:local      # create the tables in the local database
npm run dev                # http://127.0.0.1:8788
```

The local database lives in `.wrangler/state` (git ignores it); set `WRANGLER_STATE` to another folder to keep it elsewhere, for both commands.

Then point a separate Vite server at it (never put a `.env` file in `web/`):

```bash
cd web && VITE_API_URL=http://localhost:8788 VITE_TURNSTILE_SITEKEY=1x00000000000000000000AA npx vite --port 5190 --strictPort
```

Writes are only allowed to what the job has listed, so fill the local catalog from the repository root first: `WATCHLIST_API_URL=http://localhost:8788 WATCHLIST_JOB_TOKEN=dev-job-token python3 -m watchlist.sync catalog`. The review page is at `#/review`, with the token `dev-owner-token`.

Locally, Turnstile uses Cloudflare's test keys: the site key `1x00000000000000000000AA` always passes and gives the dummy token `XXXX.DUMMY.TOKEN.XXXX`, which the test secret in `.dev.vars` accepts. Those tokens report the hostname `example.com`, so `.dev.vars` turns `TURNSTILE_STRICT` off; production keeps it on (the token must come from `parthjain.ca` and name the endpoint's action).

## Tests

```bash
npm run typecheck
npm test            # fresh local database, wrangler dev on 8788, the smoke test, then three restarts:
                    # a failing bot check, production settings, and "Apply decisions now" against a stand-in for GitHub
npm run smoke       # only the smoke test, against a Worker you already started (it replaces that database's catalog)
```

`npm test` refuses to start when something already listens on its port, and stops every process it started; `WRANGLER_TEST_PORT=8799 npm test` runs it beside `npm run dev`. It calls Cloudflare's public siteverify endpoint with the test keys (no account); if that can't be reached it says "network" and exits 2.

## Limits and rules, in short

- **Daily limits (UTC):** markers 10 per visitor, 20 new ones per market from everyone, and 300 for the site (and at most 60 live per market); watch changes 30 and 1,000; adds to the main page 5 and 50. A visitor is the first 32 hex characters of HMAC-SHA256(`VISITOR_SALT`, the visitor's IPv4 address, or the /64 network of an IPv6 one); no address is stored, and Cloudflare's per-request invocation logs are off (`wrangler.toml`).
- **Bot check:** every visitor write carries a Turnstile token, checked before any limit is spent.
- **Pause:** the review page's switch (`settings.writes`) stops visitors' markers, watches and adds to the main page at once; undo and unwatch keep working.
- **Sign-in:** the owner and the job each have one bearer token. The right token always gets in; after 10 wrong ones from one address in a UTC day, its wrong tokens are answered `locked` until the next.
- **Bodies** must be sent as `application/json` (415 otherwise), so another site's page can't post one without the CORS preflight the Worker refuses.
- **The job's data** is kept as blobs (`/api/job/blobs`): each one a gzip-compressed JSON document as base64 text, in parts of 500,000 characters (`job_blob_parts`), with `job_blobs` naming the live version. A save writes the new parts beside the old ones and moves the pointer last, in one statement, so a reader never sees half a save; the parts of a save left unfinished go after a day. The state's and the lists' last 3 replaced versions are kept (`job_blob_versions`), so one bad save can be undone on its own (`python3 -m watchlist.blobs restore`); they can't be deleted. Added markets stop at 200, or 300 parts in all (409 `full`), so the database keeps room for the rest. Deleting a market's blob marks its request failed, "taken off the main page". The whole database's backup is D1 Time Travel (7 days on the free plan).
- **Taking back a decision** on the review page marks it `withdrawn`; the next run removes it from the job's store too (a run may already have saved it) and reports it, and then the row goes.
- **Errors** are `{"error": code, "message": text}`. The site shows its own words for each code and never the message.
- **Test-only headers:** with `DEV=on` (never in production), `X-Test-Visitor`, `X-Test-IP` and `X-Test-Day` stand in for the visitor hash, the visitor's address and the day the limits count under, and `GITHUB_API` can point the dispatch elsewhere.
