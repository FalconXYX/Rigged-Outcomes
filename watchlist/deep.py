"""
The deeper look at one closed market, for later: download its trades and
find the whales who bet on the winner before the answer went public. Moved
out of the closed list (watchlist.upsets) on 2026-11-12, when the user made
that list a light first filter with no trade or wallet data; this is the
second stage they want to come back to.

    analyze(market, conn, label=None) -> a dict, or {"dropped": reason}

1. When the answer went public (the reveal): the hour after the last hour
   whose median winning-side price (fills >= $10) was below 90¢; the last
   trade when the price never got there.
2. Was it in doubt? In the 14 days before the reveal the winner sat at or
   below 70¢ for at least 6 hours (a market that traded in fewer hours uses
   all of them). Under 50¢ is an upset, 50-70¢ close to a toss-up.
3. Who bet on the winner. Per wallet, its buys of the winning side inside
   those 14 days add into one position (split orders count once); its sells
   of the winning side before the reveal take away from it. A whale has at
   least scrape_insiders.whale_threshold(volume) in it ($1k-$5k, 0.1% of the
   money traded) at an average price under 98¢.
4. The score: what those whales made as a share of all money traded.
5. The user's flags on those whales (exact prior history up to the wallet's
   first trade in the market: <= 10 trades, or this position >= 80% of its
   trading).
6. Weak points: the winner was the favourite the whole time, the whales
   made under 5% of the money traded, flagged wallets took under 10% of the
   profit.

What a web check of 68 such markets found (2026-11-12): no market that
looked like a real case had its winner above 40¢ before the news; profit
share alone did not separate real cases from public news; the flags hit
almost nothing.
"""
import os
import statistics
from collections import defaultdict

import polymarket_api
import polymarket_trades
import scrape_insiders

from watchlist import filters, method
from watchlist.common import DAY, iso, to_epoch

HOUR = 3600
WINDOW_DAYS = 14          # how far back before the reveal a bet still counts
REVEAL_PRICE = 0.90       # the winner at or above this: the market knows
DOUBT_PRICE = 0.70        # the winner at or below this: still in doubt
UNDERDOG_PRICE = 0.50
SUSTAIN_HOURS = 6         # hours the winner must sit at a price for it to count
SURE_THING = 0.98         # whale positions averaging this or more are left out
DUST_USD = 10.0           # fills under this don't set the price
MIN_FLAGGED_SHARE = 0.10  # flagged wallets' share of the profit; under it the market is weaker
MIN_SCORE = 0.05          # whales' profit / money traded; under it the market is weaker
MAX_ROWS = method.MAX_ROWS
PER_DAY = 5


# ── pure parts (tested offline) ──────────────────────────────────────────────

def winner_prices(rows, winner, min_usd=DUST_USD):
    """[(ts, price of the winning side)] from raw-CSV rows: a fill on the
    other outcome at p is the winner at 1 - p (every market is binary)."""
    w = str(winner).strip().lower()
    out = []
    for r in rows:
        try:
            usd, p = float(r.get("dollar_amount") or 0), float(r.get("odds"))
        except (TypeError, ValueError):
            continue
        ts = to_epoch(r.get("timestamp"))
        if ts is None or usd < min_usd:
            continue
        out.append((ts, p if str(r.get("outcome", "")).strip().lower() == w else 1.0 - p))
    out.sort()
    return out


def hourly(points):
    """{hour_ts: median price} over (ts, price) points."""
    by = defaultdict(list)
    for ts, p in points:
        by[ts - ts % HOUR].append(p)
    return {h: statistics.median(v) for h, v in sorted(by.items())}


def reveal_time(hours, last_ts):
    """When the answer went public: the hour after the last hour whose median
    was below REVEAL_PRICE. When even the last hour was below it (trading
    stopped before the market knew), the last trade. None without data."""
    if not hours:
        return None
    below = [h for h, p in hours.items() if p < REVEAL_PRICE]
    if not below:
        return min(hours)
    last_below = max(below)
    if last_below == max(hours):
        return int(last_ts)
    return last_below + HOUR


def sustained_low(hours, start, end, sustain=SUSTAIN_HOURS):
    """(price, hours): the price the winner sat at or below for at least
    `sustain` hours in [start, end), the sustain-th lowest hourly median. A
    window that traded in fewer hours uses all of them (its highest hour).
    (None, 0) when it did not trade at all."""
    vals = sorted(p for h, p in hours.items() if start <= h < end)
    if not vals:
        return None, 0
    n = min(sustain, len(vals))
    return vals[n - 1], n


def price_before(points, reveal, hours_before):
    """Median winner price over the fills in [reveal - hours_before, reveal)."""
    vals = [p for ts, p in points if reveal - hours_before * HOUR <= ts < reveal]
    return statistics.median(vals) if vals else None


def doubt_kind(low):
    if low is None or low > DOUBT_PRICE:
        return None
    return "underdog" if low < UNDERDOG_PRICE else "toss-up"


def window_positions(rows, winner, start, reveal):
    """{wallet: position} of the winning side bought in [start, reveal): every
    buy adds into one position (orders the exchange split count once), and
    the wallet's sells of the winning side in the same span take away."""
    w = str(winner).strip().lower()
    pos = {}
    for r in rows:
        if str(r.get("outcome", "")).strip().lower() != w:
            continue
        ts = to_epoch(r.get("timestamp"))
        if ts is None or not (start <= ts < reveal):
            continue
        try:
            usd, sh = float(r.get("dollar_amount") or 0), float(r.get("contracts") or 0)
        except (TypeError, ValueError):
            continue
        wallet = str(r.get("user_id") or "").lower()
        p = pos.setdefault(wallet, {"wallet": wallet, "buy_usd": 0.0, "buy_shares": 0.0, "sell_usd": 0.0,
                                    "sell_shares": 0.0, "first_buy": None, "last_buy": None})
        if str(r.get("side", "")).upper() == "BUY":
            p["buy_usd"] += usd
            p["buy_shares"] += sh
            p["first_buy"] = ts if p["first_buy"] is None else min(p["first_buy"], ts)
            p["last_buy"] = ts if p["last_buy"] is None else max(p["last_buy"], ts)
        else:
            p["sell_usd"] += usd
            p["sell_shares"] += sh
    return pos


def whale_rows(positions, threshold, sure_thing=SURE_THING):
    """The whales among window positions, each with its profit: the shares it
    still held at the reveal pay $1, sells are matched to the window's buys
    (never more than it bought there), minus what it paid."""
    out = []
    for p in positions.values():
        if p["buy_shares"] <= 0 or p["buy_usd"] < threshold:
            continue
        avg = p["buy_usd"] / p["buy_shares"]
        if avg >= sure_thing:
            continue
        matched = min(p["sell_shares"], p["buy_shares"])
        proceeds = p["sell_usd"] * (matched / p["sell_shares"]) if p["sell_shares"] else 0.0
        held = p["buy_shares"] - matched
        out.append({**p, "avg_price": avg, "held_shares": held, "payout": held + proceeds,
                    "profit": held + proceeds - p["buy_usd"]})
    out.sort(key=lambda x: -x["profit"])
    return out


def weak_points(kind, score, flagged_share):
    """What makes a market weaker: favourite (never in doubt), small (whales
    made under MIN_SCORE of the money traded), established (flagged wallets
    took under MIN_FLAGGED_SHARE of the profit)."""
    out = []
    if kind is None:
        out.append("favourite")
    if score < MIN_SCORE:
        out.append("small")
    if flagged_share < MIN_FLAGGED_SHARE:
        out.append("established")
    return out


def rank(picks, per_day=PER_DAY):
    """Fewest weak points first, then score, then the bigger move before the news."""
    return sorted(picks, key=lambda p: (len(p["weak"]), -p["score"], -(p["move"]["ratio"] or 0)))[:per_day]


# ── one market ───────────────────────────────────────────────────────────────

def raw_rows(market, max_rows=MAX_ROWS):
    """Raw-CSV rows of every fill (cached under data/watchlist/cache/), or
    (None, reason) when the market is too large to fetch here."""
    paths = method.cache_paths(market["conditionId"])
    if os.path.exists(paths["raw_gz"]):
        return method._read_raw(paths["raw_gz"]), None
    v2, capped = polymarket_api.fetch_trades(market["conditionId"], max_rows=max_rows + 1)
    if capped or len(v2) > max_rows:
        return None, f"more than {max_rows:,} fills"
    trades = polymarket_trades.to_trade_rows(v2, market)
    if not trades:
        return None, "no fills"
    method._build_cache(trades, paths)
    return trades, None


def first_trades(rows):
    """{wallet: its first trade in the market (any side)}."""
    out = {}
    for r in rows:
        ts = to_epoch(r.get("timestamp"))
        w = str(r.get("user_id") or "").lower()
        if ts is not None and (w not in out or ts < out[w]):
            out[w] = ts
    return out


def analyze(market, conn, label=None):
    """Steps 1-6 for one closed market: a dict (with its weak points), or
    {"dropped": reason} when there is nothing to show."""
    win = filters.winning_outcome(market)
    winner = win[1]
    rows, why = raw_rows(market)
    if rows is None:
        return {"dropped": why}
    points = winner_prices(rows, winner)
    if not points:
        return {"dropped": "no fills"}
    hours = hourly(points)
    reveal = reveal_time(hours, points[-1][0])
    start = reveal - WINDOW_DAYS * DAY
    low, low_hours = sustained_low(hours, start, reveal)
    kind = doubt_kind(low)
    traded = sum(float(r.get("dollar_amount") or 0) for r in rows)
    threshold = scrape_insiders.whale_threshold(traded)
    whales = whale_rows(window_positions(rows, winner, start, reveal), threshold)
    profit = sum(w["profit"] for w in whales)
    if not whales or profit <= 0:
        return {"dropped": "no whale bet on the winner in time"}
    firsts = first_trades(rows)
    priors = method.lookup_priors([{"wallet": w["wallet"], "first_ts": firsts.get(w["wallet"], w["first_buy"]),
                                    "position": w["buy_usd"]} for w in whales], "exact", conn=conn)
    for w, pr in zip(whales, priors):
        f = method.flag_whale(w["buy_usd"], pr["trade_count"], pr["volume_usdc"])
        w.update(prior_trades=int(pr["trade_count"]), prior_volume=round(float(pr["volume_usdc"]), 2),
                 concentration=round(f["concentration"], 1), is_new=f["is_first_x"], is_all_in=f["is_percent"],
                 flagged=f["flagged"])
    flagged = [w for w in whales if w["flagged"]]
    flagged_profit = sum(w["profit"] for w in flagged)
    share = flagged_profit / profit if profit > 0 else 0.0
    score = profit / traded if traded else 0.0
    p6, p24 = price_before(points, reveal, 6), price_before(points, reveal, 24)
    ev = filters.event_of(market)
    pick = {
        "condition_id": market["conditionId"],
        "question": market.get("question"),
        "event_title": ev.get("title") or market.get("question"),
        "url": f"https://polymarket.com/event/{ev.get('slug') or market.get('slug')}",
        "type": (label or {}).get("type"),
        "winner": winner,
        "closed": iso(to_epoch(market.get("closedTime"))),
        "reveal": iso(reveal),
        "traded_usd": round(traded, 2),
        "doubt": {"kind": kind, "low": round(low, 4) if low is not None else None, "hours": low_hours,
                  "window_days": WINDOW_DAYS},
        "move": {"price_6h": round(p6, 4) if p6 is not None else None,
                 "price_24h": round(p24, 4) if p24 is not None else None,
                 "ratio": round(p6 / low, 2) if (p6 is not None and low) else None},
        "whale_threshold": threshold,
        "whales": len(whales),
        "cost": round(sum(w["buy_usd"] for w in whales), 2),
        "payout": round(sum(w["payout"] for w in whales), 2),
        "profit": round(profit, 2),
        "score": round(score, 4),
        "weak": weak_points(kind, score, share),
        "flagged": len(flagged),
        "flagged_profit": round(flagged_profit, 2),
        "flagged_share": round(share, 4),
        "wallets": [{
            "wallet": w["wallet"], "cost": round(w["buy_usd"], 2), "avg_price": round(w["avg_price"], 4),
            "profit": round(w["profit"], 2), "first_buy_hours": round((reveal - w["first_buy"]) / HOUR, 1),
            "last_buy_hours": round((reveal - w["last_buy"]) / HOUR, 1), "prior_trades": w["prior_trades"],
            "concentration": w["concentration"], "is_new": w["is_new"], "is_all_in": w["is_all_in"],
            "flagged": w["flagged"]} for w in whales],
    }
    return pick
