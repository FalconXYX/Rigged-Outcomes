"""
The closed scan: every market that resolved in a window, scored with the
user's method (watchlist.method, cutoff variant "exact" by default) and
turned into the daily closed list (the top 5 events of each UTC day). It
is also the backfill engine for the track record: point it at any past
window and run it until it reports nothing pending (in one go, or in
budget-limited chunks). Everything it learns goes into the store, so a
re-run only does what is left.

One window [since, until) (UTC), step by step:

  1. List. net.closed_markets walks Gamma once for the whole window: every
     market whose closedTime (the actual close, not the endDate deadline)
     is in it, with volumeNum >= $100k. The slimmed list is cached under
     data/watchlist/cache/closed_lists/ for --list-max-age (6 h): the
     walk's cost is set by how far back `since` lies, not by the window's
     length, so a backfill run in chunks would otherwise re-walk the whole
     distance on every chunk.
  2. Keywords (classify, pure):
       excluded    filters.exclusion: games, price markets (including an
                   asset that hits a price: "Will STRC hit $100 ..."), weather
       unresolved  no single winning outcome (a 50-50 resolution)
       candidate   the rest
  3. AI type (ai_labels). Every candidate gets an ai.label_markets label,
     cached per (market id, sha1 of its rules), so a market is asked once
     however often the window is re-run; uncached ones go 15 to a call,
     ai.MIN_INTERVAL (4.2 s) apart: the free tier's 15 requests a minute.
     game_sport / price_level / weather (ai.EXCLUDED_TYPES) become
     "excluded" with reason "ai: <type>"; the rest are "kept" with reason
     "ai: <type>". A market whose call failed is kept ("ai: unlabelled": a
     failed call never removes a market) and asked again on the next run,
     which excludes it then if its type says so (its result, if any, is
     simply no longer read). --no-ai skips the step ("ai: not asked").
  4. Group (plan_window, pure): the kept markets by (UTC day of
     closedTime, Gamma event). Groups are per day because the closed list
     is per day: an event whose markets close on several days ("Israel x
     Iran ceasefire continues through Nov 1 / 2 / 3 ...") appears on each
     of those days with that day's markets. Every market is recorded in
     store.markets with its role (kept | excluded | unresolved) and reason.
  5. Analyse EVERY kept market without a result for the cutoff variant
     through method.analyze_market, a few at a time (--workers, default
     4). Not one main market per event: in a date ladder (US x Iran
     ceasefire continues through Oct 31 / Nov 7 / ...) the market that
     resolved Yes is often the one nobody needed inside knowledge for,
     and the money that knew sat on a sibling. net's token buckets keep
     the process at 40 % of each documented rate limit, and at most one
     market of >= $3M Gamma volume is downloaded at a time, since its
     fills are held in memory. A market above the method's max_rows (200k
     fills), or whose fill walk was capped, comes back "too_large" (the
     method checks the walk's cap itself) and is listed, not retried. An
     API error is logged, kept in meta and NOT stored as a result, so the
     next run retries that market.
  6. Figure (choose_figure, pure). An event-day stands on ONE number set:
     the result of its market with the highest flagged_capital among the
     "ok" ones; the report names that market (figure_market) and lists the
     others with their own numbers. An event-day with a market still
     pending or in error is "partial" and not ranked until it is done.
  7. Events. store.events gets one row per event whose main_condition_id
     is the same figure rule over ALL its kept markets the store has seen
     (every day, not just this window) and whose type is that market's AI
     type, so the track record (store.closed_results with main_only)
     counts every event once, on the number the closed list would show.
  8. Rank (rank_day, pure): among a day's event-days with status ok and
     flagged_capital >= $10,000, the top 5 by flagged_pct (ties: more
     flagged capital first). Quiet days list fewer.
  9. Report: data/watchlist/demo/closed_days.json (build_report reads only
     the store), written at the end of every run, with the cost numbers
     (seconds and API calls per market, markets a minute) and the
     projection for a 90-day backfill (project_backfill). --compare adds
     how each day's top 5 changed against an earlier report.

Resume: a market that already has a result (any status) is skipped, and
the AI labels and wallet look-ups are cached. --budget-seconds stops
starting new markets once the time left cannot fit one (20 s for a normal
market, 120 s for a >= $3M one); the running ones finish, the report is
written, and the next run picks up. Without a budget the run goes to the
end, logging one line per market and a progress/ETA line every
PROGRESS_EVERY markets.

CLI (from the repo root):
  python3 -m watchlist.closed_scan --days 7 --budget-seconds 420
  python3 -m watchlist.closed_scan --days 7 --report-only      # rebuild the JSON, no network
  python3 -m watchlist.closed_scan --days 7 --plan             # list + classify + AI labels, no analysis
  # the 90-day backfill, unattended in one go (resumable if stopped):
  python3 -m watchlist.closed_scan --since 2026-08-12 --until 2026-11-10 \
      --report data/watchlist/demo/closed_backfill.json
"""
import gzip
import json
import math
import os
import statistics
import threading
import time
from collections import Counter, defaultdict

from watchlist import ai, filters, method, net, store
from watchlist.common import (CACHE_DIR, DAY, DEMO_DIR, Budget, day_str, iso, log, now_epoch, to_epoch,
                              utc_day)

MIN_VOLUME = 100_000          # the closed universe: volumeNum >= $100k
MIN_FLAGGED_CAPITAL = 10_000  # a day's ranking only considers events with this much flagged money
TOP_N = 5                     # the closed list's length
WORKERS = 4                   # markets analysed at once
HEAVY_VOLUME = 3_000_000      # Gamma volume from which a market counts as heavy (fills held in memory)
HEAVY_SLOTS = 1               # heavy markets downloaded at once
LIGHT_NEED_S = 20             # budget a normal market needs to be started
HEAVY_NEED_S = 120            # budget a >= HEAVY_VOLUME market needs to be started
LIST_MAX_AGE = 6 * 3600       # reuse a cached window list this long
MARKETS_SHOWN = 15            # markets of an event-day listed in the report (n_markets counts all)
PROGRESS_EVERY = 25           # a progress / ETA line every this many markets
# The 90-day walk (2026-08-12 .. 2026-11-10, closedTime) listed ~19,400
# markets >= $100k; the report projects a backfill of that window from it.
BACKFILL_DAYS = 90
BACKFILL_MARKETS = 19_400
LIST_DIR = os.path.join(CACHE_DIR, "closed_lists")
REPORT_PATH = os.path.join(DEMO_DIR, "closed_days.json")
RUNS_KEY = "closed_scan.runs"
ERRORS_KEY = "closed_scan.errors"
LIST_KEY = "closed_scan.lists"
ROLES = ("kept", "excluded", "unresolved")
AI_UNLABELLED = "ai: unlabelled"
AI_NOT_ASKED = "ai: not asked"

# What the scan keeps of a Gamma /markets object (the rest, image URLs,
# fee schedules, CLOB fields, is ~70 % of the payload and unused). The
# description is the rules text the AI labeller reads.
MARKET_KEYS = ("id", "conditionId", "question", "slug", "description", "resolutionSource", "outcomes",
               "outcomePrices", "volume", "volumeNum", "endDate", "startDate", "createdAt", "updatedAt",
               "closedTime", "umaResolutionStatus", "negRisk", "negRiskMarketID", "closed", "active", "archived",
               "groupItemTitle", "gameId", "sportsMarketType", "tags")
EVENT_KEYS = ("id", "ticker", "slug", "title", "seriesSlug", "series", "tags", "sport", "gameId", "negRisk",
              "startDate", "endDate", "creationDate")


# ── pure pieces (unit-tested) ────────────────────────────────────────────────

def slim_market(m):
    """The fields of a Gamma market (and of its events[0]) the scan, the
    method, the AI labeller and the store read; tags and series cut down
    to id/slug/label."""
    out = {k: m[k] for k in MARKET_KEYS if k in m}
    if isinstance(out.get("tags"), list):
        out["tags"] = [{k: t.get(k) for k in ("id", "slug", "label")} if isinstance(t, dict) else t
                       for t in out["tags"]]
    ev = filters.event_of(m)
    if ev:
        e = {k: ev[k] for k in EVENT_KEYS if k in ev}
        if isinstance(e.get("series"), list):
            e["series"] = [{k: s.get(k) for k in ("id", "slug", "title", "recurrence")} if isinstance(s, dict) else s
                           for s in e["series"]]
        if isinstance(e.get("tags"), list):
            e["tags"] = [{k: t.get(k) for k in ("id", "slug", "label")} if isinstance(t, dict) else t
                         for t in e["tags"]]
        out["events"] = [e]
    return out


def close_day(m):
    """UTC day number of a market's closedTime (None without one)."""
    ts = to_epoch(m.get("closedTime"))
    return None if ts is None else utc_day(ts)


def day_windows(since, until):
    """[(day, start, end)] for the UTC days that [since, until) touches,
    each clipped to the window (epochs)."""
    lo, hi = int(since), int(until)
    out = []
    d = utc_day(lo)
    while d * DAY < hi:
        out.append((d, max(lo, d * DAY), min(hi, (d + 1) * DAY)))
        d += 1
    return out


def classify(m):
    """(role, reason, winner) of one closed market by keywords and
    resolution: ("excluded", "game: tag sports", None), ("unresolved", why,
    None) or ("candidate", None, winner)."""
    ex = filters.exclusion(m)
    if ex:
        return "excluded", f"{ex[0]}: {ex[1]}", None
    w = filters.winning_outcome(m)
    if not w:
        return "unresolved", (f"no single winner (umaResolutionStatus={m.get('umaResolutionStatus')!r}, "
                              f"outcomePrices={m.get('outcomePrices')})"), None
    return "candidate", None, w[1]


def ai_role(m, labels):
    """(role, reason) of a candidate from its AI label: ("excluded",
    "ai: price_level") for an excluded type, else ("kept", "ai: <type>");
    ("kept", AI_UNLABELLED) without a label, ("kept", AI_NOT_ASKED) when
    labels is None (no AI step)."""
    if labels is None:
        return "kept", AI_NOT_ASKED
    lab = labels.get(str(m.get("id")))
    if not lab or not lab.get("type"):
        return "kept", AI_UNLABELLED
    return ("excluded" if ai.is_excluded(lab) else "kept"), f"ai: {lab['type']}"


def ai_type(reason):
    """The AI type in a market row's reason ("ai: company_product" ->
    "company_product"), None when there is none."""
    s = str(reason or "")
    if not s.startswith("ai: ") or s in (AI_UNLABELLED, AI_NOT_ASKED):
        return None
    return s[4:].strip() or None


def plan_window(markets, labels=None):
    """(groups, rows) for a window's closed markets.

    labels: {market_id: AI label} (None = no AI step). rows: [{market,
    role, reason, winner}], one per market: excluded (keywords: "game: tag
    sports"; AI: "ai: price_level"), unresolved, or kept (reason "ai:
    <type>", AI_UNLABELLED or AI_NOT_ASKED). groups: [{key, day, event_id,
    event, markets}], one per (UTC close day, event) with a kept market,
    its kept markets highest volume first; ordered by day, then the
    group's largest market (highest first). Markets without a closedTime
    are dropped (closed_markets never returns them)."""
    rows, by_day = [], defaultdict(list)
    for m in markets:
        d = close_day(m)
        if d is None:
            continue
        role, reason, winner = classify(m)
        if role == "candidate":
            role, reason = ai_role(m, labels)
        if role == "excluded":
            winner = None
        rows.append({"market": m, "role": role, "reason": reason, "winner": winner})
        if role == "kept":
            by_day[d].append(m)
    groups = []
    for d in sorted(by_day):
        day_groups = []
        for g in filters.group_by_event(by_day[d]):
            ms = sorted(g["markets"], key=lambda m: -filters.volume(m))
            day_groups.append({"key": (d, g["event_id"]), "day": d, "event_id": g["event_id"], "event": g["event"],
                               "markets": ms})
        day_groups.sort(key=lambda g: -filters.volume(g["markets"][0]))
        groups.extend(day_groups)
    return groups, rows


def work_items(groups):
    """The markets to analyse, in group order (so an event-day's markets
    finish close together and a budget-cut run leaves few partial events):
    [{market, day, event_id, title}]."""
    return [{"market": m, "day": g["day"], "event_id": g["event_id"], "title": g["event"].get("title") or ""}
            for g in groups for m in g["markets"]]


def choose_figure(candidates):
    """The market whose result stands for an event (one day's markets for
    the closed list, all of them for the event row). candidates: dicts
    with condition_id, status (None = no result yet), flagged_capital,
    flagged_pct, volume, closed_ts. Among "ok" results the highest
    flagged_capital (then flagged_pct, volume, the earlier close, the
    condition id); with no ok result, the highest-volume market with any
    result. None when no candidate has a result."""
    done = [c for c in candidates if c.get("status")]
    if not done:
        return None
    ok = [c for c in done if c["status"] == "ok"]
    if ok:
        return min(ok, key=lambda c: (-(c.get("flagged_capital") or 0), -(c.get("flagged_pct") or 0),
                                      -(c.get("volume") or 0), c.get("closed_ts") or 0, str(c.get("condition_id"))))
    return min(done, key=lambda c: (-(c.get("volume") or 0), c.get("closed_ts") or 0, str(c.get("condition_id"))))


def event_status(statuses):
    """The status of an event-day from its markets' statuses (ok, too_large,
    no_trades, unresolved, pending, error): "ok" when one is ok and none is
    pending or in error, "partial" when one is ok but some are still to
    do, else "error" / "pending" while any is, else the status of the
    markets (too_large when any is)."""
    s = Counter(statuses)
    todo = s["pending"] + s["error"]
    if s["ok"]:
        return "partial" if todo else "ok"
    if todo:
        return "error" if s["error"] else "pending"
    for k in ("too_large", "no_trades", "unresolved"):
        if s[k]:
            return k
    return "pending"


def rank_day(entries, min_flagged=MIN_FLAGGED_CAPITAL, top=TOP_N):
    """The closed list of one day: entries (dicts with status,
    flagged_capital, flagged_pct, event_id) with status "ok" and
    flagged_capital >= min_flagged, by flagged_pct (then flagged capital,
    then event id), at most `top` (None = all)."""
    ok = [e for e in entries if e.get("status") == "ok" and (e.get("flagged_capital") or 0) >= min_flagged]
    ok.sort(key=lambda e: (-(e.get("flagged_pct") or 0), -(e.get("flagged_capital") or 0), str(e.get("event_id"))))
    return ok[:top]


def pctl(values, q):
    """The q-quantile (0..1) of values by linear interpolation, None if empty."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    i = (len(v) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return round(v[lo] + (v[hi] - v[lo]) * (i - lo), 2)


def spread(values):
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "median": pctl(v, 0.5), "p90": pctl(v, 0.9), "max": round(max(v), 2),
            "mean": round(statistics.fmean(v), 2), "total": round(sum(v), 2)}


def api_calls(result):
    """Data-API requests one method run cost, at most: trade pages (1,000
    fills each, when the fills came from the API, not the cache) +
    user-volume look-ups not answered from the cache (one per whale row
    for a one-call variant; up to two for "exact", which asks both) +
    the exact variant's /v2/activity pages."""
    pages = 0
    if result.get("trades_source") == "api" or (result.get("status") == "too_large" and result.get("n_trades")):
        pages = math.ceil((result.get("n_trades") or 0) / 1000) or 1
    lk = result.get("lookups") or {}
    per_row = 2 if result.get("cutoff_variant") == "exact" else 1
    return pages + per_row * int(lk.get("fetched") or 0) + int(lk.get("exact_requests") or 0)


def project_backfill(n_listed, kept_share, candidate_share, markets_per_minute, days=BACKFILL_DAYS,
                     ai_batch=ai.BATCH_SIZE, ai_interval=ai.MIN_INTERVAL, list_pages_per_s=5.0):
    """Wall-time projection of a backfill window that lists n_listed
    markets >= $100k, from one measured window: kept_share = kept /
    listed (what gets analysed), candidate_share = (kept + AI-excluded) /
    listed (what gets labelled), markets_per_minute = the measured
    analysis speed. AI calls are ai_batch markets each, ai_interval
    seconds apart (the free tier's spacing); the Gamma list walk is ~100
    markets a page. Returns the numbers and minutes per phase."""
    kept = round(n_listed * kept_share)
    labelled = round(n_listed * candidate_share)
    ai_calls = math.ceil(labelled / ai_batch) if ai_batch else 0
    ai_min = ai_calls * ai_interval / 60
    list_min = (n_listed / 100) / list_pages_per_s / 60
    analyse_min = kept / markets_per_minute if markets_per_minute else None
    total = None if analyse_min is None else list_min + ai_min + analyse_min
    return {"days": days, "markets_listed": n_listed, "markets_labelled": labelled, "markets_analysed": kept,
            "ai_calls": ai_calls, "markets_per_minute": markets_per_minute,
            "minutes": {"list": round(list_min, 1), "ai": round(ai_min, 1),
                        "analyse": None if analyse_min is None else round(analyse_min, 1),
                        "total": None if total is None else round(total, 1)},
            "hours_total": None if total is None else round(total / 60, 2)}


def compare_previous(old, new, market_roles):
    """How each day's top 5 changed from an earlier report `old` to `new`
    (both build_report dicts; old may be of the former one-main-market
    shape). market_roles: {condition_id: (role, reason, ...)} from the
    store, to say why an event left the list. Returns one row per day in
    both: {day, before, after, dropped: [{event_title, was, why}], added:
    [{event_title, now, why}], kept: [...]}."""
    new_days = {d["day"]: d for d in new.get("days") or []}
    out = []
    for od in old.get("days") or []:
        nd = new_days.get(od.get("day"))
        if nd is None:
            continue
        old_events = {str(e.get("event_id")): e for e in od.get("events") or []}
        new_events = {str(e.get("event_id")): e for e in nd.get("events") or []}
        old_top = {str(c["event_id"]): c for c in od.get("top5") or []}
        new_top = {str(c["event_id"]): c for c in nd.get("top5") or []}

        def brief(c):
            return {"rank": c.get("rank"), "flagged_pct": c.get("flagged_pct"),
                    "flagged_capital": c.get("flagged_capital"),
                    "market": c.get("figure_question") or c.get("main_question")}

        dropped, added, kept = [], [], []
        for eid, c in old_top.items():
            if eid in new_top:
                kept.append({"event_title": c.get("event_title"), "was": brief(c), "now": brief(new_top[eid])})
                continue
            e = new_events.get(eid)
            if e is None:
                cid = ((old_events.get(eid) or {}).get("main") or {}).get("condition_id")
                role = market_roles.get(cid)
                why = (f"no longer scanned: its market is {role[0]} ({role[1]})" if role and role[0] != "kept"
                       else "no longer scanned")
            elif e.get("status") != "ok":
                why = f"status {e.get('status')}"
            elif (e.get("flagged_capital") or 0) < MIN_FLAGGED_CAPITAL:
                why = (f"flagged ${e.get('flagged_capital') or 0:,.0f} < ${MIN_FLAGGED_CAPITAL:,} under the new "
                       f"cutoff ({e.get('flagged_pct')}% flagged)")
            else:
                why = f"out-ranked: now #{e.get('position')} at {e.get('flagged_pct')}% flagged"
            dropped.append({"event_title": c.get("event_title"), "was": brief(c), "why": why})
        for eid, c in new_top.items():
            if eid in old_top:
                continue
            oe = old_events.get(eid)
            old_main = ((oe or {}).get("main") or {}).get("condition_id")
            if oe is None:
                why = "not scanned before"
            elif old_main and old_main != c.get("figure_condition_id"):
                why = (f"figure from another market than the old main ({(oe.get('main') or {}).get('question')!r}: "
                       f"{oe.get('flagged_pct')}% / ${oe.get('flagged_capital') or 0:,.0f})")
            elif oe.get("eligible"):
                why = f"moved up (was {oe.get('flagged_pct')}% flagged, not in the top {TOP_N})"
            else:
                why = (f"now eligible (was {oe.get('flagged_pct')}% / ${oe.get('flagged_capital') or 0:,.0f} "
                       f"flagged)")
            added.append({"event_title": c.get("event_title"), "now": brief(c), "why": why})
        out.append({"day": od.get("day"),
                    "before": [c.get("event_title") for c in od.get("top5") or []],
                    "after": [c.get("event_title") for c in nd.get("top5") or []],
                    "dropped": dropped, "added": added, "kept": kept})
    return out


# ── window list (cached) ─────────────────────────────────────────────────────

def _list_path(since, until, min_volume):
    def tag(ts):
        return iso(ts).replace("+00:00", "Z").replace(":", "")
    return os.path.join(LIST_DIR, f"{tag(since)}_{tag(until)}_{int(min_volume)}.json.gz")


def load_window(since, until, min_volume=MIN_VOLUME, max_age=LIST_MAX_AGE, refresh=False, fetch=None):
    """(markets, info): the window's closed markets (slimmed), from the
    cache when it is younger than max_age, else from `fetch` (default
    net.closed_markets) and cached. info: {cached, fetched_at, pages,
    seconds, n}."""
    path = _list_path(since, until, min_volume)
    if not refresh and os.path.exists(path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                blob = json.load(f)
            if now_epoch() - int(blob["fetched_at"]) < max_age:
                return blob["markets"], {"cached": True, "fetched_at": blob["fetched_at"], "pages": blob.get("pages"),
                                         "seconds": blob.get("seconds"), "n": len(blob["markets"]), "path": path}
        except (OSError, ValueError, KeyError) as e:
            log(f"  ! unreadable window list {path} ({e}); re-fetching")
    fetch = fetch or net.closed_markets
    pages = []
    t0 = time.time()

    def on_page(n, page):
        pages.append(len(page))
        if len(pages) % 50 == 0:
            log(f"  list: {len(pages)} pages")

    markets = fetch(since, until, min_volume=min_volume, on_page=on_page)
    secs = round(time.time() - t0, 1)
    markets = [slim_market(m) for m in markets]
    blob = {"fetched_at": now_epoch(), "since": iso(since), "until": iso(until), "min_volume": min_volume,
            "pages": len(pages), "seconds": secs, "markets": markets}
    os.makedirs(LIST_DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(blob, f, separators=(",", ":"))
    os.replace(tmp, path)
    return markets, {"cached": False, "fetched_at": blob["fetched_at"], "pages": len(pages), "seconds": secs,
                     "n": len(markets), "path": path}


# ── AI labels ────────────────────────────────────────────────────────────────

def ai_labels(markets, conn, budget=None, client=None):
    """({market_id: label}, info) for the candidates, through
    ai.label_markets (cached per market id + rules; the rest asked
    ai.BATCH_SIZE to a call). A failed call leaves its markets out (they
    stay kept and are asked again next run); a refused or missing key is
    logged and the labels got so far are returned. info: candidates,
    labelled, cached, failed, seconds, client (ai.Client.report())."""
    client = client or ai.Client()
    t0 = time.time()
    done = Counter()

    def on_batch(batch, got, failed, errs):
        done["asked"] += len(batch)
        done["failed"] += len(failed)
        log(f"  ai: +{len(got)} labelled" + (f", {len(failed)} failed ({errs[-1][:80] if errs else ''})"
                                             if failed else "") + f"  [{done['asked']:,} asked this run]")

    labels, error = {}, None
    try:
        labels = ai.label_markets(markets, conn=conn, client=client, budget=budget, on_batch=on_batch)
    except ai.GeminiError as e:   # the key is refused or missing: keep every market, label next run
        error = str(e)[:300]
        log(f"  ! ai: {error}; every candidate is kept unlabelled this run")
    info = {"candidates": len(markets), "labelled": len(labels),
            "cached": sum(1 for x in labels.values() if x.get("cached")),
            "asked": done["asked"], "failed": len(markets) - len(labels), "error": error,
            "excluded": dict(Counter(x["type"] for x in labels.values() if ai.is_excluded(x))),
            "seconds": round(time.time() - t0, 1), "client": client.report()}
    return labels, info


# ── store helpers this module needs (store.py has none for them) ─────────────

def _stored_roles(conn, since, until):
    with store.LOCK:
        rows = conn.execute("SELECT condition_id, role, reason, winner FROM markets WHERE closed_ts >= ? AND "
                            "closed_ts < ?", (int(since), int(until))).fetchall()
    return {r["condition_id"]: (r["role"], r["reason"], r["winner"]) for r in rows}


def record_markets(conn, rows, since, until):
    """store.save_market for every planned row whose role/reason/winner
    changed since the last run (a re-run writes nothing). Returns the count."""
    old = _stored_roles(conn, since, until)
    n = 0
    for r in rows:
        cid = r["market"].get("conditionId")
        if not cid or old.get(cid) == (r["role"], r["reason"], r["winner"]):
            continue
        store.save_market(conn, r["market"], r["role"], r["reason"], r["winner"])
        n += 1
    return n


def _headlines(conn, condition_ids, variant):
    """{condition_id: {status, flagged_capital, flagged_pct}} from the
    results table's columns (no JSON parsed)."""
    ids = [c for c in condition_ids if c]
    out = {}
    with store.LOCK:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT condition_id, status, flagged_capital, flagged_pct FROM results WHERE "
                                  f"cutoff_variant=? AND condition_id IN ({q})", (variant, *chunk)).fetchall():
                out[r["condition_id"]] = dict(r)
    return out


def refresh_events(conn, events, variant):
    """Write store.events for each event ({event_id: Gamma event dict}):
    main_condition_id = choose_figure over ALL the event's kept markets
    the store has (every day it has seen, not just this window), others =
    its other kept and unresolved markets, closed_ts = the figure
    market's, type = its AI type (type_source "ai"). Events none of whose
    kept markets has a result are left alone. Returns the number written."""
    n = 0
    for eid, event in events.items():
        if not eid or str(eid).startswith("m:") or not event or not event.get("id"):
            continue
        with store.LOCK:
            rows = [dict(r) for r in conn.execute(
                "SELECT condition_id, role, reason, winner, volume, closed_ts FROM markets WHERE event_id=? AND role "
                "IN ('kept','unresolved')", (str(eid),)).fetchall()]
        kept = [r for r in rows if r["role"] == "kept"]
        head = _headlines(conn, [r["condition_id"] for r in kept], variant)
        for r in kept:
            r.update(head.get(r["condition_id"]) or {"status": None})
        best = choose_figure(kept)
        if not best:
            continue
        others = sorted({r["condition_id"] for r in rows} - {best["condition_id"]})
        store.save_event(conn, event, main_condition_id=best["condition_id"], other_condition_ids=others,
                         closed_ts=best["closed_ts"])
        t = ai_type(best["reason"])
        if t:
            store.set_event_type(conn, eid, t, "ai")
        n += 1
    return n


# ── the scan ─────────────────────────────────────────────────────────────────

class _Lanes:
    """Hands out work items ({"market": ...}) to worker threads: in order,
    at most heavy_slots heavy (>= heavy_volume) markets at a time, and only
    while the budget can fit the market (LIGHT_NEED_S / HEAVY_NEED_S)."""

    def __init__(self, items, budget, heavy_volume, heavy_slots=HEAVY_SLOTS):
        self.items = list(items)
        self.budget = budget
        self.heavy_volume = heavy_volume
        self.heavy_slots = max(1, int(heavy_slots))
        self.heavy_running = 0
        self.lock = threading.Lock()

    def heavy(self, it):
        return bool(self.heavy_volume) and filters.volume(it["market"]) >= self.heavy_volume

    def take(self):
        with self.lock:
            left = self.budget.left()
            for i, it in enumerate(self.items):
                h = self.heavy(it)
                if h and self.heavy_running >= self.heavy_slots:
                    continue
                if left <= (HEAVY_NEED_S if h else LIGHT_NEED_S):
                    continue
                self.items.pop(i)
                if h:
                    self.heavy_running += 1
                return it
            return None

    def done(self, it):
        if self.heavy(it):
            with self.lock:
                self.heavy_running -= 1


def scan(since, until, variants=(method.DEFAULT_VARIANT,), conn=None, budget=None, workers=WORKERS,
         max_rows=method.MAX_ROWS, max_volume=None, heavy_volume=HEAVY_VOLUME, heavy_slots=HEAVY_SLOTS,
         min_volume=MIN_VOLUME, list_max_age=LIST_MAX_AGE, refresh_list=False, plan_only=False, use_ai=True,
         analyze=None, fetch=None, label=None, progress_every=PROGRESS_EVERY):
    """Scan the window [since, until) (epochs): list, classify, AI-label,
    record, analyse every kept market still missing a result (within
    `budget`), refresh events. Returns the run summary (also appended to
    meta closed_scan.runs). `analyze`, `fetch` and `label` replace
    method.analyze_market / net.closed_markets / ai_labels (tests);
    use_ai=False skips the AI step."""
    t_run = time.time()
    conn = conn if conn is not None else store.default_conn()
    budget = budget or Budget(None)
    analyze = analyze or method.analyze_market
    variants = tuple(variants)
    for v in variants:
        if v not in method.VARIANTS:
            raise ValueError(f"cutoff variant must be one of {method.VARIANTS}, not {v!r}")
    if fetch is None:
        net.install()   # once, before any thread: method.analyze_market calls it too
    calls_before = {k: v["requests"] for k, v in net.bucket_stats().items()}

    markets, list_info = load_window(since, until, min_volume=min_volume, max_age=list_max_age,
                                     refresh=refresh_list, fetch=fetch)
    how = "cached list" if list_info["cached"] else f"{list_info['pages']} pages, {list_info['seconds']} s"
    log(f"window {iso(since)} .. {iso(until)}: {len(markets):,} closed markets >= ${min_volume:,.0f} ({how})")
    if not list_info["cached"]:
        lists = store.get_meta(conn, LIST_KEY, []) or []
        lists.append({"since": iso(since), "until": iso(until), "at": iso(list_info["fetched_at"]),
                      "pages": list_info["pages"], "seconds": list_info["seconds"], "markets": list_info["n"]})
        store.set_meta(conn, LIST_KEY, lists[-200:])

    # keywords, then the AI type for what the keywords keep
    candidates = [m for m in markets if close_day(m) is not None and classify(m)[0] == "candidate"]
    labels, ai_info, t_ai = None, {"skipped": True}, time.time()
    if use_ai or label is not None:
        log(f"  ai: labelling {len(candidates):,} candidates (cached labels are reused)")
        labels, ai_info = (label or ai_labels)(candidates, conn, budget)
        log(f"  ai: {ai_info.get('labelled', len(labels)):,} of {len(candidates):,} labelled "
            f"({ai_info.get('cached', 0):,} cached), {ai_info.get('failed', 0):,} unlabelled (kept); AI-excluded "
            f"{ai_info.get('excluded')}; {time.time() - t_ai:.0f} s")
    t_ai = round(time.time() - t_ai, 1)

    groups, rows = plan_window(markets, labels)
    roles = Counter(r["role"] for r in rows)
    written = record_markets(conn, rows, since, until)
    items = work_items(groups)
    log(f"  {dict(roles)}; {len(groups)} event-day groups, {len(items)} kept markets; {written} market rows written")

    done = {v: store.analysed_conditions(conn, v) for v in variants}
    todo = [it for it in items if any(it["market"].get("conditionId") not in done[v] for v in variants)]
    log(f"  {len(items) - len(todo)} markets already analysed, {len(todo)} to go"
        + (f" (budget {budget.left():.0f} s)" if budget.deadline is not None else ""))

    results, errors, timings = [], {}, []
    errs_meta = store.get_meta(conn, ERRORS_KEY, {}) or {}
    lock = threading.Lock()
    t_an = time.time()
    n_todo = len(todo)

    def work():
        while True:
            it = lanes.take()
            if it is None:
                return
            m = it["market"]
            cid = m.get("conditionId")
            missing = [v for v in variants if cid not in done[v]]
            t0 = time.time()
            try:
                out = {v: analyze(m, cutoff_variant=v, conn=conn, max_rows=max_rows, max_volume=max_volume)
                       for v in missing}
            except Exception as e:   # noqa: BLE001  (recorded, retried on the next run)
                with lock:
                    errors[cid] = {"error": f"{type(e).__name__}: {e}"[:500], "at": iso(now_epoch()),
                                   "question": m.get("question"), "day": day_str(it["day"])}
                log(f"  ! [{day_str(it['day'])}] error on {m.get('question')!r}: {type(e).__name__}: {e}")
                continue
            finally:
                lanes.done(it)
            secs = round(time.time() - t0, 1)
            r = out.get(variants[0]) or next(iter(out.values()))
            with lock:
                results.append(r)
                timings.append(secs)
                errs_meta.pop(cid, None)
                k = len(results) + len(errors)
            pct = r.get("flagged_pct")
            log(f"  [{k}/{n_todo}] [{day_str(it['day'])}] {r.get('status'):9} {r.get('whale_count') or 0:>4} whales "
                f"{(pct if pct is not None else 0):5.1f}% ${r.get('flagged_capital') or 0:>11,.0f}  "
                f"{r.get('n_trades') or 0:>7,} fills {r.get('trades_source') or '-':5} {secs:6.1f} s  "
                f"{str(m.get('question') or '')[:60]}")
            if progress_every and k % progress_every == 0 and k < n_todo:
                el = time.time() - t_an
                rate = k / el * 60 if el > 0 else 0
                log(f"  progress {k:,}/{n_todo:,} markets in {el / 60:.1f} min: {rate:.1f} markets/min, "
                    f"~{(n_todo - k) / rate if rate else 0:.0f} min left, {len(errors)} errors")

    lanes = _Lanes(todo, budget, heavy_volume, heavy_slots)
    if not plan_only and todo:
        threads = [threading.Thread(target=work, daemon=True) for _ in range(max(1, min(workers, len(todo))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    t_an = round(time.time() - t_an, 1)

    errs_meta.update(errors)
    store.set_meta(conn, ERRORS_KEY, errs_meta)
    n_events = refresh_events(conn, {g["event_id"]: g["event"] for g in groups}, variants[0])
    left = len(lanes.items) if not plan_only else len(todo)
    calls_after = {k: v["requests"] for k, v in net.bucket_stats().items()}
    fresh = sum(1 for r in results if r.get("trades_source") == "api")
    summary = {
        "started": iso(int(t_run)), "wall_s": round(time.time() - t_run, 1), "since": iso(since), "until": iso(until),
        "variants": list(variants), "workers": workers, "heavy_slots": heavy_slots, "plan_only": plan_only,
        "list": {k: list_info[k] for k in ("cached", "pages", "seconds", "n")},
        "markets": dict(roles), "groups": len(groups), "kept_markets": len(items),
        "ai": {**{k: v for k, v in ai_info.items() if k != "client"}, "wall_s": t_ai,
               "client": {k: ai_info["client"].get(k) for k in ("requests", "answered", "retries", "fallbacks",
                                                                 "refused", "seconds", "skipped_models", "errors")}
               if ai_info.get("client") else None},
        "analysed": len(results), "fresh_trades": fresh, "errors": len(errors), "left": left,
        "statuses": dict(Counter(r.get("status") for r in results)), "analyse_s": t_an,
        "markets_per_minute": round(len(results) / t_an * 60, 2) if t_an and results else None,
        "market_seconds": spread(timings), "events_written": n_events,
        "requests": {k: calls_after.get(k, 0) - calls_before.get(k, 0) for k in calls_after
                     if calls_after.get(k, 0) - calls_before.get(k, 0)},
        "bucket_wait_s": {k: v["waited_s"] for k, v in net.bucket_stats().items()},
    }
    runs = store.get_meta(conn, RUNS_KEY, []) or []
    runs.append(summary)
    store.set_meta(conn, RUNS_KEY, runs[-500:])
    log(f"run: {summary['analysed']} analysed {summary['statuses']} ({fresh} with fresh fills), "
        f"{summary['errors']} errors, {left} left, analysis {t_an} s"
        + (f" = {summary['markets_per_minute']} markets/min" if summary["markets_per_minute"] else "")
        + f", wall {summary['wall_s']} s, requests {summary['requests']}")
    return summary


# ── the report ───────────────────────────────────────────────────────────────

def _market_line(row, res, err):
    """One market of an event-day in the report."""
    status = res.get("status") if res else ("error" if err else "pending")
    m = {"condition_id": row["condition_id"], "question": row["question"], "slug": row["slug"],
         "volume": round(row["volume"] or 0, 2), "winner": row["winner"], "type": ai_type(row["reason"]),
         "closed_time": iso(row["closed_ts"]), "status": status}
    if res:
        for k in ("flagged_pct", "flagged_capital", "flagged_count", "whale_count", "whale_capital", "n_trades"):
            m[k] = res.get(k)
        if status != "ok":
            m["note"] = res.get("too_large") or status
    if err:
        m["error"] = err.get("error")
    return m


def _entry(event_id, rows, res_by, errs):
    """(entry, lines): one event-day of the report from its kept markets'
    store rows and their method results (the figure, choose_figure, and the
    first MARKETS_SHOWN markets), and every market's line."""
    lines = [_market_line(r, res_by.get(r["condition_id"]),
                          None if r["condition_id"] in res_by else errs.get(r["condition_id"])) for r in rows]
    cands = [{"condition_id": x["condition_id"], "status": x["status"] if x["status"] not in ("pending", "error")
              else None, "flagged_capital": x.get("flagged_capital"), "flagged_pct": x.get("flagged_pct"),
              "volume": x["volume"], "closed_ts": to_epoch(x["closed_time"])} for x in lines]
    fig = choose_figure(cands)
    lines.sort(key=lambda x: (x["status"] != "ok", -(x.get("flagged_capital") or 0), -(x.get("flagged_pct") or 0),
                              -x["volume"]))
    e = {"event_id": event_id, "event_title": None, "event_slug": None, "event_url": None,
         "n_markets": len(lines), "status": event_status([x["status"] for x in lines]),
         "statuses": dict(Counter(x["status"] for x in lines)), "figure_market": None}
    res = res_by.get(fig["condition_id"]) if fig else None
    if res:
        f = next(x for x in lines if x["condition_id"] == fig["condition_id"])
        e["figure_market"] = {k: f[k] for k in ("condition_id", "question", "slug", "volume", "winner", "type",
                                                "closed_time", "status")}
        e["figure_rule"] = (f"highest flagged capital of the event's {len(lines)} kept market(s) that closed this day"
                            if res.get("status") == "ok" else "no ok result: the largest market")
        e["event_title"], e["event_slug"] = res.get("event_title"), res.get("event_slug")
        for k in ("whale_count", "whale_capital", "flagged_count", "flagged_capital", "flagged_pct", "first_x_count",
                  "percent_count", "threshold", "jump_time", "jump_price", "reveal_basis", "flagged_count_before_jump",
                  "flagged_capital_before_jump", "whale_capital_before_jump", "n_trades", "decided_by"):
            e[k] = res.get(k)
        if res.get("status") != "ok":
            e["note"] = res.get("too_large") or res.get("status")
    e["markets"] = lines[:MARKETS_SHOWN]
    if e["event_slug"]:
        e["event_url"] = f"https://polymarket.com/event/{e['event_slug']}"
    e["eligible"] = bool(e["status"] == "ok" and (e.get("flagged_capital") or 0) >= MIN_FLAGGED_CAPITAL)
    return e, lines


def _top_whales(res, n=3):
    fl = [w for w in (res.get("whales") or []) if w.get("flagged")]
    return [{"wallet": w["wallet"], "position": round(w["position"], 2), "avg_odds": w["avg_odds"],
             "first_trade": w["first_trade"], "prior_trade_count": w["prior_trade_count"],
             "prior_volume_usdc": round(w["prior_volume_usdc"], 2), "concentration": w["concentration"],
             "is_first_x": w["is_first_x"], "is_percent": w["is_percent"], "decided_by": w.get("decided_by"),
             "hours_before_jump": w.get("hours_before_jump")} for w in fl[:n]]


def _card(e, res):
    """A top-5 card: the event, the figure market and its numbers, the
    other markets of the day in short, the top flagged whales."""
    card = {k: e.get(k) for k in ("rank", "event_id", "event_title", "event_url", "flagged_pct", "flagged_capital",
                                  "flagged_count", "whale_count", "whale_capital", "jump_time", "reveal_basis",
                                  "flagged_capital_before_jump", "n_markets")}
    f = e["figure_market"]
    card.update({"figure_question": f["question"], "figure_condition_id": f["condition_id"], "volume": f["volume"],
                 "winner": f["winner"], "type": f["type"], "figure_rule": e.get("figure_rule")})
    card["other_markets"] = [{k: x.get(k) for k in ("question", "status", "flagged_pct", "flagged_capital", "winner")}
                             for x in e["markets"] if x["condition_id"] != f["condition_id"]][:4]
    card["top_flagged_whales"] = _top_whales(res)
    return card


def build_report(conn, since, until, variant=method.DEFAULT_VARIANT, max_rows=method.MAX_ROWS):
    """The closed list report for [since, until), from the store only."""
    with store.LOCK:
        mrows = [dict(r) for r in conn.execute(
            "SELECT * FROM markets WHERE closed_ts >= ? AND closed_ts < ? AND role IN ('kept','excluded',"
            "'unresolved') ORDER BY closed_ts", (int(since), int(until))).fetchall()]
    kept = [r for r in mrows if r["role"] == "kept"]
    with store.LOCK:
        res_by = {}
        ids = [r["condition_id"] for r in kept]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT condition_id, result_json FROM results WHERE cutoff_variant=? AND "
                                  f"condition_id IN ({q})", (variant, *chunk)).fetchall():
                res_by[r["condition_id"]] = json.loads(r["result_json"])
    errs = store.get_meta(conn, ERRORS_KEY, {}) or {}

    days, too_large, pending = [], [], []
    for d, lo, hi in day_windows(since, until):
        in_day = [r for r in mrows if lo <= r["closed_ts"] < hi]
        excl = [r for r in in_day if r["role"] == "excluded"]
        by_event = defaultdict(list)
        for r in in_day:
            if r["role"] == "kept":
                by_event[r["event_id"] or f"m:{r['market_id']}"].append(r)
        entries = []
        for eid, rs in by_event.items():
            e, lines = _entry(eid, sorted(rs, key=lambda r: -(r["volume"] or 0)), res_by, errs)
            if not e["event_title"]:
                e["event_title"] = rs[0]["question"]
            entries.append(e)
            for x in lines:
                if x["status"] == "too_large":
                    too_large.append({"day": day_str(d), "event_title": e["event_title"], "question": x["question"],
                                      "volume": x["volume"], "n_trades": x.get("n_trades"), "note": x.get("note")})
                elif x["status"] in ("pending", "error"):
                    pending.append({"day": day_str(d), "question": x["question"], "status": x["status"],
                                    "error": x.get("error")})
        order = rank_day(entries, top=None)
        pos = {id(e): i + 1 for i, e in enumerate(order)}
        for e in entries:
            e["position"] = pos.get(id(e))                       # among the eligible, in rank order
            e["rank"] = e["position"] if e["position"] and e["position"] <= TOP_N else None
        entries.sort(key=lambda e: (e["position"] is None, e["position"] or 0, e["status"] != "ok",
                                    -(e.get("flagged_pct") or 0), -max(x["volume"] for x in e["markets"])))
        top5 = [_card(e, res_by[e["figure_market"]["condition_id"]]) for e in order[:TOP_N]]
        ex_by = Counter(str(r["reason"]).split(":", 1)[0] for r in excl)
        reasons = defaultdict(list)
        for r in sorted(excl, key=lambda r: -(r["volume"] or 0)):
            reasons[r["reason"]].append(r["question"])
        days.append({
            "day": day_str(d),
            "markets_closed": len(in_day),
            "excluded": dict(ex_by),
            "ai_excluded": dict(Counter(ai_type(r["reason"]) for r in excl if ai_type(r["reason"]))),
            "excluded_reasons": [{"reason": k, "n": len(v), "examples": v[:3]}
                                 for k, v in sorted(reasons.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
            "unresolved": [{"question": r["question"], "reason": r["reason"]} for r in in_day
                           if r["role"] == "unresolved"],
            "markets_kept": sum(len(v) for v in by_event.values()),
            "ai_unlabelled": sum(1 for v in by_event.values() for r in v if r["reason"] == AI_UNLABELLED),
            "events_scanned": len(entries),
            "statuses": dict(Counter(e["status"] for e in entries)),
            "eligible": sum(1 for e in entries if e["eligible"]),
            "listed": len(top5),
            "top5": top5,
            "events": entries,
        })

    results = [res_by[r["condition_id"]] for r in kept if r["condition_id"] in res_by]
    fresh = [r for r in results if r.get("trades_source") == "api" or r.get("status") == "too_large"]
    ok_fresh = [r for r in fresh if r.get("status") == "ok"]
    ok_all = [r for r in results if r.get("status") == "ok"]
    per_1k = [r["n_trades"] / r["gamma_volume"] * 1000 for r in ok_fresh if r.get("gamma_volume") and r.get("n_trades")]
    runs = [r for r in (store.get_meta(conn, RUNS_KEY, []) or []) if not r.get("plan_only")]
    run_window = [r for r in runs if r.get("since") == iso(since) and r.get("until") == iso(until)
                  and variant in (r.get("variants") or [])]
    an_s = sum(r.get("analyse_s") or 0 for r in run_window)
    an_n = sum(r["analysed"] for r in run_window)
    max_workers = max([r.get("workers") or 0 for r in run_window] or [0])
    decided = sum((Counter(r.get("decided_by") or {}) for r in ok_all), Counter())
    secs_fresh = [(r.get("timing_s") or {}).get("total") for r in ok_fresh]
    # W workers kept busy finish W markets per mean market time: the speed on
    # markets whose fills came from the API, apart from those re-read from the cache
    fresh_rate = (round(max_workers * 60 / statistics.fmean(secs_fresh), 2)
                  if max_workers and len([s for s in secs_fresh if s]) >= 10 else None)
    measured_rate = round(an_n / an_s * 60, 2) if an_s and an_n else None
    n_listed = len(mrows)
    n_candidates = len(kept) + sum(1 for r in mrows if r["role"] == "excluded" and ai_type(r["reason"]))
    rate_for_projection = fresh_rate or measured_rate
    measurements = {
        "note": ("seconds = wall time inside method.analyze_market (trade download + clean + look-ups), measured "
                 "with several markets in flight at once, so bucket waits are included; 'fresh' = fills from the "
                 "API (a market re-read from the cache skips the download). api_calls = trade pages (1,000 fills "
                 "each) + uncached /v2/user-volume look-ups (up to 2 per whale for the exact variant) + its "
                 "/v2/activity pages: an upper bound; requests_all_runs is the exact count per endpoint family."),
        "workers": max_workers,
        "events_per_day": {x["day"]: x["events_scanned"] for x in days},
        "markets_closed_per_day": {x["day"]: x["markets_closed"] for x in days},
        "markets_kept_per_day": {x["day"]: x["markets_kept"] for x in days},
        "seconds_per_market": spread([(r.get("timing_s") or {}).get("total") for r in results]),
        "seconds_per_market_fresh": spread(secs_fresh),
        "seconds_trades_download_fresh": spread([(r.get("timing_s") or {}).get("trades") for r in ok_fresh]),
        "seconds_lookups": spread([(r.get("timing_s") or {}).get("lookups") for r in ok_all]),
        "api_calls_per_market": spread([api_calls(r) for r in results]),
        "fills_per_market_fresh": spread([r.get("n_trades") for r in ok_fresh]),
        "whales_per_market": spread([r.get("whale_count") for r in ok_all]),
        "decided_by": dict(sorted(decided.items())),
        "exact_requests": sum(int((r.get("lookups") or {}).get("exact_requests") or 0) for r in ok_all),
        "fills_per_1k_dollars_volume": spread(per_1k),   # to pick a --max-volume that avoids too_large downloads
        "too_large_count": len(too_large),
        "runs_for_this_window": len(run_window),
        "wall_seconds_all_runs": round(sum(r["wall_s"] for r in run_window), 1),
        "analysis_seconds_all_runs": round(an_s, 1),
        "markets_analysed_all_runs": an_n,
        "fresh_markets_analysed_all_runs": sum(r.get("fresh_trades") or 0 for r in run_window),
        "markets_per_minute_measured": measured_rate,
        "markets_per_minute_fresh_estimate": fresh_rate,
        "ai_all_runs": {"labelled_or_cached": sum((r.get("ai") or {}).get("labelled") or 0 for r in run_window),
                        "calls": sum(((r.get("ai") or {}).get("client") or {}).get("requests") or 0
                                     for r in run_window),
                        "wall_s": round(sum((r.get("ai") or {}).get("wall_s") or 0 for r in run_window), 1)},
        "list_walks": store.get_meta(conn, LIST_KEY, []) or [],
        "requests_all_runs": dict(sum((Counter(r.get("requests") or {}) for r in run_window), Counter())),
    }
    if n_listed and rate_for_projection:
        measurements["backfill_projection"] = {
            "basis": (f"this window: {len(kept)} of {n_listed} listed markets kept ({len(kept) / n_listed:.1%}), "
                      f"{n_candidates} labelled; speed "
                      + (f"{fresh_rate} markets/min = {max_workers} workers / mean seconds of a fresh market"
                         if fresh_rate else f"{measured_rate} markets/min measured over the runs")
                      + f"; {BACKFILL_MARKETS:,} markets listed over {BACKFILL_DAYS} days (the 90-day walk)"),
            **project_backfill(BACKFILL_MARKETS, len(kept) / n_listed, n_candidates / n_listed, rate_for_projection),
        }
    return {
        "generated_at": iso(now_epoch()),
        "window": {"since": iso(since), "until": iso(until)},
        "cutoff_variant": variant,
        "rules": {"universe": f"closedTime in the window, volumeNum >= ${MIN_VOLUME:,}, resolved to one outcome",
                  "exclusions": ("filters.exclusion (games/sports/esports, price markets incl. an asset hitting a "
                                 "price, weather), then the AI type: " + ", ".join(sorted(ai.EXCLUDED_TYPES))
                                 + " (a market the AI could not label is kept)"),
                  "analysis": "every kept market, with method.analyze_market (cutoff variant above)",
                  "grouping": ("per (UTC close day, Gamma event); the event-day's figure is its market with the "
                               "highest flagged capital among the ok results (figure_market); an event-day with a "
                               "market still pending is 'partial' and not ranked"),
                  "ranking": f"status ok and flagged_capital >= ${MIN_FLAGGED_CAPITAL:,}, by flagged_pct desc; "
                             f"top {TOP_N}",
                  "event_row": ("store.events: main_condition_id = the same figure rule over all the event's kept "
                                "markets (every day), type = its AI type: one track-record row per event"),
                  "max_rows": max_rows},
        "complete": not pending,
        "totals": {"days": len(days), "markets_closed": n_listed, "markets_kept": len(kept),
                   "markets_analysed": len(results),
                   "excluded": dict(sum((Counter(x["excluded"]) for x in days), Counter())),
                   "ai_excluded": dict(sum((Counter(x["ai_excluded"]) for x in days), Counter())),
                   "ai_unlabelled": sum(x["ai_unlabelled"] for x in days),
                   "events_scanned": sum(x["events_scanned"] for x in days),
                   "statuses": dict(sum((Counter(x["statuses"]) for x in days), Counter())),
                   "eligible": sum(x["eligible"] for x in days), "listed": sum(x["listed"] for x in days)},
        "too_large": too_large,
        "pending": pending,
        "measurements": measurements,
        "days": days,
    }


def variant_summary(conn, since, until, variant):
    """The closed list of [since, until) under another cutoff variant, per
    day (eligible count and the listed events), or None unless that
    variant has a result for every kept market of the window (a partial
    variant would rank on other figures than the main report)."""
    kept = [cid for cid, (role, _, _) in _stored_roles(conn, since, until).items() if role == "kept"]
    if not kept or len(_headlines(conn, kept, variant)) < len(kept):
        return None
    rep = build_report(conn, since, until, variant=variant)
    return {"cutoff_variant": variant, "complete": rep["complete"], "totals": rep["totals"],
            "days": [{"day": x["day"], "eligible": x["eligible"],
                      "top5": [{k: c[k] for k in ("rank", "event_title", "figure_question", "flagged_pct",
                                                  "flagged_capital", "flagged_count", "whale_count")}
                               for c in x["top5"]]}
                     for x in rep["days"]]}


def write_report(report, path=REPORT_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    os.replace(tmp, path)
    return path


# ── CLI ──────────────────────────────────────────────────────────────────────

def _window(args):
    today = utc_day(now_epoch()) * DAY
    until = to_epoch(args.until) if args.until else today
    if until is None:
        raise SystemExit(f"unreadable --until {args.until!r}")
    if args.since:
        since = to_epoch(args.since)
        if since is None:
            raise SystemExit(f"unreadable --since {args.since!r}")
    else:
        since = until - int(args.days) * DAY
    if since >= until:
        raise SystemExit("--since must be before --until")
    return since, until


_VARIANT_SETS = {"both": method.ONE_CALL_VARIANTS, "all": method.VARIANTS}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Closed scan: score every kept market that resolved in a window and "
                                             "rank the closed list (top 5 per UTC day). Resumable.")
    ap.add_argument("--since", help="window start (UTC date or ISO time); default --until minus --days")
    ap.add_argument("--until", help="window end, exclusive (default: today 00:00 UTC, i.e. complete days only)")
    ap.add_argument("--days", type=int, default=1, help="window length in days when --since is not given (default 1)")
    ap.add_argument("--variant", default=method.DEFAULT_VARIANT, choices=[*method.VARIANTS, *_VARIANT_SETS],
                    help=f"cutoff variant (default {method.DEFAULT_VARIANT}); both = the two one-call variants, "
                         f"all = all three; the report uses the first")
    ap.add_argument("--budget-seconds", type=float, default=None,
                    help="stop starting new markets after this long (default: no limit, run to the end)")
    ap.add_argument("--workers", type=int, default=WORKERS, help=f"markets analysed at once (default {WORKERS})")
    ap.add_argument("--max-rows", type=int, default=method.MAX_ROWS, help="fills above this -> too_large")
    ap.add_argument("--max-volume", type=float, default=None, help="Gamma volume above this -> too_large, not fetched")
    ap.add_argument("--heavy-volume", type=float, default=HEAVY_VOLUME,
                    help="markets from this Gamma volume are downloaded one at a time")
    ap.add_argument("--heavy-slots", type=int, default=HEAVY_SLOTS, help="heavy markets downloaded at once")
    ap.add_argument("--min-volume", type=float, default=MIN_VOLUME)
    ap.add_argument("--list-max-age", type=float, default=LIST_MAX_AGE, help="seconds a cached window list is reused")
    ap.add_argument("--refresh-list", action="store_true", help="re-walk Gamma even when the list is cached")
    ap.add_argument("--no-ai", action="store_true", help="skip the AI type exclusion (every candidate is kept)")
    ap.add_argument("--plan", action="store_true", help="list, classify and label only; analyse nothing")
    ap.add_argument("--report-only", action="store_true", help="rebuild the report from the store; no network")
    ap.add_argument("--report", default=REPORT_PATH, help="where to write the report (default the demo file)")
    ap.add_argument("--compare", default=None, help="an earlier report: add how each day's top 5 changed")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args(argv)

    since, until = _window(args)
    variants = _VARIANT_SETS.get(args.variant, (args.variant,))
    conn = store.default_conn()
    summary = None
    if not args.report_only:
        summary = scan(since, until, variants=variants, conn=conn, budget=Budget(args.budget_seconds),
                       workers=args.workers, max_rows=args.max_rows, max_volume=args.max_volume,
                       heavy_volume=args.heavy_volume, heavy_slots=args.heavy_slots, min_volume=args.min_volume,
                       list_max_age=args.list_max_age, refresh_list=args.refresh_list, plan_only=args.plan,
                       use_ai=not args.no_ai)
    if not args.no_report:
        rep = build_report(conn, since, until, variant=variants[0], max_rows=args.max_rows)
        rep["other_variants"] = [v for v in (variant_summary(conn, since, until, o) for o in method.VARIANTS
                                             if o != variants[0]) if v]
        if args.compare:
            with open(args.compare, encoding="utf-8") as f:
                old = json.load(f)
            rep["vs_previous"] = {"previous": os.path.relpath(args.compare),
                                  "previous_variant": old.get("cutoff_variant"),
                                  "previous_generated_at": old.get("generated_at"),
                                  "days": compare_previous(old, rep, _stored_roles(conn, since, until))}
        path = write_report(rep, args.report)
        t = rep["totals"]
        log(f"report -> {path}: {t['days']} days, {t['markets_analysed']}/{t['markets_kept']} kept markets analysed, "
            f"{t['events_scanned']} event-days {t['statuses']}, {t['eligible']} eligible, {t['listed']} listed; "
            f"complete={rep['complete']}")
        for x in rep["days"]:
            log(f"  {x['day']}: {x['events_scanned']:>3} events, {x['eligible']:>2} eligible, top: "
                + "; ".join(f"{c['flagged_pct']:.0f}% {str(c['event_title'])[:40]}" for c in x["top5"]))
        proj = rep["measurements"].get("backfill_projection")
        if proj:
            log(f"  backfill projection ({proj['days']} days): {proj['markets_analysed']:,} markets at "
                f"{proj['markets_per_minute']} /min + {proj['ai_calls']} AI calls -> ~{proj['hours_total']} h")
    return summary


if __name__ == "__main__":
    main()
