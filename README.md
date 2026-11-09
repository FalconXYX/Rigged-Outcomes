# Rigged Outcomes

Detects potential insider trading on Polymarket prediction markets by analyzing who traded profitably before major outcomes occurred.

Every trade in a resolved market is fetched, aggregated and scored, then published on the static site at **https://parthjain.ca/Rigged-Outcomes/** (bet timeline with replay, flagged traders, a home page you can sort and filter).

**Contents:** [Quick start](#quick-start) · [What it does](#what-it-does) · [Data flow](#data-flow) · [markets.json](#marketsjson-structure) · [Key columns](#key-columns) · [Detection logic](#insider-detection-logic) · [Web dashboard](#web-dashboard) · [Export](#export-exportpy) · [Data sources](#a-note-on-data-sources) · [One-time production setup](#one-time-production-setup)

---

## Quick Start

All scripts run from the repo root (paths are cwd-relative). Python 3.12, Node 24.

**Install dependencies:**

```bash
pip install -r requirements.txt     # requests, pandas, numpy — the pipeline and CI
pip install plotly dash             # optional: only the two Dash visualizers need these
npm --prefix web install            # the site
```

**Run the analysis:**

```bash
python3 run.py                          # fetch + clean every market in markets.json
python3 run.py iran                     # ...or only the markets whose name/slug contains "iran"
python3 scrape_insiders.py iran         # score the whales of ONE market (first match; no keyword = first entry)
python3 scrape_insiders.py --name fired # ...or exactly the entry named "fired"
```

**View results:**

```bash
python3 VisualizeInsiders.py iran   # interactive trader dashboard (Dash)
python3 VisualizeBetTiming.py iran  # timeline of trading activity (Dash)

python3 export.py                   # data/*.csv → web/public/data/ (the static site's JSON)
npm --prefix web run dev            # http://localhost:5173
```

Output CSVs go to the `data/` folder. Only `_clean.csv` and `_insiders.csv` are versioned: `.gitignore` excludes new raw dumps (`data/<Output>.csv`), which can be tens of MB (the seven raw CSVs of the analyzed markets were committed before that rule and stay tracked). `python3 run.py` on an already analyzed market overwrites its raw and clean CSVs with the larger v2 data set, so pass a keyword for the market you mean.

> **Note (November 2026):** Polymarket retired the Goldsky subgraph and Data API v1. The pipeline now fetches everything through Data API v2 via `polymarket_api.py` (shared by `polymarket_trades.py` and `scrape_insiders.py`); an API error raises instead of being read as "no trades". The seven analyzed markets' raw and clean CSVs were produced by the old crawl and are committed as-is; the `_insiders.csv` rows the old crawl had mis-scored were re-scored (see [A note on data sources](#a-note-on-data-sources)).

**Tests** (all offline unless noted):

```bash
python3 tests/test_offline.py        # pipeline unit tests, seconds
python3 tests/test_v2_parity.py      # network, 2-3 minutes: re-fetches two markets and compares them with the committed CSVs
npm --prefix web run typecheck
```

Under pytest the parity suite is skipped unless `PARITY_NETWORK=1`.

---

## What It Does

1. **Fetches** all trades from Polymarket for a given market
2. **Cleans** data by aggregating trades per user/outcome/side
3. **Identifies** suspicious whales: big winning bets from accounts that are brand new or staked almost everything they ever bet on this one market
4. **Visualizes** the result, in the two Dash apps and on the static site

---

## Data Flow

`markets.json` → `run.py` →

- `polymarket_trades.py` (fetch every fill from Data API v2 `/v2/trades`, maker and taker rows) → `data/{Output}.csv`
- `cleandata.py` (aggregate) → `data/{Output}_clean.csv`

then `scrape_insiders.py <keyword>` (score whales, one market per run) → `data/{Output}_insiders.csv`, and `export.py` → `web/public/data/` for the site.

Output files:

- `{Output}.csv` — Raw individual trades
- `{Output}_clean.csv` — Aggregated positions per user/outcome/side
- `{Output}_insiders.csv` — Scored whales with their flags

---

## markets.json Structure

Each market needs:

- `name`: Short identifier (used in URLs: `#/m/<name>`)
- `slug`: Polymarket market slug
- `output`: CSV filename prefix
- `correct_outcome`: "Yes" or "No" (what actually happened)
- `timeRanges`: Named periods such as "Normal" and "Leadup" (`YYYY-MM-DD-HH:MM`, US/Eastern)
- `importantDates` (optional): timestamp → label, drawn as events on the timeline
- `highlightedUsers` (optional): wallet → display name, named traders to track

Example:

```json
{
  "name": "usiran",
  "slug": "us-strikes-iran-by-february-28-2026-...",
  "output": "USxIranStrikesFeb28",
  "correct_outcome": "Yes",
  "importantDates": { "2026-02-28-01:12": "Strikes Hit" },
  "timeRanges": {
    "Leadup": { "start": "2026-02-26-00:00", "end": "2026-02-28-01:08" }
  },
  "highlightedUsers": { "0xdde15ebd95330ce69136dc0ccd810d22382e02c5": "DiceDiceDice" }
}
```

---

## Key Columns

**Raw data (`.csv`):**

- `user_id`: Wallet address (Polymarket proxy wallet, lowercase)
- `eoa_address`: kept for schema compatibility; equals `user_id` (the trade feed exposes no separate signer, and the old crawl recorded the proxy wallet here too). Five of the committed raw CSVs predate the column; `cleandata.py` then writes it empty and the scorer falls back to `user_id`
- `outcome`: Yes or No
- `side`: BUY or SELL
- `contracts`: Volume
- `dollar_amount`: USD spent
- `odds`: Price per contract
- `timestamp`: Trade time
- `transaction_hash`

**Cleaned data (`_clean.csv`):**

- One row per user/outcome/side
- `avg_odds`: Average price paid
- `contracts`, `dollar_amount`: Totals
- `win_status`: WIN or LOSS
- `first_trade` / `last_trade`: Time range
- `num_trades`: Trades aggregated into the row

**Insiders (`_insiders.csv`):**

- `market_position`: USD staked on the winning outcome in this market
- `lifetime_portfolio`: USD the wallet had ever staked up to its first trade here (at least `market_position`)
- `prior_bet_count`: Transactions before that first trade. The history fetch stops at `polymarket_api.HISTORY_MAX_PAGES` × 1 000 trades (50 000) closest before that trade and says so; for a wallet that busy the count is a floor and `portfolio_concentration` is overstated (never enough to flag it)
- `portfolio_concentration`: `market_position / lifetime_portfolio`, in percent
- `is_first_x`: new account (`prior_bet_count` ≤ 10)
- `Is_percent`: concentrated (`portfolio_concentration` ≥ 80 %)
- `is_suspicious`: either flag
- `target_trade_time`: the wallet's first trade in the market

---

## Insider Detection Logic

1. **Whales** are BUY positions on the winning outcome at or above 0.1 % of the market's volume, clamped to $1 000–$5 000 (`scrape_insiders.whale_selection`). Two kinds of rows are left out:
   - **Near-certain buys** with an average price of 0.99 or more (`NEAR_CERTAIN_ODDS`). Paying 99¢ or more wins at most about 1 %. That is collecting a payout once the outcome is public, not an informed bet.
   - **SELL rows of the winning outcome.** `cleandata.py` merges a SELL into the same wallet's complement BUY when one exists. Any SELL row left over is either the exit of a BUY that is already counted, or minted shares sold, which is a bet on the other side.

   Named `highlightedUsers` are always included.
2. Each whale's history up to its first trade in the market is fetched and the wallet is flagged when it is a **new account** (≤ 10 prior transactions) and/or **concentrated** (this bet is ≥ 80 % of everything it ever staked). Both flags at once is the *critical* category on the site.
3. The site layers the market's `timeRanges` ("Normal" vs "Leadup") on top, so a whale that only appears in the hours before the outcome stands out.

---

## Web Dashboard

The two Dash apps (`VisualizeBetTiming.py` and `VisualizeInsiders.py`) are also published as a static site at **https://parthjain.ca/Rigged-Outcomes/**. It is built with Vite + React + TypeScript + D3 and has no server: `export.py` precomputes everything into JSON and the browser does the rest.

| Route | Page |
|---|---|
| `#/` | Home: every market as a card with a preview |
| `#/m/<name>` | Bet timeline with replay, zoom/brush, pinned wallet card |
| `#/m/<name>/traders` | Flagged traders: constellation + treemap |

**Run locally:**

```bash
python3 export.py                 # data/*.csv → web/public/data/*.json (~1 s)
cd web && npm install && npm run dev
```

Re-run `python3 export.py` (or `npm run data` from `web/`) whenever the CSVs or `markets.json` change. `web/public/data/` is generated and gitignored; don't edit it by hand.

**Other scripts (in `web/`):**

- `npm run build`: type-check (`tsc -b`) and build to `web/dist/`. It doesn't run Python, so export the data first.
- `npm run preview`: serve the production build locally.
- `npm run typecheck`: type-check only.

**How deploy works:** every push to `main` (or a manual run from the Actions tab) triggers `.github/workflows/deploy.yml`. The workflow runs `pip install -r requirements.txt` and `python export.py`, then `npm ci && npm run build` in `web/`, uploads `web/dist/` as a Pages artifact and deploys it with `actions/deploy-pages`. The build uses a relative base (`./`) and hash routes (`#/m/usiran/traders`), so it works under any sub-path and survives page refreshes on GitHub Pages without extra config. First-time setup is in [One-time production setup](#one-time-production-setup).

### Home page

Markets appear in `markets.json` order. A **Sort & filter** button above the grid opens a bar to sort (*Most suspicious* — flagged wallets' share of the market's net winnings, falling back to their share of whale capital — *Flagged winnings*, *Flagged accounts*, *Volume*, *Newest*) and filter (resolved Yes/No, only markets with flagged wallets). Nothing is applied by default; the choice lives in the URL (`#/?sort=volume&resolved=yes&flagged=1`) so a sorted view is a shareable link. Ranking logic is in `web/src/views/home/ranking.ts`.

### Market replay and shareable links (timeline view)

**Replay.** Every market page has a transport row above the plot: Play/Pause, speed (1× plays the visible range in ~40 s; 4× and 16× faster), a scrubber across the visible range, the playhead time (ET) and Reset. While it runs, a playhead sweeps the chart: the odds line draws itself, crowd volume grows, whale bets pop in when their first trade is reached (highlighted users with a pulse ring and their name), and the playhead rests for a moment on each event. It works on all three tabs and with the odds axis on or off. Keyboard, with focus in the chart card: **Space** play/pause, **← →** nudge the playhead (Shift for bigger steps), **Esc** stop. Zooming, brushing or picking a range while playing pauses it. Under *reduced motion* nothing pops or pulses; the playhead just moves.

**Shareable links.** The URL always describes what you see: `#/m/<market>?view=cmp&range=Leadup&from=…&to=…&odds=0&filter=1&pin=0x…&series=events,threshold&t=…` (view, preset, zoomed window in epoch seconds, odds axis off, stats filtered, pinned wallet, hidden legend series, paused replay position). Defaults are left out, writes are throttled so zooming never thrashes the address bar, and anything unknown in a link is ignored. **Copy link** (top right of the chart card) copies it. Switching markets keeps your view / odds / filter choice and resets the range and zoom.

---

## Export (`export.py`)

`python3 export.py` (repo root, pandas + numpy, no options, no network) turns the pipeline's CSVs into the static JSON the site reads from `web/public/data/`:

| Output | What it is |
|---|---|
| `index.json` | one entry per market — including the ranking fields: `flaggedWinnings` / `flaggedWinningsPct` (net profit taken by whales whose wallet is flagged, and its share of all winnings), `flaggedWhaleCapitalPct` (= `pctSuspicious`), `namedWallets` |
| `markets/<name>.json` | timeline + traders for one market |

The whale set, threshold, winner mask and IQR filter are computed once per market (`prepare_market`) and shared by the timeline and the ranking fields. Timestamps keep the site's convention (epoch seconds of the US/Eastern wall clock read as UTC; the front end formats them with UTC APIs). The export finishes in about a second. The TypeScript contract lives in `web/src/types.ts`.

---

## A note on data sources

Polymarket retired the Data API v1 endpoints and the Goldsky orderbook subgraph in November 2026. Every fetch now goes through `polymarket_api.py` (Data API v2 + Gamma), which `polymarket_trades.py` (`/v2/trades?condition=…&taker_only=false`, one walk per market instead of a subgraph address crawl plus one v1 call per wallet), and `scrape_insiders.py` (`/v2/activity?user=…&type=TRADE&end=<first trade>`) share. Error payloads raise, 429/5xx back off and retry, and a row/page cap is announced rather than applied silently. The CSV schemas are unchanged. `tests/test_v2_parity.py` re-fetches two of the committed markets and compares: the v2 feed holds every fill the old crawl recorded, plus the fills it lost (its per-wallet walk stopped early on rate limits, keeping only a wallet's newest rows), so a re-fetched market can have more rows and larger positions than its committed CSV, never smaller. v2 trade rows carry shares × price as the dollar leg; on the committed (fee-free) markets that equals the old `usdcSize` to within $0.0001. On a market that charges a taker fee the two feeds differ: `/v2/trades` serves a taker BUY's shares net of the fee (observed 1-3 % below), while `/v2/activity`, which the whale scorer reads, serves the gross shares and `usdc_size` = gross shares × price, the old convention. Raw CSVs of such a market therefore record slightly fewer contracts and dollars per taker BUY than the old crawl would have; nothing converts between the two.

The old per-wallet history fetch also failed silently: on a rate limit or any error it returned the rows it had so far, so a busy wallet could be scored with 0 prior trades, 100 % concentration and `is_suspicious` — the signature of a failed fetch, not of a new account. Fifteen such rows existed across the committed `_insiders.csv` files (every one a wallet with thousands of earlier trades on `/v2/activity`); twelve were re-scored in place with the v2 fetcher in November 2026 (`prior_bet_count` 1 532 – 49 004, concentration ≤ 2.6 %, none suspicious) and the other three were on the losing side of the superbowl market, whose `markets.json` entry had `correct_outcome` wrong ("No"; the market resolved Yes, as its own `win_status` column says). That entry was corrected and the file regenerated from the committed `_clean.csv`; `scrape_insiders.py` now refuses a `correct_outcome` that contradicts `win_status`. The v2 fetcher raises on a failed fetch, so a run can no longer produce that signature.

---

## One-time production setup

Everything below is done once, on GitHub (repository `FalconXYX/Rigged-Outcomes`).

> **Heads-up:** `parthjain.ca/Rigged-Outcomes/` currently serves the older D3 story site ("The Story: Insider Trading Radar") straight from the `D3.js` branch. Steps 2–3 replace it with this dashboard. If that site needs to stay up (for example, as the CCT416 submission), move it or keep a copy before you switch — this is still an open decision.

1. **Merge this work into `main`.** `deploy.yml` runs on pushes to `main`.
2. **Pages source → GitHub Actions.** Settings → Pages → Build and deployment → Source: choose **GitHub Actions** (it is on "Deploy from a branch: `D3.js`" today). Or: `gh api -X PUT repos/FalconXYX/Rigged-Outcomes/pages -f build_type=workflow`.
3. **Allow `main` to deploy.** Settings → Environments → `github-pages` → Deployment branches and tags: add `main`. The environment only allows `D3.js` right now, so without this the deploy job is rejected with *"Branch "main" is not allowed to deploy to github-pages due to environment protection rules"*. Or: `gh api -X POST repos/FalconXYX/Rigged-Outcomes/environments/github-pages/deployment-branch-policies -f name=main`.
4. **Deploy.** Push to `main` or run "Deploy dashboard to GitHub Pages" from the Actions tab; the site is served at https://parthjain.ca/Rigged-Outcomes/.
