#!/usr/bin/env python3
"""
Offline tests for the replay backtest (watchlist.backtest): no network,
a few seconds.

    python3 -m pytest -q tests/test_watchlist_backtest.py     # or
    python3 tests/test_watchlist_backtest.py                  # same checks, exits 1 on failure

Covers the replay days (UTC midnights, warm-up, the last close - 1 day
inclusive), candidate selection at D (open at D: opened <= D < closedTime;
endDate <= D + 60 days; decided by the price BEFORE D, a jump after D does
not count), the event judged the way the live list would at D (every kept
market open at D with fills, not the figure chosen after the end: started
= open_scan.event_signal over the markets, end = the earliest endDate, type
by volume as of D, decided only when ALL its markets are, the outcome still
the final figure), NO LEAKAGE (the track record at D counts only events
closed strictly before D, so a later heavy close cannot lift it; an event
closed exactly at D is neither in the record nor a candidate; the entries
handed to rank_events carry no final numbers; the started signal sees only
fills in [D - 72 h, D) and flag_fn is only asked about buys before D), a
FAILED look-up (the real open_scan.MethodFlags over a raising
method.lookup_priors: the window is unknown, never "not flagged", never
cached, counted and warned about; started anyway from the other wallets
it stays started), the fills helpers (window, price at D with the
complement and dust rules, volume as of D), the metric maths (shares,
means, k_big, the hypergeometric baseline and its convolution over days,
day means vs pooled), the started cache (a re-run reads it, a changed
signal, flag path or window cap recomputes, a look-up budget skips and
caches nothing), the reveal read from a cached AI label as of D (read
errors counted apart from missing labels), the market-field sources
(table, the closed scan's window lists, Gamma once, "missing" recorded)
and the CLI on a store file with --no-started --no-gamma (and its refusal
of a --db that does not exist). The real open_scan.rank_events /
started_signal / event_signal are used where they are pure; the flag_fn is
a stub or MethodFlags over a stubbed look-up (never the network).

Fixtures go through the closed scan's own writers (store.save_market /
save_result and closed_scan.refresh_events), as the track record's tests do.
"""
import gzip
import itertools
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from watchlist import ai, closed_scan, method, net, open_scan, store  # noqa: E402
from watchlist import backtest as bt  # noqa: E402
from watchlist import track_record as tr  # noqa: E402
from watchlist.common import DAY, iso, to_epoch  # noqa: E402

NOV1 = to_epoch("2026-11-01T00:00:00Z")
D = NOV1 + 30 * DAY                      # the replay day of the leakage fixture (2026-12-01)
VARIANT = "exact"
_N = [0]
EMPTY_LISTS = tempfile.mkdtemp(prefix="bt_lists_")


def cid():
    _N[0] += 1
    return "0x%064x" % _N[0]


def gamma_market(c, eid, title, closed_ts, opened_ts, end_ts, volume=200_000, series=None, description=None,
                 question=None):
    event = {"id": eid, "title": title, "slug": f"event-{eid}", "seriesSlug": series or f"series-{eid}"}
    return {"conditionId": c, "id": f"m{c[-6:]}", "question": question or f"{title}", "slug": f"mk-{c[-6:]}",
            "description": description or f"This market resolves to Yes if {title}",
            "closedTime": iso(closed_ts), "endDate": iso(end_ts) if end_ts is not None else None,
            "createdAt": iso(opened_ts) if opened_ts is not None else None,
            "startDate": iso(opened_ts) if opened_ts is not None else None,
            "volumeNum": volume, "outcomes": '["Yes", "No"]', "events": [event]}


GAMMA = {}


def add_event(conn, eid, closed_ts, pct, cap=50_000, type_="election_vote", opened_ts=None, end_ts=None,
              volume=200_000, title=None, status="ok", description=None, question=None):
    """One market of a Gamma event, written the way the closed scan writes
    it (called again with the same eid, it adds a market to the event and
    the event row re-chooses its figure); its Gamma object goes to GAMMA
    (the fake fetcher). Returns the condition id."""
    c = cid()
    title = title or f"Event {eid}?"
    opened_ts = closed_ts - 40 * DAY if opened_ts is None else opened_ts
    end_ts = closed_ts if end_ts is None else end_ts
    m = gamma_market(c, eid, title, closed_ts, opened_ts, end_ts, volume, description=description, question=question)
    GAMMA[c] = m
    store.save_market(conn, m, "kept", f"ai: {type_}", winner="Yes")
    whale_capital = (cap / pct * 100) if pct else 100_000
    store.save_result(conn, {
        "condition_id": c, "cutoff_variant": VARIANT, "status": status, "market_id": m["id"], "event_id": eid,
        "closed_ts": closed_ts, "whale_count": 10, "whale_capital": whale_capital,
        "flagged_count": 3 if pct else 0, "flagged_capital": cap if pct else 0.0, "flagged_pct": pct,
        "jump_ts": None, "flagged_capital_before_jump": None, "whales": []})
    closed_scan.refresh_events(conn, {eid: m["events"][0]}, VARIANT)
    return c


def fake_fetcher(calls=None):
    def fetch(ids):
        if calls is not None:
            calls.append(list(ids))
        return [GAMMA[c] for c in ids if c in GAMMA]
    return fetch


def fresh():
    tr.clear_cache()
    return store.connect(":memory:")


def load(conn):
    """evaluable_events with the fields seeded from the fake Gamma (never
    the real window lists or the network)."""
    ids = [r["condition_id"] for r in conn.execute("SELECT condition_id FROM markets")]
    bt.market_fields(conn, ids, fetch=True, list_dir=EMPTY_LISTS, fetcher=fake_fetcher())
    evs, _ = bt.evaluable_events(conn, VARIANT, fetch=False)
    return evs


def loader(fills):
    def load_fills(c, outcomes=None):
        return bt.Fills(fills[c], outcomes) if c in fills else None
    return load_fills


def buy(ts, wallet, usd, price=0.6, outcome="Yes", side="BUY"):
    return (int(ts), wallet, side, outcome, usd / price, price, usd)


class Recorder:
    """Wraps rank / signal / flag functions and records what they were given."""

    def __init__(self):
        self.rank_entries, self.signal_calls, self.flag_calls, self.signal_cids = [], [], [], []

    def rank(self, entries, top=10, next_n=10, now_ts=None):
        self.rank_entries.append((now_ts, [dict(e) for e in entries]))
        return open_scan.rank_events(entries, top=top, next_n=next_n, now_ts=now_ts)

    def signal(self, trades, market_volume, flag_fn, lo, hi):
        self.signal_calls.append((lo, hi, [t["timestamp"] for t in trades], market_volume))
        self.signal_cids.append(trades[0]["condition_id"] if trades else None)
        return open_scan.started_signal(trades, market_volume, flag_fn, lo, hi)

    def flag(self, wallet, first_buy_ts, position_usd):
        self.flag_calls.append((wallet, first_buy_ts, position_usd))
        return {"flagged": True, "stub": True}


# ── replay days and candidates ───────────────────────────────────────────────

def test_replay_days_bounds():
    first, last = NOV1 + 5 * 3600, NOV1 + 40 * DAY + 3 * 3600
    days = bt.replay_days(first, last, every_days=3, warmup_days=21)
    assert days == [NOV1 + d * DAY for d in (21, 24, 27, 30, 33, 36, 39)], [(d - NOV1) / DAY for d in days]
    assert all(d % DAY == 0 for d in days)                        # UTC midnights
    assert bt.replay_days(first, NOV1 + 40 * DAY - 1, 3, 21)[-1] == NOV1 + 36 * DAY   # last - 1 day < day 39
    assert bt.replay_days(first, NOV1 + 10 * DAY, 3, 21) == []     # span shorter than the warm-up
    assert bt.replay_days(None, last) == []
    assert len(bt.replay_days(first, last, every_days=1, warmup_days=21)) == 19


def test_candidate_reason_boundaries():
    def ev(opened, closed, end):
        return {"opened_ts": opened, "closed_ts": closed, "end_ts": end}
    ok = ev(D - DAY, D + DAY, D + DAY)
    assert bt.candidate_reason(ok, D) is None
    assert bt.candidate_reason(ev(D, D + DAY, D + DAY), D) is None                 # opened exactly at D: open
    assert bt.candidate_reason(ev(D + 1, D + DAY, D + DAY), D) == "not_open_yet"
    assert bt.candidate_reason(ev(D - DAY, D, D), D) == "closed"                   # closed exactly at D: not open
    assert bt.candidate_reason(ev(D - DAY, D - 5, D), D) == "closed"
    assert bt.candidate_reason(ev(D - DAY, D + DAY, D + 60 * DAY), D) is None      # horizon inclusive
    assert bt.candidate_reason(ev(D - DAY, D + DAY, D + 60 * DAY + 1), D) == "ends_later"
    assert bt.candidate_reason(ev(D - DAY, D + DAY, None), D) == "no_end_date"
    assert bt.candidate_reason(ev(None, D + DAY, D + DAY), D) == "no_open_time"
    assert bt.candidate_reason(ev(D - 9 * DAY, D + DAY, D - 3 * DAY), D) is None   # past its deadline, still open
    cands, why = bt.base_candidates([ok, ev(D + 1, D + DAY, D + DAY), ev(D - DAY, D, D)], D)
    assert cands == [ok] and why == {"not_open_yet": 1, "closed": 1}
    # an event is a candidate when ANY of its markets is open at D; else the market closest to it says why
    closed, later, soon = ev(D - 9 * DAY, D - DAY, D - DAY), ev(D + DAY, D + 9 * DAY, D + 9 * DAY), \
        ev(D - DAY, D + 90 * DAY, D + 90 * DAY)
    assert bt.event_reason({"markets": [closed, ok]}, D) is None
    assert bt.open_markets({"markets": [closed, ok, later]}, D) == [ok]
    assert bt.event_reason({"markets": [closed, later]}, D) == "not_open_yet"
    assert bt.event_reason({"markets": [closed, later, soon]}, D) == "ends_later"
    cands, why = bt.base_candidates([{"event_id": "x", "markets": [closed, ok]},
                                     {"event_id": "y", "markets": [closed, later]}], D)
    assert [e["event_id"] for e in cands] == ["x"] and why == {"not_open_yet": 1}


def test_event_pool_rules():
    """Per market as filters.open_pool: decided only when EVERY market with
    fills is >= 0.99; a market without fills is left out unless none has
    fills; --volume-at-d drops the markets under $100k as of D."""
    def ms(*ids):
        return [{"condition_id": c} for c in ids]

    def rec(status="computed", decided=False, vol=200_000.0):
        return {"status": status, "decided": decided, "volume_at": vol}
    dec, nof = rec("decided", True), rec("no_fills", vol=None)
    pool, why, notes = bt.event_pool(ms("a", "b", "c"), {"a": dec, "b": rec(), "c": nof})
    assert [m["condition_id"] for m in pool] == ["b"] and why is None
    assert (notes["decided"], notes["unfilled"]) == (1, 1)
    assert bt.event_pool(ms("a", "b"), {"a": dec, "b": dec})[:2] == ([], "decided")
    assert bt.event_pool(ms("a", "c"), {"a": dec, "c": nof})[:2] == ([], "decided")       # the one with fills
    pool, why, _ = bt.event_pool(ms("a", "c"), {"a": nof, "c": nof})
    assert len(pool) == 2 and why is None                     # nothing priced: kept, its signal unknown
    r = {"a": rec(vol=50_000.0), "b": rec(vol=150_000.0)}
    pool, why, notes = bt.event_pool(ms("a", "b"), r)
    assert len(pool) == 2 and notes["low_volume"] == 1 and not notes["all_low_volume"]
    assert [m["condition_id"] for m in bt.event_pool(ms("a", "b"), r, volume_at_d=True)[0]] == ["b"]
    assert bt.event_pool(ms("a"), r)[2]["all_low_volume"]
    assert bt.event_pool(ms("a"), r, volume_at_d=True)[:2] == ([], "volume_lt_min_at_d")


# ── fills ────────────────────────────────────────────────────────────────────

def test_fills_window_price_volume_before_D():
    rows = [buy(D - 80 * 3600, "0xa", 1000, 0.40),      # before the window
            buy(D - 72 * 3600, "0xb", 1000, 0.50),      # window start: inside
            buy(D - 60, "0xc", 3, 0.10),                # dust: not a price
            buy(D - 1, "0xd", 2000, 0.70),
            buy(D, "0xe", 5000, 0.999),                 # at D: after the cut
            buy(D + 3600, "0xf", 100_000, 0.999)]
    f = bt.Fills(list(reversed(rows)), ["Yes", "No"])   # any order in, sorted inside
    assert [r[1] for r in f.window(D - 72 * 3600, D)] == ["0xb", "0xc", "0xd"]
    assert f.price_at(D, n=1) == 0.70                   # the fill at D (0.999) is not seen; dust skipped
    assert abs(f.price_at(D) - 0.5) < 1e-9              # median of 0.40, 0.50, 0.70 -> 0.5 (max with 1 - 0.5)
    assert f.price_at(D + 2 * 3600, n=1) == 0.999
    assert f.price_at(D - 81 * 3600) is None
    total = sum(r[6] for r in rows)
    assert abs(f.volume_at(D, 1_000_000) - 1_000_000 * 4003 / total) < 1e-6
    g = bt.Fills([buy(D - 10, "0xa", 100, 0.2, outcome="No")], ["Yes", "No"])
    assert abs(g.price_at(D) - 0.8) < 1e-9              # No at 0.2 = Yes at 0.8
    assert bt.Fills([], ["Yes", "No"]).volume_at(D, 5e5) == 0.0


def test_fill_tuple_and_to_v2():
    raw = {"user_id": "0xABC", "side": "buy", "outcome": "No", "odds": "0.25", "contracts": "40",
           "dollar_amount": "10.0", "timestamp": "2026-11-05T12:00:00+00:00"}
    t = bt.fill_tuple(raw)
    assert t == (to_epoch("2026-11-05T12:00:00Z"), "0xabc", "BUY", "No", 40.0, 0.25, 10.0)
    v2 = bt.to_v2(t, ["Yes", "No"], "0xcid")
    assert v2 == {"proxy_wallet": "0xabc", "side": "BUY", "outcome": "No", "outcome_index": 1, "size": 40.0,
                  "price": 0.25, "timestamp": t[0], "condition_id": "0xcid"}
    assert bt.fill_tuple({**raw, "timestamp": "garbage"}) is None


# ── metric maths ─────────────────────────────────────────────────────────────

def test_metrics():
    items = [{"flagged_pct": 0.0, "flagged_capital": 0.0}, {"flagged_pct": 9.99, "flagged_capital": 100_000},
             {"flagged_pct": 10.0, "flagged_capital": 5_000}, {"flagged_pct": 50.0, "flagged_capital": 20_000}]
    m = bt.metrics(items)
    assert (m["n"], m["k_heavy"], m["heavy_share"], m["k_big"], m["big_share"]) == (4, 2, 0.5, 1, 0.25)
    assert m["mean_flagged_pct"] == round((0 + 9.99 + 10 + 50) / 4, 4)
    assert m["median_flagged_pct"] == 9.995 and m["mean_flagged_capital"] == 31_250.0
    e = bt.metrics([])
    assert e["n"] == 0 and e["heavy_share"] is None and e["mean_flagged_pct"] is None
    assert bt.metrics([{"flagged_pct": None, "flagged_capital": None}])["k_heavy"] == 0


def test_hypergeometric_baseline():
    pmf = bt.hypergeom_pmf(20, 5, 4)
    assert abs(sum(pmf) - 1) < 1e-12
    assert abs(pmf[0] - 1365 / 4845) < 1e-12                       # C(15,4) / C(20,4)
    assert bt.hypergeom_pmf(10, 3, 10)[3] == 1.0                     # drawing everything
    assert bt.tail_p(pmf, 0) == 1.0 and abs(bt.tail_p(pmf, 1) - (1 - 1365 / 4845)) < 1e-12
    assert bt.summed_tail_p([(20, 5, 4)], 2) == bt.tail_p(pmf, 2)
    # two days by brute force: every way of drawing 3 of 8 (2 heavy) and 2 of 6 (3 heavy)
    a = [c for c in itertools.combinations(range(8), 3)]
    b = [c for c in itertools.combinations(range(6), 2)]
    hits = sum(1 for x in a for y in b if sum(i < 2 for i in x) + sum(i < 3 for i in y) >= 3)
    assert abs(bt.summed_tail_p([(8, 2, 3), (6, 3, 2)], 3) - hits / (len(a) * len(b))) < 1e-12
    try:
        bt.hypergeom_pmf(5, 6, 2)
        raise AssertionError("K > N must be refused")
    except ValueError:
        pass


def test_day_metrics_and_aggregate():
    finals = {f"e{i}": {"flagged_pct": 50.0 if i < 4 else 0.0, "flagged_capital": 20_000 if i < 4 else 0.0}
              for i in range(20)}
    cands = list(finals)
    top = ["e0", "e1", "e2"] + [f"e{i}" for i in range(4, 11)]          # 3 heavy of 10
    track = ["e0"] + [f"e{i}" for i in range(4, 13)]                    # 1 heavy of 10
    m1 = bt.day_metrics(cands, finals, top, track, ["e1", "e3"], top=10)
    assert m1["pool"]["k_heavy"] == 4 and m1["pool"]["heavy_share"] == 0.2
    assert m1["top"]["k_heavy"] == 3 and m1["track_top"]["k_heavy"] == 1 and m1["started"]["heavy_share"] == 1.0
    assert m1["random_top"] == {"n": 10, "expected_k_heavy": 2.0, "heavy_share": 0.2, "mean_flagged_pct": 10.0,
                                "mean_flagged_capital": 4000.0}
    assert abs(m1["top"]["p_random_ge"] - round(bt.tail_p(bt.hypergeom_pmf(20, 4, 10), 3), 4)) < 1e-9
    assert m1["informative"] and "p_random_ge" not in m1["started"]
    small = ["e0", "e5", "e6", "e7", "e8"]
    m2 = bt.day_metrics(small, finals, small, small, [], top=10)       # pool <= 10: the top is the pool
    assert not m2["informative"] and m2["top"]["k_heavy"] == 1 and m2["random_top"]["expected_k_heavy"] == 1.0
    days = [{"metrics": m1, "candidates": cands, "top_ids": top, "track_ids": track, "started_ids": ["e1", "e3"]},
            {"metrics": m2, "candidates": small, "top_ids": small, "track_ids": small, "started_ids": []}]
    s = bt.aggregate(days, top=10, finals=finals)
    assert s["days"] == 2 and s["days_informative"] == 1 and s["days_with_started"] == 1
    assert s["candidates_total"] == 25
    t = s["top"]
    assert (t["k_heavy"], t["n"]) == (4, 15) and t["pooled_heavy_share"] == round(4 / 15, 4)
    assert t["day_mean_heavy_share"] == round((0.3 + 0.2) / 2, 4)
    assert t["expected_k_heavy_random"] == 3.0 and t["lift_k_heavy"] == 1.0
    assert t["days_vs_pool_share"] == {"above": 1, "equal": 1}
    assert s["track_top"]["lift_k_heavy"] == -1.0
    st = s["started"]
    assert st["days"] == 1 and (st["k_heavy"], st["n"]) == (2, 2) and st["expected_k_heavy_random"] == 0.4
    assert s["pool"]["pooled_heavy_share"] == round(5 / 25, 4)
    assert {k: (v["n"], v["k_heavy"]) for k, v in s["distinct_events"].items()} == \
        {"pool": (20, 4), "top": (10, 3), "track_top": (10, 1), "started": (2, 2)}      # day 2 adds no new event
    assert bt.aggregate(days, top=10)["distinct_events"]["top"] == {"n": 10, "k_heavy": None, "heavy_share": None}


# ── the replay: no leakage ───────────────────────────────────────────────────

def leakage_fixture(conn):
    """History before D (election 2 of 4 heavy, court 0 of 3), events that
    close after D but opened after D (court, all 100 %: they must not lift
    the court rate at D), an election event closed EXACTLY at D (heavy:
    neither in the record nor a candidate), and two candidates open at D:
    C1 election (final 0 %), C2 court (final 90 %, with a started window)."""
    for i, pct in enumerate([50.0, 0.0, 30.0, 0.0]):
        add_event(conn, f"h-el-{i}", D - (10 - i) * DAY, pct, type_="election_vote")
    for i in range(3):
        add_event(conn, f"h-ct-{i}", D - (5 - i) * DAY, 0.0, type_="court_regulator")
    for i in range(5):
        add_event(conn, f"late-{i}", D + (3 + i) * DAY, 100.0, type_="court_regulator", opened_ts=D + DAY)
    add_event(conn, "at-D", D, 99.0, type_="election_vote")
    c1 = add_event(conn, "C1", D + 5 * DAY, 0.0, type_="election_vote", opened_ts=D - 20 * DAY)
    c2 = add_event(conn, "C2", D + 6 * DAY, 90.0, cap=200_000, type_="court_regulator", opened_ts=D - 20 * DAY)
    fills = {c2: [buy(D - 100 * 3600, "0xold", 50_000),                  # before the window
                  buy(D - 50 * 3600, "0xw1", 6_000), buy(D - 40 * 3600, "0xw2", 6_000),
                  buy(D - 30 * 3600, "0xw1", 600, side="SELL"),
                  buy(D + 3600, "0xfuture", 1_000_000, 0.95)]}            # after D: never seen
    return c1, c2, fills


def test_replay_no_leakage():
    conn = fresh()
    c1, c2, fills = leakage_fixture(conn)
    evs = load(conn)
    rec = Recorder()
    days, info = bt.replay(conn, evs, [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills),
                           workers=1)
    day = days[0]
    assert day["candidates"] == ["C1", "C2"] or sorted(day["candidates"]) == ["C1", "C2"]
    assert day["not_candidates"]["closed"] == 8 and day["not_candidates"]["not_open_yet"] == 5
    # the track record at D: only closes strictly before D
    tracks = {e["event_id"]: e["track"] for e in rec.rank_entries[0][1]}
    assert (tracks["C1"]["basis"], tracks["C1"]["k"], tracks["C1"]["n"]) == ("type", 2, 4)     # "at-D" left out
    assert (tracks["C2"]["k"], tracks["C2"]["n"], tracks["C2"]["rate"]) == (0, 3, 0.0)         # "late-*" left out
    assert all(ex["closed"] < iso(D)[:10] for t in tracks.values() for ex in t["examples"])
    # the ranking never sees a final number
    for _, entries in rec.rank_entries:
        for e in entries:
            assert set(e) == {"event_id", "title", "end_ts", "reveal_kind", "reveal_ts", "started",
                              "started_flagged_usd", "started_wallets", "track"}, sorted(e)
    # the started signal: fills in [D - 72 h, D) only, look-ups only for buys before D
    assert len(rec.signal_calls) == 1
    lo, hi, stamps, vol = rec.signal_calls[0]
    assert (lo, hi) == (D - 72 * 3600, D) and stamps and all(lo <= t < D for t in stamps)
    assert vol < 200_000                                  # the volume as of D, not the final one
    assert {w for w, _, _ in rec.flag_calls} == {"0xw1", "0xw2"}
    assert all(t < D for _, t, _ in rec.flag_calls)
    assert dict((w, round(p)) for w, _, p in rec.flag_calls) == {"0xw1": 5400, "0xw2": 6000}   # net of the sell
    # C2 started (2 flagged wallets, $11.4k): first in the full ranking; the track-only ranking puts C1 first
    assert day["started_ids"] == ["C2"] and day["top_ids"] == ["C2", "C1"] and day["track_ids"] == ["C1", "C2"]
    assert day["started_unknown"] == 1                    # C1 has no fills cache
    m = day["metrics"]
    assert m["pool"]["k_heavy"] == 1 and m["started"]["k_heavy"] == 1 and m["top"]["n"] == 2
    assert info["counters"].get("windows_computed") == 1 and info["counters"].get("windows_no_fills") == 1


def test_outcome_uses_only_markets_open_at_D():
    # The event's final figure (m1, 40% flagged) closed before D: its answer was public at D, so a pick at D
    # is scored on the markets still open then (m2 heavy, m3 not; m2 has more flagged capital).
    ev = {"event_id": "E", "flagged_pct": 40.0, "flagged_capital": 90_000.0, "condition_id": "m1",
          "markets": [{"condition_id": "m1", "closed_ts": D - 3600, "volume": 5e6},
                      {"condition_id": "m2", "closed_ts": D + 86400, "volume": 1e6},
                      {"condition_id": "m3", "closed_ts": D + 2 * 86400, "volume": 2e6}]}
    mres = {"m1": {"status": "ok", "flagged_capital": 90_000.0, "flagged_pct": 40.0, "closed_ts": D - 3600},
            "m2": {"status": "ok", "flagged_capital": 30_000.0, "flagged_pct": 12.0, "closed_ts": D + 86400},
            "m3": {"status": "ok", "flagged_capital": 2_000.0, "flagged_pct": 1.0, "closed_ts": D + 2 * 86400}}
    out = bt.outcome_at(ev, D, mres)
    assert out["condition_id"] == "m2" and out["flagged_pct"] == 12.0, out
    # a market closing exactly at D is not "after D"
    mres["m2"]["closed_ts"] = D
    assert bt.outcome_at(ev, D, mres)["condition_id"] == "m3"
    # nothing closes after D with an ok result → no outcome (the event is dropped that day)
    mres["m3"]["status"] = "too_large"
    assert bt.outcome_at(ev, D, mres) is None
    # the pick line carries the D-outcome, not the final number
    view = {"track": {"basis": "type", "key": "t", "k": 1, "n": 2, "rate": 0.5, "low": 0.1}, "signal": {},
            "type": "t", "type_market": "m2", "pool": ["m2"], "status": "not_started", "errors": []}
    entry = {"started": False, "started_flagged_usd": 0.0, "started_wallets": 0, "reveal_kind": "any_day",
             "end_ts": D + 86400}
    mres["m3"]["status"] = "ok"
    line = bt._pick(ev, entry, view, D, bt.outcome_at(ev, D, mres))
    assert line["final_flagged_pct"] == 1.0 and line["heavy"] is False and line["outcome_market"] == "m3"


def test_decided_by_price_before_D_only():
    conn = fresh()
    a = add_event(conn, "dec", D + 4 * DAY, 0.0, opened_ts=D - 10 * DAY)
    b = add_event(conn, "jump", D + 4 * DAY, 0.0, opened_ts=D - 10 * DAY)
    fills = {a: [buy(D - 3600 * i, "0xa", 500, 0.995) for i in range(1, 7)],
             b: [buy(D - 3600 * i, "0xb", 500, 0.60) for i in range(1, 7)] +
                [buy(D + 60 * i, "0xb", 50_000, 0.999) for i in range(1, 7)]}
    rec = Recorder()
    days, _ = bt.replay(conn, load(conn), [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills),
                        workers=1)
    assert days[0]["candidates"] == ["jump"] and days[0]["dropped"] == {"decided": 1}
    assert days[0]["base_candidates"] == 2


def test_volume_as_of_D_rule():
    """$1M final volume, but only 5 % of the fills' dollars traded before D:
    $50k as of D. Counted by default, dropped with volume_at_d=True."""
    conn = fresh()
    c = add_event(conn, "thin", D + 4 * DAY, 30.0, opened_ts=D - 10 * DAY, volume=1_000_000)
    fills = {c: [buy(D - 3600, "0xa", 500), buy(D + 3600, "0xb", 9_500)]}
    rec = Recorder()
    evs = load(conn)
    kept, _ = bt.replay(conn, evs, [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills), workers=1)
    assert kept[0]["candidates"] == ["thin"] and kept[0]["volume_lt_min_at_d"] == 1
    gone, _ = bt.replay(conn, evs, [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills), workers=1,
                        volume_at_d=True)
    assert gone[0]["candidates"] == [] and gone[0]["dropped"] == {"volume_lt_min_at_d": 1}
    assert abs(rec.signal_calls[0][3] - 50_000) < 1e-6        # the signal got the as-of-D volume


def test_started_cache_signal_key_and_budget():
    conn = fresh()
    c1, c2, fills = leakage_fixture(conn)
    evs = load(conn)
    rec = Recorder()
    first, _ = bt.replay(conn, evs, [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills),
                         workers=1)
    rec2 = Recorder()
    rec2.signal = rec.signal                                # the same function: the cache answers
    again, info = bt.replay(conn, evs, [D], rec2.rank, rec.signal, rec2.flag, VARIANT, fills_loader=loader(fills),
                            workers=1)
    assert len(rec.signal_calls) == 1 and not rec2.flag_calls and info["counters"]["windows_cached"] == 1
    assert again[0]["top_ids"] == first[0]["top_ids"] and again[0]["metrics"] == first[0]["metrics"]

    def other_signal(trades, market_volume, flag_fn, lo, hi):       # another source: another cache key
        other_signal.calls += 1
        return {"started": False, "flagged_usd": 0.0, "wallets": [], "big_buyers": []}
    other_signal.calls = 0
    assert bt.signal_key(other_signal) != bt.signal_key(rec.signal)
    bt.replay(conn, evs, [D], rec.rank, other_signal, rec.flag, VARIANT, fills_loader=loader(fills), workers=1)
    assert other_signal.calls == 1

    def budget_signal(trades, market_volume, flag_fn, lo, hi):
        raise AssertionError("over budget: must not be called")
    days, info = bt.replay(conn, evs, [D], rec.rank, budget_signal, rec.flag, VARIANT, fills_loader=loader(fills),
                           workers=1, max_lookup_requests=0)
    assert info["counters"]["windows_skipped_budget"] == 1 and days[0]["started_unknown"] == 2
    assert days[0]["started_ids"] == []
    n = conn.execute("SELECT COUNT(*) FROM backtest_started WHERE signal_key=?",
                     (bt.signal_key(budget_signal),)).fetchone()[0]
    assert n == 0


def test_real_open_scan_signal_on_to_v2_rows():
    """open_scan.started_signal reads the rows to_v2 builds (wallet, side,
    outcome, size x price), the window bounds and the flag answers."""
    t = D - 3600
    rows = [bt.to_v2(buy(t, "0xw1", 7_000), ["Yes", "No"]), bt.to_v2(buy(t + 5, "0xw2", 4_000), ["Yes", "No"]),
            bt.to_v2(buy(t + 9, "0xw3", 9_000, 0.995), ["Yes", "No"])]          # a near-certain buy: not big
    res = open_scan.started_signal(rows, 2_000_000, lambda w, ts, p: {"flagged": w != "0xw3"}, D - 72 * 3600, D)
    assert res["started"] and res["wallets"] == ["0xw1", "0xw2"] and abs(res["flagged_usd"] - 11_000) < 0.01


# ── reveal, market fields, CLI ───────────────────────────────────────────────

def test_reveal_from_label_as_of_D():
    conn = fresh()
    rules = ("The election will be held on December 20, 2026. This market resolves to Yes if the incumbent wins "
             "the most votes.")
    m = gamma_market(cid(), "rv", "Will the incumbent win?", D + 30 * DAY, D - 30 * DAY, D + 30 * DAY,
                     description=rules)
    item = ai._item(m)
    answer = {"id": item["id"], "type": "election_vote", "reveal_kind": "scheduled", "reveal_date": "2026-12-20",
              "quote": "The election will be held on December 20, 2026."}
    lab = ai.validate(answer, rules, item["end_ts"], today="2026-11-01")
    store.save_ai_label(conn, item["id"], item["sha1"], lab, "test-model", lab["valid"],
                        json.dumps({"prompt_version": ai.PROMPT_VERSION, "answer": answer}))
    kind, ts, state = bt.reveal_at(conn, m, D)                                 # 2026-12-01: still ahead
    assert (kind, ts, state) == ("scheduled", to_epoch("2026-12-20"), "labelled")
    kind, ts, _ = bt.reveal_at(conn, m, to_epoch("2026-12-25"))
    assert kind == ai.REVEALED and ts == to_epoch("2026-12-20")
    other = gamma_market(cid(), "rv2", "Unlabelled?", D + DAY, D - DAY, D + DAY)
    assert bt.reveal_at(conn, other, D) == ("unclear", None, "no_label")
    lab, state = bt.label_at(conn, {"market": other, "ai_type": "court_regulator"}, D)
    assert state == "no_label" and lab["type"] == "court_regulator" and lab["reveal_kind"] == "unclear"
    orig = ai._from_cache

    def broken(*a, **kw):
        raise ValueError("malformed cached row")
    ai._from_cache = broken
    try:
        assert bt.reveal_at(conn, m, D) == ("unclear", None, "read_error")      # an error is not "no label"
        lab, state = bt.label_at(conn, {"market": m, "ai_type": "election_vote"}, D)
        assert state == "read_error" and lab["type"] == "election_vote"
    finally:
        ai._from_cache = orig


def test_label_read_errors_counted_apart():
    conn = fresh()
    c1, c2, fills = leakage_fixture(conn)
    evs = load(conn)
    rec = Recorder()
    _, info = bt.replay(conn, evs, [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills), workers=1)
    assert info["label_reads"] == {"no_label": 2}
    orig = ai._from_cache

    def broken(*a, **kw):
        raise ValueError("malformed cached row")
    ai._from_cache = broken
    try:
        days, info = bt.replay(conn, evs, [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills),
                               workers=1)
    finally:
        ai._from_cache = orig
    assert info["label_reads"] == {"read_error": 2}
    assert sorted(days[0]["candidates"]) == ["C1", "C2"]          # the replay goes on, on the stored types


def test_market_fields_sources():
    conn = fresh()
    a, b, c = cid(), cid(), cid()
    ma = gamma_market(a, "fa", "A?", D, D - 9 * DAY, D)
    ma["startDate"] = iso(D - 8 * DAY)                                          # the later of the two counts
    GAMMA[b] = gamma_market(b, "fb", "B?", D, D - 5 * DAY, D)
    tmp = tempfile.mkdtemp(prefix="bt_lists_")
    try:
        with gzip.open(os.path.join(tmp, "w.json.gz"), "wt", encoding="utf-8") as f:
            json.dump({"fetched_at": 0, "markets": [closed_scan.slim_market(ma)]}, f)
        calls = []
        got = bt.market_fields(conn, [a, b, c], list_dir=tmp, fetcher=fake_fetcher(calls))
        assert calls == [[b, c]]
        assert (got[a]["source"], got[b]["source"], got[c]["source"]) == ("closed_list", "gamma", "missing")
        assert got[a]["opened_ts"] == D - 8 * DAY and got[a]["end_ts"] == D and got[a]["market"]["question"] == "A?"
        assert got[c]["market"] is None
        again = bt.market_fields(conn, [a, b, c], list_dir=tmp, fetcher=fake_fetcher(calls))
        assert calls == [[b, c]] and set(again) == {a, b, c}                    # nothing fetched twice
        bt.market_fields(conn, [c], list_dir=tmp, fetch=False, refetch=True, fetcher=fake_fetcher(calls))
        assert calls == [[b, c]]                                                # fetch=False: no Gamma
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_offline_writes_json():
    tmp = tempfile.mkdtemp(prefix="bt_cli_")
    try:
        db, out = os.path.join(tmp, "s.db"), os.path.join(tmp, "backtest.json")
        conn = store.connect(db)
        tr.clear_cache()
        leakage_fixture(conn)
        ids = [r["condition_id"] for r in conn.execute("SELECT condition_id FROM markets")]
        bt.market_fields(conn, ids, list_dir=EMPTY_LISTS, fetcher=fake_fetcher())
        conn.close()
        rep = bt.main(["--db", db, "--warmup-days", "15", "--every-days", "3", "--no-started", "--no-gamma",
                       "--out", out])
        with open(out) as f:
            got = json.load(f)
        assert got["coverage"]["evaluable_events"] == 15 and got["coverage"]["replay_days_run"] >= 1
        assert got["summary"]["days"] == rep["summary"]["days"] and got["small_sample"]
        assert got["params"]["with_started"] is False and got["biases"]
        assert all("candidates" not in d for d in got["days"]) and "top" in got["days"][0]
        assert got["run"]["lookup_requests"] == 0          # --no-started: no wallet look-up at all
        assert "started signal was on" not in got["small_sample"]     # no such warning when it is off
    finally:
        tr.clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)


# ── the event at D: its open markets, not the figure chosen after the end ────

def multi_fixture(conn):
    """History before D: election 2 of 2 heavy, court 0 of 2. Event "multi":
    m1 court (open at D, ends D + 5 d, final volume $1M but only 6 % of it
    traded before D: $60k as of D; w1 buys $6k), m2 election (open, ends
    D + 12 d, $500k as of D; w2 buys $6k), m4 election (open, ends D + 2 d,
    priced 0.995: decided; w4 buys there), m3 court (opens D + 1 d; 80 %
    flagged, $200k: the FIGURE). Event "alldec": two markets, both 0.995.
    Event "sib": one market with fills, one without (left out)."""
    for i in range(2):
        add_event(conn, f"h-e-{i}", D - (9 - i) * DAY, 40.0, type_="election_vote")
        add_event(conn, f"h-c-{i}", D - (6 - i) * DAY, 0.0, type_="court_regulator")
    o = D - 30 * DAY
    m1 = add_event(conn, "multi", D + 5 * DAY, 0.0, type_="court_regulator", opened_ts=o, volume=1_000_000,
                   title="Multi?", question="Multi: court rung?")
    m2 = add_event(conn, "multi", D + 12 * DAY, 0.0, type_="election_vote", opened_ts=o, volume=500_000,
                   title="Multi?", question="Multi: election rung?")
    m4 = add_event(conn, "multi", D + 2 * DAY, 0.0, type_="election_vote", opened_ts=o, volume=300_000,
                   title="Multi?", question="Multi: decided rung?")
    m3 = add_event(conn, "multi", D + 20 * DAY, 80.0, cap=200_000, type_="court_regulator", opened_ts=D + DAY,
                   title="Multi?", question="Multi: the figure?")
    a1 = add_event(conn, "alldec", D + 4 * DAY, 0.0, opened_ts=o)
    a2 = add_event(conn, "alldec", D + 8 * DAY, 0.0, opened_ts=o)
    s1 = add_event(conn, "sib", D + 9 * DAY, 0.0, opened_ts=o)
    add_event(conn, "sib", D + 9 * DAY, 0.0, opened_ts=o)                    # no fills file
    fills = {m1: [buy(D - 10 * 3600, "0xw1", 6_000, 0.6), buy(D + 3600, "0xlate", 94_000, 0.6)],
             m2: [buy(D - 20 * 3600, "0xw2", 6_000, 0.5)],
             m4: [buy(D - 5 * 3600 - 60 * i, "0xw4", 1_200, 0.995) for i in range(5)],
             a1: [buy(D - 3600 * i, "0xa", 500, 0.995) for i in range(1, 6)],
             a2: [buy(D - 3600 * i, "0xa", 500, 0.996) for i in range(1, 6)],
             s1: [buy(D - 7200, "0xs", 300, 0.4)]}
    return {"m1": m1, "m2": m2, "m3": m3, "m4": m4}, fills


def test_event_judged_on_its_open_markets():
    conn = fresh()
    ids, fills = multi_fixture(conn)
    evs = load(conn)
    multi = next(e for e in evs if e["event_id"] == "multi")
    assert multi["condition_id"] == ids["m3"] and len(multi["markets"]) == 4     # the figure + every kept market
    assert bt.candidate_reason(next(m for m in multi["markets"] if m["condition_id"] == ids["m3"]), D) == \
        "not_open_yet"                       # the old figure-based replay would not have had "multi" at D at all
    rec = Recorder()
    days, info = bt.replay(conn, evs, [D], rec.rank, rec.signal, rec.flag, VARIANT, fills_loader=loader(fills),
                           workers=1)
    day = days[0]
    assert sorted(day["candidates"]) == ["multi", "sib"] and day["dropped"] == {"decided": 1}     # alldec
    entry = next(e for e in rec.rank_entries[0][1] if e["event_id"] == "multi")
    # started from two markets together (each alone has one wallet, $6k): open_scan.event_signal
    assert entry["started"] and entry["started_wallets"] == 2 and abs(entry["started_flagged_usd"] - 12_000) < 0.01
    assert entry["end_ts"] == D + 5 * DAY                 # the earliest endDate of the pool (m4 is decided)
    t = entry["track"]
    assert (t["basis"], t["key"], t["k"], t["n"]) == ("type", "election_vote", 2, 2)   # m2: the most volume at D
    assert {w for w, _, _ in rec.flag_calls} == {"0xw1", "0xw2"}                     # nothing asked on m4
    assert sorted(c for c in rec.signal_cids if c) == sorted([ids["m1"], ids["m2"], next(
        m["condition_id"] for e in evs if e["event_id"] == "sib" for m in e["markets"] if m["condition_id"] in fills)])
    pick = next(p for p in day["top"] if p["event_id"] == "multi")
    assert pick["type"] == "election_vote" and pick["final_type"] == "court_regulator"
    assert pick["type_market"] == "Multi: election rung?" and pick["pool_markets"] == 2
    assert pick["outcome_market"] == ids["m3"] and pick["heavy"] and pick["final_flagged_pct"] == 80.0  # the outcome
    assert day["started_ids"] == ["multi"] and day["top_ids"][0] == "multi"
    assert day["markets"]["without_fills"] == 1 and day["started_unknown"] == 0     # sib: priced on its other market
    assert day["markets"]["decided"] == 3 and day["markets"]["multi_market_events"] == 1
    assert day["type_differs_from_final"] == 1
    assert info["counters"]["windows_computed"] == 3


# ── a failed wallet look-up ──────────────────────────────────────────────────

class Priors:
    """method.lookup_priors stand-in: raises for the wallets in `fail` (an
    address in the message, which must not reach the JSON), else a fresh
    wallet (0 prior trades: flagged)."""

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []
        self.orig = method.lookup_priors

    def __call__(self, rows, cutoff_variant, conn=None, workers=1):
        self.calls.append(sorted(r["wallet"] for r in rows))
        if any(r["wallet"] in self.fail for r in rows):
            raise RuntimeError("user-volume 503 for 0x" + "ab" * 20)
        return [{"trade_count": 0, "volume_usdc": 0.0, "counts_exact": True, "decided_by": "agree"} for _ in rows]

    def __enter__(self):
        method.lookup_priors = self
        return self

    def __exit__(self, *exc):
        method.lookup_priors = self.orig


def _cached_rows(conn, c):
    return conn.execute("SELECT COUNT(*) FROM backtest_started WHERE condition_id=?", (c,)).fetchone()[0]


def test_failed_lookup_is_unknown_and_never_cached():
    conn = fresh()
    c1, c2, fills = leakage_fixture(conn)
    load(conn)
    rec = Recorder()
    with Priors(fail={"0xw2"}) as pri:
        flags = open_scan.MethodFlags(conn, VARIANT, workers=1)
        rep = bt.build_report(conn, every_days=100, warmup_days=10, rank_fn=rec.rank,
                              signal_fn=open_scan.started_signal, flag_fn=flags, fills_loader=loader(fills),
                              fetch_gamma=False, workers=1)
    assert ["0xw2"] in pri.calls                                # it was asked, and it failed
    assert rep["coverage"]["days"] == [iso(D)[:10]]
    day = rep["days"][0]
    # 0xw1 alone is flagged ($5.4k from one wallet): NOT a "not started", an unknown
    assert day["started_unknown"] == 2 and day["started_unknown_by"] == {"error": 1, "no_fills": 1}
    assert not day["started_events"]
    c = rep["run"]["counters"]
    assert c.get("windows_error") == 1 and not c.get("windows_computed")
    assert _cached_rows(conn, c2) == 0                          # never cached: the next run asks again
    assert "failed look-ups: 1" in rep["small_sample"] and rep["summary"]["started_unknown_by"]["error"] == 1
    pick = next(p for p in day["top"] if p["event_id"] == "C2")
    assert pick["started_status"] == "unknown" and pick["lookup_errors"]
    text = json.dumps(rep)
    assert "ab" * 20 not in text and "<wallet>" in pick["lookup_errors"][0]       # addresses scrubbed
    # the look-up works again: computed, started, cached
    with Priors():
        days, info = bt.replay(conn, load(conn), [D], rec.rank, open_scan.started_signal,
                               open_scan.MethodFlags(conn, VARIANT, workers=1), VARIANT, fills_loader=loader(fills),
                               workers=1, bigs_fn=open_scan.big_buyers)
    assert info["counters"].get("windows_computed") == 1 and days[0]["started_ids"] == ["C2"]
    assert days[0]["started_unknown"] == 1 and _cached_rows(conn, c2) == 1


def test_failed_lookup_but_started_by_the_others():
    conn = fresh()
    c1, c2, fills = leakage_fixture(conn)
    fills[c2].append(buy(D - 20 * 3600, "0xw3", 6_000))
    evs = load(conn)
    rec = Recorder()
    with Priors(fail={"0xw3"}):
        days, info = bt.replay(conn, evs, [D], rec.rank, open_scan.started_signal,
                               open_scan.MethodFlags(conn, VARIANT, workers=1), VARIANT, fills_loader=loader(fills),
                               workers=1, bigs_fn=open_scan.big_buyers)
    day = days[0]
    assert day["started_ids"] == ["C2"] and day["started_with_errors"] == 1      # 0xw1 + 0xw2 start it anyway
    assert day["started_unknown"] == 1                                           # C1 (no fills) only
    assert info["counters"].get("windows_error_started") == 1 and not info["counters"].get("windows_error")
    assert _cached_rows(conn, c2) == 0                                           # still never cached
    pick = next(p for p in day["started_events"] if p["event_id"] == "C2")
    assert pick["started"] and pick["lookup_errors"] and abs(pick["started_flagged_usd"] - 11_400) < 0.01


def test_raising_signal_is_an_error_not_cached():
    conn = fresh()
    c1, c2, fills = leakage_fixture(conn)
    evs = load(conn)
    rec = Recorder()

    def raising_flag(wallet, first_buy_ts, position_usd):
        raise ConnectionError("Data API timed out")
    days, info = bt.replay(conn, evs, [D], rec.rank, rec.signal, raising_flag, VARIANT, fills_loader=loader(fills),
                           workers=1)
    assert info["counters"].get("windows_error") == 1 and days[0]["started_unknown_by"] == {"error": 1, "no_fills": 1}
    assert _cached_rows(conn, c2) == 0


# ── the cache key ────────────────────────────────────────────────────────────

def test_signal_key_covers_flag_path_and_window_cap():
    rec, rec2 = Recorder(), Recorder()
    k = bt.signal_key(rec.signal, rec.flag, 60_000)
    assert k == bt.signal_key(rec2.signal, rec2.flag, 60_000)          # the same code, another instance
    assert k != bt.signal_key(rec.signal, rec.flag, 50_000)            # the window cap
    assert k != bt.signal_key(rec.signal, rec.flag, None)

    def other_flag(wallet, first_buy_ts, position_usd):
        return {"flagged": False}
    assert k != bt.signal_key(rec.signal, other_flag, 60_000)          # another flag_fn
    flags = open_scan.MethodFlags(fresh(), VARIANT)
    cap = open_scan.MAX_WINDOW_ROWS
    base = bt.signal_key(open_scan.started_signal, flags, cap)
    assert base == bt.signal_key(open_scan.started_signal, open_scan.MethodFlags(fresh(), VARIANT), cap)
    assert base != bt.signal_key(open_scan.started_signal, rec.flag, cap)
    for mod, name in ((method, "flag_whale"), (method, "lookup_priors"), (net, "exact_prior")):
        orig = getattr(mod, name)

        def changed(*a, **kw):                # the same answers, another source: must recompute
            return orig(*a, **kw)
        setattr(mod, name, changed)
        try:
            assert bt.signal_key(open_scan.started_signal, flags, cap) != base, name
        finally:
            setattr(mod, name, orig)
    assert bt.signal_key(open_scan.started_signal, flags, cap) == base
    old = method.FIRST_X_TRADES
    method.FIRST_X_TRADES = old + 1
    try:
        assert bt.signal_key(open_scan.started_signal, flags, cap) != base
    finally:
        method.FIRST_X_TRADES = old


def test_cli_refuses_missing_db():
    tmp = tempfile.mkdtemp(prefix="bt_cli_")
    try:
        missing, out = os.path.join(tmp, "no_such.db"), os.path.join(tmp, "o.json")
        try:
            bt.main(["--db", missing, "--no-started", "--no-gamma", "--out", out])
            raise AssertionError("a --db that does not exist must be refused")
        except SystemExit as e:
            assert e.code == 2
        assert not os.path.exists(missing) and not os.path.exists(out)    # nothing created
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
    shutil.rmtree(EMPTY_LISTS, ignore_errors=True)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)
