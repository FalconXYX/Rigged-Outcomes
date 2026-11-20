# Rigged Outcomes API (`worker/`)

A Cloudflare Worker (`rigged-outcomes-api`) with one D1 database (`rigged-outcomes`), both on the free plan. It is the backend's API, job queue, schedule and store; the compute runs on Vercel in steps this Worker hands out. How it all fits together: [`docs/BACKEND.md`](../docs/BACKEND.md).

| Folder | What |
|---|---|
| `src/` | The Worker: routes (`index.ts`), the queue, the cron, wake-ups, the budget ledger, published data, and the visitor, owner and job endpoints |
| `src/contract.ts` | The shared constants, the same as `backend/contract.py` |
| `migrations/` | The D1 tables |
| `test/` | Unit tests, a smoke test of every endpoint, and the Python client's test |

## Run it locally

Local only: `wrangler dev` runs the Worker on a local copy of D1 and never touches the Cloudflare account.

```bash
cd worker
npm install
cp .dev.vars.example .dev.vars
npm run migrate:local      # create the tables in the local database
npm run dev                # http://127.0.0.1:8788
```

- **Step function:** run `python3 -m backend.dev_server` and set `STEP_URL` in `.dev.vars` to its `/api/step`. With `DEV=on` only a local address is ever woken.
- **Site:** `cd web && VITE_API_URL=http://localhost:8788 VITE_TURNSTILE_SITEKEY=1x00000000000000000000AA npx vite --port 5190 --strictPort`
- **Markets:** `WATCHLIST_API_URL=http://localhost:8788 WATCHLIST_JOB_TOKEN=dev-job-token python3 -m backend.site originals` puts the 7 original markets in the local database.
- **Catalog:** fill it first with `WATCHLIST_API_URL=http://localhost:8788 WATCHLIST_JOB_TOKEN=dev-job-token python3 -m watchlist.sync catalog`. The review page's token is `dev-owner-token`.

## Test it

```bash
npm run typecheck
npm run unit        # the pure pieces, about a second
npm test            # a fresh local database, every endpoint, the queue and the cron: about 15 seconds
```

`npm test` uses its own ports; `WRANGLER_TEST_PORT=9611 npm test` runs it beside `npm run dev`.

## Deploy it

Setup on the real account is in [`docs/DEPLOY.md`](../docs/DEPLOY.md). Nothing in this folder deploys by itself.
