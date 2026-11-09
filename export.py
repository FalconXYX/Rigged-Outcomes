#!/usr/bin/env python3
"""
Export the analysis behind VisualizeBetTiming.py and VisualizeInsiders.py as
static JSON for the web dashboard (web/). Every calculation here mirrors the
Dash apps line for line so the numbers on the site match the Python output.

    python3 export.py            # writes web/public/data/

Outputs
    data/index.json              one entry per market (+ ranking fields)
    data/markets/<name>.json     timeline + traders for one market

Timestamps are exported as epoch seconds of the US/Eastern wall clock read as
if it were UTC (the Dash apps plot naive Eastern times). The frontend formats
them with UTC formatters so every viewer sees Eastern time.
"""
import json
import math
import os
import time
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

MARKETS_FILE = "markets.json"
OUT_DIR = os.path.join("web", "public", "data")

EASTERN = ZoneInfo("America/New_York")
EPOCH = pd.Timestamp("1970-01-01")

# Must match VisualizeInsiders.py / scrape_insiders.py
PERCENT_VOLUME = 80.0
FIRST_X_TRADES = 10
ODDS_ROLLING_WINDOW = 12

# Values are exported at the CSVs' own precision (4 dp). Rounding to cents
# here made the site round twice (and sums of ~34k cent-rounded rows drift
# across a whole-dollar boundary), so tiles and hovers differed from Dash by $1.
DP = 4


def to_secs(series):
    """Naive (Eastern wall clock) datetimes -> integer epoch seconds."""
    return ((series - EPOCH) // pd.Timedelta(seconds=1)).astype("int64")


def ts_secs(ts):
    return int((ts - EPOCH) // pd.Timedelta(seconds=1))


def r(x, nd=DP):
    """Round for JSON; NaN/inf -> None."""
    if x is None:
        return None
    x = float(x)
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def js_fixed(x, digits):
    """What JS `x.toFixed(digits)` shows (Plotly's d3-format and the site's
    formatters): the exact binary value, ties away from zero."""
    return Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def rs(x, shown, nd=DP):
    """Round like r(), unless that would change the value as displayed with
    `shown` decimals; then keep full precision. Derived floats such as
    0.4015 * 100 = 40.150000000000006 show as 40.2 in Dash, but 40.15 shows
    as 40.1."""
    v = r(x, nd)
    if v is None or js_fixed(v, shown) == js_fixed(float(x), shown):
        return v
    return float(x)


def parse_market_time(s):
    return pd.to_datetime(s, format="%Y-%m-%d-%H:%M")


# ── Timeline (VisualizeBetTiming.py) ─────────────────────────────────────────

def get_net_profit(row):
    stake = float(row["dollar_amount"])
    if "contracts" in row and pd.notnull(row["contracts"]):
        payout = float(row["contracts"])
    else:
        implied_odds = float(row["avg_odds"])
        if implied_odds > 1:
            implied_odds = implied_odds / 100.0
        if implied_odds <= 0.001:
            implied_odds = 0.001
        payout = stake / implied_odds
    return max(0.0, payout - stake)


def prepare_market(m):
    """Load one market's clean CSV and compute everything the timeline and the
    ranking fields share: the whale threshold, the
    winner mask, the whale set (after the IQR time-outlier filter, with the
    per-whale profit) and the crowd. Mirrors VisualizeBetTiming.py."""
    df = pd.read_csv(f"data/{m['output']}_clean.csv")
    # Raw ISO strings: the join key back to _insiders.csv (target_trade_time).
    df["first_trade_raw"] = df["first_trade"].astype(str)
    for col in ("first_trade", "last_trade"):
        df[col] = (pd.to_datetime(df[col], utc=True)
                     .dt.tz_convert(EASTERN)
                     .dt.tz_localize(None))
    market_title = df["market_title"].iloc[0] if "market_title" in df.columns else m["name"]
    highlighted = m.get("highlightedUsers", {})

    # Whale / crowd split
    total_vol = df["dollar_amount"].sum()
    threshold = min(5000.0, 0.001 * total_vol)
    if threshold < 1000:
        threshold = 1000.0

    correct_outcome = m.get("correct_outcome")
    if correct_outcome and "outcome" in df.columns:
        winner_mask = df["outcome"].astype(str).str.lower() == correct_outcome.lower()
        # The clean file carries Gamma's resolution as win_status; a
        # correct_outcome that contradicts it would put the losing side on
        # the site (superbowl was configured that way once). scrape_insiders
        # refuses to score such a market; the export only warns.
        if "win_status" in df.columns:
            won = {str(o).lower() for o in df.loc[df["win_status"].astype(str).str.upper() == "WIN", "outcome"].unique()}
            if won and correct_outcome.lower() not in won:
                print(f"  ! {m['name']}: markets.json correct_outcome={correct_outcome!r} but the clean file marks "
                      f"{sorted(won)} as WIN; the whales below are on the losing side")
    else:
        winner_mask = df["win_status"] == "WIN"
    df["is_winner"] = winner_mask
    whale_mask = df["is_winner"] & (df["dollar_amount"] >= threshold)

    hi_mask = df["user_id"].isin(highlighted)
    raw_whales = df[whale_mask | hi_mask].copy()
    crowd = df[~(whale_mask | hi_mask)].copy()

    if len(raw_whales) >= 4:
        ts = raw_whales["first_trade"].astype("int64") // 10**9
        q1, q3 = ts.quantile(0.25), ts.quantile(0.75)
        whales = raw_whales[ts >= q1 - 1.5 * (q3 - q1)].copy()
    else:
        whales = raw_whales.copy()

    whales["display_dollar"] = whales.apply(
        lambda row: float(row["contracts"]) * float(row["avg_odds"])
        if row["user_id"] in highlighted and float(row["dollar_amount"]) == 0
        else float(row["dollar_amount"]),
        axis=1,
    ) if len(whales) else pd.Series(dtype=float)
    whales["win_amount"] = whales.apply(
        lambda row: get_net_profit(row) if row.get("is_winner", True) else 0.0,
        axis=1,
    ) if len(whales) else pd.Series(dtype=float)

    df_winners = df[df["is_winner"]]
    winner_profit = df_winners.apply(get_net_profit, axis=1) if len(df_winners) else pd.Series(dtype=float)
    total_market_winnings = float(winner_profit.sum())

    return SimpleNamespace(
        m=m,
        df=df,
        market_title=market_title,
        highlighted=highlighted,
        total_vol=total_vol,
        threshold=threshold,
        whales=whales,
        crowd=crowd,
        df_winners=df_winners,
        winner_profit=winner_profit,
        total_market_winnings=total_market_winnings,
    )


def build_timeline(prep):
    m, df = prep.m, prep.df
    market_title, highlighted = prep.market_title, prep.highlighted

    # Odds line: "No" trades flipped so everything is event probability
    odds_t, odds_pct = [], []
    if "avg_odds" in df.columns:
        p = pd.to_numeric(df["avg_odds"], errors="coerce")
        if p.max(skipna=True) and p.max(skipna=True) > 1.0:
            p = p / 100.0
        p = p.clip(lower=0.0, upper=1.0)
        if "outcome" in df.columns:
            out = df["outcome"].astype(str).str.strip().str.lower()
            is_no = out.eq("no") | out.str.startswith("no ")
            p = pd.Series(np.where(is_no, 1.0 - p, p), index=df.index)
        tmp = pd.DataFrame({
            "odds_time": df["first_trade"].dt.floor("min"),
            "odds_pct": p * 100.0,
        }).dropna(subset=["odds_time", "odds_pct"])
        if len(tmp):
            odds_line = (tmp.groupby("odds_time")["odds_pct"].median()
                            .reset_index().sort_values("odds_time"))
            odds_line["odds_pct"] = (odds_line["odds_pct"]
                                     .rolling(window=ODDS_ROLLING_WINDOW, min_periods=1, center=True)
                                     .median())
            odds_t = to_secs(odds_line["odds_time"]).tolist()
            odds_pct = [rs(v, 1) for v in odds_line["odds_pct"]]

    total_vol, threshold = prep.total_vol, prep.threshold
    whales, crowd = prep.whales, prep.crowd
    df_winners, winner_profit = prep.df_winners, prep.winner_profit
    total_market_winnings = prep.total_market_winnings

    crowd["hour"] = crowd["first_trade"].dt.floor("h")
    hourly = crowd.groupby("hour")["dollar_amount"].sum().reset_index(name="vol")
    hourly["avg"] = hourly["vol"].rolling(6, min_periods=1, center=True).mean()

    events = []
    for k, v in m.get("importantDates", {}).items():
        try:
            events.append({"t": ts_secs(parse_market_time(k)), "label": v})
        except Exception:
            pass
    events.sort(key=lambda e: e["t"])

    time_ranges = []
    for label, spec in m.get("timeRanges", {}).items():
        try:
            time_ranges.append({
                "label": label,
                "start": ts_secs(parse_market_time(spec["start"])),
                "end": ts_secs(parse_market_time(spec["end"])),
            })
        except Exception:
            continue
    labels = [tr["label"] for tr in time_ranges]
    default_range = "Total" if "Total" in labels else (labels[0] if labels else None)

    whale_rows = []
    if len(whales):
        wt = to_secs(whales["first_trade"])
        for (_, row), t in zip(whales.iterrows(), wt):
            uid = row["user_id"]
            whale_rows.append({
                "id": uid,
                "label": highlighted.get(uid),
                "t": int(t),
                "dollar": rs(row["dollar_amount"], 0),
                "display": rs(row["display_dollar"], 0),
                "odds": r(row["avg_odds"]),
                "profit": rs(row["win_amount"], 0),
                "contracts": rs(row["contracts"], 0),
                "outcome": str(row["outcome"]),
                "hedged": bool(uid in highlighted and float(row["dollar_amount"]) == 0),
            })

    return market_title, {
        "threshold": rs(threshold, 0),
        "totalVolume": rs(total_vol, 0),
        "totalMarketWinnings": rs(total_market_winnings, 0),
        "whales": whale_rows,
        "crowdHourly": {
            "t": to_secs(hourly["hour"]).tolist(),
            "vol": [rs(v, 0) for v in hourly["vol"]],
            "avg": [rs(v, 0) for v in hourly["avg"]],
        },
        "odds": {"t": odds_t, "pct": odds_pct},
        "events": events,
        "timeRanges": time_ranges,
        "defaultRange": default_range,
        "crowdRows": {
            "t": to_secs(crowd["first_trade"]).tolist(),
            "d": [r(v) for v in crowd["dollar_amount"]],
        },
        "winnerRows": {
            "t": to_secs(df_winners["first_trade"]).tolist(),
            "p": [r(v) for v in winner_profit],
        },
        "span": {
            "start": ts_secs(df["first_trade"].min()),
            "end": ts_secs(df["first_trade"].max()),
        },
        "positions": int(len(df)),
    }


# ── Traders (VisualizeInsiders.py) ───────────────────────────────────────────

CATEGORY_KEYS = ["normal", "newAccount", "concentration", "critical"]
CATEGORY_RANK = {k: i for i, k in enumerate(CATEGORY_KEYS)}


def categorize(row):
    if row["is_first_x"] and row["Is_percent"]:
        return "critical"
    if row["is_first_x"]:
        return "newAccount"
    if row["Is_percent"]:
        return "concentration"
    return "normal"


def load_insiders(m):
    """One row per whale position scored by scrape_insiders.py (a wallet can
    have several rows in one market). None when the file holds no whale
    rows (a small market scores nobody: the file is a header only, or, from
    older runs, a lone market_title column), so the market exports without
    a traders panel instead of failing the build."""
    path = f"data/{m['output']}_insiders.csv"
    df = pd.read_csv(path)
    if "user_id" not in df.columns or df.empty:
        print(f"  ! {path}: no whale rows; exporting {m['name']} without traders")
        return None
    df["category"] = df.apply(categorize, axis=1)
    return df


def build_traders(df):
    df = df.copy()

    # calculate_network_metrics — same seeded jitter as the Dash app
    vol_factor = (df["portfolio_concentration"] / 100.0) * 50
    history_factor = np.maximum(0, (10 - df["prior_bet_count"]) / 10.0) * 50
    df["risk_score"] = vol_factor + history_factor
    np.random.seed(42)
    base_radius = 110 - df["risk_score"]
    df["radius"] = base_radius + np.random.uniform(-4, 4, len(df))
    df["theta_rad"] = df["user_id"].apply(lambda x: int(x[2:8], 16) % 360) * (np.pi / 180)
    df["x"] = df["radius"] * np.cos(df["theta_rad"])
    df["y"] = df["radius"] * np.sin(df["theta_rad"])

    total_vol = df["market_position"].sum()
    suspicious_df = df[df["is_suspicious"] == True]  # noqa: E712 (matches Dash app)
    suspicious_vol = suspicious_df["market_position"].sum()
    pct_suspicious = (suspicious_vol / total_vol * 100) if total_vol > 0 else 0

    traders = []
    for _, row in df.iterrows():
        traders.append({
            "id": row["user_id"],
            "position": rs(row["market_position"], 0),
            "lifetime": rs(row["lifetime_portfolio"], 0),
            "priorBets": int(row["prior_bet_count"]),
            "concentration": rs(row["portfolio_concentration"], 1),
            "isFirstX": bool(row["is_first_x"]),
            "isPercent": bool(row["Is_percent"]),
            "suspicious": bool(row["is_suspicious"]),
            "category": row["category"],
            "riskScore": r(row["risk_score"], 3),
            "x": r(row["x"], 3),
            "y": r(row["y"], 3),
        })

    title = None
    if "market_title" in df.columns and pd.notnull(df["market_title"].iloc[0]):
        title = str(df["market_title"].iloc[0])

    return title, {
        "totalVolume": rs(total_vol, 0),
        "suspiciousVolume": rs(suspicious_vol, 0),
        "pctSuspicious": rs(pct_suspicious, 1),
        "totalWhales": int(len(df)),
        "flaggedAccounts": int(len(suspicious_df)),
        "thresholds": {"firstX": FIRST_X_TRADES, "percentVolume": PERCENT_VOLUME},
        "traders": traders,
    }


# ── Home-page card preview (downsampled; display only) ───────────────────────

PREVIEW_ODDS_BINS = 160
PREVIEW_CROWD_BINS = 72
PREVIEW_WHALES = 48


def _bin_index(t, start, end, n):
    if end <= start:
        return 0
    return min(n - 1, max(0, int((t - start) / (end - start) * n)))


def build_preview(timeline, traders):
    start, end = timeline["span"]["start"], timeline["span"]["end"]

    # Odds: median per equal-width time bin
    bins = [[] for _ in range(PREVIEW_ODDS_BINS)]
    for t, p in zip(timeline["odds"]["t"], timeline["odds"]["pct"]):
        if p is not None:
            bins[_bin_index(t, start, end, PREVIEW_ODDS_BINS)].append((t, p))
    odds = []
    for b in bins:
        if b:
            ts = [x[0] for x in b]
            odds.append([int(np.median(ts)), r(float(np.median([x[1] for x in b])), 1)])

    # Crowd: hourly volume summed per bin
    crowd = [0.0] * PREVIEW_CROWD_BINS
    for t, v in zip(timeline["crowdHourly"]["t"], timeline["crowdHourly"]["vol"]):
        crowd[_bin_index(t, start, end, PREVIEW_CROWD_BINS)] += v or 0.0
    width = (end - start) / PREVIEW_CROWD_BINS if end > start else 0
    crowd = [[int(start + i * width), r(v, 0)] for i, v in enumerate(crowd)]

    # Whales: the biggest positions plus every highlighted user
    whales = sorted(timeline["whales"], key=lambda w: w["display"] or 0, reverse=True)
    keep = whales[:PREVIEW_WHALES] + [w for w in whales[PREVIEW_WHALES:] if w["label"]]
    keep.sort(key=lambda w: w["display"] or 0)  # small first, big drawn on top
    whale_pts = [{"t": w["t"], "v": w["display"], "hi": bool(w["label"])} for w in keep]

    flag_mix = None
    if traders:
        flag_mix = {k: 0.0 for k in CATEGORY_KEYS}
        for tr in traders["traders"]:
            flag_mix[tr["category"]] += tr["position"] or 0.0
        flag_mix = {k: r(v) for k, v in flag_mix.items()}

    return {"odds": odds, "crowd": crowd, "whales": whale_pts, "flagMix": flag_mix}


# ── Ranking fields (home page "how suspicious does it look") ─────────────────

def _lower_ids(series):
    return series.astype(str).str.lower()


def build_ranking(prep, insiders):
    """Per-market suspicion summary for index.json."""
    named = len(prep.highlighted)
    if insiders is None:
        return {
            "flaggedWinnings": None,
            "flaggedWinningsPct": None,
            "flaggedWhaleCapitalPct": None,
            "namedWallets": named,
        }

    flagged_ids = set(_lower_ids(insiders.loc[insiders["is_suspicious"] == True, "user_id"]))  # noqa: E712

    # Net profit of the timeline whales (post-IQR, the dots on the chart) that
    # belong to a flagged wallet — the same per-whale `profit` the site shows.
    w = prep.whales
    if len(w):
        flagged_win = float(w.loc[_lower_ids(w["user_id"]).isin(flagged_ids), "win_amount"].sum())
    else:
        flagged_win = 0.0
    total_win = prep.total_market_winnings
    pct = (flagged_win / total_win * 100.0) if total_win > 0 else None

    total_vol = insiders["market_position"].sum()
    susp_vol = insiders.loc[insiders["is_suspicious"] == True, "market_position"].sum()  # noqa: E712
    pct_suspicious = (susp_vol / total_vol * 100) if total_vol > 0 else 0

    return {
        "flaggedWinnings": rs(flagged_win, 0),
        "flaggedWinningsPct": rs(pct, 1) if pct is not None else None,
        "flaggedWhaleCapitalPct": rs(pct_suspicious, 1),
        "namedWallets": named,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    t0 = time.perf_counter()
    with open(MARKETS_FILE) as f:
        markets = json.load(f)

    os.makedirs(os.path.join(OUT_DIR, "markets"), exist_ok=True)

    preps, insiders_by_market = {}, {}
    for m in markets:
        clean_csv = f"data/{m['output']}_clean.csv"
        if not os.path.exists(clean_csv):
            print(f"  skip {m['name']}: {clean_csv} missing")
            continue
        preps[m["name"]] = prepare_market(m)
        if os.path.exists(f"data/{m['output']}_insiders.csv"):
            insiders = load_insiders(m)
            if insiders is not None:
                insiders_by_market[m["name"]] = insiders

    index = []
    for name, prep in preps.items():
        m = prep.m
        title, timeline = build_timeline(prep)
        traders = None
        insiders = insiders_by_market.get(name)
        if insiders is not None:
            t_title, traders = build_traders(insiders)
            title = title or t_title

        payload = {
            "name": m["name"],
            "title": str(title),
            "slug": m["slug"],
            "correctOutcome": m.get("correct_outcome"),
            "timeline": timeline,
            "traders": traders,
        }
        path = os.path.join(OUT_DIR, "markets", f"{m['name']}.json")
        with open(path, "w") as f:
            json.dump(payload, f, separators=(",", ":"), allow_nan=False)

        index.append({
            "name": m["name"],
            "title": str(title),
            "slug": m["slug"],
            "correctOutcome": m.get("correct_outcome"),
            "start": timeline["span"]["start"],
            "end": timeline["span"]["end"],
            "totalVolume": timeline["totalVolume"],
            "positions": timeline["positions"],
            # Timeline whales (after VisualizeBetTiming's IQR time-outlier filter).
            "whaleCount": len(timeline["whales"]),
            # Whales scored in VisualizeInsiders (no IQR filter): the set that
            # flaggedAccounts and pctSuspicious are computed over.
            "traderWhales": traders["totalWhales"] if traders else None,
            "flaggedAccounts": traders["flaggedAccounts"] if traders else None,
            "pctSuspicious": traders["pctSuspicious"] if traders else None,
            "events": timeline["events"],
            "preview": build_preview(timeline, traders),
            **build_ranking(prep, insiders),
        })
        print(f"  ✓ {m['name']:<10} {os.path.getsize(path) / 1024:>7.0f} KB  {title}")

    with open(os.path.join(OUT_DIR, "index.json"), "w") as f:
        json.dump({"markets": index}, f, separators=(",", ":"), allow_nan=False)
    print(f"Exported {len(index)} market(s) to {OUT_DIR}/")

    print(f"Done in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
