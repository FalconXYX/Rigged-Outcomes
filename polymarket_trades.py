#!/usr/bin/env python3
"""
Fetch Polymarket trade data in two steps:
  1. Subgraph  -> collect every unique wallet address that touched the market
  2. Data API  -> fetch each wallet's actual activity (accurate, no routing artifacts)

This produces numbers that match Polymarket's own UI exactly.
"""
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API    = "https://gamma-api.polymarket.com"
SUBGRAPH_URL = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"
DATA_API     = "https://data-api.polymarket.com"

SLUG       = "us-strikes-iran-by-february-28-2026-227-967-547-688-589-491-592-418-452-924-384-915-464-672-196-157-993-596-269-535-381-391-471-256-988-997-296-225-762-973-292-827-345-182-558-215-794-879-189-761"
OUTPUT_CSV = "data/USxIranStrikesFeb28.csv"

WORKERS   = 10    # concurrent Data API requests
RETRY     = 3     # retries per failed request
PAGE_SIZE = 500   # Data API page size (max 500)

# Known CLOB operator/router addresses — infrastructure, not traders
OPERATORS = {
    "0x4bfb41d5b357570702fe9f19e29c4a26df9a28d6",
}


# ── Step 1: subgraph — addresses only ─────────────────────────────────────────

def get_market(slug):
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Market not found: {slug}")
    return results[0]


def _paginate_addresses(token_id, asset_field):
    """Minimal subgraph query — only fetches maker/taker addresses, no amounts."""
    addresses = set()
    last_id = ""
    while True:
        query = ('{ orderFilledEvents(first: 1000, where: {%s: "%s", id_gt: "%s"}) '
                 '{ id maker taker } }') % (asset_field, token_id, last_id)
        resp = requests.post(SUBGRAPH_URL, json={"query": query}, timeout=30)
        resp.raise_for_status()
        events = resp.json().get("data", {}).get("orderFilledEvents", [])
        if not events:
            break
        for e in events:
            if e.get("maker"):
                addresses.add(e["maker"].lower())
            if e.get("taker"):
                addresses.add(e["taker"].lower())
        last_id = events[-1]["id"]
        if len(events) < 1000:
            break
    return addresses


def get_all_addresses(market):
    """Collect every unique wallet that participated in this market."""
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcomes  = json.loads(market.get("outcomes", "[]"))
    addresses = set()
    for i, token_id in enumerate(token_ids):
        name = outcomes[i] if i < len(outcomes) else f"token {i}"
        print(f"  Scanning '{name}' token...", flush=True)
        for field in ("makerAssetId", "takerAssetId"):
            addresses |= _paginate_addresses(token_id, field)
    addresses -= OPERATORS
    return addresses


# ── Step 2: Data API — per-user activity ──────────────────────────────────────

def _fetch_user_activity(address, condition_id):
    """Fetch all TRADE rows for one user in this market."""
    all_rows, offset = [], 0
    for attempt in range(RETRY):
        try:
            while True:
                resp = requests.get(
                    f"{DATA_API}/activity",
                    params={"user": address, "limit": PAGE_SIZE, "offset": offset},
                    timeout=15,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    break
                resp.raise_for_status()
                page = resp.json()
                for row in page:
                    if (row.get("conditionId", "").lower() == condition_id.lower()
                            and row.get("type") == "TRADE"):
                        all_rows.append(row)
                if len(page) < PAGE_SIZE:
                    return all_rows
                offset += PAGE_SIZE
            break
        except Exception:
            if attempt == RETRY - 1:
                return all_rows
            time.sleep(1)
    return all_rows


def get_win_status(market, outcome_index):
    if not market.get("closed"):
        return "UNRESOLVED"
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        return "WIN" if prices[int(outcome_index)] == "1" else "LOSS"
    except (IndexError, ValueError, TypeError):
        return "UNRESOLVED"


def format_row(activity, market):
    ts = int(activity["timestamp"])
    return {
        "user_id":          activity["proxyWallet"],
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

def main(slug=SLUG, output=OUTPUT_CSV):
    market       = get_market(slug)
    condition_id = market["conditionId"]
    print(f"Market : {market.get('question')}")
    print(f"Status : {'Resolved' if market.get('closed') else 'Open'}")

    # Step 1
    print("\nStep 1: Collecting wallet addresses from subgraph...")
    addresses = get_all_addresses(market)
    print(f"Found {len(addresses):,} unique wallets\n")

    # Step 2
    print(f"Step 2: Fetching activity from Data API ({WORKERS} workers)...")
    all_trades, done, total = [], 0, len(addresses)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_fetch_user_activity, addr, condition_id): addr
                   for addr in addresses}
        for future in as_completed(futures):
            rows = future.result()
            for row in rows:
                all_trades.append(format_row(row, market))
            done += 1
            if done % 1000 == 0 or done == total:
                print(f"  {done:,}/{total:,} wallets  |  {len(all_trades):,} trades", flush=True)

    all_trades.sort(key=lambda r: r["timestamp"])
    print(f"\nTotal trades: {len(all_trades):,}")
    if output:
        save_csv(all_trades, output)
    return all_trades


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug",   default=SLUG)
    ap.add_argument("--output", default=OUTPUT_CSV)
    args = ap.parse_args()
    main(slug=args.slug, output=args.output)
