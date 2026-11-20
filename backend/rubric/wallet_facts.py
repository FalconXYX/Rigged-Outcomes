"""
The facts of every whale of a market: the inputs a rubric scores.

  Market       one market's inputs: its fills (one row per fill), its clean positions, the whales the site
               scores today with their current flags, and its known news times
  context()    what the whole market says: volume, close, when the answer became public (reveal_ts) and
               the winning side's probability over time
  Polymarket   the wallet history a factor needs, from Polymarket's free Data API v2, cached on disk and
               spaced (at most `rate` requests a second); getter= swaps the network for a fake in tests
  FACTORS      every factor by name: {name: Factor(name, group, unit, doc, fn)}; @factor adds one
  market_facts(market, source) -> {"market", "factors", "whales": [{"wallet", "facts", "flags"}]}

The CLI that builds every market's facts file is backend/rubric/fact_sources.py.

A factor is fn(w) -> number | bool | None, where w carries:
  w.m      context(market)          w.row    the whale's clean position (a dict)
  w.flags  today's flags            w.hist   wallet_history() of the wallet as of its first buy
  w.fills  the wallet's own fills in this market (a DataFrame)
  w.peers  the other whales' (first_buy_ts, fresh) pairs, for clustering
None means "not known for this whale"; the rubric is told so instead of being given a made-up value.

History is read as of the whale's first buy here (end=first_buy_ts), so nothing a wallet did afterwards
leaks into its facts. Times are epoch seconds (UTC).
"""
import hashlib
import json
import os
import threading
import time
from bisect import bisect_left
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

DATA_API = "https://data-api.polymarket.com/v2"
PAGE = 1000
HISTORY_PAGES = 10          # 10 000 trades before the first buy; a busier wallet is marked history_capped
POSITION_PAGES = 10
REVEAL_PROB = 0.90          # the winning side's probability from which the answer counts as public
BUCKET_S = 15 * 60          # the probability line's resolution
CLUSTER_S = 6 * 3600        # two first buys this close are "the same window"
FRESH_TRADES = 10           # a fresh wallet: at most this many prior trades ...
FRESH_DAYS = 14             # ... or at most this many days old at its first buy
DAY = 86400


# ── registry ─────────────────────────────────────────────────────────────────

Factor = namedtuple("Factor", "name group unit doc fn")
FACTORS = {}


def factor(name, group, unit, doc):
    """Register fn(w) as the factor `name` (FACTORS keeps the order they are defined in)."""
    def add(fn):
        if name in FACTORS:
            raise ValueError(f"factor {name!r} defined twice")
        FACTORS[name] = Factor(name, group, unit, doc, fn)
        return fn
    return add


def catalog():
    """The factors' descriptions, for the facts file and the rubric's prompt."""
    return {f.name: {"group": f.group, "unit": f.unit, "doc": f.doc} for f in FACTORS.values()}


# ── market ───────────────────────────────────────────────────────────────────

class Market(SimpleNamespace):
    """name, slug, title, outcome (the winning outcome's name), condition_id,
    fills   DataFrame: wallet, side, outcome, price, contracts, dollars, ts (epoch s)
    clean   DataFrame: the clean CSV (cleandata's columns)
    whales  list of dicts: wallet, user_id, first_trade (the clean row's ISO text), first_buy_ts, plus
            today's flags: prior_bet_count, concentration, is_first_x, is_percent, is_suspicious,
            category, risk_score
    news    list of (ts, label): known moments the answer came out (markets.json importantDates)"""


def risk_score(prior_bet_count, concentration):
    """export.build_traders' 0-100 score (the constellation's radius)."""
    return concentration / 100.0 * 50 + max(0.0, (10 - prior_bet_count) / 10.0) * 50


def category(is_first_x, is_percent):
    """export.categorize."""
    if is_first_x and is_percent:
        return "critical"
    return "newAccount" if is_first_x else ("concentration" if is_percent else "normal")


def _win_prob(fills, outcome):
    win = fills["outcome"].astype(str).str.lower() == str(outcome).lower()
    p = fills["price"].astype(float).clip(0, 1)
    return p.where(win, 1 - p)


def context(market):
    """The market-level facts every whale's factors read. The answer counts as public (reveal_ts) from the
    first 15-minute bucket after which the winning side's dollar-weighted median price never falls below
    REVEAL_PROB again; a known news time (market.news) is kept beside it, not in its place."""
    import numpy as np
    import pandas as pd

    f = market.fills
    f = f[f["dollars"] > 0]
    prob = _win_prob(f, market.outcome)
    bucket = (f["ts"] // BUCKET_S) * BUCKET_S
    frame = pd.DataFrame({"b": bucket, "p": prob, "w": f["dollars"]}).sort_values(["b", "p"])

    def wmedian(g):
        c = g["w"].cumsum()
        return float(g["p"].iloc[int(np.searchsorted(c, c.iloc[-1] / 2))])

    line = frame.groupby("b")[["p", "w"]].apply(wmedian).sort_index()
    t, p = line.index.to_numpy(dtype="int64"), line.to_numpy(dtype=float)
    below = np.nonzero(p < REVEAL_PROB)[0]
    if len(below) == 0:
        reveal_i, method = 0, "always_above"
    elif below[-1] == len(p) - 1:
        reveal_i, method = None, "never"
    else:
        reveal_i, method = int(below[-1]) + 1, "price"
    reveal_ts = int(t[reveal_i]) if reveal_i is not None else int(f["ts"].max())
    pre = p[(t < reveal_ts) & (t >= reveal_ts - DAY)]
    news = sorted(market.news or [])
    winning_buys = f[(f["side"] == "BUY") & (f["outcome"].astype(str).str.lower() == str(market.outcome).lower())]
    winning_buys = winning_buys.sort_values("ts", kind="stable")
    return SimpleNamespace(
        market=market,
        volume=float(market.clean["dollar_amount"].sum()),
        positions=int(len(market.clean)),
        first_ts=int(f["ts"].min()),
        close_ts=int(f["ts"].max()),
        reveal_ts=reveal_ts,
        reveal_method=method,
        pre_reveal_prob=float(np.median(pre)) if len(pre) else None,
        news_ts=int(news[0][0]) if news else None,
        news_label=news[0][1] if news else None,
        line_t=t.tolist(), line_p=p.tolist(),
        buy_ts=winning_buys["ts"].astype("int64").tolist(),
        buy_cum=[0.0] + winning_buys["dollars"].cumsum().astype(float).tolist(),
        dollars=sorted(market.clean["dollar_amount"].astype(float).tolist()),
    )


def prob_before(m, ts):
    """The winning side's probability in the last bucket before ts (None before the first trade)."""
    i = bisect_left(m.line_t, int(ts)) - 1
    return float(m.line_p[i]) if i >= 0 else None


def buy_dollars(m, start, end):
    """Winning-side BUY dollars of every wallet with start <= ts < end."""
    return m.buy_cum[bisect_left(m.buy_ts, end)] - m.buy_cum[bisect_left(m.buy_ts, start)]


# ── Polymarket ───────────────────────────────────────────────────────────────

class Polymarket:
    """The Data API v2 reads the history factors need. getter(url, params) -> decoded JSON (default
    polymarket_api.get_json: retries, back-off, errors raised). Every answer is kept under cache_dir (one
    file per request), so a second run makes no request; misses are spaced 1/rate seconds apart across
    threads. `requests` counts the misses."""

    def __init__(self, cache_dir=None, rate=5.0, getter=None):
        if getter is None:
            import polymarket_api
            getter = polymarket_api.get_json
        self.getter, self.cache_dir, self.gap = getter, cache_dir, 1.0 / rate if rate else 0.0
        self._lock, self._next = threading.Lock(), 0.0
        self.requests = 0
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def get(self, path, params):
        url = f"{DATA_API}/{path}"
        key = hashlib.sha1(json.dumps([url, sorted(params.items())], default=str).encode()).hexdigest()
        file = os.path.join(self.cache_dir, key[:2], key + ".json") if self.cache_dir else None
        if file and os.path.exists(file):
            with open(file, encoding="utf-8") as fh:
                return json.load(fh)
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.gap
            self.requests += 1
        if start > now:
            time.sleep(start - now)
        payload = self.getter(url, dict(params))
        if file:
            os.makedirs(os.path.dirname(file), exist_ok=True)
            tmp = f"{file}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, file)
        return payload

    def pages(self, path, params, max_pages):
        """(rows, capped): a cursor feed followed to its end or to max_pages."""
        rows, cursor, seen = [], None, set()
        for n in range(1, max_pages + 1):
            payload = self.get(path, {**params, **({"cursor": cursor} if cursor else {})})
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise RuntimeError(f"/v2/{path}: unexpected answer {str(payload)[:200]}")
            rows.extend(data)
            pg = payload.get("pagination") or {}
            cursor = pg.get("next_cursor")
            if not pg.get("has_more") or not cursor or cursor in seen:
                return rows, False
            seen.add(cursor)
        return rows, True

    def trades_before(self, wallet, cutoff):
        """The wallet's TRADE rows with ts <= cutoff, newest first (HISTORY_PAGES at most)."""
        return self.pages("activity", {"user": wallet, "type": "TRADE", "start": 1, "end": int(cutoff),
                                       "limit": PAGE}, HISTORY_PAGES)

    def first_trade(self, wallet):
        rows = self.get("activity", {"user": wallet, "type": "TRADE", "start": 1, "limit": 1,
                                     "sort_direction": "ASC"}).get("data") or []
        return int(rows[0]["timestamp"]) if rows else None

    def closed_before(self, wallet, cutoff):
        """Positions the wallet had fully closed (sold or redeemed) before cutoff."""
        return self.pages("positions", {"user": wallet, "status": "CLOSED", "end": int(cutoff) - 1,
                                        "limit": PAGE}, POSITION_PAGES)

    def join_date(self, wallet):
        data = self.get("user-stats", {"user": wallet}).get("data") or {}
        return int(data["join_date"]) if data.get("join_date") else None


def wallet_history(source, wallet, cutoff, condition_id):
    """What the history factors need of a wallet as of cutoff (its first buy in this market), with this
    market's own rows left out of every "prior" number."""
    rows, capped = source.trades_before(wallet, cutoff)
    cid = str(condition_id).lower()
    prior = [r for r in rows if str(r.get("condition_id") or "").lower() != cid
             and int(r.get("timestamp") or 0) <= cutoff]
    first = min((int(r["timestamp"]) for r in rows), default=None)
    if capped or first is None:
        first = source.first_trade(wallet) or first
    closed, closed_capped = source.closed_before(wallet, cutoff)
    closed = [p for p in closed if str(p.get("condition_id") or "").lower() != cid]
    pnl = [float(p.get("realized_pnl") or 0) for p in closed]
    return {
        "prior_rows": len(prior),
        "prior_txs": len({r.get("transaction_hash") for r in prior}),
        "prior_markets": len({r.get("condition_id") for r in prior}),
        "prior_volume": sum(float(r.get("usdc_size") or 0) for r in prior),
        "last_prior_ts": max((int(r["timestamp"]) for r in prior), default=None),
        "first_trade_ts": first,
        "join_ts": source.join_date(wallet),
        "closed": len(closed),
        "closed_wins": sum(1 for x in pnl if x > 0),
        "closed_pnl": sum(pnl),
        "capped": bool(capped or closed_capped),
    }


# ── factors: timing ──────────────────────────────────────────────────────────

def _hours(a, b):
    return None if a is None or b is None else round((a - b) / 3600, 3)


@factor("hours_before_reveal", "timing", "hours",
        "Hours from the wallet's first buy to the moment the market priced the answer as public "
        "(negative: it bought after)")
def _(w):
    return _hours(w.m.reveal_ts, w.row["first_buy_ts"])


@factor("hours_before_news", "timing", "hours",
        "Hours from the first buy to the known news moment, when the market has one")
def _(w):
    return _hours(w.m.news_ts, w.row["first_buy_ts"])


@factor("hours_before_close", "timing", "hours", "Hours from the first buy to the market's last trade")
def _(w):
    return _hours(w.m.close_ts, w.row["first_buy_ts"])


@factor("entry_timing_pct", "timing", "percent",
        "Where the first buy falls in the market's life before the reveal: 0 = at its first trade, "
        "100 = at the reveal")
def _(w):
    span = w.m.reveal_ts - w.m.first_ts
    if span <= 0:
        return None
    return round(100 * (w.row["first_buy_ts"] - w.m.first_ts) / span, 2)


# ── factors: price ───────────────────────────────────────────────────────────

@factor("entry_price", "price", "probability", "Average price paid for the winning side (0-1)")
def _(w):
    return round(float(w.row["avg_odds"]), 4)


@factor("payout_multiple", "price", "x", "What a dollar staked returned at that price (1 / price)")
def _(w):
    p = float(w.row["avg_odds"])
    return round(1 / p, 3) if p > 0 else None


@factor("market_prob_at_entry", "price", "probability",
        "The market's probability of the winning side just before the first buy")
def _(w):
    p = prob_before(w.m, w.row["first_buy_ts"])
    return None if p is None else round(p, 4)


@factor("prob_move_after_entry", "price", "probability",
        "How far the winning side's probability rose from just before the first buy to 24 hours later")
def _(w):
    a = prob_before(w.m, w.row["first_buy_ts"])
    b = prob_before(w.m, w.row["first_buy_ts"] + DAY)
    return None if a is None or b is None else round(b - a, 4)


# ── factors: size ────────────────────────────────────────────────────────────

@factor("position_usd", "size", "usd", "Dollars put on the winning side in this market")
def _(w):
    return round(float(w.row["dollar_amount"]), 2)


@factor("share_of_volume_pct", "size", "percent", "The position as a share of the market's total volume")
def _(w):
    return round(100 * float(w.row["dollar_amount"]) / w.m.volume, 4) if w.m.volume else None


@factor("size_rank_pct", "size", "percent", "Percent of the market's positions smaller than this one")
def _(w):
    d = w.m.dollars
    return round(100 * bisect_left(d, float(w.row["dollar_amount"])) / len(d), 2) if d else None


@factor("profit_usd", "size", "usd", "What the position paid out above its cost")
def _(w):
    return round(max(0.0, float(w.row["contracts"]) - float(w.row["dollar_amount"])), 2)


# ── factors: position building and exit ─────────────────────────────────────

def _side(w, side, winning=True):
    f = w.fills
    win = f["outcome"].astype(str).str.lower() == str(w.m.market.outcome).lower()
    return f[(f["side"] == side) & (win if winning else ~win)]


@factor("buy_fills", "build", "count", "Fills that built the winning position")
def _(w):
    return int(w.row["num_trades"])


@factor("build_span_hours", "build", "hours", "Hours from the position's first fill to its last")
def _(w):
    return _hours(w.row["last_buy_ts"], w.row["first_buy_ts"])


@factor("sold_before_close_pct", "build", "percent",
        "Share of the winning contracts bought that the wallet sold before the market closed")
def _(w):
    bought = float(_side(w, "BUY")["contracts"].sum())
    sold = float(_side(w, "SELL")["contracts"].sum())
    return round(min(100.0, 100 * sold / bought), 2) if bought else None


@factor("sold_before_reveal_pct", "build", "percent",
        "Share of the winning contracts sold before the answer was public")
def _(w):
    bought = float(_side(w, "BUY")["contracts"].sum())
    s = _side(w, "SELL")
    sold = float(s[s["ts"] < w.m.reveal_ts]["contracts"].sum())
    return round(min(100.0, 100 * sold / bought), 2) if bought else None


@factor("hedge_usd", "build", "usd", "Dollars the wallet also bought of the losing side in this market")
def _(w):
    return round(float(_side(w, "BUY", winning=False)["dollars"].sum()), 2)


# ── factors: wallet history (as of the first buy) ───────────────────────────

@factor("prior_trades", "history", "count",
        "The wallet's trades in other markets before its first buy here (transactions)")
def _(w):
    return w.hist["prior_txs"]


@factor("prior_markets", "history", "count", "Distinct other markets it had traded before")
def _(w):
    return w.hist["prior_markets"]


@factor("prior_volume_usd", "history", "usd", "Dollars it had traded in other markets before")
def _(w):
    return round(w.hist["prior_volume"], 2)


@factor("concentration_pct", "history", "percent",
        "This position as a share of everything the wallet had staked, this position included")
def _(w):
    pos = float(w.row["dollar_amount"])
    total = pos + w.hist["prior_volume"]
    return round(100 * pos / total, 2) if total else None


@factor("wallet_age_days", "history", "days",
        "Days from the wallet's first Polymarket trade (or profile creation, if earlier) to its first buy here")
def _(w):
    starts = [t for t in (w.hist["first_trade_ts"], w.hist["join_ts"]) if t]
    if not starts:
        return None
    return round(max(0.0, (w.row["first_buy_ts"] - min(starts)) / DAY), 3)


@factor("dormant_days", "history", "days",
        "Days since its last trade in another market (None when it had none)")
def _(w):
    t = w.hist["last_prior_ts"]
    return None if t is None else round(max(0.0, (w.row["first_buy_ts"] - t) / DAY), 3)


@factor("prior_closed_positions", "history", "count", "Positions in other markets it had fully closed before")
def _(w):
    return w.hist["closed"]


@factor("prior_win_rate_pct", "history", "percent", "Share of those closed positions that made money")
def _(w):
    n = w.hist["closed"]
    return round(100 * w.hist["closed_wins"] / n, 2) if n else None


@factor("prior_pnl_usd", "history", "usd", "Realized profit or loss on those closed positions")
def _(w):
    return round(w.hist["closed_pnl"], 2)


@factor("history_capped", "history", "flag",
        "True when the wallet had over 10 000 trades before, so its counts are floors")
def _(w):
    return w.hist["capped"]


# ── factors: crowd ───────────────────────────────────────────────────────────

def is_fresh(prior_trades, age_days):
    return (prior_trades is not None and prior_trades <= FRESH_TRADES) or \
           (age_days is not None and age_days <= FRESH_DAYS)


@factor("whales_same_window", "crowd", "count",
        "Other whales whose first buy on the winning side fell within 6 hours of this one")
def _(w):
    t = w.row["first_buy_ts"]
    return sum(1 for pt, _f in w.peers if abs(pt - t) <= CLUSTER_S)


@factor("fresh_whales_same_window", "crowd", "count",
        "Of those, how many were fresh wallets (10 or fewer prior trades, or 14 days old or less)")
def _(w):
    t = w.row["first_buy_ts"]
    return sum(1 for pt, fresh in w.peers if fresh and abs(pt - t) <= CLUSTER_S)


@factor("buy_surge_ratio", "crowd", "x",
        "Winning-side buying by everyone in the 6 hours around the first buy, against the market's "
        "average 6 hours before the reveal")
def _(w):
    m, t = w.m, w.row["first_buy_ts"]
    span = max(CLUSTER_S, m.reveal_ts - m.first_ts)
    base = buy_dollars(m, m.first_ts, m.reveal_ts) * CLUSTER_S / span
    here = buy_dollars(m, t - CLUSTER_S / 2, t + CLUSTER_S / 2)
    return round(here / base, 3) if base > 0 else None


# ── putting it together ──────────────────────────────────────────────────────

def _iso(ts):
    return None if ts is None else datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _position_rows(market):
    """The clean row (as a dict, with first/last buy as epoch s) of each whale, in market.whales' order."""
    import pandas as pd
    clean = market.clean
    first = pd.to_datetime(clean["first_trade"], utc=True)
    last = pd.to_datetime(clean["last_trade"], utc=True)
    by_key = {}
    for i, row in enumerate(clean.to_dict("records")):
        row["first_buy_ts"] = int(first.iloc[i].timestamp())
        row["last_buy_ts"] = int(last.iloc[i].timestamp())
        by_key.setdefault((str(row["user_id"]).lower(), row["first_buy_ts"]), []).append(row)
    out = []
    for wh in market.whales:
        rows = by_key.get((wh["user_id"].lower(), wh["first_buy_ts"]), [])
        win = [r for r in rows if str(r["outcome"]).lower() == str(market.outcome).lower()] or rows
        if not win:
            raise ValueError(f"{market.name}: whale {wh['user_id']} ({_iso(wh['first_buy_ts'])}) has no clean row")
        out.append(max(win, key=lambda r: float(r["dollar_amount"])))
    return out


def market_facts(market, source=None, workers=5, on_whale=None, histories=None):
    """{"market": the market's context, "factors": catalog(), "whales": [{"wallet", "first_buy",
    "facts": {factor: value}, "flags": today's flags}]}, whales in market.whales' order. histories: each
    whale's wallet_history() already read (a step's checkpoints), in that order; else read through source."""
    m = context(market)
    rows = _position_rows(market)
    fills_by = {k: g for k, g in market.fills.groupby(market.fills["wallet"].str.lower())}
    empty = market.fills.iloc[0:0]

    def hist(i):
        wh = market.whales[i]
        h = wallet_history(source, wh["wallet"], wh["first_buy_ts"], market.condition_id)
        if on_whale:
            on_whale(i)
        return h

    if histories is not None:
        if len(histories) != len(rows):
            raise ValueError(f"{len(histories)} histories for {len(rows)} whales")
        hists = list(histories)
    else:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            hists = list(pool.map(hist, range(len(rows))))

    ages = []
    for row, h in zip(rows, hists):
        starts = [t for t in (h["first_trade_ts"], h["join_ts"]) if t]
        ages.append((row["first_buy_ts"] - min(starts)) / DAY if starts else None)
    fresh = [is_fresh(h["prior_txs"], a) for h, a in zip(hists, ages)]
    first_ts = [r["first_buy_ts"] for r in rows]

    import scrape_insiders as si
    threshold = si.whale_threshold(m.volume)
    whales = []
    for i, (wh, row, h) in enumerate(zip(market.whales, rows, hists)):
        peers = [(first_ts[j], fresh[j]) for j in range(len(rows)) if j != i]
        w = SimpleNamespace(m=m, row=row, flags=wh, hist=h, peers=peers,
                            fills=fills_by.get(str(row["user_id"]).lower(), empty))
        facts = {f.name: f.fn(w) for f in FACTORS.values()}
        flags = {k: wh[k] for k in ("prior_bet_count", "concentration", "is_first_x", "is_percent",
                                    "is_suspicious", "category", "risk_score")}
        side = str(row.get("side") or "BUY").upper()
        # The site's whale sets were scored before scrape_insiders left out winning-side SELL rows (exits)
        # and near-certain buys; current_rules says whether today's whale_selection would keep this one.
        current = side == "BUY" and float(row["avg_odds"]) < si.NEAR_CERTAIN_ODDS and \
            float(row["dollar_amount"]) >= threshold
        whales.append({"wallet": wh["wallet"], "user_id": wh["user_id"], "first_buy": _iso(row["first_buy_ts"]),
                       "position_side": side, "current_rules": current, "facts": facts, "flags": flags})
    return {
        "market": {
            "name": market.name, "title": market.title, "slug": market.slug, "outcome": market.outcome,
            "condition_id": market.condition_id, "volume_usd": round(m.volume, 2), "positions": m.positions,
            "whales": len(whales), "whale_threshold_usd": threshold, "first_trade": _iso(m.first_ts), "close": _iso(m.close_ts),
            "reveal": _iso(m.reveal_ts), "reveal_method": m.reveal_method,
            "pre_reveal_prob": None if m.pre_reveal_prob is None else round(m.pre_reveal_prob, 4),
            "news": _iso(m.news_ts), "news_label": m.news_label,
        },
        "factors": catalog(),
        "whales": whales,
    }
