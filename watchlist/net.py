"""
Every Polymarket request the watchlist makes, rate-limited.

Rate limits (docs.polymarket.com, per IP, per 10 s): Gamma /markets 300,
/events 500; Data API v2 general 800, /v2/trades 300, /v2/activity and
/v2/positions 200. The docs disagree on what happens above them (the
request is throttled, or answered 429 with Retry-After), so this module
handles both: a token bucket per endpoint family keeps each process at
SHARE (40 %) of the documented limit, and polymarket_api.get_json already
honours Retry-After / backs off on 429 and 5xx. The buckets are shared by
all threads of the process.

Every request goes through get_json() here. install() additionally routes
polymarket_api.get_json through the buckets, so the legacy fetchers this
package reuses (polymarket_api.fetch_trades -> v2_walk) are limited too;
method.py calls it before a trade download and day_trades before its
/v2/activity walk. (Offline tests that fake polymarket_api.get_json patch
install() too, or install() would capture the fake.)

Endpoint facts (verified 2026-11-09/10):

  Closed markets  GET {GAMMA}/markets/keyset?closed=true&order=updatedAt
      &ascending=false&volume_num_min=…&include_tag=true&limit=100
      [&after_cursor=…] -> {"markets": [...], "next_cursor": …}.
      Why updatedAt and not closedTime (verified 2026-11-10):
      - The offset list (/markets?offset=N) stops at offset 2000 (2100 is a
        422 "offset too large, use /markets/keyset for deeper pagination"):
        ~8 days of $100k closes, so it cannot serve a 90-day window.
      - There is no closedTime filter (closed_time_min and friends are
        silently ignored) and endDate is no proxy: 44 % of $100k markets
        close before it, by up to 425 days, others up to 156 days after,
        and some have no endDate at all.
      - order=closedTime on the keyset list gets slower with every page
        back in time, whatever the other filters (a $100k-130k band failed
        at the same date as the full list): ~2 s a page three weeks back,
        then a deterministic HTTP 500 (a server timeout; retrying the same
        cursor, a smaller limit or dropping include_tag all fail). That
        walk never got past ~2026-10-19.
      - order=updatedAt pages in ~0.05-0.2 s at any depth, and closing a
        market updates it, so updatedAt >= closedTime (0 exceptions in
        3,500 markets): walking updatedAt down to `since` sees every
        market that closed since then. Over the last 14 days it found
        3,435 of the closedTime walk's 3,436; the one missed was updated
        DURING the walk (a row updated mid-walk jumps to the top, behind
        the cursor), which is why closed_markets re-reads the top until
        it reaches the walk's start time.
      include_tag=true adds the market's tag list (events[0] carries
      series/seriesSlug/gameId but no tags in this list).
  Open events     GET {GAMMA}/events/keyset?closed=false&limit=100
      &volume_min=…[&after_cursor=…] -> {"events": [...], "next_cursor"}.
      volume_min filters on EVENT volume, which is at least any of its
      markets' volume, so volume_min=100000 loses no $100k market:
      measured 2026-11-10, 10 pages / 4 s and the same 1,569 open $100k
      markets as the unfiltered walk (192 pages, 20 s, 1.7 GB). Events
      also hold their closed markets; filters.pool_reason drops those.
  Trades          GET {DATA_V2}/trades?condition=…&taker_only=false
      &limit=1000[&cursor=…], newest first; start/end are ignored for the
      condition shape, so a window walk pages until a row is older than
      the window start.
  User volume     GET {DATA_V2}/user-volume?user=<0x40hex>&start=1&end=E
      -> {"data": {"volume" (shares), "volume_usdc", "trade_count"}}. The
      bounds are floored to whole UTC days and the end day is INCLUDED:
      every E on one UTC day gives the same answer, E one second before
      that day's midnight gives the previous day's. A malformed address is
      answered 200 with zeros (so addresses are validated here first); a
      protocol contract address is a 400. A null or non-numeric field
      raises (parse_user_volume): only a real 0 is a zero.
  First-day fills GET {DATA_V2}/activity?user=…&type=TRADE&start=<00:00 UTC>
      &end=<cutoff>&limit=1000 (bounds inclusive, to the second): the part
      of the cutoff's day user-volume cannot cut. exact_prior = user-volume
      to the day before + these fills = the activity crawl at the exact
      cutoff, counting fills, for ~1 page instead of the whole history.
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import polymarket_api
from polymarket_api import DATA_API_V2, GAMMA_API, OPERATORS, PAGE_SIZE, PolymarketAPIError

from watchlist import store
from watchlist.common import DAY, log, now_epoch, to_epoch, utc_day

# Documented requests per 10 s per IP, by endpoint family.
LIMITS_PER_10S = {
    "gamma_markets": 300,
    "gamma_events": 500,
    "gamma_other": 300,      # not documented separately; treated like /markets
    "data_trades": 300,
    "data_activity": 200,    # /v2/activity, /v2/positions
    "data_general": 800,     # everything else on Data v2 (user-volume, holders, …)
}
SHARE = 0.40

# A user-volume answer for a day that is not over yet can still change;
# such rows are re-fetched once they are older than this.
INCOMPLETE_MAX_AGE = 600
# A day counts as over (its answer final) this long after its UTC midnight.
SETTLE_SECONDS = 3600

MAX_PAGES = 5000          # safety net on any keyset walk here
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


class TokenBucket:
    """Thread-safe token bucket: `rate` tokens a second, bursts up to `burst`."""

    def __init__(self, rate, burst=None):
        self.rate = float(rate)
        self.burst = float(burst if burst is not None else max(1.0, rate))
        self.tokens = self.burst
        self.stamp = time.monotonic()
        self.lock = threading.Lock()
        self.waited = 0.0
        self.taken = 0

    def acquire(self, n=1.0):
        """Take n tokens, sleeping until they are available."""
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
                self.stamp = now
                if self.tokens >= n:
                    self.tokens -= n
                    self.taken += 1
                    return
                wait = (n - self.tokens) / self.rate
            self.waited += wait
            time.sleep(wait)


BUCKETS = {name: TokenBucket(limit * SHARE / 10.0) for name, limit in LIMITS_PER_10S.items()}


def family(url):
    """The rate-limit family of a request URL."""
    u = str(url)
    if u.startswith(GAMMA_API):
        path = u[len(GAMMA_API):]
        if path.startswith("/events"):
            return "gamma_events"
        if path.startswith("/markets"):
            return "gamma_markets"
        return "gamma_other"
    if u.startswith(DATA_API_V2):
        path = u[len(DATA_API_V2):]
        if path.startswith("/trades"):
            return "data_trades"
        if path.startswith("/activity") or path.startswith("/positions"):
            return "data_activity"
        return "data_general"
    return "gamma_other"


# ── transport ────────────────────────────────────────────────────────────────

_INSTALLED = {}


def _upstream():
    """The unlimited get_json: the original when install() has patched
    polymarket_api, else whatever polymarket_api.get_json is right now (so
    tests can fake it)."""
    return _INSTALLED.get("orig") or polymarket_api.get_json


def get_json(url, params=None, timeout=30, retry=polymarket_api.RETRY):
    """polymarket_api.get_json behind the family's token bucket."""
    BUCKETS[family(url)].acquire()
    return _upstream()(url, params=params, timeout=timeout, retry=retry)


def install():
    """Route polymarket_api.get_json (and so fetch_trades / v2_walk /
    fetch_market) through the buckets. Idempotent."""
    if "orig" in _INSTALLED:
        return
    orig = polymarket_api.get_json

    def limited(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        BUCKETS[family(url)].acquire()
        return orig(url, params=params, timeout=timeout, retry=retry)

    _INSTALLED["orig"] = orig
    polymarket_api.get_json = limited


def uninstall():
    """Undo install() (tests)."""
    orig = _INSTALLED.pop("orig", None)
    if orig is not None:
        polymarket_api.get_json = orig


def bucket_stats():
    """{family: {"requests", "waited_s"}} since import (for run summaries)."""
    return {k: {"requests": b.taken, "waited_s": round(b.waited, 1)} for k, b in BUCKETS.items() if b.taken}


# ── Gamma keyset walks ───────────────────────────────────────────────────────

def _keyset(path, params, key, stop=None, on_page=None, max_pages=MAX_PAGES):
    """Follow a Gamma keyset feed. Yields pages (lists). stop(page) -> True
    ends the walk after that page.

    The feed ends on a page WITHOUT next_cursor: the last rows, or an empty
    page when the total is a multiple of the limit (verified 2026-11-10:
    18 events at limit 6 give 6/6/6 with a cursor, then 0 without). An
    empty page that still carries a next_cursor is not an end: it is asked
    again once, and if it is still empty with a cursor the walk raises
    instead of stopping silently on a truncated list. A repeated cursor
    raises too."""
    cursor, seen, pages = None, set(), 0
    while True:
        p = dict(params)
        if cursor:
            p["after_cursor"] = cursor
        for attempt in (1, 2):
            payload = get_json(f"{GAMMA_API}{path}", p)
            if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                raise PolymarketAPIError(f"{path}: unexpected response shape: {str(payload)[:200]}", payload=payload)
            page = payload[key]
            nxt = payload.get("next_cursor") or None
            if page or not nxt:
                break
            if attempt == 2:
                raise PolymarketAPIError(f"{path}: page {pages + 1} came back empty twice but still carries a "
                                         f"next_cursor; the walk would end on a truncated list", payload=payload)
            log(f"  ! {path}: page {pages + 1} is empty but carries a next_cursor; asking again")
        pages += 1
        if on_page:
            on_page(pages, page)
        yield page
        cursor = nxt
        if not cursor or not page or (stop and stop(page)):
            return
        if cursor in seen:
            raise PolymarketAPIError(f"{path}: page {pages} repeated an earlier cursor; the feed is not advancing")
        seen.add(cursor)
        if pages >= max_pages:
            raise PolymarketAPIError(f"{path}: {pages} pages without reaching the end; giving up (safety net)")


def walk_open_events(min_volume=100_000, on_page=None):
    """Every open Gamma event with volume >= min_volume (None = all), with
    its markets (open and closed). ~10 pages / 5 s at $100k."""
    params = {"closed": "false", "limit": 100}
    if min_volume:
        params["volume_min"] = int(min_volume)
    events, ids = [], set()
    for page in _keyset("/events/keyset", params, "events", on_page=on_page):
        for e in page:
            eid = str(e.get("id"))
            if eid not in ids:
                ids.add(eid)
                events.append(e)
    return events


def closed_markets(since, until=None, min_volume=100_000, include_tags=True, on_page=None):
    """Markets whose closedTime is in [since, until) with volumeNum >=
    min_volume, newest close first. since/until: epoch seconds or ISO.

    Walks /markets/keyset by updatedAt descending (see the module docstring
    for why not closedTime) down to the first page older than `since`, then
    re-reads the top of the feed until it reaches the walk's own start time,
    to catch markets updated while the walk was running. Cost: the markets
    updated since `since` (~3,800 for 14 days, ~40 pages, a few seconds;
    a 90-day window is a few hundred pages). One call per window, not per
    day: bucket the result by closedTime yourself. Markets without a
    closedTime are skipped; a market whose closedTime is after its
    updatedAt (never seen) is logged, since this walk could miss such rows."""
    lo = to_epoch(since)
    hi = to_epoch(until) if until is not None else None
    if lo is None:
        raise ValueError(f"closed_markets: unreadable since={since!r}")
    params = {"closed": "true", "order": "updatedAt", "ascending": "false", "limit": 100}
    if min_volume:
        params["volume_num_min"] = int(min_volume)
    if include_tags:
        params["include_tag"] = "true"

    out, ids, odd = [], set(), 0

    def take(page):
        nonlocal odd
        for m in page:
            ts = to_epoch(m.get("closedTime"))
            up = to_epoch(m.get("updatedAt"))
            if ts is not None and up is not None and ts > up + 1:
                odd += 1
            if ts is None or ts < lo or (hi is not None and ts >= hi):
                continue
            if min_volume and float(m.get("volumeNum") or 0) < min_volume:
                continue
            mid = str(m.get("id"))
            if mid not in ids:
                ids.add(mid)
                out.append(m)

    def older_than(bound):
        def stop(page):
            last = to_epoch(page[-1].get("updatedAt"))
            return last is not None and last < bound
        return stop

    started = now_epoch() - 5
    for page in _keyset("/markets/keyset", params, "markets", stop=older_than(lo), on_page=on_page):
        take(page)
    for page in _keyset("/markets/keyset", params, "markets", stop=older_than(started), on_page=on_page):
        take(page)
    if odd:
        log(f"  ! closed_markets: {odd} market(s) with closedTime after updatedAt; the updatedAt walk can miss "
            f"such rows")
    out.sort(key=lambda m: -(to_epoch(m.get("closedTime")) or 0))
    return out


def fetch_event(event_id):
    """One Gamma event (with all its markets) by id."""
    return get_json(f"{GAMMA_API}/events/{event_id}")


def markets_by_condition(condition_ids, closed="any"):
    """Gamma market objects for condition ids (50 per request). closed:
    "any" (default: open and closed; the list hides closed markets unless
    asked), True or False."""
    out, ids = [], [c for c in condition_ids if c]
    flag = closed if isinstance(closed, str) else ("true" if closed else "false")
    for i in range(0, len(ids), 50):
        params = [("condition_ids", c) for c in ids[i:i + 50]] + [("limit", 100), ("closed", flag)]
        page = get_json(f"{GAMMA_API}/markets", params)
        if not isinstance(page, list):
            raise PolymarketAPIError(f"/markets?condition_ids: unexpected response shape: {str(page)[:200]}")
        out.extend(page)
    return out


# ── Data API v2: recent trades ───────────────────────────────────────────────

def recent_trades(condition_id, since, until=None, taker_only=False, max_rows=None):
    """(rows, capped): the market's v2 trade rows with since <= timestamp <
    until (epoch or ISO), newest first, CLOB operator rows dropped (like
    polymarket_api.fetch_trades). Pages newest-first and stops at the first
    page whose oldest row is older than `since`, so the cost is the window's
    trade count, not the market's. max_rows caps the rows kept (announced)."""
    lo, hi = to_epoch(since), to_epoch(until) if until is not None else None
    if lo is None:
        raise ValueError(f"recent_trades: unreadable since={since!r}")
    cid = str(condition_id or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", cid):
        raise ValueError(f"not a condition id: {condition_id!r}")
    params = {"condition": cid, "taker_only": "true" if taker_only else "false", "limit": PAGE_SIZE}
    rows, cursor, seen, pages = [], None, set(), 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        payload = get_json(f"{DATA_API_V2}/trades", p)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PolymarketAPIError(f"/v2/trades: unexpected response shape: {str(payload)[:200]}", payload=payload)
        page = payload["data"]
        pages += 1
        oldest = None
        for r in page:
            ts = int(r.get("timestamp") or 0)
            oldest = ts if oldest is None else min(oldest, ts)
            if ts < lo or (hi is not None and ts >= hi):
                continue
            w = str(r.get("proxy_wallet") or "").lower()
            if w and w not in OPERATORS:
                rows.append(r)
        if max_rows and len(rows) >= max_rows:
            log(f"  ! /v2/trades {cid[:10]}…: window capped at {max_rows:,} rows; older rows in the window NOT fetched")
            return rows[:max_rows], True
        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            raise PolymarketAPIError(f"/v2/trades: page {pages} carries no pagination object")
        if oldest is not None and oldest < lo:
            return rows, False
        if pagination.get("has_more") is not True:
            return rows, False
        cursor = pagination.get("next_cursor") or None
        if not cursor:
            raise PolymarketAPIError(f"/v2/trades: page {pages} says has_more but sent no next_cursor")
        if cursor in seen:
            raise PolymarketAPIError(f"/v2/trades: page {pages} repeated an earlier cursor")
        seen.add(cursor)
        if pages >= MAX_PAGES:
            raise PolymarketAPIError(f"/v2/trades: {pages} pages inside one window; giving up (safety net)")


# ── Data API v2: prior history in one call ───────────────────────────────────

def _day_settled(day, now):
    return now >= (day + 1) * DAY + SETTLE_SECONDS


def _amount(data, key, integral=False):
    """data[key] as a finite number >= 0 (an int when integral), or None when
    it is missing, null, a bool, non-numeric, negative or fractional where a
    count is expected. Numeric strings are accepted (the value, not the JSON
    type, is what counts)."""
    v = data.get(key)
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")) or x < 0:
        return None
    if integral:
        return int(x) if x == int(x) else None
    return x


def parse_user_volume(payload, wallet=""):
    """{"volume", "volume_usdc", "trade_count"} from a /v2/user-volume answer.
    Raises PolymarketAPIError unless all three are present and numeric: only
    a real 0 is a zero. A null, a string or a missing field is an error, and
    must never become a "new wallet" (0 prior trades flags every whale)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise PolymarketAPIError(f"/v2/user-volume: unexpected response shape for {wallet}: {str(payload)[:200]}",
                                 payload=payload)
    out = {"volume": _amount(data, "volume"), "volume_usdc": _amount(data, "volume_usdc"),
           "trade_count": _amount(data, "trade_count", integral=True)}
    bad = [k for k, v in out.items() if v is None]
    if bad:
        raise PolymarketAPIError(f"/v2/user-volume: {', '.join(bad)} null, missing or not a number for {wallet}: "
                                 f"{str(payload)[:200]}", payload=payload)
    return out


def user_volume(wallet, end_epoch, conn=None, refresh=False):
    """The wallet's lifetime trading up to and including the UTC day of
    end_epoch: {"volume" (shares), "volume_usdc", "trade_count", "day",
    "cached"}. Answers are cached in the store per (wallet, day); a settled
    day is never fetched twice. Raises on a malformed address (the endpoint
    would answer zeros) and on any API error or null/non-numeric field
    (parse_user_volume: never a silent zero)."""
    w = str(wallet or "").strip().lower()
    if not _ADDRESS.match(w):
        raise ValueError(f"not a wallet address: {wallet!r}")
    end = int(end_epoch)
    day = utc_day(end)
    conn = conn or store.default_conn()
    now = now_epoch()
    if not refresh:
        row = store.get_wallet_volume(conn, w, day)
        if row and (row["complete"] or now - row["fetched_at"] < INCOMPLETE_MAX_AGE):
            return {"volume": row["volume"], "volume_usdc": row["volume_usdc"], "trade_count": row["trade_count"],
                    "day": day, "cached": True}
    payload = get_json(f"{DATA_API_V2}/user-volume", {"user": w, "start": 1, "end": end})
    data = parse_user_volume(payload, w)
    store.put_wallet_volume(conn, w, day, data, complete=_day_settled(day, now))
    return {**data, "day": day, "cached": False}


def user_volumes(pairs, conn=None, workers=10):
    """user_volume for many (wallet, end_epoch) pairs in parallel. Returns a
    list in the input order; the first error raises (after the pool stops)."""
    conn = conn or store.default_conn()
    pairs = list(pairs)
    if not pairs:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pairs)))) as pool:
        futures = [pool.submit(user_volume, w, e, conn) for w, e in pairs]
        try:
            return [f.result() for f in futures]
        except BaseException:
            for f in futures:
                f.cancel()
            raise


# ── the exact cutoff: user-volume to the day before + the first day's fills ──

# Page cap of the first-day walk: polymarket_api.fetch_user_history_up_to's
# own cap (50 x 1000 fills), so a capped wallet is a floor exactly where
# scrape_insiders' crawl would be one.
DAY_MAX_PAGES = polymarket_api.HISTORY_MAX_PAGES


def day_trades(wallet, cutoff, conn=None, refresh=False, max_pages=DAY_MAX_PAGES):
    """The wallet's TRADE fills from 00:00 UTC of cutoff's day up to and
    including cutoff (the bound scrape_insiders' crawl uses):
    {"trade_count" (fills), "tx_count" (unique transaction hashes),
    "volume_usdc", "capped", "day_start", "cached", "requests"}.

    One /v2/activity?type=TRADE&start=<midnight>&end=<cutoff> walk, usually
    one page; cached in the store per (wallet, cutoff) once the cutoff is
    SETTLE_SECONDS old. Rows are re-checked for type TRADE and the bounds,
    as fetch_user_history_up_to does. A capped walk (more than max_pages x
    1000 fills that day) gives floors and says so. Errors raise. Goes through
    the rate-limit buckets (install())."""
    w = str(wallet or "").strip().lower()
    if not _ADDRESS.match(w):
        raise ValueError(f"not a wallet address: {wallet!r}")
    cut = int(cutoff)
    d0 = utc_day(cut) * DAY
    conn = conn or store.default_conn()
    now = now_epoch()
    if not refresh:
        row = store.get_wallet_day_trades(conn, w, cut)
        if row and (row["complete"] or now - row["fetched_at"] < INCOMPLETE_MAX_AGE):
            return {"trade_count": row["trade_count"], "tx_count": row["tx_count"], "volume_usdc": row["volume_usdc"],
                    "capped": bool(row["capped"]), "day_start": d0, "cached": True, "requests": 0}
    install()
    raw, capped = polymarket_api.v2_walk("activity", {"user": w, "type": "TRADE", "start": d0, "end": cut,
                                                      "limit": PAGE_SIZE}, max_pages=max_pages, announce=False)
    rows = [polymarket_api.as_v1(r) for r in raw
            if (r.get("type") or "TRADE") == "TRADE" and d0 <= int(r.get("timestamp") or 0) <= cut]
    if capped:
        log(f"  ! first-day fills of {w} capped at {len(rows):,} ({max_pages} pages) before {cut}; counts are floors")
    out = {"trade_count": len(rows), "tx_count": len({h["transactionHash"] for h in rows}),
           "volume_usdc": round(sum(h["usdcSize"] for h in rows), 6), "capped": bool(capped), "day_start": d0,
           "cached": False, "requests": max(1, -(-len(raw) // PAGE_SIZE))}
    store.put_wallet_day_trades(conn, w, cut, d0, out["trade_count"], out["tx_count"], out["volume_usdc"], capped,
                                complete=now >= cut + SETTLE_SECONDS)
    return out


def exact_prior(wallet, first_ts, conn=None):
    """The wallet's prior history at the exact cutoff (its first trade,
    inclusive), counting fills: /v2/user-volume up to the day before the
    first trade + day_trades for the first-trade day up to the trade.
    {"trade_count", "volume_usdc", "day_rows", "day_tx", "day_usdc",
    "base_trade_count", "base_volume_usdc", "capped", "cached", "requests"}
    (the keys of the former parity.hybrid_prior, plus the parts)."""
    first_ts = int(first_ts)
    base = user_volume(wallet, first_ts - DAY, conn=conn)
    day = day_trades(wallet, first_ts, conn=conn)
    return {"trade_count": int(base["trade_count"]) + day["trade_count"],
            "volume_usdc": round(float(base["volume_usdc"]) + day["volume_usdc"], 4),
            "day_rows": day["trade_count"], "day_tx": day["tx_count"], "day_usdc": round(day["volume_usdc"], 4),
            "base_trade_count": int(base["trade_count"]), "base_volume_usdc": round(float(base["volume_usdc"]), 4),
            "capped": day["capped"], "cached": bool(base["cached"] and day["cached"]),
            "requests": (0 if base["cached"] else 1) + day["requests"]}


def exact_priors(pairs, conn=None, workers=4):
    """exact_prior for many (wallet, first_ts) pairs in parallel, in input
    order; the first error raises (after the pool stops)."""
    conn = conn or store.default_conn()
    pairs = list(pairs)
    if not pairs:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pairs)))) as pool:
        futures = [pool.submit(exact_prior, w, t, conn) for w, t in pairs]
        try:
            return [f.result() for f in futures]
        except BaseException:
            for f in futures:
                f.cancel()
            raise
