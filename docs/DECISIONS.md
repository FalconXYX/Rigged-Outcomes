# Decisions

Why Rigged Outcomes is built the way it is: one line per decision, one line of reason. How to run it is in the [README](../README.md); setup is in [DEPLOY.md](DEPLOY.md).

## The pipeline

| Decision | Why |
|---|---|
| Every fetch goes through Data API v2 (`polymarket_api.py`) | Polymarket retired Data API v1 and the Goldsky subgraph in November 2026 |
| A failed fetch raises; it is never read as "no trades" | The old fetcher scored busy wallets as brand new and 100% concentrated |
| 12 committed `_insiders.csv` rows were re-scored with v2 | They were failed v1 fetches: wallets with 1,532 to 49,004 earlier trades |
| superbowl's `correct_outcome` is "Yes", and `scrape_insiders.py` refuses one that contradicts `win_status` | It said "No", so its whales were picked from the losing side |
| Buys at 99¢ or more are not whales | Paying 99¢ wins at most about 1%: collecting a payout, not an informed bet |
| Leftover SELL rows of the winning outcome are not whales | Each is the exit of a counted BUY, or minted shares sold (a bet on the other side) |
| A re-fetched market can have more rows than its committed CSV, never fewer | v2 holds every fill the old crawl kept, plus the ones it lost to rate limits |
| On a market with a taker fee, raw CSVs hold a taker BUY's shares net of the fee | That is what `/v2/trades` serves; the scorer's `/v2/activity` serves gross; nothing converts |
| New raw CSVs are gitignored; clean and insiders CSVs are committed | Raw dumps can be tens of MB |

## The Watchlist: both lists

| Decision | Why |
|---|---|
| A model reads the market's text and the news only: no prices, trades or wallets | The whale and price ranking "isn't working", and wallet analysis is too heavy for a first pass (2026-11-12) |
| Everything is free: no paid keys, no billing | A hard requirement for the whole project |
| The code fetches Google News RSS and hands the model the headlines | The free Gemini key has no web search |
| Markets with $10,000+ traded | At $100,000 a day had only 0 to 8 non-game markets |
| Word rules run before any model call | Election day cost 920 Gemini calls before them |
| Left out: games, prices, weather, elections and votes, public counts, mention markets, nature | Insider knowledge can't explain them |
| Left out: central bank decisions, official statistics, scheduled results, share-offering prices | They are held under legal lock-ups, so they aren't insider-traded |
| The targets: answers held by a small, loosely controlled group, often on any day | Firings, strikes, launches, rulings; the seven featured markets all fit |
| The code checks every answer: copied words must match, numbers must be ones it gave | Models read; fixed rules a person wrote choose the cards |
| Gemini through the Interactions API | `generateContent` returns 404 for this key |

## Recently closed (`watchlist/upsets.py`)

| Decision | Why |
|---|---|
| A market's day is its `closedTime`, not its `endDate` | 44% of $100,000 markets close before their end date |
| Three questions: could a small group know first, was it a surprise, was it kept quiet | The verdict (potential, weak, not a case) is made to agree with the answers |
| Headlines from 3 weeks before the end date (or the close, if earlier) to 2 days after the close | Enough to see whether it leaked or came out at one moment |
| `gemini-3-flash-preview`, with `gemini-3.5-flash-lite` when it is busy | Not `gemini-3.8-flash`: 20 requests a day and often busy |
| A "nothing happened" deadline answer is left out | There is no moment of news for anyone to know early |
| A date ladder, or markets settled by the same news, count once | One story never fills two places |
| At least 3 a day (topped up with the closest of the rest), at most 5 | The user asked for at least 3; "none on most days" was rejected |
| A market that was on an Open now card is checked and shown whatever its verdict | The open list's picks get their answer |
| A day counted before it ended is counted again by the next run | A failed run never leaves a day half done |
| Whale analysis waits for a second stage (`watchlist/deep.py`) | Run only on the markets people pick |

## Open now (`watchlist/opennow.py`)

| Decision | Why |
|---|---|
| Pick open markets from this week's news, before any betting | Insider betting comes in the final hours, so the list has to predict it |
| Pool: open events with $10,000+ traded, not due within 12 hours | Volume only builds the pool; it never ranks a card |
| Never padded: fewer than 3 cards when fewer qualify | Every card shown has met the rules |
| The 99¢ check runs once, when an event first enters the pool | 99¢+ is only arbitrage, and a later jump to 99¢ may be the very case |
| Odds and betting-site headlines are removed before the model reads | No price reaches a model |
| Events one piece of news would settle share a story | One piece of news never fills two cards |
| Five places: 1-3 new steps (last 3 days), 4 gone quiet, 5 an answer due within 3 days | Fresh news leads; a quiet story and a dated reveal still get a place |
| At most 2 cards with the same decider | One decider never fills the list |
| A card that enters with a new step stays at least 3 lists | The list stays steady day to day |
| Steps are read by the free Gemma `gemma-4-31b-it` | Its own allowance, separate from Gemini's |
| A story with a step is read again with its headlines reversed; a step counts when both readings mark it | A step must not depend on the order the model saw |
| A headline's verdict is stored the first time it is read | Nothing flips overnight by chance |
| `gemini-3.5-flash-lite` names stories and checks, twice, whether the answer is already out | `gemini-3-flash-preview`'s small allowance stays with the closed list |
| A person approves each card's step (`STEPS_BY = "person"`) | The step reading missed the 8-in-10 bar out of sample (table below) |
| A dated card needs no step | Its date is the reason to watch it |
| `STEPS_BY = "model"` only after a fresh blind grade of 30 marked steps passes | `--consistency DIR` builds that grading sheet |

How the step reading was tested (2026-11-13 and 14):

| Test | Readings agree (bar 90%) | Steps that were real (bar 8 in 10) |
|---|---|---|
| flash-lite, 155 stories | 86% | about 3 in 10 |
| Gemma, the 63 stories the prompt was tuned on | 63 of 63 | 95% |
| Gemma, new stories (this row decides) | — | 23 of 37 (62%) |

## Data lives in D1, not git

| Decision | Why |
|---|---|
| Everything the job keeps is in the D1 database, through the Worker | No `watchlist-data` branch and no bot commits: the job never writes to git |
| The lists and added markets are built into the site at deploy time | The site keeps working when the API is down; only the visitor controls wait |
| While the API is down, the deploy uses the live site's copies | A deploy never publishes empty lists |
| Saves are gzip JSON in parts, with the pointer moved last | A reader never sees half a save |
| The last 3 saves of the state and of each list are kept | One bad save can be undone on its own (`watchlist.blobs restore`) |
| `watchlist.state push` refuses when the saved state changed since the pull | Two runs never overwrite each other |
| At most 200 added markets | The 500 MB database keeps room for the job |
| "Add to main page" adds directly; one site-wide watchlist; no per-visitor state | The site is effectively single-user |
| An added market never changes `markets.json` | The committed markets win a name clash |
| Every visitor write needs a Turnstile check, then fits daily limits; a pause switch stops them | No accounts, so the bot check and the limits are the guard |
| Visitors are counted by a salted hash of their IP address | Daily limits work without storing anyone's address |

## The daily job (`.github/workflows/watchlist.yml`)

| Decision | Why |
|---|---|
| One full run a day, at about 04:17 UTC | Off the hour: GitHub delays on-the-hour schedules |
| No hourly run; "apply" runs only when asked (review page or Actions) | Once a day is enough for the lists |
| Nothing assumes the run starts on time | GitHub often starts it 8 to 9 hours late; the lists count the UTC day the run is in |
| Budgets: closed list 30 min, open list 180 (120 in a catch-up); the job 355 of GitHub's 360 | A slow model can't time a step out |
| Readings are saved as they come; what a run can't read carries to the catch-up run, then the next day | A busy model means fewer new cards, never a broken run |
| A catch-up run at about 10:17 UTC reads only what Open now left unread; it deploys only when that changes the list | A busy model costs hours, not a day's cards; with nothing left it ends in seconds |
| After 3 busy answers in a row, a request's next try waits 5–10 minutes instead of 20–45 s (the closed list: once a run) | A rush on the free models passes in minutes; quick retries only used up the budget |
| A model that is down: the day keeps the last list's still-live cards and adds none | Yesterday's list beats an empty one |
| A second run on the same day replaces the list only when it finishes | A run made while a model is down never wipes a good list |
| A run fails only for what a person must fix (`watchlist/problems.py`) | Outages are handled and reported; a red run means act |
| `scripts/watchlist-mac.sh` runs the same job on a Mac | For days Google News or Polymarket refuse GitHub's runner |
| The review page warns after 40 hours without a report | GitHub turns schedules off after 60 days with no repository activity |

## Free-tier limits

| Service | Limit that shapes the design |
|---|---|
| `gemini-3.5-flash-lite` | 15 requests a minute: 4.2 s between request starts |
| `gemini-3-flash-preview` | A small daily allowance: the closed list only |
| `gemini-3.8-flash` | 20 requests a day: last fallback for labels and news queries, never for verdicts |
| `gemma-4-31b-it` | Its own allowance (no cap met in about 540 requests); 20 to 90 s an answer, often busy |
| Google News RSS | Free with no key; about 100 headlines a search |
| GitHub Actions | 360 minutes a job; schedules off after 60 idle days |
| Cloudflare D1 | 500 MB a database; Time Travel restores up to 7 days back |

## Tried and rejected

| Tried | Why it went |
|---|---|
| Scout, wallet dossiers and a request system (2026-11-08) | Unexplained scores and model "evidence" written as if events had happened; all deleted |
| Ranking open markets by the record of similar ended ones (`watchlist/open_scan.py`, `watchlist/backtest.py`) | Replaced by the news-step list; the demo modules stay outside the daily job |
| "Could a small group know?" as a filter of its own | Yes 95% of the time, so it filtered nothing |
| Long-shot or price-level rules | "Making 40% at 70¢ is still insider trading" |
| A 95¢ "already decided" cutoff | Markets at 4-5¢ for weeks before a war must stay in |
| An open list built on scheduled releases (2026-11-13) | Those are under lock-up, and its evidence was the closed list's own verdicts |
