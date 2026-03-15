#!/usr/bin/env python3
import json
import os
import sys
import time
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

DATA_API = "https://data-api.polymarket.com"
MARKETS_FILE = "markets.json"
WORKERS = 10
PAGE_SIZE = 500

# Configuration Constants
FIRST_X_TRADES = 10
PERCENT_VOLUME = 80.0

def fetch_user_history_up_to(user_address, cutoff_ts):
    all_rows = []
    offset = 0
    
    while True:
        page = []
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{DATA_API}/activity",
                    params={"user": user_address, "limit": PAGE_SIZE, "offset": offset},
                    timeout=15,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                page = resp.json()
                break
            except Exception:
                if attempt == 2:
                    return all_rows
                time.sleep(1)
                
        if not page:
            break
            
        for row in page:
            if row.get("type") == "TRADE":
                ts = int(row.get("timestamp", 0))
                if ts <= cutoff_ts:
                    all_rows.append(row)
                    
        if len(page) < PAGE_SIZE:
            break
            
        offset += PAGE_SIZE
        
        if offset >= 15000:
            break

    return all_rows

def analyze_whale(row):
    # Uses the EOA address you added to the clean file, or defaults to the proxy wallet
    user_id = row.get("eoa_address", row["user_id"])
    
    target_pos = float(row["dollar_amount"])
    
    # Extract the temporal element for the CSV
    trade_time = str(row["first_trade"])
    
    cutoff_dt = pd.to_datetime(row["first_trade"])
    cutoff_ts = int(cutoff_dt.timestamp())
    
    history = fetch_user_history_up_to(user_id, cutoff_ts)
    
    api_historical_stake = 0.0
    unique_txs = set()
    
    for h in history:
        api_historical_stake += float(h.get("usdcSize", 0))
        unique_txs.add(h.get("transactionHash", ""))
        
    prior_bet_count = len(unique_txs)
    
    lifetime_portfolio = max(api_historical_stake, target_pos)
    
    is_first_x = prior_bet_count <= FIRST_X_TRADES
    is_percent = False
    
    concentration = (target_pos / lifetime_portfolio) * 100 if lifetime_portfolio > 0 else 100.0
    
    if concentration >= PERCENT_VOLUME:
        is_percent = True
        
    return {
        "user_id": row["user_id"], 
        "market_position": target_pos,
        "lifetime_portfolio": lifetime_portfolio,
        "prior_bet_count": prior_bet_count,
        "portfolio_concentration": round(concentration, 2),
        "is_first_x": is_first_x,
        "Is_percent": is_percent,
        "is_suspicious": is_first_x or is_percent,
        "target_trade_time": trade_time
    }

def main(keyword=None):
    with open(MARKETS_FILE) as f:
        ALL_MARKETS = json.load(f)
    
    if keyword:
        keyword = keyword.lower()
        matches = [m for m in ALL_MARKETS
                   if keyword in m["name"].lower() or keyword in m["slug"].lower()]
        if not matches:
            raise ValueError(f"No market matched '{keyword}'.")
        m = matches[0]
    else:
        m = ALL_MARKETS[0]

    clean_csv = f"data/{m['output']}_clean.csv"
    out_csv = f"data/{m['output']}_insiders.csv"
    
    if not os.path.exists(clean_csv):
        print(f"Clean data missing: {clean_csv}")
        sys.exit(1)
        
    print(f"\nAnalyzing whales for: {m['name']}")
    
    df = pd.read_csv(clean_csv)
                         
    market_title = df["market_title"].iloc[0] if "market_title" in df.columns else m["name"]
    highlighted = m.get("highlightedUsers", {})

    total_vol  = df["dollar_amount"].sum()
    threshold  = min(5000.0, 0.001 * total_vol)
    if threshold < 1000:
        threshold = 1000.0
        
    correct_outcome = m.get("correct_outcome")
    if correct_outcome and "outcome" in df.columns:
        winner_mask = df["outcome"].astype(str).str.lower() == correct_outcome.lower()
    else:
        winner_mask = df["win_status"] == "WIN"
        
    df["is_winner"] = winner_mask
    whale_mask = df["is_winner"] & (df["dollar_amount"] >= threshold)

    hi_mask = df["user_id"].isin(highlighted)
    
    whales = df[whale_mask | hi_mask].copy()

    print(f"Found {len(whales)} whales matching criteria. Fetching historical API data...")
    
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(analyze_whale, row): row for _, row in whales.iterrows()}
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            print(f"Processed {done}/{len(whales)} whales", end="\r", flush=True)
            
    print("\nSaving results...")
    out_df = pd.DataFrame(results)
    
    out_df["market_title"] = market_title
        
    out_df.to_csv(out_csv, index=False)
    print(f"Saved to {out_csv}")

if __name__ == "__main__":
    kw = sys.argv[1] if len(sys.argv) > 1 else None
    main(keyword=kw)