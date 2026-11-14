"""
The replay backtest (demo output 4): on past days, would the open list's
ranking have picked markets that ended up with heavy flagged money more
often than the pool average?

The open list cannot be judged on today's markets: their outcome is not
known yet. So the ranking is replayed on past days D, on the markets the
closed scan has already finished, and each pick is scored on what the
method found once the event had ended.

Evaluable events. The store's event-level ok results
(track_record.load_events with as_of None): one row per Gamma event with
its FINAL flagged_pct and flagged_capital (the outcome label, see below)
and its series_key, plus EVERY kept market the store has for the event
(store.markets, role kept), each with its open time (the later of Gamma
createdAt / startDate), endDate, closedTime, volume, outcomes and rules.
Those fields come from the closed scan's cached window lists
(data/watchlist/cache/closed_lists/, no network) or, failing those, from
Gamma once (net.markets_by_condition); either way they are kept in this
module's own table backtest_markets, so they are never fetched twice.
Event rows are written by closed_scan.refresh_events at the END of each
closed-scan run, so the markets of a backfill that is still running are
not evaluable until that run has finished (or been stopped by its budget).

Replay days. D = UTC midnight, every --every-days (3) days from the
earliest event close + --warmup-days (21; the track record needs history
to stand on) to the latest close - 1 day. Midnight matters: the
user-volume look-ups behind the flags are floored to whole UTC days, so
with D at midnight even their day-wide answers end at D.

The event at D, judged the way the live open list would judge it (not on
the figure market the closed scan chose after the end):
  pool     its kept markets that the live pool would hold at D, per market
           as filters.open_pool does: open at D (opened <= D < closedTime),
           endDate <= D + 60 days, a cached fills file
           (data/watchlist/cache/<condition>.csv.gz, method.cache_paths),
           and not decided: the price at D (the median over the last 5
           non-dust fills before D of the first outcome's price, then the
           higher of it and its complement) < 0.99. So the event is
           "already decided at D" only when ALL its open markets with fills
           were >= 0.99. --volume-at-d also drops pool markets whose volume
           as of D was under $100k. An event none of whose open markets has
           a fills file keeps them (its started signal is unknown).
  started  open_scan.event_signal over open_scan.started_signal of each
           pool market (exactly the live check_event's combination: flagged
           dollars summed over markets, wallets distinct across them), on
           each market's cached fills with timestamp in [D - 72 h, D), its
           volume estimated AS OF D (Gamma's final volume x the share of the
           fills' dollars traded before D), and open_scan.MethodFlags as
           flag_fn (the live list's own: method.lookup_priors, cutoff
           variant "exact", at the buyer's first buy in the window, then
           method.flag_whale; a window's big buyers are prefetched in
           parallel): the wallet's history before that moment, which has
           not changed since. A window holds at most
           open_scan.MAX_WINDOW_ROWS fills, newest first, as the live scan
           reads it. A failed wallet look-up is never "not flagged": the
           window is status "error" (unknown; if the event is started
           anyway from the other wallets it stays started, the errors
           recorded) and is never cached.
  end_ts   open_scan.event_end: the earliest endDate among the pool markets.
  type     open_scan.event_type: the AI type of the pool market with the
           highest volume AS OF D that has a label (the cached AI label,
           else the type in its store.markets reason).
  reveal   open_scan.event_reveal over the pool markets' cached AI labels,
           each re-validated with D as "today" (a scheduled reveal before D
           becomes revealed_awaiting_resolution): the live rule (the
           earliest scheduled reveal, else any_day, ...), not only the
           highest-volume market's.
  track    track_record's snapshot as of D (= rate_for(conn, type,
           series_key, as_of_ts=D)): only events whose figure closed
           strictly before D, the figure re-chosen among the markets closed
           by then.
What the ranking sees is ONLY these (the entries passed to
open_scan.rank_events carry the interface fields; the final numbers stay
outside and are joined back by event id afterwards).

Outcome. An event's label stays its FINAL number: the event row's figure
market, i.e. its highest-flagged kept market (closed_scan.choose_figure,
the same rule as the closed list), heavy when flagged_pct >= 10 %.

Cache. Look-ups are cached in the store (wallet_volume /
wallet_day_trades); each (market, D) answer is cached in
backtest_started, keyed by a hash of the code behind it (signal_key: the
open-scan signal, the flag path open_scan.MethodFlags / method.flag_whale /
method.lookup_priors / net.exact_prior, and MAX_WINDOW_ROWS), so a re-run
(or a run cut short by --max-lookup-requests) resumes and a changed
signal recomputes.

Metrics, per D and over all days, for four sets: the top 10 of the full
ranking, the top 10 of a "track record only" ranking (the started signal
ignored: every entry not started), the "started only" subset (every
candidate with the started signal on, however many) and the whole pool.
Per set: n, mean final flagged_pct, share with flagged_pct >= 10 % (k of
n), share heavy and >= $10k flagged (k_big), mean flagged_capital. A
random 10 has the pool's means as its expected values; its heavy count
is hypergeometric, which gives each day's P(random 10 >= the top 10's
count) and, convolved over days, the same for the total. Days share
events (an open market is a candidate on every replay day it is open),
so these p-values treat dependent days as independent and are optimistic;
the distinct-event view (each event counted once) is printed beside them.

Biases this replay cannot remove are listed in BIASES (and the JSON).

CLI (from the repo root; progress lines every <= 20 s; resumable):
  python3 -m watchlist.backtest                       # -> data/watchlist/demo/backtest.json
  python3 -m watchlist.backtest --db data/watchlist/dev_backtest.db --warmup-days 2 --every-days 3 \\
      --max-lookup-requests 300 --out /tmp/backtest.json
  --no-started skips the started signal (no network); --no-gamma uses the cached fields only;
  --volume-at-d applies the $100k volume rule as of D; --db must be an existing store.
"""
import bisect
import csv
import glob
import gzip
import hashlib
import inspect
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from watchlist import ai, closed_scan, filters, method, net, store
from watchlist import track_record as tr
from watchlist.common import CACHE_DIR, DAY, DB_PATH, DEMO_DIR, day_str, iso, log, now_epoch, to_epoch, utc_day

EVERY_DAYS = 3
WARMUP_DAYS = 21
HORIZON_DAYS = filters.HORIZON_DAYS        # 60: the open pool's endDate horizon
WINDOW_HOURS = 72                          # the started signal's look-back
TOP_N = 10
NEXT_N = 10
HEAVY_PCT = tr.HEAVY_PCT                   # 10 %
BIG_CAPITAL = tr.BIG_CAPITAL               # $10,000
DECIDED_PRICE = filters.DECIDED_PRICE      # 0.99: the open pool drops a market priced at or above this
MIN_VOLUME = filters.MIN_VOLUME            # $100k
PRICE_FILLS = 5                            # fills the price at D is the median of
DUST_USD = method.JUMP_MIN_USD             # $10: smaller fills are not the market's price
WORKERS = 4                                # markets processed at once (each holds its fills in memory)
HEAVY_VOLUME = closed_scan.HEAVY_VOLUME    # $3M: markets with this much volume load their fills
HEAVY_SLOTS = 2                            #   at most this many at a time
PROGRESS_S = 20                            # heartbeat interval
GAMMA_BATCH = 50
OUT_PATH = os.path.join(DEMO_DIR, "backtest.json")
STARTED_KEYS = ("started", "flagged_usd", "wallets", "big_buyers")
PREFETCH_WORKERS = 8                       # look-ups of one window's big buyers at once
# a market window whose started answer is not known (the event is "unknown" unless started from its other markets)
UNKNOWN = ("error", "skipped_budget", "no_fills")
# why an event is not a candidate, reported by the market closest to being one
REASON_ORDER = ("ends_later", "no_end_date", "not_open_yet", "closed", "no_open_time")

SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_markets (
    condition_id  TEXT PRIMARY KEY,
    market_id     TEXT,
    opened_ts     INTEGER,             -- the later of createdAt / startDate
    created_ts    INTEGER,
    start_ts      INTEGER,
    end_ts        INTEGER,             -- endDate
    closed_ts     INTEGER,             -- closedTime
    volume        REAL,                -- Gamma volumeNum (final)
    source        TEXT NOT NULL,       -- closed_list | gamma | missing
    market_json   TEXT,                -- closed_scan.slim_market of the Gamma object
    fetched_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_started (
    condition_id  TEXT    NOT NULL,
    day_ts        INTEGER NOT NULL,    -- D (UTC midnight)
    window_s      INTEGER NOT NULL,
    variant       TEXT    NOT NULL,    -- the method's cutoff variant behind flag_fn
    signal_key    TEXT    NOT NULL,    -- signal_key(): sha1 of the signal + flag path source in use
    price_at      REAL,
    volume_at     REAL,
    decided       INTEGER NOT NULL,
    n_fills       INTEGER,             -- raw fills in [D - window, D)
    started       INTEGER,             -- NULL when decided (not computed)
    flagged_usd   REAL,
    n_wallets     INTEGER,
    result_json   TEXT,
    computed_at   INTEGER NOT NULL,
    PRIMARY KEY (condition_id, day_ts, window_s, variant, signal_key)
);
"""


def ensure_schema(conn):
    with store.LOCK:
        conn.executescript(SCHEMA)
        conn.commit()


# ── pure pieces (unit-tested) ────────────────────────────────────────────────

def replay_days(first_close_ts, last_close_ts, every_days=EVERY_DAYS, warmup_days=WARMUP_DAYS):
    """UTC midnights D, every `every_days` days from the first close's day +
    warmup_days up to last_close_ts - 1 day (inclusive). [] when the
    store's span is shorter than the warm-up."""
    if first_close_ts is None or last_close_ts is None:
        return []
    start = (utc_day(first_close_ts) + int(warmup_days)) * DAY
    stop = int(last_close_ts) - DAY
    step = max(1, int(every_days)) * DAY
    return list(range(start, stop + 1, step)) if start <= stop else []


def candidate_reason(mk, day_ts, horizon_days=HORIZON_DAYS):
    """Why a market is NOT in the pool at D on its dates (None when it is):
    no_open_time, not_open_yet (opened after D), closed (closedTime <= D),
    no_end_date, ends_later (endDate > D + horizon). The price at D is
    checked later, from the fills."""
    D = int(day_ts)
    if mk.get("opened_ts") is None:
        return "no_open_time"
    if mk["opened_ts"] > D:
        return "not_open_yet"
    if mk.get("closed_ts") is None or mk["closed_ts"] <= D:
        return "closed"
    if mk.get("end_ts") is None:
        return "no_end_date"
    if mk["end_ts"] > D + int(horizon_days) * DAY:
        return "ends_later"
    return None


def event_markets(ev):
    """The kept markets of an evaluable event (a dict without a "markets"
    list stands for one market: its own dates)."""
    return ev.get("markets") or [ev]


def open_markets(ev, day_ts, horizon_days=HORIZON_DAYS):
    """The event's markets that are in the pool at D on their dates."""
    return [m for m in event_markets(ev) if candidate_reason(m, day_ts, horizon_days) is None]


def event_reason(ev, day_ts, horizon_days=HORIZON_DAYS):
    """None when one of the event's markets is in the pool at D on its
    dates; else the reason of the market closest to being in it
    (REASON_ORDER: ends_later first, no_open_time last)."""
    rs = [candidate_reason(m, day_ts, horizon_days) for m in event_markets(ev)]
    if any(r is None for r in rs):
        return None
    return min(rs, key=REASON_ORDER.index)


def base_candidates(events, day_ts, horizon_days=HORIZON_DAYS):
    """(candidates, Counter of why the others are not) at D: the events with
    at least one market open at D within the horizon."""
    out, why = [], Counter()
    for ev in events:
        r = event_reason(ev, day_ts, horizon_days)
        if r:
            why[r] += 1
        else:
            out.append(ev)
    return out, why


def event_pool(markets, recs, volume_at_d=False, min_volume=MIN_VOLUME):
    """(pool, why, notes) of one event at D. markets: its markets open at D
    on their dates; recs: {condition_id: that market's record at D}
    (market_days). pool = the markets the live pool would hold: with a
    fills file, not decided (price at D < 0.99) and, with volume_at_d,
    >= min_volume as of D. why: None, "decided" (every market with fills
    was >= 0.99 at D) or "volume_lt_min_at_d" (volume_at_d, none left).
    An event none of whose markets has a fills file keeps them all (none
    can be priced). notes: {unfilled (open markets without fills, left
    out), decided, low_volume (pool markets under min_volume at D),
    all_low_volume (every pool market was: the live pool would not hold
    the event)}."""
    filled = [m for m in markets if recs[m["condition_id"]]["status"] != "no_fills"]
    notes = {"unfilled": len(markets) - len(filled), "decided": 0, "low_volume": 0, "all_low_volume": False}
    if not filled:
        return list(markets), None, notes
    live = [m for m in filled if not recs[m["condition_id"]]["decided"]]
    notes["decided"] = len(filled) - len(live)
    if not live:
        return [], "decided", notes

    def low(m):
        v = recs[m["condition_id"]].get("volume_at")
        return v is not None and v < min_volume

    lows = [m for m in live if low(m)]
    notes["low_volume"] = len(lows)
    if volume_at_d:
        live = [m for m in live if not low(m)]
        if not live:
            return [], "volume_lt_min_at_d", notes
    else:
        notes["all_low_volume"] = len(lows) == len(live)
    return live, None, notes


class Fills:
    """A market's raw fills, oldest first, as compact tuples (ts, wallet,
    side, outcome, size, price, usd), with prefix sums of dollars."""

    def __init__(self, rows, outcomes=None):
        self.rows = sorted(rows, key=lambda r: r[0])
        self.ts = [r[0] for r in self.rows]
        self.cum = [0.0]
        for r in self.rows:
            self.cum.append(self.cum[-1] + r[6])
        self.total_usd = self.cum[-1]
        self.outcomes = list(outcomes or [])

    def window(self, lo, hi):
        """Rows with lo <= ts < hi."""
        return self.rows[bisect.bisect_left(self.ts, int(lo)):bisect.bisect_left(self.ts, int(hi))]

    def usd_before(self, t):
        return self.cum[bisect.bisect_left(self.ts, int(t))]

    def volume_at(self, t, gamma_volume):
        """Gamma's final volume scaled by the share of the fills' dollars
        traded before t (the market's volume as of t, on Gamma's scale)."""
        if self.total_usd <= 0:
            return 0.0
        return float(gamma_volume or 0) * self.usd_before(t) / self.total_usd

    def price_at(self, t, n=PRICE_FILLS, dust=DUST_USD):
        """The market's highest outcome price just before t: the median over
        the last n fills worth >= dust before t of the first outcome's
        price (a fill on the other outcome at p counts as 1 - p), then the
        higher of it and its complement. None without such a fill."""
        first = str(self.outcomes[0]).strip().lower() if self.outcomes else None
        i = bisect.bisect_left(self.ts, int(t)) - 1
        got = []
        while i >= 0 and len(got) < n:
            r = self.rows[i]
            i -= 1
            if r[6] < dust:
                continue
            p = r[5]
            if first is not None and str(r[3]).strip().lower() != first:
                p = 1.0 - p
            got.append(p)
        if not got:
            return None
        m = statistics.median(got)
        return max(m, 1.0 - m)


def fill_tuple(row):
    """A raw-CSV row (polymarket_trades.format_row shape) as a Fills tuple."""
    ts = row.get("timestamp")
    try:
        t = int(datetime.fromisoformat(ts).timestamp())
    except (TypeError, ValueError):
        t = to_epoch(ts)
    if t is None:
        return None
    try:
        size, price, usd = float(row.get("contracts") or 0), float(row.get("odds") or 0), float(row.get("dollar_amount") or 0)
    except ValueError:
        return None
    return (t, sys.intern(str(row.get("user_id") or "").strip().lower()), sys.intern(str(row.get("side") or "").upper()),
            sys.intern(str(row.get("outcome") or "")), size, price, usd)


def to_v2(t, outcomes, condition_id=None):
    """A Fills tuple as a /v2/trades row (what open_scan.started_signal reads)."""
    names = [str(o).strip().lower() for o in outcomes or []]
    o = str(t[3]).strip().lower()
    return {"proxy_wallet": t[1], "side": t[2], "outcome": t[3],
            "outcome_index": names.index(o) if o in names else None,
            "size": t[4], "price": t[5], "timestamp": t[0], "condition_id": condition_id}


def rank_entry(event_id, title, end_ts, track, reveal_kind, reveal_ts, signal=None):
    """The open_scan.rank_events entry for a candidate: ONLY what is known
    at D (no final numbers). signal: the event's started signal
    (open_scan.event_signal over its pool markets) or None; as in the live
    list (open_scan.build_entry), its dollars and wallets count only when
    it is started."""
    s = signal or {}
    started = bool(s.get("started"))
    return {"event_id": event_id, "title": title, "end_ts": int(end_ts),
            "reveal_kind": reveal_kind or "unclear", "reveal_ts": reveal_ts, "started": started,
            "started_flagged_usd": float(s.get("flagged_usd") or 0.0) if started else 0.0,
            "started_wallets": len(s.get("wallets") or []) if started else 0, "track": track}


def track_only(entry):
    """The same entry with the started signal switched off."""
    return {**entry, "started": False, "started_flagged_usd": 0.0, "started_wallets": 0}


def _mean(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 4) if xs else None


def metrics(items, heavy_pct=HEAVY_PCT, big_capital=BIG_CAPITAL):
    """{n, k_heavy, heavy_share, k_big, big_share, mean_flagged_pct,
    median_flagged_pct, mean_flagged_capital} of events carrying their
    FINAL flagged_pct / flagged_capital. Shares and means are None at n 0."""
    n = len(items)
    pcts = [float(e.get("flagged_pct") or 0.0) for e in items]
    caps = [float(e.get("flagged_capital") or 0.0) for e in items]
    heavy = [tr.is_heavy(e.get("flagged_pct"), heavy_pct) for e in items]
    k = sum(heavy)
    k_big = sum(1 for h, c in zip(heavy, caps) if h and c >= big_capital)
    return {"n": n, "k_heavy": k, "heavy_share": round(k / n, 4) if n else None,
            "k_big": k_big, "big_share": round(k_big / n, 4) if n else None,
            "mean_flagged_pct": _mean(pcts), "median_flagged_pct": round(statistics.median(pcts), 4) if n else None,
            "mean_flagged_capital": round(sum(caps) / n, 2) if n else None}


def hypergeom_pmf(N, K, n):
    """[P(X = x) for x in 0..n] for X ~ Hypergeometric(population N, K
    successes, n draws without replacement)."""
    N, K, n = int(N), int(K), int(n)
    if not (0 <= K <= N and 0 <= n <= N):
        raise ValueError(f"bad hypergeometric parameters N={N} K={K} n={n}")
    tot = math.comb(N, n)
    return [math.comb(K, x) * math.comb(N - K, n - x) / tot for x in range(n + 1)]


def tail_p(pmf, k):
    """P(X >= k) from a pmf list."""
    return min(1.0, max(0.0, sum(pmf[int(k):]))) if k > 0 else 1.0


def convolve(a, b):
    out = [0.0] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        if x:
            for j, y in enumerate(b):
                out[i + j] += x * y
    return out


def summed_tail_p(draws, observed):
    """P(sum of independent hypergeometrics >= observed); draws = [(N, K, n)]."""
    pmf = [1.0]
    for N, K, n in draws:
        pmf = convolve(pmf, hypergeom_pmf(N, K, n))
    return tail_p(pmf, observed)


def day_metrics(cands, finals, top_ids, track_ids, started_ids, top=TOP_N):
    """One replay day's metric block. cands: the day's candidate event ids;
    finals: {event_id: final event dict}; top_ids / track_ids: the two
    rankings' top lists; started_ids: candidates with the signal on."""
    pool = [finals[e] for e in cands]
    pm = metrics(pool)
    N, K = pm["n"], pm["k_heavy"]
    out = {"pool": pm}
    for name, ids in (("top", top_ids), ("track_top", track_ids), ("started", started_ids)):
        m = metrics([finals[e] for e in ids])
        if name != "started" and N:
            m["p_random_ge"] = round(tail_p(hypergeom_pmf(N, K, m["n"]), m["k_heavy"]), 4)
        out[name] = m
    n_draw = min(top, N)
    out["random_top"] = {"n": n_draw, "expected_k_heavy": round(n_draw * K / N, 4) if N else None,
                         "heavy_share": pm["heavy_share"], "mean_flagged_pct": pm["mean_flagged_pct"],
                         "mean_flagged_capital": pm["mean_flagged_capital"]}
    out["informative"] = N > top
    return out


def aggregate(days, top=TOP_N, finals=None):
    """The all-days summary of day records ({day, metrics, top_ids,
    track_ids, started_ids, candidates}). Day means weigh each day once
    (over the days where the set is non-empty); pooled numbers add up
    k and n over days; hits compare the summed heavy counts of a top list
    with the summed expectation of a random draw of the same size.
    distinct_events counts each event once per set ({n, k_heavy}; k_heavy
    needs finals = {event_id: final event}): an event that sits in the top
    10 on eight days is eight picks but one event."""
    out = {"days": len(days), "days_informative": sum(1 for d in days if d["metrics"]["informative"]),
           "days_with_started": sum(1 for d in days if d["metrics"]["started"]["n"]),
           "candidates_total": sum(d["metrics"]["pool"]["n"] for d in days)}
    sizes = [d["metrics"]["pool"]["n"] for d in days]
    out["candidates_per_day"] = {"min": min(sizes) if sizes else None, "median": statistics.median(sizes) if sizes else None,
                                 "max": max(sizes) if sizes else None}
    for name in ("pool", "top", "track_top", "started"):
        ms = [d["metrics"][name] for d in days if d["metrics"][name]["n"]]
        k = sum(m["k_heavy"] for m in ms)
        n = sum(m["n"] for m in ms)
        kb = sum(m["k_big"] for m in ms)
        blk = {"days": len(ms), "n": n, "k_heavy": k, "k_big": kb,
               "pooled_heavy_share": round(k / n, 4) if n else None,
               "day_mean_heavy_share": _mean(m["heavy_share"] for m in ms),
               "day_mean_flagged_pct": _mean(m["mean_flagged_pct"] for m in ms),
               "day_mean_flagged_capital": round(sum(m["mean_flagged_capital"] for m in ms) / len(ms), 2) if ms else None}
        if name in ("top", "track_top", "started"):
            # the pool on the same days, and the summed random expectation
            same = [d["metrics"] for d in days if d["metrics"][name]["n"]]
            exp = sum(m[name]["n"] * m["pool"]["k_heavy"] / m["pool"]["n"] for m in same if m["pool"]["n"])
            blk["pool_heavy_share_same_days"] = _mean(m["pool"]["heavy_share"] for m in same)
            blk["expected_k_heavy_random"] = round(exp, 3)
            blk["lift_k_heavy"] = round(k - exp, 3)
            draws = [(m["pool"]["n"], m["pool"]["k_heavy"], m[name]["n"]) for m in same if m["pool"]["n"]]
            blk["p_random_ge_summed"] = round(summed_tail_p(draws, k), 4) if draws else None
            if name != "started":
                cmp = Counter()
                for m in same:
                    a, b = m[name]["heavy_share"], m["pool"]["heavy_share"]
                    cmp["above" if a > b else "below" if a < b else "equal"] += 1
                blk["days_vs_pool_share"] = dict(cmp)
        out[name] = blk
    # each event counted once: the events ever picked vs the events ever in a pool
    out["distinct_events"] = {}
    for name, key in (("pool", "candidates"), ("top", "top_ids"), ("track_top", "track_ids"),
                      ("started", "started_ids")):
        ids = {e for d in days for e in d[key]}
        if all("heavy_ids" in d for d in days):
            # heavy as scored on the days the event was in this set (its markets still open then)
            k = len({e for d in days for e in d[key] if e in set(d["heavy_ids"])})
        else:
            k = sum(1 for e in ids if tr.is_heavy(finals[e].get("flagged_pct"))) if finals is not None else None
        out["distinct_events"][name] = {"n": len(ids), "k_heavy": k,
                                        "heavy_share": round(k / len(ids), 4) if k is not None and ids else None}
    return out


# ── the inputs ───────────────────────────────────────────────────────────────

def _list_markets(list_dir=closed_scan.LIST_DIR):
    """{condition_id: slim market} from the closed scan's cached window lists."""
    out = {}
    for path in sorted(glob.glob(os.path.join(list_dir, "*.json.gz")), key=os.path.getmtime):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for m in json.load(f).get("markets") or []:
                    if m.get("conditionId"):
                        out[m["conditionId"]] = m
        except (OSError, ValueError) as e:
            log(f"  ! unreadable window list {path} ({e}); skipped")
    return out


def _fields_row(cid, m, source):
    created, start = to_epoch(m.get("createdAt")), to_epoch(m.get("startDate"))
    opened = max([t for t in (created, start) if t is not None], default=None)
    return (cid, str(m.get("id") or "") or None, opened, created, start, to_epoch(m.get("endDate")),
            to_epoch(m.get("closedTime")), filters.volume(m), source, json.dumps(m, separators=(",", ":")),
            now_epoch())


def market_fields(conn, condition_ids, fetch=True, refetch=False, list_dir=closed_scan.LIST_DIR, fetcher=None):
    """{condition_id: row of backtest_markets (market_json parsed into
    "market")}: from the table, else the closed scan's window lists, else
    Gamma (fetch=True; one request per 50 markets). Every market looked for
    gets a row (source "missing" when Gamma has none), so nothing is
    fetched twice."""
    ensure_schema(conn)
    ids = sorted({c for c in condition_ids if c})
    have = {}
    if not refetch:
        with store.LOCK:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                for r in conn.execute(f"SELECT * FROM backtest_markets WHERE condition_id IN ({q})", chunk):
                    have[r["condition_id"]] = dict(r)
    need = [c for c in ids if c not in have]
    new_rows = []
    if need:
        lists = _list_markets(list_dir)
        for c in need:
            if c in lists:
                new_rows.append(_fields_row(c, lists[c], "closed_list"))
        got = {r[0] for r in new_rows}
        rest = [c for c in need if c not in got]
        if rest and fetch:
            fetcher = fetcher or net.markets_by_condition
            log(f"  Gamma: fields of {len(rest)} markets not in the cached lists ({-(-len(rest) // GAMMA_BATCH)} requests)")
            found = {}
            for i in range(0, len(rest), GAMMA_BATCH):
                for m in fetcher(rest[i:i + GAMMA_BATCH]):
                    if m.get("conditionId") in rest:
                        found[m["conditionId"]] = closed_scan.slim_market(m)
            for c in rest:
                if c in found:
                    new_rows.append(_fields_row(c, found[c], "gamma"))
                else:
                    new_rows.append((c, None, None, None, None, None, None, None, "missing", None, now_epoch()))
        if new_rows:
            with store.transaction(conn):
                conn.executemany("INSERT OR REPLACE INTO backtest_markets (condition_id, market_id, opened_ts, "
                                 "created_ts, start_ts, end_ts, closed_ts, volume, source, market_json, fetched_at) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", new_rows)
            for r in new_rows:
                have[r[0]] = dict(zip(("condition_id", "market_id", "opened_ts", "created_ts", "start_ts", "end_ts",
                                       "closed_ts", "volume", "source", "market_json", "fetched_at"), r))
    for r in have.values():
        r["market"] = json.loads(r["market_json"]) if r.get("market_json") else None
    return have


def _kept_markets(conn, event_ids):
    """{event_id: [{condition_id, reason, volume}]}: the kept markets the
    store has for these events (every day the closed scan has seen)."""
    want = {str(e) for e in event_ids if e}
    with store.LOCK:
        rows = conn.execute("SELECT condition_id, event_id, reason, volume FROM markets WHERE role='kept'").fetchall()
    out = defaultdict(list)
    for r in rows:
        if r["event_id"] in want:
            out[r["event_id"]].append(dict(r))
    return out


def evaluable_events(conn, variant=method.DEFAULT_VARIANT, fetch=True, refetch=False, fetcher=None):
    """(events, counts): the store's event-level ok results (the final
    numbers, the outcome label) with every kept market of the event:
    "markets" = [{condition_id, market_id, opened_ts, end_ts, closed_ts,
    volume (final), outcomes, market (the Gamma object), ai_type (from its
    store.markets reason)}], highest final volume first. Markets without
    fields are left out (counted); an event with none left is dropped.
    Markets without an open time are kept (candidate_reason says why they
    never qualify)."""
    evs = tr.load_events(conn, None, variant)
    kept = _kept_markets(conn, [e["event_id"] for e in evs])
    for e in evs:                      # the figure is a kept market; make sure it is listed
        rows = kept[e["event_id"]]
        if e["condition_id"] and e["condition_id"] not in {r["condition_id"] for r in rows}:
            rows.append({"condition_id": e["condition_id"], "event_id": e["event_id"], "reason": None, "volume": None})
    fields = market_fields(conn, [r["condition_id"] for e in evs for r in kept[e["event_id"]]], fetch=fetch,
                           refetch=refetch, fetcher=fetcher)
    out, counts = [], Counter()
    for e in evs:
        mks = []
        for r in kept[e["event_id"]]:
            f = fields.get(r["condition_id"])
            if not f or f["source"] == "missing" or not f.get("market"):
                counts["markets_no_fields"] += 1
                continue
            m = f["market"]
            counts[f"fields_{f['source']}"] += 1
            mks.append({"condition_id": r["condition_id"], "market_id": f["market_id"], "event_id": e["event_id"],
                        "opened_ts": f["opened_ts"], "end_ts": f["end_ts"], "closed_ts": f["closed_ts"],
                        "volume": f["volume"] or 0.0, "question": m.get("question"),
                        "outcomes": filters._json_list(m.get("outcomes")) or ["Yes", "No"], "market": m,
                        "ai_type": closed_scan.ai_type(r.get("reason"))})
        if not mks:
            counts["no_market_fields"] += 1
            continue
        mks.sort(key=lambda m: (-(m["volume"] or 0), m["condition_id"]))
        counts["markets"] += len(mks)
        out.append({**e, "markets": mks})
    counts["evaluable"] = len(out)
    return out, counts


def label_at(conn, mk, day_ts):
    """(label, state) of one market at D: its cached AI label validated
    with D's date as today (state "labelled"), else None with state
    "no_label", or "read_error" when reading the cached row failed. Without
    a cached label the type in the market's store.markets reason stands in
    ({type, reveal_kind "unclear"}: a type, no reveal)."""
    m = mk.get("market") or mk
    try:
        lab = ai._from_cache(conn, ai._item(m), today=day_str(day_ts, is_day=False))
        state = "labelled" if lab else "no_label"
    except Exception:          # a malformed cached row must not stop the replay
        lab, state = None, "read_error"
    if not lab and mk.get("ai_type"):
        lab = {"type": mk["ai_type"], "reveal_kind": "unclear", "reveal_date": None, "quote": None,
               "source": "markets.reason"}
    return lab, state


def reveal_at(conn, market, day_ts):
    """(reveal_kind, reveal_ts, state) from the market's cached AI label,
    validated with D's date as today; ("unclear", None, "no_label") without
    one (state "read_error" when the cached row could not be read)."""
    lab, state = label_at(conn, {"market": market}, day_ts)
    d = (lab or {}).get("reveal_date")
    return (lab or {}).get("reveal_kind") or "unclear", (to_epoch(d) if d else None), state


def load_fills(condition_id, outcomes=None, cache_dir=CACHE_DIR):
    """Fills of a market from its cached raw CSV (.csv.gz, else .csv), or None."""
    gz = method.cache_paths(condition_id, cache_dir)["raw_gz"]
    for path in (gz, gz[:-3]):
        if os.path.exists(path):
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", newline="", encoding="utf-8") as f:
                rows = [t for t in (fill_tuple(r) for r in csv.DictReader(f)) if t is not None]
            return Fills(rows, outcomes)
    return None


# ── the started signal at D ──────────────────────────────────────────────────

class Counters:
    """Thread-safe run counters (look-ups, windows), read by the heartbeat."""

    def __init__(self):
        self.lock = threading.Lock()
        self.c = Counter()

    def add(self, key, n=1):
        with self.lock:
            self.c[key] += n

    def get(self, key):
        with self.lock:
            return self.c[key]

    def snapshot(self):
        with self.lock:
            return dict(self.c)


def lookup_requests():
    """Wallet look-up requests this process has made (user-volume on Data v2
    general + the exact check's /v2/activity pages): the --max-lookup-requests
    budget counts these."""
    return net.BUCKETS["data_general"].taken + net.BUCKETS["data_activity"].taken


# The open-scan pieces whose source decides a started answer (the cache key) ...
SIGNAL_PARTS = ("positions", "big_buyers", "_verdict", "started_signal")
SIGNAL_CONSTS = ("STARTED_MIN_USD", "STARTED_MIN_WALLETS", "NEAR_CERTAIN_ODDS")
# ... and the flag path behind open_scan.MethodFlags: the prior look-up and the method's rule
FLAG_PARTS = ((method, ("lookup_priors", "lookup_end", "bracket", "flag_whale")),
              (net, ("exact_prior", "day_trades", "parse_user_volume")))
FLAG_CONSTS = ((method, ("FIRST_X_TRADES", "PERCENT_VOLUME")),)


def _source(f):
    try:
        return inspect.getsource(f)
    except (OSError, TypeError):
        return getattr(f, "__qualname__", repr(f))


def signal_key(signal_fn, flag_fn=None, max_window_rows=None):
    """A short hash of the code behind a cached started answer: the
    signal's source (started_signal and the open_scan helpers it calls,
    plus its thresholds), the flag path (for open_scan.MethodFlags: the
    class, method.flag_whale / lookup_priors and their helpers,
    net.exact_prior and the counts it adds, FIRST_X_TRADES /
    PERCENT_VOLUME; for any other flag_fn: its own source) and the window
    cap MAX_WINDOW_ROWS, so a change to any of them recomputes instead of
    reading stale cache rows. A part whose source is unavailable counts by
    its name."""
    mod = inspect.getmodule(signal_fn)
    srcs = [_source(f) for f in [signal_fn] + [getattr(mod, n) for n in SIGNAL_PARTS
                                               if mod is not None and hasattr(mod, n)]]
    srcs += [f"{n}={getattr(mod, n)!r}" for n in SIGNAL_CONSTS if mod is not None and hasattr(mod, n)]
    if flag_fn is not None:
        try:
            from watchlist import open_scan
            method_flags = isinstance(flag_fn, open_scan.MethodFlags)
        except ImportError:
            open_scan, method_flags = None, False
        if method_flags:
            srcs.append(_source(open_scan.MethodFlags))
            for m, names in FLAG_PARTS:
                srcs += [_source(getattr(m, n)) for n in names if hasattr(m, n)]
            for m, names in FLAG_CONSTS:
                srcs += [f"{m.__name__}.{n}={getattr(m, n)!r}" for n in names if hasattr(m, n)]
        else:
            srcs.append("flag_fn:" + _source(flag_fn if inspect.isroutine(flag_fn) else type(flag_fn)))
    srcs.append(f"MAX_WINDOW_ROWS={max_window_rows!r}")
    return hashlib.sha1("\n".join(dict.fromkeys(srcs)).encode("utf-8")).hexdigest()[:12]


def _cached_started(conn, cid, days, window_s, variant, skey):
    q = ",".join("?" * len(days))
    with store.LOCK:
        rows = conn.execute(f"SELECT * FROM backtest_started WHERE condition_id=? AND window_s=? AND variant=? AND "
                            f"signal_key=? AND day_ts IN ({q})", (cid, window_s, variant, skey, *days)).fetchall()
    out = {}
    for r in rows:
        res = json.loads(r["result_json"]) if r["result_json"] else None
        out[r["day_ts"]] = {"price_at": r["price_at"], "volume_at": r["volume_at"], "decided": bool(r["decided"]),
                            "n_fills": r["n_fills"], "started": res, "status": "cached"}
    return out


def _save_started(conn, cid, D, window_s, variant, skey, rec):
    res = rec.get("started")
    with store.transaction(conn):
        conn.execute("INSERT OR REPLACE INTO backtest_started (condition_id, day_ts, window_s, variant, signal_key, "
                     "price_at, volume_at, decided, n_fills, started, flagged_usd, n_wallets, result_json, computed_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, int(D), int(window_s), variant, skey, rec.get("price_at"), rec.get("volume_at"),
                      1 if rec["decided"] else 0, rec.get("n_fills"),
                      None if res is None else (1 if res.get("started") else 0),
                      None if res is None else float(res.get("flagged_usd") or 0),
                      None if res is None else len(res.get("wallets") or []),
                      None if res is None else json.dumps(res, separators=(",", ":"), default=str), now_epoch()))


def _scrub(text, limit=300):
    """An error text without wallet addresses (open_scan.scrub), cut to limit."""
    try:
        from watchlist.open_scan import scrub
    except ImportError:
        return str(text)[:limit]
    return scrub(text, limit)


def market_days(conn, mk, days, signal_fn, flag_fn, cfg, counters, fills_loader=load_fills):
    """{D: record} for one market on the days it is open: price_at /
    volume_at at D, decided (price >= 0.99), n_fills in the window and its
    started signal ({started, flagged_usd, wallets, big_buyers, threshold,
    positions} or None), with status cached | computed | decided |
    no_fills | skipped_budget | error | started_with_errors | off. A window
    in which a big buyer's look-up failed (its flag carries an "error") is
    "error" (its partial answer kept for the event's combination) or, when
    it is started from the other wallets anyway, "started_with_errors"; it
    is never cached, so the next run asks again. Only fills before D are
    read."""
    cid, window_s = mk["condition_id"], cfg["window_s"]
    use_cache = cfg.get("use_cache", True) and cfg.get("with_started", True)
    done = _cached_started(conn, cid, days, window_s, cfg["variant"], cfg["signal_key"]) if use_cache else {}
    out = dict(done)
    if len(done) == len(days):
        counters.add("windows_cached", len(days))
        return out
    fills = fills_loader(cid, mk.get("outcomes"))
    counters.add("windows_cached", len(done))
    for D in days:
        if D in done:
            continue
        if fills is None:
            out[D] = {"price_at": None, "volume_at": None, "decided": False, "n_fills": None, "started": None,
                      "status": "no_fills"}
            counters.add("windows_no_fills")
            continue
        price, vol = fills.price_at(D), fills.volume_at(D, mk.get("volume"))
        rows = fills.window(D - window_s, D)
        rec = {"price_at": None if price is None else round(price, 4), "volume_at": round(vol, 2),
               "decided": price is not None and price >= DECIDED_PRICE, "n_fills": len(rows), "started": None}
        if rec["decided"]:
            rec["status"] = "decided"
        elif not cfg.get("with_started", True):
            rec["status"] = "off"
        elif cfg.get("max_lookup_requests") is not None and \
                lookup_requests() - cfg.get("requests_at_start", 0) >= cfg["max_lookup_requests"]:
            rec["status"] = "skipped_budget"
            counters.add("windows_skipped_budget")
            out[D] = rec
            continue
        else:
            cap = cfg.get("max_window_rows")
            if cap and len(rows) > cap:        # the live scan reads at most this many fills, newest first
                rows = rows[-cap:]
                rec["capped"] = True
                counters.add("windows_capped")
            trades = [to_v2(t, mk.get("outcomes"), cid) for t in rows]
            t0 = time.time()
            try:
                if cfg.get("bigs_fn") is not None and hasattr(flag_fn, "prefetch"):
                    bigs = cfg["bigs_fn"](trades, vol, D - window_s, D)
                    flag_fn.prefetch([(b["wallet"], b["first_buy_ts"], b["net_usd"]) for b in bigs])
                res = signal_fn(trades, vol, flag_fn, D - window_s, D)
            except Exception as e:     # a look-up failed: not cached, retried on the next run
                rec["status"], rec["error"] = "error", _scrub(f"{type(e).__name__}: {e}")
                counters.add("windows_error")
                counters.add("window_seconds", time.time() - t0)
                out[D] = rec
                continue
            counters.add("window_seconds", time.time() - t0)
            rec["started"] = {k: res.get(k) for k in STARTED_KEYS + ("threshold", "positions")}
            bad = [b for b in res.get("big_buyers") or [] if (b.get("flag") or {}).get("error")]
            if bad:
                # a failed look-up is not "not flagged": the answer is incomplete, so it is never cached
                rec["lookup_errors"] = len(bad)
                rec["error"] = _scrub(f"the prior look-up failed for {len(bad)} big buyer(s): {bad[0]['flag']['error']}")
                if res.get("started"):
                    rec["status"] = "started_with_errors"
                    counters.add("windows_error_started")
                else:
                    rec["status"] = "error"
                    counters.add("windows_error")
            else:
                rec["status"] = "computed"
                counters.add("windows_computed")
        if use_cache and rec["status"] in ("computed", "decided"):
            _save_started(conn, cid, D, window_s, cfg["variant"], cfg["signal_key"], rec)
        out[D] = rec
    del fills
    return out


def event_view(ev, pool, recs, labels, snap, osc, event_fn, min_series_n=tr.MIN_SERIES_N):
    """What the live open list would know of an event at D, from its pool
    markets (event_pool) and their records and labels at D ({condition_id:
    ...}): the started signal (event_fn = open_scan.event_signal over the
    markets' started_signal answers), its status (started | unknown | off
    | not_started: unknown when a pool market's answer is not known and
    the event is not started anyway), end_ts (open_scan.event_end), type
    (open_scan.event_type, by volume AS OF D), reveal
    (open_scan.event_reveal) and the track record at D (snap.rate)."""
    mdicts, labs = [], {}
    for m in pool:
        r = recs[m["condition_id"]]
        vol = r.get("volume_at")
        mdicts.append({**m["market"], "volumeNum": m["volume"] if vol is None else vol})
        lab = labels.get(m["condition_id"])
        if lab and m["market"].get("id") is not None:
            labs[str(m["market"]["id"])] = lab
    type_, type_m = osc.event_type(mdicts, labs)
    rev = osc.event_reveal(mdicts, labs)
    end_ts = osc.event_end(mdicts)
    sigs = [recs[m["condition_id"]]["started"] for m in pool if recs[m["condition_id"]].get("started")]
    sig = event_fn(sigs) if sigs else None
    statuses = [recs[m["condition_id"]]["status"] for m in pool]
    unknown = next((s for s in UNKNOWN if s in statuses), None)       # the worst first
    errors = [recs[m["condition_id"]]["error"] for m in pool if recs[m["condition_id"]].get("error")]
    started = bool(sig and sig.get("started"))
    status = ("started" if started else "unknown" if unknown else
              "off" if statuses and all(s == "off" for s in statuses) else "not_started")
    return {"signal": sig, "status": status, "unknown": None if started else unknown, "errors": errors,
            "end_ts": end_ts, "type": type_, "type_market": type_m.get("question") if type_m else None,
            "reveal_kind": rev["kind"], "reveal_ts": rev["ts"],
            "track": snap.rate(type_, ev.get("series_key"), min_series_n),
            "pool": [m["condition_id"] for m in pool]}


# ── the replay ───────────────────────────────────────────────────────────────

class Heartbeat:
    """Prints status() every `every` seconds until stopped (so an unattended
    run never goes quiet for long)."""

    def __init__(self, status, every=PROGRESS_S):
        self.status, self.every = status, every
        self.stop_ev = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop_ev.wait(self.every):
            try:
                log(self.status())
            except Exception as e:  # noqa: BLE001  (a status line must never kill the run)
                log(f"  [backtest] status unavailable: {e}")

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop_ev.set()
        self.t.join(timeout=2)


def _open_scan():
    """watchlist.open_scan (rank_events, started_signal, event_signal, big_buyers, MethodFlags, event_type,
    event_reveal, event_end)."""
    try:
        from watchlist import open_scan
    except ImportError as e:
        raise RuntimeError("watchlist.open_scan is not available (rank_events / started_signal / event_signal); "
                           "the backtest replays the open list's own code") from e
    return open_scan


def _closed_before(ev, D):
    """True when the event's outcome (figure) market closed before D."""
    return ev.get("closed_ts") is not None and ev["closed_ts"] < D


def market_results(conn, variant=method.DEFAULT_VARIANT):
    """{condition_id: {status, flagged_capital, flagged_pct, closed_ts}} of
    every analysed market, for scoring an event on the markets still open
    at D (outcome_at)."""
    with store.LOCK:
        rows = conn.execute("SELECT condition_id, status, flagged_capital, flagged_pct, closed_ts FROM results "
                            "WHERE cutoff_variant=?", (variant,)).fetchall()
    return {r["condition_id"]: dict(r) for r in rows}


def outcome_at(ev, D, mres):
    """The event's outcome as seen from day D: the closed list's figure rule
    (closed_scan.choose_figure: the highest flagged capital among ok
    results) over ONLY its markets that closed after D. Its final number
    (ev["flagged_pct"]) may come from a market that had already closed by
    D, whose result was then public: scoring a pick on it would count a
    known answer as a hit. None when no market of the event closes after D
    with a result."""
    cands = []
    for m in ev.get("markets") or []:
        r = mres.get(m["condition_id"])
        closed = (r or {}).get("closed_ts") or m.get("closed_ts")
        if r is None or closed is None or closed <= D:
            continue
        cands.append({"condition_id": m["condition_id"], "status": r["status"], "flagged_capital": r["flagged_capital"],
                      "flagged_pct": r["flagged_pct"], "volume": m.get("volume"), "closed_ts": closed})
    fig = closed_scan.choose_figure(cands)
    if fig is None or fig.get("status") != "ok":
        return None
    return {"event_id": ev["event_id"], "condition_id": fig["condition_id"], "closed_ts": fig["closed_ts"],
            "flagged_pct": fig.get("flagged_pct"), "flagged_capital": fig.get("flagged_capital")}


def _pick(ev, entry, view, D, outcome=None):
    """The JSON line of one ranked event: what the ranking saw + the outcome
    (outcome_at(ev, D): its markets still open at D; the event's final
    number when not given)."""
    track = view["track"]
    sig = view["signal"] or {}
    outcome = outcome or ev
    closed = outcome.get("closed_ts")
    return {"event_id": ev["event_id"], "title": ev.get("title"), "type": view["type"],
            "type_market": view["type_market"], "final_type": ev.get("type"),
            "series_key": ev.get("series_key"), "pool_markets": len(view["pool"]),
            "outcome_market": outcome.get("condition_id"),
            "basis": track.get("basis"), "track_key": track.get("key"), "track_k": track.get("k"),
            "track_n": track.get("n"), "track_rate": track.get("rate"), "track_low": track.get("low"),
            "started": entry["started"], "started_status": view["status"],
            "started_flagged_usd": round(entry["started_flagged_usd"], 2), "started_wallets": entry["started_wallets"],
            "flagged_usd_at_d": round(float(sig.get("flagged_usd") or 0.0), 2),
            "lookup_errors": view["errors"][:3], "reveal_kind": entry["reveal_kind"],
            "end": day_str(entry["end_ts"], is_day=False),
            "closed": day_str(closed, is_day=False) if closed is not None else None,
            "days_to_close": round((closed - D) / DAY, 1) if closed is not None else None,
            "outcome_closed_before_d": _closed_before(ev, D),
            "final_flagged_pct": outcome.get("flagged_pct"),
            "final_flagged_capital": round(outcome.get("flagged_capital") or 0, 2),
            "heavy": tr.is_heavy(outcome.get("flagged_pct"))}


def replay(conn, events, days, rank_fn, signal_fn=None, flag_fn=None, variant=method.DEFAULT_VARIANT,
           horizon_days=HORIZON_DAYS, window_hours=WINDOW_HOURS, top=TOP_N, next_n=NEXT_N,
           min_series_n=tr.MIN_SERIES_N, with_started=True, max_lookup_requests=None, workers=WORKERS,
           counters=None, fills_loader=load_fills, use_cache=True, bigs_fn=None, max_window_rows=None,
           volume_at_d=False, event_fn=None):
    """Replay the open ranking on `days` over `events` (evaluable_events).
    Returns (day_records, info). Three phases: (1) reads only: base
    candidates, the track record snapshot of each D and the AI labels per
    (market, D); (2) per market, in parallel: price at D, the started
    signal (look-ups, cached); (3) pure: each event's pool at D, its view
    (event_view: the live list's combination), rank, metrics.
    volume_at_d=True also drops the pool markets whose volume as of D was
    under $100k (the live pool's rule, which the final-volume universe
    cannot apply otherwise). event_fn defaults to open_scan.event_signal."""
    counters = counters or Counters()
    osc = _open_scan()
    event_fn = event_fn or osc.event_signal
    window_s = int(window_hours * 3600)
    t0 = time.time()
    by_id = {e["event_id"]: e for e in events}
    mk_by = {m["condition_id"]: m for e in events for m in event_markets(e)}
    mres = market_results(conn, variant)

    # phase 1 (no writes, so each D's track-record snapshot is read before any look-up writes to the store)
    base, why_not, opened, snaps, labels = {}, {}, {}, {}, {}
    label_reads = Counter()
    last_line = time.time()
    for i, D in enumerate(days):
        if time.time() - last_line >= PROGRESS_S:
            log(f"  [backtest] phase 1: day {i}/{len(days)} ({day_str(D, False)}), "
                f"{sum(len(v) for v in base.values()):,} candidates so far, {time.time() - t0:.0f} s")
            last_line = time.time()
        cands, why = base_candidates(events, D, horizon_days)
        base[D], why_not[D] = [e["event_id"] for e in cands], why
        # = track_record.rate_for(conn, type, series_key, as_of_ts=D), answered later from ONE snapshot per day
        # (the type at D needs the prices of phase 2; the snapshot must be read before phase 2 writes)
        snaps[D] = tr.snapshot(conn, D, variant)
        for e in cands:
            oms = open_markets(e, D, horizon_days)
            opened[(e["event_id"], D)] = [m["condition_id"] for m in oms]
            for m in oms:
                lab, state = label_at(conn, m, D)
                labels[(m["condition_id"], D)] = lab
                label_reads[state] += 1
    t1 = time.time()
    pairs = sum(len(v) for v in base.values())
    market_pairs = sum(len(v) for v in opened.values())
    log(f"  phase 1: {len(days)} days, {pairs:,} (event, day) base candidates on {market_pairs:,} (market, day) "
        f"pairs; track snapshots + labels in {t1 - t0:.1f} s")

    # phase 2 (per market; look-ups write to the store)
    days_of = defaultdict(list)
    for (eid, D), cids in opened.items():
        for c in cids:
            days_of[c].append(D)
    for c in days_of:
        days_of[c].sort()
    if with_started and flag_fn is None:
        flag_fn = osc.MethodFlags(conn, variant, workers=PREFETCH_WORKERS)
        bigs_fn = bigs_fn or osc.big_buyers
    cfg = {"window_s": window_s, "variant": variant, "with_started": with_started,
           "max_lookup_requests": max_lookup_requests, "requests_at_start": lookup_requests(),
           "signal_key": signal_key(signal_fn, flag_fn, max_window_rows) if signal_fn else "none",
           "use_cache": use_cache, "bigs_fn": bigs_fn, "max_window_rows": max_window_rows}
    recs = {}
    order = sorted(days_of, key=lambda c: (-(mk_by[c].get("volume") or 0), c))   # big downloads first, not last
    done = [0]
    lock = threading.Lock()

    def status():
        c = counters.snapshot()
        return (f"  [backtest] markets {done[0]:,}/{len(order):,}  windows: computed {c.get('windows_computed', 0):,}, "
                f"cached {c.get('windows_cached', 0):,}, no fills {c.get('windows_no_fills', 0):,}, "
                f"errors {c.get('windows_error', 0):,} (+{c.get('windows_error_started', 0):,} started anyway), "
                f"skipped {c.get('windows_skipped_budget', 0):,}  "
                f"look-up requests {lookup_requests() - cfg['requests_at_start']:,}  {time.time() - t0:.0f} s")

    heavy = threading.BoundedSemaphore(HEAVY_SLOTS)

    def work(cid):
        mk = mk_by[cid]
        if (mk.get("volume") or 0) >= HEAVY_VOLUME:
            with heavy:
                r = market_days(conn, mk, days_of[cid], signal_fn, flag_fn, cfg, counters, fills_loader)
        else:
            r = market_days(conn, mk, days_of[cid], signal_fn, flag_fn, cfg, counters, fills_loader)
        with lock:
            recs[cid] = r
            done[0] += 1

    with Heartbeat(status):
        if workers > 1 and len(order) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(work, order))
        else:
            for cid in order:
                work(cid)
    t2 = time.time()
    log(status().replace("[backtest]", "phase 2 done:"))

    # phase 3 (pure)
    out, reveal_kinds = [], Counter()       # reveal_kinds: the reveal the ranking saw, per candidate (event, day)
    for D in days:
        cands, dropped, entries, track_entries, views = [], Counter(), [], [], {}
        unknown_by, n = Counter(), Counter()
        finals_d = {}
        for eid in base[D]:
            ev = by_id[eid]
            cids = opened[(eid, D)]
            r_at = {c: recs[c][D] for c in cids}
            pool, why, notes = event_pool([mk_by[c] for c in cids], r_at, volume_at_d)
            n["open_markets"] += len(cids)
            n["unfilled_markets"] += notes["unfilled"]
            n["decided_markets"] += notes["decided"]
            if why:
                dropped[why] += 1
                continue
            out_d = outcome_at(ev, D, mres)
            if out_d is None:
                dropped["no_outcome_after_d"] += 1
                continue
            finals_d[eid] = out_d
            v = event_view(ev, pool, r_at, {c: labels[(c, D)] for c in cids}, snaps[D], osc, event_fn, min_series_n)
            cands.append(eid)
            views[eid] = v
            reveal_kinds[v["reveal_kind"]] += 1
            n["pool_markets"] += len(pool)
            n["multi_market"] += len(pool) > 1
            n["volume_lt_min_at_d"] += notes["all_low_volume"]
            n["outcome_closed_before_d"] += _closed_before(ev, D)
            n["type_differs_from_final"] += v["type"] != ev.get("type")
            if v["unknown"]:
                unknown_by[v["unknown"]] += 1
            if v["errors"] and v["status"] == "started":
                n["started_with_errors"] += 1
            entry = rank_entry(eid, ev.get("title"), v["end_ts"], v["track"], v["reveal_kind"], v["reveal_ts"],
                               v["signal"])
            entries.append(entry)
            track_entries.append(track_only(entry))
        ent_by = {e["event_id"]: e for e in entries}
        ranked = rank_fn(entries, top=top, next_n=next_n, now_ts=D) if entries else {"top": [], "next": [], "rest": []}
        ranked_t = rank_fn(track_entries, top=top, next_n=next_n, now_ts=D) if entries else {"top": []}
        top_ids = [e["event_id"] for e in ranked["top"]]
        track_ids = [e["event_id"] for e in ranked_t["top"]]
        started_ids = [eid for eid in cands if ent_by[eid]["started"]]
        m = day_metrics(cands, finals_d, top_ids, track_ids, started_ids, top)
        out.append({
            "day": day_str(D, is_day=False), "day_ts": D,
            "base_candidates": len(base[D]), "not_candidates": dict(why_not[D]), "dropped": dict(dropped),
            "candidates": cands, "n_candidates": len(cands),
            "started_unknown": sum(unknown_by.values()), "started_unknown_by": dict(unknown_by),
            "started_with_errors": n["started_with_errors"],
            "volume_lt_min_at_d": n["volume_lt_min_at_d"],
            "outcome_closed_before_d": n["outcome_closed_before_d"],
            "markets": {"open": n["open_markets"], "in_pools": n["pool_markets"], "decided": n["decided_markets"],
                        "without_fills": n["unfilled_markets"], "multi_market_events": n["multi_market"]},
            "type_differs_from_final": n["type_differs_from_final"],
            "basis": dict(Counter(views[eid]["track"]["basis"] for eid in cands)),
            "metrics": m, "top_ids": top_ids, "track_ids": track_ids, "started_ids": started_ids,
            "heavy_ids": [eid for eid in cands if tr.is_heavy(finals_d[eid].get("flagged_pct"))],
            "top": [_pick(by_id[eid], ent_by[eid], views[eid], D, finals_d[eid]) for eid in top_ids],
            "track_top": [{"event_id": eid, "title": by_id[eid].get("title"),
                           "track_rate": views[eid]["track"]["rate"], "track_low": views[eid]["track"].get("low"),
                           "basis": views[eid]["track"]["basis"], "final_flagged_pct": finals_d[eid].get("flagged_pct"),
                           "heavy": tr.is_heavy(finals_d[eid].get("flagged_pct"))} for eid in track_ids],
            "started_events": [_pick(by_id[eid], ent_by[eid], views[eid], D, finals_d[eid]) for eid in started_ids],
        })
    t3 = time.time()
    c = counters.snapshot()
    n_windows = c.get("windows_computed", 0) + c.get("windows_error", 0) + c.get("windows_error_started", 0)
    reqs = lookup_requests() - cfg["requests_at_start"]
    info = {"seconds": {"phase1_reads": round(t1 - t0, 1), "phase2_started": round(t2 - t1, 1),
                        "phase3_rank": round(t3 - t2, 1)},
            "counters": c, "lookup_requests": reqs,
            "markets": len(order), "base_pairs": pairs, "base_market_pairs": market_pairs,
            "label_reads": dict(label_reads),
            "reveal_kinds": dict(reveal_kinds),
            "signal_key": cfg["signal_key"],
            "per_window": {"windows_signal_run": n_windows,
                           "phase2_s_per_window": round((t2 - t1) / n_windows, 3) if n_windows else None,
                           "signal_s_per_window": round(c.get("window_seconds", 0) / n_windows, 3) if n_windows else None,
                           "lookup_requests_per_window": round(reqs / n_windows, 2) if n_windows else None,
                           "note": "phase 2 wall time (fills loading included, %d workers) and the signal's own time "
                                   "(look-ups) per window that ran the signal; cached / decided / no-fills windows "
                                   "cost no look-up" % workers}}
    return out, info


BIASES = [
    "Pool = the closed scan's universe: markets with Gamma volume >= $100k at the END, resolved, not excluded, and "
    "an ok result; an event is judged on those of its markets only. At D some had far less volume "
    "(volume_lt_min_at_d counts the candidates all of whose pool markets were under $100k as of D; --volume-at-d "
    "drops such markets, the live pool's rule); markets that never reached $100k, or whose result is too_large / "
    "no_trades, are not in the pool at all, though the live list would have held and checked them.",
    "Near the end of the store's coverage the pool only holds events that closed by then (pool_horizon_days < 60): "
    "events still open when the store was built are missing from those days.",
    "The outcome of a pick at D is scored on the event's markets that closed AFTER D only, with the closed list's "
    "rule (closed_scan.choose_figure: the highest flagged capital among their ok results), so a market whose answer "
    "was already public at D never counts as a hit; an event with no such market is dropped that day "
    "(dropped.no_outcome_after_d). outcome_closed_before_d still counts candidates whose FINAL figure market had "
    "closed by D (the number the closed list shows), for comparison.",
    "Rotation is not replayed: the measured list is the ranking's top 10 (open_scan.rank_events), not the rotated "
    "list the live page shows (open_scan.rotate keeps at most 7 of yesterday's top 10, more only for started "
    "events, and tops up with yesterday's when too few new events exist).",
    "The price at D (>= 0.99 drops a market from the event's pool) is the median of the last 5 non-dust fills "
    "before D, not Gamma's outcomePrices at D, which are not kept historically; the volume at D (which orders the "
    "event's markets for its type) is Gamma's final volume scaled by the share of the fills' dollars before D.",
    "Days share events (an open market is a candidate on every replay day it is open), so the summed p-values treat "
    "dependent days as independent and are optimistic; read them with distinct_events.",
]

RULES = {
    "event_at_d": ("the event's kept markets the live pool would hold at D: open (opened <= D < closedTime), endDate "
                   "<= D + 60 days, a cached fills file, price at D < 0.99 (decided only when ALL of them were >= "
                   "0.99); started = open_scan.event_signal over open_scan.started_signal of each; end = the "
                   "earliest endDate among them (open_scan.event_end); type = the AI type of the highest-volume "
                   "(as of D) labelled one (open_scan.event_type); reveal = open_scan.event_reveal over their "
                   "labels as of D; track = the track record as of D for that type and the event's series"),
    "outcome": ("the event's final flagged_pct / flagged_capital: its highest-flagged kept market "
                "(closed_scan.choose_figure, the closed list's rule); heavy = flagged_pct >= 10%"),
    "failed_lookup": ("a window where a big buyer's prior look-up failed is unknown (status error; counted in "
                      "started_unknown), or started when the other wallets start it; never cached"),
    "ranking": "open_scan.rank_events (imported, not copied); rotation not replayed",
}


def projection(events, all_days, run_days, horizon_days, seconds, lookup_reqs):
    """A full-run estimate from a --limit-days sample: the sample's phase-2
    time and look-up requests scaled by the (market, day) pairs of all
    replay days over those of the days run (no fills read). Later days
    reuse wallets looked up before, so it leans high."""
    def pairs(days):
        return sum(len(open_markets(e, D, horizon_days)) for D in days for e in events)
    n_all, n_run = pairs(all_days), pairs(run_days)
    scale = n_all / n_run if n_run else None
    return {"market_day_pairs_all_days": n_all, "market_day_pairs_run": n_run, "scale": round(scale, 2) if scale else None,
            "phase2_s_run": seconds, "projected_phase2_s": round(seconds * scale) if scale else None,
            "lookup_requests_run": lookup_reqs,
            "projected_lookup_requests": round(lookup_reqs * scale) if scale else None,
            "note": "the sample's phase-2 time and look-up requests x (market, day) pairs of all replay days / of the "
                    "days run; leans high (later days reuse cached look-ups)"}


def build_report(conn, every_days=EVERY_DAYS, warmup_days=WARMUP_DAYS, horizon_days=HORIZON_DAYS,
                 window_hours=WINDOW_HOURS, top=TOP_N, variant=method.DEFAULT_VARIANT, min_series_n=tr.MIN_SERIES_N,
                 with_started=True, max_lookup_requests=None, limit_days=None, workers=WORKERS, fetch_gamma=True,
                 refetch_gamma=False, rank_fn=None, signal_fn=None, flag_fn=None, db_path=None, fetcher=None,
                 fills_loader=load_fills, volume_at_d=False):
    """The backtest.json document. rank_fn / signal_fn / flag_fn default to
    open_scan.rank_events / started_signal / MethodFlags (and big_buyers
    for the look-up prefetch); tests pass their own."""
    t0 = time.time()
    ensure_schema(conn)
    bigs_fn, max_rows = None, None
    if rank_fn is None or (with_started and (signal_fn is None or flag_fn is None)):
        osc = _open_scan()
        rank_fn = rank_fn or osc.rank_events
        if with_started and signal_fn is None:
            signal_fn, bigs_fn = osc.started_signal, osc.big_buyers
            max_rows = getattr(osc, "MAX_WINDOW_ROWS", None)
        if with_started and flag_fn is None:
            flag_fn = osc.MethodFlags(conn, variant, workers=PREFETCH_WORKERS)
    log("  loading evaluable events ...")
    events, counts = evaluable_events(conn, variant, fetch=fetch_gamma, refetch=refetch_gamma, fetcher=fetcher)
    closes = [e["closed_ts"] for e in events if e.get("closed_ts") is not None]
    first, last = (min(closes), max(closes)) if closes else (None, None)
    days = replay_days(first, last, every_days, warmup_days)
    all_days = list(days)
    if limit_days:
        days = days[:int(limit_days)]
    log(f"  {len(events):,} evaluable events ({counts.get('markets', 0):,} kept markets), closes "
        f"{day_str(first, False) if first else '-'} .. {day_str(last, False) if last else '-'}; {len(all_days)} replay "
        f"days" + (f" (first {len(days)} run: --limit-days)" if limit_days else ""))
    recs, info = replay(conn, events, days, rank_fn, signal_fn if with_started else None, flag_fn, variant,
                        horizon_days, window_hours, top, NEXT_N, min_series_n, with_started, max_lookup_requests,
                        workers, fills_loader=fills_loader, bigs_fn=bigs_fn, max_window_rows=max_rows,
                        volume_at_d=volume_at_d) \
        if days else ([], {"seconds": {}, "counters": {}})
    for d in recs:
        d["pool_horizon_days"] = round(min(float(horizon_days), (last - d["day_ts"]) / DAY), 1)
    summ = aggregate(recs, top, finals={e["event_id"]: e for e in events})
    unknown_by = Counter()
    for d in recs:
        unknown_by.update(d["started_unknown_by"])
    unknown = sum(unknown_by.values())
    started_err = sum(d["started_with_errors"] for d in recs)
    warn = []
    if summ["days"] < 10:
        warn.append(f"only {summ['days']} replay day(s)")
    if summ["distinct_events"]["pool"]["n"] < 50:
        warn.append(f"only {summ['distinct_events']['pool']['n']} distinct candidate events")
    n_started = summ["distinct_events"]["started"]["n"]
    if with_started and n_started < 10:
        warn.append(f"the started signal was on for only {n_started} distinct event(s), on "
                    f"{summ['days_with_started']} day(s)")
    if unknown:
        warn.append(f"{unknown} (event, day) started signals unknown (failed look-ups: {unknown_by.get('error', 0)}, "
                    f"look-up budget: {unknown_by.get('skipped_budget', 0)}, no fills cache: "
                    f"{unknown_by.get('no_fills', 0)}): counted as not started")
    if started_err:
        warn.append(f"{started_err} (event, day) started with some look-ups failed (started from the other wallets)")
    if with_started and limit_days and info.get("seconds"):
        info["projection"] = projection(events, all_days, days, horizon_days, info["seconds"].get("phase2_started"),
                                        info.get("lookup_requests"))
    rep = {
        "generated_at": iso(now_epoch()),
        "db": tr._rel(db_path) if db_path else None,
        "question": ("On past days, would the open list's ranking have picked markets that ended up with heavy "
                     "flagged money (flagged_pct >= 10%) more often than the pool average?"),
        "small_sample": "; ".join(warn) or None,
        "rules": RULES,
        "params": {"every_days": every_days, "warmup_days": warmup_days, "horizon_days": horizon_days,
                   "window_hours": window_hours, "top": top, "cutoff_variant": variant, "min_series_n": min_series_n,
                   "heavy_pct": HEAVY_PCT, "big_capital": BIG_CAPITAL, "decided_price": DECIDED_PRICE,
                   "with_started": with_started, "max_lookup_requests": max_lookup_requests,
                   "limit_days": limit_days, "volume_at_d": volume_at_d, "max_window_rows": max_rows},
        "coverage": {"evaluable_events": len(events), "event_counts": dict(counts),
                     "first_close": day_str(first, False) if first else None,
                     "last_close": day_str(last, False) if last else None,
                     "replay_days_possible": len(all_days), "replay_days_run": len(days),
                     "days": [day_str(D, False) for D in days]},
        "summary": {**summ, "started_unknown": unknown, "started_unknown_by": dict(unknown_by),
                    "started_with_errors": started_err},
        "biases": BIASES,
        "run": {**info, "total_seconds": round(time.time() - t0, 1), "requests": net.bucket_stats()},
        "days": [{k: v for k, v in d.items() if k not in ("candidates", "top_ids", "track_ids", "started_ids")}
                 for d in recs],
    }
    return rep


def write_report(rep, path=OUT_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, default=str)
    os.replace(tmp, path)
    return path


def _share(blk, key="pooled_heavy_share"):
    v = blk.get(key)
    return "-" if v is None else f"{v * 100:.1f}%"


def _num(v, fmt="{:,.2f}"):
    return "-" if v is None else fmt.format(v)


def print_summary(rep):
    s = rep["summary"]
    log(f"\nBacktest: {s['days']} replay days ({s['days_informative']} with more than {rep['params']['top']} "
        f"candidates), {s['candidates_total']:,} (event, day) candidates, "
        f"{s['distinct_events']['pool']['n']} distinct events; per day {s['candidates_per_day']}")
    if rep.get("small_sample"):
        log(f"  SMALL SAMPLE: {rep['small_sample']}")
    log(f"  {'set':<11} {'days':>4} {'k/n heavy':>11} {'pooled':>7} {'day-mean':>8} {'mean pct':>8} "
        f"{'mean $flag':>11} {'exp. random':>11} {'p(rand>=)':>9} {'distinct k/n':>12}")
    for name in ("pool", "top", "track_top", "started"):
        b = s[name]
        exp = b.get("expected_k_heavy_random")
        log(f"  {name:<11} {b['days']:>4} {b['k_heavy']:>5}/{b['n']:<5} {_share(b):>7} "
            f"{_share(b, 'day_mean_heavy_share'):>8} {_num(b['day_mean_flagged_pct']):>8} "
            f"{_num(b['day_mean_flagged_capital'], '{:,.0f}'):>11} {_num(exp):>11} "
            f"{_num(b.get('p_random_ge_summed'), '{:.3f}'):>9} "
            f"{'{}/{}'.format(s['distinct_events'][name]['k_heavy'], s['distinct_events'][name]['n']):>12}")
    for d in rep["days"]:
        m = d["metrics"]
        log(f"  {d['day']}: {d['n_candidates']:>4} cand. (pool {m['pool']['k_heavy']}/{m['pool']['n']} heavy), "
            f"top {m['top']['k_heavy']}/{m['top']['n']}, track-only {m['track_top']['k_heavy']}/{m['track_top']['n']}, "
            f"started {m['started']['k_heavy']}/{m['started']['n']}; unknown {d['started_unknown']}; "
            f"horizon {d['pool_horizon_days']} d")
    run = rep.get("run") or {}
    if run.get("per_window"):
        log(f"  per window: {run['per_window']}")
    if run.get("projection"):
        log(f"  projection for all {rep['coverage']['replay_days_possible']} replay days: {run['projection']}")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Replay the open ranking on past days (demo output 4).")
    ap.add_argument("--db", default=DB_PATH, help="the store (default data/watchlist/watchlist.db; must exist); "
                                                  "written to: the backtest_* tables and the wallet look-up cache")
    ap.add_argument("--every-days", type=int, default=EVERY_DAYS)
    ap.add_argument("--warmup-days", type=int, default=WARMUP_DAYS)
    ap.add_argument("--horizon-days", type=int, default=HORIZON_DAYS)
    ap.add_argument("--window-hours", type=float, default=WINDOW_HOURS)
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--variant", default=method.DEFAULT_VARIANT, choices=method.VARIANTS)
    ap.add_argument("--min-series-n", type=int, default=tr.MIN_SERIES_N)
    ap.add_argument("--limit-days", type=int, default=None, help="run only the first N replay days (a sample)")
    ap.add_argument("--max-lookup-requests", type=int, default=None,
                    help="stop starting new started-signal windows once this many wallet look-up requests were made "
                         "(a sample; the skipped windows are counted as unknown and computed by the next run)")
    ap.add_argument("--no-started", action="store_true", help="skip the started signal (no look-ups, no network)")
    ap.add_argument("--no-gamma", action="store_true", help="never call Gamma for missing market fields")
    ap.add_argument("--refetch-gamma", action="store_true")
    ap.add_argument("--volume-at-d", action="store_true",
                    help="also drop pool markets whose volume as of D was under $100k (the live pool's volume rule)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args(argv)
    if not os.path.isfile(args.db):      # never create an empty store by mistake: the backtest reads the closed scan's
        ap.error(f"--db {args.db}: no such store (the backtest reads an existing closed-scan store)")
    t0 = time.time()
    log(f"[backtest] {store.stamp()}  db {args.db}")
    conn = store.connect(args.db)
    if not args.no_started or not args.no_gamma:
        net.install()
    rep = build_report(conn, args.every_days, args.warmup_days, args.horizon_days, args.window_hours, args.top,
                       args.variant, args.min_series_n, with_started=not args.no_started,
                       max_lookup_requests=args.max_lookup_requests, limit_days=args.limit_days, workers=args.workers,
                       fetch_gamma=not args.no_gamma, refetch_gamma=args.refetch_gamma, db_path=args.db,
                       volume_at_d=args.volume_at_d)
    path = write_report(rep, args.out)
    print_summary(rep)
    log(f"\n[backtest] wrote {path} in {time.time() - t0:.1f} s")
    return rep


if __name__ == "__main__":
    main()
