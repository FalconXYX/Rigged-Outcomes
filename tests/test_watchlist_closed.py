#!/usr/bin/env python3
"""
Offline tests for the closed scan (watchlist.closed_scan): no network, a
second or two.

    python3 -m pytest -q tests/test_watchlist_closed.py     # or
    python3 tests/test_watchlist_closed.py                  # same checks, exits 1 on failure

Covers the ranking rule (flagged_pct among events with >= $10,000 flagged,
top 5), the $10k floor, day bucketing by closedTime (UTC, half-open
days), the per-(day, event) grouping of EVERY kept market, the AI type
exclusion (excluded types dropped before analysis, a failed label keeps
its market), the figure rule (an event-day stands on its market with the
highest flagged capital, not on the one that resolved Yes), the event row
(one per event, the same rule over all its days, its AI type), the
heavy-market lane and the budget, and whole scans over a scripted window
with a fake method: resume (nothing analysed twice), API errors retried
rather than stored, partial event-days left unranked, the report, the
comparison with an earlier report and the backfill projection. The list
walk, the method and the labeller are injected fakes; a test that would
reach the network (Polymarket or Gemini) fails instead.
"""
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket_api  # noqa: E402
from watchlist import ai, method, net, store  # noqa: E402
from watchlist import closed_scan as cs  # noqa: E402
from watchlist import filters  # noqa: E402
from watchlist.common import DAY, to_epoch, utc_day  # noqa: E402

NOV3 = to_epoch("2026-11-03T00:00:00Z")
NOV4 = NOV3 + DAY
NOV5 = NOV4 + DAY


@contextlib.contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@contextlib.contextmanager
def offline_env():
    """A temp list cache, an in-memory store, and no network at all (the
    Gemini client included)."""
    tmp = tempfile.mkdtemp(prefix="wl_closed_")

    def no_network(*a, **k):
        raise AssertionError(f"network call in an offline test: {a[:1]}")

    try:
        with patched(cs, "LIST_DIR", tmp), patched(polymarket_api, "get_json", no_network), \
                patched(net, "get_json", no_network), patched(ai.Client, "generate", no_network), \
                patched(cs, "log", lambda msg: None), patched(ai, "log", lambda msg: None):
            yield store.connect(":memory:")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mk(mid, closed, event_id="E1", q=None, vol=150_000, prices='["1", "0"]', neg_risk=False, tags=(), title=None,
       **kw):
    """A resolved Gamma /markets object (Yes won unless prices say otherwise)."""
    m = {"id": str(mid), "conditionId": "0x" + f"{int(mid):064x}", "question": q or f"Will thing {mid} happen?",
         "slug": f"thing-{mid}", "outcomes": '["Yes", "No"]', "outcomePrices": prices, "volumeNum": vol,
         "closedTime": closed, "updatedAt": closed, "umaResolutionStatus": "resolved", "negRisk": neg_risk,
         "closed": True, "tags": [{"slug": t} for t in tags], "image": "x" * 50,
         "description": f"This market resolves to Yes if thing {mid} happens by the listed date.",
         "events": [{"id": event_id, "title": title or f"Event {event_id}", "slug": f"event-{event_id.lower()}",
                     "description": "long text", "seriesSlug": None}]}
    m.update(kw)
    return m


def cid(mid):
    return "0x" + f"{int(mid):064x}"


def entry(eid, pct, cap, status="ok"):
    return {"event_id": eid, "flagged_pct": pct, "flagged_capital": cap, "status": status}


# ── ranking ──────────────────────────────────────────────────────────────────

def test_rank_day_orders_by_flagged_pct_and_keeps_five():
    es = [entry("a", 40, 50_000), entry("b", 90, 20_000), entry("c", 10, 400_000), entry("d", 60, 11_000),
          entry("e", 75, 30_000), entry("f", 55, 12_000), entry("g", 5, 1_000_000)]
    top = cs.rank_day(es)
    assert [e["event_id"] for e in top] == ["b", "e", "d", "f", "a"]     # by %, not by dollars; at most 5
    assert len(cs.rank_day(es, top=None)) == 7


def test_ten_thousand_dollar_floor_and_status():
    es = [entry("tiny", 100, 9_999.99), entry("edge", 50, 10_000), entry("big", 30, 2_000_000),
          entry("huge", 99, 5_000_000, status="too_large"), entry("none", 0, 0), entry("pend", None, None, "pending"),
          entry("part", 99, 50_000, status="partial")]
    top = cs.rank_day(es)
    assert [e["event_id"] for e in top] == ["edge", "big"]     # 100 % of $9,999 is not enough; $10,000 is
    assert cs.rank_day(es, min_flagged=0, top=10)[0]["event_id"] == "tiny"
    assert cs.rank_day([]) == []                                # a quiet day lists nothing, not filler


def test_rank_ties_break_on_flagged_capital_then_event_id():
    es = [entry("x", 50, 20_000), entry("y", 50, 80_000), entry("a", 50, 20_000)]
    assert [e["event_id"] for e in cs.rank_day(es)] == ["y", "a", "x"]


# ── day bucketing ────────────────────────────────────────────────────────────

def test_close_day_is_the_utc_day_of_closed_time():
    assert cs.close_day({"closedTime": "2026-11-03 23:59:59.999+00"}) == utc_day(NOV3)
    assert cs.close_day({"closedTime": "2026-11-04 00:00:00+00"}) == utc_day(NOV4)
    assert cs.close_day({"closedTime": "2026-11-03T22:30:00-05:00"}) == utc_day(NOV4)   # 03:30 UTC next day
    assert cs.close_day({"closedTime": None, "endDate": "2026-11-03T00:00:00Z"}) is None   # never the endDate


def test_day_windows_are_half_open_and_clipped():
    ws = cs.day_windows(NOV3, NOV5)
    assert [(d, lo, hi) for d, lo, hi in ws] == [(utc_day(NOV3), NOV3, NOV4), (utc_day(NOV4), NOV4, NOV5)]
    ws = cs.day_windows(NOV3 + 3600, NOV4 + 60)
    assert ws == [(utc_day(NOV3), NOV3 + 3600, NOV4), (utc_day(NOV4), NOV4, NOV4 + 60)]
    assert cs.day_windows(NOV3, NOV3 + 1) == [(utc_day(NOV3), NOV3, NOV3 + 1)]


# ── classification, AI roles, grouping ───────────────────────────────────────

def lab(t):
    return {"type": t, "excluded": t in ai.EXCLUDED_TYPES, "cached": False}


def test_plan_groups_every_kept_market_per_day_and_event():
    ms = [
        # E1 closes markets on two days: two groups with ALL that day's kept markets
        mk(1, "2026-11-03 10:00:00+00", "E1", vol=300_000, prices='["0", "1"]'),
        mk(2, "2026-11-03 23:59:59+00", "E1", vol=200_000, neg_risk=True),
        mk(3, "2026-11-04 00:00:00+00", "E1", vol=900_000, prices='["0", "1"]'),
        mk(4, "2026-11-03 12:00:00+00", "E2", vol=5_000_000, prices='["0", "1"]'),
        # keyword-excluded, unresolved, AI-excluded
        mk(5, "2026-11-03 12:00:00+00", "G", q="Lakers vs. Celtics", tags=["sports"]),
        mk(6, "2026-11-03 12:00:00+00", "P", q="Bitcoin Up or Down on Nov 3?"),
        mk(7, "2026-11-04 12:00:00+00", "U", prices='["0.5", "0.5"]'),
        mk(8, "2026-11-04 12:00:00+00", "W", q="Highest temperature in NYC on Nov 4?"),
        mk(9, "2026-11-03 12:00:00+00", "S", q="Will STRC hit $100 by December 31?"),
        mk(10, "2026-11-04 12:00:00+00", "K", q="Kai Cenat GTA marathon ends by Nov 4?"),
    ]
    labels = {"1": lab("military_gov_action"), "2": lab("military_gov_action"), "3": lab("military_gov_action"),
              "4": lab("election_vote"), "10": lab("game_sport")}
    groups, rows = cs.plan_window(ms, labels)
    keys = [(g["day"] - utc_day(NOV3), g["event_id"], [m["id"] for m in g["markets"]]) for g in groups]
    assert keys == [(0, "E2", ["4"]), (0, "E1", ["1", "2"]), (1, "E1", ["3"])]     # highest volume first
    role = {r["market"]["id"]: (r["role"], r["reason"]) for r in rows}
    assert {k: v[0] for k, v in role.items()} == {"1": "kept", "2": "kept", "3": "kept", "4": "kept",
                                                  "5": "excluded", "6": "excluded", "7": "unresolved",
                                                  "8": "excluded", "9": "excluded", "10": "excluded"}
    assert role["1"][1] == "ai: military_gov_action" and role["10"][1] == "ai: game_sport"
    assert role["5"][1].startswith("game:") and role["6"][1].startswith("price:")
    assert role["8"][1].startswith("weather:") and role["9"][1] == "price: pattern hit/reach $<price>"
    assert role["7"][1].startswith("no single winner")
    assert all(r["winner"] is None for r in rows if r["role"] in ("excluded", "unresolved"))
    assert next(r for r in rows if r["market"]["id"] == "1")["winner"] == "No"
    # no label (a failed call) keeps the market; no AI step at all says so
    _, rows = cs.plan_window(ms[:1], {})
    assert (rows[0]["role"], rows[0]["reason"]) == ("kept", cs.AI_UNLABELLED)
    _, rows = cs.plan_window(ms[:1], None)
    assert (rows[0]["role"], rows[0]["reason"]) == ("kept", cs.AI_NOT_ASKED)
    assert cs.ai_type("ai: company_product") == "company_product"
    assert cs.ai_type(cs.AI_UNLABELLED) is None and cs.ai_type("game: tag sports") is None


def test_choose_figure_takes_the_most_flagged_capital_not_the_yes_market():
    # a ceasefire ladder: the market that resolved Yes carried little flagged money; a sibling carried it
    c = [{"condition_id": "yes", "status": "ok", "flagged_capital": 12_000, "flagged_pct": 5.0, "volume": 9e6},
         {"condition_id": "no", "status": "ok", "flagged_capital": 90_000, "flagged_pct": 70.0, "volume": 3e5},
         {"condition_id": "big", "status": "too_large", "flagged_capital": None, "volume": 4e7},
         {"condition_id": "todo", "status": None, "flagged_capital": None, "volume": 5e7}]
    assert cs.choose_figure(c)["condition_id"] == "no"
    tie = [dict(c[0], flagged_capital=90_000), c[1]]
    assert cs.choose_figure(tie)["condition_id"] == "no"            # equal dollars: the higher %
    assert cs.choose_figure([c[2], c[3]])["condition_id"] == "big"  # no ok result: the largest with a result
    assert cs.choose_figure([c[3]]) is None and cs.choose_figure([]) is None


def test_event_status():
    assert cs.event_status(["ok", "ok", "too_large"]) == "ok"
    assert cs.event_status(["ok", "pending"]) == "partial"
    assert cs.event_status(["ok", "error"]) == "partial"
    assert cs.event_status(["pending", "error"]) == "error"
    assert cs.event_status(["pending", "too_large"]) == "pending"
    assert cs.event_status(["too_large", "no_trades"]) == "too_large"
    assert cs.event_status(["no_trades"]) == "no_trades"


def test_slim_market_keeps_what_the_rules_and_the_labeller_read():
    m = mk(9, "2026-11-03 12:00:00+00", "E9", tags=["politics"], negRisk=True, clobTokenIds="[1,2]", feeSchedule={})
    m["events"][0]["series"] = [{"id": 1, "slug": "s-1", "title": "S", "image": "x"}]
    s = cs.slim_market(m)
    assert "image" not in s and "clobTokenIds" not in s and "description" not in s["events"][0]
    assert s["description"] == m["description"] and ai.rules_text(s) == ai.rules_text(m)
    assert s["events"][0]["series"] == [{"id": 1, "slug": "s-1", "title": "S", "recurrence": None}]
    assert filters.exclusion(s) == filters.exclusion(m) and filters.winning_outcome(s) == filters.winning_outcome(m)
    assert filters.series_key(filters.event_of(s)) == "s-1" and s["negRisk"] is True


def test_api_calls_counts_pages_and_uncached_lookups():
    assert cs.api_calls({"trades_source": "api", "n_trades": 7060, "lookups": {"fetched": 17, "cached": 2},
                         "cutoff_variant": "same_day"}) == 8 + 17
    assert cs.api_calls({"trades_source": "cache", "n_trades": 7060, "lookups": {"fetched": 3}}) == 3
    assert cs.api_calls({"status": "too_large", "n_trades": 200_001}) == 201
    # exact: up to two user-volume calls per uncached whale + the /v2/activity pages
    assert cs.api_calls({"trades_source": "api", "n_trades": 1000, "cutoff_variant": "exact",
                         "lookups": {"fetched": 10, "exact_requests": 4}}) == 1 + 20 + 4
    assert cs.pctl([1, 2, 3, 4], 0.5) == 2.5 and cs.pctl([], 0.5) is None


def test_project_backfill():
    p = cs.project_backfill(19_400, 0.2, 0.21, 20.0, ai_batch=15, ai_interval=4.2, list_pages_per_s=5.0)
    assert p["markets_analysed"] == 3_880 and p["markets_labelled"] == 4_074 and p["ai_calls"] == 272
    assert p["minutes"]["analyse"] == 194.0 and p["minutes"]["ai"] == round(272 * 4.2 / 60, 1)
    assert p["minutes"]["list"] == round(194 / 5 / 60, 1)
    assert p["hours_total"] == round((194 + 272 * 4.2 / 60 + 194 / 5 / 60) / 60, 2)
    assert cs.project_backfill(100, 0.5, 0.5, None)["hours_total"] is None


# ── lanes and budget ─────────────────────────────────────────────────────────

class StepBudget:
    """A Budget whose time left is scripted (a deadline exists)."""
    deadline = 0

    def __init__(self, lefts):
        self.lefts = list(lefts)

    def left(self):
        return self.lefts.pop(0) if len(self.lefts) > 1 else self.lefts[0]

    def expired(self, reserve=0.0):
        return self.left() <= reserve


def g(mid, vol):
    return {"market": {"id": mid, "volumeNum": vol}}


def test_heavy_markets_run_one_at_a_time():
    lanes = cs._Lanes([g("h1", 9e6), g("h2", 8e6), g("l1", 2e5)], StepBudget([1e9]), heavy_volume=3e6)
    a = lanes.take()
    b = lanes.take()
    assert a["market"]["id"] == "h1" and b["market"]["id"] == "l1"   # h2 waits while h1 downloads
    assert lanes.take() is None
    lanes.done(a)
    assert lanes.take()["market"]["id"] == "h2"


def test_budget_stops_new_work_by_size():
    # 60 s left: too little for a heavy market (120 s), enough for a light one (20 s)
    lanes = cs._Lanes([g("h1", 9e6), g("l1", 2e5)], StepBudget([60]), heavy_volume=3e6)
    assert lanes.take()["market"]["id"] == "l1" and lanes.take() is None and len(lanes.items) == 1
    lanes = cs._Lanes([g("l1", 2e5)], StepBudget([cs.LIGHT_NEED_S]), heavy_volume=3e6)
    assert lanes.take() is None


# ── whole scans with a fake method and a fake labeller ───────────────────────

def window_markets():
    return [
        mk(11, "2026-11-03 09:00:00+00", "A", vol=400_000, title="Strike by Nov 3?"),
        mk(12, "2026-11-03 10:00:00+00", "B", vol=300_000, prices='["0", "1"]', title="Deal by Nov 2?"),
        mk(13, "2026-11-03 11:00:00+00", "C", vol=250_000, title="CEO out?"),
        # ladder L, one day: the Yes market (18) carried little flagged money, the No sibling (19) a lot
        mk(18, "2026-11-03 12:00:00+00", "L", vol=900_000, title="Ceasefire continues through...?"),
        mk(19, "2026-11-03 12:00:00+00", "L", vol=300_000, prices='["0", "1"]', title="Ceasefire continues through...?"),
        mk(14, "2026-11-04 09:00:00+00", "A", vol=600_000, title="Strike by Nov 3?"),
        mk(15, "2026-11-04 09:30:00+00", "D", vol=40_000_000, title="Who wins the House?", neg_risk=True),
        mk(16, "2026-11-04 10:00:00+00", "D", vol=500_000, prices='["0", "1"]', title="Who wins the House?",
           neg_risk=True),
        mk(17, "2026-11-04 12:00:00+00", "G", q="Lakers vs. Celtics", tags=["sports"]),
        mk(20, "2026-11-04 13:00:00+00", "K", vol=700_000, title="Kai Cenat GTA marathon?"),   # AI: game_sport
    ]


# flagged % and flagged $ per market id in the fake method
FAKE = {"11": (80.0, 50_000), "12": (100.0, 9_000), "13": (20.0, 15_000), "14": (60.0, 120_000), "16": (10.0, 1),
        "18": (5.0, 12_000), "19": (70.0, 90_000), "20": (99.0, 500_000)}
TYPES = {"11": "military_gov_action", "12": "court_regulator", "13": "person_leaves_role",
         "14": "military_gov_action", "15": "election_vote", "16": "election_vote", "18": "military_gov_action",
         "19": "military_gov_action", "20": "game_sport"}
KEPT = sorted(set(TYPES) - {"20"})


def fake_method(calls, fail=(), status=None):
    def analyze(market, cutoff_variant="exact", conn=None, max_rows=200_000, max_volume=None, **kw):
        mid = market["id"]
        calls.append((mid, cutoff_variant))
        if mid in fail:
            raise polymarket_api.PolymarketAPIError("HTTP 503 from /v2/trades")
        ev = filters.event_of(market)
        res = {"status": "ok", "cutoff_variant": cutoff_variant, "condition_id": market["conditionId"],
               "market_id": mid, "question": market["question"], "event_id": ev.get("id"),
               "event_title": ev.get("title"), "event_slug": ev.get("slug"), "gamma_volume": market["volumeNum"],
               "closed_ts": to_epoch(market["closedTime"]), "trades_source": "api", "n_trades": 3_000,
               "lookups": {"fetched": 5, "cached": 1, "exact_checks": 2, "exact_requests": 2},
               "decided_by": {"agree": 8, "exact_check": 2},
               "timing_s": {"trades": 1.0, "lookups": 0.5, "total": 2.0}}
        if filters.volume(market) > 10_000_000 or (status or {}).get(mid) == "too_large":
            res.update(status="too_large", n_trades=200_001, trades_capped=True,
                       too_large="the fill walk was capped; not analysed")
        else:
            pct, cap = FAKE[mid]
            res.update(whale_count=10, whale_capital=round(cap / pct * 100, 2), flagged_count=3, flagged_capital=cap,
                       flagged_pct=pct, jump_time="2026-11-02T12:00:00+00:00", reveal_basis="price_jump",
                       flagged_capital_before_jump=cap / 2,
                       whales=[{"user_id": "0x" + "1" * 40, "wallet": "0x" + "1" * 40, "position": cap,
                                "avg_odds": 0.2, "first_trade": "2026-11-01T00:00:00+00:00",
                                "first_trade_ts": 1, "prior_trade_count": 2, "prior_volume_usdc": 10.0,
                                "concentration": 99.0, "is_first_x": True, "is_percent": True, "flagged": True,
                                "before_jump": True, "hours_before_jump": 36.0, "decided_by": "agree"}])
        store.save_result(conn, res)
        return res
    return analyze


def fake_labeller(seen, fail=(), types=None):
    """A stand-in for ai_labels: labels from TYPES, none for `fail`."""
    def label(markets, conn, budget):
        seen.append(sorted(m["id"] for m in markets))
        labels = {m["id"]: lab((types or TYPES)[m["id"]]) for m in markets if m["id"] not in fail}
        return labels, {"candidates": len(markets), "labelled": len(labels), "cached": 0, "failed": len(fail),
                        "excluded": {}}
    return label


def fetcher(markets, seen=None):
    def fetch(since, until, min_volume=100_000, on_page=None):
        if seen is not None:
            seen.append((since, until))
        if on_page:
            on_page(1, markets)
        return [m for m in markets if since <= to_epoch(m["closedTime"]) < until and m["volumeNum"] >= min_volume]
    return fetch


def run(conn, since=NOV3, until=NOV5, calls=None, markets=None, labeller=None, **kw):
    calls = calls if calls is not None else []
    kw.setdefault("analyze", fake_method(calls))
    return cs.scan(since, until, conn=conn, fetch=fetcher(markets or window_markets()),
                   label=labeller or fake_labeller([]), **kw)


def test_scan_analyses_every_kept_market_once_and_resumes():
    with offline_env() as conn:
        calls, walks, seen = [], [], []
        fetch, analyze = fetcher(window_markets(), walks), fake_method(calls)
        # left() is read once for the start-of-run log line, then once per market handed out
        s1 = cs.scan(NOV3, NOV5, conn=conn, budget=StepBudget([1e9] * 4 + [0]), workers=1, analyze=analyze,
                     fetch=fetch, label=fake_labeller(seen))
        assert s1["kept_markets"] == 8 and s1["groups"] == 6 and s1["analysed"] == 3 and s1["left"] == 5
        assert s1["markets"] == {"kept": 8, "excluded": 2}          # the game by keyword, Kai Cenat by AI type
        s2 = cs.scan(NOV3, NOV5, conn=conn, workers=3, analyze=analyze, fetch=fetch, label=fake_labeller(seen))
        assert s2["analysed"] == 5 and s2["left"] == 0
        s3 = cs.scan(NOV3, NOV5, conn=conn, workers=3, analyze=analyze, fetch=fetch, label=fake_labeller(seen))
        assert s3["analysed"] == 0
        assert sorted(c[0] for c in calls) == KEPT                  # every kept market once; "20" never
        assert {c[1] for c in calls} == {method.DEFAULT_VARIANT} == {"exact"}
        assert seen[0] == sorted(TYPES)                             # the labeller saw the keyword candidates only
        assert len(walks) == 1 and s2["list"]["cached"] and s3["list"]["cached"]  # the window list is cached
        assert store.get_market(conn, cid(20))["role"] == "excluded"
        assert store.get_market(conn, cid(20))["reason"] == "ai: game_sport"
        assert store.get_market(conn, cid(17))["role"] == "excluded"
        assert store.get_market(conn, cid(19))["role"] == "kept"


def test_event_row_uses_the_figure_over_all_days_and_its_ai_type():
    with offline_env() as conn:
        run(conn)
        # A: Nov 3 market $50k flagged, Nov 4 market $120k -> the Nov 4 one stands for the event
        ev = store.get_event(conn, "A")
        assert ev["main_condition_id"] == cid(14) and ev["other_condition_ids"] == [cid(11)]
        assert ev["closed_ts"] == to_epoch("2026-11-04 09:00:00+00") and ev["type"] == "military_gov_action"
        # L: the No sibling, not the Yes market
        assert store.get_event(conn, "L")["main_condition_id"] == cid(19)
        # D: the too_large market is not the figure; the ok one is
        assert store.get_event(conn, "D")["main_condition_id"] == cid(16)
        rows = store.closed_results(conn, "exact")    # one row per event
        assert sorted((r["event_id"], r["condition_id"]) for r in rows) == \
            [("A", cid(14)), ("B", cid(12)), ("C", cid(13)), ("D", cid(16)), ("L", cid(19))]
        assert store.get_event(conn, "K") is None     # AI-excluded: never analysed, no event row


def test_variants_default_exact_and_both():
    with offline_env() as conn:
        calls = []
        ms = window_markets()[:3]
        run(conn, NOV3, NOV4, calls, ms)
        run(conn, NOV3, NOV4, calls, ms, variants=("same_day", "day_before"))
        assert sorted(calls) == sorted([(m, v) for m in ("11", "12", "13") for v in ("exact", "same_day",
                                                                                      "day_before")])
        raises = False
        try:
            run(conn, NOV3, NOV4, calls, ms, variants=("bogus",))
        except ValueError:
            raises = True
        assert raises
        assert cs._VARIANT_SETS["both"] == method.ONE_CALL_VARIANTS and cs._VARIANT_SETS["all"] == method.VARIANTS


def test_ai_failure_keeps_markets_and_relabels_next_run():
    with offline_env() as conn:
        calls, seen = [], []
        ms = window_markets()
        # the label for "20" fails: it is kept and analysed this run
        run(conn, calls=calls, markets=ms, labeller=fake_labeller(seen, fail={"20"}))
        assert ("20", "exact") in calls
        assert store.get_market(conn, cid(20))["reason"] == cs.AI_UNLABELLED
        rep = cs.build_report(conn, NOV3, NOV5)
        assert rep["totals"]["ai_unlabelled"] == 1
        assert rep["days"][1]["top5"][0]["event_id"] == "K"
        # next run labels it game_sport: excluded, its result no longer read, nothing re-analysed
        n = len(calls)
        run(conn, calls=calls, markets=ms, labeller=fake_labeller(seen))
        assert len(calls) == n and store.get_market(conn, cid(20))["role"] == "excluded"
        rep = cs.build_report(conn, NOV3, NOV5)
        assert "K" not in [c["event_id"] for d in rep["days"] for c in d["top5"]]
        assert rep["days"][1]["ai_excluded"] == {"game_sport": 1} and rep["days"][1]["excluded"] == {"game": 1, "ai": 1}


class FakeGemini(ai.Client):
    """The real ai.Client bookkeeping, with generate() answering from TYPES."""

    def __init__(self, refuse=False):
        super().__init__(min_interval=0, sleep=lambda s: None)
        self.prompts = []
        self.refuse = refuse

    def generate(self, prompt, schema=None):
        if self.refuse:
            raise ai.GeminiError("x: HTTP 403: key refused", status=403)
        self.prompts.append(prompt)
        ids = re.findall(r"### Market id=(\S+)", prompt)
        out = [{"id": i, "type": TYPES[i], "reveal_kind": "any_day", "reveal_date": None,
                "quote": f"resolves to Yes if thing {i} happens"} for i in ids]
        return json.dumps({"labels": out}), "fake-model", {}


def test_ai_labels_goes_through_label_markets_and_its_cache():
    with offline_env() as conn:
        ms = [m for m in window_markets() if cs.classify(m)[0] == "candidate"]
        c1 = FakeGemini()
        labels, info = cs.ai_labels(ms, conn, client=c1)
        assert sorted(labels) == sorted(TYPES) and labels["20"]["excluded"] and not labels["11"]["excluded"]
        assert info["labelled"] == 9 and info["cached"] == 0 and info["excluded"] == {"game_sport": 1}
        assert len(c1.prompts) == 1                                  # 9 markets: one call
        c2 = FakeGemini()
        labels, info = cs.ai_labels(ms, conn, client=c2)
        assert not c2.prompts and info["cached"] == 9                # every label from the cache
        # a refused key: nothing labelled, nothing raised, every market kept
        with offline_env() as conn2:
            labels, info = cs.ai_labels(ms, conn2, client=FakeGemini(refuse=True))
            assert labels == {} and info["failed"] == 9 and "403" in info["error"]


def test_api_errors_are_not_stored_and_are_retried():
    with offline_env() as conn:
        calls = []
        ms = window_markets()[:5]
        s = run(conn, NOV3, NOV4, markets=ms, analyze=fake_method(calls, fail={"18"}))
        assert s["errors"] == 1 and s["analysed"] == 4
        assert store.get_result(conn, cid(18), "exact") is None
        assert cid(18) in store.get_meta(conn, cs.ERRORS_KEY)
        rep = cs.build_report(conn, NOV3, NOV4)
        assert rep["complete"] is False and rep["pending"][0]["status"] == "error"
        # L has one market done and one in error: partial, not ranked, though 70 % of $90k is flagged
        lev = next(e for e in rep["days"][0]["events"] if e["event_id"] == "L")
        assert lev["status"] == "partial" and lev["rank"] is None and lev["eligible"] is False
        assert "L" not in [c["event_id"] for c in rep["days"][0]["top5"]]
        s = run(conn, NOV3, NOV4, markets=ms, analyze=fake_method(calls))
        assert s["analysed"] == 1 and cid(18) not in store.get_meta(conn, cs.ERRORS_KEY)
        rep = cs.build_report(conn, NOV3, NOV4)
        assert rep["complete"] is True and rep["days"][0]["top5"][1]["event_id"] == "L"


def test_capped_walks_are_listed_too_large_and_not_retried():
    with offline_env() as conn:
        calls = []
        ms = window_markets()[:1]
        run(conn, NOV3, NOV4, calls, ms, analyze=fake_method(calls, status={"11": "too_large"}))
        run(conn, NOV3, NOV4, calls, ms, analyze=fake_method(calls, status={"11": "too_large"}))
        assert calls == [("11", "exact")]
        rep = cs.build_report(conn, NOV3, NOV4)
        assert rep["too_large"][0]["question"] == ms[0]["question"] and rep["days"][0]["statuses"] == {"too_large": 1}


def test_report_days_figures_top5_and_measurements():
    with offline_env() as conn:
        run(conn, workers=2)
        rep = cs.build_report(conn, NOV3, NOV5)
        d3, d4 = rep["days"]
        assert (d3["day"], d4["day"]) == ("2026-11-03", "2026-11-04")
        assert d3["events_scanned"] == 4 and d4["events_scanned"] == 2 and d4["excluded"] == {"game": 1, "ai": 1}
        assert d3["markets_kept"] == 5 and d4["markets_kept"] == 3
        assert d4["excluded_reasons"][0]["n"] == 1
        # Nov 3: L 70 % (its No market), A 80 %, C 20 %; B is 100 % flagged but only $9,000 -> not listed
        assert [c["event_id"] for c in d3["top5"]] == ["A", "L", "C"]
        lcard = d3["top5"][1]
        assert lcard["figure_condition_id"] == cid(19) and lcard["flagged_pct"] == 70.0 and lcard["n_markets"] == 2
        assert lcard["figure_question"] == window_markets()[4]["question"] and lcard["winner"] == "No"
        assert [o["flagged_capital"] for o in lcard["other_markets"]] == [12_000]
        assert lcard["top_flagged_whales"][0]["concentration"] == 99.0 and lcard["type"] == "military_gov_action"
        lev = next(e for e in d3["events"] if e["event_id"] == "L")
        assert [x["condition_id"] for x in lev["markets"]] == [cid(19), cid(18)]
        assert lev["figure_market"]["condition_id"] == cid(19) and "highest flagged capital" in lev["figure_rule"]
        b = next(e for e in d3["events"] if e["event_id"] == "B")
        assert b["eligible"] is False and b["rank"] is None and b["flagged_pct"] == 100.0
        # Nov 4: A again (its Nov 4 market); D's too_large market listed, its ok sibling is the figure
        assert [c["event_id"] for c in d4["top5"]] == ["A"]
        d = next(e for e in d4["events"] if e["event_id"] == "D")
        assert d["status"] == "ok" and d["n_markets"] == 2 and d["figure_market"]["condition_id"] == cid(16)
        assert d["statuses"] == {"ok": 1, "too_large": 1}
        assert [t["question"] for t in rep["too_large"]] == [window_markets()[6]["question"]]
        assert rep["complete"] and rep["totals"]["events_scanned"] == 6 and rep["totals"]["listed"] == 4
        assert rep["totals"]["markets_kept"] == 8 and rep["totals"]["markets_analysed"] == 8
        assert rep["cutoff_variant"] == "exact"
        m = rep["measurements"]
        assert m["events_per_day"] == {"2026-11-03": 4, "2026-11-04": 2}
        assert m["seconds_per_market"]["n"] == 8 and m["api_calls_per_market"]["max"] == 201 + 10 + 2
        assert m["runs_for_this_window"] == 1 and m["workers"] == 2 and m["decided_by"] == {"agree": 56,
                                                                                              "exact_check": 14}
        assert m["markets_per_minute_fresh_estimate"] is None        # fewer than 10 fresh markets: no estimate
        assert ("backfill_projection" in m) == bool(m["markets_per_minute_measured"])


def test_variant_summary_only_when_complete():
    with offline_env() as conn:
        ms = window_markets()[:3]
        run(conn, NOV3, NOV4, markets=ms)
        assert cs.variant_summary(conn, NOV3, NOV4, "day_before") is None          # nothing scored that way yet
        run(conn, NOV3, NOV4, markets=ms, variants=("day_before",), budget=StepBudget([1e9] * 3 + [0]), workers=1)
        assert len(store.analysed_conditions(conn, "day_before")) == 2
        assert cs.variant_summary(conn, NOV3, NOV4, "day_before") is None          # 2 of 3 is not a list
        run(conn, NOV3, NOV4, markets=ms, variants=("day_before",))
        v = cs.variant_summary(conn, NOV3, NOV4, "day_before")
        assert v["complete"] and [c["event_title"] for c in v["days"][0]["top5"]] == ["Strike by Nov 3?", "CEO out?"]
        assert cs.build_report(conn, NOV3, NOV4)["measurements"]["runs_for_this_window"] == 1   # exact runs only


def test_pending_markets_are_reported():
    with offline_env() as conn:
        run(conn, NOV3, NOV4, plan_only=True)
        rep = cs.build_report(conn, NOV3, NOV4)
        assert rep["complete"] is False and len(rep["pending"]) == 5
        assert {e["status"] for e in rep["days"][0]["events"]} == {"pending"}
        assert rep["measurements"]["runs_for_this_window"] == 0      # plan-only runs are not cost samples


def test_compare_previous_explains_the_changes():
    old = {"cutoff_variant": "same_day", "days": [{
        "day": "2026-11-03",
        "top5": [{"rank": 1, "event_id": "S", "event_title": "STRC hits $100 by...", "flagged_pct": 19.7,
                  "flagged_capital": 45_000, "main_question": "Will STRC hit $100 by December 31?"},
                 {"rank": 2, "event_id": "A", "event_title": "Strike by Nov 3?", "flagged_pct": 80.0,
                  "flagged_capital": 50_000, "main_question": "q11"},
                 {"rank": 3, "event_id": "C", "event_title": "CEO out?", "flagged_pct": 20.0,
                  "flagged_capital": 15_000, "main_question": "q13"}],
        "events": [{"event_id": "S", "main": {"condition_id": "0xs", "question": "STRC"}},
                   {"event_id": "A", "main": {"condition_id": cid(11)}},
                   {"event_id": "C", "main": {"condition_id": cid(13)}},
                   {"event_id": "L", "main": {"condition_id": cid(18), "question": "Yes market"}, "flagged_pct": 5.0,
                    "flagged_capital": 12_000, "eligible": True}]}]}
    new = {"days": [{
        "day": "2026-11-03",
        "top5": [{"rank": 1, "event_id": "L", "event_title": "Ceasefire", "flagged_pct": 70.0, "flagged_capital": 90_000,
                  "figure_question": "No market", "figure_condition_id": cid(19)},
                 {"rank": 2, "event_id": "A", "event_title": "Strike by Nov 3?", "flagged_pct": 60.0,
                  "flagged_capital": 40_000, "figure_question": "q11", "figure_condition_id": cid(11)}],
        "events": [{"event_id": "A", "status": "ok", "flagged_capital": 40_000, "flagged_pct": 60.0, "position": 2},
                   {"event_id": "L", "status": "ok", "flagged_capital": 90_000, "flagged_pct": 70.0, "position": 1},
                   {"event_id": "C", "status": "ok", "flagged_capital": 9_000, "flagged_pct": 12.0, "position": None}]}]}
    roles = {"0xs": ("excluded", "price: pattern hit/reach $<price>", None)}
    (d,) = cs.compare_previous(old, new, roles)
    assert d["before"] == ["STRC hits $100 by...", "Strike by Nov 3?", "CEO out?"]
    assert d["after"] == ["Ceasefire", "Strike by Nov 3?"]
    why = {x["event_title"]: x["why"] for x in d["dropped"]}
    assert why["STRC hits $100 by..."] == "no longer scanned: its market is excluded (price: pattern hit/reach $<price>)"
    assert why["CEO out?"].startswith("flagged $9,000 < $10,000")
    assert d["kept"][0]["was"]["rank"] == 2 and d["kept"][0]["now"]["flagged_pct"] == 60.0
    assert d["added"][0]["why"].startswith("figure from another market than the old main ('Yes market'")


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
