#!/usr/bin/env python3
"""
polymarket_api.py — the one Polymarket client the pipeline shares.

Polymarket retired the Data API v1 (`/activity?user=…`, HTTP 410 "Data API v1
has been retired") and the Goldsky orderbook subgraph (429 "paused and
deprecated") in November 2026. Everything that fetches from Polymarket now
goes through this module: polymarket_trades.py (raw trades), scrape_insiders.py
(prior history) and the watchlist package (watchlist.net, watchlist.method)
import from here, and nothing here imports them, so there is no import cycle.

Endpoints and the fields observed on 2026-11-05 (full spec:
https://data-api.polymarket.com/v2/openapi.json, saved alongside the parity
test's scratch output):

Gamma  GET https://gamma-api.polymarket.com/markets/slug/<slug>
    One market object. Used fields: conditionId, question, closed, outcomes
    (JSON-encoded list), outcomePrices (JSON-encoded list of strings, "1"/"0"
    once resolved), clobTokenIds, endDate, volumeNum, umaResolutionStatus.
    404 {"type":"not found error","error":"slug not found"} for an unknown
    slug, 422 {"error":"slug is invalid"} for a slug with path characters.
    The list endpoint GET /markets?slug=<slug> hides closed markets unless
    closed=true is passed, which is why the slug endpoint is tried first.

Data API v2  GET https://data-api.polymarket.com/v2/trades
    ?condition=<0x…64 hex>&taker_only=false&limit=1000[&cursor=…]
    Every fill in the market, newest first, keyset-paginated: the response is
    {"data":[…], "pagination":{"limit","offset","has_more","next_cursor"}};
    pass next_cursor back until has_more is false. limit max 1000 (a larger
    value is a 400). taker_only defaults to true (each fill served once, on
    its taker side); false adds the maker rows, which is what the retired
    per-wallet crawl saw and what the committed CSVs hold. The condition
    shape serves a fixed three-year window; start/end are honoured on the
    user shape only. Row fields: proxy_wallet, side (BUY|SELL), token_id,
    condition_id, size (shares), price, timestamp (epoch seconds),
    title, slug, event_slug, icon, outcome, outcome_index, name, pseudonym,
    bio, profile_image, profile_image_optimized, transaction_hash. There is
    no usdc_size and no per-fill id. A malformed condition id is a 400
    {"error":"invalid condition id: …","code":"invalid_request",
    "parameter":"condition","retryable":false,"trace_id":…}; a well-formed
    one that matches nothing is an empty data array.

Data API v2  GET https://data-api.polymarket.com/v2/activity
    ?user=<0x…40 hex>&type=TRADE&start=1&end=<cutoff>&limit=1000[&cursor=…]
    The wallet's activity, newest first, same envelope and cursor paging.
    start/end are inclusive bounds on the block timestamp (verified: a row
    at timestamp == end is returned, end-1 drops it); start=1 asks for full
    history (omitted or 0 floors to three years back). type filters
    server-side (TRADE, SPLIT, MERGE, REDEEM, …); without it DEPOSIT/
    WITHDRAWAL rows are dropped unless exclude_deposits_withdrawals=false.
    TRADE rows carry the trade fields above plus type and usdc_size (the
    dollar leg; equals size x price to within rounding). A malformed address
    is NOT an error: the feed answers 200 with an empty data array, so
    addresses are validated here before the request. The type filter is
    applied server-side AND re-checked here, as the v1 scraper did.

Fees. On the seven committed (fee-free) markets v2 `size x price` equals the
old v1 `usdcSize` to within $0.0001. On a FEE-BEARING market the two feeds
differ: /v2/trades serves a taker BUY's size net of the taker fee (observed
0.969-0.998x on "Bitcoin Up or Down" fills), while /v2/activity serves the
gross size and usdc_size = gross size x price, which is the v1 convention.
So a raw CSV built from /v2/trades records ~1-3 % fewer contracts and
dollars per taker BUY on such a market than the old crawl would have, and
prior-history stakes (from /v2/activity) stay gross. Nothing here converts
between the two; if exact v1 parity matters on a fee-bearing market, build
the rows from /v2/activity?condition=<id>&type=TRADE per wallet instead.

Error handling: an error payload (a dict carrying "error"/"errors", whatever
the status) raises PolymarketAPIError; HTTP 410 or a "retired" message raises
RetiredEndpointError; 429 and 5xx back off (Retry-After when sent, else
exponential) and retry up to RETRY times before raising. Nothing here ever
turns an error into "no data". A row/page cap is reported on stdout and
returned as `capped`, never applied silently, and a feed that stops making
progress (missing pagination, has_more without a cursor, a repeated cursor,
or empty pages that claim more) raises instead of ending the walk quietly.
"""
import re
import time
from urllib.parse import quote

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API_V2 = "https://data-api.polymarket.com/v2"

PAGE_SIZE = 1000          # v2 max page size on both feeds
RETRY = 5                 # attempts per request on 429 / 5xx / network errors
MAX_BACKOFF = 60.0        # seconds; Retry-After above this is still honoured
# Prior-history cap per wallet: HISTORY_MAX_PAGES x 1000 trades closest BEFORE
# the cutoff. The retired v1 scraper stopped at offset 15 000 and that cap bit
# on most large whales (their prior_bet_count was a floor and their
# concentration overstated); 50 pages (~0.25 s each) covers every wallet seen
# so far. A capped wallet is announced; its counts are floors.
HISTORY_MAX_PAGES = 50
# Safety net on any cursor walk without an explicit cap: 20 000 pages = 20 M
# rows, far beyond any market seen (USxIran, the largest, is ~140 k rows).
# Hitting it is announced and reported as capped, like the explicit caps.
WALK_MAX_PAGES = 20_000
# A feed that claims has_more but serves this many empty pages in a row is
# broken; the walk raises rather than spinning.
MAX_EMPTY_PAGES = 3

# Known CLOB operator/router addresses: infrastructure, not traders.
OPERATORS = {
    "0x4bfb41d5b357570702fe9f19e29c4a26df9a28d6",
}

USER_AGENT = "rigged-outcomes/1.0 (+https://github.com/FalconXYX/Rigged-Outcomes)"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CONDITION = re.compile(r"^0x[0-9a-fA-F]{64}$")
_NUMBER = re.compile(r"\d+(\.\d+)?")


class PolymarketAPIError(RuntimeError):
    """The API answered with an error (status or payload), or retries ran out."""

    def __init__(self, message, status=None, payload=None, url=None):
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.url = url


class RetiredEndpointError(PolymarketAPIError):
    """HTTP 410 / 'retired' payload: the endpoint is gone and retrying will not help."""


def log(msg):
    print(msg, flush=True)


def _error_message(payload):
    """The error text of an error payload, or None for a normal one."""
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        return str(payload["error"])
    errs = payload.get("errors")
    if errs:
        if isinstance(errs, list):
            return "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errs)
        return str(errs)
    return None


def _backoff(resp, attempt):
    wait = (resp.headers.get("Retry-After") or "").strip()
    if _NUMBER.fullmatch(wait):
        return float(wait)
    return min(MAX_BACKOFF, 2.0 ** attempt)


def get_json(url, params=None, timeout=30, retry=RETRY):
    """GET and decode JSON. Raises PolymarketAPIError on any error payload or
    HTTP error, RetiredEndpointError on 410/"retired", and retries 429/5xx and
    network failures with backoff. Never returns an error payload as data."""
    last = None
    for attempt in range(retry):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last = e
            time.sleep(min(MAX_BACKOFF, 1.0 + attempt))
            continue
        status = resp.status_code
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        err = _error_message(payload)
        retryable = isinstance(payload, dict) and payload.get("retryable") is True

        if status == 410 or (err and "retired" in err.lower()):
            raise RetiredEndpointError(f"{url} has been retired by Polymarket: {err or resp.text[:200]}",
                                       status=status, payload=payload, url=url)
        if status == 429 or status >= 500 or (retryable and not resp.ok):
            last = PolymarketAPIError(f"HTTP {status} from {url}: {err or resp.text[:200]}",
                                      status=status, payload=payload, url=url)
            time.sleep(_backoff(resp, attempt))
            continue
        if not resp.ok:
            raise PolymarketAPIError(f"HTTP {status} from {url}: {err or resp.text[:200]}",
                                     status=status, payload=payload, url=url)
        if err:
            raise PolymarketAPIError(f"{url} answered with an error payload: {err}",
                                     status=status, payload=payload, url=url)
        if payload is None:
            last = PolymarketAPIError(f"{url} answered with non-JSON: {resp.text[:200]}", status=status, url=url)
            time.sleep(min(MAX_BACKOFF, 1.0 + attempt))
            continue
        return payload
    raise PolymarketAPIError(f"gave up on {url} after {retry} attempts: {last}",
                             status=getattr(last, "status", None), url=url)


def v2_walk(path, params, max_rows=None, max_pages=None, on_progress=None, announce=True):
    """Follow a v2 cursor feed to the end (or to a cap). Returns (rows, capped).

    `path` is "trades" or "activity"; `params` the first-page query (limit
    included). on_progress(rows_so_far, pages) is called after every page.
    A cap (max_rows, max_pages or the WALK_MAX_PAGES safety net) is announced
    on stdout (unless announce=False, for a caller that prints its own, more
    specific notice) and reported as capped=True; the rows returned are the
    newest ones, so what is missing is the OLDEST part of the feed.

    The walk ends normally only on a page whose pagination says has_more is
    false (the live feed always sends {"has_more": false, "next_cursor":
    null} on the last page, empty results included). Anything else that
    would otherwise end or stall the walk raises PolymarketAPIError: a page
    without a pagination object, has_more true without a next_cursor, a
    cursor the feed already served, or MAX_EMPTY_PAGES consecutive empty
    pages that still claim more."""
    rows, cursor, pages, seen, empty_run = [], None, 0, set(), 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        payload = get_json(f"{DATA_API_V2}/{path}", p)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PolymarketAPIError(f"/v2/{path}: unexpected response shape: {str(payload)[:200]}", payload=payload)
        page = payload["data"]
        rows.extend(page)
        pages += 1
        if on_progress:
            on_progress(len(rows), pages)

        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            raise PolymarketAPIError(f"/v2/{path}: page {pages} carries no pagination object; cannot tell whether the "
                                     f"feed is complete ({len(rows):,} rows so far)", payload=payload)
        has_more = pagination.get("has_more")
        next_cursor = pagination.get("next_cursor") or None
        if has_more is False or (has_more is None and next_cursor is None):
            return rows, False
        if has_more is not True:
            raise PolymarketAPIError(f"/v2/{path}: page {pages} has an unreadable has_more={has_more!r}", payload=payload)
        if not next_cursor:
            raise PolymarketAPIError(f"/v2/{path}: page {pages} says has_more but sent no next_cursor "
                                     f"({len(rows):,} rows so far)", payload=payload)
        if next_cursor in seen or next_cursor == cursor:
            raise PolymarketAPIError(f"/v2/{path}: page {pages} repeated an earlier cursor; the feed is not advancing "
                                     f"({len(rows):,} rows so far)", payload=payload)
        seen.add(next_cursor)
        empty_run = empty_run + 1 if not page else 0
        if empty_run >= MAX_EMPTY_PAGES:
            raise PolymarketAPIError(f"/v2/{path}: {empty_run} consecutive empty pages that still claim has_more "
                                     f"({len(rows):,} rows so far)", payload=payload)

        if max_rows and len(rows) >= max_rows:
            if announce:
                log(f"  ! /v2/{path}: capped at {max_rows:,} rows after {pages} pages; the oldest rows were NOT fetched")
            return rows[:max_rows], True
        if max_pages and pages >= max_pages:
            if announce:
                log(f"  ! /v2/{path}: capped at {pages} pages ({len(rows):,} rows); the oldest rows were NOT fetched")
            return rows, True
        if pages >= WALK_MAX_PAGES:
            log(f"  ! /v2/{path}: hit the {WALK_MAX_PAGES:,}-page safety net ({len(rows):,} rows); "
                f"the oldest rows were NOT fetched")
            return rows, True
        cursor = next_cursor


# ── Gamma ────────────────────────────────────────────────────────────────────

def bare_slug(slug):
    """markets.json slugs may carry an event path and a #fragment
    ("event-slug/market-slug#id"); Gamma wants just the market part."""
    return str(slug or "").split("#", 1)[0].split("?", 1)[0].rstrip("/").split("/")[-1].strip()


def fetch_market(slug):
    """Gamma market object for a slug, open or closed. Tries
    /markets/slug/<slug> first, then /markets?slug= (with and without
    closed=true, since the list endpoint hides closed markets by default)."""
    s = bare_slug(slug)
    if not s:
        raise ValueError(f"empty market slug: {slug!r}")
    try:
        m = get_json(f"{GAMMA_API}/markets/slug/{quote(s, safe='')}", timeout=15)
    except PolymarketAPIError as e:
        if e.status not in (400, 404, 422):
            raise
        m = None
    if isinstance(m, dict) and m.get("conditionId"):
        return m
    for extra in ({}, {"closed": "true"}):
        results = get_json(f"{GAMMA_API}/markets", {"slug": s, **extra}, timeout=15)
        if isinstance(results, list) and results:
            return results[0]
    raise ValueError(f"Market not found on Gamma: {slug}")


# ── Data API v2: trades ──────────────────────────────────────────────────────

def fetch_trades(condition_id, max_rows=None, on_progress=None):
    """Every fill in a market, maker and taker rows, newest first, minus the
    CLOB operator. Returns (v2 rows, capped).

    Rows are kept exactly as served, identical ones included: v2 has no
    per-fill id, and a taker order filled against several identical maker
    orders in one transaction legitimately yields identical rows. The v1
    crawl counted every such activity row too (the committed raw CSVs hold
    them with the same multiplicity), so de-duplicating here would understate
    positions. Cursor pages do not overlap. On a fee-bearing market the
    served size of a taker BUY is net of the taker fee (see the module
    docstring); the seven committed markets charged none."""
    cid = str(condition_id or "").strip()
    if not _CONDITION.match(cid):
        raise ValueError(f"not a condition id: {condition_id!r}")
    params = {"condition": cid, "taker_only": "false", "limit": PAGE_SIZE}
    raw, capped = v2_walk("trades", params, max_rows=max_rows or None, on_progress=on_progress)
    rows = []
    for r in raw:
        w = str(r.get("proxy_wallet") or "").lower()
        if w and w not in OPERATORS:
            rows.append(r)
    return rows, capped


# ── Data API v2: prior history ───────────────────────────────────────────────

def fetch_user_history_up_to(user_address, cutoff_ts, max_pages=HISTORY_MAX_PAGES):
    """The wallet's TRADE rows at or before cutoff_ts (inclusive), newest
    first, as v1-shaped dicts (proxyWallet, timestamp, conditionId, type,
    side, outcome, outcomeIndex, price, size, usdcSize, transactionHash),
    which is what scrape_insiders.analyze_whale reads.

    The server applies the cutoff (end=cutoff, inclusive), so the walk starts
    at the cutoff and goes back in time; the max_pages cap therefore keeps
    the max_pages x 1000 trades closest BEFORE the cutoff and drops older
    ones (announced on stdout). The retired v1 scraper walked the newest
    15 000 activity rows of any date and could reach 0 prior trades for a
    busy wallet; this cannot. Errors raise; they are never an empty history."""
    user = str(user_address or "").strip().lower()
    if not _ADDRESS.match(user):
        raise ValueError(f"not a wallet address: {user_address!r}")
    cutoff = int(cutoff_ts)
    params = {"user": user, "type": "TRADE", "start": 1, "end": cutoff, "limit": PAGE_SIZE}
    rows, capped = v2_walk("activity", params, max_pages=max_pages or None, announce=False)
    if capped:
        log(f"  ! prior history of {user} capped at {len(rows):,} trades ({max_pages} pages) before {cutoff}; "
            f"older trades not counted, so its prior count is a floor")
    # The server filters type=TRADE; re-check anyway (the v1 scraper counted
    # only TRADE rows, and a served SPLIT/MERGE/REDEEM row would otherwise be
    # counted as a prior bet and its size added to the lifetime stake).
    return [as_v1(r) for r in rows
            if (r.get("type") or "TRADE") == "TRADE" and int(r.get("timestamp") or 0) <= cutoff]


# ── v2 -> v1 row adapter ─────────────────────────────────────────────────────

def as_v1(r):
    """A v2 trade/activity row in the v1 shape polymarket_trades.format_row
    reads. v2 trade rows carry no usdc_size, so dollars = shares x price;
    activity rows carry usdc_size, which is used when present (it equals
    shares x price to within rounding on every row checked)."""
    size = float(r.get("size") or 0)
    price = float(r.get("price") or 0)
    usdc = r.get("usdc_size")
    return {
        "proxyWallet": str(r.get("proxy_wallet") or "").lower(),
        "timestamp": int(r.get("timestamp") or 0),
        "conditionId": str(r.get("condition_id") or ""),
        "type": r.get("type") or "TRADE",
        "side": r.get("side") or "",
        "outcome": r.get("outcome") or "",
        "outcomeIndex": r.get("outcome_index") if r.get("outcome_index") is not None else 0,
        "price": price,
        "size": size,
        "usdcSize": float(usdc) if usdc is not None else size * price,
        "transactionHash": r.get("transaction_hash") or "",
    }
