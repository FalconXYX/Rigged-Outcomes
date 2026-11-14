#!/usr/bin/env python3
"""
Offline tests for the track record (watchlist.track_record): no network,
under a second.

    python3 -m pytest -q tests/test_watchlist_track.py     # or
    python3 tests/test_watchlist_track.py                  # same checks, exits 1 on failure

Covers the Wilson interval (textbook values, k = 0 / k = n / n = 0,
symmetry), the chi-square tail (closed forms for df 1 and 2, table
values), the test of independence and its permutation p-value, the heavy
threshold (10 % counts, 9.99 % does not, a $500 market at 100 % is heavy
but small), time (as_of_ts keeps only events closed strictly before it,
examples never show a later event, a date ladder's figure is re-chosen
among the rungs closed by then, as_of after everything equals the stored
main rows), the series -> type -> none fallback, what is left out (non-ok
results, excluded types, markets no longer kept), the snapshot cache
seeing new writes, the table (vs_overall, the type test's inputs) and the
CLI writing its JSON from a store file opened read-only.

Fixtures go through the closed scan's own writers (store.save_market /
save_result and closed_scan.refresh_events), so the event rows have the
main market and type the real scan would give them.
"""
import json
import math
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from watchlist import closed_scan, store  # noqa: E402
from watchlist import track_record as tr  # noqa: E402
from watchlist.common import DAY, iso, to_epoch  # noqa: E402

NOV1 = to_epoch("2026-11-01T00:00:00Z")
VARIANT = "exact"
_N = [0]


def cid():
    _N[0] += 1
    return "0x%064x" % _N[0]


def add_event(conn, eid, markets, type_="election_vote", series=None, title=None, refresh=True):
    """One Gamma event with its markets, written the way the closed scan
    writes them. markets: [dict(closed_ts, pct, cap=50_000, status="ok",
    role="kept", reason=None, volume=200_000)]. Returns the condition ids."""
    title = title or f"Event {eid}?"
    event = {"id": eid, "title": title, "slug": f"event-{eid}", **({"seriesSlug": series} if series else {})}
    ids = []
    for spec in markets:
        c = cid()
        ids.append(c)
        m = {"conditionId": c, "id": f"m{c[-6:]}", "question": f"{title} ({len(ids)})", "slug": f"mk-{c[-6:]}",
             "closedTime": iso(spec["closed_ts"]), "volumeNum": spec.get("volume", 200_000), "events": [event]}
        store.save_market(conn, m, spec.get("role", "kept"), spec.get("reason") or f"ai: {type_}", winner="Yes")
        status = spec.get("status", "ok")
        pct, cap = spec.get("pct"), spec.get("cap", 50_000)
        whale_capital = (cap / pct * 100) if pct else 100_000
        store.save_result(conn, {
            "condition_id": c, "cutoff_variant": VARIANT, "status": status, "market_id": m["id"], "event_id": eid,
            "closed_ts": spec["closed_ts"], "whale_count": 10, "whale_capital": whale_capital,
            "flagged_count": 3 if pct else 0, "flagged_capital": cap if pct else 0.0, "flagged_pct": pct,
            "jump_ts": None, "flagged_capital_before_jump": None, "whales": []})
    if refresh:
        closed_scan.refresh_events(conn, {eid: event}, VARIANT)
    return ids


def fresh():
    tr.clear_cache()
    return store.connect(":memory:")


# ── statistics ───────────────────────────────────────────────────────────────

def test_wilson_textbook_values():
    lo, hi = tr.wilson(5, 10)
    assert abs(lo - 0.2366) < 1e-4 and abs(hi - 0.7634) < 1e-4, (lo, hi)
    z2 = tr.Z95 ** 2
    lo, hi = tr.wilson(0, 10)
    assert lo == 0.0 and abs(hi - z2 / (10 + z2)) < 1e-12        # 0.2775
    lo, hi = tr.wilson(10, 10)
    assert hi == 1.0 and abs(lo - 10 / (10 + z2)) < 1e-12        # 0.7225
    assert tr.wilson(0, 0) == (0.0, 1.0)
    for k, n in ((1, 3), (2, 7), (13, 111), (40, 400)):
        lo, hi = tr.wilson(k, n)
        lo2, hi2 = tr.wilson(n - k, n)
        assert abs(lo - (1 - hi2)) < 1e-12 and abs(hi - (1 - lo2)) < 1e-12
        assert lo < k / n < hi
    # narrower with more data at the same rate
    assert (tr.wilson(40, 400)[1] - tr.wilson(40, 400)[0]) < (tr.wilson(4, 40)[1] - tr.wilson(4, 40)[0])


def test_chi2_tail():
    for x in (0.1, 1.0, 3.0, 5.991464547107979, 20.0):
        assert abs(tr.chi2_sf(x, 2) - math.exp(-x / 2)) < 1e-12
        assert abs(tr.chi2_sf(x, 1) - math.erfc(math.sqrt(x / 2))) < 1e-12
    assert abs(tr.chi2_sf(3.841458820694124, 1) - 0.05) < 1e-9
    assert abs(tr.chi2_sf(11.070497693516351, 5) - 0.05) < 1e-9
    assert abs(tr.chi2_sf(43.77297182574219, 30) - 0.05) < 1e-9
    assert tr.chi2_sf(0, 3) == 1.0


def test_chi2_independence_and_permutation():
    res = tr.chi2_independence([(10, 50), (20, 50)])
    assert abs(res["chi2"] - 50 * 100 ** 2 / (50 * 30 * 70)) < 1e-9        # 4.7619
    assert res["df"] == 1 and abs(res["p"] - math.erfc(math.sqrt(res["chi2"] / 2))) < 1e-12
    assert res["expected_min"] == 15 and res["cells_expected_lt5"] == 0
    assert tr.chi2_independence([(0, 10), (0, 20)]) is None                  # no heavy event
    assert tr.chi2_independence([(3, 10)]) is None                           # one group
    # same rate in both groups -> statistic 0, permutation p = 1
    labels = [1, 0, 0, 0, 0] * 4 + [1, 0, 0, 0, 0] * 4
    index = [0] * 20 + [1] * 20
    assert tr.permutation_p(labels, index, 2, n_perm=300) == 1.0
    # very different groups -> small p, close to the asymptotic one, deterministic per seed
    labels = [1] * 15 + [0] * 25 + [1] * 2 + [0] * 38
    index = [0] * 40 + [1] * 40
    p1 = tr.permutation_p(labels, index, 2, n_perm=2000, seed=1)
    assert p1 == tr.permutation_p(labels, index, 2, n_perm=2000, seed=1)
    assert p1 < 0.01
    assert tr.permutation_p([0, 0, 0], [0, 1, 1], 2) is None


# ── the heavy threshold ──────────────────────────────────────────────────────

def test_heavy_threshold():
    assert tr.HEAVY_PCT == 10.0
    assert tr.is_heavy(10.0) and tr.is_heavy(100) and not tr.is_heavy(9.99) and not tr.is_heavy(None)
    conn = fresh()
    add_event(conn, "A", [dict(closed_ts=NOV1, pct=10.0)])
    add_event(conn, "B", [dict(closed_ts=NOV1 + 60, pct=9.99)])
    add_event(conn, "C", [dict(closed_ts=NOV1 + 120, pct=100.0, cap=500)])     # tiny but all flagged
    add_event(conn, "D", [dict(closed_ts=NOV1 + 180, pct=None, cap=0)])        # no whale flagged
    r = tr.rate_for(conn, "election_vote", None)
    assert (r["basis"], r["k"], r["n"]) == ("type", 2, 4) and r["rate"] == 0.5
    assert r["k_big"] == 1                                                      # only A has >= $10k flagged
    ex = {e["title"]: e for e in r["examples"]}
    assert set(ex) == {"Event A?", "Event C?"}
    assert ex["Event C?"]["small"] is True and ex["Event C?"]["flagged_capital"] == 500
    assert ex["Event A?"]["small"] is False
    assert r["examples"][0]["title"] == "Event C?"                              # most recent first
    lo, hi = tr.wilson(2, 4)
    assert r["low"] == round(lo, 4) and r["high"] == round(hi, 4)


# ── time ─────────────────────────────────────────────────────────────────────

def test_as_of_strictly_before_and_no_leakage():
    conn = fresh()
    for i in range(4):          # 4 quiet events in the first days
        add_event(conn, f"Q{i}", [dict(closed_ts=NOV1 + i * DAY, pct=1.0)], series="ser")
    add_event(conn, "H1", [dict(closed_ts=NOV1 + 5 * DAY, pct=60.0)], series="ser", title="Later heavy")
    add_event(conn, "H2", [dict(closed_ts=NOV1 + 6 * DAY, pct=70.0)], series="ser", title="Latest heavy")
    cut = NOV1 + 5 * DAY        # H1 closes exactly at the cut: not "strictly before"
    r = tr.rate_for(conn, "election_vote", "ser", as_of_ts=cut)
    assert (r["basis"], r["k"], r["n"], r["examples"]) == ("series", 0, 4, [])
    r = tr.rate_for(conn, "election_vote", "ser", as_of_ts=cut + 1)
    assert (r["k"], r["n"]) == (1, 5) and [e["title"] for e in r["examples"]] == ["Later heavy"]
    r = tr.rate_for(conn, "election_vote", "ser")
    assert (r["k"], r["n"]) == (2, 6) and [e["title"] for e in r["examples"]] == ["Latest heavy", "Later heavy"]
    # the table respects as_of too
    evs = tr.load_events(conn, as_of_ts=cut)
    assert [e["event_id"] for e in evs] == ["Q0", "Q1", "Q2", "Q3"]
    assert all(e["closed_ts"] < cut for e in evs)


def test_ladder_figure_rechosen_as_of():
    """A date ladder: the early rung was quiet, the later rung heavy. The
    stored main is the later rung (highest flagged capital, chosen with
    hindsight). As of a moment between the two closes, the event counts on
    the early rung, not dropped and not heavy."""
    conn = fresh()
    early, late = add_event(conn, "L", [dict(closed_ts=NOV1, pct=2.0, cap=2_000),
                                        dict(closed_ts=NOV1 + 7 * DAY, pct=80.0, cap=90_000)],
                            type_="military_gov_action", series="ceasefire-ladder")
    assert store.get_event(conn, "L")["main_condition_id"] == late
    mid = NOV1 + 3 * DAY
    (e,) = tr.load_events(conn, as_of_ts=mid)
    assert e["condition_id"] == early and not e["heavy"] and e["type"] == "military_gov_action"
    r = tr.rate_for(conn, "military_gov_action", "ceasefire-ladder", as_of_ts=mid)
    assert (r["basis"], r["k"], r["n"]) == ("type", 0, 1)
    # filtering the hindsight main row by date would have lost the event entirely
    assert [x for x in store.closed_results(conn, VARIANT, until_ts=mid) if x["event_id"] == "L"] == []
    (e,) = tr.load_events(conn)
    assert e["condition_id"] == late and e["heavy"]


def test_as_of_after_everything_equals_main_rows():
    conn = fresh()
    add_event(conn, "A", [dict(closed_ts=NOV1, pct=5.0, cap=5_000), dict(closed_ts=NOV1 + DAY, pct=30.0)])
    add_event(conn, "B", [dict(closed_ts=NOV1 + 2 * DAY, pct=12.0)], type_="court_regulator")
    add_event(conn, "C", [dict(closed_ts=NOV1 + 3 * DAY, pct=None, cap=0), dict(closed_ts=NOV1 + 3 * DAY + 5,
                                                                               pct=None, status="too_large")])
    main = tr.load_events(conn)
    later = tr.load_events(conn, as_of_ts=NOV1 + 100 * DAY)
    assert main == later and len(main) == 3


# ── fallback ─────────────────────────────────────────────────────────────────

def test_series_fallback_to_type():
    conn = fresh()
    add_event(conn, "S1", [dict(closed_ts=NOV1, pct=50.0)], type_="person_behavior", series="tweets")
    add_event(conn, "S2", [dict(closed_ts=NOV1 + DAY, pct=50.0)], type_="person_behavior", series="tweets")
    for i in range(5):
        add_event(conn, f"T{i}", [dict(closed_ts=NOV1 + i * 3600, pct=0.5)], type_="person_behavior")
    r = tr.rate_for(conn, "person_behavior", "tweets")
    assert (r["basis"], r["key"], r["k"], r["n"], r["series_n"]) == ("type", "person_behavior", 2, 7, 2)
    assert tr.rate_for(conn, "person_behavior", "tweets", min_series_n=2)["basis"] == "series"
    add_event(conn, "S3", [dict(closed_ts=NOV1 + 2 * DAY, pct=1.0)], type_="person_behavior", series="tweets")
    r = tr.rate_for(conn, "person_behavior", "tweets")
    assert (r["basis"], r["key"], r["k"], r["n"]) == ("series", "tweets", 2, 3)
    # as of before S3 closed the series is back to n = 2 -> type
    r = tr.rate_for(conn, "person_behavior", "tweets", as_of_ts=NOV1 + 2 * DAY)
    assert (r["basis"], r["n"], r["series_n"]) == ("type", 7, 2)
    # no series key / an unseen series -> type; unknown type and no series -> none
    assert tr.rate_for(conn, "person_behavior", None)["basis"] == "type"
    assert tr.rate_for(conn, "person_behavior", "never-seen")["basis"] == "type"
    r = tr.rate_for(conn, "data_release", "never-seen")
    assert (r["basis"], r["n"], r["rate"], r["low"], r["high"]) == ("none", 0, None, 0.0, 1.0)
    assert tr.rate_for(conn, None, None)["basis"] == "none"
    # a known series still answers when the type is unknown
    assert tr.rate_for(conn, None, "tweets")["basis"] == "series"
    # title-derived series keys work the same (filters.series_key of the event)
    add_event(conn, "N1", [dict(closed_ts=NOV1, pct=20.0)], title="Who wins NC-01 on November 3?")
    assert store.get_event(conn, "N1")["series_key"].startswith("title:")


# ── what is counted ──────────────────────────────────────────────────────────

def test_only_ok_kept_and_allowed_types():
    conn = fresh()
    add_event(conn, "OK", [dict(closed_ts=NOV1, pct=20.0)])
    add_event(conn, "BIG", [dict(closed_ts=NOV1, pct=None, status="too_large")])
    add_event(conn, "NOTR", [dict(closed_ts=NOV1, pct=None, status="no_trades")])
    add_event(conn, "GAME", [dict(closed_ts=NOV1, pct=40.0)], type_="game_sport")
    (c,) = add_event(conn, "GONE", [dict(closed_ts=NOV1, pct=40.0)])
    with store.transaction(conn):       # relabelled after the event row was written
        conn.execute("UPDATE markets SET role='excluded', reason='ai: price_level' WHERE condition_id=?", (c,))
    for asof in (None, NOV1 + DAY):
        assert [e["event_id"] for e in tr.load_events(conn, asof)] == ["OK"], asof
    # an event the scan has not written a row for yet is not counted
    add_event(conn, "NOROW", [dict(closed_ts=NOV1, pct=40.0)], refresh=False)
    assert [e["event_id"] for e in tr.load_events(conn, NOV1 + DAY)] == ["OK"]


def test_cache_sees_new_writes():
    conn = fresh()
    add_event(conn, "A", [dict(closed_ts=NOV1, pct=1.0)])
    assert tr.rate_for(conn, "election_vote", None)["n"] == 1
    assert tr.snapshot(conn) is tr.snapshot(conn)                    # cached while nothing changes
    add_event(conn, "B", [dict(closed_ts=NOV1 + 1, pct=90.0)])
    r = tr.rate_for(conn, "election_vote", None)
    assert (r["n"], r["k"]) == (2, 1)
    other = fresh()                                                    # another store is never served from it
    assert tr.rate_for(other, "election_vote", None)["n"] == 0


# ── the table ────────────────────────────────────────────────────────────────

def _bulk(conn, type_, n, k, t0):
    for i in range(n):
        add_event(conn, f"{type_}-{i}", [dict(closed_ts=t0 + i * 60, pct=50.0 if i < k else 1.0)], type_=type_)


def test_table_and_type_test():
    conn = fresh()
    _bulk(conn, "court_regulator", 40, 30, NOV1)               # 75 %
    _bulk(conn, "election_vote", 60, 3, NOV1 + DAY)              # 5 %
    _bulk(conn, "company_product", 4, 2, NOV1 + 2 * DAY)        # too few for the test
    add_event(conn, "U", [dict(closed_ts=NOV1 + 3 * DAY, pct=50.0, reason="ai: unlabelled")])
    evs = tr.load_events(conn)
    assert len(evs) == 105 and [e for e in evs if e["event_id"] == "U"][0]["type"] is None
    t = tr.table(evs, n_perm=500)
    o = t["overall"]
    assert (o["n"], o["k"]) == (105, 36)
    rows = {r["type"]: r for r in t["by_type"]}
    assert set(rows) == {"court_regulator", "election_vote", "company_product", tr.UNLABELLED}
    assert rows["court_regulator"]["vs_overall"] == "above" and rows["election_vote"]["vs_overall"] == "below"
    assert rows["company_product"]["vs_overall"] == "overlaps"
    assert {x["type"] for x in t["types_off_overall"]} == {"court_regulator", "election_vote"}
    tt = t["type_test"]
    assert tt["types_tested"] == ["election_vote", "court_regulator"]
    assert tt["types_left_out"] == ["company_product"] and tt["events_tested"] == 100
    assert tt["result"]["df"] == 1 and tt["result"]["p"] < 1e-6 and tt["result"]["p_perm"] < 0.01
    assert tt["note"].startswith("the types differ")
    sens = {s["threshold_pct"]: s["k"] for s in t["threshold_sensitivity"]}
    assert sens[10.0] == 36 and sens[50.0] == 36 and sens[5.0] == 36
    # one testable type -> no test, said so
    t1 = tr.table([e for e in evs if e["type"] == "election_vote"], n_perm=0)
    assert t1["type_test"]["result"] is None and "needs >= 2 types" in t1["type_test"]["note"]


def test_table_series_rows():
    conn = fresh()
    for i in range(3):
        add_event(conn, f"E{i}", [dict(closed_ts=NOV1 + i * DAY, pct=30.0 if i == 0 else 2.0)],
                  type_="person_behavior", series="elon-tweets")
    add_event(conn, "P1", [dict(closed_ts=NOV1, pct=2.0)], series="pair")
    add_event(conn, "P2", [dict(closed_ts=NOV1 + 1, pct=2.0)], series="pair")
    t = tr.table(tr.load_events(conn), n_perm=0)
    (row,) = t["by_series"]
    assert (row["series_key"], row["n"], row["k"], row["types"]) == ("elon-tweets", 3, 1, {"person_behavior": 3})
    sc = t["series_counts"]
    assert (sc["series"], sc["usable"], sc["events_in_usable_series"], sc["series_with_2"]) == (2, 1, 3, 1)


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_writes_json_read_only():
    tmp = tempfile.mkdtemp(prefix="wl_track_")
    try:
        db = os.path.join(tmp, "store.db")
        conn = store.connect(db)
        add_event(conn, "A", [dict(closed_ts=NOV1, pct=25.0)], series="s")
        add_event(conn, "B", [dict(closed_ts=NOV1 + 2 * DAY, pct=3.0)], series="s")
        conn.close()
        before = os.path.getmtime(db), os.path.getsize(db)
        out = os.path.join(tmp, "demo", "track_record.json")
        tr.clear_cache()
        rep = tr.main(["--db", db, "--out", out, "--as-of", "2026-11-02", "--perm", "100"])
        with open(out, encoding="utf-8") as f:
            got = json.load(f)
        assert got["n_events"] == 1 == rep["n_events"] and got["as_of"].startswith("2026-11-02")
        assert got["heavy_events"][0]["title"] == "Event A?" and got["heavy_pct"] == 10.0
        assert got["overall"]["k"] == 1 and got["by_type"][0]["type"] == "election_vote"
        assert (os.path.getmtime(db), os.path.getsize(db)) == before
        ro = tr.open_db(db)
        try:
            ro.execute("DELETE FROM results")
            raise AssertionError("open_db must be read-only")
        except Exception as e:  # sqlite3.OperationalError: attempt to write a readonly database
            assert "readonly" in str(e).lower(), e
        ro.close()
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
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)
