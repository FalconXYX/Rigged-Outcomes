"""
The open list (demo output 5): today's 10 open events most worth watching
for insider money, plus the next 10, each with the card the user reads.

Why these steps. The watchlist exists to point at OPEN markets where
inside knowledge could pay, before the answer is out. Two things can say
"watch this one", and the ranking uses them in this order:

  1. It has already started. The method (watchlist.method: the user's
     scrape_insiders rules) is run on the last 72 h of the event's trades
     (--window-hours; every card sentence says the window it used):
     wallets with a big net buy (net BUY$ - SELL$ per wallet and outcome
     >= scrape_insiders.whale_threshold of the market's volume, average buy
     price < 0.99) are flagged exactly as the closed scan flags whales
     (method.lookup_priors at the wallet's first buy in the window, then
     method.flag_whale). Started = >= $10,000 of flagged net buys from >= 2
     flagged wallets, over all of the event's pool markets. Those events
     go first, the most flagged dollars first. This is a fact about
     trades, never a claim about the outcome: the cards say what the
     flagged wallets hold, not that anyone knew anything.
  2. Its track record. How often ENDED events like it carried heavy
     flagged money (k of n ended events with flagged_pct >= 10 %, by
     series once a series has >= 3 ended events, else by the event's AI
     type). Ordered by the Wilson 95 % LOWER bound of k / n, not by k / n
     itself, so a tiny base no longer wins on one lucky event: a series at
     1 of 3 (33 %, interval 6-79 %) now ranks below election_vote's 33 of
     169 (20 %, 14-26 %). The bound shrinks a tiny base but does not sink
     it: 1 of 1 still has a lower bound of 21 %, above 9 of 82 (6 %).
     Then the larger n (more evidence first); no record last; ties: a
     scheduled reveal within 14 days first (the window for inside money is
     now), then the sooner end date, then the event id.

Pool. net.walk_open_events -> filters.open_pool (volume >= $100k, endDate
<= now + 60 days, highest outcome price < 0.99: a 4-5 cent market weeks
before a war is exactly what this is for) -> ai.label_pool (cached per
market id + rules; game_sport / price_level / weather removed by
ai.apply_exclusions). A market whose AI call failed is kept, unlabelled,
and its card says so ("type not known: the AI label failed this run"). A
GeminiError (a refused or missing key) does not wipe the labels: every
label already in the cache is read back market by market and used, with
the exclusions it implies, so an AI outage never turns into "no data".
Grouped by Gamma event. An event's type is the AI type of its
highest-volume labelled pool market; its reveal is the earliest scheduled
reveal among its markets, else "any_day" if one of them can resolve on any
day, else revealed_awaiting_resolution, else unclear (the card names the
market and quotes its rules); its end is the earliest endDate among its
pool markets.

Rotation. A list that never changes stops being read, so rotate() walks
today's ranking from the top: an event that was not on yesterday's top 10
is always taken, one of yesterday's only while fewer than 7 of yesterday's
are in, and the walk stops at 10. So >= 3 are new whenever 3 new events
exist, and a stale event is never kept above a better one that was left
out. When too few new events exist the list is topped up with the
next-ranked of yesterday's (10 whenever 10 candidates exist), and a
started event is always taken in its rank order (never dropped for a
lower-ranked one). Each day's list is stored in the store's open_lists
table (created here; on the --db given, so a copy gets its own history).
A run limited with --limit-events is stored as a sample (complete = 0),
never used as "yesterday", and written to open_today.sample.json (unless
--out says otherwise) so it never overwrites the real demo file.

Order of work, and why: one track_record snapshot is taken per run, BEFORE
the started checks, and every event's rate is read from it, so every
event ranks on the same ended events (the checks write wallet look-ups to
the store, and a later snapshot could differ).

Addresses. Wallet addresses appear only in a card's flagged_wallets list;
error text is scrubbed of 0x + 40-hex addresses before it reaches a card.

Public API (pure unless noted):
  positions(trades, window_start_ts, window_end_ts) -> [position]
  big_buyers(trades, market_volume, window_start_ts, window_end_ts) -> [position]
  started_signal(trades, market_volume, flag_fn, window_start_ts, window_end_ts)
      -> {"started", "flagged_usd", "wallets" (flagged addresses), "big_buyers", ...}
  event_signal(market_signals) -> the same rule over an event's markets
  rank_events(entries, top=10, next_n=10, now_ts=None) -> {"top", "next", "rest"}
  rotate(prev_ids, ranked_ids, size=10, keep_max=7, pinned=()) -> [event_id]
  event_type(markets, labels) / event_reveal(markets, labels) / event_end(markets)
  scrub(text, limit=None) -> text without wallet addresses
  cached_labels(groups, conn, today) -> {market_id: label} from the AI cache
  MethodFlags(conn, variant)  the flag_fn (network: method.lookup_priors)
  scan(conn, ...) -> the report (network, store writes); write_report(rep, path)
  ensure_schema(conn) / save_list(...) / previous_list(...)

CLI (from the repo root; progress lines at least every 30 s; --db must exist):
  python3 -m watchlist.open_scan                         # -> data/watchlist/demo/open_today.json
  python3 -m watchlist.open_scan --db data/watchlist/dev_open.db --limit-events 15
                                                         # -> data/watchlist/demo/open_today.sample.json
"""
import json
import math
import os
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrape_insiders import NEAR_CERTAIN_ODDS, whale_threshold

from watchlist import ai, filters, method, net, store, track_record
from watchlist.common import DB_PATH, DEMO_DIR, ROOT, Budget, day_str, iso, log, now_epoch, to_epoch, utc_day

WINDOW_HOURS = 72             # the "already started" window
STARTED_MIN_USD = 10_000      # flagged net buys an event needs to count as started ...
STARTED_MIN_WALLETS = 2       # ... from at least this many flagged wallets
TOP_N = 10
NEXT_N = 10
SOON_DAYS = 14                # a scheduled reveal this close wins ties
KEEP_MAX = 7                  # at most this many of yesterday's top 10 stay
EVENT_WORKERS = 4             # events checked at once (net's buckets cap the request rate either way)
MAX_WINDOW_ROWS = 60_000      # fills read per market and window (newest first); more is announced as capped
MARKETS_SHOWN = 12            # markets listed on a card (n_markets counts all)
PROGRESS_EVERY_S = 30
OUT_PATH = os.path.join(DEMO_DIR, "open_today.json")
SAMPLE_OUT_PATH = os.path.join(DEMO_DIR, "open_today.sample.json")   # where a --limit-events run writes
LABEL_FAILED = "type not known: the AI label failed this run"
ADDRESS = re.compile(r"(?<![0-9a-fA-F])0[xX][0-9a-fA-F]{40}(?![0-9a-fA-F])")   # a wallet, not a 64-hex condition id
EVENT_URL = "https://polymarket.com/event/{slug}"
MARKET_URL = "https://polymarket.com/event/{event_slug}/{slug}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS open_lists (
    day          TEXT    NOT NULL,           -- YYYY-MM-DD (UTC) of the run
    complete     INTEGER NOT NULL,           -- 1 = a full run (rotation reads only these), 0 = a --limit-events sample
    list         TEXT    NOT NULL,           -- top | next
    rank         INTEGER NOT NULL,           -- 1-based within the list
    event_id     TEXT    NOT NULL,
    title        TEXT,
    started      INTEGER,
    flagged_usd  REAL,
    rate         REAL,
    basis        TEXT,                       -- series | type | none
    rotation     TEXT,                       -- kept | new (top list only)
    run_at       INTEGER NOT NULL,
    PRIMARY KEY (day, complete, list, rank)
);
"""


# ── already started (pure) ───────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def positions(trades, window_start_ts, window_end_ts=None):
    """Net positions in [window_start_ts, window_end_ts) per (wallet,
    outcome) from v2 trade rows (proxy_wallet, side, outcome / outcome_index,
    size, price, timestamp). Dollars are size x price (v2 trade rows carry
    no usdc_size; polymarket_api.as_v1 does the same). Each: wallet,
    outcome, outcome_index, buy_usd, sell_usd, net_usd (BUY$ - SELL$),
    buy_shares, avg_buy_price (dollar / share over the buys, None without
    one), first_buy_ts, last_buy_ts, n_buys, n_sells. Largest net first."""
    lo = int(window_start_ts)
    hi = None if window_end_ts is None else int(window_end_ts)
    acc = {}
    for r in trades:
        ts = int(_f(r.get("timestamp")))
        if ts < lo or (hi is not None and ts >= hi):
            continue
        wallet = str(r.get("proxy_wallet") or "").strip().lower()
        side = str(r.get("side") or "").strip().upper()
        if not wallet or side not in ("BUY", "SELL"):
            continue
        name = str(r.get("outcome") or "").strip()
        idx = r.get("outcome_index")
        key = (wallet, name.lower() if name else f"#{idx}")
        size, price = _f(r.get("size")), _f(r.get("price"))
        usd = size * price
        p = acc.get(key)
        if p is None:
            p = acc[key] = {"wallet": wallet, "outcome": name or None,
                            "outcome_index": int(idx) if idx is not None and str(idx).lstrip("-").isdigit() else None,
                            "buy_usd": 0.0, "sell_usd": 0.0, "buy_shares": 0.0, "first_buy_ts": None,
                            "last_buy_ts": None, "n_buys": 0, "n_sells": 0}
        if side == "BUY":
            p["buy_usd"] += usd
            p["buy_shares"] += size
            p["n_buys"] += 1
            p["first_buy_ts"] = ts if p["first_buy_ts"] is None else min(p["first_buy_ts"], ts)
            p["last_buy_ts"] = ts if p["last_buy_ts"] is None else max(p["last_buy_ts"], ts)
        else:
            p["sell_usd"] += usd
            p["n_sells"] += 1
    out = []
    for p in acc.values():
        p["net_usd"] = p["buy_usd"] - p["sell_usd"]
        p["avg_buy_price"] = p["buy_usd"] / p["buy_shares"] if p["buy_shares"] > 0 else None
        out.append(p)
    out.sort(key=lambda p: (-p["net_usd"], p["wallet"], str(p["outcome"])))
    return out


def big_buyers(trades, market_volume, window_start_ts, window_end_ts=None):
    """The positions of positions() that count as big buyers: net_usd >=
    scrape_insiders.whale_threshold(market_volume) (clamp(0.1 % of the
    volume, $1k, $5k)) and an average buy price below NEAR_CERTAIN_ODDS
    (0.99: a near-certain buy is a resolution sweep, as in whale_selection)."""
    threshold = whale_threshold(market_volume)
    return [p for p in positions(trades, window_start_ts, window_end_ts)
            if p["net_usd"] >= threshold and p["avg_buy_price"] is not None
            and p["avg_buy_price"] < NEAR_CERTAIN_ODDS]


def _verdict(flagged_usd, wallets):
    return flagged_usd >= STARTED_MIN_USD and len(wallets) >= STARTED_MIN_WALLETS


def started_signal(trades, market_volume, flag_fn, window_start_ts, window_end_ts):
    """Has inside money already started on ONE market? Pure apart from
    calling flag_fn(wallet, first_buy_ts, position_usd) -> {"flagged": bool,
    ...} once per big buyer (big_buyers()), with the wallet's first buy in
    the window as the cutoff and its net dollars as the position.

    Returns {"started": flagged net $ >= STARTED_MIN_USD from >=
    STARTED_MIN_WALLETS distinct flagged wallets, "flagged_usd": the summed
    net of the flagged big buyers, "wallets": the distinct flagged wallet
    addresses (largest flagged net first), "big_buyers": every big buyer
    (the positions() fields + "flag": flag_fn's answer + "flagged"),
    "threshold", "positions" (wallet-outcome pairs in the window)}."""
    threshold = whale_threshold(market_volume)
    pos = positions(trades, window_start_ts, window_end_ts)
    bigs = [p for p in pos if p["net_usd"] >= threshold and p["avg_buy_price"] is not None
            and p["avg_buy_price"] < NEAR_CERTAIN_ODDS]
    out, by_wallet = [], defaultdict(float)
    for p in bigs:
        res = flag_fn(p["wallet"], p["first_buy_ts"], p["net_usd"]) or {}
        flagged = bool(res.get("flagged"))
        out.append({**p, "flag": res, "flagged": flagged})
        if flagged:
            by_wallet[p["wallet"]] += p["net_usd"]
    wallets = sorted(by_wallet, key=lambda w: (-by_wallet[w], w))
    flagged_usd = round(sum(by_wallet.values()), 2)
    return {"started": _verdict(flagged_usd, wallets), "flagged_usd": flagged_usd, "wallets": wallets,
            "big_buyers": out, "threshold": round(float(threshold), 2), "positions": len(pos)}


def event_signal(market_signals):
    """The started rule over an event's markets: started_signal results
    (each may carry "market" / "condition_id" keys the caller added).
    flagged_usd sums over markets, wallets are distinct across them, so two
    markets with one flagged wallet each can start an event together."""
    by_wallet = defaultdict(float)
    bigs = []
    for s in market_signals:
        for b in s.get("big_buyers") or []:
            bigs.append(b)
            if b.get("flagged"):
                by_wallet[b["wallet"]] += b["net_usd"]
    wallets = sorted(by_wallet, key=lambda w: (-by_wallet[w], w))
    flagged_usd = round(sum(by_wallet.values()), 2)
    return {"started": _verdict(flagged_usd, wallets), "flagged_usd": flagged_usd, "wallets": wallets,
            "big_buyers": bigs}


# ── ranking and rotation (pure) ──────────────────────────────────────────────

def scheduled_soon(entry, now_ts, days=SOON_DAYS):
    """True when the entry's reveal is scheduled on a UTC day from today to
    `days` days ahead (inclusive)."""
    if entry.get("reveal_kind") != "scheduled" or entry.get("reveal_ts") is None:
        return False
    ahead = utc_day(entry["reveal_ts"]) - utc_day(now_ts)
    return 0 <= ahead <= days


def track_low(track):
    """The number the ranking orders track records by: the Wilson 95 % lower
    bound of k / n (track["low"]). An entry that carries k and n but no low
    gets it computed; one with a bare rate (a hand-built entry) falls back
    to the rate. 0.0 without a record."""
    t = track or {}
    low = t.get("low")
    if low is None and t.get("n"):
        low = track_record.wilson(t.get("k") or 0, t["n"])[0]
    if low is None:
        low = t.get("rate")
    return _f(low)


def rank_key(entry, now_ts):
    """The sort key of rank_events (smaller first)."""
    started = bool(entry.get("started"))
    usd = _f(entry.get("started_flagged_usd"))
    t = entry.get("track") or {}
    rated = t.get("rate") is not None
    end = entry.get("end_ts")
    return (0 if started else 1, -usd if started else 0.0, 0 if rated else 1,
            -track_low(t) if rated else 0.0, -int(t.get("n") or 0) if rated else 0,
            0 if scheduled_soon(entry, now_ts) else 1,
            int(end) if end is not None else float("inf"), str(entry.get("event_id")))


def rank_events(entries, top=TOP_N, next_n=NEXT_N, now_ts=None):
    """{"top": [...], "next": [...], "rest": [...]}: the entries in rank
    order, cut after `top` and `top + next_n`. Order: started events first,
    by started_flagged_usd descending; then by the track record's Wilson
    lower bound track["low"] descending (track_low(); an entry without a
    rate after every rated one), then the larger track["n"]; ties: a
    scheduled reveal within SOON_DAYS days of now_ts first, then the sooner
    end_ts, then event_id. now_ts None = the clock. The entries are not
    modified."""
    now_ts = now_epoch() if now_ts is None else int(now_ts)
    ranked = sorted(entries, key=lambda e: rank_key(e, now_ts))
    return {"top": ranked[:top], "next": ranked[top:top + next_n], "rest": ranked[top + next_n:]}


def rotate(prev_ids, ranked_ids, size=TOP_N, keep_max=KEEP_MAX, pinned=()):
    """Today's list of `size` event ids, in rank order.

    Walks today's ranking (ranked_ids, best first) from the top: an event
    that was NOT on yesterday's list (prev_ids) is always taken; one of
    yesterday's only while fewer than keep_max of yesterday's are taken;
    the walk stops once `size` are taken. So >= size - keep_max are new
    whenever that many new events exist, and nothing of yesterday's sits
    on the list while a better-ranked event is left off for it. If the
    walk runs out of ranking first (too few new events), the list is
    topped up with the next-ranked of yesterday's, so it holds `size` ids
    whenever that many candidates exist.

    pinned (the started events) are taken in their rank order like new
    ones, whatever keep_max says (one of yesterday's still counts towards
    it): a started event is never dropped in favour of a lower-ranked
    one."""
    prev = {str(i) for i in (prev_ids or ())}
    pin = {str(i) for i in (pinned or ())}
    ranked, seen = [], set()
    for i in ranked_ids:
        i = str(i)
        if i not in seen:
            seen.add(i)
            ranked.append(i)
    size = max(0, int(size))
    chosen, n_old = set(), 0
    for i in ranked:
        if len(chosen) >= size:
            break
        if i not in prev or i in pin:
            chosen.add(i)
            n_old += i in prev
        elif n_old < keep_max:
            chosen.add(i)
            n_old += 1
    for i in ranked:              # the top-up: only yesterday's are left out at this point
        if len(chosen) >= size:
            break
        chosen.add(i)
    return [i for i in ranked if i in chosen]


# ── event facts from its markets (pure) ──────────────────────────────────────

def _label(labels, m):
    return (labels or {}).get(str(m.get("id")))


def event_type(markets, labels):
    """(type, market) from the highest-volume market with an AI type
    (markets in any order); (None, None) when none is labelled."""
    for m in sorted(markets, key=lambda m: -filters.volume(m)):
        lab = _label(labels, m)
        if lab and lab.get("type"):
            return lab["type"], m
    return None, None


def _date_ts(d):
    return to_epoch(d) if d else None


def event_reveal(markets, labels):
    """The event's reveal from its markets' AI labels: the earliest
    scheduled reveal date; else any_day (the highest-volume such market);
    else revealed_awaiting_resolution (the earliest date); else unclear.
    {kind, date, ts (00:00 UTC of the date), quote, market_id, question,
    end_date (that market's deadline), rule}."""
    by_vol = sorted(markets, key=lambda m: -filters.volume(m))
    labelled = [(m, _label(labels, m)) for m in by_vol if _label(labels, m)]

    def pack(kind, m, lab, rule):
        d = (lab or {}).get("reveal_date") if kind in ("scheduled", ai.REVEALED) else None
        return {"kind": kind, "date": d, "ts": _date_ts(d), "quote": (lab or {}).get("quote") or None,
                "market_id": str(m.get("id")) if m is not None else None,
                "question": m.get("question") if m is not None else None,
                "end_date": m.get("endDate") if m is not None else None, "rule": rule}

    sched = [(lab["reveal_date"], i, m, lab) for i, (m, lab) in enumerate(labelled)
             if lab.get("reveal_kind") == "scheduled" and lab.get("reveal_date")]
    if sched:
        _, _, m, lab = min(sched)
        return pack("scheduled", m, lab, "the earliest scheduled reveal among the event's pool markets")
    for m, lab in labelled:
        if lab.get("reveal_kind") == "any_day":
            return pack("any_day", m, lab, "no market has a scheduled reveal; this one (the highest-volume "
                                           "any_day market) can resolve on any day up to its end date")
    past = [(lab["reveal_date"], i, m, lab) for i, (m, lab) in enumerate(labelled)
            if lab.get("reveal_kind") == ai.REVEALED and lab.get("reveal_date")]
    if past:
        _, _, m, lab = min(past)
        return pack(ai.REVEALED, m, lab, "the scheduled moment is past; the market waits for the result to be "
                                         "made official")
    if labelled:
        m, lab = labelled[0]
        return pack("unclear", m, lab, "the AI could not tell from the rules when the answer comes out")
    return pack("unclear", by_vol[0] if by_vol else None, None, "no market of the event has an AI label")


def event_end(markets):
    """The earliest endDate (epoch) among the markets, or None."""
    ends = [to_epoch(m.get("endDate")) for m in markets]
    ends = [e for e in ends if e is not None]
    return min(ends) if ends else None


def scrub(text, limit=None):
    """text with every wallet address (0x + 40 hex) replaced by "<wallet>",
    then cut to `limit` characters: scrubbed BEFORE cutting, so a cut can
    never leave half an address behind. Addresses belong only in a card's
    flagged_wallets list."""
    s = ADDRESS.sub("<wallet>", str(text))
    return s if limit is None else s[:limit]


# ── the method as flag_fn (network) ──────────────────────────────────────────

class MethodFlags:
    """flag_fn for started_signal: the closed scan's flagging of one
    whale, applied to a big buyer of an open market. The prior history is
    method.lookup_priors at the wallet's first buy in the window (the
    variant's cutoff: "exact" = up to and including that buy, counting
    fills), then method.flag_whale(net position, prior trade count, prior
    volume). prefetch() looks up many at once (parallel, cached in the
    store); a call that was not prefetched looks itself up. Answers are
    memoised per (wallet, first buy, position). A failed look-up is never
    scored (the method's rule): when a batch fails, its wallets are asked
    one by one, and a wallet that still fails is answered {"flagged":
    False, "error": ...}, which check_event reports."""

    def __init__(self, conn, variant=method.DEFAULT_VARIANT, workers=method.WORKERS):
        self.conn, self.variant, self.workers = conn, variant, workers
        self.memo = {}
        self.lock = threading.Lock()

    @staticmethod
    def _key(wallet, first_ts, position):
        return str(wallet).lower(), int(first_ts), round(float(position), 4)

    def prefetch(self, items):
        """items: [(wallet, first_buy_ts, position_usd)]."""
        todo, seen = [], set()
        for w, t, p in items:
            k = self._key(w, t, p)
            with self.lock:
                if k in self.memo or k in seen:
                    continue
            seen.add(k)
            todo.append(k)
        if not todo:
            return
        rows = [{"wallet": w, "first_ts": t, "position": p} for w, t, p in todo]
        try:
            priors = method.lookup_priors(rows, self.variant, conn=self.conn, workers=self.workers)
        except Exception as e:  # one bad wallet must not void the others: ask one by one
            if len(todo) > 1:
                for k in todo:
                    self.prefetch([k])
                return
            with self.lock:
                self.memo[todo[0]] = {"flagged": False, "error": scrub(f"{type(e).__name__}: {e}", 240),
                                      "cutoff_ts": todo[0][1], "variant": self.variant}
            return
        with self.lock:
            for k, pr in zip(todo, priors):
                f = method.flag_whale(k[2], pr["trade_count"], pr["volume_usdc"])
                self.memo[k] = {"flagged": f["flagged"], "is_first_x": f["is_first_x"],
                                "is_percent": f["is_percent"], "concentration": round(f["concentration"], 2),
                                "prior_trade_count": int(pr["trade_count"]),
                                "prior_volume_usdc": round(float(pr["volume_usdc"]), 2),
                                "prior_counts_exact": bool(pr["counts_exact"]), "decided_by": pr["decided_by"],
                                "cutoff_ts": k[1], "variant": self.variant}

    def __call__(self, wallet, first_buy_ts, position_usd):
        k = self._key(wallet, first_buy_ts, position_usd)
        with self.lock:
            hit = self.memo.get(k)
        if hit is None:
            self.prefetch([(wallet, first_buy_ts, position_usd)])
            with self.lock:
                hit = self.memo[k]
        return hit


# ── the store's open_lists table ─────────────────────────────────────────────

def ensure_schema(conn):
    with store.LOCK:
        conn.executescript(SCHEMA)
        conn.commit()


def previous_list(conn, day):
    """(day, [event_id] in rank order) of the latest COMPLETE top list
    stored before `day` (YYYY-MM-DD), or (None, [])."""
    ensure_schema(conn)
    with store.LOCK:
        r = conn.execute("SELECT MAX(day) AS d FROM open_lists WHERE complete=1 AND list='top' AND day < ?",
                         (day,)).fetchone()
        if not r or r["d"] is None:
            return None, []
        rows = conn.execute("SELECT event_id FROM open_lists WHERE day=? AND complete=1 AND list='top' "
                            "ORDER BY rank", (r["d"],)).fetchall()
    return r["d"], [x["event_id"] for x in rows]


def save_list(conn, day, complete, top_entries, next_entries, rotation=None, run_at=None):
    """Replace the stored (day, complete) lists with these entries."""
    ensure_schema(conn)
    run_at = now_epoch() if run_at is None else int(run_at)
    rot = rotation or {}
    rows = []
    for name, entries in (("top", top_entries), ("next", next_entries)):
        for i, e in enumerate(entries, 1):
            tr = e.get("track") or {}
            rows.append((day, 1 if complete else 0, name, i, str(e["event_id"]), e.get("title"),
                         1 if e.get("started") else 0, _f(e.get("started_flagged_usd")), tr.get("rate"),
                         tr.get("basis"), rot.get(str(e["event_id"])) if name == "top" else None, run_at))
    with store.transaction(conn):
        conn.execute("DELETE FROM open_lists WHERE day=? AND complete=?", (day, 1 if complete else 0))
        conn.executemany("INSERT INTO open_lists (day, complete, list, rank, event_id, title, started, flagged_usd, "
                         "rate, basis, rotation, run_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


# ── cards ────────────────────────────────────────────────────────────────────

def _range(t):
    """The Wilson 95 % interval of a track record as "25–51%"."""
    lo, hi = t.get("low"), t.get("high")
    if lo is None or hi is None:
        return "-"
    return f"{100 * lo:.0f}–{100 * hi:.0f}%"


def _n(n, one, many=None):
    """'1 wallet' / '3 wallets'."""
    n = int(n or 0)
    return f"{n} {one if n == 1 else (many or one + 's')}"


def started_rule(hours=WINDOW_HOURS):
    return (f"≥ ${STARTED_MIN_USD:,} of net buys (buys minus sells, last {hours:g} h) from ≥ {STARTED_MIN_WALLETS} "
            f"distinct wallets the method flags, over the event's pool markets")


BIG_BUYER_RULE = ("net buys (buys minus sells) per wallet and outcome ≥ clamp(0.1% of the market's volume, $1,000, "
                  f"$5,000), average buy price < {NEAR_CERTAIN_ODDS}")


def _usd(x):
    return f"${_f(x):,.0f}"


def _what(t):
    """What a track record counts, in words."""
    if t.get("basis") == "series":
        return f"ended events in this event's series ({t.get('key')})"
    return f"ended {t.get('key')} events"


def no_record_reason(track, type_, label_failed=False):
    """Why an event has no track record, without contradicting itself: the
    type's side and the series' side are said separately (a series with 2
    ended events is real evidence the store holds, but a series counts only
    from track_record.MIN_SERIES_N)."""
    t = track or {}
    if type_:
        a = f"the store has no ended {type_} events"
    elif label_failed:
        a = "the event's type is not known (the AI label failed this run)"
    else:
        a = "the event has no AI type"
    sn = int(t.get("series_n") or 0)
    if sn:
        b = (f"its series has only {_n(sn, 'ended event')} (a series' own record is used from "
             f"{track_record.MIN_SERIES_N})")
    else:
        b = "no ended event shares its series"
    return f"{a}, and {b}"


def track_sentence(track, type_, label_failed=False):
    """What the track record says, in words that name every number."""
    t = track or {}
    if t.get("basis") not in ("series", "type"):
        return f"No track record: {no_record_reason(t, type_, label_failed)}."
    s = (f"{t['k']} of {t['n']} {_what(t)} had ≥ {track_record.HEAVY_PCT:g}% of their whale capital flagged by the "
         f"method (range {_range(t)}: the Wilson 95% interval of {t['k']} of {t['n']})")
    big = f"${track_record.BIG_CAPITAL:,.0f}"
    if t["k"] == 1:
        s += f"; that one had {'≥' if t.get('k_big') else 'less than'} {big} flagged."
    elif t["k"]:
        s += f"; {t.get('k_big', 0)} of those {t['k']} had ≥ {big} flagged."
    else:
        s += "."
    if t.get("basis") == "type" and t.get("series_n"):
        s += (f" The event's series has only {_n(t['series_n'], 'ended event')} (fewer than "
              f"{track_record.MIN_SERIES_N}), so the type is used.")
    if t.get("basis") == "type" and label_failed:
        s += " For some of the event's markets the type is not known: the AI label failed this run."
    return s


def _thresholds(market_sigs):
    """The big-buyer threshold(s) of the checked markets, in words."""
    thr = sorted({float(s["threshold"]) for s in market_sigs if s.get("threshold") is not None})
    if not thr:
        return "the market's threshold"
    if len(thr) == 1:
        return _usd(thr[0])
    return f"{_usd(thr[0])}–{_usd(thr[-1])} (by market volume)"


def started_block(status, sig, market_sigs, window, errors, capped):
    """The card's already-started section and the separate wallet list."""
    errors = [scrub(e) for e in errors or []]
    bigs = [b for s in market_sigs for b in s["big_buyers"]]
    flagged = [b for b in bigs if b["flagged"]]
    n_markets = len(market_sigs)
    side = defaultdict(lambda: {"flagged_net_usd": 0.0, "wallets": set(), "first": None, "last": None})
    for b in flagged:
        k = (b["question"], b.get("outcome"), b.get("outcome_index"))
        s = side[k]
        s["flagged_net_usd"] += b["net_usd"]
        s["wallets"].add(b["wallet"])
        s["first"] = b["first_buy_ts"] if s["first"] is None else min(s["first"], b["first_buy_ts"])
        s["last"] = b["last_buy_ts"] if s["last"] is None else max(s["last"], b["last_buy_ts"])
    by_outcome = [{"market": q, "outcome": o if o is not None else (f"#{i}" if i is not None else None),
                   "flagged_net_usd": round(v["flagged_net_usd"], 2), "flagged_wallets": len(v["wallets"]),
                   "first_buy": iso(v["first"]), "last_buy": iso(v["last"])}
                  for (q, o, i), v in sorted(side.items(), key=lambda kv: -kv[1]["flagged_net_usd"])]
    first = min((b["first_buy_ts"] for b in flagged), default=None)
    last = max((b["last_buy_ts"] for b in flagged), default=None)
    usd = sig["flagged_usd"] if sig else 0.0
    nw = len(sig["wallets"]) if sig else 0
    hours = window["hours"]
    rule = started_rule(hours)
    fills = sum(s.get("fills", 0) for s in market_sigs)
    thr = _thresholds(market_sigs)
    if status == "error":
        sentence = (f"Not known: part of the {hours:g} h check failed ({_n(len(errors), 'problem')}, listed under "
                    f"errors); what was read shows {_n(nw, 'flagged wallet')} with net buys (buys minus sells) of "
                    f"{_usd(usd)}.")
    elif status == "not_checked":
        sentence = "Not checked this run (the time budget ran out before this event)."
    elif status == "started":
        sides = "; ".join(f"{_usd(x['flagged_net_usd'])} on {x['outcome']}"
                          + (f" (\"{x['market']}\")" if n_markets > 1 else "") for x in by_outcome[:3])
        sentence = (f"Already started: in the last {hours:g} h, {nw} wallets the method flags have net buys "
                    f"(buys minus sells) of {_usd(usd)} in this event ({sides}); their buys in the window run "
                    f"from {iso(first)} to {iso(last)} (UTC).")
    elif nw:
        sentence = (f"Not started: {_n(nw, 'flagged wallet')} with net buys (buys minus sells) of {_usd(usd)} in "
                    f"the last {hours:g} h (started needs ≥ {_usd(STARTED_MIN_USD)} from ≥ {STARTED_MIN_WALLETS} "
                    f"flagged wallets).")
    elif bigs:
        sentence = (f"Not started: {_n(len(bigs), 'big buyer')} in the last {hours:g} h (a wallet's net buys of "
                    f"one outcome ≥ {thr} at an average price < {NEAR_CERTAIN_ODDS}), none flagged by the method.")
    else:
        sentence = (f"Not started: no wallet's net buys (buys minus sells) of one outcome reached {thr} in the last "
                    f"{hours:g} h ({fills:,} fills read on {_n(n_markets, 'market')}).")
    if capped and status in ("started", "not_started"):
        sentence += f" Only the newest {MAX_WINDOW_ROWS:,} fills of a market were read where it had more."
    block = {
        "status": status, "sentence": sentence, "rule": rule,
        "window": {"from": iso(window["from"]), "to": iso(window["to"]), "hours": hours},
        "flagged_net_usd": round(usd, 2), "flagged_wallets": nw,
        "big_buyers": len(bigs), "big_buyers_net_usd": round(sum(b["net_usd"] for b in bigs), 2),
        "big_buyers_rule": BIG_BUYER_RULE,
        "first_flagged_buy": iso(first), "last_flagged_buy": iso(last),
        "by_outcome": by_outcome,
        "markets_checked": n_markets, "fills_read": fills,
        "capped": bool(capped), "errors": errors,
    }
    wallets = [{"wallet": b["wallet"], "market": b["question"], "outcome": b.get("outcome"),
                "net_usd": round(b["net_usd"], 2), "buy_usd": round(b["buy_usd"], 2),
                "sell_usd": round(b["sell_usd"], 2), "avg_buy_price": round(b["avg_buy_price"], 4),
                "first_buy": iso(b["first_buy_ts"]), "last_buy": iso(b["last_buy_ts"]), "buys": b["n_buys"],
                "sells": b["n_sells"], "prior_trade_count": b["flag"].get("prior_trade_count"),
                "prior_volume_usdc": b["flag"].get("prior_volume_usdc"),
                "prior_counts_exact": b["flag"].get("prior_counts_exact"),
                "concentration_pct": b["flag"].get("concentration"), "is_first_x": b["flag"].get("is_first_x"),
                "is_percent": b["flag"].get("is_percent"), "decided_by": b["flag"].get("decided_by")}
               for b in sorted(flagged, key=lambda b: -b["net_usd"])]
    return block, wallets


def _market_url(ev, m):
    if not ev.get("slug"):
        return None
    if not m.get("slug") or m.get("slug") == ev.get("slug"):
        return EVENT_URL.format(slug=ev["slug"])
    return MARKET_URL.format(event_slug=ev["slug"], slug=m["slug"])


def _market_row(m, ev, lab, failed=False):
    names, prices = filters.outcomes(m), filters.outcome_prices(m)
    return {"question": m.get("question"), "market_id": str(m.get("id")), "condition_id": m.get("conditionId"),
            "url": _market_url(ev, m),
            "prices": [{"outcome": n, "price": p} for n, p in zip(names, prices)],
            "last_trade_price": m.get("lastTradePrice"), "best_bid": m.get("bestBid"), "best_ask": m.get("bestAsk"),
            "volume_usd": round(filters.volume(m), 2), "end_date": m.get("endDate"),
            "ai_type": (lab or {}).get("type"), "reveal_kind": (lab or {}).get("reveal_kind"),
            "reveal_date": (lab or {}).get("reveal_date"), "reveal_quote": (lab or {}).get("quote"),
            "ai_note": LABEL_FAILED if failed and not lab else None}


TYPE_RULE = "the AI type of the event's highest-volume labelled pool market"


def build_entry(group, labels, track, series_key, check, now_ts, read_at, label_failed=()):
    """One event's rank fields + card. check: {"status", "signal",
    "markets": [market signals with question / fills], "errors", "capped",
    "window", "seconds"}. label_failed: the market ids whose AI label failed
    this run (their cards say so)."""
    ev, markets = group["event"], group["markets"]
    failed_ids = {str(i) for i in (label_failed or ())}
    failed_ids = {str(m.get("id")) for m in markets if str(m.get("id")) in failed_ids and not _label(labels, m)}
    failed_here = [m for m in markets if str(m.get("id")) in failed_ids]
    type_, type_m = event_type(markets, labels)
    rev = event_reveal(markets, labels)
    end_ts = event_end(markets)
    sig = check.get("signal") or {"started": False, "flagged_usd": 0.0, "wallets": [], "big_buyers": []}
    started = check["status"] == "started"
    block, wallets = started_block(check["status"], check.get("signal"), check.get("markets") or [],
                                   check["window"], check.get("errors") or [], check.get("capped"))
    by_vol = sorted(markets, key=lambda m: -filters.volume(m))
    if failed_here and type_ is None:
        type_note = f"{LABEL_FAILED} ({_n(len(failed_here), 'market')} of the event)"
    elif failed_here:
        type_note = (f"{_n(len(failed_here), 'market')} of the event: {LABEL_FAILED}; the type is that of the "
                     f"highest-volume market that has a label")
    else:
        type_note = None
    reveal = {k: rev[k] for k in ("kind", "date", "quote", "question", "end_date", "rule")}
    if failed_here and not any(_label(labels, m) for m in markets):
        reveal["rule"] = "not known: the AI label failed this run for every market of the event"
    card = {
        "event_id": str(group["event_id"]), "title": ev.get("title"),
        "url": EVENT_URL.format(slug=ev.get("slug")) if ev.get("slug") else None,
        "end": {"end_date": iso(end_ts), "rule": "the earliest endDate among the event's pool markets"},
        "reveal": reveal,
        "type": {"type": type_, "rule": TYPE_RULE, "market": type_m.get("question") if type_m else None,
                 "label_failed": len(failed_here), "note": type_note},
        "track_record": {**{k: (track or {}).get(k) for k in ("basis", "key", "k", "n", "rate", "low", "high", "k_big",
                                                              "series_n")},
                         "series_key": series_key, "heavy_rule": f"flagged_pct ≥ {track_record.HEAVY_PCT:g}%",
                         "sentence": track_sentence(track, type_, bool(failed_here)),
                         "examples": (track or {}).get("examples") or []},
        "already_started": block,
        "markets": [_market_row(m, ev, _label(labels, m), str(m.get("id")) in failed_ids)
                    for m in by_vol[:MARKETS_SHOWN]],
        "n_markets": len(markets), "markets_not_shown": max(0, len(markets) - MARKETS_SHOWN),
        "prices_read_at": iso(read_at),
        "flagged_wallets": wallets,
        "check_seconds": check.get("seconds"),
    }
    return {"event_id": str(group["event_id"]), "title": ev.get("title"), "end_ts": end_ts,
            "reveal_kind": rev["kind"], "reveal_ts": rev["ts"], "started": started,
            "started_flagged_usd": float(sig["flagged_usd"]) if started else 0.0,
            "started_wallets": len(sig["wallets"]) if started else 0, "track": track,
            "type": type_, "label_failed": len(failed_here),
            "started_status": check["status"], "card": card}


def why_ranked(entry, now_ts, hours=WINDOW_HOURS):
    """One line: what put the entry where it is, every number named."""
    t = entry.get("track") or {}
    if entry.get("started"):
        return (f"Already started: {entry['started_wallets']} wallets the method flags bought "
                f"{_usd(entry['started_flagged_usd'])} net (buys minus sells) in the last {hours:g} h")
    if t.get("rate") is None:
        s = f"No track record: {no_record_reason(t, entry.get('type'), bool(entry.get('label_failed')))}"
    else:
        s = (f"Track record: {t['k']} of {t['n']} {_what(t)} had ≥ {track_record.HEAVY_PCT:g}% flagged capital "
             f"(range {_range(t)})")
    if scheduled_soon(entry, now_ts):
        s += f"; reveal scheduled for {day_str(entry['reveal_ts'], is_day=False)}, within {SOON_DAYS} days"
    return s


def fields_legend(window_hours=WINDOW_HOURS, top=TOP_N):
    """The report's legend: every card key (dotted path; [] = each item of
    a list) and what it holds."""
    h = f"{window_hours:g} h"
    return {
        "list": "top or next",
        "position": f"the card's place in its list (top 1-{top}, next 1-...) after rotation",
        "rank": "the event's place in today's full ranking, before rotation",
        "rotation": "top list only: kept (was on the previous day's top list) or new",
        "why": "what put the event at its rank: the started signal, else the track record (k of n ended events "
               "with ≥ 10% flagged capital, range = the Wilson 95% interval), plus a reveal scheduled soon",
        "event_id": "the Gamma event id",
        "title": "the Gamma event title",
        "url": "the event on polymarket.com",
        "end.end_date": "the earliest endDate among the event's pool markets (UTC)",
        "end.rule": "how end_date was chosen",
        "reveal.kind": "scheduled / any_day / revealed_awaiting_resolution / unclear (from the AI labels)",
        "reveal.date": "the scheduled reveal date (YYYY-MM-DD), null unless scheduled or awaiting resolution",
        "reveal.quote": "the words of the market's rules the AI quoted for the reveal",
        "reveal.question": "the market the reveal comes from",
        "reveal.end_date": "that market's endDate",
        "reveal.rule": "how the reveal was chosen among the event's markets",
        "type.type": "the event's AI type (null: not known)",
        "type.rule": "how the type was chosen",
        "type.market": "the market whose label gave the type",
        "type.label_failed": "how many of the event's markets have no AI label because the AI call failed this run",
        "type.note": f"set when label_failed > 0 (\"{LABEL_FAILED}\")",
        "track_record.basis": "series (the event's series has ≥ 3 ended events), type, or none",
        "track_record.key": "the series key or the type the record counts",
        "track_record.k": "ended events of the basis that were heavy (flagged_pct ≥ 10%)",
        "track_record.n": "ended events of the basis in the store",
        "track_record.rate": "k / n",
        "track_record.low": "Wilson 95% lower bound of k / n (what the ranking orders by)",
        "track_record.high": "Wilson 95% upper bound of k / n",
        "track_record.k_big": "heavy ended events that also had ≥ $10,000 flagged",
        "track_record.series_n": "ended events in the event's series (used as the basis from 3)",
        "track_record.series_key": "the event's series key (Gamma seriesSlug, else the title without dates and "
                                   "numbers)",
        "track_record.heavy_rule": "what makes an ended event heavy",
        "track_record.sentence": "the track record in words",
        "track_record.examples[].event_id": "a recent heavy ended event of the basis: its Gamma event id",
        "track_record.examples[].title": "its title",
        "track_record.examples[].flagged_pct": "its flagged capital / whale capital, in %",
        "track_record.examples[].flagged_capital": "its flagged capital in $",
        "track_record.examples[].small": "true when its flagged capital was below $10,000",
        "track_record.examples[].closed": "the day it closed (YYYY-MM-DD, UTC)",
        "already_started.status": "started / not_started / error (part of the check failed) / not_checked",
        "already_started.sentence": "the started check in words",
        "already_started.rule": "what counts as started",
        "already_started.window.from": f"start of the {h} trade window (UTC)",
        "already_started.window.to": "end of the trade window (UTC, the run's time)",
        "already_started.window.hours": "the window's length in hours (--window-hours)",
        "already_started.flagged_net_usd": "net buys (buys minus sells) in the window by wallets the method "
                                           "flags, summed over the event's pool markets",
        "already_started.flagged_wallets": "distinct wallets among the flagged big buyers",
        "already_started.big_buyers": "wallet-outcome positions in the window that pass the big-buyer rule "
                                      "(a wallet on two markets or outcomes is two positions)",
        "already_started.big_buyers_net_usd": "the big buyers' net buys summed, flagged or not",
        "already_started.big_buyers_rule": "what makes a big buyer",
        "already_started.first_flagged_buy": "the earliest buy in the window by a flagged big buyer (UTC)",
        "already_started.last_flagged_buy": "the latest buy in the window by a flagged big buyer (UTC)",
        "already_started.by_outcome[].market": "a market with flagged net buys",
        "already_started.by_outcome[].outcome": "the outcome bought",
        "already_started.by_outcome[].flagged_net_usd": "flagged net buys of that outcome in the window",
        "already_started.by_outcome[].flagged_wallets": "flagged wallets on that outcome",
        "already_started.by_outcome[].first_buy": "their first buy in the window (UTC)",
        "already_started.by_outcome[].last_buy": "their last buy in the window (UTC)",
        "already_started.markets_checked": "pool markets whose window trades were read",
        "already_started.fills_read": "trade rows (fills) read over those markets",
        "already_started.capped": f"true when a market had more than {MAX_WINDOW_ROWS:,} fills in the window (only "
                                  f"the newest were read)",
        "already_started.errors": "what failed in the check (addresses removed)",
        "markets[].question": "a pool market of the event (highest volume first, at most 12)",
        "markets[].market_id": "its Gamma market id",
        "markets[].condition_id": "its condition id",
        "markets[].url": "the market on polymarket.com",
        "markets[].prices[].outcome": "an outcome",
        "markets[].prices[].price": "its Gamma outcomePrice when the pool was read (prices_read_at)",
        "markets[].last_trade_price": "Gamma lastTradePrice",
        "markets[].best_bid": "Gamma bestBid",
        "markets[].best_ask": "Gamma bestAsk",
        "markets[].volume_usd": "Gamma volumeNum, all-time",
        "markets[].end_date": "the market's endDate (its deadline)",
        "markets[].ai_type": "the market's AI type (null: no label)",
        "markets[].reveal_kind": "the market's AI reveal kind",
        "markets[].reveal_date": "the market's AI reveal date",
        "markets[].reveal_quote": "the words of its rules the AI quoted",
        "markets[].ai_note": f"\"{LABEL_FAILED}\" when its AI call failed this run, else null",
        "n_markets": "the event's pool markets (all of them, shown or not)",
        "markets_not_shown": "pool markets left off the markets list",
        "prices_read_at": "when the pool (and its prices) was read (UTC)",
        "flagged_wallets[].wallet": "a flagged big buyer's wallet (addresses appear only here)",
        "flagged_wallets[].market": "the market it bought",
        "flagged_wallets[].outcome": "the outcome it bought",
        "flagged_wallets[].net_usd": "its net buys (buys minus sells) in the window",
        "flagged_wallets[].buy_usd": "its buys in the window, $",
        "flagged_wallets[].sell_usd": "its sells in the window, $",
        "flagged_wallets[].avg_buy_price": "dollars per share over its buys in the window",
        "flagged_wallets[].first_buy": "its first buy in the window (UTC; the cutoff of its prior history)",
        "flagged_wallets[].last_buy": "its last buy in the window (UTC)",
        "flagged_wallets[].buys": "its buy fills in the window",
        "flagged_wallets[].sells": "its sell fills in the window",
        "flagged_wallets[].prior_trade_count": "its trades before the cutoff (the method's first-10 rule)",
        "flagged_wallets[].prior_volume_usdc": "its traded volume before the cutoff, $",
        "flagged_wallets[].prior_counts_exact": "false: the count runs to the end of the day before the cutoff, a "
                                                "lower bound, used where both one-call answers agree on the flags",
        "flagged_wallets[].concentration_pct": "net position / max(prior volume, position) x 100",
        "flagged_wallets[].is_first_x": "prior_trade_count ≤ 10",
        "flagged_wallets[].is_percent": "concentration_pct ≥ 80",
        "flagged_wallets[].decided_by": "how the prior look-up was settled (method.lookup_priors)",
        "check_seconds": "seconds the event's started check took",
    }


def card_paths(card, prefix=""):
    """Every leaf key path of a card (lists of dicts as "key[].sub"), for
    checking the legend covers the card."""
    out = set()
    for k, v in card.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out |= card_paths(v, p + ".")
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for x in v:
                out |= card_paths(x, p + "[].")
        else:
            out.add(p)
    return out


# ── the scan (network, store writes) ─────────────────────────────────────────

class Heartbeat:
    """A log line every `every` seconds with the current stage, so an
    unattended run never goes quiet."""

    def __init__(self, every=PROGRESS_EVERY_S):
        self.every, self.t0 = every, time.monotonic()
        self.stage, self.detail = "start", ""
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()

    def set(self, stage, detail=""):
        self.stage, self.detail = stage, detail

    def _run(self):
        while not self.stop.wait(self.every):
            log(f"  [{time.monotonic() - self.t0:6.0f} s] {self.stage}" + (f": {self.detail}" if self.detail else ""))


def cached_labels(groups, conn, today):
    """{market_id: label} for every pool market whose label is already in
    the AI cache (same market id, same rules, same prompt version), read
    market by market: what ai.label_pool answers without asking. Used when
    a GeminiError stops label_pool, so the labels (and the exclusions they
    imply) survive an AI outage."""
    out = {}
    for g in groups:
        for m in g["markets"]:
            mid = str(m.get("id") or "")
            if not mid or mid in out:
                continue
            hit = ai._from_cache(conn, ai._item(m, g["event"]), today)
            if hit:
                out[mid] = hit
    return out


def _label_all(groups, conn, budget, client, today):
    """ai.label_pool with closed_scan's tolerance: a refused or missing key
    (GeminiError) does not stop the run and does not wipe the labels: every
    cached label is read back (cached_labels) and used; the markets left
    without one are reported (info["failed_ids"]) and their cards say
    LABEL_FAILED."""
    done, batch_errors = Counter(), []

    def on_batch(batch, got, failed, errs):
        done["asked"] += len(batch)
        done["failed"] += len(failed)
        for e in errs or ():
            e = scrub(e, 300)
            if e not in batch_errors and len(batch_errors) < 5:
                batch_errors.append(e)
        log(f"  ai: +{len(got)} labelled" + (f", {len(failed)} failed" if failed else "")
            + f"  [{done['asked']} asked this run]")

    t0 = time.time()
    try:
        labels = ai.label_pool(groups, conn=conn, client=client, budget=budget, today=today, on_batch=on_batch)
        error = None
    except ai.GeminiError as e:
        error = scrub(e, 300)
        labels = cached_labels(groups, conn, today)
        log(f"  ! ai: {error}; {len(labels)} cached label(s) used, the other markets are kept unlabelled this run")
    failed = sorted({str(m.get("id")) for g in groups for m in g["markets"]
                     if m.get("id") is not None and str(m.get("id")) not in labels})
    return labels, {"labelled": len(labels), "cached": sum(1 for x in labels.values() if x.get("cached")),
                    "asked": done["asked"], "failed": len(failed), "failed_ids": failed, "error": error,
                    "batch_errors": batch_errors, "seconds": round(time.time() - t0, 1)}


def sample_groups(groups, n):
    """n groups spread evenly over the pool ranked by kept volume (the
    biggest, the smallest and evenly between), for --limit-events: a
    timing sample that is neither all heavy nor all light events."""
    if n is None or n >= len(groups):
        return list(groups)
    ranked = sorted(groups, key=lambda g: (-sum(filters.volume(m) for m in g["markets"]), str(g["event_id"])))
    if n <= 0:
        return []
    if n == 1:
        return ranked[:1]
    idx = sorted({round(i * (len(ranked) - 1) / (n - 1)) for i in range(n)})
    return [ranked[i] for i in idx]


def check_event(group, flags, window, fetch_trades, budget=None):
    """The started check of one event: every pool market's window trades,
    its big buyers flagged (prefetched together), the event rule."""
    t0 = time.time()
    if budget is not None and budget.expired(reserve=10):
        return {"status": "not_checked", "signal": None, "markets": [], "errors": [], "capped": False,
                "window": window, "seconds": 0.0}
    errors, mk, capped = [], [], False
    for m in group["markets"]:
        cid = m.get("conditionId")
        try:
            rows, cap = fetch_trades(cid, window["from"], window["to"])
            capped = capped or bool(cap)
            vol = filters.volume(m)
            bigs = big_buyers(rows, vol, window["from"], window["to"])
            if hasattr(flags, "prefetch"):     # one parallel look-up for the market's big buyers
                flags.prefetch([(b["wallet"], b["first_buy_ts"], b["net_usd"]) for b in bigs])
            s = started_signal(rows, vol, flags, window["from"], window["to"])
            for b in s["big_buyers"]:
                b["question"], b["condition_id"] = m.get("question"), cid
            bad = [b for b in s["big_buyers"] if (b.get("flag") or {}).get("error")]
            if bad:
                errors.append(f"{(m.get('question') or cid)[:60]}: the prior look-up failed for "
                              f"{_n(len(bad), 'big buyer')} ({scrub(bad[0]['flag']['error'], 160)})")
            s.update(question=m.get("question"), condition_id=cid, fills=len(rows), capped=bool(cap))
            mk.append(s)
        except Exception as e:  # an API error on one market: the event's check is incomplete
            errors.append(f"{(m.get('question') or cid)[:60]}: {scrub(f'{type(e).__name__}: {e}', 200)}")
    sig = event_signal(mk)
    if errors and not sig["started"]:
        status = "error"          # a failed market or look-up could hold the missing flagged money
    else:
        status = "started" if sig["started"] else "not_started"
    return {"status": status, "signal": sig, "markets": mk, "errors": errors, "capped": capped,
            "window": window, "seconds": round(time.time() - t0, 2)}


def _fetch_trades(cid, since, until):
    return net.recent_trades(cid, since, until, taker_only=False, max_rows=MAX_WINDOW_ROWS)


def scan(conn, now=None, limit_events=None, variant=method.DEFAULT_VARIANT, window_hours=WINDOW_HOURS,
         top=TOP_N, next_n=NEXT_N, workers=EVENT_WORKERS, budget=None, db_path=None, save=True,
         walk=None, label=None, fetch_trades=None, flags=None, ai_client=None):
    """Build today's open list. Returns the report (see write_report).
    walk / label / fetch_trades / flags replace the network pieces in tests:
    walk() -> [open event]; label(groups, conn, budget, today) -> (labels,
    info); fetch_trades(cid, since, until) -> (rows, capped); flags = a
    flag_fn with .prefetch()."""
    t_all = time.monotonic()
    now = now_epoch() if now is None else int(now)
    today = day_str(utc_day(now))
    budget = budget or Budget(None)
    ensure_schema(conn)
    timing = {}
    with Heartbeat() as hb:
        # 1. pool
        hb.set("walking open events")
        log(f"open list for {today}: walking open events (volume >= ${filters.MIN_VOLUME:,}) …")
        t0 = time.monotonic()
        pages = Counter()

        def on_page(n, page):
            pages["pages"], pages["events"] = n, pages["events"] + len(page)
            hb.set("walking open events", f"{n} pages, {pages['events']} events")

        events = walk() if walk else net.walk_open_events(min_volume=filters.MIN_VOLUME, on_page=on_page)
        read_at = now_epoch() if walk is None else now
        groups, counts = filters.open_pool(events, now=now)
        timing["walk_s"] = round(time.monotonic() - t0, 1)
        n_pool_events, n_pool_markets = len(groups), sum(len(g["markets"]) for g in groups)
        log(f"  {len(events)} open events walked in {timing['walk_s']} s; keyword pool {n_pool_events} events / "
            f"{n_pool_markets} markets ({dict(counts.most_common())})")
        if limit_events is not None:
            groups = sample_groups(groups, limit_events)
            log(f"  --limit-events {limit_events}: {len(groups)} events spread over the pool by volume rank "
                f"({sum(len(g['markets']) for g in groups)} markets)")

        # 2. AI labels and exclusions
        hb.set("AI labels", f"{sum(len(g['markets']) for g in groups)} markets")
        t0 = time.monotonic()
        if label:
            labels, ai_info = label(groups, conn, budget, today)
        else:
            labels, ai_info = _label_all(groups, conn, budget, ai_client or ai.Client(), today)
        kept, excluded = ai.apply_exclusions(groups, labels)
        timing["ai_s"] = round(time.monotonic() - t0, 1)
        # every pool market left without a label failed this run (call failed, key refused, budget): its
        # card says so, it is never dropped, and a cached label still excludes its market
        label_failed = {str(m.get("id")) for g in groups for m in g["markets"]
                        if m.get("id") is not None and str(m.get("id")) not in labels}
        ai_info = {**(ai_info or {}), "failed": len(label_failed), "failed_ids": sorted(label_failed)}
        if ai_info.get("error"):
            ai_info["error"] = scrub(ai_info["error"])
        unlabelled = sum(1 for g in kept for m in g["markets"] if str(m.get("id")) not in labels)
        log(f"  AI: {ai_info.get('labelled')} labelled ({ai_info.get('cached')} cached) in {timing['ai_s']} s; "
            f"excluded {dict(Counter(lab['type'] for _, lab in excluded))}; {len(kept)} events left"
            + (f"; {unlabelled} market(s) kept unlabelled ({LABEL_FAILED})" if unlabelled else ""))

        # 3. track records: ONE snapshot, taken before any wallet look-up writes to the store, so every
        #    event ranks on the same ended events
        hb.set("track records", f"{len(kept)} events")
        t0 = time.monotonic()
        snap = track_record.snapshot(conn, None, variant)
        tracks, series = {}, {}
        for g in kept:
            type_, _ = event_type(g["markets"], labels)
            series[g["event_id"]] = filters.series_key(g["event"])
            tracks[g["event_id"]] = snap.rate(type_, series[g["event_id"]])
        timing["track_s"] = round(time.monotonic() - t0, 2)
        log(f"  track records for {len(kept)} events from {len(snap.events)} ended events in the store "
            f"({timing['track_s']} s): basis {dict(Counter(t['basis'] for t in tracks.values()))}")

        # 4. already started?
        window = {"from": now - int(window_hours * 3600), "to": now, "hours": window_hours}
        flags = flags or MethodFlags(conn, variant)
        fetch = fetch_trades or _fetch_trades
        checks, done = {}, Counter()
        t0 = time.monotonic()
        log(f"  started checks: {len(kept)} events, {sum(len(g['markets']) for g in kept)} markets, trades "
            f"{iso(window['from'])} .. {iso(window['to'])}, {workers} at a time")
        hb.set("started checks", f"0/{len(kept)} events")
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(check_event, g, flags, window, fetch, budget): g for g in kept}
            for fut in as_completed(futs):
                g = futs[fut]
                c = fut.result()
                checks[g["event_id"]] = c
                done[c["status"]] += 1
                n = sum(done.values())
                el = time.monotonic() - t0
                eta = el / n * (len(kept) - n)
                sig = c.get("signal") or {}
                log(f"  [{n}/{len(kept)}] {c['status']:<11} {c['seconds']:5.1f} s  "
                    f"{sum(s.get('fills', 0) for s in c['markets']):>6} fills  "
                    f"{len(sig.get('big_buyers') or [])} big / {len(sig.get('wallets') or [])} flagged  "
                    f"{_usd(sig.get('flagged_usd'))}  {str(g['event'].get('title'))[:60]}  (ETA {eta:.0f} s)")
                hb.set("started checks", f"{n}/{len(kept)} events, {dict(done)}, ETA {eta:.0f} s")
        timing["started_s"] = round(time.monotonic() - t0, 1)

    # 5. rank, rotate, store, report
    entries = [build_entry(g, labels, tracks[g["event_id"]], series[g["event_id"]], checks[g["event_id"]], now,
                           read_at, label_failed) for g in kept]
    ranked = rank_events(entries, top=len(entries), next_n=0, now_ts=now)["top"]
    prev_day, prev_ids = previous_list(conn, today)
    unchecked = sum(1 for c in checks.values() if c["status"] == "not_checked")
    complete = limit_events is None and not unchecked
    top_ids = rotate(prev_ids, [e["event_id"] for e in ranked], size=top,
                     pinned=[e["event_id"] for e in ranked if e["started"]])
    top_set = set(top_ids)
    by_id = {e["event_id"]: e for e in ranked}
    top_entries = [by_id[i] for i in top_ids]
    next_entries = [e for e in ranked if e["event_id"] not in top_set][:next_n]
    rotation = {i: ("kept" if i in set(prev_ids) else "new") for i in top_ids}
    if save:
        save_list(conn, today, complete, top_entries, next_entries, rotation, run_at=now)
    rank_of = {e["event_id"]: i for i, e in enumerate(ranked, 1)}

    def card(e, pos, name):
        c = dict(e["card"])
        return {"list": name, "position": pos, "rank": rank_of[e["event_id"]],
                "why": why_ranked(e, now, window_hours),
                **({"rotation": rotation[e["event_id"]]} if name == "top" else {}), **c}

    per_event = [c["seconds"] for c in checks.values() if c["status"] != "not_checked"]
    report = {
        "generated_at": iso(now), "day": today,
        "db": _rel(db_path) if db_path else None,
        "about": (f"Today's open events most worth watching for insider money: started events first (flagged net "
                  f"buys in the last {window_hours:g} h), then by how often ended events of the same series or type "
                  f"carried heavy flagged money. Cards describe trades and past events only; nothing here says how "
                  f"an open event will resolve."),
        "rules": {
            "pool": (f"open markets with volume >= ${filters.MIN_VOLUME:,}, endDate <= now + {filters.HORIZON_DAYS} "
                     f"days, highest outcome price < {filters.DECIDED_PRICE}, not a game / price / weather market "
                     f"(keywords, then AI type: {sorted(ai.EXCLUDED_TYPES)} excluded)"),
            "started": started_rule(window_hours),
            "big_buyer": BIG_BUYER_RULE,
            "flagging": (f"the method's flags (prior trades <= 10 or position >= 80% of prior volume), prior "
                         f"history at the wallet's first buy in the window, cutoff variant {variant}"),
            "track_record": (f"k of n ended events with flagged_pct >= {track_record.HEAVY_PCT:g}%, by series once "
                             f"it has >= {track_record.MIN_SERIES_N} ended events, else by AI type; Wilson 95% "
                             f"interval"),
            "rank": (f"started events first (more flagged $ first); then the track record's Wilson 95% lower bound "
                     f"(a 1-of-3 series, 6-79%, ranks below 33 of 169, 14-26%), then its n (more ended events "
                     f"first), no record last; ties: a scheduled reveal within {SOON_DAYS} days first, then the "
                     f"sooner end date, then event id"),
            "rotation": (f"walk today's ranking from the top: an event not on the previous day's top {top} is always "
                         f"taken, one of the previous day's only while fewer than {KEEP_MAX} of them are in, and the "
                         f"walk stops at {top} (so >= {top - KEEP_MAX} new whenever that many exist, and nothing "
                         f"stale sits above a better event left out); too few new events: topped up with the "
                         f"previous day's next-ranked; a started event is always taken"),
            "type": TYPE_RULE + f" (no label: \"{LABEL_FAILED}\")",
            "reveal": "the earliest scheduled reveal among the event's markets, else any_day, else past, else unclear",
        },
        "fields": fields_legend(window_hours, top),
        "pool": {"open_events_walked": len(events), "pool_reasons": dict(counts.most_common()),
                 "keyword_pool": {"events": n_pool_events, "markets": n_pool_markets},
                 "limited_to": limit_events, "sampled_events": len(groups) if limit_events is not None else None,
                 "after_ai": {"events": len(kept), "markets": sum(len(g["markets"]) for g in kept)},
                 "ai_excluded": dict(Counter(lab["type"] for _, lab in excluded)), "unlabelled_kept": unlabelled,
                 "label_failed": {"markets_kept": unlabelled,
                                  "events": sum(1 for e in ranked if e.get("label_failed")),
                                  "card_note": LABEL_FAILED},
                 "ai": ai_info},
        "track_record_source": {"ended_events": len(snap.events), "variant": variant,
                                "first_close": iso(min((e["closed_ts"] for e in snap.events if e["closed_ts"]),
                                                       default=None)),
                                "last_close": iso(max((e["closed_ts"] for e in snap.events if e["closed_ts"]),
                                                      default=None))},
        "rotation": {"previous_day": prev_day, "previous_top": prev_ids,
                     "kept": [i for i in top_ids if rotation[i] == "kept"],
                     "new": [i for i in top_ids if rotation[i] == "new"], "keep_max": KEEP_MAX,
                     "stored_as": ("not stored" if not save else "complete" if complete
                                   else "sample (not used for rotation)")},
        "counts": {"events": len(ranked), "started": sum(1 for e in ranked if e["started"]),
                   "check_status": dict(Counter(c["status"] for c in checks.values())),
                   "with_track_record": sum(1 for e in ranked if (e["track"] or {}).get("rate") is not None),
                   "scheduled_within_14_days": sum(1 for e in ranked if scheduled_soon(e, now))},
        "top": [card(e, i, "top") for i, e in enumerate(top_entries, 1)],
        "next": [card(e, i, "next") for i, e in enumerate(next_entries, 1)],
        "ranking": [{"rank": i, "event_id": e["event_id"], "title": e["title"], "started": e["started"],
                     "check": e["started_status"], "flagged_usd": e["card"]["already_started"]["flagged_net_usd"],
                     "rate": (e["track"] or {}).get("rate"), "basis": (e["track"] or {}).get("basis"),
                     "k": (e["track"] or {}).get("k"), "n": (e["track"] or {}).get("n"),
                     "reveal": e["reveal_kind"], "reveal_date": e["card"]["reveal"]["date"],
                     "end_date": e["card"]["end"]["end_date"], "why": why_ranked(e, now, window_hours)}
                    for i, e in enumerate(ranked, 1)],
        "errors": [{"event_id": i, "errors": [scrub(x) for x in c["errors"]]}
                   for i, c in checks.items() if c["errors"]],
    }
    timing["total_s"] = round(time.monotonic() - t_all, 1)
    timing["per_event_check_s"] = ({"mean": round(statistics.mean(per_event), 2),
                                    "median": round(statistics.median(per_event), 2),
                                    "p90": round(sorted(per_event)[int(0.9 * (len(per_event) - 1))], 2),
                                    "max": round(max(per_event), 2), "n": len(per_event)} if per_event else None)
    timing["requests"] = net.bucket_stats()
    if per_event and kept:
        wall_per_event = timing["started_s"] / len(per_event)
        # a sample's after-AI share stands in for the whole pool's
        full = round(n_pool_events * len(kept) / max(1, len(groups))) if limit_events is not None else len(kept)
        timing["estimate_full_run"] = {
            "events": full, "started_checks_s": round(wall_per_event * full),
            "total_s": round(timing["walk_s"] + wall_per_event * full + (timing["ai_s"] if limit_events is None
                                                                          else 0)),
            "note": (f"started-check wall time per event ({wall_per_event:.1f} s with {workers} workers) x events in "
                     f"the pool, plus the walk; AI labels cost ~{ai.MIN_INTERVAL} s per {ai.BATCH_SIZE} uncached "
                     f"markets on top, nothing when cached")}
    report["timing"] = timing
    return report


def _rel(path):
    full = os.path.abspath(path)
    return os.path.relpath(full, ROOT) if full.startswith(ROOT + os.sep) else full


def write_report(report, path=OUT_PATH):
    """Write the report JSON atomically."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)
    return path


def print_list(rep):
    for name in ("top", "next"):
        log(f"{name}:")
        for c in rep[name]:
            st = c["already_started"]
            t = c["track_record"]
            log(f"  {c['position']:>2}. [{c.get('rotation', '-'):<4}] {st['status']:<11} "
                f"{_usd(st['flagged_net_usd']):>9}  {t['k'] if t['k'] is not None else '-'}/"
                f"{t['n'] if t['n'] is not None else '-'} {str(t['basis']):<6}  {c['reveal']['kind']:<10} "
                f"{(c['end']['end_date'] or '')[:10]}  {str(c['title'])[:60]}")


def output_path(out=None, limit_events=None):
    """Where the report goes: --out when given; else the sample file for a
    --limit-events run (a sample must never overwrite the real demo file);
    else OUT_PATH."""
    if out:
        return out
    return SAMPLE_OUT_PATH if limit_events is not None else OUT_PATH


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Today's open list (demo output 5): top 10 + next 10 open events "
                                             "with card data.")
    ap.add_argument("--db", default=DB_PATH, help="the store (default the real one): AI labels, track record, "
                                                  "wallet look-up cache and the open_lists table are read and "
                                                  "written here")
    ap.add_argument("--limit-events", type=int, default=None, metavar="N",
                    help="check only N events spread over the pool (a sample: stored, but not used for rotation)")
    ap.add_argument("--out", default=None, help=f"where to write the JSON (default {_rel(OUT_PATH)}; a "
                                                f"--limit-events run: {_rel(SAMPLE_OUT_PATH)})")
    ap.add_argument("--variant", default=method.DEFAULT_VARIANT, choices=list(method.VARIANTS))
    ap.add_argument("--window-hours", type=float, default=WINDOW_HOURS)
    ap.add_argument("--workers", type=int, default=EVENT_WORKERS, help="events checked at once")
    ap.add_argument("--budget-seconds", type=float, default=None,
                    help="stop starting started-checks when this runs out (unchecked events say so)")
    ap.add_argument("--no-save", action="store_true", help="do not store today's list")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.db):        # store.connect would create an empty store and rank on nothing
        ap.error(f"no store at {args.db} (--db must be an existing store file)")
    out = output_path(args.out, args.limit_events)
    log(f"open list: store {_rel(args.db)}, variant {args.variant}, -> {_rel(out)}")
    conn = store.connect(args.db)
    net.install()
    rep = scan(conn, limit_events=args.limit_events, variant=args.variant, window_hours=args.window_hours,
               workers=args.workers, budget=Budget(args.budget_seconds), db_path=args.db, save=not args.no_save)
    print_list(rep)
    t = rep["timing"]
    log(f"timing: walk {t['walk_s']} s, AI {t['ai_s']} s, track {t['track_s']} s, started checks "
        f"{t['started_s']} s, total {t['total_s']} s; per event {t.get('per_event_check_s')}")
    if t.get("estimate_full_run"):
        log(f"estimate for the full pool: {t['estimate_full_run']}")
    log(f"requests: {t['requests']}")
    path = write_report(rep, out)
    log(f"open list -> {_rel(path)}")
    return rep


if __name__ == "__main__":
    main()
