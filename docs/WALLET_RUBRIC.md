# Wallet rubric

Every whale gets an **insider score from 0 to 100** and a band, decided by Gemini from a rubric that lives in **one file**: [`rubric/wallet_rubric.toml`](../rubric/wallet_rubric.toml). The old yes/no flags (≤10 prior trades, ≥80% concentration) are still computed next to it for comparison.

---

## How a score is made

1. **Facts:** `backend/rubric/wallet_facts.py` computes every registered factor for every whale (Polymarket's free Data API, history read as of the whale's first buy).
2. **Input:** the rubric's `[input]` template turns the market and one whale's chosen factors into a short text.
3. **Questions:** the scorer answers the rubric's questions about that text: each answer is a probability for every level.
4. **Score:** `[combine]` weights turn the answers into 0–100; `[[bands]]` give it a label and a colour.

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
| Weights | `[combine] weights` | nothing (cached answers re-combined) |
| Bands: cut-offs, labels, colours, `flagged` | `[[bands]]` | nothing |
| What the wallet card shows | `card = true` on a factor | nothing |
| The version alone | `version` | nothing (every result shows it) |
| Scoring after "Add to main page" | `[backend] score_on_add` | nothing |

- **Question types:** `score` (2–10 ordered levels, lowest first), `noul` (yes/no: `true_description` and `false_description`), `choice` (2–255 options).
- **Weight 0:** shown on the card, not counted (the `profile` choice, the `single_purpose` yes/no).
- **A counted choice:** give every option a `value` from 0 to 1 and the question a weight.
- **Typos:** `check` names any key it does not know (`lable`, `[[question]]`).
- **Colours** are `--c-*` tokens from `web/src/styles/tokens.css`; `check` refuses any other.
- **Question ids** key the cache: rename one only when you mean a new question.

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

18 more factors are already registered and unused (`prior_win_rate_pct`, `hedge_usd`, `buy_surge_ratio`, ...): using one is step 2 only.

---

## Scoring

```bash
python3 -m backend.rubric score --market usiran   # one market
python3 -m backend.rubric score --all             # every facts file
python3 -m backend.rubric score --all --max-requests 300   # at most 300 requests
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
- **Going live:** deploy the Worker first (it must know the `wallets.score` kind and the `rubric:*` blobs), then the step function, then set `WALLET_SCORES=on`.
- **Busy model:** the step comes back in 5–10 minutes and asks only the whales still missing. Both scorers out for the day: the step publishes what it has.
- **`rubric:<name>`** (D1): the answers, the facts and the page part. Every page `backend.site` builds again keeps the scores.
- **Backfill:** a `wallets.score` job with input `{"name": "<market>"}` re-scores one market. It reuses the saved facts, so a rubric change costs only the questions it touched. From a laptop: `python3 -m backend.rubric publish --market NAME` puts local scores on the live site.

---

## On the site

Each market page carries the rubric's display part and each trader's result, so a rubric change needs no web change. The index entry carries the band split (`spectrum`: name, version, bands with `count` and `stake`, `flaggedWinnings`):
```jsonc
"traders": {
  "rubric": { "version", "name", "scale", "bands": [{ "id", "label", "min", "max", "color", "flagged" }],
              "questions": [{ "id", "type", "label", "weight", "levels" | "options" }], "factors": [...] },
  "traders": [{ ...today's fields, "spectrum": {
      "score": 73.4, "band": "suspicious",
      "q": { "timing": { "p": [0.02, 0.05, 0.11, 0.40, 0.42], "level": 4 },
             "single_purpose": { "p": 0.71 }, "profile": { "choice": "early_informed", "p": 0.62 } },
      "facts": { "hours_before_reveal": 9.0, ... } } }]
}
```

- **Spectrum strip** (new, full width above the two charts): a 0–100 axis split into the bands; one dot per whale, sized by position, coloured by band. Hover and click link to the other charts.
- **Constellation:** radius = 110 − score; the guide rings sit at the band cut-offs, labelled with band names; dots coloured by band.
- **Capital concentration (treemap):** grouped by band, highest first.
- **Wallet card:** score `73 / 100` and band; one row per question with a stacked bar of its level probabilities and its most likely level ("Timing · Hours before · 42%"); "Reads as"; the `card` factors.
- **Stat tiles and lead sentence:** "flagged" = bands with `flagged = true`.
- **Cut-offs view:** a two-way switch beside the legend (Insider score / Cut-offs) brings back today's four categories.

---

## Code

`backend/rubric/`: `spec.py` (load, check, display) · `wallet_facts.py` (factors) · `fact_sources.py` (facts files) · `prompt.py` (input text) · `scorers.py` (both scorers) · `combine.py` (score, band) · `cache.py` (answers, daily ledger) · `pipeline.py` (`score_market`, `page_part`) · `store.py` (the D1 blob, `publish_page`) · `steps.py` (`wallets.score`) · `__main__.py` (CLI). `pipeline/export.py` attaches the page part (`build_traders(df, spectrum)`, `spectrum_summary`). Web: `web/src/views/traders/spectrum.ts` (bands, layout, colours).

- **Tests:** `python3 tests/test_rubric_scoring.py` (a fake Decisions API on a local port), `python3 tests/test_rubric_step.py` (the step against the fake Worker), `python3 tests/test_rubric_wallet_facts.py`.
- **Local site data:** `python3 -m backend.rubric pages --to DIR` writes `DIR/api/data/` from the live API with the local scores attached; serve `DIR` and point `VITE_API_URL` at it.
