# Rigged Outcomes

Detects potential insider trading on Polymarket prediction markets by analyzing who traded profitably before major outcomes occurred.

Live at **https://parthjain.ca/Rigged-Outcomes/**. Why things are built the way they are: [`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Quick Start

**Install dependencies** (Python 3.12, Node 24; run everything from the repo root):

```bash
pip install -r requirements.txt     # requests, pandas, numpy, tzdata
npm --prefix web install
npm --prefix worker install
```

**Run it locally** (first-time setup of the local API: [`worker/README.md`](worker/README.md)):

```bash
npm --prefix worker run dev         # the API and its local database at http://127.0.0.1:8788
python3 -m backend.dev_server       # a local step function for the API to wake (docs/BACKEND.md)
WATCHLIST_API_URL=http://localhost:8788 WATCHLIST_JOB_TOKEN=dev-job-token \
  python3 -m backend.site originals # the 7 original markets into the local database
VITE_API_URL=http://localhost:8788 VITE_TURNSTILE_SITEKEY=1x00000000000000000000AA \
  npm --prefix web run dev          # the site at http://localhost:5173
```

**Add a market by hand** (what "Add to main page" does; it saves to the API in `WATCHLIST_API_URL`):

```bash
python3 -m pipeline.add_market --condition-id 0x... --slug SLUG --outcome Yes [--dry-run]
```

The original class project (the two Dash apps, `run.py`, the scripts and the CSVs in `data/`) lives on the `original-project` branch.

---

## What It Does

1. **Fetches** all trades from Polymarket for a given market (Data API v2, via `pipeline/polymarket_api.py`)
2. **Cleans** data by aggregating trades per user/outcome/side
3. **Identifies** suspicious whales: big winning bets from brand-new accounts, or from accounts that staked almost everything on this one market
4. **Visualizes** the result on the site (`web/`)
5. **Watches** for new cases: a daily Watchlist page built from market text and the news

---

## Data Flow

"Add to main page" → the Worker queues an `add` run → the step function (`backend/add_steps.py`, through `pipeline/add_market.py`):

- `pipeline/polymarket_trades.py` (each trade) → `pipeline/cleandata.py` (aggregate) → the clean CSV
- `pipeline/scrape_insiders.py` (detect patterns) → the insiders CSV
- `pipeline/export.py` → the market's page and index entry

all in memory, saved in D1 (`backend/site.py`: the page, the two CSVs, the index) and served by the Worker at `/api/data/`. The 7 original markets (`markets.json`) are saved the same way, by `python3 -m backend.site originals`.

---

## markets.json Structure

Each market needs:

- `name`: Short identifier (the site's URL: `#/m/<name>`)
- `slug`: Polymarket market slug
- `output`: the CSVs' name prefix
- `correct_outcome`: "Yes" or "No" (what actually happened)
- `timeRanges` (optional): Define "Normal" vs "Leadup" periods for analysis (`YYYY-MM-DD-HH:MM`, US/Eastern)
- `importantDates` (optional): Events drawn on the timeline
- `highlightedUsers` (optional): Named traders to track

Example:

```json
{
  "name": "usiran",
  "slug": "us-strikes-iran-by-february-28-2026-...",
  "output": "USxIranStrikesFeb28",
  "correct_outcome": "Yes",
  "timeRanges": {
    "Leadup": { "start": "2026-02-26-00:00", "end": "2026-02-28-01:08" }
  }
}
```

---

## Key Columns

**Raw trades:** `user_id` (wallet address), `outcome` (Yes or No), `side` (BUY or SELL), `contracts`, `dollar_amount`, `odds` (price per contract), `timestamp`

**Clean CSV (`_clean.csv`):** one row per user/outcome/side, with `avg_odds`, `win_status` (WIN or LOSS), `first_trade` / `last_trade` and `num_trades`

**Insiders CSV (`_insiders.csv`):** `market_position` (USD on the winning outcome), `prior_bet_count`, `portfolio_concentration` and the flags below

---

## Insider Detection Logic

Whales are BUY positions on the winning outcome worth at least 0.1% of the market's volume (clamped to $1,000–$5,000), bought under 99¢. Flags whales who:

1. Are a new account (`is_first_x`): 10 or fewer trades before this market
2. Are concentrated (`Is_percent`): this bet is at least 80% the size of everything they staked before it

Either sets `is_suspicious`; both at once is *critical* on the site. The "Leadup" period on the timeline shows who only appeared in the final hours.

---

## Web Dashboard

Vite + React + TypeScript + D3 in `web/`. Every market (the 7 original ones and those added later) and the Watchlist's lists come from the API as the page loads. Every push to `main` publishes the site to GitHub Pages (`.github/workflows/deploy.yml`); setup and fixes are in [`docs/DEPLOY.md`](docs/DEPLOY.md).

| Route | Page |
|---|---|
| `#/` | Home: every market as a card |
| `#/m/<name>` | Bet timeline with replay and visitors' markers; `/traders`: flagged traders |
| `#/watchlist` | Open now and Recently closed (below) |

---

## Watchlist

Two daily lists made from market text and Google News headlines, read by free Gemini and Gemma models. No prices, trades or wallets. Every day and time is Toronto's.

- **Open now** (`watchlist/opennow.py`): up to 5 open markets where a few people will know the answer first and this week's news shows a step, as the model reads it. A market whose answer is already public (a released game, film, show, album or book included) is dropped. "Watch this market" puts one on the site's watchlist: once it closes with a result, it is added to the main page by itself.
- **Recently closed** (`watchlist/upsets.py`): 3 closed markets a day someone may have known early; "Add to main page" runs the analysis above on one, and it is on the main page within minutes.
- **Backend** ([`docs/BACKEND.md`](docs/BACKEND.md)): a Cloudflare Worker and its D1 database (the API, the job queue, the schedule, everything published) wake a Vercel function that does the work in short steps. Both on free plans; nothing runs on GitHub but the site build.
- **Schedule:** the daily run from 04:00 Toronto time, a catch-up from 10:00 only when work was left; an add starts the moment it is clicked.

**Run it locally** (the lists ask Gamma, Gemini and Google News; the key is `GEMINI_API_KEY` or `../.env`):

```bash
python3 -m watchlist.upsets             # Recently closed: today so far → web/public/data/closed.json
python3 -m watchlist.opennow            # Open now → web/public/data/open.json
```
