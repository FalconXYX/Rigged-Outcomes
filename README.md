# Rigged Outcomes

Detects potential insider trading on Polymarket prediction markets by analyzing who traded profitably before major outcomes occurred.

Every trade in a resolved market is fetched, aggregated and scored, then published on the static site at **https://parthjain.ca/Rigged-Outcomes/** (bet timeline with replay, flagged traders, a home page you can sort and filter). A Watchlist page adds two daily lists made from market text and the news, with no trade data: open markets worth watching, and recently closed markets someone may have known about early.

**Contents:** [Quick start](#quick-start) · [What it does](#what-it-does) · [Data flow](#data-flow) · [markets.json](#marketsjson-structure) · [Key columns](#key-columns) · [Detection logic](#insider-detection-logic) · [Web dashboard](#web-dashboard) · [Export](#export-exportpy) · [Recently closed list](#recently-closed-list-watchlistupsetspy) · [Open now list](#open-now-list-watchlistopennowpy) · [Data sources](#a-note-on-data-sources) · [One-time production setup](#one-time-production-setup)

---

## Quick Start

All scripts run from the repo root (paths are cwd-relative). Python 3.12, Node 24.

**Install dependencies:**

```bash
pip install -r requirements.txt     # requests, pandas, numpy — the pipeline and CI
pip install plotly dash dash-mantine-components   # optional: only the two Dash visualizers need these
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
for f in tests/test_watchlist_*.py; do python3 "$f" || break; done   # the watchlist modules, about a second each
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
| `#/watchlist` | Watchlist: Open now (open markets to watch) and Recently closed (closed markets someone may have known about early); a card's details open in a side sheet. The old `#/closed` redirects here |

**Run locally:**

```bash
python3 export.py                 # data/*.csv → web/public/data/*.json (~1 s)
cd web && npm install && npm run dev
```

Re-run `python3 export.py` (or `npm run data` from `web/`) whenever the CSVs or `markets.json` change. `web/public/data/` is generated and gitignored; don't edit it by hand. Its `closed.json` and `open.json` come from the two watchlist lists ([Recently closed](#recently-closed-list-watchlistupsetspy), [Open now](#open-now-list-watchlistopennowpy)), not from `export.py`.

**Other scripts (in `web/`):**

- `npm run build`: type-check (`tsc -b`) and build to `web/dist/`. It doesn't run Python, so export the data first.
- `npm run preview`: serve the production build locally.
- `npm run typecheck`: type-check only.

**How deploy works:** every push to `main` (or a manual run from the Actions tab) triggers `.github/workflows/deploy.yml`. The workflow runs `pip install -r requirements.txt` and `python export.py`, then `npm ci && npm run build` in `web/`, uploads `web/dist/` as a Pages artifact and deploys it with `actions/deploy-pages`. It does not make the Watchlist's data: `closed.json` and `open.json` are made only by local runs of `watchlist.upsets` and `watchlist.opennow` and are gitignored, so a deployed build has neither (its Watchlist page shows a load error under Recently closed and "Coming soon" under Open now). The build uses a relative base (`./`) and hash routes (`#/m/usiran/traders`), so it works under any sub-path and survives page refreshes on GitHub Pages without extra config. First-time setup is in [One-time production setup](#one-time-production-setup).

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

## Recently closed list (`watchlist/upsets.py`)

The "Recently closed" section of the Watchlist page shows, for each UTC day, 3 to 5 markets that closed that day and are worth a person's look as possible insider cases: someone could have known the answer before the public and bet on it. It is a light first filter from what the market asked, how it resolved and the news. No price, trade or wallet is read. For each day:

1. Every market that closed in it with at least $10,000 traded (from Gamma); one with no single winner is left out.
2. Left out before any model call: games, prices and weather (`watchlist/filters.py`), and by the word rules both lists share (`kinds.closed_exclusion` in `watchlist/kinds.py`) elections and votes, embargoed releases (official statistics, central bank decisions, scheduled company results, share-offering prices), things counted or charted in public, mention markets and nature. A model label (`watchlist/ai.py`, `gemini-3.5-flash-lite` first) then takes out the games, prices, weather, elections and votes the words missed. A deadline question whose answer was that nothing happened is left out too, a date ladder counts once, and markets settled by the same news count once.
3. The check (`watchlist/judge.py`): a model writes a news query per market, Google News gives the headlines from three weeks before the close to two days after, and `gemini-3-flash-preview` (`gemini-3.5-flash-lite` when it is busy) answers three questions: could a small group know first, was the outcome a surprise, and was it kept quiet until it came out. Its verdict (potential case, weak, not a case) is made to agree with those answers.
4. The day's list: the potential and weak cases, up to 5, topped up to 3 with the closest of the rest when the day has that many. A market that was on an Open now card is checked and shown whatever its verdict, unless it is a game, a price, weather or an embargoed release.

```bash
python3 -m watchlist.upsets                          # today so far (UTC), merged into web/public/data/closed.json
python3 -m watchlist.upsets --days 3                 # the last 3 UTC days, today included
python3 -m watchlist.upsets --since 2026-10-30       # every UTC day from this one to today
python3 -m watchlist.upsets --out PATH --keep-days 14   # another output file; days kept in it (default 14)
python3 tests/test_watchlist_upsets.py               # offline tests (the check: tests/test_watchlist_judge.py)
```

There is no dry run: every run asks Gamma, Gemini and Google News. AI labels and checks are cached in `data/watchlist/watchlist.db`, so a re-run only asks about markets it has not checked. The file is written after every day, so a stalled run keeps the days it finished. The key is read from `GEMINI_API_KEY` or from the `.env` file next to the repository.

---

## Open now list (`watchlist/opennow.py`)

The "Open now" list (a trial) shows 3 to 5 markets that are still open, where a few people will know the answer before the public and this week's news shows them taking a step toward it. One run walks Polymarket's open events with at least $10,000 traded, searches Google News for each story and writes `web/public/data/open.json`. No price, odds, volume, trade or wallet reaches the model or chooses a card: volume and the 99¢ check (made once, when an event first enters the pool) only decide what is in the pool. The rules and their reasons are in the module's docstring and in `watchlist/kinds.py`.

**The step reading proposes the steps; a person approves each card's step.** A market is listed when this week's news shows the few people who decide taking a concrete step toward the answer. The free Gemma model `gemma-4-31b-it` (same key, its own allowance, never a paid model) reads each shortlisted story once per request: the question, the event that would settle it, who decides by name, the start of the rules, and up to 8 of its step-word headlines from the last 7 days, numbered. For every number it says who acts, what happened, and one category ("step", or another event, another thing with the same name, already out, a denial, not the deciders, a rumour with no inside source). The code keeps a step only for a number it was given, once, whose "who acts" words are in that headline; a reading that marked a step is asked again with the headlines numbered in reverse, and only a headline both readings mark is proposed to a person (approved, it counts as the card's step). A headline's verdict is stored the first time it is read, so nothing flips overnight. A denial both readings name is shown as "the other side". `gemini-3.5-flash-lite` still names new stories and checks whether an answer is already out (twice, every copy checked word for word); `gemini-3-flash-preview` is never used, so its small allowance stays with the closed list.

Why a person still approves: the first reading (flash-lite copying step headlines) failed its bars on 2026-11-13 (its two readings agreed on step or no step for 86% of 155 stories, bar 90%; about 3 in 10 copied steps were real, bar 8 in 10). The Gemma reading was then scored on a frozen yardstick of 63 of that day's stories and 397 hand-graded headline pairs: its two readings agreed on step or no step for all 63 stories, 83% of the headlines either reading marked were real steps (95% of those both marked, the rule the list uses), and it found 12 of the 13 stories with a real step. But the prompt was chosen on that yardstick. An independent check on 2026-11-14 graded every headline both readings marked that no gold label covers (from the day's live run, and from a second pass over the headlines the 8-headline cap had left out): on stories outside the yardstick, 23 of 37 were real steps (62%); over all of them, 52 of 68 (76%). That is under the 8-in-10 bar, so, as the proposal says when the bar is not met, the readings' steps wait in the `--review` queue and a card shows once a person approves its step (`STEPS_BY = "person"` at the top of `watchlist/opennow.py`; a dated card needs no step and shows on its own). `--review` lists the stories the list would take, each with the steps both readings marked first; `--approve` makes a headline count, and `--reject` takes a headline (or a dated card) off for good. The approvals made on 2026-11-13 still count. `STEPS_BY = "model"` lets the readings' steps show without approval, once a fresh test on another day (a person's blind grade of 30 marked steps, as the proposal asks) passes.

```bash
python3 -m watchlist.opennow                        # today's list: stored in data/watchlist/watchlist.db, written to web/public/data/open.json
python3 -m watchlist.opennow --review               # the steps waiting for a person, numbered (both readings' first)
python3 -m watchlist.opennow --approve 1 3.2        # item 1's first headline and item 3's second count as steps; rebuilds the reviewed list
python3 -m watchlist.opennow --reject 2 4.1         # never shown (2: every headline offered for item 2, or its dated card); rebuilds
python3 -m watchlist.opennow --dry-run --report data/watchlist/r.json   # walk the pool and search the news, ask the model nothing; nothing goes in the database or open.json (the searches are cached)
python3 -m watchlist.opennow --dry-run --skip-news  # the pool walk alone (about 15 seconds)
python3 -m watchlist.opennow --consistency DIR      # the consistency test of the step reading: both readings of the shortlist, the agreement report and a blind grading sheet in DIR
python3 tests/test_watchlist_opennow.py             # offline tests
```

`--approve` and `--reject` rebuild the list that `--review` showed (the latest stored day), as it was made: at that day's own run time, from its searches (up to 30 hours older than the run) and its stored readings. The rebuild never asks the step model, so it takes seconds; a card that moves up to fill a freed place may cost one flash-lite request for its "already out" check. A day the model was down for stays "not updated today". A daily run takes about 15 minutes of Google News at about one search a second (about 1,400 searches, since stories whose outcomes can each happen, such as "Who will Trump pardon?", became one story per outcome), then the step reading: one Gemma request per shortlisted story (about 145 on a heavy day such as 2026-11-13), three at a time, 20 to 90 seconds each because the model is often busy (its 503s come back at once and are retried), so one to two hours; then the second reading of the stories with a step (10 to 25 requests). The rerun of 2026-11-13 read 144 stories and 25 again in 124 minutes: 169 answers from 525 HTTP requests (356 were busy retries), plus 3 flash-lite requests. Later runs read only headlines no reading has had before it: a reading is given up to 8 of a story's step-word headlines, and the others it picked from count as seen too, so a second run on the same news asks nothing and lists the same cards. It also makes a handful of flash-lite requests for the "already out" check, and naming a new story for its sentences costs one flash-lite request per 40 stories, once. `--workers N` sets how many step readings run at once. The key is read from `GEMINI_API_KEY` or from the `.env` file next to the repository. When a model is busy or down (three step readings in a row fail, every one fails for any reason, the key is refused, or an "already out" request fails every try), the day keeps yesterday's cards that are still live, adds none, and says "not updated today"; one story whose step reading fails while others are answered is only left for the next run. Run it at about 04:00 UTC, once a day. A second run on the same day replaces that day's list only when it finishes: one made while a model is down leaves the list already made that day as it was.

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
