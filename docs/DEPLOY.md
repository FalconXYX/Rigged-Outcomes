# Deploying Rigged Outcomes

Everything here runs on free plans.

| Piece | Where | What it does |
|---|---|---|
| The site | GitHub Pages | Static pages that read every market from the API. Rebuilt when you push `main` |
| The API | Cloudflare Worker + D1 database | Visitor actions, the job queue, the schedule (every minute, in Toronto time), and the data the site reads |
| The job | Vercel (Python function) | Builds the lists and adds markets, in short steps the API starts |
| Bot check | Cloudflare Turnstile | Runs before any visitor write |

## Before you start

- Accounts: Cloudflare, Vercel (Hobby), GitHub, and a Gemini API key from https://aistudio.google.com/apikey.
- On your computer: Node 20+, Python 3.12, and the GitHub CLI (`gh`).
- Keep your secrets in a `.env` file in the folder **above** the repo, so it is never committed. The Python tools read `WATCHLIST_API_URL`, `WATCHLIST_JOB_TOKEN` and `GEMINI_API_KEY` from there.

Make random values with `openssl rand -hex 32`.

## 1. Cloudflare (the API)

Run these from `worker/`:

1. Sign in: `npx wrangler login`.
2. Create the database: `npx wrangler d1 create rigged-outcomes`. Copy the id it prints into `database_id` in `wrangler.toml`.
3. Create the tables: `npx wrangler d1 migrations apply rigged-outcomes --remote`.
4. Make the bot check. In the Cloudflare dashboard, go to **Turnstile → Add widget**. Add your site's domain as the hostname, choose **Managed**, and keep the **Site key** and **Secret key**.
5. Set the secrets. Run `npx wrangler secret put NAME` for each one and paste the value when asked:

   | Name | Value |
   |---|---|
   | `TURNSTILE_SECRET` | Turnstile's Secret key |
   | `OWNER_TOKEN` | A random value: your password for the review page |
   | `JOB_TOKEN` | A random value: the job's key to the API |
   | `VISITOR_SALT` | A random value |
   | `WAKE_SECRET` | A random value: signs the API's calls to Vercel |
   | `WAKE_GATE` | A random value: the header the Vercel firewall lets through |

6. In `wrangler.toml`, set `ALLOWED_ORIGINS` and `TURNSTILE_HOSTNAMES` to your site's domain.
7. Deploy: `npx wrangler deploy`. It prints the API's address, for example `https://rigged-outcomes-api.<you>.workers.dev`.

## 2. Vercel (the job)

Run these from the repository root:

1. Sign in and link: `npx vercel login`, then `npx vercel link` (create a project, directory `./`).
2. Add four settings. Run `npx vercel env add NAME production --sensitive` for each one and paste the value:

   | Name | Value |
   |---|---|
   | `GEMINI_API_KEY` | Your Gemini key |
   | `WATCHLIST_API_URL` | The API's address from step 1.7 |
   | `WATCHLIST_JOB_TOKEN` | The same value as `JOB_TOKEN` |
   | `WAKE_SECRET` | The same value as the Worker's `WAKE_SECRET` |

3. Deploy: `npx vercel deploy --prod`. It prints the production address, for example `https://rigged-outcomes.vercel.app`.
4. Put that address plus `/api/step` into `STEP_URL` in `worker/wrangler.toml`, then run `npx wrangler deploy` again from `worker/`.
5. Add the firewall rule, so only your API can start the job. In the Vercel dashboard, open the project and go to **Firewall → Configure → New Rule**:
   - **Name:** `only the Worker`
   - **If** `Request Path` `equals` `/api/step`
   - **And** `Request Header` with key `x-ro-gate` `does not equal` the `WAKE_GATE` value itself. That's the long random string, not the word "WAKE_GATE".
   - **Then:** `Deny`

   Click **Save Rule**, then **Review Changes → Publish**.
6. Check it: `curl -X POST https://<your-project>.vercel.app/api/step` should answer `403`.

## 3. GitHub (the site)

1. In the repo, go to **Settings → Secrets and variables → Actions → Variables** and add two variables:
   - `API_URL`: the API's address
   - `TURNSTILE_SITEKEY`: Turnstile's Site key
2. Go to **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Put the 7 original markets in the database, from the repository root: `python3 -m backend.site originals`. It builds their pages from the CSVs on the `original-project` branch (on a fresh clone, run `git fetch origin original-project` first). Running it again changes nothing.
4. Push `main`. The "Deploy dashboard to GitHub Pages" action builds the site.

## After a code change

Pushing only rebuilds the site. Deploy the other parts yourself:

| You changed | Run |
|---|---|
| `web/` | Push `main` |
| `worker/` | `cd worker && npx wrangler deploy`. If there's a new file in `worker/migrations/`, run `npx wrangler d1 migrations apply rigged-outcomes --remote` first |
| The Python job: `api/`, `backend/`, `watchlist/`, `add_market.py` and the other scripts | `npx vercel deploy --prod` |

Before pushing, run the tests: `python3 tests/test_*.py`, `cd worker && npm test`, and the end-to-end tests in `tests/end-to-end/` (see its README).

## Day to day

- **The daily run:** at 4 AM Toronto time. If a step couldn't finish, a catch-up runs at 10 AM.
- **The review page:** `https://<your site>/#/review`, unlocked with `OWNER_TOKEN`. It shows:
  - the Open now queue;
  - recent runs and any failed step;
  - how much of Vercel's free allowance is used.
- **The free allowance:** adds wait when Vercel use reaches 65% of the free monthly allowance, and all work stops at 80%. That keeps the account from being paused.

## If something breaks

| Problem | Fix |
|---|---|
| See the API's errors | `cd worker && npx wrangler tail` |
| See the job's log | `npx vercel logs --deployment <your-project>.vercel.app` |
| Stop visitors' changes | The pause switch on the review page |
| Stop all work on Vercel | Set the Worker's `WAKE_SECRET` to a new random value. Set it back to resume |
| Take a market off the main page | `python3 -m backend.site rm NAME` |
| Undo a bad save of a list | `python3 -m watchlist.blobs versions site:closed.json`, then `python3 -m watchlist.blobs restore site:closed.json <version>`. The same works for `site:open.json` and `state` |
| Put the database back to an earlier time (up to 7 days) | `cd worker && npx wrangler d1 time-travel restore rigged-outcomes --timestamp <time>` |
| Vercel can't reach Google News or Polymarket | `scripts/watchlist-mac.sh` runs the daily lists on your own computer |
