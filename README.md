# Rigged Outcomes

Detects potential insider trading on Polymarket prediction markets by analyzing who traded profitably before major outcomes occurred.

---

## Quick Start

**Install dependencies:**

```bash
pip install requests pandas plotly dash numpy
```

**Run full analysis on a market:**

```bash
python3 run.py marketName          # Analyze market
python3 run.py              # Run all markets in markets.json
```

**View results:**

```bash
# Interactive trader dashboard
python3 VisualizeInsiders.py marketName

# Timeline of trading activity
python3 VisualizeBetTiming.py marketName
```

Output CSVs go to `data/` folder.

---

## What It Does

1. **Fetches** all trades from Polymarket for a given market (via Subgraph + Data API)
2. **Cleans** data by aggregating trades per user/outcome
3. **Identifies** suspicious traders: those who concentrated bets in the final hours before outcome
4. **Visualizes** insider patterns in interactive dashboards

---

## Data Flow

`markets.json` → `run.py` →

- `polymarket_trades.py` (fetch raw trades) → `{MarketName}.csv`
- `cleandata.py` (aggregate) → `{MarketName}_clean.csv`
- `scrape_insiders.py` (detect patterns) → `{MarketName}_insiders.csv`

Output files:

- `{MarketName}.csv` — Raw individual trades
- `{MarketName}_clean.csv` — Aggregated positions per user
- `{MarketName}_insiders.csv` — Flagged suspicious traders

---

## markets.json Structure

Each market needs:

- `name`: Short identifier
- `slug`: Polymarket market ID
- `output`: CSV filename prefix
- `correct_outcome`: "Yes" or "No" (what actually happened)
- `timeRanges`: Define "Normal" vs "Leadup" periods for analysis
- `highlightedUsers`: Named traders to track

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

**Raw data (`.csv`):**

- `user_id`: Wallet address
- `outcome`: Yes or No
- `side`: BUY or SELL
- `contracts`: Volume
- `dollar_amount`: USD spent
- `odds`: Price per contract
- `timestamp`: Trade time

**Cleaned data (`_clean.csv`):**

- Same as above, but aggregated per user/outcome
- `avg_odds`: Average price paid
- `win_status`: WIN or LOSS
- `first_trade` / `last_trade`: Time range

**Insiders (`_insiders.csv`):**

- `insider_score`: 0-1, higher = more suspicious
- `P_volume`: % of trades in Leadup period
- `P_winning`: Win rate on this market

---

## Insider Detection Logic

Flags traders who:

1. Concentrated 80%+ of their volume in the "Leadup" period
2. Showed consistent profitability on correct outcome
3. Have high confidence (avg odds far from 0.5)
