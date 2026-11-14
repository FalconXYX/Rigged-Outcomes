"""
PARITY: does the one-call look-up reproduce the user's flags on the seven
published markets? Writes data/watchlist/demo/parity.json.

The watchlist scores whales with ONE /v2/user-volume call per whale instead
of scrape_insiders' paged /v2/activity crawl. That is only acceptable if it
gives the same is_first_x / Is_percent / is_suspicious as the committed
data/*_insiders.csv, or if every difference is understood and the new
answer is the right one. This module checks every committed whale row
(1,113 rows over 7 markets), not a sample.

Three answers are compared for each row, all on the row's own position
(market_position) and cutoff (target_trade_time, its first trade):

  committed  the row as committed. Six files are the v1 crawl of March 2026
             (newest 15,000 activity rows of any type, partial on errors)
             with 12 rows re-scored on v2 in November (RESCORED_V2);
             SuperbowlFirstSong was regenerated on v2 in November.
  one_call   method.flag_whale on /v2/user-volume for each cutoff variant
             (same_day: the first-trade UTC day included; day_before: up to
             the day before). Answers are cached in the store.
  truth      polymarket_api.fetch_user_history_up_to, the paged v2 crawl
             scrape_insiders uses today, scored exactly as
             scrape_insiders.analyze_whale scores it (unique transaction
             hashes at or before the cutoff, usdcSize summed). One crawl per
             wallet up to the end of its first-trade UTC day (+6 h) gives
             every cut point from the same rows: before that day, at the
             exact cutoff, and to the end of that day.

Each disagreement between committed and one_call is decided against truth
("which side was right") and explained by walking from the one-call answer
to the truth one step at a time:

  user-volume answer -> the crawl at the same whole-day boundary (counting
  fills)                                   flip = "uv_vs_activity"
  -> the crawl at the exact cutoff (fills) flip = "day_rounding"
  -> the crawl at the exact cutoff, unique transactions (= truth)
                                           flip = "fills_vs_tx"

and, when the committed side is wrong, by comparing its prior_bet_count and
lifetime_portfolio with the crawl at the same cutoff ("v1_undercount" etc.).
For an under-count that moves a flag, the v1 crawl's 15,000-row window is
tested directly (v1_window_probe): how many activity rows of any type the
wallet had between the cutoff and the March 2026 run (V1_RUN_TS), and how
many prior trades the rest of the window held, i.e. what v1 would have
counted had its walk not stopped early.

A checked sample (>= 20 disagreements, spread over markets and causes) is
then re-fetched with the exact call scrape_insiders makes
(fetch_user_history_up_to(wallet, first_trade)), plus a live run of the
"exact hybrid" (user-volume to the day before + /v2/activity for the
first-trade day up to the cutoff), to confirm the derived numbers.

Findings of the 2026-11-10 run (the numbers are in parity.json):
  - user-volume IS the activity feed in whole UTC days: trade_count equals
    the number of TRADE fills (not unique transactions) up to the end of
    the end bound's day on 1,055 of 1,098 uncapped rows; the rest differ by
    a few fills on very busy wallets, plus one small wallet where
    user-volume holds 2 trades that neither /v2/activity nor /v2/trades
    serves. Counting fills instead of transactions never moved a flag.
  - The variants' errors are all day rounding. same_day counts the rest of
    the first-trade day (typically the whale's own position-building) and
    only ever MISSES flags; day_before drops that day up to and including
    the first trade and only ever ADDS flags. day_before is far closer.
  - target_trade_time is the UTC epoch (a trade in the market at exactly
    that second for 1,113 of 1,113 rows); eoa_address equals user_id on
    every row that has one.
  - 18 committed v1 rows still carry the v1 bug (flags the v2 crawl
    contradicts); in all 18 the v1 walk stopped early, not at its window.

Resumable: the crawls are cached in data/watchlist/cache/parity_truth.jsonl
and parity_sample.jsonl (one line per wallet / sampled row), the user-volume
answers in the store. Run it again with the same flags to continue; the
report is rewritten at the end of every run, with truth_complete false
while crawls are still pending.

  python3 -m watchlist.parity [--budget-seconds 480] [--markets usiran,fired]
"""
import argparse
import json
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import pandas as pd

import polymarket_api
from scrape_insiders import PERCENT_VOLUME, wallet_for, whale_selection, whale_threshold

from watchlist import method, net, store
from watchlist.common import CACHE_DIR, DAY, DEMO_DIR, ROOT, Budget, iso, log, now_epoch, to_epoch, utc_day

MARKETS_FILE = os.path.join(ROOT, "markets.json")
OUT_PATH = os.path.join(DEMO_DIR, "parity.json")
TRUTH_CACHE = os.path.join(CACHE_DIR, "parity_truth.jsonl")
SAMPLE_CACHE = os.path.join(CACHE_DIR, "parity_sample.jsonl")
V1_PROBE_CACHE = os.path.join(CACHE_DIR, "parity_v1probe.jsonl")

VARIANTS = method.CUTOFF_VARIANTS
# (our key, the committed CSV's column)
FLAGS = (("is_first_x", "is_first_x"), ("is_percent", "Is_percent"), ("flagged", "is_suspicious"))
FLAG_KEYS = tuple(k for k, _ in FLAGS)

# The v1 scraper (scrape_insiders before November 2026) read /activity?user=
# newest first, 500 rows a page, all activity types, and stopped at offset
# 15 000; the committed v1 files were written by the run squashed into commit
# 95de7d3 (2026-03-24 13:33 -04:00). Rows newer than the cutoff counted
# against that window, so this bounds what the run could have seen.
V1_WINDOW_ROWS = 15_000
V1_RUN_TS = 1_774_373_609          # 2026-03-24T17:33:29Z
# Rows the November v2 re-score replaced (a0a68af, "provable v1 artifacts":
# 0 prior trades for wallets with thousands), keyed (output, user_id,
# target_trade_time). SuperbowlFirstSong was regenerated whole on v2.
V2_FILES = {"SuperbowlFirstSong"}
RESCORED_V2 = {
    ("KristiNoemFiredMarch31", "0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b", "2026-03-05T16:41:49+00:00"),
    ("MaduroOut", "0xe2314b08a84feff5787d6a7b7984b7e334ef4e3e", "2026-01-03T10:53:57+00:00"),
    ("MaduroOut", "0x43440ab002eaac9fede6f9d21bea96d84228f90d", "2026-01-03T12:12:25+00:00"),
    ("OpenAIBrowser2025", "0x9d4aea6f0609c6a77070c573d78679ba4d136cb1", "2025-09-05T06:21:52+00:00"),
    ("SupremeCourtTariffRuling", "0xb51b3beffd13cde071ed799e70a7b0b598e8b4d9", "2026-01-20T14:27:16+00:00"),
    ("SupremeCourtTariffRuling", "0x9cb98fc57fe8a3bdf75c3b48bd419e5d262f7b80", "2026-01-20T15:17:12+00:00"),
    ("SupremeCourtTariffRuling", "0xa82b0e89cf734e2fa70cbd52ca65c77d64595220", "2026-01-20T20:04:36+00:00"),
    ("SupremeCourtTariffRuling", "0x01ed5c64d1d8905afd7917d75a5652665f5cca5f", "2026-02-20T15:05:36+00:00"),
    ("USxIranStrikesFeb28", "0x95b7a5c9a62c9b1a3e1bd7d6826e6aa493ffd633", "2026-01-26T05:30:18+00:00"),
    ("USxIranStrikesFeb28", "0x8f42ae0a01c0383c7ca8bd060b86a645ee74b88f", "2026-02-02T08:26:56+00:00"),
    ("USxIranStrikesFeb28", "0x2a019dc0089ea8c6edbbafc8a7cc9ba77b4b6397", "2026-02-28T06:13:55+00:00"),
    ("USxIranStrikesFeb28", "0xe0dbdb7f005f233f4510e4ef7e53a2f76a8df44e", "2026-02-28T06:49:59+00:00"),
}
# Crawl past the end of the first-trade day so an ET-wall-clock label
# (UTC = label + 4 h or + 5 h) would still find its trade.
CRAWL_MARGIN = 6 * 3600
ET_SHIFTS = (4 * 3600, 5 * 3600)
SAMPLE_MIN = 30
CAPPED_ROWS = polymarket_api.HISTORY_MAX_PAGES * polymarket_api.PAGE_SIZE


# ── pure pieces (unit-tested) ────────────────────────────────────────────────

def as_bool(v):
    """The committed CSVs' True/False cells (str, bool or numpy bool) as bool."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no", ""):
            return False
        raise ValueError(f"not a boolean: {v!r}")
    return bool(v)


def flags(position, count, usdc):
    """{is_first_x, is_percent, flagged} (+ concentration) for a prior
    count and dollar volume: scrape_insiders.analyze_whale's rule, via
    method.flag_whale."""
    f = method.flag_whale(position, count, usdc)
    return {"is_first_x": f["is_first_x"], "is_percent": f["is_percent"], "flagged": f["flagged"],
            "concentration": round(f["concentration"], 2)}


def _agg(hist):
    return {"rows": len(hist), "tx": len({h.get("transactionHash", "") for h in hist}),
            "usdc": round(sum(float(h.get("usdcSize") or 0) for h in hist), 4),
            "size": round(sum(float(h.get("size") or 0) for h in hist), 4)}


def window_stats(hist, first_ts, condition_id):
    """Every cut point of one whale from its crawled history (v1-shaped TRADE
    rows: timestamp, usdcSize, size, transactionHash, conditionId):

      before  rows before the first-trade UTC day   (what day_before sees)
      cut     rows at or before the first trade     (what scrape_insiders sees)
      day     rows to the end of the first-trade day (what same_day sees)

    each as {rows (fills), tx (unique transaction hashes), usdc, size}, plus
    `market`: this market's rows around the cutoff, for the timestamp
    check (a row at exactly first_ts means the label is the UTC epoch)."""
    first_ts = int(first_ts)
    d0 = utc_day(first_ts) * DAY
    d1 = d0 + DAY - 1
    cid = str(condition_id or "").lower()
    ts_of = [(int(h.get("timestamp") or 0), h) for h in hist]
    before = [h for t, h in ts_of if t < d0]
    cut = [h for t, h in ts_of if t <= first_ts]
    day = [h for t, h in ts_of if t <= d1]
    mk = [(t, h) for t, h in ts_of if str(h.get("conditionId") or "").lower() == cid]
    at = [t for t, _ in mk if t == first_ts]
    shifted = {str(s // 3600): sum(1 for t, _ in mk if t == first_ts + s) for s in ET_SHIFTS}
    after_cut_day = [h for t, h in ts_of if first_ts < t <= d1]
    return {
        "first_ts": first_ts,
        "day_start": d0,
        "before": _agg(before),
        "cut": _agg(cut),
        "day": _agg(day),
        "same_day_before_cut": _agg([h for t, h in ts_of if d0 <= t <= first_ts]),
        "same_day_after_cut": _agg(after_cut_day),
        "same_day_after_cut_this_market": sum(1 for t, _ in mk if first_ts < t <= d1),
        "market": {"rows_at_first_ts": len(at),
                   "rows_at_or_before_cut": sum(1 for t, _ in mk if t <= first_ts),
                   "earliest_ts": min((t for t, _ in mk), default=None),
                   "rows_at_et_shift_h": shifted},
    }


def truth_determined(stats, position, capped):
    """Whether the crawl decides the flags. An uncapped crawl always does; a
    capped one (only the newest HISTORY_MAX_PAGES pages) gives floors, which
    decide them only when they already rule both flags out."""
    if not capped:
        return True
    c = stats["cut"]
    return c["tx"] > method.FIRST_X_TRADES and c["usdc"] > float(position) * 100.0 / PERCENT_VOLUME


def truth_chain(position, stats, variant):
    """The steps from a one-call answer to the truth, from the crawl alone:
    W = the variant's whole-day window counting fills (what user-volume
    should answer), X = the exact cutoff counting fills, T = the exact
    cutoff counting unique transactions (scrape_insiders = truth)."""
    win = stats["day"] if variant == "same_day" else stats["before"]
    c = stats["cut"]
    return {"W": flags(position, win["rows"], win["usdc"]),
            "X": flags(position, c["rows"], c["usdc"]),
            "T": flags(position, c["tx"], c["usdc"])}


def money(x):
    return f"${float(x):,.0f}"


def explain(flag, row, variant):
    """Decide one committed-vs-one_call disagreement on `flag` against the
    truth and say why the wrong side is wrong. Returns {right, cause,
    detail}; right is "one_call", "committed" or "unverified"."""
    c = row["committed"][flag]
    v = row["one_call"][variant][flag]
    st = row.get("truth")
    if not st or not row.get("truth_determined"):
        return {"right": "unverified", "cause": "no_truth",
                "detail": "the v2 crawl is missing or capped without deciding the flags"}
    pos = row["position"]
    ch = truth_chain(pos, st, variant)
    t = ch["T"][flag]
    uv = row["uv"][variant]
    if v == t:
        com = row["committed"]
        tc, tu = st["cut"]["tx"], st["cut"]["usdc"]
        src = row["committed_source"]
        if com["prior_bet_count"] < tc:
            cause = "v1_undercount" if src == "v1" else "committed_v2_undercount"
        elif com["prior_bet_count"] > tc:
            cause = "v1_overcount" if src == "v1" else "committed_v2_overcount"
        elif abs(com["lifetime_portfolio"] - max(tu, pos)) > 1.0:
            cause = "v1_volume" if src == "v1" else "committed_v2_volume"
        else:
            cause = "committed_other"
        detail = (f"committed ({src}) {com['prior_bet_count']} prior trades / {money(com['lifetime_portfolio'])} "
                  f"lifetime; v2 crawl at the same cutoff {tc} transactions ({st['cut']['rows']} fills) / "
                  f"{money(max(tu, pos))} lifetime")
        if row.get("v1_window"):
            detail += f"; {row['v1_window']['verdict']}"
        return {"right": "one_call", "cause": cause, "detail": detail}
    # the one-call side is wrong: find the step(s) that flip the flag
    steps = [("uv_vs_activity", v, ch["W"][flag]), ("day_rounding", ch["W"][flag], ch["X"][flag]),
             ("fills_vs_tx", ch["X"][flag], t)]
    flips = [name for name, a, b in steps if a != b]
    cause = "+".join(flips) or "unexplained"
    bits = [f"user-volume {variant}: {uv['trade_count']} fills / {money(uv['volume_usdc'])}"]
    if "uv_vs_activity" in flips:
        win = st["day"] if variant == "same_day" else st["before"]
        bits.append(f"the crawl at the same day boundary has {win['rows']} fills / {money(win['usdc'])}")
    if "day_rounding" in flips:
        if variant == "same_day":
            a = st["same_day_after_cut"]
            bits.append(f"includes {a['rows']} fills ({money(a['usdc'])}) after the first trade on its UTC day, "
                        f"{st['same_day_after_cut_this_market']} of them in this market")
        else:
            b = st["same_day_before_cut"]
            bits.append(f"drops {b['rows']} fills ({money(b['usdc'])}) of the first-trade day up to and including "
                        f"the first trade")
    if "fills_vs_tx" in flips:
        bits.append(f"at the exact cutoff {st['cut']['rows']} fills are {st['cut']['tx']} transactions")
    bits.append(f"truth (v2 crawl at the cutoff): {st['cut']['tx']} transactions / "
                f"{money(max(st['cut']['usdc'], pos))} lifetime")
    return {"right": "committed", "cause": cause, "detail": "; ".join(bits)}


def agreement(pairs):
    """[(a_flags, b_flags)] -> {flag: {n, agree, pct, a_only, b_only}}:
    a_only = a says True and b False (a flags more), b_only the reverse."""
    out = {}
    for f in FLAG_KEYS:
        n = agree = a_only = b_only = 0
        for a, b in pairs:
            n += 1
            if a[f] == b[f]:
                agree += 1
            elif a[f]:
                a_only += 1
            else:
                b_only += 1
        out[f] = {"n": n, "agree": agree, "pct": round(agree / n * 100, 2) if n else None,
                  "a_only": a_only, "b_only": b_only}
    return out


def flagged_pct(rows, get):
    """Flagged capital / whale capital x 100 over rows, with get(row) -> the
    flags dict to use (None rows are skipped). Returns (pct, n)."""
    cap = fcap = 0.0
    n = 0
    for r in rows:
        f = get(r)
        if f is None:
            continue
        n += 1
        cap += r["position"]
        if f["flagged"]:
            fcap += r["position"]
    return (round(fcap / cap * 100, 2) if cap > 0 else None), n


def pick_sample(items, n_min=SAMPLE_MIN, key=lambda it: it["id"]):
    """Spread a sample over markets and causes: items are dicts with
    "market" and "cause"; round-robin over markets, and within a market
    over causes, until n_min are picked (all of them if fewer). An item
    whose key(item) (the row id) is already picked is skipped, so a row
    that disagrees under several causes is sampled once."""
    by_market = {}
    for it in items:
        by_market.setdefault(it["market"], {}).setdefault(it["cause"], []).append(it)
    picked, keys = [], set()

    def take(m):
        """Pick one item of market m, from the cause it has given least."""
        causes = by_market[m]
        order = sorted(causes, key=lambda c: (sum(1 for p in picked if p["market"] == m and p["cause"] == c), c))
        for c in order:
            while causes[c]:
                it = causes[c].pop(0)
                if key(it) not in keys:
                    picked.append(it)
                    keys.add(key(it))
                    return True
        return False

    while len(picked) < n_min:
        progress = False
        for m in sorted(by_market):
            if len(picked) >= n_min:
                break
            progress = take(m) or progress
        if not progress:
            break
    return picked


def v1_window_verdict(probe, committed_count):
    """Why the v1 crawl under-counted, from a v1_window_probe result.

    The v1 fetcher read /activity newest first, 500 rows a page, any type,
    and stopped at offset 15 000; it also stopped silently, returning what
    it had, after three 429s in a row or on any exception. So:
      never_reached  >= 15 000 rows newer than the cutoff: the window ended
                     before the cutoff (v1 would count 0)
      window         the committed count is what the window could hold
                     (>= 90 % of predicted_v1_count, the unique TRADE
                     transactions among the window's rows before the cutoff)
      stopped_early  the window held more prior trades than v1 counted: the
                     walk ended early (429s / an error)
    The run time is bounded by the commit (V1_RUN_TS); a run a few hours
    earlier leaves a slightly larger window, so `window` is a floor."""
    n_after, capped = probe["rows_after_cut_to_run"], probe["probe_capped"]
    pred = probe.get("predicted_v1_count")
    if capped or n_after >= V1_WINDOW_ROWS:
        return {"explained_by": "never_reached",
                "verdict": f">= {V1_WINDOW_ROWS:,} activity rows between the cutoff and the March run: the v1 "
                           f"crawl's {V1_WINDOW_ROWS:,}-row window never reached the cutoff"}
    if pred is None:
        return {"explained_by": "unknown", "verdict": "window not probed"}
    left = V1_WINDOW_ROWS - n_after
    if committed_count >= 0.9 * pred:
        return {"explained_by": "window",
                "verdict": f"{n_after:,} activity rows between the cutoff and the March run left the v1 window "
                           f"{left:,} rows before the cutoff, holding ~{pred:,} prior trades (committed "
                           f"{committed_count:,}): the 15,000-row window explains it"}
    return {"explained_by": "stopped_early",
            "verdict": f"the v1 window ({left:,} rows before the cutoff after {n_after:,} newer ones) held ~{pred:,} "
                       f"prior trades but v1 counted {committed_count:,}: its walk ended early (the v1 fetcher "
                       f"returned what it had after three 429s in a row or an error)"}


def read_jsonl(path, key):
    """{key(record): record} from a JSON-lines cache; the last line wins and
    a torn last line (interrupted write) is ignored."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            out[key(rec)] = rec
    return out


_APPEND_LOCK = threading.Lock()


def append_jsonl(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(rec, separators=(",", ":"), default=str)
    with _APPEND_LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


# ── inputs ───────────────────────────────────────────────────────────────────

def load_rows(names=None, markets_file=MARKETS_FILE, data_dir=None):
    """Every committed _insiders.csv row of the markets.json entries (or of
    the entries named in `names`), joined to its clean row. Returns
    (rows, inputs): inputs describes each market (whale rule counts)."""
    data_dir = data_dir or os.path.join(ROOT, "data")
    with open(markets_file) as f:
        markets = json.load(f)
    rows, inputs = [], []
    for m in markets:
        if names and m["name"] not in names:
            continue
        out = m["output"]
        ins = pd.read_csv(os.path.join(data_dir, f"{out}_insiders.csv"))
        clean = pd.read_csv(os.path.join(data_dir, f"{out}_clean.csv"))
        threshold = whale_threshold(clean["dollar_amount"].sum())
        winner = m.get("correct_outcome")
        if winner:
            clean["is_winner"] = clean["outcome"].astype(str).str.lower() == winner.lower()
        else:
            clean["is_winner"] = clean["win_status"] == "WIN"
        whale_mask, sweeps, exits = whale_selection(clean, threshold)
        highlighted = {str(u).lower() for u in (m.get("highlightedUsers") or {})}
        index = {}
        for i, (u, t) in enumerate(zip(clean["user_id"].astype(str).str.lower(), clean["first_trade"].astype(str))):
            index.setdefault((u, t), []).append(i)
        used = set()
        n_unmatched = 0
        for i, r in ins.iterrows():
            uid = str(r["user_id"]).strip().lower()
            t = str(r["target_trade_time"])
            pos = float(r["market_position"])
            cands = index.get((uid, t), [])
            ci = next((c for c in cands if abs(float(clean.iloc[c]["dollar_amount"]) - pos) < 0.01 and c not in used),
                      cands[0] if cands else None)
            crow = clean.iloc[ci] if ci is not None else None
            if ci is None:
                n_unmatched += 1
            else:
                used.add(ci)
            src = "v2" if out in V2_FILES or (out, uid, t) in RESCORED_V2 else "v1"
            rows.append({
                "id": f"{out}:{i}",
                "market": m["name"],
                "output": out,
                "condition_id": str(crow["market_id"]) if crow is not None else None,
                "user_id": uid,
                "wallet": wallet_for(crow) if crow is not None else uid,
                "eoa_column": crow is not None and "eoa_address" in crow.index,
                "eoa_differs": bool(crow is not None and "eoa_address" in crow.index
                                    and isinstance(crow["eoa_address"], str)
                                    and crow["eoa_address"].strip().lower() != uid),
                "position": pos,
                "first_trade": t,
                "first_ts": to_epoch(t),
                "side": str(crow["side"]) if crow is not None else None,
                "avg_odds": float(crow["avg_odds"]) if crow is not None else None,
                "current_rule": bool(whale_mask.iloc[ci]) if ci is not None else False,
                "sweep": bool(sweeps.iloc[ci]) if ci is not None else False,
                "exit": bool(exits.iloc[ci]) if ci is not None else False,
                "highlighted": uid in highlighted,
                "committed_source": src,
                "committed": {"prior_bet_count": int(r["prior_bet_count"]),
                              "lifetime_portfolio": float(r["lifetime_portfolio"]),
                              "concentration": float(r["portfolio_concentration"]),
                              "is_first_x": as_bool(r["is_first_x"]), "is_percent": as_bool(r["Is_percent"]),
                              "flagged": as_bool(r["is_suspicious"])},
            })
        missing = [int(c) for c in range(len(clean)) if bool(whale_mask.iloc[c]) and c not in used]
        inputs.append({
            "market": m["name"], "output": out, "condition_id": str(clean["market_id"].iloc[0]),
            "title": str(clean["market_title"].iloc[0]), "correct_outcome": winner,
            "committed_rows": int(len(ins)), "threshold": round(float(threshold), 2),
            "committed_source": "v2 crawl (regenerated 2026-11-05)" if out in V2_FILES else
                                "v1 crawl (March 2026) + rows re-scored on v2 2026-11-05",
            "rows_rescored_v2": sum(1 for k in RESCORED_V2 if k[0] == out),
            "rows_not_matched_to_clean": n_unmatched,
            "rows_current_rule": sum(1 for r in rows if r["output"] == out and r["current_rule"]),
            "rows_sell": sum(1 for r in rows if r["output"] == out and r["exit"]),
            "rows_near_certain": sum(1 for r in rows if r["output"] == out and r["sweep"]),
            "rows_highlighted_only": sum(1 for r in rows if r["output"] == out and r["highlighted"]
                                         and not (r["current_rule"] or r["sweep"] or r["exit"])),
            "current_rule_whales_missing_from_committed": len(missing),
        })
    return rows, inputs


# ── live steps ───────────────────────────────────────────────────────────────

def run_one_call(rows, conn, workers=10):
    """Attach row["uv"][variant] and row["one_call"][variant] for both
    variants (one /v2/user-volume call per wallet and UTC day, cached)."""
    for v in VARIANTS:
        pairs = [(r["wallet"], method.lookup_end(r["first_ts"], v)) for r in rows]
        t = time.time()
        answers = net.user_volumes(pairs, conn=conn, workers=workers)
        fetched = sum(1 for a in answers if not a["cached"])
        log(f"  user-volume {v}: {len(answers)} rows ({fetched} fetched, {len(answers) - fetched} cached) "
            f"in {time.time() - t:.1f}s")
        for r, a in zip(rows, answers):
            r.setdefault("uv", {})[v] = {"trade_count": a["trade_count"], "volume_usdc": round(a["volume_usdc"], 4),
                                         "volume": round(a["volume"], 4), "day": a["day"],
                                         "end": method.lookup_end(r["first_ts"], v)}
            r.setdefault("one_call", {})[v] = flags(r["position"], a["trade_count"], a["volume_usdc"])


def _wallet_jobs(rows):
    """{wallet: {"until", "cuts": [(first_ts, condition_id)]}} for the truth crawl."""
    jobs = {}
    for r in rows:
        j = jobs.setdefault(r["wallet"], {"until": 0, "cuts": set()})
        j["until"] = max(j["until"], utc_day(r["first_ts"]) * DAY + DAY - 1 + CRAWL_MARGIN)
        j["cuts"].add((r["first_ts"], r["condition_id"]))
    return jobs


def _cut_key(first_ts, cid):
    return f"{int(first_ts)}|{str(cid).lower()}"


def crawl_wallet(wallet, until, cuts):
    """One fetch_user_history_up_to crawl -> the cache record for the wallet."""
    t = time.time()
    hist = polymarket_api.fetch_user_history_up_to(wallet, until)
    capped = len(hist) >= CAPPED_ROWS
    return {"wallet": wallet, "until": until, "n_hist": len(hist), "capped": capped,
            "oldest_ts": min((h["timestamp"] for h in hist), default=None),
            "fetched_at": now_epoch(), "secs": round(time.time() - t, 2),
            "cuts": {_cut_key(ft, cid): window_stats(hist, ft, cid) for ft, cid in sorted(cuts)}}


def run_truth(rows, budget, workers=4, priority=None):
    """The v2 crawl for every wallet (resumable via TRUTH_CACHE). Attaches
    row["truth"] (window_stats) and row["truth_determined"]. Returns the
    number of wallets still pending when the budget ran out."""
    cache = read_jsonl(TRUTH_CACHE, key=lambda r: r["wallet"])
    jobs = _wallet_jobs(rows)
    todo = []
    for w, j in jobs.items():
        rec = cache.get(w)
        if rec and rec["until"] >= j["until"] and all(_cut_key(*c) in rec["cuts"] for c in j["cuts"]):
            continue
        todo.append(w)
    prio = priority or {}
    todo.sort(key=lambda w: (prio.get(w, 1), w))
    log(f"  v2 crawl: {len(jobs) - len(todo)} wallet(s) cached, {len(todo)} to fetch")
    done = 0
    t0 = time.time()
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending, it = {}, iter(todo)
            while True:
                while len(pending) < workers and not budget.expired(reserve=45):
                    w = next(it, None)
                    if w is None:
                        break
                    pending[pool.submit(crawl_wallet, w, jobs[w]["until"], jobs[w]["cuts"])] = w
                if not pending:
                    break
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in finished:
                    w = pending.pop(fut)
                    rec = fut.result()   # an API error raises: never a silent empty history
                    append_jsonl(TRUTH_CACHE, rec)
                    cache[w] = rec
                    done += 1
                    if done % 50 == 0:
                        log(f"    {done}/{len(todo)} wallets crawled ({time.time() - t0:.0f}s, "
                            f"{net.bucket_stats().get('data_activity', {})})")
    left = sum(1 for w in jobs if w not in cache or not all(_cut_key(*c) in cache[w]["cuts"] for c in jobs[w]["cuts"]))
    for r in rows:
        rec = cache.get(r["wallet"])
        st = rec["cuts"].get(_cut_key(r["first_ts"], r["condition_id"])) if rec else None
        r["truth"] = st
        r["truth_capped"] = bool(rec and rec["capped"])
        r["truth_determined"] = bool(st) and truth_determined(st, r["position"], rec["capped"])
        if st:
            r["truth_flags"] = flags(r["position"], st["cut"]["tx"], st["cut"]["usdc"])
            r["exact_fills_flags"] = flags(r["position"], st["cut"]["rows"], st["cut"]["usdc"])
    log(f"  v2 crawl: {done} fetched this run, {left} wallet(s) pending")
    return left


def hybrid_prior(wallet, first_ts, conn=None):
    """The exact cutoff from two calls: /v2/user-volume up to the day before
    the first trade + /v2/activity (TRADE) from that day's midnight to the
    first trade. trade_count counts fills, like user-volume."""
    uv = net.user_volume(wallet, int(first_ts) - DAY, conn=conn)
    d0 = utc_day(first_ts) * DAY
    raw, capped = polymarket_api.v2_walk("activity", {"user": wallet, "type": "TRADE", "start": d0,
                                                      "end": int(first_ts), "limit": polymarket_api.PAGE_SIZE},
                                         max_pages=20, announce=False)
    day = [polymarket_api.as_v1(r) for r in raw if (r.get("type") or "TRADE") == "TRADE"]
    return {"trade_count": uv["trade_count"] + len(day),
            "volume_usdc": round(uv["volume_usdc"] + sum(h["usdcSize"] for h in day), 4),
            "day_rows": len(day), "day_tx": len({h["transactionHash"] for h in day}), "capped": capped,
            "requests": 1 + max(1, -(-len(raw) // polymarket_api.PAGE_SIZE))}


def v1_window_probe(wallet, first_ts, pages=16):
    """What the v1 crawl's 15 000-row window could hold for this cutoff:
    the activity rows (any type; the feed leaves deposits/withdrawals out by
    default) newer than the cutoff up to the March run, and, in the rest of
    the window, the unique TRADE transactions at or before the cutoff
    (predicted_v1_count). At most `pages` + 15 requests."""
    w, ft = str(wallet).lower(), int(first_ts)
    after, capped = polymarket_api.v2_walk("activity", {"user": w, "start": ft + 1, "end": V1_RUN_TS,
                                                        "limit": polymarket_api.PAGE_SIZE},
                                           max_pages=pages, announce=False)
    out = {"rows_after_cut_to_run": len(after), "probe_capped": capped, "predicted_v1_count": None,
           "window_rows_before_cut": None}
    left = V1_WINDOW_ROWS - len(after)
    if capped or left <= 0:
        out.update(predicted_v1_count=0, window_rows_before_cut=0)
        return out
    before, _ = polymarket_api.v2_walk("activity", {"user": w, "start": 1, "end": ft,
                                                    "limit": polymarket_api.PAGE_SIZE},
                                       max_pages=-(-left // polymarket_api.PAGE_SIZE), announce=False)
    window = before[:left]
    out["window_rows_before_cut"] = len(window)
    out["predicted_v1_count"] = len({r.get("transaction_hash") or "" for r in window
                                     if (r.get("type") or "") == "TRADE"})
    return out


def check_sample_row(row, conn=None):
    """The direct checks on one sampled disagreement: scrape_insiders' exact
    call, the live hybrid and (for a committed under-count) the v1 window."""
    t = time.time()
    hist = polymarket_api.fetch_user_history_up_to(row["wallet"], row["first_ts"])
    direct = {"transactions": len({h["transactionHash"] for h in hist}), "fills": len(hist),
              "usdc": round(sum(h["usdcSize"] for h in hist), 4), "capped": len(hist) >= CAPPED_ROWS}
    direct["flags"] = flags(row["position"], direct["transactions"], direct["usdc"])
    rec = {"id": row["id"], "wallet": row["wallet"], "first_ts": row["first_ts"], "direct": direct,
           "hybrid": hybrid_prior(row["wallet"], row["first_ts"], conn=conn)}
    rec["hybrid"]["flags"] = flags(row["position"], rec["hybrid"]["trade_count"], rec["hybrid"]["volume_usdc"])
    rec["secs"] = round(time.time() - t, 2)
    return rec


def v1_artifacts(rows):
    """Rows whose committed (v1) flags the v2 crawl contradicts because the
    v1 crawl saw fewer prior trades: the rows still carrying the v1 bug."""
    out = []
    for r in rows:
        if r["committed_source"] != "v1" or not r.get("truth_determined"):
            continue
        if r["committed"]["prior_bet_count"] >= r["truth"]["cut"]["tx"]:
            continue
        if any(r["committed"][f] != r["truth_flags"][f] for f in FLAG_KEYS):
            out.append(r)
    return out


def run_v1_probes(rows, budget, workers=4):
    """v1_window_probe for every v1 artifact row (cached in V1_PROBE_CACHE);
    attaches row["v1_window"] with the verdict."""
    cache = read_jsonl(V1_PROBE_CACHE, key=lambda r: r["id"])
    targets = v1_artifacts(rows)
    todo = [r for r in targets if r["id"] not in cache or "predicted_v1_count" not in cache[r["id"]]]
    if todo and not budget.expired(reserve=30):
        log(f"  v1 window probes: {len(targets)} artifact rows, {len(todo)} to fetch")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [(r, pool.submit(v1_window_probe, r["wallet"], r["first_ts"])) for r in todo]
            for r, fut in futs:
                rec = {"id": r["id"], **fut.result(), "fetched_at": now_epoch()}
                append_jsonl(V1_PROBE_CACHE, rec)
                cache[r["id"]] = rec
    for r in targets:
        rec = cache.get(r["id"])
        if rec and "predicted_v1_count" in rec:
            probe = {k: rec[k] for k in ("rows_after_cut_to_run", "probe_capped", "window_rows_before_cut",
                                         "predicted_v1_count")}
            r["v1_window"] = {**probe, **v1_window_verdict(probe, r["committed"]["prior_bet_count"])}
    return targets


# ── report ───────────────────────────────────────────────────────────────────

def _disagreements(rows, variant):
    """Every committed-vs-one_call disagreement of a variant, explained."""
    out = []
    for r in rows:
        for f in FLAG_KEYS:
            if r["committed"][f] != r["one_call"][variant][f]:
                e = explain(f, r, variant)
                out.append({"id": r["id"], "market": r["market"], "flag": f, "variant": variant,
                            "committed": r["committed"][f], "one_call": r["one_call"][variant][f],
                            "current_rule": r["current_rule"], "committed_source": r["committed_source"], **e})
    return out


def _tally(items):
    by = {}
    for d in items:
        k = f"{d['right']}:{d['cause']}"
        b = by.setdefault(k, {"right": d["right"], "cause": d["cause"], "count": 0, "by_flag": {}, "by_market": {},
                              "current_rule": 0, "examples": []})
        b["count"] += 1
        b["by_flag"][d["flag"]] = b["by_flag"].get(d["flag"], 0) + 1
        b["by_market"][d["market"]] = b["by_market"].get(d["market"], 0) + 1
        b["current_rule"] += int(d["current_rule"])
        if len(b["examples"]) < 3:
            b["examples"].append({"id": d["id"], "flag": d["flag"], "detail": d["detail"]})
    return sorted(by.values(), key=lambda b: -b["count"])


CAUSE_TEXT = {
    "v1_undercount": "The committed v1 crawl saw fewer prior trades than exist at the same cutoff. The v1 fetcher "
                     "read /activity newest first and stopped at offset 15,000, and it also stopped silently, "
                     "returning what it had, after three 429s in a row or any error. hypotheses.v1_undercount "
                     "tests which of the two it was for every row whose flags it moved. The one-call answer agrees "
                     "with the v2 crawl.",
    "v1_overcount": "The committed v1 count is HIGHER than the v2 crawl at the same cutoff.",
    "v1_volume": "Same trade count, different dollar volume in the committed v1 crawl.",
    "day_rounding": "user-volume floors its bounds to whole UTC days. same_day counts the whole first-trade day, "
                    "including the fills after the first trade (usually the whale's own position-building); "
                    "day_before drops that day, including the first trade itself.",
    "fills_vs_tx": "user-volume's trade_count counts fills (activity rows); scrape_insiders counts unique "
                   "transaction hashes, and one transaction can carry several fills.",
    "uv_vs_activity": "user-volume's answer differs from the /v2/activity crawl at the same whole-day boundary.",
}


def _per_market_agreement(rows, get_a, get_b):
    out = {}
    for m in sorted({r["market"] for r in rows}):
        pairs = [(get_a(r), get_b(r)) for r in rows if r["market"] == m and get_a(r) and get_b(r)]
        out[m] = agreement(pairs)
    return out


def _agree_block(rows, get_a, get_b):
    pairs = [(get_a(r), get_b(r)) for r in rows if get_a(r) and get_b(r)]
    cur = [(get_a(r), get_b(r)) for r in rows if r["current_rule"] and get_a(r) and get_b(r)]
    return {"all_rows": agreement(pairs), "current_rule_whales": agreement(cur),
            "per_market": _per_market_agreement(rows, get_a, get_b)}


def _hypotheses(rows):
    """The evidence for each hypothesis the parity step was asked to test."""
    tr = [r for r in rows if r.get("truth") and not r["truth_capped"]]
    # user-volume vs the crawl at the same whole-day boundary
    uv_match = {}
    for v in VARIANTS:
        key = "day" if v == "same_day" else "before"
        ok_c = sum(1 for r in tr if r["uv"][v]["trade_count"] == r["truth"][key]["rows"])
        ok_u = sum(1 for r in tr if abs(r["uv"][v]["volume_usdc"] - r["truth"][key]["usdc"]) <= 0.01 + 1e-6
                   * r["truth"][key]["usdc"])
        ok_tx = sum(1 for r in tr if r["uv"][v]["trade_count"] == r["truth"][key]["tx"])
        bad = [r for r in tr if r["uv"][v]["trade_count"] != r["truth"][key]["rows"]]
        diffs = [r["uv"][v]["trade_count"] - r["truth"][key]["rows"] for r in bad]
        moved = [r for r in tr if r.get("truth_determined")
                 and r["one_call"][v]["flagged"] != truth_chain(r["position"], r["truth"], v)["W"]["flagged"]]
        mism = [{"id": r["id"], "uv_trade_count": r["uv"][v]["trade_count"], "crawl_fills": r["truth"][key]["rows"],
                 "crawl_tx": r["truth"][key]["tx"], "uv_usdc": r["uv"][v]["volume_usdc"],
                 "crawl_usdc": r["truth"][key]["usdc"]}
                for r in sorted(bad, key=lambda r: -abs(r["uv"][v]["trade_count"] - r["truth"][key]["rows"])
                                / max(1, r["truth"][key]["rows"]))[:8]]
        uv_match[v] = {"rows_compared": len(tr), "trade_count_equals_fills": ok_c, "trade_count_equals_tx": ok_tx,
                       "volume_usdc_equals_crawl": ok_u,
                       "mismatches": {"uv_higher": sum(1 for d in diffs if d > 0),
                                      "uv_lower": sum(1 for d in diffs if d < 0),
                                      "median_abs_diff_fills": sorted(abs(d) for d in diffs)[len(diffs) // 2]
                                      if diffs else None,
                                      "rows_where_it_moves_flagged": [r["id"] for r in moved]},
                       "largest_relative_mismatches": mism}
    # fills vs transactions at the exact cutoff
    multi = [r for r in tr if r["truth"]["cut"]["rows"] != r["truth"]["cut"]["tx"]]
    flips = [r for r in rows if r.get("truth_determined") and r.get("exact_fills_flags")
             and r["exact_fills_flags"]["is_first_x"] != r["truth_flags"]["is_first_x"]]
    # committed vs truth on counts, by source
    by_src = {}
    for r in rows:
        if not r.get("truth") or r["truth_capped"]:
            continue
        s = by_src.setdefault(r["committed_source"], {"rows": 0, "count_equal": 0, "count_lower": 0,
                                                      "count_higher": 0, "lower_by_median": None, "_d": []})
        s["rows"] += 1
        d = r["committed"]["prior_bet_count"] - r["truth"]["cut"]["tx"]
        if d == 0:
            s["count_equal"] += 1
        elif d < 0:
            s["count_lower"] += 1
            s["_d"].append(-d)
        else:
            s["count_higher"] += 1
    for s in by_src.values():
        ds = sorted(s.pop("_d"))
        s["lower_by_median"] = ds[len(ds) // 2] if ds else None
    # timestamp convention
    with_truth = [r for r in rows if r.get("truth")]
    exact = sum(1 for r in with_truth if r["truth"]["market"]["rows_at_first_ts"] > 0)
    no_exact = [r for r in with_truth if r["truth"]["market"]["rows_at_first_ts"] == 0]
    et_hits = sum(1 for r in no_exact if any(r["truth"]["market"]["rows_at_et_shift_h"].values()))
    # eoa vs proxy
    eoa_rows = [r for r in rows if r["eoa_column"]]
    eoa_diff = sum(1 for r in rows if r["eoa_differs"])
    # the v1 rows whose flags the v2 crawl contradicts, with the window probe
    arts = v1_artifacts(rows)
    art_rows = [{"id": r["id"], "market": r["market"], "wallet": r["wallet"], "position": r["position"],
                 "first_trade": r["first_trade"], "current_rule": r["current_rule"], "side": r["side"],
                 "avg_odds": r["avg_odds"],
                 "committed": {k: r["committed"][k] for k in ("prior_bet_count", "lifetime_portfolio", *FLAG_KEYS)},
                 "v2_crawl": {"transactions": r["truth"]["cut"]["tx"], "usdc": r["truth"]["cut"]["usdc"],
                              "capped": r["truth_capped"], **{f: r["truth_flags"][f] for f in FLAG_KEYS}},
                 "one_call": {v: {f: r["one_call"][v][f] for f in FLAG_KEYS} for v in VARIANTS},
                 "v1_window": r.get("v1_window")}
                for r in sorted(arts, key=lambda r: -r["position"])]
    cap_verdicts = {}
    for a in art_rows:
        k = (a["v1_window"] or {}).get("explained_by", "not_probed")
        cap_verdicts[k] = cap_verdicts.get(k, 0) + 1
    return {
        "v1_undercount": {
            "question": "Is the committed v1 data under-counted (fewer prior trades than exist)?",
            "committed_vs_v2_crawl_at_the_same_cutoff": by_src,
            "rows_whose_flags_the_v2_crawl_contradicts": len(art_rows),
            "of_them_current_rule_whales": sum(1 for a in art_rows if a["current_rule"]),
            "flagged_capital_wrongly_flagged": round(sum(
                a["position"] for a in art_rows if a["committed"]["flagged"] and not a["v2_crawl"]["flagged"]), 2),
            "v1_window_explains": cap_verdicts,
            "rows": art_rows,
        },
        "eoa_vs_proxy": {
            "question": "Did the committed crawl and the one-call look-up query different addresses (EOA vs proxy)?",
            "rows_with_eoa_column": len(eoa_rows),
            "rows_where_eoa_differs_from_user_id": eoa_diff,
            "files_with_eoa_column": sorted({r["output"] for r in eoa_rows}),
        },
        "day_rounding": {
            "question": "Does user-volume's whole-UTC-day rounding move the flags?",
            "user_volume_matches_the_crawl_at_the_day_boundary": uv_match,
            "rows_with_fills_after_the_cutoff_on_the_first_trade_day":
                sum(1 for r in tr if r["truth"]["same_day_after_cut"]["rows"] > 0),
            "rows_with_fills_before_the_cutoff_on_the_first_trade_day":
                sum(1 for r in tr if r["truth"]["same_day_before_cut"]["rows"] > 0),
            "rows_compared": len(tr),
        },
        "fills_vs_tx": {
            "question": "Does trade_count count fills where the old prior_bet_count counted unique transactions?",
            "rows_where_fills_differ_from_tx_at_the_cutoff": len(multi),
            "rows_compared": len(tr),
            "is_first_x_flips_from_fills_alone": len(flips),
            "flip_examples": [{"id": r["id"], "fills": r["truth"]["cut"]["rows"], "tx": r["truth"]["cut"]["tx"]}
                              for r in flips[:10]],
        },
        "timestamp_convention": {
            "question": "Is target_trade_time the UTC epoch, or an ET wall clock labelled +00:00?",
            "rows_checked": len(with_truth),
            "rows_with_a_trade_in_this_market_at_exactly_target_trade_time": exact,
            "rows_without_one": len(no_exact),
            "of_those_with_a_trade_4h_or_5h_later": et_hits,
            "examples_without_exact_match": [{"id": r["id"], "first_trade": r["first_trade"],
                                              "earliest_market_ts": iso(r["truth"]["market"]["earliest_ts"])}
                                             for r in no_exact[:10]],
        },
    }


def build_report(rows, inputs, sample, pending, run_notes):
    have_truth = [r for r in rows if r.get("truth_determined")]
    agreement_out = {"committed_vs_one_call": {}, "one_call_vs_truth": {}, "committed_vs_truth": None,
                     "exact_fills_vs_truth": None}
    for v in VARIANTS:
        agreement_out["committed_vs_one_call"][v] = _agree_block(rows, lambda r: r["committed"],
                                                                 lambda r, v=v: r["one_call"][v])
        agreement_out["one_call_vs_truth"][v] = _agree_block(have_truth, lambda r, v=v: r["one_call"][v],
                                                             lambda r: r["truth_flags"])
    agreement_out["committed_vs_truth"] = _agree_block(have_truth, lambda r: r["committed"], lambda r: r["truth_flags"])
    agreement_out["exact_fills_vs_truth"] = _agree_block(have_truth, lambda r: r["exact_fills_flags"],
                                                         lambda r: r["truth_flags"])
    agreement_out["note"] = ("a_only = the first-named side flags and the second does not; b_only the reverse. "
                             "current_rule_whales = rows scrape_insiders' current whale rule keeps (winning-outcome "
                             "BUY >= threshold, avg price < 0.99); the committed v1 files also hold SELL rows, "
                             "near-certain buys and highlighted users, which the method no longer scores.")

    disagreements = {}
    for v in VARIANTS:
        items = _disagreements(rows, v)
        rows_any = len({d["id"] for d in items})
        disagreements[v] = {
            "rows_with_any_flag_disagreement": rows_any,
            "flag_disagreements": len(items),
            "one_call_right": sum(1 for d in items if d["right"] == "one_call"),
            "committed_right": sum(1 for d in items if d["right"] == "committed"),
            "unverified": sum(1 for d in items if d["right"] == "unverified"),
            "categories": _tally(items),
            "all": items,
        }

    # flagged capital per market, as the closed list would rank it
    capital = {}
    for m in [i["market"] for i in inputs]:
        mr = [r for r in rows if r["market"] == m]
        block = {}
        scopes = (("current_rule_whales", lambda r: r["current_rule"]), ("all_committed_rows", lambda r: True))
        for scope, sel in scopes:
            rr = [r for r in mr if sel(r)]
            rt = [r for r in rr if r.get("truth_determined")]
            block[scope] = {
                "rows": len(rr),
                "committed": flagged_pct(rr, lambda r: r["committed"])[0],
                "truth": flagged_pct(rt, lambda r: r["truth_flags"])[0] if len(rt) == len(rr) else None,
                "same_day": flagged_pct(rr, lambda r: r["one_call"]["same_day"])[0],
                "day_before": flagged_pct(rr, lambda r: r["one_call"]["day_before"])[0],
                "exact_hybrid": flagged_pct(rt, lambda r: r["exact_fills_flags"])[0] if len(rt) == len(rr) else None,
            }
        capital[m] = block

    hyp = _hypotheses(rows)
    rec = _recommend(agreement_out, capital, disagreements)
    rec["errors_on_current_rule_whales"] = {v: error_profile(have_truth, v) for v in VARIANTS}
    return {
        "generated_at": iso(now_epoch()),
        "truth_complete": pending == 0 and all(r.get("truth") for r in rows),
        "wallets_pending": pending,
        "what": ("The user's method with the one-call /v2/user-volume look-up, both cutoff variants, against the "
                 "committed data/*_insiders.csv on every committed whale row, each disagreement decided against "
                 "the paged /v2/activity crawl (polymarket_api.fetch_user_history_up_to, scored as "
                 "scrape_insiders.analyze_whale scores it)."),
        "definitions": {
            "committed": "the committed _insiders.csv row (is_first_x / Is_percent / is_suspicious)",
            "one_call": "method.flag_whale on /v2/user-volume?user=<wallet>&start=1&end=E, E per variant",
            "same_day": "E = the first trade: the endpoint includes that whole UTC day",
            "day_before": "E = the first trade - 86400: up to the end of the previous UTC day",
            "truth": "the v2 crawl at the exact cutoff, unique transaction hashes, usdcSize summed (scrape_insiders "
                     "today)",
            "exact_hybrid": "user-volume to the day before + /v2/activity for the first-trade day up to the cutoff "
                            "(counts fills); computed from the crawl for every row and run live on the sample",
            "inputs_per_row": "position = market_position, cutoff = target_trade_time, wallet = "
                              "scrape_insiders.wallet_for(the matching clean row)",
        },
        "inputs": {"markets": inputs, "rows": len(rows),
                   "rows_with_truth": sum(1 for r in rows if r.get("truth")),
                   "rows_truth_determined": len(have_truth),
                   "rows_truth_capped": sum(1 for r in rows if r.get("truth_capped"))},
        "recommendation": rec,
        "agreement": agreement_out,
        "flagged_pct_by_market": capital,
        "disagreements": disagreements,
        "cause_explanations": CAUSE_TEXT,
        "hypotheses": hyp,
        "checked_sample_summary": _sample_summary(sample),
        "checked_sample": sample,
        "run": run_notes,
        "rows": [_compact(r) for r in rows],
    }


def _compact(r):
    out = {k: r.get(k) for k in ("id", "market", "user_id", "position", "first_trade", "side", "avg_odds",
                                 "current_rule", "highlighted", "committed_source", "committed")}
    out["one_call"] = {v: {**r["uv"][v], **{f: r["one_call"][v][f] for f in FLAG_KEYS},
                           "concentration": r["one_call"][v]["concentration"]} for v in VARIANTS}
    if r.get("truth"):
        st = r["truth"]
        out["truth"] = {"tx": st["cut"]["tx"], "fills": st["cut"]["rows"], "usdc": st["cut"]["usdc"],
                        "before_day_fills": st["before"]["rows"], "day_fills": st["day"]["rows"],
                        "same_day_fills_after_cut": st["same_day_after_cut"]["rows"],
                        "same_day_fills_before_cut": st["same_day_before_cut"]["rows"],
                        "market_rows_at_first_ts": st["market"]["rows_at_first_ts"],
                        "capped": r["truth_capped"], "determined": r["truth_determined"]}
        if r.get("truth_flags"):
            out["truth"].update({f: r["truth_flags"][f] for f in FLAG_KEYS})
    return out


def _recommend(agreement_out, capital, disagreements):
    """The default variant: the one closer to the truth on `flagged`, over
    the whales the method scores (current rule), with the numbers."""
    score = {}
    for v in VARIANTS:
        cur = agreement_out["one_call_vs_truth"][v]["current_rule_whales"]["flagged"]
        allr = agreement_out["one_call_vs_truth"][v]["all_rows"]["flagged"]
        errs = [abs(b["current_rule_whales"][v] - b["current_rule_whales"]["truth"]) for b in capital.values()
                if b["current_rule_whales"]["truth"] is not None and b["current_rule_whales"][v] is not None]
        score[v] = {
            "flagged_agreement_with_truth_current_rule_pct": cur["pct"],
            "false_flags": cur["a_only"], "missed_flags": cur["b_only"],
            "flagged_agreement_with_truth_all_rows_pct": allr["pct"],
            "flagged_pct_mean_abs_error_points": round(sum(errs) / len(errs), 2) if errs else None,
            "flagged_pct_max_abs_error_points": round(max(errs), 2) if errs else None,
        }
    if any(s["flagged_agreement_with_truth_current_rule_pct"] is None for s in score.values()):
        return {"default": None, "why": ["truth incomplete: run again to finish the crawl"], "scores": score}
    best = max(VARIANTS, key=lambda v: (score[v]["flagged_agreement_with_truth_current_rule_pct"],
                                        -(score[v]["flagged_pct_mean_abs_error_points"] or 0)))
    other = [v for v in VARIANTS if v != best][0]
    hyb = agreement_out["exact_fills_vs_truth"]["current_rule_whales"]["flagged"]
    b, o = score[best], score[other]
    n = agreement_out["one_call_vs_truth"][best]["current_rule_whales"]["flagged"]["n"]
    why = [
        f"On the {n} whales the method scores (current rule), {best} agrees with the exact-cutoff crawl on `flagged` "
        f"{b['flagged_agreement_with_truth_current_rule_pct']}% of the time, {other} "
        f"{o['flagged_agreement_with_truth_current_rule_pct']}%.",
        f"{best}: {b['false_flags']} false flag(s), {b['missed_flags']} missed; {other}: {o['false_flags']} false, "
        f"{o['missed_flags']} missed.",
        f"Per-market flagged_pct error against the crawl: {best} mean {b['flagged_pct_mean_abs_error_points']} / max "
        f"{b['flagged_pct_max_abs_error_points']} points, {other} mean {o['flagged_pct_mean_abs_error_points']} / max "
        f"{o['flagged_pct_max_abs_error_points']} points.",
        f"The exact hybrid (user-volume to the day before + one /v2/activity page for the first-trade day) agrees "
        f"{hyb['pct']}% ({hyb['a_only']} false, {hyb['b_only']} missed) at one extra request per whale.",
    ]
    return {"default": best, "why": why, "scores": score,
            "exact_hybrid": {"flagged_agreement_with_truth_current_rule_pct": hyb["pct"],
                             "false_flags": hyb["a_only"], "missed_flags": hyb["b_only"]}}


def error_profile(rows, variant):
    """The variant's `flagged` errors against the truth on current-rule
    whales, each with the numbers that explain it, plus counts by pattern:
      missed_day_one_builder  truth: <= 10 prior trades at the first trade;
                              the rest of that UTC day pushes user-volume
                              over 10 (a wallet building its position in
                              many fills on day one)
      missed_same_day_volume  truth: concentrated; the rest of the day's
                              volume dilutes it below 80 %
      false_day_start         day_before sees too few trades / too little
                              volume because the wallet's trading up to the
                              first trade happened on that same UTC day
                              (the first trade itself included)"""
    out, counts = [], {}
    for r in rows:
        if not r["current_rule"] or not r.get("truth_determined"):
            continue
        t, o = r["truth_flags"], r["one_call"][variant]
        if o["flagged"] == t["flagged"]:
            continue
        st = r["truth"]
        if variant == "same_day" and not o["flagged"]:
            kind = "missed_day_one_builder" if t["is_first_x"] else "missed_same_day_volume"
        elif variant == "day_before" and o["flagged"]:
            kind = "false_day_start"
        else:
            kind = "other"
        counts[kind] = counts.get(kind, 0) + 1
        out.append({"id": r["id"], "kind": kind, "position": round(r["position"], 2),
                    "truth": {"transactions": st["cut"]["tx"], "usdc": st["cut"]["usdc"], "flagged": t["flagged"]},
                    "one_call": {"trade_count": r["uv"][variant]["trade_count"],
                                 "volume_usdc": r["uv"][variant]["volume_usdc"], "flagged": o["flagged"]},
                    "first_day_fills_up_to_cut": st["same_day_before_cut"]["rows"],
                    "first_day_fills_after_cut": st["same_day_after_cut"]["rows"],
                    "of_which_in_this_market": st["same_day_after_cut_this_market"]})
    out.sort(key=lambda e: -e["position"])
    return {"count": len(out), "by_kind": counts,
            "capital": round(sum(e["position"] for e in out), 2), "rows": out}


def _sample_summary(sample):
    with_check = [s for s in sample if s.get("direct_check")]
    verdicts = {}
    for s in sample:
        for v, items in s["verdicts"].items():
            for it in items:
                k = f"{v}:{it['right']}"
                verdicts[k] = verdicts.get(k, 0) + 1
    return {"rows": len(sample), "markets": sorted({s["market"] for s in sample}),
            "picked_for": sorted({s["picked_for"] for s in sample if s.get("picked_for")}),
            "direct_checked": len(with_check),
            "direct_flags_equal_crawl_truth": sum(1 for s in with_check if s["direct_flags_equal_truth"]),
            "direct_numbers_equal_crawl": sum(1 for s in with_check if s["direct_numbers_equal_crawl"]),
            "direct_or_crawl_capped": sum(1 for s in with_check if s["direct_or_crawl_capped"]),
            "hybrid_live_flags_equal_truth": sum(1 for s in with_check if s["hybrid_flags_equal_truth"]),
            "flag_verdicts": verdicts}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Parity of the one-call look-up against the committed insiders CSVs.")
    ap.add_argument("--budget-seconds", type=float, default=None, help="stop fetching after this long (resumable)")
    ap.add_argument("--markets", default=None, help="comma-separated markets.json names (default: all)")
    ap.add_argument("--workers", type=int, default=10, help="threads for user-volume")
    ap.add_argument("--crawl-workers", type=int, default=4, help="threads for the /v2/activity crawls")
    ap.add_argument("--sample", type=int, default=SAMPLE_MIN, help="disagreements to re-check directly")
    ap.add_argument("--no-truth", action="store_true", help="skip the crawls (report from the cache only)")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args(argv)
    budget = Budget(args.budget_seconds)
    t0 = time.time()
    net.install()
    conn = store.default_conn()
    names = set(args.markets.split(",")) if args.markets else None

    rows, inputs = load_rows(names)
    log(f"parity: {len(rows)} committed whale rows over {len(inputs)} markets")
    run_one_call(rows, conn, workers=args.workers)

    # crawl wallets in a disagreement first, so a short budget covers them
    prio = {}
    for r in rows:
        if any(r["committed"][f] != r["one_call"][v][f] for v in VARIANTS for f in FLAG_KEYS):
            prio[r["wallet"]] = 0
    if args.no_truth:
        budget = Budget(0)
    pending = run_truth(rows, budget, workers=args.crawl_workers, priority=prio)
    if not pending:
        run_v1_probes(rows, budget, workers=args.crawl_workers)

    # the checked sample: spread over markets and over (variant, cause);
    # a row is sampled once even when it disagrees under both variants
    sample_cache = read_jsonl(SAMPLE_CACHE, key=lambda r: r["id"])
    by_id = {r["id"]: r for r in rows}
    items, seen = [], set()
    for v in VARIANTS:
        for d in _disagreements(rows, v):
            if d["right"] != "unverified" and (d["id"], v) not in seen:
                seen.add((d["id"], v))
                items.append({**d, "cause": f"{v}:{d['right']}:{d['cause']}"})
    picked = pick_sample(items, n_min=args.sample) if not pending else []
    to_check = [it for it in picked if it["id"] not in sample_cache]
    if to_check and not budget.expired(reserve=30):
        log(f"  checked sample: {len(picked)} rows, {len(to_check)} to fetch")
        with ThreadPoolExecutor(max_workers=args.crawl_workers) as pool:
            futs = [pool.submit(check_sample_row, by_id[it["id"]], conn) for it in to_check]
            for fut in futs:
                rec = fut.result()
                append_jsonl(SAMPLE_CACHE, rec)
                sample_cache[rec["id"]] = rec
    sample = [_sample_entry(by_id[it["id"]], sample_cache.get(it["id"]), it["cause"]) for it in picked]

    notes = {"seconds": round(time.time() - t0, 1), "requests": net.bucket_stats(),
             "budget_seconds": args.budget_seconds}
    report = build_report(rows, inputs, sample, pending, notes)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = f"{args.out}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    os.replace(tmp, args.out)
    log(f"parity: wrote {args.out} (truth_complete={report['truth_complete']}, pending={pending}) "
        f"in {time.time() - t0:.0f}s")
    _print_summary(report)
    return report


def _sample_entry(r, rec, picked_for=None):
    """One checked-sample row: every number, both variants, which side was
    right on each disagreeing flag, and the direct re-checks: scrape_insiders'
    exact call (fetch_user_history_up_to(wallet, first_trade)) and the live
    hybrid."""
    st = r.get("truth") or {}
    entry = {"id": r["id"], "market": r["market"], "picked_for": picked_for, "wallet": r["wallet"],
             "position": r["position"], "first_trade": r["first_trade"], "side": r["side"],
             "avg_odds": r["avg_odds"], "current_rule": r["current_rule"],
             "committed_source": r["committed_source"], "committed": r["committed"],
             "one_call": {v: {**r["uv"][v], **r["one_call"][v]} for v in VARIANTS},
             "crawl": {k: st.get(k) for k in ("before", "same_day_before_cut", "cut", "same_day_after_cut", "day")}
                      if st else None,
             "truth": r.get("truth_flags"),
             "verdicts": {v: [{"flag": f, **explain(f, r, v)} for f in FLAG_KEYS
                              if r["committed"][f] != r["one_call"][v][f]] for v in VARIANTS}}
    if rec:
        d = rec["direct"]
        entry["direct_check"] = d
        entry["direct_flags_equal_truth"] = bool(r.get("truth_flags")) and all(
            d["flags"][f] == r["truth_flags"][f] for f in FLAG_KEYS)
        entry["direct_numbers_equal_crawl"] = bool(st) and (
            d["transactions"] == st["cut"]["tx"] and abs(d["usdc"] - st["cut"]["usdc"]) < 0.01)
        entry["direct_or_crawl_capped"] = bool(d["capped"] or r.get("truth_capped"))
        entry["hybrid_live"] = rec["hybrid"]
        entry["hybrid_flags_equal_truth"] = bool(r.get("truth_flags")) and all(
            rec["hybrid"]["flags"][f] == r["truth_flags"][f] for f in FLAG_KEYS)
    if r.get("v1_window"):
        entry["v1_window"] = r["v1_window"]
    return entry


def _print_summary(rep):
    log("\nagreement with the committed CSVs (flagged / first_x / percent), all rows:")
    for v in VARIANTS:
        a = rep["agreement"]["committed_vs_one_call"][v]["all_rows"]
        log(f"  {v:10s} " + "  ".join(f"{f} {a[f]['agree']}/{a[f]['n']} ({a[f]['pct']}%)" for f in FLAG_KEYS))
    if rep["inputs"]["rows_truth_determined"]:
        log("agreement with the v2 crawl (truth), current-rule whales:")
        for name, blk in [(v, rep["agreement"]["one_call_vs_truth"][v]) for v in VARIANTS] + \
                         [("committed", rep["agreement"]["committed_vs_truth"]),
                          ("hybrid", rep["agreement"]["exact_fills_vs_truth"])]:
            a = blk["current_rule_whales"]
            log(f"  {name:10s} " + "  ".join(f"{f} {a[f]['agree']}/{a[f]['n']} ({a[f]['pct']}%)" for f in FLAG_KEYS))
    for v in VARIANTS:
        d = rep["disagreements"][v]
        log(f"{v}: {d['flag_disagreements']} flag disagreements on {d['rows_with_any_flag_disagreement']} rows; "
            f"one_call right {d['one_call_right']}, committed right {d['committed_right']}, "
            f"unverified {d['unverified']}")
        for c in d["categories"][:8]:
            log(f"    {c['count']:4d}  {c['right']}:{c['cause']}  {c['by_flag']}")
    rec = rep["recommendation"]
    log(f"recommended default cutoff variant: {rec['default']}")
    for line in rec.get("why", []):
        log(f"  - {line}")


if __name__ == "__main__":
    main()
