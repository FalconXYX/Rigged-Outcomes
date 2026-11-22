#!/usr/bin/env python3
"""
Offline checks of backend/rubric/wallet_facts.py: a made-up market and a fake Polymarket, no network.

    python3 -m pytest -q tests/test_rubric_wallet_facts.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from backend.rubric import wallet_facts as wf  # noqa: E402

H = 3600
T0 = 1_699_999_200       # on a 15-minute boundary
CID = "0x" + "a" * 64
OTHER = "0x" + "b" * 64
FRESH = "0x" + "1" * 40
OLD = "0x" + "2" * 40


def market():
    fills = []
    for i in range(48):                                       # the crowd: Yes at 0.2 for two days
        fills.append(("0x" + "9" * 40, "BUY", "Yes", 0.2, 50, 10, T0 + i * H))
    fills.append((FRESH, "BUY", "Yes", 0.2, 10_000, 2_000, T0 + 40 * H))
    fills.append((FRESH, "SELL", "Yes", 0.5, 2_500, 1_250, T0 + 45 * H))
    fills.append((OLD, "BUY", "Yes", 0.25, 8_000, 2_000, T0 + 41 * H))
    for i in range(12):                                       # the reveal: 0.97 from hour 48
        fills.append(("0x" + "8" * 40, "BUY", "Yes", 0.97, 100, 97, T0 + (48 + i) * H))
    f = pd.DataFrame(fills, columns=["wallet", "side", "outcome", "price", "contracts", "dollars", "ts"])
    iso = lambda t: pd.Timestamp(t, unit="s", tz="UTC").isoformat()   # noqa: E731
    clean = pd.DataFrame([
        {"user_id": FRESH, "outcome": "Yes", "side": "BUY", "avg_odds": 0.2, "contracts": 10_000,
         "dollar_amount": 2_000, "first_trade": iso(T0 + 40 * H), "last_trade": iso(T0 + 40 * H), "num_trades": 1},
        {"user_id": OLD, "outcome": "Yes", "side": "BUY", "avg_odds": 0.25, "contracts": 8_000,
         "dollar_amount": 2_000, "first_trade": iso(T0 + 41 * H), "last_trade": iso(T0 + 41 * H), "num_trades": 1},
        {"user_id": "0x" + "9" * 40, "outcome": "Yes", "side": "BUY", "avg_odds": 0.2, "contracts": 2_400,
         "dollar_amount": 480, "first_trade": iso(T0), "last_trade": iso(T0 + 47 * H), "num_trades": 48},
    ])
    flags = lambda p, c: {"prior_bet_count": p, "concentration": c, "is_first_x": p <= 10,   # noqa: E731
                          "is_percent": c >= 80, "is_suspicious": p <= 10 or c >= 80,
                          "category": wf.category(p <= 10, c >= 80), "risk_score": wf.risk_score(p, c)}
    whales = [{"wallet": FRESH, "user_id": FRESH, "first_trade": iso(T0 + 40 * H), "first_buy_ts": T0 + 40 * H,
               **flags(1, 100.0)},
              {"wallet": OLD, "user_id": OLD, "first_trade": iso(T0 + 41 * H), "first_buy_ts": T0 + 41 * H,
               **flags(300, 5.0)}]
    return wf.Market(name="t", slug="t", title="T?", outcome="Yes", condition_id=CID, fills=f, clean=clean,
                     whales=whales, news=[(T0 + 48 * H, "News")])


def fake(url, params):
    user, path = params.get("user"), url.rsplit("/", 1)[-1]
    if path == "user-stats":
        return {"data": {"join_date": T0 + 39 * H if user == FRESH else T0 - 400 * 86400}}
    if path == "positions":
        rows = [] if user == FRESH else [{"condition_id": OTHER, "realized_pnl": 50.0},
                                         {"condition_id": OTHER, "realized_pnl": -10.0}]
        return {"data": rows, "pagination": {"has_more": False}}
    if params.get("sort_direction") == "ASC":
        return {"data": [{"timestamp": T0 + 39 * H if user == FRESH else T0 - 300 * 86400}]}
    if user == FRESH:      # its own first fill in this market is the only row up to the cutoff
        rows = [{"condition_id": CID, "timestamp": T0 + 40 * H, "usdc_size": 2000, "transaction_hash": "x"}]
    else:
        rows = [{"condition_id": OTHER, "timestamp": T0 - 86400 * k, "usdc_size": 100, "transaction_hash": f"h{k}"}
                for k in range(1, 301)]
    return {"data": rows, "pagination": {"has_more": False}}


def test_market_facts():
    with tempfile.TemporaryDirectory() as d:
        src = wf.Polymarket(d, rate=0, getter=fake)
        doc = wf.market_facts(market(), src, workers=2)
        again = wf.Polymarket(d, rate=0, getter=lambda *a: (_ for _ in ()).throw(AssertionError("not cached")))
        assert wf.market_facts(market(), again, workers=2) == doc
    m = doc["market"]
    assert m["reveal_method"] == "price" and m["reveal"] == pd.Timestamp(T0 + 48 * H, unit="s", tz="UTC").isoformat()
    fresh, old = (w["facts"] for w in doc["whales"])
    assert fresh["hours_before_reveal"] == 8 and fresh["hours_before_news"] == 8
    assert fresh["prior_trades"] == 0 and fresh["prior_markets"] == 0 and fresh["concentration_pct"] == 100
    assert fresh["wallet_age_days"] == round(1 / 24, 3)
    assert fresh["sold_before_close_pct"] == 25 and fresh["sold_before_reveal_pct"] == 25
    assert fresh["market_prob_at_entry"] == 0.2 and fresh["payout_multiple"] == 5
    # the line: 0.2 until its own sale at 0.5 (hour 45), 0.97 from the reveal (hour 48)
    assert fresh["hours_to_big_move"] == 5 and fresh["max_rise_48h"] == 0.77
    assert old["hours_to_big_move"] == 4 and old["max_rise_48h"] == 0.77
    assert fresh["whales_same_window"] == 1 and fresh["fresh_whales_same_window"] == 0
    assert old["prior_trades"] == 300 and old["prior_win_rate_pct"] == 50 and old["prior_pnl_usd"] == 40
    assert old["fresh_whales_same_window"] == 1 and old["wallet_age_days"] > 300
    assert set(doc["factors"]) == set(fresh) == set(wf.FACTORS)
    assert [w["flags"]["category"] for w in doc["whales"]] == ["critical", "normal"]


if __name__ == "__main__":
    test_market_facts()
    print("ok")
