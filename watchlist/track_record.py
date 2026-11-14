"""
The track record: how often did ENDED markets like this one carry heavy
flagged money? It is the open list's main ranking criterion (an open event
is ranked by the rate of its series, else of its type) and the backtest's
yardstick, so it has to be honest about two things: how much evidence a
rate stands on, and time.

Source. The closed scan's event rows: store.closed_results with
main_only, i.e. one row per Gamma event, standing on its figure market
(the kept market with the highest flagged capital, the number the closed
list shows), with that market's AI type and the event's series_key
(Gamma seriesSlug, else the title with dates and numbers stripped). Only
status "ok" counts: too_large / no_trades / unresolved markets say
nothing about flagged money. Events whose type is an excluded one (game,
price, weather: a label that changed after the event row was written) and
events whose figure market is now recorded with another role than "kept"
are dropped.

Heavy. An event is heavy when its flagged_pct (flagged capital / whale
capital) is >= 10 %. flagged_capital is kept beside it, and every count
also has k_big (heavy AND >= $10,000 flagged, the closed list's floor),
so a $500 market at 100 % shows up as heavy but small.

Rates. k heavy of n events, with the Wilson 95 % interval (sensible at
k = 0, k = n and small n, unlike the normal approximation): a 2/3 series
is "between 21 % and 94 %", not "67 %". rate_for(conn, type, series_key)
uses the series once it has >= 3 ended events, else the type, else
nothing (basis "none", rate None, interval [0, 1]).

Time. rate_for(..., as_of_ts=T) only sees events whose figure market
closed strictly before T, and the figure itself is re-chosen among the
event's markets closed before T. The stored main_condition_id was chosen
with hindsight over ALL the event's markets: in a date ladder ("ceasefire
continues through Oct 31 / Nov 7 / ...") the figure may be a rung that
closed after T, and filtering the main rows by date would then drop the
whole event, or count it on a number nobody had at T. With as_of_ts=None
the stored main rows are used as they are (the same numbers as the closed
list); with T after every close the two paths agree (tested).

Do the types really differ? table() adds a chi-square test of
independence (heavy / not heavy x type) over the types with >= 10
events, its p-value from the chi-square distribution and, because small
expected counts make that approximation shaky, a permutation p-value
(heavy labels shuffled across the same events, fixed seed). Each type is
also marked "above" / "below" when its Wilson interval excludes the
overall rate, "overlaps" otherwise.

Repeated look-ups are cheap: snapshot(conn, as_of_ts) loads the events
once and is cached per (connection, variant, as_of_ts) until the database
changes (sqlite's data_version + the connection's total_changes), so the
open list and the backtest can call rate_for per event.

CLI (from the repo root; reads the store read-only, writes one JSON):
  python3 -m watchlist.track_record                                   # -> data/watchlist/demo/track_record.json
  python3 -m watchlist.track_record --db data/watchlist/dev_snapshot.db --as-of 2026-11-05 --out /tmp/tr.json
"""
import json
import math
import os
import sqlite3
import threading
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

import numpy as np

from watchlist import ai, closed_scan, method, store
from watchlist.common import DB_PATH, DEMO_DIR, ROOT, day_str, iso, log, now_epoch, to_epoch

HEAVY_PCT = 10.0             # flagged_pct at or above this makes an event heavy
BIG_CAPITAL = closed_scan.MIN_FLAGGED_CAPITAL   # $10,000: heavy AND this much flagged = k_big
MIN_SERIES_N = 3             # a series' own rate is used once it has this many ended events
MIN_TYPE_N = 10              # types with fewer events stay out of the chi-square test
Z95 = 1.959963984540054      # two-sided 95 % normal quantile
N_PERM = 5000                # permutations for the type test's permutation p-value
SEED = 416
EXAMPLES = 3                 # recent heavy events shown with a rate
UNLABELLED = "(unlabelled)"  # by_type key for events without an AI type
SENSITIVITY = (5.0, 10.0, 20.0, 30.0, 50.0)
OUT_PATH = os.path.join(DEMO_DIR, "track_record.json")
CACHE_SIZE = 128


# ── statistics (pure) ────────────────────────────────────────────────────────

def is_heavy(flagged_pct, threshold=HEAVY_PCT):
    """True when flagged_pct (in percent) is at or above the threshold."""
    return flagged_pct is not None and float(flagged_pct) >= float(threshold)


def wilson(k, n, z=Z95):
    """(low, high) Wilson score interval for k successes in n trials; (0, 1)
    when n == 0."""
    k, n = int(k), int(n)
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    z2 = z * z
    den = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den
    low, high = max(0.0, centre - half), min(1.0, centre + half)
    if k == 0:
        low = 0.0
    if k == n:
        high = 1.0
    return low, high


def _gamma_q(a, x):
    """Regularised upper incomplete gamma Q(a, x) (series below a + 1,
    Lentz continued fraction above), to ~1e-14."""
    if x <= 0:
        return 1.0
    log_front = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(10_000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return max(0.0, 1.0 - total * math.exp(log_front))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 10_000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-16:
            break
    return min(1.0, math.exp(log_front) * h)


def chi2_sf(x, df):
    """P(X >= x) for X ~ chi-square(df)."""
    if df <= 0:
        raise ValueError("df must be positive")
    return _gamma_q(df / 2.0, max(0.0, float(x)) / 2.0)


def _chi2_stat(ks, ns):
    """Pearson chi-square of a 2 x G table given heavy counts ks and group
    sizes ns (numpy arrays; ks may be 2-D, one row per permutation)."""
    n_tot = ns.sum()
    k_tot = ks.sum(axis=-1, keepdims=True) if ks.ndim > 1 else ks.sum()
    e = ns * k_tot / n_tot
    return (((ks - e) ** 2) * (n_tot ** 2 / (ns * k_tot * (n_tot - k_tot)))).sum(axis=-1)


def chi2_independence(groups):
    """Chi-square test of independence for heavy / not heavy x group.
    groups: [(k, n)] per group (n > 0). Returns {chi2, df, p, n, k,
    expected_min, cells_expected_lt5, cells, cramers_v}, or None when the
    test is undefined (fewer than 2 groups, or no / only heavy events)."""
    ks = np.array([float(k) for k, _ in groups])
    ns = np.array([float(n) for _, n in groups])
    if len(groups) < 2 or (ns <= 0).any():
        return None
    n_tot, k_tot = ns.sum(), ks.sum()
    if k_tot <= 0 or k_tot >= n_tot:
        return None
    stat = float(_chi2_stat(ks, ns))
    df = len(groups) - 1
    expected = np.concatenate([ns * k_tot / n_tot, ns * (n_tot - k_tot) / n_tot])
    return {"chi2": stat, "df": df, "p": chi2_sf(stat, df), "n": int(n_tot), "k": int(k_tot),
            "expected_min": float(expected.min()), "cells_expected_lt5": int((expected < 5).sum()),
            "cells": int(expected.size), "cramers_v": math.sqrt(stat / n_tot)}


def permutation_p(labels, group_index, n_groups, n_perm=N_PERM, seed=SEED, chunk=500):
    """Permutation p-value of the chi-square statistic: the heavy labels
    (0/1 per event) shuffled across the same events n_perm times, p = (1 +
    #{stat >= observed}) / (1 + n_perm). None when the statistic is
    undefined or n_perm < 1."""
    y = np.asarray(labels, dtype=np.float64)
    g = np.asarray(group_index, dtype=np.int64)
    if n_perm < 1 or y.size == 0 or y.sum() in (0, y.size):
        return None
    ns = np.bincount(g, minlength=n_groups).astype(np.float64)
    if (ns <= 0).any() or n_groups < 2:
        return None
    observed = float(_chi2_stat(np.bincount(g, weights=y, minlength=n_groups), ns))
    onehot = np.zeros((y.size, n_groups))
    onehot[np.arange(y.size), g] = 1.0
    rng = np.random.default_rng(seed)
    hits, done = 0, 0
    while done < n_perm:
        c = min(chunk, n_perm - done)
        ys = rng.permuted(np.tile(y, (c, 1)), axis=1)
        stats = _chi2_stat(ys @ onehot, ns)
        hits += int((stats >= observed - 1e-9).sum())
        done += c
    return (1 + hits) / (1 + n_perm)


# ── events from the store ────────────────────────────────────────────────────

def open_db(path=DB_PATH):
    """A READ-ONLY connection to a store file (the track record never
    writes; a backfill may be writing to the same file)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"no store at {path}")
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False,
                           timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _market_rows(conn):
    """{condition_id: {role, reason, volume}} for every recorded market."""
    with store.LOCK:
        rows = conn.execute("SELECT condition_id, role, reason, volume FROM markets").fetchall()
    return {r["condition_id"]: {"role": r["role"], "reason": r["reason"], "volume": r["volume"]} for r in rows}


def _event(row, type_):
    pct = row.get("flagged_pct")
    cap = row.get("flagged_capital") or 0.0
    return {"event_id": row.get("event_id"), "title": row.get("event_title"), "slug": row.get("event_slug"),
            "series_key": row.get("series_key"), "type": type_, "condition_id": row.get("condition_id"),
            "closed_ts": row.get("closed_ts"), "whale_count": row.get("whale_count"),
            "whale_capital": row.get("whale_capital"), "flagged_count": row.get("flagged_count"),
            "flagged_capital": cap, "flagged_pct": pct, "heavy": is_heavy(pct), "big": cap >= BIG_CAPITAL}


def load_events(conn, as_of_ts=None, variant=method.DEFAULT_VARIANT):
    """The track record's events, one dict per Gamma event, oldest close
    first: event_id, title, slug, series_key, type (None = unlabelled),
    condition_id (the figure market), closed_ts, whale/flagged numbers,
    heavy, big.

    as_of_ts=None: the store's main rows (closed_results main_only, status
    ok). as_of_ts=T: the figure re-chosen per event (closed_scan's rule)
    among its kept markets with an ok result closed strictly before T."""
    markets = _market_rows(conn)
    if as_of_ts is None:
        rows = store.closed_results(conn, variant, main_only=True, status="ok")
        picked = []
        for r in rows:
            m = markets.get(r["condition_id"])
            if m and m["role"] != "kept":
                continue
            picked.append((r, r.get("type")))
    else:
        rows = store.closed_results(conn, variant, until_ts=int(as_of_ts), main_only=False, status="ok")
        by_event = defaultdict(list)
        for r in rows:
            m = markets.get(r["condition_id"])
            if not r.get("main_condition_id") or r.get("closed_ts") is None or (m and m["role"] != "kept"):
                continue          # no event row (yet), no close time, or no longer a kept market
            by_event[r["event_id"]].append(dict(r, volume=(m or {}).get("volume") or 0.0,
                                                reason=(m or {}).get("reason")))
        picked = []
        for eid, rs in by_event.items():
            best = closed_scan.choose_figure(rs)
            picked.append((best, closed_scan.ai_type(best.get("reason")) or best.get("type")))
    out = [_event(r, t) for r, t in picked if t not in ai.EXCLUDED_TYPES]
    out.sort(key=lambda e: (e["closed_ts"] or 0, str(e["event_id"])))
    return out


# ── rates ────────────────────────────────────────────────────────────────────

def _r4(x):
    return None if x is None else round(float(x), 4)


def example(e):
    """The card line for one heavy event."""
    return {"event_id": e["event_id"], "title": e["title"], "flagged_pct": e["flagged_pct"],
            "flagged_capital": round(e["flagged_capital"] or 0.0, 2), "small": not e["big"],
            "closed": day_str(e["closed_ts"], is_day=False) if e["closed_ts"] is not None else None}


def summarize(events, examples=EXAMPLES):
    """{n, k, rate, low, high, k_big, flagged_capital, examples} of a list
    of events (examples: the most recent heavy ones)."""
    n = len(events)
    heavy = [e for e in events if e["heavy"]]
    k = len(heavy)
    low, high = wilson(k, n)
    recent = sorted(heavy, key=lambda e: (-(e["closed_ts"] or 0), str(e["event_id"])))[:examples]
    return {"n": n, "k": k, "rate": _r4(k / n) if n else None, "low": _r4(low), "high": _r4(high),
            "k_big": sum(1 for e in heavy if e["big"]),
            "flagged_capital": round(sum(e["flagged_capital"] or 0.0 for e in events), 2),
            "examples": [example(e) for e in recent]}


class Snapshot:
    """The track record's events as of one moment, indexed by type and
    series; .rate() answers rate_for without touching the database."""

    def __init__(self, events, as_of_ts=None, variant=method.DEFAULT_VARIANT):
        self.events = list(events)
        self.as_of_ts = as_of_ts
        self.variant = variant
        self.by_type = defaultdict(list)
        self.by_series = defaultdict(list)
        for e in self.events:
            if e["type"]:
                self.by_type[e["type"]].append(e)
            if e["series_key"]:
                self.by_series[e["series_key"]].append(e)

    def rate(self, event_type, series_key, min_series_n=MIN_SERIES_N):
        """{basis, key, k, n, rate, low, high, examples, series_n}: the
        series once it has >= min_series_n events, else the type, else
        basis "none" (n 0, rate None, interval [0, 1])."""
        series = self.by_series.get(series_key) if series_key else None
        series_n = len(series) if series else 0
        if series and series_n >= min_series_n:
            basis, key, group = "series", series_key, series
        elif event_type and self.by_type.get(event_type):
            basis, key, group = "type", event_type, self.by_type[event_type]
        else:
            basis, key, group = "none", None, []
        s = summarize(group)
        return {"basis": basis, "key": key, "k": s["k"], "n": s["n"], "rate": s["rate"], "low": s["low"],
                "high": s["high"], "k_big": s["k_big"], "examples": s["examples"], "series_n": series_n}


_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()


def _version(conn):
    return (conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes)


def snapshot(conn, as_of_ts=None, variant=method.DEFAULT_VARIANT):
    """Snapshot of the store as of as_of_ts (None = everything), cached
    until the database changes. The cache holds the connection itself, so
    an id is never reused by another connection while its entry lives."""
    key = (id(conn), variant, None if as_of_ts is None else int(as_of_ts))
    with _CACHE_LOCK:
        with store.LOCK:
            ver = _version(conn)
        hit = _CACHE.get(key)
        if hit and hit[0] is conn and hit[1] == ver:
            _CACHE.move_to_end(key)
            return hit[2]
        snap = Snapshot(load_events(conn, as_of_ts, variant), as_of_ts, variant)
        _CACHE[key] = (conn, ver, snap)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
        return snap


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def rate_for(conn, event_type, series_key, as_of_ts=None, min_series_n=MIN_SERIES_N,
             variant=method.DEFAULT_VARIANT):
    """How often ended events like this one were heavy (flagged_pct >= 10 %).

    Only events whose figure market closed strictly before as_of_ts count
    (None = all). The series (filters.series_key) is the basis once it has
    >= min_series_n such events, else the AI type. Returns {"basis":
    "series" | "type" | "none", "key", "k", "n", "rate" (None when n = 0),
    "low", "high" (Wilson 95 %), "k_big" (heavy with >= $10k flagged),
    "examples": up to 3 recent heavy events {title, flagged_pct,
    flagged_capital, small, closed (YYYY-MM-DD), event_id}, "series_n"}."""
    return snapshot(conn, as_of_ts, variant).rate(event_type, series_key, min_series_n)


# ── the table ────────────────────────────────────────────────────────────────

def _row(key_name, key, events, overall_rate=None):
    s = summarize(events)
    row = {key_name: key, **s}
    if overall_rate is not None and s["n"]:
        row["vs_overall"] = ("above" if s["low"] > overall_rate else "below" if s["high"] < overall_rate
                             else "overlaps")
    return row


def type_test(events, min_type_n=MIN_TYPE_N, n_perm=N_PERM, seed=SEED):
    """Do the types differ? The chi-square test of independence (heavy x
    type) over labelled types with >= min_type_n events, with its
    asymptotic and permutation p-values."""
    by_type = defaultdict(list)
    for e in events:
        if e["type"]:
            by_type[e["type"]].append(e)
    tested = sorted((t for t, es in by_type.items() if len(es) >= min_type_n), key=lambda t: (-len(by_type[t]), t))
    left_out = sorted(t for t in by_type if t not in tested)
    out = {"min_type_n": min_type_n, "types_tested": tested, "types_left_out": left_out,
           "events_tested": sum(len(by_type[t]) for t in tested)}
    if len(tested) < 2:
        out.update(result=None, note=f"needs >= 2 types with >= {min_type_n} events; have {len(tested)}")
        return out
    res = chi2_independence([(sum(e["heavy"] for e in by_type[t]), len(by_type[t])) for t in tested])
    if res is None:
        out.update(result=None, note="undefined: no heavy event (or only heavy events) among the tested types")
        return out
    labels = [int(e["heavy"]) for t in tested for e in by_type[t]]
    index = [i for i, t in enumerate(tested) for _ in by_type[t]]
    p_perm = permutation_p(labels, index, len(tested), n_perm=n_perm, seed=seed)
    res = {**{k: (_r4(v) if isinstance(v, float) else v) for k, v in res.items()},
           "p": float(f"{res['p']:.4g}"), "p_perm": None if p_perm is None else float(f"{p_perm:.4g}"),
           "n_perm": n_perm, "seed": seed}
    small = res["cells_expected_lt5"]
    verdict = ("the types differ (p < 0.05)" if (p_perm if p_perm is not None else res["p"]) < 0.05
               else "no evidence the types differ (p >= 0.05)")
    note = verdict + ("; " + f"{small} of {res['cells']} cells expect < 5 events, so read p_perm, not p"
                      if small else "")
    out.update(result=res, note=note)
    return out


def table(events, min_series_n=MIN_SERIES_N, min_type_n=MIN_TYPE_N, n_perm=N_PERM, seed=SEED):
    """Rates overall, by type and by series (series with >= min_series_n
    events), the type test, which types sit off the overall rate, and how
    the heavy count moves with the threshold."""
    overall = summarize(events)
    p0 = overall["rate"]
    by_type = defaultdict(list)
    by_series = defaultdict(list)
    for e in events:
        by_type[e["type"] or UNLABELLED].append(e)
        if e["series_key"]:
            by_series[e["series_key"]].append(e)
    types = [_row("type", t, es, p0) for t, es in by_type.items()]
    types.sort(key=lambda r: (-r["n"], -(r["rate"] or 0), r["type"]))
    series = []
    for sk, es in by_series.items():
        if len(es) < min_series_n:
            continue
        r = _row("series_key", sk, es, p0)
        r["types"] = dict(Counter(e["type"] or UNLABELLED for e in es).most_common())
        series.append(r)
    series.sort(key=lambda r: (-r["n"], -(r["rate"] or 0), r["series_key"]))
    off = [{"type": r["type"], "vs_overall": r["vs_overall"], "n": r["n"], "rate": r["rate"], "low": r["low"],
            "high": r["high"]} for r in types if r.get("vs_overall") in ("above", "below") and r["type"] != UNLABELLED]
    n_series_events = sum(len(es) for es in by_series.values())
    sens = []
    for th in SENSITIVITY:
        k = sum(1 for e in events if is_heavy(e["flagged_pct"], th))
        sens.append({"threshold_pct": th, "k": k, "rate": _r4(k / len(events)) if events else None,
                     "k_big": sum(1 for e in events if is_heavy(e["flagged_pct"], th) and e["big"])})
    return {
        "overall": overall,
        "by_type": types,
        "type_test": type_test(events, min_type_n, n_perm, seed),
        "types_off_overall": off,
        "by_series": series,
        "series_counts": {"series": len(by_series), "usable": len(series),
                          "events_in_usable_series": sum(r["n"] for r in series),
                          "events_with_series": n_series_events,
                          "events_without_series": len(events) - n_series_events,
                          "series_with_1": sum(1 for es in by_series.values() if len(es) == 1),
                          "series_with_2": sum(1 for es in by_series.values() if len(es) == 2)},
        "threshold_sensitivity": sens,
    }


def _coverage(conn, variant):
    """What the store holds for this variant (to read the table against)."""
    with store.LOCK:
        statuses = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM results WHERE cutoff_variant=? GROUP BY status", (variant,)).fetchall()}
        ev = dict(conn.execute("SELECT COUNT(*) n, SUM(type IS NULL) untyped, MIN(closed_ts) first, "
                               "MAX(closed_ts) last FROM events").fetchone())
        kept = conn.execute("SELECT COUNT(*) FROM markets WHERE role='kept'").fetchone()[0]
    return {"results_by_status": statuses, "kept_markets": kept, "event_rows": ev["n"],
            "event_rows_untyped": ev["untyped"] or 0,
            "first_close": day_str(ev["first"], is_day=False) if ev["first"] else None,
            "last_close": day_str(ev["last"], is_day=False) if ev["last"] else None}


def build_report(conn, as_of_ts=None, variant=method.DEFAULT_VARIANT, min_series_n=MIN_SERIES_N,
                 min_type_n=MIN_TYPE_N, n_perm=N_PERM, seed=SEED, db_path=None):
    """The track_record.json document (reads the store only)."""
    events = load_events(conn, as_of_ts, variant)
    heavy = sorted((e for e in events if e["heavy"]), key=lambda e: (-(e["closed_ts"] or 0), str(e["event_id"])))
    rep = {
        "generated_at": iso(now_epoch()),
        "db": _rel(db_path) if db_path else None,
        "cutoff_variant": variant,
        "as_of": iso(as_of_ts) if as_of_ts is not None else None,
        "heavy_pct": HEAVY_PCT,
        "big_capital": BIG_CAPITAL,
        "min_series_n": min_series_n,
        "rules": (f"one row per Gamma event (its figure market: highest flagged capital among its kept markets), "
                  f"status ok only; heavy = flagged_pct >= {HEAVY_PCT:g}%; k_big = heavy with >= "
                  f"${BIG_CAPITAL:,.0f} flagged; intervals are Wilson 95%; a series' own rate is used once it has "
                  f">= {min_series_n} ended events, else the type's"
                  + ("; only events closed strictly before as_of, figure re-chosen among markets closed by then"
                     if as_of_ts is not None else "")),
        "store": _coverage(conn, variant),
        "n_events": len(events),
        "first_close": day_str(events[0]["closed_ts"], is_day=False) if events else None,
        "last_close": day_str(events[-1]["closed_ts"], is_day=False) if events else None,
        **table(events, min_series_n, min_type_n, n_perm, seed),
        "heavy_events": [{**example(e), "type": e["type"], "series_key": e["series_key"],
                          "whale_capital": round(e["whale_capital"] or 0.0, 2), "condition_id": e["condition_id"]}
                         for e in heavy],
    }
    return rep


def write_report(report, path=OUT_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    os.replace(tmp, path)
    return path


# ── CLI ──────────────────────────────────────────────────────────────────────

def _rel(path):
    """path relative to the repo root when it is inside it, else absolute."""
    full = os.path.abspath(path)
    return os.path.relpath(full, ROOT) if full.startswith(ROOT + os.sep) else full


def _pct(x):
    return "  -  " if x is None else f"{100 * x:4.0f}%"


def print_table(rep, series_shown=15):
    o = rep["overall"]
    log(f"overall: {o['k']}/{o['n']} heavy = {_pct(o['rate']).strip()} [{_pct(o['low']).strip()}, "
        f"{_pct(o['high']).strip()}], k_big {o['k_big']}")
    log(f"  {'type':<22}{'n':>5}{'k':>5}{'rate':>7}  {'95% interval':<14}{'k_big':>6}  vs overall")
    for r in rep["by_type"]:
        log(f"  {r['type']:<22}{r['n']:>5}{r['k']:>5}{_pct(r['rate']):>7}  [{_pct(r['low'])},{_pct(r['high'])}]"
            f"{r['k_big']:>6}  {r.get('vs_overall', '')}")
    tt = rep["type_test"]
    res = tt.get("result")
    if res:
        log(f"type test over {len(tt['types_tested'])} types (n >= {tt['min_type_n']}, {tt['events_tested']} events): "
            f"chi2 {res['chi2']} df {res['df']} p {res['p']} p_perm {res['p_perm']} Cramer's V {res['cramers_v']}")
    log(f"type test: {tt['note']}")
    sc = rep["series_counts"]
    log(f"series: {sc['series']} keys, {sc['usable']} with n >= {rep['min_series_n']} "
        f"({sc['events_in_usable_series']} events); {sc['series_with_1']} singletons, {sc['series_with_2']} pairs")
    for r in rep["by_series"][:series_shown]:
        log(f"  {str(r['series_key'])[:50]:<52}{r['n']:>4}{r['k']:>4}{_pct(r['rate']):>7}  "
            f"[{_pct(r['low'])},{_pct(r['high'])}]")
    log("threshold sensitivity: " + ", ".join(f">={s['threshold_pct']:g}%: {s['k']}" for s in
                                             rep["threshold_sensitivity"]))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Track record: per-type and per-series rates of heavy flagged money "
                                             "(flagged_pct >= 10%) among ended events in the store. Read-only.")
    ap.add_argument("--db", default=DB_PATH, help="the store to read (default the real one; opened read-only)")
    ap.add_argument("--as-of", default=None, help="only events closed strictly before this UTC date / ISO time")
    ap.add_argument("--out", default=OUT_PATH, help="where to write the JSON (default the demo file)")
    ap.add_argument("--variant", default=method.DEFAULT_VARIANT, choices=list(method.VARIANTS))
    ap.add_argument("--min-series-n", type=int, default=MIN_SERIES_N)
    ap.add_argument("--min-type-n", type=int, default=MIN_TYPE_N)
    ap.add_argument("--perm", type=int, default=N_PERM, help="permutations for the type test (0 = skip)")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)

    as_of = None
    if args.as_of:
        as_of = to_epoch(args.as_of)
        if as_of is None:
            raise SystemExit(f"unreadable --as-of {args.as_of!r}")
    log(f"track record: reading {_rel(args.db)} (read-only), variant {args.variant}"
        + (f", as of {iso(as_of)}" if as_of is not None else ""))
    conn = open_db(args.db)
    rep = build_report(conn, as_of, args.variant, args.min_series_n, args.min_type_n, args.perm, args.seed,
                       db_path=args.db)
    st = rep["store"]
    log(f"store: {st['event_rows']} event rows ({st['event_rows_untyped']} untyped), {st['kept_markets']} kept "
        f"markets, results {st['results_by_status']}; closes {st['first_close']} .. {st['last_close']}")
    log(f"{rep['n_events']} events counted ({rep['first_close']} .. {rep['last_close']})")
    print_table(rep)
    path = write_report(rep, args.out)
    log(f"track record -> {_rel(path)}")
    return rep


if __name__ == "__main__":
    main()
