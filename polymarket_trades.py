#!/usr/bin/env python3
"""
Fetch every fill of one Polymarket market and save it as the pipeline's raw
trade CSV (data/<Output>.csv):

  1. Gamma        -> the market object for the slug (condition id, question,
                     outcomes, resolution)
  2. Data API v2  -> GET /v2/trades?condition=<id>&taker_only=false: every
                     fill in the market, maker AND taker rows, in one
                     cursor-paginated walk (polymarket_api.fetch_trades)
  3. to_trade_rows -> the v2 rows in the v1 shape format_row reads, oldest
                     first, then save_csv.

Until November 2026 this script crawled wallet addresses from the Goldsky
orderbook subgraph and then pulled each wallet's activity from Data API v1;
Polymarket retired both (the subgraph answers 429 "paused and deprecated",
v1 answers 410 "retired"). The row shape and the CSV schema are unchanged,
so cleandata.py, export.py and the site are untouched. A v2 dump holds both
counterparties of every fill, exactly like the old per-wallet crawl did, so
positions aggregate the same way; the old crawl was lossy (a wallet's walk
stopped early on rate limits or the v1 offset cap and silently kept the
newest rows only), so a re-fetch of an old market can hold MORE rows than
the committed CSV, never fewer (see tests/test_v2_parity.py).
"""
import csv
import json
import os
from datetime import datetime, timezone

import polymarket_api
from polymarket_api import GAMMA_API, OPERATORS  # noqa: F401

# ── Config ────────────────────────────────────────────────────────────────────
SLUG       = "us-strikes-iran-by-february-28-2026-227-967-547-688-589-491-592-418-452-924-384-915-464-672-196-157-993-596-269-535-381-391-471-256-988-997-296-225-762-973-292-827-345-182-558-215-794-879-189-761"
OUTPUT_CSV = "data/USxIranStrikesFeb28.csv"

PROGRESS_EVERY = 10   # print a progress line every N pages (1 000 rows each)


# ── Step 1: Gamma — the market ────────────────────────────────────────────────
def get_market(slug):
    """Gamma market object (open or closed) for a markets.json slug."""
    return polymarket_api.fetch_market(slug)


# ── Step 2: Data API v2 — every fill in the market ────────────────────────────
def fetch_market_trades(market, max_rows=None):
    """Raw-CSV rows (format_row) for every fill in `market`, oldest first.
    max_rows caps the fetch (newest rows kept; the cap is announced, never
    silent). Errors from the API raise."""
    condition_id = market["conditionId"]

    def progress(n_rows, pages):
        if pages % PROGRESS_EVERY == 0:
            print(f"  page {pages:,}  |  {n_rows:,} rows", flush=True)

    # polymarket_api announces a cap on stdout itself (one notice, not two).
    v2_rows, _capped = polymarket_api.fetch_trades(condition_id, max_rows=max_rows, on_progress=progress)
    return to_trade_rows(v2_rows, market)


def get_win_status(market, outcome_index):
    if not market.get("closed"):
        return "UNRESOLVED"
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        return "WIN" if prices[int(outcome_index)] == "1" else "LOSS"
    except (IndexError, ValueError, TypeError):
        return "UNRESOLVED"


def format_row(activity, market, eoa_address=""):
    ts = int(activity["timestamp"])
    return {
        "user_id":          activity["proxyWallet"],
        "eoa_address":      eoa_address,
        "side":             activity.get("side", ""),
        "market_id":        market.get("conditionId", ""),
        "market_title":     market.get("question", ""),
        "outcome":          activity.get("outcome", ""),
        "odds":             round(float(activity.get("price", 0)), 6),
        "contracts":        round(float(activity.get("size", 0)), 4),
        "dollar_amount":    round(float(activity.get("usdcSize", 0)), 4),
        "win_status":       get_win_status(market, activity.get("outcomeIndex", 0)),
        "timestamp":        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "transaction_hash": activity.get("transactionHash", ""),
    }


def to_trade_rows(v2_rows, market):
    """Raw-CSV rows (format_row) for v2 trade rows, oldest first. The subgraph
    crawl recorded the proxy wallet as eoa_address; v2 has no separate signer
    field, so the proxy wallet goes there too."""
    trades = [format_row(polymarket_api.as_v1(r), market, eoa_address=str(r.get("proxy_wallet") or "").lower())
              for r in v2_rows]
    trades.sort(key=lambda t: t["timestamp"])
    return trades


# ── Save ──────────────────────────────────────────────────────────────────────

def save_csv(trades, path):
    if not trades:
        print("No trades to save.")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
    print(f"Saved {len(trades):,} trades -> {path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main(slug=SLUG, output=OUTPUT_CSV, max_rows=None):
    market       = get_market(slug)
    condition_id = market["conditionId"]
    print(f"Market : {market.get('question')}")
    print(f"Status : {'Resolved' if market.get('closed') else 'Open'}")
    print(f"\nFetching every fill from Data API v2 ({polymarket_api.DATA_API_V2}/trades, condition {condition_id[:10]}…, maker + taker rows)...")
    all_trades = fetch_market_trades(market, max_rows=max_rows)
    wallets = {t["user_id"] for t in all_trades}
    print(f"Found {len(wallets):,} unique wallets")
    print(f"\nTotal trades: {len(all_trades):,}")
    if not all_trades:
        print("  ! the v2 feed served no fills for this market (an error would have raised)")
    if output:
        save_csv(all_trades, output)
    return all_trades


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug",   default=SLUG)
    ap.add_argument("--output", default=OUTPUT_CSV)
    ap.add_argument("--max-rows", type=int, default=None, help="cap the fetch at this many fills (newest kept; announced)")
    args = ap.parse_args()
    main(slug=args.slug, output=args.output, max_rows=args.max_rows)
