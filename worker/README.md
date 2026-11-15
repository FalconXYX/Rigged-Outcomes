# Rigged Outcomes API (`worker/`)

A small Cloudflare Worker (`rigged-outcomes-api`) with one D1 database (`rigged-outcomes`), both on Cloudflare's free plan with no card. It keeps what the static site can't: timeline markers that visitors add, the site's one watchlist ("Watch" puts an open market on it and "Stop watching" takes it off, for everyone), the markets sent with "Add to main page" (recorded as approved at once; the job's next run, hourly, adds each under a page name made from its slug), the owner's decisions on the open list's review queue, and the job's reports. The site works exactly as before when no API is configured or the API is down.

Setting it up on the real account (creating the database, secrets, Turnstile, deploying) is in [`docs/DEPLOY.md`](../docs/DEPLOY.md). Nothing in this folder deploys anything by itself.

## Files

| File | What it is |
|---|---|
| `wrangler.toml` | The Worker, its D1 binding (the live database's `database_id`) and the production vars |
| `migrations/` | The D1 schema: `0001_init.sql`, then `0002_site_watchlist.sql` (one watchlist for the site; adds need no review) |
| `src/index.ts` | Routes, CORS (only the origins in `ALLOWED_ORIGINS`) and the error wrapper |
| `src/lib.ts` | Shared pieces: bodies, the visitor hash, token checks, daily limits, Turnstile, the label rules |
| `src/public.ts` | Visitor endpoints: markers, the watchlist, Add to main page |
| `src/owner.ts` | Owner endpoints for the review page (`#/review`), behind `OWNER_TOKEN` |
| `src/job.ts` | Daily job endpoints (`watchlist/sync.py`), behind `JOB_TOKEN` |
| `test/smoke.mjs` | Checks every endpoint against a running local Worker |
| `test/run.mjs` | `npm test`: the smoke test on a throwaway local database, start to finish |
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
- **The public backup** (`GET /api/job/export`, committed daily to the `watchlist-data` branch) has no visitor or undo hashes, and of the markers only the live ones at least 2 days old.
- **Taking back a decision** on the review page marks it `withdrawn`; the next run removes it from the job's store too (a run may already have saved it) and reports it, and then the row goes.
- **Errors** are `{"error": code, "message": text}`. The site shows its own words for each code and never the message.
- **Test-only headers:** with `DEV=on` (never in production), `X-Test-Visitor`, `X-Test-IP` and `X-Test-Day` stand in for the visitor hash, the visitor's address and the day the limits count under, and `GITHUB_API` can point the dispatch elsewhere.
