#!/usr/bin/env python3
"""
Fetch full historical implied odds for a Polymarket market slug and save to CSV.

Output rows represent the market-implied probability that the event happens
(using the "Yes" token when available) across the market's full available history.
"""

import csv
import json
import os
from datetime import datetime, timezone

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

SLUG = "us-strikes-iran-by-february-28-2026-227-967-547-688-589-491-592-418-452-924-384-915-464-672-196-157-993-596-269-535-381-391-471-256-988-997-296-225-762-973-292-827-345-182-558-215-794-879-189-761"
OUTPUT_CSV = "data/USxIranStrikesFeb28_odds.csv"


def get_market(slug):
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=20)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise ValueError(f"Market not found for slug: {slug}")
    return rows[0]


def get_yes_token_id(market):
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcomes = json.loads(market.get("outcomes", "[]"))

    if not token_ids:
        raise ValueError("Market has no clobTokenIds.")

    for idx, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() == "yes" and idx < len(token_ids):
            return token_ids[idx], outcome

    # Fallback: first token if explicit Yes label is unavailable.
    return token_ids[0], outcomes[0] if outcomes else "UNKNOWN"


def fetch_price_history(token_id, fidelity=1):
    # interval=max gives the full available range from the CLOB API.
    resp = requests.get(
        f"{CLOB_API}/prices-history",
        params={"market": token_id, "interval": "max", "fidelity": fidelity},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("history", [])


def to_rows(history, market, slug, outcome_label):
    rows = []
    for point in history:
        ts = int(point.get("t", 0))
        prob = float(point.get("p", 0.0))
        rows.append(
            {
                "slug": slug,
                "market_id": market.get("conditionId", ""),
                "market_title": market.get("question", ""),
                "outcome": outcome_label,
                "timestamp_unix": ts,
                "timestamp_iso_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "odds_probability": round(prob, 6),
                "odds_percent": round(prob * 100.0, 4),
            }
        )
    return rows


def save_csv(rows, path):
    if not rows:
        print("No odds history rows to save.")
        return

    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows):,} rows -> {path}")


def resolve_market_from_keyword(keyword=None, markets_path="markets.json"):
    with open(markets_path, encoding="utf-8") as f:
        all_markets = json.load(f)

    if not all_markets:
        raise ValueError("markets.json is empty.")

    if not keyword:
        return all_markets[0]

    key = keyword.lower().strip()
    matches = [
        m
        for m in all_markets
        if key in str(m.get("name", "")).lower() or key in str(m.get("slug", "")).lower()
    ]
    if not matches:
        raise ValueError(f"No market matched keyword '{keyword}'.")
    return matches[0]


def default_output_path(market):
    base = market.get("output") or market.get("name") or "market"
    return os.path.join("data", f"{base}_odds.csv")


def main(slug=SLUG, output=OUTPUT_CSV, fidelity=1):
    market = get_market(slug)
    token_id, outcome_label = get_yes_token_id(market)

    print(f"Market : {market.get('question', slug)}")
    print(f"Outcome: {outcome_label}")
    print(f"Token  : {token_id}")

    history = fetch_price_history(token_id, fidelity=fidelity)
    rows = to_rows(history, market, slug, outcome_label)

    print(f"Points : {len(rows):,}")
    if output:
        save_csv(rows, output)
    return rows


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--keyword",
        help="Market keyword to filter (e.g. 'usiran'). If omitted, uses first market from markets.json.",
    )
    parser.add_argument("--output", help="CSV output path. Defaults to data/<market_output>_odds.csv")
    parser.add_argument(
        "--fidelity",
        default=1,
        type=int,
        help="CLOB history fidelity (1 = highest granularity)",
    )
    parser.add_argument(
        "--markets",
        default="markets.json",
        help="Path to markets.json file",
    )

    args = parser.parse_args()

    market_cfg = resolve_market_from_keyword(args.keyword, markets_path=args.markets)
    slug = market_cfg["slug"]
    output = args.output or default_output_path(market_cfg)

    main(slug=slug, output=output, fidelity=args.fidelity)
