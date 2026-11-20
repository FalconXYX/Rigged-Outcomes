"""
Every Polymarket request the watchlist makes, rate-limited.

Rate limits (docs.polymarket.com, per IP, per 10 s): Gamma /markets 300,
/events 500; Data API v2 general 800, /v2/trades 300, /v2/activity and
/v2/positions 200. The docs disagree on what happens above them (the
request is throttled, or answered 429 with Retry-After), so this module
handles both: a token bucket per endpoint family keeps each process at
SHARE (40 %) of the documented limit, and polymarket_api.get_json already
honours Retry-After / backs off on 429 and 5xx. The buckets are shared by
all threads of the process; inside a step of the Vercel function each job
has its own (backend.outcomes.per_job), since several calls can share one
process.

Inside a step (backend.outcomes.DEADLINE set) nothing here waits longer than
the step can: requests are cut to the deadline (polymarket_api.get_json),
thread pools carry the step's deadline into their threads, and patiently()
does not sleep through an outage but raises backend.outcomes.Later
("polymarket_down", CONTRACT.retry.polymarket_down_s), so the job comes back
later instead of holding a Vercel call while Polymarket is down.

Every request goes through get_json() here. install() additionally routes
polymarket_api.get_json through the buckets, so the legacy fetchers this
package reuses (polymarket_api.fetch_trades -> v2_walk) are limited too.
(Offline tests that fake polymarket_api.get_json patch install() too, or
install() would capture the fake.)

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
"""
import threading
import time

from pipeline import polymarket_api
from backend import outcomes
from backend.contract import RETRY
from pipeline.polymarket_api import DATA_API_V2, GAMMA_API, PolymarketAPIError

from watchlist import problems
from watchlist.common import log, now_epoch, to_epoch

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

MAX_PAGES = 5000          # safety net on any keyset walk here


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


def new_buckets():
    """A token bucket per endpoint family."""
    return {name: TokenBucket(limit * SHARE / 10.0) for name, limit in LIMITS_PER_10S.items()}


BUCKETS = new_buckets()             # a command's (outside a step)


def buckets():
    """The buckets requests go through: the job's own while a step runs, else the process's."""
    return outcomes.per_job("net_buckets", new_buckets, default=BUCKETS)


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
    buckets()[family(url)].acquire()
    return _upstream()(url, params=params, timeout=timeout, retry=retry)


def install():
    """Route polymarket_api.get_json (and so fetch_trades / v2_walk /
    fetch_market) through the buckets. Idempotent."""
    if "orig" in _INSTALLED:
        return
    orig = polymarket_api.get_json

    def limited(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        buckets()[family(url)].acquire()
        return orig(url, params=params, timeout=timeout, retry=retry)

    _INSTALLED["orig"] = orig
    polymarket_api.get_json = limited


def bucket_stats():
    """{family: {"requests", "waited_s"}} since import (for run summaries)."""
    return {k: {"requests": b.taken, "waited_s": round(b.waited, 1)} for k, b in buckets().items() if b.taken}


# A whole walk asked again after each of these waits when Polymarket still fails it once get_json's own retries
# (about 30 s) are spent: about 7.5 minutes in all, so a short outage never costs a list its day.
WALK_WAITS = (30, 120, 300)


def patiently(fn, what="the walk", waits=None, sleep=time.sleep):
    """fn() asked again after each of waits (default WALK_WAITS) while it raises PolymarketAPIError. An error
    waiting will not cure raises at once: an endpoint retired, or a status that says the request is wrong (a
    400 or a 404: problems.polymarket_kind). A feed that came back in a shape the code does not know is asked
    again (a glitch passes; a change raises after the waits). The last error raises. Inside a step there are no
    waits: an error waiting could cure raises Later("polymarket_down") at once."""
    waits = WALK_WAITS if waits is None else tuple(waits)
    for attempt in range(len(waits) + 1):
        try:
            return fn()
        except polymarket_api.RetiredEndpointError:
            raise
        except PolymarketAPIError as e:
            wrong = getattr(e, "status", None) is not None and problems.polymarket_kind(e) == problems.CRASH
            if not wrong and outcomes.left() is not None:
                log(f"  ! {what}: Polymarket did not answer ({str(e)[:160]}); the job comes back in "
                    f"{RETRY['polymarket_down_s']} s")
                raise outcomes.Later.after(RETRY["polymarket_down_s"], "polymarket_down") from e
            if attempt == len(waits) or wrong:
                raise
            log(f"  ! {what}: Polymarket did not answer ({str(e)[:160]}); asking again in {waits[attempt]} s")
            sleep(waits[attempt])


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
