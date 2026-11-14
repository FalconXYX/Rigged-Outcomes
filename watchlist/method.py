"""
THE USER'S METHOD on one resolved market: scrape_insiders.score_market's
whale set, scored with prior history from /v2/user-volume (one or two calls
per whale, plus one /v2/activity page where those cannot decide) instead
of the paged /v2/activity crawl of the whole history.

What is reused, not reimplemented (so the whale set is exactly theirs):
  polymarket_api.fetch_trades + polymarket_trades.to_trade_rows / save_csv
      -> the raw CSV (fetch_market_trades' two steps, called directly so
         the walk's `capped` flag is not lost: fetch_market_trades drops it)
  cleandata.main                                    -> the clean CSV
  scrape_insiders.whale_threshold                   -> clamp(0.1 % of the
      clean file's dollar total, $1k, $5k), as score_market computes it
  scrape_insiders.check_correct_outcome / whale_selection(...)[0]
      -> winning-outcome BUY positions >= the threshold, near-certain
         buys (avg price >= 0.99) left out
  scrape_insiders.wallet_for, FIRST_X_TRADES (10), PERCENT_VOLUME (80)

Per whale, the prior history at its first trade, then, as in
scrape_insiders.analyze_whale:
  is_first_x    = trade_count <= 10
  concentration = position / max(volume_usdc, position) x 100
  is_percent    = concentration >= 80
  flagged       = is_first_x or is_percent

cutoff_variant (default "exact"):
  same_day    ONE user-volume(wallet, start=1, end=first trade): the
              endpoint includes that whole UTC day, so it also counts the
              rest of the day after the first trade (often the whale's own
              position-building).
  day_before  ONE user-volume call with end = first trade - 1 day: nothing
              from the first-trade day, not even the fills before the trade.
  exact       the history at the exact cutoff (first trade, inclusive, as
              scrape_insiders' crawl cuts it), counting fills. Both one-call
              answers are fetched (cached per wallet and day); counting fills,
                day_before <= exact <= same_day   (trade_count and volume_usdc)
              and each flag only switches one way as those grow, so the
              parity run's finding holds for every whale: flagged under
              same_day => flagged at the exact cutoff => flagged under
              day_before (likewise is_first_x and is_percent). Where the two
              answers agree on is_first_x AND is_percent, that IS the exact
              answer (decided_by "agree"); only where they disagree (or are
              not ordered, which the argument needs) is the exact prior
              fetched: user-volume to the day before + the wallet's TRADE
              fills from 00:00 UTC of that day up to the first trade, one
              /v2/activity page (net.exact_prior; decided_by "exact_check").
              On "agree" rows the flags are exact but the prior numbers are
              the day_before answer (a lower bound; prior_counts_exact false,
              both bounds in prior_bounds).

Timing. The "price jump" is the first trade on the WINNING outcome at a
price >= 0.90 after which no trade on it goes below 0.80 (trades in the
same second are taken together; fills under JUMP_MIN_USD, $10, are
ignored as dust). A whale is "before the jump" when its
first trade is strictly earlier. When the price never jumps on trades (the
market resolved without trading up first), the close time stands in and
reveal_basis says so.

Files: data/watchlist/cache/<conditionId>_clean.csv (small: one row per
position) and <conditionId>.csv.gz (the raw CSV, gzip'd after cleaning:
~10x smaller, since a backfill keeps hundreds of them). A closed market's
trades never change, so both are reused on the next run (refetch=True
re-downloads). A market with more than max_rows fills is not analysed
(status "too_large") instead of being held in memory (every row is a dict,
~2-3 KB each), and neither is one whose fill walk was capped for any
reason (the walk's row cap, which counts CLOB-operator rows that are
dropped after it, or polymarket_api's page safety net): a truncated
download would give a wrong whale set.

Errors: any API failure raises (a whale is never scored on a failed look-up,
the rule scrape_insiders adopted after the v1 incident); scanners catch and
record. Statuses returned: ok, too_large, no_trades, unresolved.

CLI (smoke test / one-off; --verify-exact N re-crawls a sample of N whales
with fetch_user_history_up_to and writes data/watchlist/demo/exact_verify.json):
  python3 -m watchlist.method --slug <market-slug> [--variant exact|same_day|day_before|both|all]
      [--clean-csv data/X_clean.csv --raw-csv data/X.csv] [--verify-exact 20]
"""
import csv
import gzip
import io
import json
import os
import shutil
import threading
import time
from collections import Counter

import pandas as pd

import cleandata
import polymarket_api
import polymarket_trades
from scrape_insiders import (FIRST_X_TRADES, NEAR_CERTAIN_ODDS, PERCENT_VOLUME, check_correct_outcome, wallet_for,
                             whale_selection, whale_threshold)

from watchlist import filters, net, store
from watchlist.common import CACHE_DIR, DAY, DEMO_DIR, day_str, iso, log, now_epoch, to_epoch, utc_day

# The one-call variants (one user-volume call per whale; lookup_end knows
# them). CUTOFF_VARIANTS keeps its old meaning for parity / closed_scan,
# which iterate it as "the one-call variants"; VARIANTS is everything
# analyze_market accepts.
ONE_CALL_VARIANTS = ("same_day", "day_before")
CUTOFF_VARIANTS = ONE_CALL_VARIANTS
VARIANTS = ("exact",) + ONE_CALL_VARIANTS
DEFAULT_VARIANT = "exact"
# day_before <= same_day is checked with this slack on dollars (sums of
# rounded fills); beyond it the bracket is not trusted.
USD_TOLERANCE = 0.01
EXACT_WORKERS = 4       # /v2/activity is the tightest bucket (8 req/s at 40 %)
MAX_ROWS = 200_000
JUMP_HIGH = 0.90
JUMP_FLOOR = 0.80
# Fills smaller than this are ignored for the jump: on the smoke-test market
# one $0.81 BUY at 0.689, six weeks after the price had settled above 0.90,
# moved the "stayed >= 0.80" jump by those six weeks.
JUMP_MIN_USD = 10.0
WORKERS = 10


# ── pure pieces (unit-tested) ────────────────────────────────────────────────

def lookup_end(first_trade_ts, cutoff_variant):
    """The /v2/user-volume end bound for a whale's first trade (one-call
    variants; "exact" uses both)."""
    if cutoff_variant == "same_day":
        return int(first_trade_ts)
    if cutoff_variant == "day_before":
        return int(first_trade_ts) - DAY
    raise ValueError(f"cutoff_variant must be one of {ONE_CALL_VARIANTS}, not {cutoff_variant!r}")


def flag_whale(position, trade_count, volume_usdc):
    """scrape_insiders.analyze_whale's flags from one user-volume answer."""
    position = float(position)
    lifetime = max(float(volume_usdc or 0), position)
    concentration = (position / lifetime) * 100 if lifetime > 0 else 100.0
    is_first_x = int(trade_count or 0) <= FIRST_X_TRADES
    is_percent = concentration >= PERCENT_VOLUME
    return {"lifetime_portfolio": lifetime, "concentration": concentration,
            "is_first_x": is_first_x, "is_percent": is_percent, "flagged": is_first_x or is_percent}


def bracket(position, before, same_day):
    """Whether the two one-call answers decide the exact cutoff's flags.

    before / same_day: {"trade_count", "volume_usdc"} to the end of the day
    before the first trade / of the first-trade day. Counting fills, the
    exact cutoff lies between them on both numbers, and is_first_x (count
    <= 10) and is_percent (position / max(usdc, position) >= 80 %) only turn
    False as the numbers grow; so when both answers give the same
    is_first_x AND the same is_percent, the exact cutoff gives them too.
    Returns {"agree", "monotone", "before": flag_whale(...), "same_day":
    flag_whale(...)}. agree is False when the answers are not ordered
    (before > same_day, which the argument needs): check those exactly."""
    fb = flag_whale(position, before["trade_count"], before["volume_usdc"])
    fs = flag_whale(position, same_day["trade_count"], same_day["volume_usdc"])
    monotone = (int(before["trade_count"]) <= int(same_day["trade_count"])
                and float(before["volume_usdc"]) <= float(same_day["volume_usdc"]) + USD_TOLERANCE)
    agree = monotone and fb["is_first_x"] == fs["is_first_x"] and fb["is_percent"] == fs["is_percent"]
    return {"agree": agree, "monotone": monotone, "before": fb, "same_day": fs}


def price_jump(points, high=JUMP_HIGH, floor=JUMP_FLOOR):
    """(ts, price) of the price jump in `points` [(ts, price), any order]:
    the first second with a trade >= high from which on no trade is below
    floor. None when there is none (never reached `high`, or fell back)."""
    by_ts = {}
    for ts, p in points:
        ts, p = int(ts), float(p)
        lo, hi = by_ts.get(ts, (p, p))
        by_ts[ts] = (min(lo, p), max(hi, p))
    stamps = sorted(by_ts)
    suffix_min, m = [0.0] * len(stamps), float("inf")
    for i in range(len(stamps) - 1, -1, -1):
        m = min(m, by_ts[stamps[i]][0])
        suffix_min[i] = m
    for i, ts in enumerate(stamps):
        if by_ts[ts][1] >= high and suffix_min[i] >= floor:
            return ts, by_ts[ts][1]
    return None


def summarize(whales):
    """Market totals over scored whale rows (flag_whale fields + position,
    before_jump)."""
    cap = sum(w["position"] for w in whales)
    fl = [w for w in whales if w["flagged"]]
    fcap = sum(w["position"] for w in fl)
    before = [w for w in whales if w.get("before_jump")]
    fbefore = [w for w in fl if w.get("before_jump")]
    return {
        "whale_count": len(whales),
        "whale_capital": round(float(cap), 2),
        "flagged_count": len(fl),
        "flagged_capital": round(float(fcap), 2),
        "flagged_pct": round(fcap / cap * 100, 2) if cap > 0 else 0.0,
        "first_x_count": sum(1 for w in whales if w["is_first_x"]),
        "percent_count": sum(1 for w in whales if w["is_percent"]),
        "whale_capital_before_jump": round(float(sum(w["position"] for w in before)), 2),
        "flagged_count_before_jump": len(fbefore),
        "flagged_capital_before_jump": round(float(sum(w["position"] for w in fbefore)), 2),
    }


# ── trades on disk ───────────────────────────────────────────────────────────

def cache_paths(condition_id, cache_dir=CACHE_DIR):
    return {"raw_gz": os.path.join(cache_dir, f"{condition_id}.csv.gz"),
            "clean": os.path.join(cache_dir, f"{condition_id}_clean.csv")}


def _read_raw(path):
    """Raw-CSV rows (dicts of strings) from a .csv or .csv.gz."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def winning_points(rows, winner, min_usd=JUMP_MIN_USD):
    """(ts, price) of every raw row on the winning outcome worth at least
    min_usd (dust fills at stale prices are not the market's price)."""
    w = str(winner).strip().lower()
    out = []
    for r in rows:
        if str(r.get("outcome", "")).strip().lower() != w:
            continue
        ts = to_epoch(r.get("timestamp"))
        if ts is None:
            continue
        try:
            price, usd = float(r.get("odds")), float(r.get("dollar_amount") or 0)
        except (TypeError, ValueError):
            continue
        if usd >= min_usd:
            out.append((ts, price))
    return out


def _tmp(path):
    return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"


def _build_cache(trades, paths):
    """save_csv + cleandata.main into the cache, atomically: raw gz + clean."""
    os.makedirs(os.path.dirname(paths["clean"]), exist_ok=True)
    raw_tmp, clean_tmp, gz_tmp = _tmp(paths["raw_gz"][:-3]), _tmp(paths["clean"]), _tmp(paths["raw_gz"])
    try:
        polymarket_trades.save_csv(trades, raw_tmp)
        cleandata.main(raw_tmp, clean_tmp)
        with open(raw_tmp, "rb") as src, gzip.open(gz_tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        os.replace(gz_tmp, paths["raw_gz"])
        os.replace(clean_tmp, paths["clean"])
    finally:
        for p in (raw_tmp, clean_tmp, gz_tmp):
            if os.path.exists(p):
                os.remove(p)


def _clean_from_raw(raw_path, paths):
    """cleandata.main on an existing raw CSV (.csv or .csv.gz) into the cache."""
    os.makedirs(os.path.dirname(paths["clean"]), exist_ok=True)
    clean_tmp = _tmp(paths["clean"])
    plain = raw_path
    if raw_path.endswith(".gz"):
        plain = _tmp(paths["raw_gz"][:-3])
        with gzip.open(raw_path, "rb") as src, open(plain, "wb") as dst:
            shutil.copyfileobj(src, dst)
    try:
        cleandata.main(plain, clean_tmp)
        os.replace(clean_tmp, paths["clean"])
    finally:
        for p in {clean_tmp, plain} - {raw_path}:
            if os.path.exists(p):
                os.remove(p)


# ── the method ───────────────────────────────────────────────────────────────

def _identity(market, cutoff_variant):
    ev = filters.event_of(market)
    return {
        "status": None,
        "cutoff_variant": cutoff_variant,
        "condition_id": market.get("conditionId"),
        "market_id": str(market.get("id") or "") or None,
        "question": market.get("question"),
        "slug": market.get("slug"),
        "event_id": str(ev.get("id") or "") or None,
        "event_title": ev.get("title"),
        "event_slug": ev.get("slug"),
        "series_slug": ev.get("seriesSlug"),
        "neg_risk": bool(market.get("negRisk")),
        "gamma_volume": round(filters.volume(market), 2),
        "end_date": market.get("endDate"),
        "closed_time": iso(to_epoch(market.get("closedTime"))),
        "closed_ts": to_epoch(market.get("closedTime")),
    }


def analyze_market(market, cutoff_variant=DEFAULT_VARIANT, correct_outcome=None, conn=None, save=True,
                   max_rows=MAX_ROWS, max_volume=None, clean_csv=None, raw_csv=None, cache_dir=CACHE_DIR,
                   refetch=False, workers=WORKERS):
    """The method on one resolved Gamma market dict. Returns the result dict
    (see RESULT_FIELDS) and, with save=True, stores it (store.save_result).
    conn: the store for the result and the wallet look-up cache (default:
    data/watchlist/watchlist.db). Thread-safe; for many markets in
    parallel, mind max_rows x ~3 KB of memory per concurrent download.

    cutoff_variant: "exact" (default), "same_day" or "day_before" (see the
        module docstring).
    correct_outcome: the winning outcome name; default = Gamma's resolution
        (filters.winning_outcome). Pass markets.json's value for the seven
        published markets; check_correct_outcome refuses a mismatch.
    clean_csv / raw_csv: use these files instead of the cache/the network
        (read only; e.g. the committed data/<Output>_clean.csv for parity).
        Without a raw file the timing fields are None.
    max_rows: fills above this -> status "too_large" (None = no guard; a
        walk capped by polymarket_api's safety net is too_large either way).
    max_volume: Gamma volume above this -> "too_large" without fetching.
    """
    if cutoff_variant not in VARIANTS:
        raise ValueError(f"cutoff_variant must be one of {VARIANTS}, not {cutoff_variant!r}")
    t0 = time.time()
    conn = conn if conn is not None else store.default_conn()   # wallet look-ups are cached either way
    res = _identity(market, cutoff_variant)
    cid = res["condition_id"]
    if not cid:
        raise ValueError("market has no conditionId")

    def finish(status, **extra):
        res.update(extra)
        res["status"] = status
        res["analysed_at"] = iso(now_epoch())
        res.setdefault("timing_s", {})["total"] = round(time.time() - t0, 2)
        if save:
            store.save_result(conn, res)
        return res

    win = filters.winning_outcome(market)
    winner = correct_outcome or (win[1] if win else None)
    res["winner"] = winner
    res["winner_source"] = "given" if correct_outcome else "gamma"
    if not winner:
        return finish("unresolved")

    paths = cache_paths(cid, cache_dir)
    raw_rows, raw_source = None, None
    t_fetch = time.time()
    if clean_csv:
        clean_path = clean_csv
        raw_path = raw_csv or (paths["raw_gz"] if os.path.exists(paths["raw_gz"]) else None)
        if raw_path:
            raw_rows, raw_source = _read_raw(raw_path), raw_path
    elif raw_csv:
        raw_rows, raw_source = _read_raw(raw_csv), raw_csv
        if refetch or not os.path.exists(paths["clean"]):
            _clean_from_raw(raw_csv, paths)
        clean_path = paths["clean"]
    elif not refetch and os.path.exists(paths["raw_gz"]):
        raw_rows, raw_source = _read_raw(paths["raw_gz"]), "cache"
        if not os.path.exists(paths["clean"]):
            _clean_from_raw(paths["raw_gz"], paths)
        clean_path = paths["clean"]
    else:
        if max_volume and filters.volume(market) > max_volume:
            return finish("too_large", too_large=f"Gamma volume ${filters.volume(market):,.0f} > max_volume "
                                                 f"${max_volume:,.0f}; not fetched")
        net.install()
        log(f"  fetching fills of {cid[:12]}… ({res['question']})")

        def progress(n_rows, pages):
            if pages % polymarket_trades.PROGRESS_EVERY == 0:
                log(f"  page {pages:,}  |  {n_rows:,} rows")

        cap = (max_rows + 1) if max_rows else None
        v2_rows, capped = polymarket_api.fetch_trades(cid, max_rows=cap, on_progress=progress)
        if capped:
            # the cap counts CLOB-operator rows, which fetch_trades drops
            # after the walk: the kept rows can be <= max_rows although the
            # oldest fills were never fetched
            return finish("too_large", n_trades=len(v2_rows), trades_capped=True,
                          too_large=f"the fill walk was capped ({len(v2_rows):,} rows kept, cap "
                                    f"{f'{cap:,} rows' if cap else 'the page safety net'}); the oldest fills "
                                    f"were not fetched; not analysed")
        if max_rows and len(v2_rows) > max_rows:
            return finish("too_large", n_trades=len(v2_rows), trades_capped=False,
                          too_large=f"more than {max_rows:,} fills; not analysed")
        trades = polymarket_trades.to_trade_rows(v2_rows, market)
        del v2_rows
        if not trades:
            return finish("no_trades", n_trades=0)
        _build_cache(trades, paths)
        raw_rows, raw_source, clean_path = trades, "api", paths["clean"]
        del trades
    res["files"] = {"raw": paths["raw_gz"] if raw_source in ("api", "cache") else raw_source, "clean": clean_path}
    res["trades_source"] = raw_source if raw_source in ("api", "cache") else ("file" if raw_source else None)
    t_fetch = time.time() - t_fetch

    # ── score_market's whale set, step for step ──────────────────────────────
    df = pd.read_csv(clean_path)
    total_vol = df["dollar_amount"].sum()
    threshold = whale_threshold(total_vol)
    check_correct_outcome(df, winner, res["question"] or cid)
    df["is_winner"] = df["outcome"].astype(str).str.lower() == str(winner).lower()
    whale_mask, sweeps, exits = whale_selection(df, threshold)
    whales = df[whale_mask]

    res.update({
        "n_trades": len(raw_rows) if raw_rows is not None else None,
        "n_positions": int(len(df)),
        "clean_volume": round(float(total_vol), 2),
        "threshold": round(float(threshold), 2),
        "sweeps": {"count": int(sweeps.sum()), "capital": round(float(df.loc[sweeps, "dollar_amount"].sum()), 2),
                   "rule": f"avg price >= {NEAR_CERTAIN_ODDS}"},
        "exits": {"count": int(exits.sum()), "capital": round(float(df.loc[exits, "dollar_amount"].sum()), 2)},
    })

    # ── prior history ────────────────────────────────────────────────────────
    t_look = time.time()
    rows = []
    for _, row in whales.iterrows():
        first_ts = int(pd.to_datetime(row["first_trade"]).timestamp())
        rows.append({"row": row, "wallet": wallet_for(row), "first_ts": first_ts,
                     "position": float(row["dollar_amount"])})
    priors = lookup_priors(rows, cutoff_variant, conn=conn, workers=workers)
    t_look = time.time() - t_look

    # ── timing ───────────────────────────────────────────────────────────────
    jump = price_jump(winning_points(raw_rows, winner)) if raw_rows is not None else None
    del raw_rows
    if jump:
        reveal_ts, basis = jump[0], "price_jump"
    elif raw_source is not None and res["closed_ts"]:
        reveal_ts, basis = res["closed_ts"], "close"
    else:
        reveal_ts, basis = None, "none"

    scored = []
    for r, p in zip(rows, priors):
        row = r["row"]
        position = r["position"]
        f = flag_whale(position, p["trade_count"], p["volume_usdc"])
        w = {
            "user_id": str(row["user_id"]).lower(),
            "wallet": r["wallet"],
            "position": round(position, 4),
            "avg_odds": float(row["avg_odds"]),
            "num_trades": int(row["num_trades"]),
            "first_trade": str(row["first_trade"]),
            "first_trade_ts": r["first_ts"],
            "lookup_end": p["end"],
            "lookup_day": day_str(utc_day(p["end"])),
            "prior_trade_count": p["trade_count"],
            "prior_volume_usdc": round(p["volume_usdc"], 4),
            "prior_counts_exact": p["counts_exact"],
            "lifetime_portfolio": round(f["lifetime_portfolio"], 4),
            "concentration": round(f["concentration"], 2),
            "is_first_x": f["is_first_x"],
            "is_percent": f["is_percent"],
            "flagged": f["flagged"],
            "decided_by": p["decided_by"],
            "before_jump": (r["first_ts"] < reveal_ts) if reveal_ts else None,
            "hours_before_jump": round((reveal_ts - r["first_ts"]) / 3600, 2) if reveal_ts else None,
            "cached": p["cached"],
        }
        if cutoff_variant == "exact":
            w["prior_bounds"] = p["bounds"]
            w["exact_check"] = p["exact_check"]
        scored.append(w)
    scored.sort(key=lambda w: -w["position"])

    res.update(summarize(scored))
    if reveal_ts is None:
        for k in ("whale_capital_before_jump", "flagged_count_before_jump", "flagged_capital_before_jump"):
            res[k] = None
    res.update({
        "jump_ts": jump[0] if jump else None,
        "jump_time": iso(jump[0]) if jump else None,
        "jump_price": jump[1] if jump else None,
        "jump_rule": {"high": JUMP_HIGH, "floor": JUMP_FLOOR, "min_usd": JUMP_MIN_USD},
        "reveal_basis": basis,
        "reveal_ts": reveal_ts,
        "whales": scored,
        "decided_by": dict(sorted(Counter(w["decided_by"] for w in scored).items())),
        "lookups": {"cached": sum(1 for w in scored if w["cached"]),
                    "fetched": sum(1 for w in scored if not w["cached"]),
                    "exact_checks": sum(1 for p in priors if p["exact_check"]),
                    "exact_requests": sum(p["exact_check"]["requests"] for p in priors if p["exact_check"])},
        "timing_s": {"trades": round(t_fetch, 2), "lookups": round(t_look, 2)},
    })
    return finish("ok")


def lookup_priors(rows, cutoff_variant, conn=None, workers=WORKERS):
    """The prior history of each whale row ({"wallet", "first_ts",
    "position"}) under cutoff_variant, in order: {"trade_count",
    "volume_usdc", "end" (the user-volume bound the numbers start from),
    "counts_exact", "decided_by" ("one_call" | "agree" | "exact_check"),
    "bounds", "exact_check", "cached"}. Any look-up error raises."""
    if cutoff_variant in ONE_CALL_VARIANTS:
        ends = [lookup_end(r["first_ts"], cutoff_variant) for r in rows]
        answers = net.user_volumes([(r["wallet"], e) for r, e in zip(rows, ends)], conn=conn, workers=workers)
        return [{"trade_count": a["trade_count"], "volume_usdc": a["volume_usdc"], "end": e, "counts_exact": False,
                 "decided_by": "one_call", "bounds": None, "exact_check": None, "cached": a["cached"]}
                for a, e in zip(answers, ends)]
    if cutoff_variant != "exact":
        raise ValueError(f"cutoff_variant must be one of {VARIANTS}, not {cutoff_variant!r}")
    n = len(rows)
    ends_b = [lookup_end(r["first_ts"], "day_before") for r in rows]
    ends_s = [lookup_end(r["first_ts"], "same_day") for r in rows]
    answers = net.user_volumes([(r["wallet"], e) for r, e in zip(rows, ends_b)] +
                               [(r["wallet"], e) for r, e in zip(rows, ends_s)], conn=conn, workers=workers)
    before, same = answers[:n], answers[n:]
    brackets = [bracket(r["position"], b, s) for r, b, s in zip(rows, before, same)]
    need = [i for i, br in enumerate(brackets) if not br["agree"]]
    checked = dict(zip(need, net.exact_priors([(rows[i]["wallet"], rows[i]["first_ts"]) for i in need],
                                              conn=conn, workers=min(workers, EXACT_WORKERS))))
    out = []
    for i, (b, s, br) in enumerate(zip(before, same, brackets)):
        bounds = {"day_before": {"trade_count": b["trade_count"], "volume_usdc": round(b["volume_usdc"], 4)},
                  "same_day": {"trade_count": s["trade_count"], "volume_usdc": round(s["volume_usdc"], 4)},
                  "monotone": br["monotone"]}
        ex = checked.get(i)
        if ex is None:
            out.append({"trade_count": b["trade_count"], "volume_usdc": b["volume_usdc"], "end": ends_b[i],
                        "counts_exact": False, "decided_by": "agree", "bounds": bounds, "exact_check": None,
                        "cached": bool(b["cached"] and s["cached"])})
        else:
            out.append({"trade_count": ex["trade_count"], "volume_usdc": ex["volume_usdc"], "end": ends_b[i],
                        "counts_exact": not ex["capped"], "decided_by": "exact_check", "bounds": bounds,
                        "exact_check": {k: ex[k] for k in ("day_rows", "day_tx", "day_usdc", "capped", "cached",
                                                            "requests")},
                        "cached": bool(b["cached"] and s["cached"] and ex["cached"])})
    return out


# The keys of an "ok" result, for consumers (other statuses carry the
# identity keys plus status-specific notes).
RESULT_FIELDS = [
    "status", "cutoff_variant", "condition_id", "market_id", "question", "slug", "event_id", "event_title",
    "event_slug", "series_slug", "neg_risk", "gamma_volume", "end_date", "closed_time", "closed_ts",
    "winner", "winner_source", "files", "trades_source", "n_trades", "n_positions", "clean_volume", "threshold",
    "sweeps", "exits", "whale_count", "whale_capital", "flagged_count", "flagged_capital", "flagged_pct",
    "first_x_count", "percent_count", "whale_capital_before_jump", "flagged_count_before_jump",
    "flagged_capital_before_jump", "jump_ts", "jump_time", "jump_price", "jump_rule", "reveal_basis", "reveal_ts",
    "whales", "decided_by", "lookups", "timing_s", "analysed_at",
]


def analyze_slug(slug, cutoff_variant=DEFAULT_VARIANT, **kw):
    """analyze_market for a market slug (fetched from Gamma)."""
    net.install()
    return analyze_market(polymarket_api.fetch_market(slug), cutoff_variant=cutoff_variant, **kw)


def brief(res):
    """The headline numbers of a result (no whale rows), for logs and JSON."""
    keep = [k for k in RESULT_FIELDS if k not in ("whales", "files")]
    return {k: res.get(k) for k in keep if k in res}


# ── live check of the exact variant ──────────────────────────────────────────

VERIFY_PATH = os.path.join(DEMO_DIR, "exact_verify.json")
FLAG_KEYS = ("is_first_x", "is_percent", "flagged")


def pick_verify_sample(whales, n):
    """Up to n whale rows of an "exact" result to re-crawl: the exact_check
    rows first (the disagreements between the one-call variants, largest
    positions first; at most n - n // 4 of them, so agree rows are always
    in the sample), then agree rows spread evenly over the position ranking."""
    checks = [w for w in whales if w.get("decided_by") == "exact_check"]
    agree = [w for w in whales if w.get("decided_by") == "agree"]
    take_checks = checks[:max(n - n // 4, n - len(agree))]
    k = min(len(agree), n - len(take_checks))
    take_agree = [agree[round(i * (len(agree) - 1) / max(1, k - 1))] for i in range(k)] if k else []
    seen, out = set(), []
    for w in take_checks + take_agree:
        if w["user_id"] not in seen:
            seen.add(w["user_id"])
            out.append(w)
    return out


def compare_with_crawl(w, hist, capped):
    """One whale row of an "exact" result against its full prior-history
    crawl (polymarket_api.fetch_user_history_up_to at the first trade, the
    call scrape_insiders makes). The crawl is scored twice: counting fills
    (what user-volume and the exact check count) and counting unique
    transactions (scrape_insiders.analyze_whale's count). A capped crawl
    gives floors, which decide the flags only when they rule both out."""
    pos = float(w["position"])
    fills = len(hist)
    tx = len({h.get("transactionHash", "") for h in hist})
    usdc = round(sum(float(h.get("usdcSize") or 0) for h in hist), 4)
    by_fills, by_tx = flag_whale(pos, fills, usdc), flag_whale(pos, tx, usdc)
    determined = (not capped) or (tx > FIRST_X_TRADES and usdc > pos * 100.0 / PERCENT_VOLUME)
    mine = {f: w[f] for f in FLAG_KEYS}
    rec = {
        "user_id": w["user_id"], "position": w["position"], "first_trade": w["first_trade"],
        "decided_by": w["decided_by"],
        "exact": {"trade_count": w["prior_trade_count"], "volume_usdc": w["prior_volume_usdc"],
                  "counts_exact": w.get("prior_counts_exact"), **mine},
        "bounds": w.get("prior_bounds"),
        "crawl": {"fills": fills, "transactions": tx, "volume_usdc": usdc, "capped": bool(capped),
                  "determined": determined,
                  "flags_fills": {f: by_fills[f] for f in FLAG_KEYS},
                  "flags_tx": {f: by_tx[f] for f in FLAG_KEYS}},
        "flags_match_crawl_fills": all(mine[f] == by_fills[f] for f in FLAG_KEYS),
        "flags_match_crawl_tx": all(mine[f] == by_tx[f] for f in FLAG_KEYS),
    }
    b = w.get("prior_bounds")
    if b and not capped:
        rec["crawl_fills_within_bounds"] = (b["day_before"]["trade_count"] <= fills <= b["same_day"]["trade_count"])
    if w["decided_by"] == "exact_check" and not capped:
        rec["exact_minus_crawl"] = {"fills": w["prior_trade_count"] - fills,
                                    "volume_usdc": round(w["prior_volume_usdc"] - usdc, 4)}
    return rec


def verify_exact(res, n=20, workers=4, budget=None):
    """Re-crawl a sample of an "exact" result's whales (pick_verify_sample)
    with fetch_user_history_up_to and compare the flags. Returns the market's
    verification block (summary + one record per whale)."""
    from concurrent.futures import ThreadPoolExecutor
    if res.get("cutoff_variant") != "exact" or res.get("status") != "ok":
        raise ValueError("verify_exact needs an ok result of the exact variant")
    net.install()
    sample = pick_verify_sample(res["whales"], n)
    cap_rows = polymarket_api.HISTORY_MAX_PAGES * polymarket_api.PAGE_SIZE
    t0 = time.time()

    def crawl(w):
        if budget is not None and budget.expired(reserve=30):
            return None
        hist = polymarket_api.fetch_user_history_up_to(w["wallet"], w["first_trade_ts"])
        return compare_with_crawl(w, hist, len(hist) >= cap_rows)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        recs = [r for r in pool.map(crawl, sample) if r is not None]
    det = [r for r in recs if r["crawl"]["determined"]]
    summary = {
        "sampled": len(recs), "planned": len(sample), "determined": len(det),
        "by_decided_by": dict(sorted(Counter(r["decided_by"] for r in recs).items())),
        "flags_match_crawl_fills": sum(1 for r in det if r["flags_match_crawl_fills"]),
        "flags_match_crawl_tx": sum(1 for r in det if r["flags_match_crawl_tx"]),
        "exact_check_rows_flags_match_fills": sum(1 for r in det if r["decided_by"] == "exact_check"
                                                  and r["flags_match_crawl_fills"]),
        "exact_check_rows": sum(1 for r in det if r["decided_by"] == "exact_check"),
        "agree_rows_flags_match_fills": sum(1 for r in det if r["decided_by"] == "agree"
                                            and r["flags_match_crawl_fills"]),
        "agree_rows": sum(1 for r in det if r["decided_by"] == "agree"),
        "crawl_fills_within_bounds": sum(1 for r in recs if r.get("crawl_fills_within_bounds")),
        "with_bounds_uncapped": sum(1 for r in recs if "crawl_fills_within_bounds" in r),
        "exact_check_counts_equal_crawl": sum(1 for r in recs if r.get("exact_minus_crawl", {}).get("fills") == 0),
        "mismatches": [r["user_id"] for r in det if not r["flags_match_crawl_fills"]
                       or not r["flags_match_crawl_tx"]],
        "secs": round(time.time() - t0, 1),
    }
    return {"condition_id": res["condition_id"], "question": res["question"], "slug": res.get("slug"),
            "whale_count": res["whale_count"], "decided_by": res.get("decided_by"), "lookups": res.get("lookups"),
            "checked_at": iso(now_epoch()), "summary": summary, "whales": recs}


def save_verify(block, path=VERIFY_PATH):
    """Merge one market's verification block into exact_verify.json."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("markets", {})[block["condition_id"]] = block
    ms = list(doc["markets"].values())
    doc["about"] = ("The exact cutoff variant (two user-volume calls, plus one /v2/activity page where they "
                    "disagree) against the full fetch_user_history_up_to crawl at each sampled whale's first trade. "
                    "flags_match_crawl_fills: is_first_x, is_percent and flagged all equal the crawl counted in "
                    "fills (what the exact variant counts); _tx: counted in unique transactions "
                    "(scrape_insiders.analyze_whale). Only crawls that decide the flags (uncapped, or capped "
                    "floors that rule both out) are counted.")
    doc["totals"] = {k: sum(m["summary"][k] for m in ms) for k in
                     ("sampled", "determined", "flags_match_crawl_fills", "flags_match_crawl_tx",
                      "exact_check_rows", "exact_check_rows_flags_match_fills", "agree_rows",
                      "agree_rows_flags_match_fills")}
    doc["updated_at"] = iso(now_epoch())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _tmp(path)
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2, default=str)
    os.replace(tmp, path)
    return doc


def _published(name):
    """(slug, correct_outcome, clean_csv, raw_csv) of a markets.json entry
    (the committed files are only read)."""
    from watchlist.common import ROOT
    with open(os.path.join(ROOT, "markets.json")) as f:
        entries = json.load(f)
    for e in entries:
        if name in (e.get("name"), e.get("output")):
            out = os.path.join(ROOT, "data", e["output"])
            raw = f"{out}.csv" if os.path.exists(f"{out}.csv") else None
            return e["slug"], e.get("correct_outcome"), f"{out}_clean.csv", raw
    raise ValueError(f"no markets.json entry named {name!r}")


if __name__ == "__main__":
    import argparse
    from watchlist.common import Budget
    ap = argparse.ArgumentParser(description="Run the user's method on one resolved market.")
    ap.add_argument("--slug", help="market slug (or --market)")
    ap.add_argument("--market", help="a markets.json name/output: its slug, outcome and committed CSVs (read only)")
    ap.add_argument("--variant", default=DEFAULT_VARIANT, choices=[*VARIANTS, "both", "all"],
                    help="exact (default), same_day, day_before, both (the two one-call variants) or all")
    ap.add_argument("--correct-outcome", default=None)
    ap.add_argument("--clean-csv", default=None, help="use this clean CSV (read only)")
    ap.add_argument("--raw-csv", default=None, help="use this raw CSV (read only)")
    ap.add_argument("--max-rows", type=int, default=MAX_ROWS)
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--whales", action="store_true", help="print every whale row too")
    ap.add_argument("--verify-exact", type=int, default=0, metavar="N",
                    help="re-crawl N whales of the exact result with fetch_user_history_up_to (writes exact_verify.json)")
    ap.add_argument("--budget-seconds", type=float, default=None)
    args = ap.parse_args()
    slug, outcome, clean_csv, raw_csv = args.slug, args.correct_outcome, args.clean_csv, args.raw_csv
    if args.market:
        slug, pub_outcome, pub_clean, pub_raw = _published(args.market)
        outcome, clean_csv, raw_csv = outcome or pub_outcome, clean_csv or pub_clean, raw_csv or pub_raw
    if not slug:
        ap.error("--slug or --market is required")
    variants = {"both": ONE_CALL_VARIANTS, "all": VARIANTS}.get(args.variant, (args.variant,))
    if args.verify_exact and "exact" not in variants:
        ap.error("--verify-exact needs the exact variant")
    budget = Budget(args.budget_seconds)
    net.install()
    m = polymarket_api.fetch_market(slug)
    for i, v in enumerate(variants):
        r = analyze_market(m, cutoff_variant=v, correct_outcome=outcome, max_rows=args.max_rows,
                           clean_csv=clean_csv, raw_csv=raw_csv, refetch=args.refetch and i == 0,
                           save=not args.no_save)
        out = r if args.whales else brief(r)
        buf = io.StringIO()
        json.dump(out, buf, indent=2, default=str)
        print(buf.getvalue())
        if v == "exact" and args.verify_exact and r["status"] == "ok":
            block = verify_exact(r, n=args.verify_exact, budget=budget)
            save_verify(block)
            print(json.dumps({"verify_exact": block["summary"], "written": VERIFY_PATH}, indent=2))
    print(json.dumps({"requests": net.bucket_stats()}))
