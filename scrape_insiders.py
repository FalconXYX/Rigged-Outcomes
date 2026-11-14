#!/usr/bin/env python3
"""
Score the whales of one market (data/<Output>_clean.csv -> _insiders.csv).

Whales (whale_selection) = BUY positions on the winning outcome of at least
clamp(0.1% of volume, $1 000, $5 000), minus near-certain buys (average
price >= 0.99, see NEAR_CERTAIN_ODDS), plus any highlightedUsers of the
markets.json entry. A SELL row of the winning outcome is not a winning bet:
cleandata folds a SELL into the same wallet's complement BUY when there is
one, so a SELL row left over is either the exit of a BUY that is already
counted, or (shares minted, then sold) a bet on the OTHER outcome. Those rows
were scored as winning whales until November 2026 (about a third of every
market's whale set).

Prior history comes from Data API v2 (/v2/activity?user=…&type=TRADE&end=<first
trade>) through polymarket_api.fetch_user_history_up_to; the v1 endpoint this
script used until November 2026 was retired (HTTP 410). The v1 fetcher also
returned whatever it had on a rate limit or an error, which scored busy
wallets as "0 prior trades / 100 % concentrated / suspicious"; the v2 fetcher
raises instead, so a failed fetch aborts the run rather than inventing a
new account.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import polymarket_api

MARKETS_FILE = "markets.json"
WORKERS = 10

# Configuration Constants
FIRST_X_TRADES = 10
PERCENT_VOLUME = 80.0
# A position bought at an average of 99 cents or more can win at most ~1%:
# that is collecting a payout that is already all but certain (typically
# after the outcome is public, while the market waits to close), not a bet
# that needs private information. Such "resolution sweeps" are not whales.
# Small markets showed why: in one, every whale above the threshold was a
# Yes BUY at 0.999 placed seconds before the close, 12 days after the event.
NEAR_CERTAIN_ODDS = 0.99


def near_certain(df):
    """Boolean mask: positions whose average price is >= NEAR_CERTAIN_ODDS
    (resolution sweeps). Used by whale_selection (and meant for export.py)
    so every whale count on the site leaves them out the same way."""
    if "avg_odds" not in df.columns:
        return pd.Series(False, index=df.index)
    return pd.to_numeric(df["avg_odds"], errors="coerce").fillna(0) >= NEAR_CERTAIN_ODDS


def whale_selection(df, threshold):
    """(whales, sweeps, exits): boolean masks over a clean frame that carries
    is_winner. whales = winning-outcome BUY positions >= threshold below
    NEAR_CERTAIN_ODDS; sweeps = the near-certain ones left out; exits = the
    winning-outcome SELL rows >= threshold left out (see the module doc).
    export.py should select its timeline whales with this too."""
    big_winner = df["is_winner"].astype(bool) & (pd.to_numeric(df["dollar_amount"], errors="coerce") >= threshold)
    if "side" in df.columns:
        buys = df["side"].astype(str).str.strip().str.upper() == "BUY"
    else:
        buys = pd.Series(True, index=df.index)
    sweeps = big_winner & buys & near_certain(df)
    exits = big_winner & ~buys
    return big_winner & buys & ~sweeps, sweeps, exits


def whale_threshold(total_volume):
    """clamp(0.1% of the market's volume, $1 000, $5 000)."""
    threshold = min(5000.0, 0.001 * float(total_volume))
    return 1000.0 if threshold < 1000 else threshold

# _insiders.csv schema (export.py reads every one of these; the header is
# written even when a market has no whale, so a small market never breaks
# the site build).
INSIDER_COLS = [
    "user_id", "market_position", "lifetime_portfolio", "prior_bet_count",
    "portfolio_concentration", "is_first_x", "Is_percent", "is_suspicious",
    "target_trade_time", "market_title",
]

def fetch_user_history_up_to(user_address, cutoff_ts):
    """The wallet's TRADE rows at or before cutoff_ts (inclusive), v1-shaped
    (usdcSize, transactionHash, …). Capped at polymarket_api.HISTORY_MAX_PAGES
    x 1 000 trades closest before the cutoff (announced when it bites).
    Raises on an API error; never returns a partial history silently."""
    return polymarket_api.fetch_user_history_up_to(user_address, cutoff_ts)

def wallet_for(row):
    """The address whose history is scored: the clean row's eoa_address when
    it holds one, else the proxy wallet (user_id). cleandata.py writes an
    eoa_address column even when the raw CSV had none, and pandas reads
    those empty cells as NaN; the v1 scraper silently sent "nan" to the API
    and scored the whale as a new account, the v2 client refuses it."""
    eoa = row.get("eoa_address") if hasattr(row, "get") else None
    if isinstance(eoa, str) and eoa.strip():
        return eoa.strip().lower()
    return str(row["user_id"]).strip().lower()

def analyze_whale(row):
    user_id = wallet_for(row)

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

def select_market(markets, keyword=None, name=None):
    """The markets.json entry to score. name= is an exact match on the entry's
    "name" (the --name option, so "fired" can never pick "fired-2");
    keyword= keeps the original first-substring match on name or
    slug; neither picks the first entry."""
    if name is not None:
        matches = [m for m in markets if str(m.get("name", "")) == str(name)]
        if not matches:
            raise ValueError(f"No market named '{name}' in {MARKETS_FILE}.")
        return matches[0]
    if keyword:
        keyword = keyword.lower()
        matches = [m for m in markets
                   if keyword in m["name"].lower() or keyword in m["slug"].lower()]
        if not matches:
            raise ValueError(f"No market matched '{keyword}'.")
        return matches[0]
    return markets[0]


def main(keyword=None, name=None):
    with open(MARKETS_FILE) as f:
        ALL_MARKETS = json.load(f)

    m = select_market(ALL_MARKETS, keyword=keyword, name=name)

    clean_csv = f"data/{m['output']}_clean.csv"
    out_csv = f"data/{m['output']}_insiders.csv"

    if not os.path.exists(clean_csv):
        print(f"Clean data missing: {clean_csv}")
        sys.exit(1)

    score_market(m, clean_csv, out_csv)


def score_market(m, clean_csv, out_csv):
    """Score the whales of market entry `m` (a markets.json dict) from
    clean_csv into out_csv. Paths are explicit, so a caller working on
    another checkout never depends on the cwd.
    The file is written whole (temp file + rename), so an interrupted run
    never leaves a truncated _insiders.csv that looks finished."""
    if not os.path.exists(clean_csv):
        raise FileNotFoundError(f"Clean data missing: {clean_csv}")

    print(f"\nAnalyzing whales for: {m['name']}")

    df = pd.read_csv(clean_csv)

    market_title = df["market_title"].iloc[0] if "market_title" in df.columns else m["name"]
    highlighted = m.get("highlightedUsers", {})

    total_vol  = df["dollar_amount"].sum()
    threshold  = whale_threshold(total_vol)

    correct_outcome = m.get("correct_outcome")
    check_correct_outcome(df, correct_outcome, m["name"])
    if correct_outcome and "outcome" in df.columns:
        winner_mask = df["outcome"].astype(str).str.lower() == correct_outcome.lower()
    else:
        winner_mask = df["win_status"] == "WIN"

    df["is_winner"] = winner_mask
    whale_mask, sweeps, exits = whale_selection(df, threshold)

    hi_mask = df["user_id"].isin(highlighted)  # hand-picked wallets are always scored

    whales = df[whale_mask | hi_mask].copy()

    if sweeps.any():
        print(f"  left out {int(sweeps.sum())} near-certain position(s) (average price >= {NEAR_CERTAIN_ODDS:.2f}, "
              f"${df.loc[sweeps, 'dollar_amount'].sum():,.0f}): resolution sweeps, not informed bets")
    if exits.any():
        print(f"  left out {int(exits.sum())} SELL row(s) of the winning outcome (${df.loc[exits, 'dollar_amount'].sum():,.0f}): "
              f"exits or bets on the other side, not winning bets")
    print(f"Found {len(whales)} whales matching criteria. Fetching historical API data...")

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(analyze_whale, row): row for _, row in whales.iterrows()}
        for future in as_completed(futures):
            row = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                # Never score a whale whose history could not be fetched (that
                # is how the v1 run manufactured "new account" rows); abort
                # the market and say which wallet failed.
                pool.shutdown(wait=False, cancel_futures=True)
                raise RuntimeError(f"prior history of {wallet_for(row)} (first trade {row['first_trade']}) "
                                   f"could not be fetched; no _insiders.csv written for {m['name']}: "
                                   f"{type(e).__name__}: {e}") from e
            done += 1
            print(f"Processed {done}/{len(whales)} whales", end="\r", flush=True)

    print("\nSaving results...")
    if results:
        out_df = pd.DataFrame(results)
        out_df["market_title"] = market_title
    else:
        # A market with no winning position above the threshold (a small
        # market): write the full header so export.py
        # and the site treat it as "no whales" instead of crashing on a
        # file with a lone market_title column.
        print(f"  ! no whale met the ${threshold:,.0f} threshold on the {correct_outcome or 'winning'} side "
              f"(below {NEAR_CERTAIN_ODDS:.2f}); writing the header only")
        out_df = pd.DataFrame(columns=INSIDER_COLS)
    out_df = out_df[INSIDER_COLS]

    tmp_csv = f"{out_csv}.tmp"
    out_df.to_csv(tmp_csv, index=False)
    os.replace(tmp_csv, out_csv)
    print(f"Saved {len(out_df)} whale row(s) to {out_csv}")
    return out_df


def check_correct_outcome(df, correct_outcome, name):
    """markets.json's correct_outcome must agree with the clean file's own
    win_status (set from Gamma's resolution when the raw CSV was built).
    superbowl was configured as "No" while the market resolved Yes, so every
    whale scored was on the losing side; this turns that into an error."""
    if not correct_outcome or "outcome" not in df.columns or "win_status" not in df.columns:
        return
    status = df["win_status"].astype(str).str.upper()
    winners = {str(o).lower() for o in df.loc[status == "WIN", "outcome"].unique()}
    if not winners:
        return  # unresolved rows: nothing to compare against
    if correct_outcome.lower() not in winners:
        losers = sorted({str(o) for o in df.loc[status == "LOSS", "outcome"].unique()})
        raise ValueError(f"markets.json says correct_outcome={correct_outcome!r} for {name!r}, but the clean file's "
                         f"win_status marks {sorted(winners)} as WIN and {losers} as LOSS; fix markets.json "
                         f"before scoring (the whales would be picked from the losing side)")

if __name__ == "__main__":
    # python3 scrape_insiders.py iran          first market whose name/slug contains "iran"
    # python3 scrape_insiders.py --name fired   exactly the entry named "fired"
    if len(sys.argv) > 2 and sys.argv[1] == "--name":
        main(name=sys.argv[2])
    else:
        kw = sys.argv[1] if len(sys.argv) > 1 else None
        main(keyword=kw)
