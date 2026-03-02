import csv
import json
import os
from datetime import datetime, timezone

import requests

GAMMA_API    = "https://gamma-api.polymarket.com"
SUBGRAPH_URL = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"

SLUG         = "us-strikes-iran-by-february-28-2026-227-967-547-688-589-491-592-418-452-924-384-915-464-672-196-157-993-596-269-535-381-391-471-256-988-997-296-225-762-973-292-827-345-182-558-215-794-879-189-761"  # market slug from the Polymarket URL
CONDITION_ID = None   # use this instead of SLUG if you have the 0x condition ID
OUTPUT_CSV   = "data/USxIranStrikesFeb28.csv"  # set to a filename like "data/trades.csv" to save, or None to print
# ─────────────────────────────────────────────────────────────────────────────

DISPLAY_FIELDS = ["timestamp", "user_id", "outcome", "side", "odds", "contracts", "dollar_amount", "win_status"]


def get_market(slug=None, asset_id=None):
    params = {"slug": slug} if slug else {"clob_token_ids": asset_id}
    resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError("Market not found.")
    return results[0]


def fetch_subgraph_fills(token_id):
    """Fetch ALL order fill events for a token ID from the on-chain subgraph (no offset limit)."""
    all_events = []
    last_id = ""
    while True:
        query = """
        {
            orderFilledEvents(
                first: 1000,
                where: {makerAssetId: "%s", id_gt: "%s"}
            ) { id timestamp maker taker makerAmountFilled takerAmountFilled fee transactionHash }
        }
        """ % (token_id, last_id)
        resp = requests.post(SUBGRAPH_URL, json={"query": query}, timeout=30)
        resp.raise_for_status()
        events = resp.json().get("data", {}).get("orderFilledEvents", [])
        if not events:
            break
        all_events.extend(events)
        last_id = events[-1]["id"]
        print(f"  {len(all_events)} on-chain fills fetched...", flush=True)
        if len(events) < 1000:
            break
    return all_events


def fetch_all_trades(market):
    """Fetch trades for both Yes and No tokens, then combine and sort by time."""
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcomes  = json.loads(market.get("outcomes", "[]"))

    all_trades = []
    for i, token_id in enumerate(token_ids):
        outcome_name = outcomes[i] if i < len(outcomes) else f"Outcome {i}"
        print(f"\nFetching fills for '{outcome_name}' token...")
        fills = fetch_subgraph_fills(token_id)
        for f in fills:
            all_trades.append({**f, "outcome": outcome_name, "outcomeIndex": i})

    all_trades.sort(key=lambda t: int(t["timestamp"]))
    return all_trades


def get_win_status(market, outcome_index):
    if not market.get("closed"):
        return "UNRESOLVED"
    prices = json.loads(market.get("outcomePrices", "[]"))
    return "WIN" if prices[outcome_index] == "1" else "LOSS"


def format_trade(fill, market):
    # On-chain amounts are in 6-decimal USDC. makerAmountFilled = tokens sold, takerAmountFilled = USDC paid.
    tokens_raw = int(fill["makerAmountFilled"])
    usdc_raw   = int(fill["takerAmountFilled"])
    contracts  = tokens_raw / 1_000_000
    dollar_amt = usdc_raw / 1_000_000
    odds       = round(dollar_amt / contracts, 4) if contracts > 0 else 0
    ts         = int(fill["timestamp"])

    return {
        "user_id":          fill.get("taker", ""),
        "market_id":        market.get("conditionId", ""),
        "market_title":     market.get("question", ""),
        "outcome":          fill.get("outcome", ""),
        "side":             "BUY",
        "odds":             odds,
        "contracts":        round(contracts, 4),
        "dollar_amount":    round(dollar_amt, 4),
        "win_status":       get_win_status(market, fill.get("outcomeIndex", 0)),
        "timestamp":        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "transaction_hash": fill.get("transactionHash", ""),
    }


def print_trades(trades):
    w = 18
    header = " | ".join(f"{f:>{w}}" for f in DISPLAY_FIELDS)
    print("\n" + header)
    print("─" * len(header))
    for t in trades:
        print(" | ".join(f"{str(t.get(f, ''))[:w]:>{w}}" for f in DISPLAY_FIELDS))


def save_csv(trades, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
    print(f"Saved {len(trades)} trades → {path}")


def main(slug=None, condition_id=None, output=None):
    if slug:
        market = get_market(slug=slug)
    elif condition_id:
        market = get_market(slug=None, asset_id=None)
        raise ValueError("For subgraph mode, please use --slug. Condition ID lookup not supported yet.")
    else:
        raise ValueError("Provide a SLUG.")

    condition_id = market["conditionId"]

    raw_trades = fetch_all_trades(market)
    if not raw_trades:
        print("No trades found.")
        return []

    print(f"\nMarket : {market.get('question')}")
    print(f"Status : {'Resolved' if market.get('closed') else 'Open'}")
    print(f"Trades : {len(raw_trades)}\n")

    trades = [format_trade(t, market) for t in raw_trades]

    if output:
        save_csv(trades, output)
    else:
        print_trades(trades[:25])
        if len(trades) > 25:
            print(f"\n...and {len(trades) - 25} more. Use --output file.csv to save all.")
    return trades


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug",   default=SLUG)
    ap.add_argument("--output", default=OUTPUT_CSV)
    args = ap.parse_args()
    main(slug=args.slug, output=args.output)
