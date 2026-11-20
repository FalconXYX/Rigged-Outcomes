"""
The wallet rubric: a 0-100 insider score for every whale, decided by Gemini
from a rubric that lives in one file (rubric/wallet_rubric.toml).

Guide: docs/WALLET_RUBRIC.md. Modules, in the order data flows:

  spec.py          load_rubric(path) -> dict; check(rubric) -> [problems];
                   display(rubric) -> the part the site renders (labels,
                   bands, colours, questions, card factors)
  wallet_facts.py  FACTORS: every factor function by name (@factor), the
                   market's context and the wallet histories they read;
                   market_facts() -> a facts document
  fact_sources.py  a market's inputs (the original-project branch, the
                   site's record or its API) and the facts files' CLI
  prompt.py        render_input(rubric, market, facts) -> the text read
  scorers.py       "decisions" (Decisions API) and "interactions" (regular
                   Gemini), one interface; get_scorer(rubric)
  combine.py       spectrum(rubric, answers) -> (score 0-100, band id)
  cache.py         answers keyed by (wallet, question hash, input hash);
                   the day's request ledger
  pipeline.py      score_market(); trader_fields() / page_part(): what a
                   market page carries
  store.py         the rubric:<name> blob; publish_page()
  steps.py         the wallets.score step (docs/BACKEND.md)

CLI:  python3 -m backend.rubric check | score | show | compare | pages | publish
"""

DEFAULT_PATH = "rubric/wallet_rubric.toml"
