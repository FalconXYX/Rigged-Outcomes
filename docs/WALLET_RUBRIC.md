# Wallet rubric

Every whale gets an **insider score from 0 to 100** and a band, decided by Gemini from a rubric that lives in **one file**: [`rubric/wallet_rubric.toml`](../rubric/wallet_rubric.toml). The old yes/no flags (≤10 prior trades, ≥80% concentration) are still computed next to it for comparison.

---

## How a score is made

1. **Facts:** `backend/rubric/wallet_facts.py` computes every registered factor for every whale (Polymarket's free Data API, history read as of the whale's first buy).
2. **Input:** the rubric's `[input]` template turns the market and one whale's chosen factors into a short text.
3. **Questions:** the scorer answers the rubric's questions about that text: each answer is a probability for every level.
4. **Score:** `[combine]` turns the answers into 0–100; `[[bands]]` give it a label and a colour.

### `[combine]`

- **`method = "weighted_mean"`:** the weighted mean of the counted answers (`weights`) × 100.
- **`method = "tiered"`** (in use): that weighted mean is every wallet's **base**. A `[[combine.tiers]]` block lifts the wallets that match all its conditions into `floor`–`ceiling`:
  - **conditions:** `flags` (today's yes/no flags: `is_first_x`, `is_percent`, `category`, ...) and `facts` (any registered factor); a value must be equal, `{ min, max }` holds both ends; an unknown fact never matches;
  - **where in the range:** the tier's own `weights` (else `[combine] weights`), stretched so `span = [low, high]` covers the range;
  - the **first** matching tier applies, and a wallet keeps its base when that is higher.

| Tier (today's file) | Who | Range |
|---|---|---|
| `both_old_markers` | both old flags, with `prior_trades` ≤ 10 and `concentration_pct` ≥ 40 in the facts | 60–100 (`span = [0.25, 0.80]`): Suspicious at least; timing, conviction and the crowd decide Highly suspicious |
| `new_account` | ≤10 prior trades (flag and facts) | 40–90 |
| `concentration` | ≥80% flag, with `concentration_pct` ≥ 40 | 40–90 |
| none | everyone else | the base |

The fact checks keep out flags made from an old, cut-short history (wallets with hundreds or thousands of trades, or whose bet is a sliver of their money). The span's top of 0.80 keeps middling evidence (a lead of weeks, ordinary odds, a position sold before the reveal) in Suspicious, so only strong timing reaches Highly suspicious. The scores file keeps each whale's `tier`, `base` and `evidence`; `compare` counts the tiers.

---

## Editing the rubric

Edit `rubric/wallet_rubric.toml`, bump `version`, then:

```bash
python3 -m backend.rubric check                  # every problem, one per line
python3 -m backend.rubric score --all --dry-run  # how many requests the change costs
```

| To change | Edit | Costs |
|---|---|---|
| A question's wording, level criteria or options | its `[[questions]]` block | that question, for every wallet |
| A level's `label` (what the site shows) | its `[[questions]]` block | nothing |
| Which factors the model reads, or their wording | `[[factors]]` (`send`, `label`, `description`) | every question, for every wallet |
| The input text | `[input]` | every question, for every wallet |
| Weights, tiers (conditions, floor, ceiling, span) | `[combine]`, `[[combine.tiers]]` | nothing (cached answers re-combined) |
| Bands: cut-offs, labels, colours, `flagged` | `[[bands]]` | nothing |
| What the wallet card shows | `card = true` on a factor | nothing |
| The version alone | `version` | nothing (every result shows it) |
| Scoring after "Add to main page" | `[backend] score_on_add` | nothing |

- **Question types:** `score` (2–10 ordered levels, lowest first), `noul` (yes/no: `true_description` and `false_description`), `choice` (2–255 options).
- **Weight 0:** shown on the card, not counted (the `profile` choice, the `single_purpose` yes/no).
- **A counted choice:** give every option a `value` from 0 to 1 and the question a weight.
- **Typos:** `check` names any key it does not know (`lable`, `[[question]]`).
- **Colours** are `--c-*` tokens from `web/src/styles/tokens.css`; `check` refuses any other. Notable uses `--c-notable` (a quiet slate), so it reads apart from Unusual's `--c-new` in both themes.
- **Fact definitions in descriptions:** a factor's `description` may cite `{big_move_points}`, `{reveal_points}`, `{rise_hours}`, `{fresh_trades}`, `{fresh_days}` or `{cluster_hours}`; the model and the card read the number from `wallet_facts.DEFINITIONS`, so the text cannot drift from the computation (`check` names an unknown one). These numbers define the facts: changing one in `wallet_facts.py` means rebuilding the facts, and every whale is asked again.
- **Question ids** key the cache: rename one only when you mean a new question.
- **`[about]`** (`heading`, `text`): the paragraph at the foot of the Insider score tab (costs nothing). Its placeholders (`{questions}`, `{band_<id>}`, `{flagged_min}`, `{tier_<id>_band}`, `{tier_<id>_<fact>_max}`, ...; listed above `[about]`) are filled in from the file, so the words follow its bands, tiers and weights. `check` names an unknown one; the page then leaves the paragraph out, and scoring on the backend goes on. The text cites one tier for what all three share; `tests/test_rubric_scoring.py` fails if a tier edit parts them.

---

## Adding a factor

1. Write one function in `backend/rubric/wallet_facts.py`:
   ```python
   @factor("dormant_days", "history", "days", "Days since its last trade in another market")
   def _(w):
       ...  # w.row, w.hist, w.fills, w.m, w.peers; return a number, a bool or None
   ```
2. Add one `[[factors]]` block with that id to the rubric (`label`, `description`, `send`, `card`).
3. Rebuild the facts: `python3 -m backend.rubric.fact_sources --out DIR --cache DIR`.

19 more factors are already registered and unused (`prior_win_rate_pct`, `hedge_usd`, `buy_surge_ratio`, `max_rise_48h`, ...): using one is step 2 only. `hours_to_big_move` (hours from the first buy until the winning side's chance stood `big_move_points` (30) points above its level before the buy, or at the reveal level) is the "bought right before a massive odds move" signal. It is only negative for a buy made after the reveal.

---

## Scoring

```bash
python3 -m backend.rubric score --market usiran   # one market
python3 -m backend.rubric score --all             # every facts file
python3 -m backend.rubric score --all --max-requests 300   # at most 300 requests
python3 -m backend.rubric score --all --wallets FILE --out DIR   # a pilot: only the listed wallets (one per line)
python3 -m backend.rubric show                    # the rubric in one table
python3 -m backend.rubric show --market usiran [--wallet 0x...]   # whales by score; one wallet's text and answers
python3 -m backend.rubric compare --all           # against today's yes/no flag
```

- **Cache:** one answer per (wallet, question hash, input hash), in `data/rubric/cache/<market>.json` locally and the D1 blob `rubric:<market>` on the backend. Only what a change touched is asked again; `--refresh` asks everything again.
- **Version:** every result carries the rubric's `version`. A bump alone costs nothing: the cached answers are combined again under it.
- **Output:** `data/rubric/scores/<market>.json`: score, band, answers and facts per whale.
- **Daily cap:** `daily_cap` counts across the day's runs (`data/rubric/cache/usage.json`, Gemini's Pacific day).

| Scorer | What it is | Per request | Free tier |
|---|---|---|---|
| `decisions` (default) | Gemini Decisions API, `gemini-decision-1` | 1 wallet, every question it still needs (~1,600 tokens) | 10,000 a day, 200 a minute, 50,000 tokens a minute |
| `interactions` (fallback) | regular Gemini via `watchlist/ai.py`, strict JSON | 8 wallets | 15 a minute |

- **Spacing:** `rpm` and `tpm` in `[scorer.decisions]`, measured from each answer's token count.
- **Errors:** 429 and 5xx are retried with back-off. A refused key, a gone model, today's quota or `daily_cap` switch the rest of the run to the fallback. A 400 or a blocked input leaves only that wallet unscored.
- **Question formats (checked against the live API, 2026-11-20):** `score` sends `criteria` as a list; `noul` as `{true_description, false_description}`; `choice` as `{option id: text}`. One function holds them: `scorers.decisions_question`.

---

## On the backend

- **`wallets.score`** (`backend/rubric/steps.py`): `add.export` spawns it once a market is on the main page. It walks the market's fills, reads each whale's history, scores, then publishes the page again with the scores. Each phase checkpoints, so a cut-off call loses at most one whale.
- **When it runs:** where the step function's settings have `WALLET_SCORES=on` and `GEMINI_API_KEY` (Vercel), and `[backend] score_on_add = true`. A local stack or a test never spends the quota.
- **In the background:** a new add or the daily walk goes first; the step gives way at its next checkpoint and resumes from what it saved.
- **Going live:** deploy the Worker first (it must know the `wallets.score` kind and the `rubric:*` blobs), then the step function, then set `WALLET_SCORES=on`.
- **Busy model:** the step comes back in 5–10 minutes and asks only the whales still missing. Both scorers out for the day (today's quota or `daily_cap`): it comes back when the quota resets (`model_quota`; other adds go on meanwhile) and asks the rest. A refused key: it publishes what it has.
- **`rubric:<name>`** (D1): the answers, the facts and the page part. Every page `backend.site` builds again keeps the scores.
- **Backfill:** a `wallets.score` job with input `{"name": "<market>"}` re-scores one market. It reuses the saved facts, so a rubric change costs only the questions it touched. From a laptop: `python3 -m backend.rubric publish --market NAME` puts local scores on the live site.

---

## On the site

Each market page carries the rubric's display part and each trader's result, so a rubric change needs no web change. The index entry carries the band split (`spectrum`: name, version, bands with `count` and `stake`, `flaggedWinnings`):
```jsonc
"traders": {
  "rubric": { "version", "name", "scale", "bands": [{ "id", "label", "min", "max", "color", "flagged" }],
              "questions": [{ "id", "type", "label", "weight", "levels" | "options" }], "factors": [...],
              "about": { "heading", "text" } },
  "traders": [{ ...today's fields, "spectrum": {
      "score": 73.4, "band": "suspicious",
      "q": { "timing": { "p": [0.02, 0.05, 0.11, 0.40, 0.42], "level": 4 },
             "single_purpose": { "p": 0.71 }, "profile": { "choice": "early_informed", "p": 0.62 } },
      "facts": { "hours_before_reveal": 9.0, ... } } }]
}
```

The scores get their own tab on the market page, named after the rubric ("Insider score", after Timeline and Flagged traders; its count is the whales in flagged bands). The Flagged traders tab and the home page keep today's four cut-off categories, unchanged. The tab shows only when the market's data has a rubric and at least one scored whale; `#/m/<name>/score` on a market without one swaps to `/traders`.

- **Spectrum strip** (full width above the two charts): a 0–100 axis split into the bands; one dot per whale, sized by position, coloured by band. Hover and click link to the other charts.
- **Constellation:** radius = 110 − score; the guide rings sit at the band cut-offs, labelled as a numeric axis ("Score 0", 20, 40, 60; the outer two only on a chart under 520px) on one bearing kept clear of dots, callouts and "THE MARKET"; dots coloured by band. The two callouts name the top band (at the core) and the lowest band (the outer orbit) and point at the largest visible wallet of that band; a callout whose band shows no wallet is not drawn.
- **Capital concentration (treemap):** grouped by band, highest first.
- **Wallet card:** score `73 / 100` and band; one row per question with a stacked bar of its level probabilities and its most likely level ("Timing · Hours before · 42%"); "Reads as"; the `card` factors.
- **Stat tiles and lead sentence:** "flagged" = bands with `flagged = true`.
- **How the Insider score works** (the foot of the tab): the `[about]` paragraph.

---

## Code

`backend/rubric/`: `spec.py` (load, check, display) · `wallet_facts.py` (factors) · `fact_sources.py` (facts files) · `prompt.py` (input text) · `scorers.py` (both scorers) · `combine.py` (score, band) · `cache.py` (answers, daily ledger) · `pipeline.py` (`score_market`, `page_part`) · `store.py` (the D1 blob, `publish_page`) · `steps.py` (`wallets.score`) · `__main__.py` (CLI). `pipeline/export.py` attaches the page part (`build_traders(df, spectrum)`, `spectrum_summary`). Web, in `web/src/views/traders/`: `ScoreView.tsx` (the tab) · `ScoreConstellation.tsx` · `ScoreTreemap.tsx` · `ScoreCard.tsx` · `SpectrumStrip.tsx` · `spectrum.ts` (bands, layout, colours) · `groups.ts` (bands as chart groups) · `score.css`. The Flagged traders files are main's, untouched.

- **Tests:** `python3 tests/test_rubric_scoring.py` (a fake Decisions API on a local port), `python3 tests/test_rubric_step.py` (the step against the fake Worker), `python3 tests/test_rubric_wallet_facts.py`.
- **Local site data:** `python3 -m backend.rubric pages --to DIR` writes `DIR/api/data/` from the live API with the local scores attached; serve `DIR` and point `VITE_API_URL` at it.
