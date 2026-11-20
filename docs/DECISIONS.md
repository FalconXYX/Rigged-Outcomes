# Decisions

Why Rigged Outcomes is built the way it is: one row per decision that still holds. How the backend works is in [BACKEND.md](BACKEND.md); setup is in [DEPLOY.md](DEPLOY.md).

## The analysis

| Decision | Why |
|---|---|
| Every fetch uses Data API v2 (`polymarket_api.py`) | Polymarket retired Data API v1 and the subgraph in November 2026 |
| A failed fetch raises; it is never read as "no trades" | The old fetcher scored busy wallets as brand new |
| Buys at 99¢ or more are not whales | Paying 99¢ is collecting a payout, not an informed bet |
| `scrape_insiders.py` refuses a `correct_outcome` that contradicts `win_status` | A wrong outcome once picked superbowl's whales from the losing side |

## Both Watchlist lists

| Decision | Why |
|---|---|
| Models read market text and news headlines only: no prices, odds, trades or wallets | Price and whale ranking didn't work; wallet analysis is too heavy for a first pass |
| Everything is free: no paid keys, no billing | A hard requirement |
| The code fetches Google News RSS and hands Gemini the headlines, through the Interactions API | The free key has no web search, and `generateContent` returns 404 for it |
| Markets with $10,000+ traded; word rules run before any model call | Enough markets a day, and election day cost 920 calls before the rules |
| Left out: games, prices, weather, elections, public counts, central banks, official statistics | Insider knowledge can't explain them, or they are under legal lock-up |
| Targets: answers held by a small, loose group, often on any day | Firings, strikes, launches, rulings; the 7 featured markets all fit |
| The code checks every model answer against what it gave the model | Models read; fixed rules choose the cards |

## Recently closed

| Decision | Why |
|---|---|
| A market's day is the Toronto day it closed, not its end date | 44% of large markets close before their end date |
| Three questions: could a small group know, was it a surprise, was it kept quiet | The verdict is made to agree with the answers |
| `gemini-3-flash-preview`, with `gemini-3.5-flash-lite` when busy | `gemini-3.8-flash` allows only 20 requests a day |
| Exactly 3 a day, topped up with the closest of the rest | The user asked for 3 a day |
| A date ladder, or markets settled by the same news, count once | One story never fills two places |

## Open now

| Decision | Why |
|---|---|
| Pick open markets from this week's news, before any betting | Insider betting comes in the final hours, so the list has to predict it |
| Five places: 1–3 new steps, 4 gone quiet, 5 an answer due within 3 days | Fresh news leads; a quiet story and a dated reveal still get a place |
| One search, one step reading and one "already out" check per story, capped by the budget | Open now should cost little |
| Steps are read by `gemma-4-31b-it` | Its own free allowance, separate from Gemini's |
| The model decides the steps; no one approves a card | The user asked for no review step |
| Every card is re-checked each day; a released game, film, show, album or book counts as out | An answer already public must never show (GTA VI) |

## Data and the site

| Decision | Why |
|---|---|
| Everything the backend keeps is in D1, nothing in git | No data branch and no bot commits |
| D1 only, no R2 | R2 can bill past its free allowance; D1 Free never charges |
| Every market and both lists come from the Worker; the 7 original markets are saved there like added ones (`python3 -m backend.site originals`) | An add or a day's lists show with no site build, and the repository holds no data |
| Files are gzip in base64 text parts, with the pointer moved last | A reader never sees half a save; D1 returns a BLOB too slowly for 10 ms of CPU |
| The site reads with ETags, a market page by version, and keeps the last good copy | Polling costs almost nothing, and the site is never empty when the API is down |
| Visitor writes need Turnstile and fit daily limits, counted by a salted IP hash | No accounts, and no address is stored |

## The backend

| Decision | Why |
|---|---|
| A Cloudflare Worker queues and schedules; a Vercel function computes. The GitHub Actions job is gone | Free on both with no card; GitHub keeps only the site build |
| One every-minute cron that decides in Toronto time; the daily run from 04:00 | Daylight saving is handled in code; 04:00 is just after Gemini's quota resets |
| Each add is its own run | A second click is never lost |
| Steps are deadline-driven and checkpoint each item | A step never runs into Vercel's 300 s limit, and loses at most one item |
| One live step per lane, one heavy step at a time; only the main lane saves the state | Saves never race, and several calls can share one 2 GB instance |
| Wake-ups are signed, and a firewall rule denies the rest | Junk traffic never invokes the function |
| A 30-day ledger slows, then stops, work at 50%, 65% and 80% of the allowance | Going over pauses the Vercel project for 30 days |
| `scripts/watchlist-mac.sh` runs the daily lists on a Mac | For days Google News or Polymarket refuse Vercel |

## Tried and rejected

| Tried | Why it went |
|---|---|
| Scout, wallet dossiers and a request system | Unexplained scores and made-up "evidence"; all deleted |
| Ranking open markets by similar ended ones | Replaced by the news-step list |
| "Could a small group know?" as its own filter | Yes 95% of the time, so it filtered nothing |
| Long-shot or price-level rules | "Making 40% at 70¢ is still insider trading" |
| An open list built on scheduled releases | They are under lock-up |
