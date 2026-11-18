# Rigged Outcomes

Detects potential insider trading on Polymarket prediction markets by analyzing who traded profitably before major outcomes occurred.

Live at **https://parthjain.ca/Rigged-Outcomes/**. Why things are built the way they are: [`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Quick Start

**Install dependencies** (Python 3.12, Node 24; run everything from the repo root):

```bash
pip install -r requirements.txt                    # requests, pandas, numpy
pip install plotly dash dash-mantine-components    # only for the two Dash apps
npm --prefix web install
```

**Run full analysis on a market:**

```bash
python3 run.py iran               # fetch + clean the markets whose name/slug contains "iran"
python3 run.py                    # fetch + clean every market (rewrites the committed CSVs)
python3 scrape_insiders.py iran   # score the whales of one market
```

**View results:**

```bash
python3 export.py                   # data/*.csv → web/public/data/ (the site's JSON)
npm --prefix web run dev            # the site at http://localhost:5173
python3 VisualizeInsiders.py iran   # interactive trader dashboard (Dash)
python3 VisualizeBetTiming.py iran  # timeline of trading activity (Dash)
```

Output CSVs go to `data/` folder (new raw CSVs are gitignored: tens of MB).

---

## What It Does

1. **Fetches** all trades from Polymarket for a given market (Data API v2, via `polymarket_api.py`)
2. **Cleans** data by aggregating trades per user/outcome/side
3. **Identifies** suspicious whales: big winning bets from brand-new accounts, or from accounts that staked almost everything on this one market
4. **Visualizes** the result on the site (`web/`) and in the two Dash apps
5. **Watches** for new cases: a daily Watchlist page built from market text and the news

---

## Data Flow

`markets.json` → `run.py` →

- `polymarket_trades.py` (fetch raw trades) → `{Output}.csv`
- `cleandata.py` (aggregate) → `{Output}_clean.csv`

then `scrape_insiders.py` (detect patterns) → `{Output}_insiders.csv`, and `export.py` → `web/public/data/` (the site's JSON).

---

## markets.json Structure

Each market needs:

- `name`: Short identifier (the site's URL: `#/m/<name>`)
- `slug`: Polymarket market slug
- `output`: CSV filename prefix
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

**Raw data (`.csv`):** `user_id` (wallet address), `outcome` (Yes or No), `side` (BUY or SELL), `contracts`, `dollar_amount`, `odds` (price per contract), `timestamp`

**Cleaned data (`_clean.csv`):** one row per user/outcome/side, with `avg_odds`, `win_status` (WIN or LOSS), `first_trade` / `last_trade` and `num_trades`

**Insiders (`_insiders.csv`):** `market_position` (USD on the winning outcome), `prior_bet_count`, `portfolio_concentration` and the flags below

---

## Insider Detection Logic

Whales are BUY positions on the winning outcome worth at least 0.1% of the market's volume (clamped to $1,000–$5,000), bought under 99¢. Flags whales who:

1. Are a new account (`is_first_x`): 10 or fewer trades before this market
2. Are concentrated (`Is_percent`): this bet is at least 80% the size of everything they staked before it

Either sets `is_suspicious`; both at once is *critical* on the site. The "Leadup" period on the timeline shows who only appeared in the final hours.

---

## Web Dashboard

Vite + React + TypeScript + D3 in `web/`, built from the JSON `export.py` writes. Every push to `main` publishes it to GitHub Pages (`.github/workflows/deploy.yml`); setup and fixes are in [`docs/DEPLOY.md`](docs/DEPLOY.md).

| Route | Page |
|---|---|
| `#/` | Home: every market as a card |
| `#/m/<name>` | Bet timeline with replay and visitors' markers; `/traders`: flagged traders |
| `#/watchlist` | Open now and Recently closed (below) |
| `#/review` | The owner's page: the last run, the Open now queue, what visitors added, a pause switch |

---

## Watchlist

Two daily lists made from market text and Google News headlines, read by free Gemini and Gemma models. No prices, trades or wallets.

- **Open now** (`watchlist/opennow.py`): up to 5 open markets where a few people will know the answer first and this week's news shows a step; a person approves each step on `#/review`.
- **Recently closed** (`watchlist/upsets.py`): 3 to 5 closed markets a day someone may have known early; "Add to main page" runs the analysis above on one (`add_market.py`).
- **Backend** (`worker/`): a Cloudflare Worker + D1 database holding markers, the watchlist, added markets, the owner's decisions and the job's data (never git); see [`worker/README.md`](worker/README.md).
- **Daily job** (`.github/workflows/watchlist.yml`): at about 04:17 UTC the owner's decisions, both lists, the added markets and a deploy; a catch-up at about 10:17 UTC reads what Open now left unread.

**Run it locally** (the lists ask Gamma, Gemini and Google News; the key is `GEMINI_API_KEY` or `../.env`):

```bash
python3 -m watchlist.upsets             # Recently closed: today so far → web/public/data/closed.json
python3 -m watchlist.opennow            # Open now → web/public/data/open.json
npm --prefix worker run dev             # the API at http://127.0.0.1:8788 (first-time setup: worker/README.md)
```
