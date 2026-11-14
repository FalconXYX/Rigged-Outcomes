#!/usr/bin/env python3
"""
Offline tests for the deeper look at one closed market (watchlist.deep): no
network.

    python3 tests/test_watchlist_deep.py

Covers the winner's price from fills on either outcome, the reveal (the hour
after the last hour below 90¢), the price the winner sat at for 6 hours,
upset vs toss-up vs favourite, split orders adding into one position with
sells taken away, whales (threshold, 98¢), profit, weak points and the
ranking.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchlist import deep as up  # noqa: E402

H = 3600
T0 = 1_790_000_000 - 1_790_000_000 % H


def row(ts, outcome, odds, usd, side="BUY", user="0xa", shares=None):
    return {"timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "outcome": outcome, "odds": str(odds),
            "dollar_amount": str(usd), "side": side, "user_id": user,
            "contracts": str(shares if shares is not None else usd / odds)}


def test_winner_prices_both_outcomes_and_dust():
    rows = [row(T0, "Yes", 0.2, 50), row(T0 + 1, "No", 0.7, 50), row(T0 + 2, "Yes", 0.9, 5)]
    pts = up.winner_prices(rows, "Yes")
    assert [round(p, 2) for _, p in pts] == [0.2, 0.3]          # No at 0.7 = Yes at 0.3; the $5 fill is dust

def test_reveal_time():
    hours = {T0: 0.2, T0 + H: 0.3, T0 + 2 * H: 0.95, T0 + 3 * H: 0.97}
    assert up.reveal_time(hours, T0 + 3 * H + 100) == T0 + 2 * H          # the hour after the last one below 90¢
    hours2 = {T0: 0.2, T0 + H: 0.95, T0 + 2 * H: 0.85}
    assert up.reveal_time(hours2, T0 + 2 * H + 50) == T0 + 2 * H + 50      # never settled: the last trade
    assert up.reveal_time({T0: 0.96, T0 + H: 0.99}, T0 + H) == T0          # already known
    assert up.reveal_time({}, 0) is None

def test_sustained_low_and_doubt():
    hours = {T0 + i * H: p for i, p in enumerate([0.05, 0.8, 0.8, 0.8, 0.8, 0.8, 0.8, 0.8])}
    assert up.sustained_low(hours, T0, T0 + 8 * H) == (0.8, 6)          # one odd 5¢ hour is not where it sat
    hours = {T0 + i * H: p for i, p in enumerate([0.1, 0.1, 0.1, 0.1, 0.1, 0.3])}
    assert up.sustained_low(hours, T0, T0 + 6 * H) == (0.3, 6)
    assert up.sustained_low(hours, T0, T0 + 3 * H) == (0.1, 3)          # traded in fewer hours: all of them
    assert up.sustained_low(hours, T0 + 10 * H, T0 + 12 * H) == (None, 0)
    assert up.doubt_kind(0.1) == "underdog" and up.doubt_kind(0.6) == "toss-up"
    assert up.doubt_kind(0.7) == "toss-up" and up.doubt_kind(0.71) is None and up.doubt_kind(None) is None

def test_window_positions_split_orders_and_sells():
    start, reveal = T0, T0 + 100 * H
    rows = [row(T0 + H, "Yes", 0.2, 600, user="0xA"), row(T0 + H + 1, "Yes", 0.25, 400, user="0xA"),  # one split order
            row(T0 + 5 * H, "Yes", 0.3, 300, side="SELL", user="0xA", shares=1000),
            row(T0 - H, "Yes", 0.1, 999, user="0xA"),                                                 # before the window
            row(T0 + 200 * H, "Yes", 0.99, 999, user="0xA"),                                          # after the reveal
            row(T0 + 2 * H, "No", 0.8, 5000, user="0xB")]                                             # the losing side
    pos = up.window_positions(rows, "Yes", start, reveal)
    a = pos["0xa"]
    assert round(a["buy_usd"]) == 1000 and round(a["buy_shares"]) == 4600 and round(a["sell_shares"]) == 1000
    assert a["first_buy"] == T0 + H and a["last_buy"] == T0 + H + 1
    assert "0xb" not in pos

def test_whale_rows_threshold_sure_thing_and_profit():
    pos = {
        "big": {"wallet": "big", "buy_usd": 2000.0, "buy_shares": 10000.0, "sell_usd": 300.0, "sell_shares": 1000.0,
                "first_buy": T0, "last_buy": T0},
        "small": {"wallet": "small", "buy_usd": 500.0, "buy_shares": 5000.0, "sell_usd": 0.0, "sell_shares": 0.0,
                  "first_buy": T0, "last_buy": T0},
        "sure": {"wallet": "sure", "buy_usd": 9900.0, "buy_shares": 10000.0, "sell_usd": 0.0, "sell_shares": 0.0,
                 "first_buy": T0, "last_buy": T0},
    }
    ws = up.whale_rows(pos, 1000)
    assert [w["wallet"] for w in ws] == ["big"]                 # small is under the threshold, sure is at 99¢
    w = ws[0]
    assert round(w["avg_price"], 2) == 0.2 and round(w["held_shares"]) == 9000
    assert round(w["profit"]) == 9000 + 300 - 2000              # held shares pay $1, plus the sale, minus the cost
    # selling more than the window bought never counts more than it bought
    pos2 = {"x": {"wallet": "x", "buy_usd": 1000.0, "buy_shares": 2000.0, "sell_usd": 900.0, "sell_shares": 3000.0,
                  "first_buy": T0, "last_buy": T0}}
    w2 = up.whale_rows(pos2, 1000)[0]
    assert w2["held_shares"] == 0 and round(w2["payout"]) == 600 and round(w2["profit"]) == -400

def test_weak_points_and_rank():
    assert up.weak_points("underdog", 0.2, 0.5) == []
    assert up.weak_points(None, 0.01, 0.0) == ["favourite", "small", "established"]
    assert up.weak_points("toss-up", 0.05, 0.1) == []                   # at the line is not weaker
    picks = [{"score": 0.1, "weak": [], "move": {"ratio": 1.0}}, {"score": 0.3, "weak": [], "move": {"ratio": 1.0}},
             {"score": 0.1, "weak": [], "move": {"ratio": 3.0}}, {"score": 0.9, "weak": ["small"], "move": {"ratio": 9}},
             {"score": 0.2, "weak": [], "move": {"ratio": None}},
             {"score": 0.8, "weak": ["favourite", "established"], "move": {"ratio": 2.0}}]
    r = up.rank(picks, per_day=5)
    # fewer weak points first; then score; then the move
    assert [(p["score"], len(p["weak"])) for p in r] == [(0.3, 0), (0.2, 0), (0.1, 0), (0.1, 0), (0.9, 1)]
    assert r[2]["move"]["ratio"] == 3.0


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for fn in checks:
        try:
            fn()
            print(f"PASS {fn.__name__}", flush=True)
        except Exception as e:
            failed.append(fn.__name__)
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}", flush=True)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)
