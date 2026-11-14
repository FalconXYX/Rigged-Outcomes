"""
Pure rules over Gamma market/event dicts: which markets the watchlist
ignores, which market of an event gets analysed, and which open markets
make the pool. No network, no store; everything here is unit-tested offline.

Exclusions (exclusion()) remove what insider knowledge cannot explain:
  game     sports / esports matches: event or market tags sports, games,
           esports; an event with `sport` or a real `gameId` (Gamma sends
           gameId -1 on non-game events such as "Blue wave in 2026?", so -1
           and 0 do not count); a market with sportsMarketType; question
           patterns " vs ", "O/U", "Spread:", "win on 20..", "(BO3)".
  price    asset-price markets: tags crypto-prices, up-or-down, hit-price,
           finance-updown, price-milestone, price-comparison, hit-first,
           daily-close, fdv, 5M, 15M; patterns "Up or Down", "price of",
           "above $", "below $", "between $", "dip to", and an asset that
           hits/reaches a price ("Will STRC hit $100 by December 31?",
           "Will Gold (GC) hit (HIGH) $5,000 ...": PRICE_LEVEL) unless the
           amount is in M/B/T or the question names a size such as a
           valuation, debt, revenue or wage (PRICE_LEVEL_NOT).
  weather  "temperature", "°F"/"°C", "precipitation", "rainfall",
           "snowfall". By words only: Gamma puts the `weather` tag on
           pandemic markets ("Ebola pandemic in 2026?", "Hantavirus
           vaccine in 2026?"), so the tag would drop real candidates.
The keyword layer is deliberately conservative (a false exclusion is a
market lost for good, while a miss is caught by the AI type later): it
does not touch valuation ladders ("Will OpenAI's valuation hit $2T"),
which a funding round, i.e. a company decision, resolves.

The open pool (pool_reason/open_pool) keeps an open market when it has
volume >= $100k, ends within 60 days and its highest outcome price is
below 0.99: a market at 4-5 cents for weeks before a war is exactly what
the watchlist is for, and only >= 99 cents means "decided, arbitrage left".
Markets past their endDate that are still open (awaiting resolution) are
kept; the price rule drops the decided ones.
"""
import json
import re
from collections import Counter

from watchlist.common import DAY, now_epoch, to_epoch

MIN_VOLUME = 100_000
HORIZON_DAYS = 60
DECIDED_PRICE = 0.99

GAME_TAGS = {"sports", "games", "esports"}
PRICE_TAGS = {"crypto-prices", "up-or-down", "hit-price", "finance-updown", "price-milestone", "price-comparison",
              "hit-first", "daily-close", "fdv", "5m", "15m"}

GAME_PATTERNS = [re.compile(p, re.I) for p in (
    r"\svs\.?\s",            # "Wizards vs. Pistons", "your end vs Damajuana"
    r"\bO/U\b",              # "O/U 2.5"
    r"\bSpread:",            # "Spread: Pistons (-8.5)"
    r"\bwin on 20\d\d",      # "Will Arsenal win on 2026-11-08?"
    r"\(BO\d\)",             # "(BO3)"
)]
PRICE_PATTERNS = [re.compile(p, re.I) for p in (
    r"\bup or down\b",
    r"\bprice of\b",
    r"\babove \$",
    r"\bbelow \$",
    r"\bbetween \$",
    r"\bdip to\b",
)]
# An asset at a price level: "Will STRC hit $100 by December 31?", "Will
# Bitcoin reach $150k ...", "Will Gold (GC) hit (HIGH) $5,000 ...", "Will S&P
# 500 (SPX) hit $9,000 (HIGH) ...", "When will Ethereum hit $3k?". An amount
# in millions/billions/trillions is a size, not a price ("valuation hit
# (HIGH) $1.5T", "national debt hit $39 trillion", "revenue hit $1M", "open
# interest hit $1B"), and so is any amount in a question that names one of
# PRICE_LEVEL_NOT ("minimum wage reach $15", "jackpot hit $900"): those stay.
PRICE_LEVEL = re.compile(
    r"\b(?:hits?|reach(?:es)?)\s+(?:\((?:high|low)\)\s+)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?k\b)?"
    r"(?![\d,.]*\d|\s?(?:m|mm|mn|b|bn|t|tn|million|billion|trillion)\b)", re.I)
PRICE_LEVEL_NOT = re.compile(
    r"\b(?:valuation|net worth|market cap(?:italization)?|revenues?|sales|gross(?:es|ed)?|box office|debt|"
    r"deficit|budget|wages?|salary|salaries|funding|raises?|raised|fundrais\w*|donations?|jackpot|bounty|reward|"
    r"prize|settlement|bail|fines?|open interest|tvl|aum|earnings|eps|dividends?)\b", re.I)
PRICE_LEVEL_LABEL = "hit/reach $<price>"
WEATHER_PATTERNS = [re.compile(p, re.I) for p in (
    r"temperature",
    r"°\s?[FC]\b",
    r"\bprecipitation\b",
    r"\brainfall\b",
    r"\bsnowfall\b",
)]

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")


# ── field access ─────────────────────────────────────────────────────────────

def _json_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    try:
        out = json.loads(v)
    except (TypeError, ValueError):
        return []
    return out if isinstance(out, list) else []


def outcomes(m):
    """The market's outcome names (Gamma serves a JSON-encoded list)."""
    return [str(o) for o in _json_list(m.get("outcomes"))]


def outcome_prices(m):
    """The market's outcome prices as floats (unparseable entries dropped)."""
    out = []
    for p in _json_list(m.get("outcomePrices")):
        try:
            out.append(float(p))
        except (TypeError, ValueError):
            pass
    return out


def volume(m):
    """Lifetime volume in dollars (volumeNum, else volume)."""
    for k in ("volumeNum", "volume"):
        try:
            v = float(m.get(k))
        except (TypeError, ValueError):
            continue
        return v
    return 0.0


def event_of(m):
    """The market's Gamma event (events[0] on a /markets object), or {}."""
    evs = m.get("events")
    if isinstance(evs, list) and evs and isinstance(evs[0], dict):
        return evs[0]
    return {}


def tag_slugs(obj):
    """Lower-cased tag slugs of a market or event (tags as dicts or strings)."""
    out = set()
    for t in (obj or {}).get("tags") or []:
        s = t.get("slug") if isinstance(t, dict) else t
        if s:
            out.add(str(s).strip().lower())
    return out


def _real_game_id(v):
    return v not in (None, "", -1, "-1", 0, "0", False)


# ── exclusions ───────────────────────────────────────────────────────────────

def _text(m, event):
    return f"{m.get('question') or ''} | {event.get('title') or ''}"


def price_level(text):
    """True when a question (or title) asks whether an asset hits/reaches a
    price: PRICE_LEVEL matches and the same part of the text (question or
    title, split on " | ") names none of PRICE_LEVEL_NOT."""
    for part in str(text or "").split(" | "):
        if PRICE_LEVEL.search(part) and not PRICE_LEVEL_NOT.search(part):
            return True
    return False


def exclusion(m, event=None):
    """(category, detail) when the market is a game, a price market or
    weather; None when it stays. `event` defaults to the market's events[0];
    pass the parent event for markets nested in an /events object."""
    ev = event if event is not None else event_of(m)
    tags = tag_slugs(m) | tag_slugs(ev)
    text = _text(m, ev)

    hit = sorted(tags & GAME_TAGS)
    if hit:
        return "game", f"tag {hit[0]}"
    if ev.get("sport"):
        return "game", "event has a sport"
    if _real_game_id(ev.get("gameId")) or _real_game_id(m.get("gameId")):
        return "game", "event has a gameId"
    if m.get("sportsMarketType"):
        return "game", f"sportsMarketType {m.get('sportsMarketType')}"
    for p in GAME_PATTERNS:
        if p.search(text):
            return "game", f"pattern {p.pattern}"

    hit = sorted(tags & PRICE_TAGS)
    if hit:
        return "price", f"tag {hit[0]}"
    for p in PRICE_PATTERNS:
        if p.search(text):
            return "price", f"pattern {p.pattern}"
    if price_level(text):
        return "price", f"pattern {PRICE_LEVEL_LABEL}"

    for p in WEATHER_PATTERNS:
        if p.search(text):
            return "weather", f"pattern {p.pattern}"
    return None


# ── resolution ───────────────────────────────────────────────────────────────

def winning_outcome(m):
    """(index, name) of the outcome a RESOLVED market paid out on, else None.

    Resolved = umaResolutionStatus "resolved" and exactly one outcome price
    of 1 (Gamma serves "1"/"0"; polymarket_trades.get_win_status reads the
    same field). A 50-50 resolution (["0.5","0.5"]) has no winner."""
    if str(m.get("umaResolutionStatus") or "").strip().lower() != "resolved":
        return None
    names, raw = outcomes(m), _json_list(m.get("outcomePrices"))
    ones = []
    for i, p in enumerate(raw):
        try:
            if float(p) == 1.0:
                ones.append(i)
        except (TypeError, ValueError):
            return None
    if len(ones) != 1 or ones[0] >= len(names):
        return None
    return ones[0], names[ones[0]]


# ── events ───────────────────────────────────────────────────────────────────

def group_by_event(markets):
    """[{event_id, event, markets}] in first-seen order. A market without an
    event becomes its own group keyed "m:<market id>"."""
    groups, order = {}, []
    for m in markets:
        ev = event_of(m)
        eid = str(ev.get("id") or "") or f"m:{m.get('id') or m.get('conditionId')}"
        if eid not in groups:
            groups[eid] = {"event_id": eid, "event": ev, "markets": []}
            order.append(eid)
        groups[eid]["markets"].append(m)
    return [groups[e] for e in order]


def choose_main_market(markets):
    """(main, others, rule) for one closed event's markets in the window.

    negRisk or multi-market event: the market that resolved Yes (the
    highest-volume one when a date ladder resolved several Yes); with no Yes
    among them, or for a single market, the highest-volume market.
    `others` are the rest, highest volume first."""
    if not markets:
        raise ValueError("choose_main_market needs at least one market")
    by_vol = sorted(markets, key=lambda m: -volume(m))
    if len(markets) > 1 or any(m.get("negRisk") for m in markets):
        for m in by_vol:
            w = winning_outcome(m)
            if w and w[1].strip().lower() == "yes":
                return m, [x for x in by_vol if x is not m], "resolved_yes"
    return by_vol[0], by_vol[1:], "highest_volume"


def title_key(title):
    """An event title with dates, numbers and blanks stripped, so recurring
    events ("... ceasefire continues through November 6?") share a key."""
    s = str(title or "").lower()
    s = re.sub(rf"\b({_MONTHS})\b\.?", " ", s)
    s = re.sub(r"\d+(?:[.,:/]\d+)*(?:st|nd|rd|th|k|m|b|t|bn|%)?\b", " ", s)
    s = re.sub(r"[_?!,:;()\[\]\"'’$#…+*/]|\.{2,}", " ", s)
    words = [w.strip("-–—.") for w in s.split()]
    s = " ".join(w for w in words if w)
    s = re.sub(r"(?:\s+(?:by|before|after|in|on|of|the|through|from|to|until|and|at))+$", "", s)
    return s.strip()


def series_key(event):
    """Track-record series of an event: Gamma's seriesSlug (or series[0].slug),
    else "title:" + title_key(title)."""
    ev = event or {}
    slug = ev.get("seriesSlug")
    if not slug and isinstance(ev.get("series"), list) and ev["series"]:
        first = ev["series"][0]
        slug = first.get("slug") if isinstance(first, dict) else None
    if slug:
        return str(slug)
    t = title_key(ev.get("title"))
    return f"title:{t}" if t else None


# ── open pool ────────────────────────────────────────────────────────────────

def pool_reason(m, event=None, now=None, min_volume=MIN_VOLUME, horizon_days=HORIZON_DAYS,
                max_price=DECIDED_PRICE):
    """Why an open-event market is NOT in the pool, or None when it is.
    Reasons: closed, inactive, volume, no_end_date, ends_later, no_prices,
    decided, game, price, weather."""
    now = now_epoch() if now is None else int(now)
    if m.get("closed"):
        return "closed"
    if m.get("active") is False or m.get("archived"):
        return "inactive"
    if volume(m) < min_volume:
        return "volume"
    end = to_epoch(m.get("endDate"))
    if end is None:
        return "no_end_date"
    if end > now + horizon_days * DAY:
        return "ends_later"
    prices = outcome_prices(m)
    if not prices:
        return "no_prices"
    if max(prices) >= max_price:
        return "decided"
    ex = exclusion(m, event)
    if ex:
        return ex[0]
    return None


def open_pool(events, now=None, min_volume=MIN_VOLUME, horizon_days=HORIZON_DAYS, max_price=DECIDED_PRICE):
    """(groups, counts) for open Gamma events (net.walk_open_events output).
    groups = [{event_id, event, markets}] with only the kept markets (highest
    volume first) and only events that kept one; counts = Counter of
    pool_reason over every market seen ("kept" for the kept ones)."""
    now = now_epoch() if now is None else int(now)
    groups, counts = [], Counter()
    for e in events:
        kept = []
        for m in e.get("markets") or []:
            r = pool_reason(m, e, now, min_volume, horizon_days, max_price)
            counts[r or "kept"] += 1
            if r is None:
                kept.append(m)
        if kept:
            groups.append({"event_id": str(e.get("id")), "event": e,
                           "markets": sorted(kept, key=lambda m: -volume(m))})
    return groups, counts
