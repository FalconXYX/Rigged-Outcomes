#!/usr/bin/env python3
"""
Offline tests for the open list (watchlist.open_scan): no network, a
second or two.

    python3 -m pytest -q tests/test_watchlist_open.py     # or
    python3 tests/test_watchlist_open.py                  # same checks, exits 1 on failure

Covers started_signal (net BUY$ - SELL$ per wallet and outcome inside the
window, the whale_threshold of the market's volume, the 0.99 near-certain
rule, >= 2 flagged wallets AND >= $10,000, flag_fn called with the first
buy in the window and the net position), the event-level rule across
markets, rank_events (started first by flagged $, then the track
record's Wilson lower bound, then n, None last, then a scheduled reveal
within 14 days, the sooner end, the event id; inputs untouched), rotate
(>= 3 new when possible, nothing stale above a better event, started
events never dropped, the top-up when fewer than 3 new exist, invariants
over random rankings), the event's type / reveal / end, address
scrubbing, MethodFlags on a faked method.lookup_priors, the open_lists
table (complete vs sample, "yesterday" = the latest complete day before
today), card text (every number named, --window-hours in every
sentence, the legend covers every card key), AI failures (cached labels
and their exclusions kept, failed markets marked), one track-record
snapshot per run, the CLI's --db / --out rules, and whole scans with
every network piece faked, against a store whose ended events are
written by the closed scan's own writers.
"""
import contextlib
import io
import json
import os
import random
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from watchlist import ai, closed_scan, method, store  # noqa: E402
from watchlist import open_scan as op  # noqa: E402
from watchlist import track_record as tr  # noqa: E402
from watchlist.common import DAY, iso, to_epoch  # noqa: E402

NOW = to_epoch("2026-11-10T12:00:00Z")
W0, W1 = NOW - 72 * 3600, NOW
A, B, C, D = ("0x" + c * 40 for c in "abcd")


@contextlib.contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def t(wallet, side, usd, price=0.5, ts=None, outcome="Yes", idx=None):
    """One v2 trade row worth `usd` dollars at `price`."""
    return {"proxy_wallet": wallet, "side": side, "outcome": outcome,
            "outcome_index": idx if idx is not None else (0 if outcome == "Yes" else 1),
            "size": usd / price, "price": price, "timestamp": ts if ts is not None else NOW - 3600}


def flag_all(calls=None, flagged=None):
    """flag_fn: every wallet flagged (or only those in `flagged`), calls recorded."""
    def fn(wallet, first_ts, position):
        if calls is not None:
            calls.append((wallet, first_ts, round(position, 2)))
        return {"flagged": flagged is None or wallet in flagged, "prior_trade_count": 1}
    return fn


# ── started_signal ───────────────────────────────────────────────────────────

def test_positions_net_buy_minus_sell_per_wallet_and_outcome():
    rows = [t(A, "BUY", 8_000, ts=NOW - 7200), t(A, "BUY", 4_000, price=0.6, ts=NOW - 3600),
            t(A, "SELL", 3_000, price=0.7), t(A, "BUY", 1_000, outcome="No"), t(A, "SELL", 500, outcome="No")]
    pos = {p["outcome"]: p for p in op.positions(rows, W0, W1)}
    yes, no = pos["Yes"], pos["No"]
    assert abs(yes["net_usd"] - 9_000) < 1e-6 and abs(yes["buy_usd"] - 12_000) < 1e-6
    assert abs(yes["sell_usd"] - 3_000) < 1e-6 and yes["n_buys"] == 2 and yes["n_sells"] == 1
    # avg buy price is dollars per share over the buys: 12,000 / (16,000 + 6,666.67)
    assert abs(yes["avg_buy_price"] - 12_000 / (16_000 + 4_000 / 0.6)) < 1e-9
    assert yes["first_buy_ts"] == NOW - 7200 and yes["last_buy_ts"] == NOW - 3600
    assert abs(no["net_usd"] - 500) < 1e-6           # the other outcome is its own position


def test_window_is_start_inclusive_end_exclusive():
    rows = [t(A, "BUY", 1_000, ts=W0 - 1), t(A, "BUY", 2_000, ts=W0), t(A, "BUY", 4_000, ts=W1 - 1),
            t(A, "BUY", 8_000, ts=W1), t(A, "SELL", 16_000, ts=W0 - 100)]
    (p,) = op.positions(rows, W0, W1)
    assert abs(p["net_usd"] - 6_000) < 1e-6 and p["first_buy_ts"] == W0 and p["last_buy_ts"] == W1 - 1


def test_big_buyer_threshold_follows_market_volume():
    # whale_threshold: clamp(0.1 % of volume, $1k, $5k)
    for volume, thr in ((100_000, 1_000), (2_000_000, 2_000), (50_000_000, 5_000)):
        rows = [t(A, "BUY", thr - 0.01), t(B, "BUY", thr)]
        sig = op.started_signal(rows, volume, flag_all(), W0, W1)
        assert [b["wallet"] for b in sig["big_buyers"]] == [B], (volume, sig["big_buyers"])
        assert sig["threshold"] == thr


def test_big_buyer_is_net_not_gross():
    rows = [t(A, "BUY", 30_000), t(A, "SELL", 29_500)]          # $500 net on a $1k threshold
    assert op.started_signal(rows, 200_000, flag_all(), W0, W1)["big_buyers"] == []


def test_near_certain_buys_are_not_big_buyers():
    rows = [{"proxy_wallet": A, "side": "BUY", "outcome": "Yes", "outcome_index": 0, "size": 30_000, "price": 0.99,
             "timestamp": NOW - 60},                             # avg exactly 0.99: a sweep, left out
            t(B, "BUY", 20_000, price=0.989)]
    sig = op.started_signal(rows, 1_000_000, flag_all(), W0, W1)
    assert [b["wallet"] for b in sig["big_buyers"]] == [B]


def test_started_needs_two_flagged_wallets():
    rows = [t(A, "BUY", 50_000)]
    sig = op.started_signal(rows, 1_000_000, flag_all(), W0, W1)
    assert sig["flagged_usd"] == 50_000 and sig["wallets"] == [A] and not sig["started"]
    # the same wallet on both outcomes is still one wallet
    rows.append(t(A, "BUY", 20_000, outcome="No", price=0.3))
    sig = op.started_signal(rows, 1_000_000, flag_all(), W0, W1)
    assert sig["wallets"] == [A] and not sig["started"] and len(sig["big_buyers"]) == 2


def test_started_needs_10k_flagged():
    sig = op.started_signal([t(A, "BUY", 5_000), t(B, "BUY", 4_999)], 1_000_000, flag_all(), W0, W1)
    assert not sig["started"] and len(sig["wallets"]) == 2 and abs(sig["flagged_usd"] - 9_999) < 0.01
    sig = op.started_signal([t(A, "BUY", 5_000), t(B, "BUY", 5_000)], 1_000_000, flag_all(), W0, W1)
    assert sig["started"] and sig["flagged_usd"] == 10_000


def test_unflagged_big_buyers_do_not_count():
    rows = [t(A, "BUY", 30_000), t(B, "BUY", 30_000), t(C, "BUY", 2_000)]
    sig = op.started_signal(rows, 1_000_000, flag_all(flagged={A, C}), W0, W1)
    assert sig["wallets"] == [A, C] and sig["flagged_usd"] == 32_000 and sig["started"]
    assert [b["flagged"] for b in sig["big_buyers"]] == [True, False, True]
    sig = op.started_signal(rows, 1_000_000, flag_all(flagged={A}), W0, W1)
    assert not sig["started"] and sig["flagged_usd"] == 30_000


def test_flag_fn_gets_first_buy_in_window_and_net_position():
    calls = []
    rows = [t(A, "BUY", 9_000, ts=W0 - 500),                   # before the window: ignored
            t(A, "BUY", 6_000, ts=W0 + 100), t(A, "BUY", 2_000, ts=W0 + 50), t(A, "SELL", 1_000, ts=W0 + 900),
            t(B, "BUY", 500)]                                    # below the threshold: never looked up
    op.started_signal(rows, 1_000_000, flag_all(calls), W0, W1)
    assert calls == [(A, W0 + 50, 7_000.0)], calls


def test_event_signal_combines_markets():
    m1 = op.started_signal([t(A, "BUY", 6_000)], 1_000_000, flag_all(), W0, W1)
    m2 = op.started_signal([t(B, "BUY", 6_000)], 1_000_000, flag_all(), W0, W1)
    assert not m1["started"] and not m2["started"]
    ev = op.event_signal([m1, m2])
    assert ev["started"] and ev["wallets"] == [A, B] and ev["flagged_usd"] == 12_000
    # the same wallet in two markets counts once
    m3 = op.started_signal([t(A, "BUY", 6_000)], 1_000_000, flag_all(), W0, W1)
    ev = op.event_signal([m1, m3])
    assert not ev["started"] and ev["wallets"] == [A] and ev["flagged_usd"] == 12_000


# ── rank_events ──────────────────────────────────────────────────────────────

def entry(eid, started=False, usd=0.0, rate=None, kind="any_day", reveal_ts=None, end_ts=NOW + 30 * DAY, k=None,
          n=None):
    """A rank entry. k / n: a track record as track_record writes it (rate, Wilson low / high); rate alone:
    a hand-built record without an interval."""
    if n is not None:
        low, high = tr.wilson(k, n)
        track = {"basis": "type", "key": "t", "k": k, "n": n, "rate": round(k / n, 4), "low": round(low, 4),
                 "high": round(high, 4)}
    else:
        track = {"rate": rate, "basis": "type" if rate is not None else "none"}
    return {"event_id": eid, "title": eid, "end_ts": end_ts, "reveal_kind": kind, "reveal_ts": reveal_ts,
            "started": started, "started_flagged_usd": usd, "started_wallets": 2 if started else 0, "track": track}


def ids(lst):
    return [e["event_id"] for e in lst]


def test_rank_started_first_by_flagged_usd():
    es = [entry("r1", rate=0.9), entry("s1", True, 20_000, rate=0.0), entry("s2", True, 80_000),
          entry("r2", rate=0.5)]
    r = op.rank_events(es, now_ts=NOW)
    assert ids(r["top"]) == ["s2", "s1", "r1", "r2"] and r["next"] == [] and r["rest"] == []


def test_rank_rate_desc_none_last():
    es = [entry("none1"), entry("low", rate=0.1), entry("zero", rate=0.0), entry("high", rate=0.6)]
    assert ids(op.rank_events(es, now_ts=NOW)["top"]) == ["high", "low", "zero", "none1"]


def test_rank_by_wilson_low_bound_then_n():
    es = [entry("tiny_series", k=1, n=3),          # 33%, interval 6-79%
          entry("big_type", k=33, n=169),          # 20%, interval 14-26%
          entry("zero_small", k=0, n=2), entry("zero_big", k=0, n=50), entry("none1"),
          entry("half", k=2, n=4)]                 # 50%, interval 15-85%
    got = ids(op.rank_events(es, now_ts=NOW)["top"])
    # by the lower bound, not the rate (the rate would put half and tiny_series first); equal bounds: larger n
    assert got == ["half", "big_type", "tiny_series", "zero_big", "zero_small", "none1"], got
    by_rate = sorted((e for e in es if e["track"]["rate"] is not None), key=lambda e: -e["track"]["rate"])
    assert ids(by_rate)[:2] == ["half", "tiny_series"]
    # k and n without a stored low: the bound is computed; a bare rate falls back to the rate
    assert abs(op.track_low({"k": 1, "n": 3, "rate": 0.33}) - tr.wilson(1, 3)[0]) < 1e-12
    assert op.track_low({"rate": 0.4}) == 0.4 and op.track_low({}) == 0.0 and op.track_low(None) == 0.0
    # the interface the backtest uses is unchanged: entries in, {"top", "next", "rest"} out, entries untouched
    snap = json.dumps(es, sort_keys=True)
    r = op.rank_events(es, top=2, next_n=3, now_ts=NOW)
    assert ids(r["top"]) == ["half", "big_type"] and len(r["next"]) == 3 and len(r["rest"]) == 1
    assert json.dumps(es, sort_keys=True) == snap


def test_rank_ties_scheduled_within_14_days_then_end_then_id():
    soon = NOW + 5 * DAY
    es = [entry("z_any", rate=0.2, end_ts=NOW + DAY),
          entry("y_far", rate=0.2, kind="scheduled", reveal_ts=NOW + 15 * DAY, end_ts=NOW + 2 * DAY),
          entry("x_soon", rate=0.2, kind="scheduled", reveal_ts=soon, end_ts=NOW + 20 * DAY),
          entry("w_today", rate=0.2, kind="scheduled", reveal_ts=NOW - 11 * 3600, end_ts=NOW + 25 * DAY),
          entry("v_past", rate=0.2, kind="scheduled", reveal_ts=NOW - 2 * DAY, end_ts=NOW + 3 * DAY),
          entry("b_same", rate=0.2, end_ts=NOW + 4 * DAY), entry("a_same", rate=0.2, end_ts=NOW + 4 * DAY)]
    got = ids(op.rank_events(es, now_ts=NOW)["top"])
    # scheduled today or within 14 days first (sooner end first among them), then by end, then id
    assert got == ["x_soon", "w_today", "z_any", "y_far", "v_past", "a_same", "b_same"], got
    assert op.scheduled_soon(entry("e", kind="scheduled", reveal_ts=NOW + 14 * DAY), NOW)
    assert not op.scheduled_soon(entry("e", kind="any_day", reveal_ts=NOW + DAY), NOW)


def test_rank_split_and_purity():
    es = [entry(f"e{i:02d}", rate=i / 100) for i in range(25)]
    snapshot = json.dumps(es, sort_keys=True)
    r = op.rank_events(es, top=10, next_n=10, now_ts=NOW)
    assert ids(r["top"]) == [f"e{i:02d}" for i in range(24, 14, -1)]
    assert ids(r["next"]) == [f"e{i:02d}" for i in range(14, 4, -1)] and ids(r["rest"]) == ["e04", "e03", "e02",
                                                                                            "e01", "e00"]
    assert json.dumps(es, sort_keys=True) == snapshot
    assert op.rank_events([], now_ts=NOW) == {"top": [], "next": [], "rest": []}


# ── rotate ───────────────────────────────────────────────────────────────────

def test_rotate_keeps_at_most_seven_in_rank_order():
    prev = [f"p{i}" for i in range(10)]
    ranked = ["p9", "p0", "p1", "n1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "n2", "n3", "n4"]
    got = op.rotate(prev, ranked)
    # walking the ranking: the first 7 of yesterday's, every new one, stop at 10
    assert got == ["p9", "p0", "p1", "n1", "p2", "p3", "p4", "p5", "n2", "n3"], got
    assert sum(1 for i in got if i in prev) == 7 and sum(1 for i in got if i not in prev) == 3


def test_rotate_at_least_three_new_when_possible():
    prev = [f"p{i}" for i in range(10)]
    # new ones get in even when every one of yesterday's still ranks above them
    got = op.rotate(prev, prev + ["n1", "n2", "n3", "n4"])
    assert got == ["p0", "p1", "p2", "p3", "p4", "p5", "p6", "n1", "n2", "n3"], got
    # more new ones ranked high: more of them, fewer of yesterday's (keep_max is a cap, not a quota)
    got = op.rotate(prev, ["n1", "p0", "n2", "n3", "n4", "p1", "n5", "p2", "n6", "n7", "p3"])
    assert got == ["n1", "p0", "n2", "n3", "n4", "p1", "n5", "p2", "n6", "n7"], got


def test_rotate_never_keeps_a_stale_event_above_better_ones():
    prev = [f"p{i}" for i in range(10)]
    # yesterday's whole list fell to the bottom: none of it is kept over 10 better new events
    ranked = [f"n{i}" for i in range(20)] + prev
    assert op.rotate(prev, ranked) == [f"n{i}" for i in range(10)]
    # the old rule (the 7 best of yesterday's wherever they rank) kept p0..p6 at ranks 9-15 over n3..n7
    ranked = [f"n{i}" for i in range(8)] + prev
    got = op.rotate(prev, ranked)
    assert got == ["n0", "n1", "n2", "n3", "n4", "n5", "n6", "n7", "p0", "p1"], got


def test_rotate_started_events_are_never_dropped():
    started = [f"s{i}" for i in range(8)]            # 8 started events, all on yesterday's list
    prev = started + ["p8", "p9"]
    ranked = started + ["n0", "n1", "n2", "n3"] + ["p8", "p9"]
    got = op.rotate(prev, ranked, pinned=started)
    assert got == started + ["n0", "n1"], got       # all 8 kept (the cap of 7 gives way), then the best new
    # without the pin the cap would drop s7 for the lower-ranked n2
    assert op.rotate(prev, ranked) == started[:7] + ["n0", "n1", "n2"]
    # a started event below the cut is not pulled up: it is never dropped for a LOWER-ranked one only
    ranked = [f"n{i}" for i in range(10)] + ["s9"]
    assert op.rotate([], ranked, pinned=["s9"]) == ranked[:10]
    # pinned old events count towards the cap: 2 pinned + 5 more of yesterday's
    prev = [f"p{i}" for i in range(10)]
    got = op.rotate(prev, prev + ["n1", "n2", "n3"], pinned=["p0", "p1"])
    assert got == ["p0", "p1", "p2", "p3", "p4", "p5", "p6", "n1", "n2", "n3"], got


def test_rotate_tops_up_when_fewer_than_three_new():
    prev = [f"p{i}" for i in range(10)]
    got = op.rotate(prev, prev + ["n1"])
    # 7 of yesterday's + the only new one, then topped up with yesterday's next-ranked: 10 whenever 10 exist
    assert got == ["p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "n1"], got
    assert op.rotate(prev, prev) == prev                                       # no new event at all
    prev = ["a", "b", "c", "d", "e", "f", "g", "h"]
    assert op.rotate(prev, list("abcdefgh") + ["n"]) == ["a", "b", "c", "d", "e", "f", "g", "h", "n"]
    assert op.rotate(prev, list("abcdefgh")) == list("abcdefgh")             # fewer than 10 candidates


def test_rotate_fills_with_more_new_when_old_ones_fell_out():
    prev = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    ranked = ["x1", "c", "x2", "x3", "a", "x4", "x5", "x6", "x7", "x8", "x9"]     # 8 of yesterday's are gone
    got = op.rotate(prev, ranked)
    assert got == ["x1", "c", "x2", "x3", "a", "x4", "x5", "x6", "x7", "x8"]


def test_rotate_invariants_on_random_rankings():
    rng = random.Random(416)
    for _ in range(400):
        pool = [f"e{i}" for i in range(rng.randint(0, 25))]
        rng.shuffle(pool)
        prev = rng.sample(pool, min(len(pool), rng.randint(0, 10))) + ["gone1", "gone2"]
        pinned = set(rng.sample(pool, min(len(pool), rng.randint(0, 3))))
        got = op.rotate(prev, pool, pinned=pinned)
        rank = {e: i for i, e in enumerate(pool)}
        new = [e for e in pool if e not in prev]
        assert got == sorted(got, key=rank.get) and len(set(got)) == len(got)            # rank order, no repeats
        assert len(got) == min(10, len(pool))                                             # full whenever possible
        n_old = sum(1 for e in got if e in prev)
        # >= 3 new when possible; fewer only when pinned (started) events of yesterday's pushed past the cap
        assert len(got) - n_old >= min(3, len(new)) or (n_old > 7 and any(e in pinned for e in got if e in prev))
        # nothing chosen sits below a new event that was left out, and then the cap of 7 held for the unpinned
        left_new = [rank[e] for e in new if e not in got]
        if left_new:
            assert all(rank[e] < min(left_new) for e in got)
            assert sum(1 for e in got if e in prev and e not in pinned) <= 7
        # a pinned event is only ever left out below the cut (all 10 chosen rank above it)
        for p in pinned - set(got):
            assert len(got) == 10 and all(rank[e] < rank[p] for e in got)


def test_rotate_first_day_and_duplicates():
    ranked = [f"e{i}" for i in range(15)]
    assert op.rotate(None, ranked) == ranked[:10]
    assert op.rotate([], ["a", "a", "b"]) == ["a", "b"]
    assert op.rotate(["x"], [1, 2, 3], size=2) == ["1", "2"]
    assert op.rotate([], ["a", "b", "c"]) == ["a", "b", "c"]
    assert op.rotate(["a"], []) == []


# ── event facts ──────────────────────────────────────────────────────────────

def mk(mid, vol, end="2026-12-01T00:00:00Z", q=None, prices=("0.3", "0.7")):
    return {"id": mid, "conditionId": "0x" + mid.rjust(64, "0"), "question": q or f"Market {mid}?",
            "slug": f"market-{mid}", "volumeNum": vol, "endDate": end, "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps(list(prices)), "active": True, "closed": False}


def lab(type_, kind="any_day", date=None, quote="the rules say so here"):
    return {"type": type_, "reveal_kind": kind, "reveal_date": date, "quote": quote, "excluded": False}


def test_event_type_reveal_end():
    ms = [mk("1", 300_000, "2026-12-20T00:00:00Z"), mk("2", 900_000, "2026-12-05T00:00:00Z"),
          mk("3", 100_000, "2026-11-30T00:00:00Z")]
    labels = {"1": lab("court_regulator", "scheduled", "2026-11-25"), "2": lab("election_vote"),
              "3": lab("court_regulator", "scheduled", "2026-11-20")}
    assert op.event_type(ms, labels)[0] == "election_vote"           # the highest-volume market's type
    assert op.event_type(ms, {"1": labels["1"]})[0] == "court_regulator"   # highest-volume LABELLED one
    assert op.event_type(ms, {}) == (None, None)
    r = op.event_reveal(ms, labels)
    assert r["kind"] == "scheduled" and r["date"] == "2026-11-20" and r["market_id"] == "3"
    assert r["ts"] == to_epoch("2026-11-20")
    r = op.event_reveal(ms, {"1": lab("x", "unclear"), "2": lab("x", "any_day"), "3": lab("x", "any_day")})
    assert r["kind"] == "any_day" and r["market_id"] == "2" and r["ts"] is None
    r = op.event_reveal(ms, {"1": lab("x", "revealed_awaiting_resolution", "2026-11-03")})
    assert r["kind"] == "revealed_awaiting_resolution" and r["date"] == "2026-11-03"
    assert op.event_reveal(ms, {})["kind"] == "unclear"
    assert op.event_end(ms) == to_epoch("2026-11-30T00:00:00Z")


# ── MethodFlags ──────────────────────────────────────────────────────────────

def test_method_flags_uses_lookup_priors_and_flag_whale():
    seen = []

    def fake_lookup(rows, variant, conn=None, workers=10):
        seen.append(([(r["wallet"], r["first_ts"], r["position"]) for r in rows], variant))
        out = []
        for r in rows:
            n, usd = {A: (3, 100.0), B: (500, 1_000_000.0), C: (50, 2_000.0)}[r["wallet"]]
            out.append({"trade_count": n, "volume_usdc": usd, "end": r["first_ts"] - DAY, "counts_exact": True,
                        "decided_by": "exact_check", "bounds": None, "exact_check": None, "cached": False})
        return out

    with patched(method, "lookup_priors", fake_lookup):
        f = op.MethodFlags(conn=None)
        f.prefetch([(A, 100, 5_000.0), (B, 200, 5_000.0), (C, 300, 9_000.0), (A, 100, 5_000.0)])
        assert seen == [([(A, 100, 5_000.0), (B, 200, 5_000.0), (C, 300, 9_000.0)], "exact")]
        a, b, c = f(A, 100, 5_000.0), f(B, 200, 5_000.0), f(C, 300, 9_000.0)
        assert len(seen) == 1                                     # answered from the prefetch
        assert a["flagged"] and a["is_first_x"] and not b["flagged"]
        assert c["flagged"] and c["is_percent"] and not c["is_first_x"] and c["concentration"] == round(
            9_000 / 9_000 * 100, 2)
        assert c == {**c, "prior_trade_count": 50, "prior_volume_usdc": 2_000.0, "cutoff_ts": 300}
        f(A, 999, 5_000.0)                                        # not prefetched: looks itself up
        assert seen[-1] == ([(A, 999, 5_000.0)], "exact")


def test_method_flags_one_failed_lookup_does_not_void_the_others():
    seen = []

    def fake_lookup(rows, variant, conn=None, workers=10):
        seen.append([r["wallet"] for r in rows])
        if any(r["wallet"] == B for r in rows):
            raise RuntimeError("HTTP 400 from /v2/user-volume")
        return [{"trade_count": 2, "volume_usdc": 10.0, "end": 0, "counts_exact": True, "decided_by": "agree",
                 "bounds": None, "exact_check": None, "cached": False} for _ in rows]

    with patched(method, "lookup_priors", fake_lookup):
        f = op.MethodFlags(conn=None)
        rows = [t(A, "BUY", 6_000), t(B, "BUY", 50_000), t(C, "BUY", 6_000)]
        bigs = op.big_buyers(rows, 1_000_000, W0, W1)
        f.prefetch([(x["wallet"], x["first_buy_ts"], x["net_usd"]) for x in bigs])
        assert seen == [[B, A, C], [B], [A], [C]], seen          # the batch, then one by one
        sig = op.started_signal(rows, 1_000_000, f, W0, W1)
        assert sig["started"] and sig["wallets"] == [A, C] and sig["flagged_usd"] == 12_000
        bad = [x for x in sig["big_buyers"] if x["flag"].get("error")]
        assert [x["wallet"] for x in bad] == [B] and not bad[0]["flagged"] and "400" in bad[0]["flag"]["error"]
        assert len(seen) == 4                                     # answered from the memo, errors included


def test_check_event_reports_lookup_errors_without_addresses_in_the_sentence():
    def flag(wallet, first_ts, position):
        return {"flagged": False, "error": f"PolymarketAPIError: HTTP 400 for user={wallet}"}

    g = {"event_id": "1", "event": {"title": "E?", "slug": "e"}, "markets": [mk("11", 1_000_000)]}
    window = {"from": W0, "to": W1, "hours": 72}
    c = op.check_event(g, flag, window, lambda cid, s, u: ([t(A, "BUY", 20_000)], False))
    assert c["status"] == "error" and "look-up failed for 1 big buyer (" in c["errors"][0]
    assert A not in c["errors"][0] and "user=<wallet>" in c["errors"][0]
    e = op.build_entry(g, {"11": lab("other")}, {"basis": "none", "series_n": 0}, None, c, NOW, NOW)
    st = e["card"]["already_started"]
    assert not e["started"] and A not in json.dumps(st) and st["sentence"].startswith("Not known")
    assert "(1 problem, listed under errors)" in st["sentence"]
    assert e["card"]["markets"][0]["url"] == "https://polymarket.com/event/e/market-11"


def test_track_sentence_names_its_numbers():
    s = op.track_sentence({"basis": "type", "key": "court_regulator", "k": 0, "n": 2, "low": 0.0, "high": 0.66,
                           "k_big": 0, "series_n": 1}, "court_regulator")
    assert s.startswith("0 of 2 ended court_regulator events had ≥ 10% of their whale capital flagged"), s
    assert "of those 0" not in s and "fewer than 3" in s and "range 0–66%" in s and "only 1 ended event " in s
    s = op.track_sentence({"basis": "series", "key": "fed", "k": 2, "n": 5, "low": 0.1, "high": 0.8, "k_big": 1,
                           "series_n": 5}, "data_release")
    assert "in this event's series (fed)" in s and "1 of those 2 had ≥ $10,000 flagged" in s
    assert "Wilson 95% interval of 2 of 5" in s
    ev = {"slug": "solo"}
    assert op._market_url(ev, {"slug": "solo"}) == "https://polymarket.com/event/solo"
    assert op._market_url(ev, {"slug": "rung"}) == "https://polymarket.com/event/solo/rung"


def test_no_track_record_sentence_does_not_contradict_itself():
    # the old text said "no ended events (... series with 2 ended events)"
    s = op.track_sentence({"basis": "none", "series_n": 2}, "court_regulator")
    assert s == ("No track record: the store has no ended court_regulator events, and its series has only 2 ended "
                 "events (a series' own record is used from 3)."), s
    s = op.track_sentence({"basis": "none", "series_n": 0}, None)
    assert s == "No track record: the event has no AI type, and no ended event shares its series.", s
    s = op.track_sentence({"basis": "none", "series_n": 1}, None, label_failed=True)
    assert s.startswith("No track record: the event's type is not known (the AI label failed this run), and its "
                        "series has only 1 ended event (")
    assert "no ended events" not in s


def test_why_line_names_every_number():
    low, high = tr.wilson(18, 49)
    e = {"event_id": "1", "started": False, "reveal_kind": "any_day", "reveal_ts": None,
         "track": {"basis": "type", "key": "company_product", "k": 18, "n": 49, "rate": round(18 / 49, 4),
                   "low": round(low, 4), "high": round(high, 4)}}
    assert op.why_ranked(e, NOW) == ("Track record: 18 of 49 ended company_product events had ≥ 10% flagged capital "
                                     "(range 25–51%)")
    e["track"] = {"basis": "series", "key": "fed", "k": 1, "n": 3, "rate": 0.3333, "low": 0.0615, "high": 0.7923}
    e.update(reveal_kind="scheduled", reveal_ts=NOW + 3 * DAY)
    assert op.why_ranked(e, NOW) == ("Track record: 1 of 3 ended events in this event's series (fed) had ≥ 10% "
                                     "flagged capital (range 6–79%); reveal scheduled for 2026-11-13, within 14 days")
    e = {"event_id": "2", "started": True, "started_flagged_usd": 25_000, "started_wallets": 3, "track": {}}
    assert op.why_ranked(e, NOW, hours=24) == ("Already started: 3 wallets the method flags bought $25,000 net (buys "
                                               "minus sells) in the last 24 h")
    e = {"event_id": "3", "started": False, "track": {"basis": "none", "rate": None, "series_n": 2},
         "type": "other", "label_failed": 0}
    assert op.why_ranked(e, NOW) == ("No track record: the store has no ended other events, and its series has only "
                                     "2 ended events (a series' own record is used from 3)")


def test_scrub_removes_wallet_addresses_only():
    cid = "0x" + "c" * 64
    s = op.scrub(f"HTTP 400 for /v2/user-volume?user={A}&start=1 on {cid} (also {B.upper()[2:]} and 0X{C[2:]})")
    assert A not in s and "user=<wallet>&start=1" in s and cid in s            # a condition id is not a wallet
    assert "0X" + C[2:] not in s and B.upper()[2:] in s                        # no 0x: not an address
    long_hex = "0x" + "a" * 80                                                 # 80 hex in a row is not an address
    assert op.scrub(f"[{A}]") == "[<wallet>]" and op.scrub(long_hex) == long_hex
    # scrubbed before cutting: a cut never leaves half an address behind
    s = op.scrub("x" * 50 + A, 60)
    assert s == "x" * 50 + "<wallet>" and "0xaaaa" not in s
    assert op.scrub(None) == "None" and op.scrub(12) == "12"


def test_error_text_on_cards_carries_no_addresses():
    long_tail = " z" * 120

    def fetch(cid, s, u):
        if cid.endswith("12"):
            raise RuntimeError(f"HTTP 429 for /v2/trades?user={B}{long_tail}")
        return [t(A, "BUY", 20_000)], False

    def flag(wallet, first_ts, position):
        return {"flagged": False, "error": f"PolymarketAPIError: HTTP 400 for user={wallet}{long_tail} {C}"}

    g = {"event_id": "1", "event": {"title": "E?", "slug": "e"}, "markets": [mk("11", 1_000_000), mk("12", 500_000)]}
    c = op.check_event(g, flag, {"from": W0, "to": W1, "hours": 72}, fetch)
    assert c["status"] == "error" and len(c["errors"]) == 2
    e = op.build_entry(g, {}, {"basis": "none", "series_n": 0}, None, c, NOW, NOW)
    card = json.dumps({k: v for k, v in e["card"].items() if k != "flagged_wallets"})
    for w in (A, B, C):
        assert w not in card and w[:12] not in card, w
    assert "<wallet>" in card
    # MethodFlags' own error text is scrubbed too
    def boom(rows, variant, conn=None, workers=10):
        raise RuntimeError(f"HTTP 400 from /v2/user-volume?user={rows[0]['wallet']}")
    with patched(method, "lookup_priors", boom):
        f = op.MethodFlags(conn=None)
        assert A not in f(A, 100, 5_000.0)["error"]


# ── open_lists ───────────────────────────────────────────────────────────────

def test_open_lists_complete_vs_sample():
    conn = store.connect(":memory:")
    assert op.previous_list(conn, "2026-11-10") == (None, [])
    op.save_list(conn, "2026-11-08", True, [entry("a"), entry("b")], [entry("c")], {"a": "new", "b": "new"})
    op.save_list(conn, "2026-11-09", False, [entry("x")], [], {"x": "new"})        # a sample: never "yesterday"
    assert op.previous_list(conn, "2026-11-10") == ("2026-11-08", ["a", "b"])
    op.save_list(conn, "2026-11-09", True, [entry("d"), entry("a")], [], {"d": "new", "a": "kept"})
    assert op.previous_list(conn, "2026-11-10") == ("2026-11-09", ["d", "a"])
    assert op.previous_list(conn, "2026-11-09") == ("2026-11-08", ["a", "b"])     # strictly before the day
    op.save_list(conn, "2026-11-09", True, [entry("e")], [], {"e": "new"})        # a rerun replaces the day
    assert op.previous_list(conn, "2026-11-10") == ("2026-11-09", ["e"])
    n = conn.execute("SELECT COUNT(*) FROM open_lists WHERE day='2026-11-09'").fetchone()[0]
    assert n == 2                                                                  # the sample + the rerun


# ── the whole scan, every network piece faked ────────────────────────────────

def closed_event(conn, eid, type_, pct, closed_ts, series=None, title=None):
    """An ended event as the closed scan writes it (store writers + refresh_events)."""
    title = title or f"Closed {eid}?"
    event = {"id": eid, "title": title, "slug": f"closed-{eid}", **({"seriesSlug": series} if series else {})}
    c = "0x" + str(eid).encode().hex().rjust(64, "0")
    m = {"conditionId": c, "id": f"cm{eid}", "question": title, "slug": f"cm-{eid}", "closedTime": iso(closed_ts),
         "volumeNum": 500_000, "events": [event]}
    store.save_market(conn, m, "kept", f"ai: {type_}", winner="Yes")
    store.save_result(conn, {"condition_id": c, "cutoff_variant": "exact", "status": "ok", "market_id": m["id"],
                             "event_id": eid, "closed_ts": closed_ts, "whale_count": 10, "whale_capital": 100_000,
                             "flagged_count": 2, "flagged_capital": pct * 1_000, "flagged_pct": pct,
                             "jump_ts": None, "flagged_capital_before_jump": None, "whales": []})
    closed_scan.refresh_events(conn, {eid: event}, "exact")


def open_event(eid, title, markets, series=None):
    return {"id": eid, "title": title, "slug": f"open-{eid}", "markets": markets,
            **({"seriesSlug": series} if series else {})}


class FakeFlags:
    """flag_fn with .prefetch(): every wallet but D flagged."""

    def __init__(self):
        self.calls = []

    def prefetch(self, items):
        self.calls.append(list(items))

    def __call__(self, wallet, first_ts, position):
        return {"flagged": wallet != D, "prior_trade_count": 2, "prior_volume_usdc": 10.0,
                "concentration": 99.0, "is_first_x": wallet != D, "is_percent": True,
                "decided_by": "agree", "prior_counts_exact": False}


OPEN_LABELS = {"101": lab("company_product"), "102": lab("company_product"),
               "201": lab("court_regulator"), "301": lab("data_release", "scheduled", "2026-11-15"),
               "401": lab("election_vote"), "501": lab("weather"),
               "701": lab("court_regulator", "scheduled", "2026-11-18", quote="ruling on November 18, 2026")}


@contextlib.contextmanager
def scan_fixture():
    """A store with ended events written by the closed scan's own writers, the open events, and a fake
    trade fetcher: (conn, db_path, open events, fetch, fetched calls)."""
    tr.clear_cache()
    tmp = tempfile.mkdtemp()
    try:
        db = os.path.join(tmp, "w.db")
        conn = store.connect(db)
        t0 = NOW - 20 * DAY
        # ended events: court_regulator 2 of 4 heavy; election_vote 0 of 2; series "fed" 1 of 3 heavy
        for i, pct in enumerate((50, 12, 1, 0)):
            closed_event(conn, f"c{i}", "court_regulator", pct, t0 + i * 3600)
        for i in range(2):
            closed_event(conn, f"e{i}", "election_vote", 0, t0 + i * 3600)
        for i, pct in enumerate((30, 2, 3)):
            closed_event(conn, f"f{i}", "data_release", pct, t0 + i * 3600, series="fed")
        tr.clear_cache()
        ev = [
            open_event("100", "Started event?", [mk("101", 2_000_000), mk("102", 400_000, q="Second market?")]),
            open_event("200", "Court case?", [mk("201", 300_000, "2026-12-30T00:00:00Z")]),
            open_event("300", "Fed decision?", [mk("301", 500_000)], series="fed"),
            open_event("400", "Election?", [mk("401", 800_000)]),
            open_event("500", "Weather?", [mk("501", 300_000, q="Rain in NYC?")]),              # AI excludes it
            open_event("600", "Decided?", [mk("601", 900_000, prices=("0.995", "0.005"))]),     # pool drops it
            open_event("700", "Court soon?", [mk("701", 200_000, "2026-12-29T00:00:00Z")]),
        ]
        trades = {"0x" + "101".rjust(64, "0"): [t(A, "BUY", 9_000, price=0.2), t(B, "BUY", 1_500, price=0.25)],
                  "0x" + "102".rjust(64, "0"): [t(C, "BUY", 4_000, price=0.4, outcome="No")],
                  "0x" + "401".rjust(64, "0"): [t(D, "BUY", 30_000, price=0.5)]}
        fetched = []

        def fetch(cid, since, until):
            fetched.append((cid, since, until))
            if cid.endswith("201"):
                raise RuntimeError(f"HTTP 503 from /v2/trades (asked for user={A})")
            return [dict(r, timestamp=until - 3600) for r in trades.get(cid, [])], False

        yield conn, db, ev, fetch, fetched
        conn.close()
    finally:
        tr.clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def given_labels(labels=None):
    labels = OPEN_LABELS if labels is None else labels
    return lambda g, c, b, d: (labels, {"labelled": len(labels), "cached": 0})


def undocumented(card, legend):
    """Card key paths the legend does not name (exactly, or as the parent of documented keys: an empty list)."""
    return sorted(p for p in op.card_paths(card)
                  if p not in legend and not any(k.startswith(p + ".") or k.startswith(p + "[].") for k in legend))


def test_scan_end_to_end_offline():
    with scan_fixture() as (conn, db, ev, fetch, fetched):
        # yesterday's complete list: 400, 200 and a now-gone event
        op.save_list(conn, "2026-11-09", True, [entry("400"), entry("200"), entry("999")], [],
                     {"400": "new", "200": "new", "999": "new"})
        rep = op.scan(conn, now=NOW, walk=lambda: ev, label=given_labels(), fetch_trades=fetch, flags=FakeFlags(),
                      top=3, next_n=2, workers=2, db_path=db)
        assert all(s == W0 and u == W1 for _, s, u in fetched) and len(fetched) == 6
        assert rep["pool"]["after_ai"]["events"] == 5 and rep["pool"]["ai_excluded"] == {"weather": 1}
        assert rep["pool"]["ai"]["failed"] == 0 and rep["pool"]["label_failed"]["events"] == 0
        order = [r["event_id"] for r in rep["ranking"]]
        # 100 started ($9,000 + $4,000 from A and C over two markets; B is below the $2k threshold);
        # then 200 (court 2/4, the check failed so it is not started) and 700 (court 2/4, scheduled in
        # 8 days: wins the tie), then 300 (series fed 1/3: lower bound 6% < 15%), then 400 (election 0/2)
        assert order == ["100", "700", "200", "300", "400"], order
        top = rep["top"]
        # the walk: 100 and 700 are new, 200 is one of yesterday's (kept in its rank order); 400, yesterday's
        # too but ranked 5th, is not kept above the better 300
        assert [c["event_id"] for c in top] == ["100", "700", "200"]
        assert [c["rotation"] for c in top] == ["new", "new", "kept"]
        assert [c["event_id"] for c in rep["next"]] == ["300", "400"]
        assert rep["rotation"]["previous_day"] == "2026-11-09" and rep["rotation"]["kept"] == ["200"]
        c100 = top[0]
        st = c100["already_started"]
        assert st["status"] == "started" and st["flagged_wallets"] == 2 and st["flagged_net_usd"] == 13_000
        assert st["markets_checked"] == 2 and st["big_buyers"] == 2
        assert {w["wallet"] for w in c100["flagged_wallets"]} == {A, C}
        cards = json.dumps([{k: v for k, v in c.items() if k != "flagged_wallets"} for c in top + rep["next"]])
        cards += json.dumps(rep["errors"]) + json.dumps(rep["ranking"])
        for w in (A, B, C, D):
            assert w not in cards                                            # addresses only in the wallet lists
        assert c100["why"] == ("Already started: 2 wallets the method flags bought $13,000 net (buys minus sells) "
                               "in the last 72 h")
        assert c100["track_record"]["sentence"] == ("No track record: the store has no ended company_product "
                                                    "events, and no ended event shares its series.")
        assert c100["url"] == "https://polymarket.com/event/open-100"
        assert c100["markets"][0]["prices"] == [{"outcome": "Yes", "price": 0.3}, {"outcome": "No", "price": 0.7}]
        assert c100["markets"][0]["url"] == "https://polymarket.com/event/open-100/market-101"
        assert c100["markets"][0]["ai_note"] is None and c100["type"]["label_failed"] == 0
        c200 = top[2]
        assert c200["already_started"]["status"] == "error" and "503" in c200["already_started"]["errors"][0]
        assert "user=<wallet>" in c200["already_started"]["errors"][0]
        assert c200["already_started"]["sentence"].startswith("Not known: part of the 72 h check failed (1 problem")
        assert c200["track_record"]["basis"] == "type" and (c200["track_record"]["k"], c200["track_record"]["n"]) == (2, 4)
        assert c200["track_record"]["sentence"].startswith("2 of 4 ended court_regulator events had ≥ 10%")
        assert c200["why"] == "Track record: 2 of 4 ended court_regulator events had ≥ 10% flagged capital (range 15–85%)"
        c300 = rep["next"][0]
        assert c300["track_record"]["basis"] == "series" and c300["track_record"]["key"] == "fed"
        assert c300["reveal"]["kind"] == "scheduled" and c300["reveal"]["date"] == "2026-11-15"
        assert top[1]["why"].endswith("; reveal scheduled for 2026-11-18, within 14 days")
        c400 = rep["next"][1]
        assert c400["already_started"]["sentence"] == ("Not started: 1 big buyer in the last 72 h (a wallet's net "
                                                       "buys of one outcome ≥ $1,000 at an average price < 0.99), "
                                                       "none flagged by the method.")
        assert c300["already_started"]["sentence"] == ("Not started: no wallet's net buys (buys minus sells) of one "
                                                       "outcome reached $1,000 in the last 72 h (0 fills read on 1 "
                                                       "market).")
        assert rep["counts"]["started"] == 1 and rep["errors"][0]["event_id"] == "200"
        # the legend names every key of every card
        for c in top + rep["next"]:
            assert undocumented(c, rep["fields"]) == [], undocumented(c, rep["fields"])
        # stored: today's complete list, so tomorrow can rotate
        assert op.previous_list(conn, "2026-11-11") == ("2026-11-10", ["100", "700", "200"])
        path = op.write_report(rep, os.path.join(os.path.dirname(db), "open.json"))
        with open(path, encoding="utf-8") as f:
            assert json.load(f)["top"][0]["event_id"] == "100"
        # a --limit-events run is stored as a sample and leaves "yesterday" alone
        rep2 = op.scan(conn, now=NOW + DAY, limit_events=2, walk=lambda: ev, label=given_labels(),
                       fetch_trades=fetch, flags=FakeFlags(), top=3, next_n=2)
        assert rep2["pool"]["sampled_events"] == 2 and rep2["rotation"]["stored_as"].startswith("sample")
        assert op.previous_list(conn, "2026-11-12") == ("2026-11-10", ["100", "700", "200"])


def test_scan_says_the_window_it_used():
    with scan_fixture() as (conn, db, ev, fetch, fetched):
        rep = op.scan(conn, now=NOW, window_hours=24, walk=lambda: ev, label=given_labels(), fetch_trades=fetch,
                      flags=FakeFlags(), top=3, next_n=2, save=False)
        assert all(s == NOW - 24 * 3600 and u == NOW for _, s, u in fetched)
        text = json.dumps({k: rep[k] for k in ("about", "rules", "fields", "top", "next", "ranking")},
                          ensure_ascii=False)
        assert "72 h" not in text and "72h" not in text
        assert "last 24 h" in rep["about"] and "last 24 h" in rep["rules"]["started"]
        c100 = rep["top"][0]
        assert c100["why"].endswith("in the last 24 h") and "in the last 24 h" in c100["already_started"]["sentence"]
        assert c100["already_started"]["window"]["hours"] == 24
        assert rep["top"][2]["already_started"]["sentence"].startswith("Not known: part of the 24 h check failed")
        assert rep["rotation"]["stored_as"] == "not stored" and op.previous_list(conn, "2026-11-11") == (None, [])


def test_scan_takes_one_track_record_snapshot():
    with scan_fixture() as (conn, db, ev, fetch, fetched):
        calls = []
        real = tr.snapshot

        def counting(conn_, as_of_ts=None, variant=method.DEFAULT_VARIANT):
            calls.append((as_of_ts, variant))
            return real(conn_, as_of_ts, variant)

        def no_rate_for(*a, **k):
            raise AssertionError("rate_for re-reads the store per event; the scan must use its one snapshot")

        with patched(tr, "snapshot", counting), patched(tr, "rate_for", no_rate_for):
            rep = op.scan(conn, now=NOW, walk=lambda: ev, label=given_labels(), fetch_trades=fetch,
                          flags=FakeFlags(), top=3, next_n=2, save=False)
        assert calls == [(None, method.DEFAULT_VARIANT)], calls
        assert rep["track_record_source"]["ended_events"] == 9
        assert {r["event_id"]: (r["k"], r["n"]) for r in rep["ranking"]}["200"] == (2, 4)


class RefusedKey:
    """An ai.Client whose every call is refused (HTTP 401): label_pool raises GeminiError."""

    def __init__(self, status=401):
        self.status, self.calls = status, 0

    def generate(self, prompt, schema=None):
        self.calls += 1
        raise ai.GeminiError(f"gemini: HTTP {self.status}: API key not valid", status=self.status)


def cache_label(conn, m, event, type_, today="2026-11-10"):
    """Write one AI label to the store's cache the way ai.label_markets does."""
    item = ai._item(m, event)
    answer = {"id": item["id"], "type": type_, "reveal_kind": "any_day", "reveal_date": None,
              "quote": item["rules"][:40]}
    ai._save(conn, item, answer, "fake-model", ai._label(item, answer, "fake-model", False, NOW, today))


def test_ai_failure_keeps_cached_labels_and_marks_failed_markets():
    with scan_fixture() as (conn, db, ev, fetch, fetched):
        by_id = {e["id"]: e for e in ev}
        # cached: the weather market (its exclusion must survive), 201, 301, 401, 701 and 102; NOT 101
        for eid, mid, type_ in (("500", "501", "weather"), ("200", "201", "court_regulator"),
                                ("300", "301", "data_release"), ("400", "401", "election_vote"),
                                ("700", "701", "court_regulator"), ("100", "102", "company_product")):
            m = next(x for x in by_id[eid]["markets"] if x["id"] == mid)
            cache_label(conn, m, by_id[eid], type_)
        tr.clear_cache()
        client = RefusedKey(401)
        rep = op.scan(conn, now=NOW, walk=lambda: ev, ai_client=client, fetch_trades=fetch, flags=FakeFlags(),
                      top=3, next_n=2, save=False)
        info = rep["pool"]["ai"]
        assert client.calls == 1 and "401" in info["error"] and info["failed_ids"] == ["101"], info
        assert info["labelled"] == 6 and info["cached"] == 6
        assert rep["pool"]["ai_excluded"] == {"weather": 1}                 # the cached exclusion held
        assert rep["pool"]["after_ai"]["events"] == 5 and rep["pool"]["unlabelled_kept"] == 1
        assert rep["pool"]["label_failed"] == {"markets_kept": 1, "events": 1, "card_note": op.LABEL_FAILED}
        cards = {c["event_id"]: c for c in rep["top"] + rep["next"]}
        c100 = cards["100"]                                                  # not dropped
        row = {m["market_id"]: m for m in c100["markets"]}
        assert row["101"]["ai_note"] == op.LABEL_FAILED and row["101"]["ai_type"] is None
        assert row["102"]["ai_note"] is None and row["102"]["ai_type"] == "company_product"
        # the event's type comes from the labelled market, and the card says one label failed
        assert c100["type"]["type"] == "company_product" and c100["type"]["label_failed"] == 1
        assert op.LABEL_FAILED in c100["type"]["note"]
        assert cards["200"]["type"]["label_failed"] == 0 and cards["200"]["type"]["note"] is None
        assert cards["200"]["track_record"]["k"] == 2                        # cached types still rank


def test_label_all_keeps_cached_labels_on_errors():
    conn = store.connect(":memory:")
    ev = open_event("1", "E?", [mk("11", 500_000), mk("12", 400_000), mk("13", 300_000)])
    groups = [{"event_id": "1", "event": ev, "markets": ev["markets"]}]
    cache_label(conn, ev["markets"][0], ev, "weather")
    cache_label(conn, ev["markets"][1], ev, "other")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        # a refused key raises GeminiError out of label_pool: the cached labels are read back
        labels, info = op._label_all(groups, conn, None, RefusedKey(401), "2026-11-10")
        assert set(labels) == {"11", "12"} and info["failed_ids"] == ["13"] and "401" in info["error"]
        kept, excluded = ai.apply_exclusions(groups, labels)
        assert [m["id"] for m, _ in excluded] == ["11"] and [m["id"] for m in kept[0]["markets"]] == ["12", "13"]
        # a failing batch that does not raise (HTTP 503 twice): the same labels, the same failed market
        labels, info = op._label_all(groups, conn, None, RefusedKey(503), "2026-11-10")
        assert set(labels) == {"11", "12"} and info["failed_ids"] == ["13"] and info["error"] is None
        assert info["batch_errors"] and "503" in info["batch_errors"][0]                # why, in the report
    assert op.cached_labels(groups, conn, "2026-11-10").keys() == {"11", "12"}


def test_cli_refuses_a_missing_db_and_keeps_samples_apart():
    tmp = tempfile.mkdtemp()
    try:
        missing = os.path.join(tmp, "nope.db")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                op.main(["--db", missing, "--limit-events", "1"])
                raise AssertionError("main accepted a --db that does not exist")
            except SystemExit as e:
                assert e.code == 2
        assert "no store at" in err.getvalue() and not os.path.exists(missing)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert op.output_path(None, None) == op.OUT_PATH
    assert op.output_path(None, 12) == op.SAMPLE_OUT_PATH != op.OUT_PATH
    assert op.output_path(None, 0) == op.SAMPLE_OUT_PATH
    assert op.output_path("x.json", 12) == "x.json" and op.output_path("x.json", None) == "x.json"
    assert op.SAMPLE_OUT_PATH.endswith(os.path.join("data", "watchlist", "demo", "open_today.sample.json"))


def test_heartbeat_logs_while_quiet():
    import io
    import time
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with op.Heartbeat(every=0.05) as hb:
            hb.set("started checks", "3/10 events")
            time.sleep(0.2)
    lines = [x for x in buf.getvalue().splitlines() if "started checks: 3/10 events" in x]
    assert len(lines) >= 2, buf.getvalue()


def test_sample_groups_spreads_over_volume_rank():
    gs = [{"event_id": str(i), "markets": [mk(str(i), 100_000 * (i + 1))]} for i in range(10)]
    got = [g["event_id"] for g in op.sample_groups(gs, 4)]
    assert got == ["9", "6", "3", "0"], got
    assert len(op.sample_groups(gs, 50)) == 10 and op.sample_groups(gs, 0) == []


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
