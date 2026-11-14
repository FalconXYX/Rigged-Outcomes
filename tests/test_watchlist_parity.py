#!/usr/bin/env python3
"""
Offline tests for watchlist.parity: no network, a few seconds.

    python3 -m pytest -q tests/test_watchlist_parity.py     # or
    python3 tests/test_watchlist_parity.py                  # same checks, exits 1 on failure

Covers the pure pieces the parity report is built from: the committed
CSVs' booleans, the cut points of a crawled history (before the first-trade
day / at the cutoff / to the end of that day, fills vs transactions, the
UTC timestamp check), when a capped crawl still decides the flags, the
step-by-step explanation of a disagreement (day rounding, fills vs
transactions, user-volume vs the activity feed, a v1 under-count), the
agreement and flagged-capital tallies, the spread of the checked sample,
the v1-window verdicts, the JSON-lines caches, loading the committed rows
from a scripted markets.json, the error profile, and the live helpers
(one-call step, crawl record, hybrid, v1 probe) over faked feeds.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket_api  # noqa: E402
from watchlist import net, parity, store  # noqa: E402
from watchlist.common import DAY, to_epoch  # noqa: E402

CID = "0x" + "c" * 64
OTHER = "0x" + "d" * 64
FIRST = to_epoch("2026-02-13T16:06:57Z")
D0 = FIRST // DAY * DAY


def W(n):
    return "0x" + f"{n:040x}"


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


@contextlib.contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def no_network(*a, **k):
    raise AssertionError(f"network call in an offline test: {a[:1]}")


def h(ts, usdc, tx, cid=CID, size=None):
    """A v1-shaped TRADE row, as fetch_user_history_up_to returns them."""
    return {"timestamp": ts, "usdcSize": usdc, "size": size if size is not None else usdc * 2,
            "transactionHash": tx, "conditionId": cid, "type": "TRADE"}


def history():
    """3 trades before the first-trade day (one tx with 2 fills), 2 earlier
    that day, the first trade itself (2 fills, 1 tx), 3 later that day."""
    return [
        h(D0 - 5 * DAY, 100.0, "0xa"), h(D0 - 5 * DAY, 50.0, "0xa", cid=OTHER), h(D0 - DAY, 25.0, "0xb", cid=OTHER),
        h(D0 + 10, 10.0, "0xc", cid=OTHER), h(FIRST - 60, 20.0, "0xd", cid=OTHER),
        h(FIRST, 1000.0, "0xe"), h(FIRST, 500.0, "0xe"),
        h(FIRST + 5, 300.0, "0xf"), h(FIRST + 100, 300.0, "0xg"), h(D0 + DAY - 1, 1.0, "0xh", cid=OTHER),
        h(D0 + DAY + 5, 9.0, "0xi"),     # the next day: in no window
    ]


def make_row(committed=None, uv=None, stats=None, position=2000.0, source="v1", current_rule=True, rid="M:0"):
    st = stats if stats is not None else parity.window_stats(history(), FIRST, CID)
    c = {"prior_bet_count": 5, "lifetime_portfolio": 2000.0, "concentration": 100.0,
         "is_first_x": True, "is_percent": True, "flagged": True}
    c.update(committed or {})
    r = {"id": rid, "market": "m", "output": "M", "position": position, "first_ts": FIRST, "wallet": W(1),
         "committed": c, "committed_source": source, "current_rule": current_rule,
         "truth": st, "truth_capped": False, "truth_determined": True,
         "uv": uv or {"same_day": {"trade_count": st["day"]["rows"], "volume_usdc": st["day"]["usdc"]},
                      "day_before": {"trade_count": st["before"]["rows"], "volume_usdc": st["before"]["usdc"]}}}
    r["one_call"] = {v: parity.flags(position, r["uv"][v]["trade_count"], r["uv"][v]["volume_usdc"])
                     for v in parity.VARIANTS}
    r["truth_flags"] = parity.flags(position, st["cut"]["tx"], st["cut"]["usdc"])
    r["exact_fills_flags"] = parity.flags(position, st["cut"]["rows"], st["cut"]["usdc"])
    return r


# ── pure pieces ──────────────────────────────────────────────────────────────

def test_as_bool_reads_the_csv_cells():
    assert parity.as_bool("True") is True and parity.as_bool(" false ") is False
    assert parity.as_bool(True) is True and parity.as_bool(0) is False
    raises(lambda: parity.as_bool("maybe"), ValueError)


def test_flags_are_analyze_whales_rule():
    f = parity.flags(1000.0, 10, 1300.0)
    assert f["is_first_x"] and not f["is_percent"] and f["flagged"] and f["concentration"] == 76.92
    assert parity.flags(1000.0, 11, 1250.0) == {"is_first_x": False, "is_percent": True, "flagged": True,
                                                 "concentration": 80.0}
    assert not parity.flags(1000.0, 11, 1250.01)["flagged"]
    assert parity.flags(500.0, 0, 0)["concentration"] == 100.0


def test_window_stats_cut_points():
    st = parity.window_stats(history(), FIRST, CID)
    assert st["day_start"] == D0
    assert st["before"] == {"rows": 3, "tx": 2, "usdc": 175.0, "size": 350.0}
    assert st["same_day_before_cut"]["rows"] == 4 and st["same_day_before_cut"]["tx"] == 3
    assert st["cut"]["rows"] == 7 and st["cut"]["tx"] == 5 and st["cut"]["usdc"] == 1705.0
    assert st["day"]["rows"] == 10 and st["day"]["tx"] == 8
    assert st["same_day_after_cut"]["rows"] == 3 and st["same_day_after_cut_this_market"] == 2
    # the market's own trade sits at exactly the cutoff: the label is the UTC epoch
    assert st["market"]["rows_at_first_ts"] == 2 and st["market"]["earliest_ts"] == D0 - 5 * DAY
    assert st["market"]["rows_at_et_shift_h"] == {"4": 0, "5": 0}


def test_window_stats_sees_an_et_wall_clock_label():
    # the trade happened 5 h after the label (an EST wall clock written as +00:00)
    st = parity.window_stats([h(FIRST + 5 * 3600, 100.0, "0x1")], FIRST, CID)
    assert st["market"]["rows_at_first_ts"] == 0 and st["market"]["rows_at_et_shift_h"]["5"] == 1


def test_capped_crawl_decides_only_when_both_flags_are_ruled_out():
    st = {"cut": {"tx": 11, "usdc": 1300.0}}
    assert parity.truth_determined(st, 1000.0, capped=False)
    assert parity.truth_determined(st, 1000.0, capped=True)            # > 10 tx and > 1.25 x position
    assert not parity.truth_determined({"cut": {"tx": 11, "usdc": 1250.0}}, 1000.0, capped=True)
    assert not parity.truth_determined({"cut": {"tx": 10, "usdc": 9e9}}, 1000.0, capped=True)


def test_explain_same_day_day_rounding():
    # 5 tx at the cutoff (first_x), 10 fills by the end of the day: still first_x;
    # make the day heavier so same_day crosses 10 fills
    hist = history() + [h(FIRST + 200 + i, 1.0, f"0xz{i}", cid=OTHER) for i in range(5)]
    st = parity.window_stats(hist, FIRST, CID)
    r = make_row(stats=st)
    assert r["truth_flags"]["is_first_x"] and not r["one_call"]["same_day"]["is_first_x"]
    e = parity.explain("is_first_x", r, "same_day")
    assert e["right"] == "committed" and e["cause"] == "day_rounding"
    assert "8 fills" in e["detail"] and "after the first trade" in e["detail"]


def test_explain_day_before_drops_the_first_trade_day():
    # truth: 12 tx at the cutoff -> not first_x; day_before sees 3 fills -> first_x
    hist = [h(D0 - DAY, 1.0, f"0xp{i}", cid=OTHER) for i in range(3)] + \
           [h(D0 + 60 + i, 1.0, f"0xq{i}", cid=OTHER) for i in range(8)] + [h(FIRST, 900.0, "0xe")]
    st = parity.window_stats(hist, FIRST, CID)
    r = make_row(stats=st, committed={"prior_bet_count": 12, "is_first_x": False, "is_percent": True})
    e = parity.explain("is_first_x", r, "day_before")
    assert r["one_call"]["day_before"]["is_first_x"] and not r["truth_flags"]["is_first_x"]
    assert e["right"] == "committed" and e["cause"] == "day_rounding" and "drops 9 fills" in e["detail"]


def test_explain_fills_vs_transactions():
    # 11 fills in 6 transactions at the cutoff; user-volume counts fills
    hist = [h(D0 - DAY, 1.0, f"0xt{i // 2}", cid=OTHER) for i in range(10)] + [h(FIRST, 900.0, "0xe")]
    st = parity.window_stats(hist, FIRST, CID)
    assert st["cut"] == {"rows": 11, "tx": 6, "usdc": 910.0, "size": 1820.0}
    r = make_row(stats=st, committed={"prior_bet_count": 6})
    e = parity.explain("is_first_x", r, "same_day")
    assert e == {"right": "committed", "cause": "fills_vs_tx", "detail": e["detail"]}
    assert "11 fills are 6 transactions" in e["detail"]


def test_explain_user_volume_vs_activity():
    st = parity.window_stats(history(), FIRST, CID)
    uv = {"same_day": {"trade_count": 12, "volume_usdc": st["day"]["usdc"]},
          "day_before": {"trade_count": st["before"]["rows"], "volume_usdc": st["before"]["usdc"]}}
    r = make_row(stats=st, uv=uv)
    e = parity.explain("is_first_x", r, "same_day")
    assert e["right"] == "committed" and e["cause"] == "uv_vs_activity"


def test_explain_v1_undercount_sides_with_the_one_call_answer():
    hist = [h(D0 - 9 * DAY + i, 100.0, f"0xu{i}", cid=OTHER) for i in range(40)] + [h(FIRST, 900.0, "0xe")]
    st = parity.window_stats(hist, FIRST, CID)
    r = make_row(stats=st, position=900.0,
                 committed={"prior_bet_count": 1, "lifetime_portfolio": 900.0, "is_first_x": True, "is_percent": True})
    r["v1_window"] = {"verdict": "its walk ended early"}
    for v in parity.VARIANTS:
        e = parity.explain("flagged", r, v)
        assert e["right"] == "one_call" and e["cause"] == "v1_undercount", e
        assert "41 transactions" in e["detail"] and "ended early" in e["detail"]
    r["committed_source"] = "v2"
    assert parity.explain("flagged", r, "same_day")["cause"] == "committed_v2_undercount"
    r["truth_determined"] = False
    assert parity.explain("flagged", r, "same_day")["right"] == "unverified"


def test_agreement_and_flagged_pct():
    T = {"is_first_x": True, "is_percent": True, "flagged": True}
    F = {"is_first_x": False, "is_percent": False, "flagged": False}
    a = parity.agreement([(T, T), (T, F), (F, T), (F, F)])
    assert a["flagged"] == {"n": 4, "agree": 2, "pct": 50.0, "a_only": 1, "b_only": 1}
    assert parity.agreement([])["flagged"]["pct"] is None
    rows = [{"position": 100.0, "f": T}, {"position": 300.0, "f": F}, {"position": 50.0, "f": None}]
    assert parity.flagged_pct(rows, lambda r: r["f"]) == (25.0, 2)
    assert parity.flagged_pct([], lambda r: r) == (None, 0)


def test_pick_sample_spreads_over_markets_and_causes_and_dedupes_rows():
    items = [{"id": f"a{i}", "market": "a", "cause": "x"} for i in range(10)] + \
            [{"id": f"a{i}", "market": "a", "cause": "y"} for i in range(3)] + \
            [{"id": "b0", "market": "b", "cause": "x"}] + \
            [{"id": f"c{i}", "market": "c", "cause": "z"} for i in range(4)]
    got = parity.pick_sample(items, n_min=6)
    ids = [g["id"] for g in got]
    assert len(ids) == 6 and len(set(ids)) == 6
    assert {g["market"] for g in got} == {"a", "b", "c"}
    assert {g["cause"] for g in got if g["market"] == "a"} == {"x", "y"}
    assert len(parity.pick_sample(items, n_min=100)) == 15          # every distinct row once
    assert parity.pick_sample([], n_min=5) == []


def test_v1_window_verdicts():
    never = parity.v1_window_verdict({"rows_after_cut_to_run": 15_000, "probe_capped": False}, 0)
    assert never["explained_by"] == "never_reached"
    assert parity.v1_window_verdict({"rows_after_cut_to_run": 9, "probe_capped": True}, 0)["explained_by"] == \
        "never_reached"
    probe = {"rows_after_cut_to_run": 14_000, "probe_capped": False, "predicted_v1_count": 600}
    assert parity.v1_window_verdict(probe, 590)["explained_by"] == "window"
    early = parity.v1_window_verdict({**probe, "predicted_v1_count": 3_494}, 1)
    assert early["explained_by"] == "stopped_early" and "3,494" in early["verdict"]
    assert parity.v1_window_verdict({"rows_after_cut_to_run": 5, "probe_capped": False}, 1)["explained_by"] == \
        "unknown"


def test_jsonl_cache_last_line_wins_and_torn_lines_are_ignored():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "sub", "c.jsonl")
        assert parity.read_jsonl(p, key=lambda r: r["k"]) == {}
        parity.append_jsonl(p, {"k": 1, "v": "old"})
        parity.append_jsonl(p, {"k": 2, "v": "x"})
        parity.append_jsonl(p, {"k": 1, "v": "new"})
        with open(p, "a") as f:
            f.write('{"k": 3, "v": "tor')
        got = parity.read_jsonl(p, key=lambda r: r["k"])
        assert got == {1: {"k": 1, "v": "new"}, 2: {"k": 2, "v": "x"}}
    finally:
        shutil.rmtree(d)


def test_v1_artifacts_and_error_profile():
    hist = [h(D0 - 9 * DAY + i, 100.0, f"0xu{i}", cid=OTHER) for i in range(40)] + [h(FIRST, 900.0, "0xe")]
    art = make_row(stats=parity.window_stats(hist, FIRST, CID), position=900.0, rid="M:1",
                   committed={"prior_bet_count": 1, "lifetime_portfolio": 900.0})
    fine = make_row(rid="M:2", committed={"prior_bet_count": 5})
    v2 = make_row(stats=art["truth"], position=900.0, source="v2", rid="M:3", committed={"prior_bet_count": 1})
    assert [r["id"] for r in parity.v1_artifacts([art, fine, v2])] == ["M:1"]
    # a day-one builder: 1 tx at the cutoff, 30 fills by the end of the day
    builder_hist = [h(FIRST, 2000.0, "0xe")] + [h(FIRST + 10 + i, 100.0, f"0xb{i}") for i in range(30)]
    builder = make_row(stats=parity.window_stats(builder_hist, FIRST, CID), rid="M:4")
    prof = parity.error_profile([builder, fine], "same_day")
    assert prof["count"] == 1 and prof["by_kind"] == {"missed_day_one_builder": 1}
    assert prof["rows"][0]["first_day_fills_after_cut"] == 30 and prof["rows"][0]["of_which_in_this_market"] == 30
    # day_before: the wallet's history starts on the first-trade day
    starter_hist = [h(D0 + 1 + i, 300.0, f"0xs{i}", cid=OTHER) for i in range(12)] + [h(FIRST, 2000.0, "0xe")]
    starter = make_row(stats=parity.window_stats(starter_hist, FIRST, CID), rid="M:5")
    assert parity.error_profile([starter], "day_before")["by_kind"] == {"false_day_start": 1}
    builder["current_rule"] = False
    assert parity.error_profile([builder], "same_day")["count"] == 0


def test_load_rows_joins_the_clean_rows():
    d = tempfile.mkdtemp()
    try:
        markets = [{"name": "fired", "output": "Noem", "correct_outcome": "Yes",
                    "highlightedUsers": {W(9): "x"}}]
        mf = os.path.join(d, "markets.json")
        with open(mf, "w") as f:
            json.dump(markets, f)
        t1, t2, t3 = "2026-02-13T16:06:57+00:00", "2026-02-14T10:00:00+00:00", "2026-03-05T16:41:49+00:00"
        clean = ("user_id,market_id,market_title,outcome,side,avg_odds,contracts,dollar_amount,win_status,"
                 "first_trade,last_trade,num_trades\n"
                 f"{W(1)},{CID},Noem out?,Yes,BUY,0.30,5000,1500.0,WIN,{t1},{t1},1\n"
                 f"{W(1)},{CID},Noem out?,Yes,SELL,0.50,3000,1500.0,WIN,{t2},{t2},1\n"
                 f"{W(2)},{CID},Noem out?,Yes,BUY,0.995,1005,1000.0,WIN,{t2},{t2},1\n"
                 f"{W(3)},{CID},Noem out?,No,BUY,0.70,1000000,700000.0,LOSS,{t1},{t1},1\n"
                 f"{W(4)},{CID},Noem out?,Yes,BUY,0.20,10000,2000.0,WIN,{t3},{t3},3\n")
        with open(os.path.join(d, "Noem_clean.csv"), "w") as f:
            f.write(clean)
        ins = ("user_id,market_position,lifetime_portfolio,prior_bet_count,portfolio_concentration,is_first_x,"
               "Is_percent,is_suspicious,target_trade_time,market_title\n"
               f"{W(1)},1500.0,1500.0,1,100.0,True,True,True,{t1},Noem out?\n"
               f"{W(1)},1500.0,90000.0,91,1.67,False,False,False,{t2},Noem out?\n"
               f"{W(2)},1000.0,1000.0,3,100.0,True,True,True,{t2},Noem out?\n")
        with open(os.path.join(d, "Noem_insiders.csv"), "w") as f:
            f.write(ins)
        rows, inputs = parity.load_rows(markets_file=mf, data_dir=d)
        assert [r["id"] for r in rows] == ["Noem:0", "Noem:1", "Noem:2"]
        a, b, c = rows
        assert a["first_ts"] == to_epoch(t1) and a["wallet"] == W(1) and a["condition_id"] == CID
        assert (a["current_rule"], b["exit"], c["sweep"]) == (True, True, True)
        assert not b["current_rule"] and not c["current_rule"]
        assert a["committed"] == {"prior_bet_count": 1, "lifetime_portfolio": 1500.0, "concentration": 100.0,
                                  "is_first_x": True, "is_percent": True, "flagged": True}
        assert b["committed"]["flagged"] is False and a["committed_source"] == "v1"
        assert not a["eoa_column"] and not a["eoa_differs"]
        info = inputs[0]
        assert info["committed_rows"] == 3 and info["rows_current_rule"] == 1 and info["rows_sell"] == 1
        assert info["rows_near_certain"] == 1 and info["rows_not_matched_to_clean"] == 0
        assert info["current_rule_whales_missing_from_committed"] == 1    # W(4): a whale the old file lacks
        assert parity.load_rows(names={"other"}, markets_file=mf, data_dir=d) == ([], [])
    finally:
        shutil.rmtree(d)


# ── live helpers over faked feeds ────────────────────────────────────────────

def test_run_one_call_attaches_both_variants():
    rows = [{"wallet": W(1), "first_ts": FIRST, "position": 1000.0}]
    seen = []

    def fake(pairs, conn=None, workers=10):
        seen.extend(pairs)
        return [{"trade_count": 3 if e == FIRST else 1, "volume_usdc": 50.0, "volume": 100.0, "day": e // DAY,
                 "cached": False} for _, e in pairs]

    with patched(net, "user_volumes", fake), contextlib.redirect_stdout(io.StringIO()):
        parity.run_one_call(rows, conn=None)
    assert seen == [(W(1), FIRST), (W(1), FIRST - DAY)]
    r = rows[0]
    assert r["uv"]["same_day"]["trade_count"] == 3 and r["uv"]["day_before"]["end"] == FIRST - DAY
    assert r["one_call"]["same_day"]["flagged"] and r["one_call"]["day_before"]["is_percent"]


def test_crawl_wallet_record():
    calls = []

    def fake(user, cutoff, max_pages=polymarket_api.HISTORY_MAX_PAGES):
        calls.append((user, cutoff))
        return history()

    with patched(polymarket_api, "fetch_user_history_up_to", fake):
        rec = parity.crawl_wallet(W(1), D0 + DAY - 1 + parity.CRAWL_MARGIN, {(FIRST, CID)})
    assert calls == [(W(1), D0 + DAY - 1 + parity.CRAWL_MARGIN)]
    assert rec["n_hist"] == 11 and rec["capped"] is False
    assert rec["cuts"][f"{FIRST}|{CID}"]["cut"]["tx"] == 5
    jobs = parity._wallet_jobs([{"wallet": W(1), "first_ts": FIRST, "condition_id": CID},
                                {"wallet": W(1), "first_ts": FIRST - 3 * DAY, "condition_id": CID}])
    assert jobs[W(1)]["until"] == D0 + DAY - 1 + parity.CRAWL_MARGIN and len(jobs[W(1)]["cuts"]) == 2


def _activity_pages(rows):
    return {"data": rows, "pagination": {"has_more": False, "next_cursor": None}}


def test_hybrid_prior_is_day_before_volume_plus_the_first_day():
    conn = store.connect(":memory:")
    seen = []

    def fake_get(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        seen.append((url.rsplit("/", 1)[-1], dict(params or {})))
        if url.endswith("/user-volume"):
            return {"data": {"volume": 10.0, "volume_usdc": 175.0, "trade_count": 3}}
        return _activity_pages([
            {"type": "TRADE", "timestamp": D0 + 10, "size": 20, "price": 0.5, "usdc_size": 10.0,
             "transaction_hash": "0xc"},
            {"type": "TRADE", "timestamp": FIRST, "size": 2000, "price": 0.5, "usdc_size": 1000.0,
             "transaction_hash": "0xe"},
            {"type": "TRADE", "timestamp": FIRST, "size": 1000, "price": 0.5, "usdc_size": 500.0,
             "transaction_hash": "0xe"},
        ])

    with patched(net, "get_json", fake_get), patched(polymarket_api, "get_json", fake_get):
        got = parity.hybrid_prior(W(1), FIRST, conn=conn)
    assert got["trade_count"] == 6 and got["volume_usdc"] == 1685.0 and got["day_rows"] == 3 and got["day_tx"] == 2
    assert seen[0] == ("user-volume", {"user": W(1), "start": 1, "end": FIRST - DAY})
    assert seen[1][0] == "activity" and seen[1][1]["start"] == D0 and seen[1][1]["end"] == FIRST
    assert seen[1][1]["type"] == "TRADE"


def test_v1_window_probe_predicts_what_the_window_held():
    def fake_get(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        if params["start"] == FIRST + 1:
            assert params["end"] == parity.V1_RUN_TS and "type" not in params
            return _activity_pages([{"type": "TRADE", "timestamp": FIRST + 9, "transaction_hash": "0x1"}] * 14_990)
        return _activity_pages([{"type": "TRADE", "timestamp": FIRST - i, "transaction_hash": f"0x{i // 2}"}
                                for i in range(30)] + [{"type": "REDEEM", "timestamp": 1, "transaction_hash": "0xr"}])

    with patched(polymarket_api, "get_json", fake_get):
        got = parity.v1_window_probe(W(1), FIRST)
    # 15 000 - 14 990 = 10 rows of window left before the cutoff: 10 fills in 5 transactions
    assert got == {"rows_after_cut_to_run": 14_990, "probe_capped": False, "predicted_v1_count": 5,
                   "window_rows_before_cut": 10}

    def full(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        return _activity_pages([{"type": "TRADE", "timestamp": FIRST + 9, "transaction_hash": "0x1"}] * 15_000)

    with patched(polymarket_api, "get_json", full):
        assert parity.v1_window_probe(W(1), FIRST)["predicted_v1_count"] == 0


def test_offline_suite_never_touches_the_network():
    with patched(polymarket_api, "get_json", no_network), patched(net, "get_json", no_network):
        test_window_stats_cut_points()
        test_explain_v1_undercount_sides_with_the_one_call_answer()


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
