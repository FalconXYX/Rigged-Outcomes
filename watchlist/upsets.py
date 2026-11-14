"""
The closed list: each day's closed markets that are worth a person's look as
a possible insider case (someone knew the answer before the public and bet
on it). A light first filter, by the user's design (2026-11-12): market text,
how it resolved and the news, no prices, trades or wallets. The trade data
is the next stage, for the markets people pick (watchlist.deep).

For each UTC day, every market that closed that day:

1. Candidates. closedTime in the day, volumeNum >= $10k, one winner. Left
   out, because nobody knows them early or the answer builds up in public:
   games, price markets and weather (filters.exclusion, plus storms and
   earthquakes); elections and votes (the AI type election_vote and a
   keyword check); things counted or charted in public (views, tweets, box
   office, music and Netflix charts, case counts, ship transits, live
   streams); mention markets ("will X say ..."); embargoed releases
   (official statistics, central bank rate decisions, scheduled company
   results, scheduled share-offering prices), which a procedure keeps from
   traders and no documented case involves. A company's own year-end list
   (Google's Year in Search, Spotify Wrapped) is not a public count, and the
   words of a scripted speech or a produced video are not a mention market:
   both are settled before they come out, so they are checked. These word
   rules (watchlist.kinds.closed_exclusion, the vocabulary the "Open now"
   list shares, approved 2026-11-13) run before any model call. A deadline
   question whose answer was that nothing happened (No on "by / in / on /
   between <date>", Yes on "continues through <date>" or "will there be no
   ...") is left out too: there was no event to know about. A date ladder
   counts once (the shortest-dated market whose answer means it happened),
   and markets settled by the same news (sharing a name such as "Gemini
   Carbon") count once, under the one with the most money traded.
2. The check (watchlist.judge): Gemini writes a news query per market,
   Google News gives the headlines from three weeks before the close to two
   days after, and Gemini says what happened, when it became public, whether
   it was a surprise, who could have known first, whether there were public
   signs beforehand, and a verdict: potential case, weak, or not a case.
3. The day's list: potential cases, then weak ones, the bigger surprise
   first; up to 5, and at least 3 when the day has 3 checked markets (the
   ones that fill a short day are marked as such by their verdict). A pick
   whose market was ever on an "Open now" card carries the first day it was
   (on_open_list_from, from store.open_listed_since), and is checked and
   shown whatever its verdict: of the rules above, only games, prices,
   weather and the embargo still leave it out, so the open list's readers
   find its verdict here. Every pick carries the shared vocabulary's kind
   and group (watchlist.kinds.file_event) and, when it was on an open card,
   that card's "who would know first" sentence.

Writes web/public/data/closed.json (the Watchlist page's Recently closed
section), keeping the
last KEEP_DAYS days, after every day so a stalled run keeps what it did.
AI labels and checks are cached in data/watchlist/, so a re-run only asks
about markets it has not checked.

    python3 -m watchlist.upsets [--days 7 | --since 2026-10-30] [--out PATH]
"""
import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from watchlist import ai, filters, judge, kinds, net, store
from watchlist.common import DAY, iso, log, now_epoch, to_epoch
# The word rules both lists share live in watchlist.kinds (one vocabulary for the closed and the open
# list, 2026-11-13): the embargo, public counts, mention markets, nature, the election keyword,
# "continues through". candidates() applies them through kinds.closed_exclusion; the old names stay
# importable from here, unchanged.
from watchlist.kinds import CONTINUES, ELECTION, MENTION, NATURE, NEGATED, PUBLIC_COUNT, happened_side  # noqa: F401

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO, "web", "public", "data", "closed.json")

MIN_VOLUME = 10_000
PER_DAY = 5
AT_LEAST = 3
KEEP_DAYS = 14
EXCLUDED_TYPES = frozenset(ai.EXCLUDED_TYPES | {"election_vote"})
TYPE_REASONS = {"game_sport": "game or sport", "price_level": "price market", "weather": "weather",
                "election_vote": "election or vote"}
# A deadline question ("by Oct 31", "through Nov 8", "in October", "between Nov 2 and Nov 8", "on
# November 6", "this week") whose answer was that nothing happened.
_MONTH = (r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|"
          r"nov(ember)?|dec(ember)?)")
DEADLINE = re.compile(r"\b(by|before|through|until|in|on|between)\s+(the\s+)?(end\s+of\s+)?(" + _MONTH
                      + r"\b|q[1-4]\b|20\d\d\b)|\bthis (week|month)\b", re.I)
# Names that mark two of a day's markets as the same news (Gemini Carbon, Susie Wiles), not these.
_NAME = re.compile(r"\b([A-Z][\w.+-]*(?:\s+[A-Z0-9][\w.+-]*)+)")
COMMON_NAMES = frozenset({"donald trump", "white house", "united states", "us x", "x iran", "will the", "will donald"})


# ── pure parts (tested offline) ──────────────────────────────────────────────

def ladder_choice(markets):
    """(kept, folded): one closed event's markets. If any resolved the way
    that means the event happened (Yes; No on "continues through"), keep the
    shortest-dated of those (in a date ladder that is the real event; later
    dates resolved on the same news) and fold the rest under it; otherwise
    keep every market."""
    hit = [m for m in markets
           if (filters.winning_outcome(m) or (None, ""))[1].strip().lower() == happened_side(m.get("question"))]
    if not hit:
        return list(markets), []
    first = min(hit, key=lambda m: (to_epoch(m.get("endDate")) or 0, -filters.volume(m)))
    return [first], [m for m in markets if m is not first]


def nothing_happened(question, winner):
    """True when a deadline question resolved because nothing happened by the
    deadline: No on "will X happen by <date>", Yes on "X continues through
    <date>". There was no event anyone could know about in advance."""
    q, w = str(question or ""), str(winner or "").strip().lower()
    if not DEADLINE.search(q):
        return False
    return w != happened_side(q)


def names(text):
    """Proper names of two or more words in a question ("Gemini Carbon",
    "Susie Wiles"), lower-cased; the leading "Will" is not part of one."""
    out = set()
    for m in _NAME.finditer(str(text or "")):
        words = m.group(1).split()
        if words[0].lower() == "will":
            words = words[1:]
        for a, b in zip(words, words[1:]):
            pair = f"{a} {b}".lower()
            if pair not in COMMON_NAMES and not re.fullmatch(_MONTH + r" \d+,?", pair, re.I):
                out.add(pair)
    return out


def fold_same_news(markets):
    """Markets in priority order -> (kept, {kept conditionId: [folded
    markets]}). A market is folded into an earlier one of the same day when
    they share a name: one piece of news (Gemini Carbon released) can settle
    several separate events."""
    kept, folded = [], {}
    for m in markets:
        mine = names(m.get("question"))
        home = next((k for k in kept if mine & names(k.get("question"))), None)
        if home is None:
            kept.append(m)
        else:
            folded.setdefault(home["conditionId"], []).append(m)
    return kept, folded


def open_idents(markets):
    """The event ids, event slugs and market slugs of markets, the way the
    open list records its cards (store.open_listed): what the closed list
    matches to find when a market was first on an "Open now" card."""
    out = set()
    for m in markets:
        ev = filters.event_of(m)
        out.update(str(x) for x in (ev.get("id"), ev.get("slug"), m.get("slug")) if x not in (None, ""))
    return sorted(out)


def choose(picks, per_day=PER_DAY, at_least=AT_LEAST):
    """The day's list from checked picks: potential cases and weak ones in
    judge.sort_key order, up to per_day; topped up to at_least with the next
    ones when there are fewer. A pick whose market was on an "Open now" card
    (on_open_list_from) is always shown, whatever its verdict, so the open
    list's readers find the verdict here."""
    ranked = sorted(picks, key=judge.sort_key)
    shown = [p for p in ranked if p["verdict"] != "not a case"][:per_day]
    for p in ranked:
        if len(shown) >= at_least:
            break
        if p not in shown:
            shown.append(p)
    extra = [p for p in ranked if p.get("on_open_list_from") and p not in shown]
    return sorted(shown + extra, key=judge.sort_key) if extra else shown


# ── one day ──────────────────────────────────────────────────────────────────

def candidates(markets, conn):
    """(candidates [(market, label, folded markets)], dropped Counter) for
    one day's closed markets: resolved, not excluded, one per date ladder
    and per piece of news. A market that was on an "Open now" card is
    always checked unless it is a game, a price, weather or an embargoed
    release."""
    dropped = Counter()
    kept = []
    for m in markets:
        win = filters.winning_outcome(m)
        if not win:
            dropped["no single winner"] += 1
            continue
        ex = filters.exclusion(m)
        if ex:
            dropped[{"game": "game or sport", "price": "price market", "weather": "weather"}[ex[0]]] += 1
            continue
        # the shared word rules run before the AI labels, so they cost no AI calls: elections, embargoed
        # releases (kinds.EMBARGO_REASON), public counts, mention markets, nature
        why = kinds.closed_exclusion(m.get("question"), filters.event_of(m).get("title"))
        if why and (why in (kinds.EMBARGO_REASON, "weather") or not was_open_listed(conn, [m])):
            dropped[why] += 1
            continue
        kept.append(m)
    labels = {}
    if kept:
        try:
            labels = ai.label_markets(kept, conn=conn)
        except ai.GeminiError as e:
            log(f"  ! AI labels unavailable ({e}); the election keyword check still applies")
    chosen_all = []
    for g in filters.group_by_event(kept):
        chosen, folded = ladder_choice(g["markets"])
        for m in chosen:
            lab = labels.get(str(m.get("id")))
            t = (lab or {}).get("type")
            listed = was_open_listed(conn, [m] + folded)        # an open card's market gets its verdict
            # a dropped market takes the later dates folded under it along, under the same reason, so every
            # closed market is counted once
            if t == "election_vote" and not listed:
                dropped["election or vote"] += 1 + len(folded)
                continue
            if t in EXCLUDED_TYPES and t != "election_vote":
                dropped[TYPE_REASONS[t]] += 1 + len(folded)
                continue
            if not listed and nothing_happened(m.get("question"), filters.winning_outcome(m)[1]):
                dropped["nothing happened by the deadline"] += 1 + len(folded)
                continue
            chosen_all.append((m, lab, folded))
            if folded:
                dropped["later date of the same event"] += len(folded)
    chosen_all.sort(key=lambda x: -filters.volume(x[0]))
    kept_news, news_folded = fold_same_news([m for m, _, _ in chosen_all])
    keep_ids = {m["conditionId"] for m in kept_news}
    out = []
    for m, lab, folded in chosen_all:
        if m["conditionId"] in keep_ids:
            out.append((m, lab, folded + news_folded.get(m["conditionId"], [])))
        else:
            dropped["same news as another market"] += 1
    return out, dropped


def was_open_listed(conn, markets):
    """Whether any of these markets was ever on an "Open now" card."""
    return bool(conn is not None and store.open_listed_since(conn, open_idents(markets)))


def vocabulary(market, conn=None, folded=()):
    """The shared vocabulary's words for a closed market (watchlist.kinds):
    {"kind", "group"} from its event's title and its question (None when the
    open list's rules leave such a market out or cannot place it), plus
    "who_knows": the sentence its "Open now" card carried, when it was on
    one (a card a person approved, so the two lists say the same thing)."""
    ev = filters.event_of(market)
    f = kinds.file_event(ev.get("title") or market.get("question"), market.get("question"))
    kind = f.kind if f.how == "rule" else None
    out = {"kind": kind, "group": kinds.KINDS[kind].group if kind else None}
    card = store.open_listed_card(conn, open_idents([market] + list(folded))) if conn is not None else None
    if card and card.get("who_knows"):
        out["who_knows"] = card["who_knows"]
    return out


def pick(market, label, folded, check, open_since=None, words=None):
    """One shown market as closed.json has it. folded: the markets counted
    under it (later dates, same news), listed by question. open_since: the
    first day its market was on an "Open now" card, or None (the key is
    then left out). words: vocabulary() (kind, group, who_knows)."""
    ev = filters.event_of(market)
    out = {
        "condition_id": market["conditionId"],
        "question": market.get("question"),
        "event_title": ev.get("title") or market.get("question"),
        "url": f"https://polymarket.com/event/{ev.get('slug') or market.get('slug')}",
        "slug": market.get("slug"),                        # the market's own slug (markets.json takes it)
        "type": (label or {}).get("type"),
        "winner": filters.winning_outcome(market)[1],
        "closed": iso(to_epoch(market.get("closedTime"))),
        "verdict": check["verdict"],
        "what_happened": check["what_happened"],
        "public_at": check.get("public_at") or None,
        "surprise": check["surprise"],
        "who_knew_first": check["who_knew_first"],
        "public_signals": check["public_signals"],
        "reason": check["reason"],
        "headlines": sorted(check.get("headlines") or [], key=lambda h: h.get("published") or ""),   # all the check read
        "same_event": [x.get("question") for x in folded],
    }
    if open_since:
        out["on_open_list_from"] = open_since
    for k, v in (words or {}).items():
        if v is not None:
            out[k] = v
    return out


def run_day(day_ts, markets, conn, client=None):
    """The closed list of one UTC day from the markets that closed in it."""
    cands, dropped = candidates(markets, conn)
    checks = judge.check_markets([m for m, _, _ in cands], conn, client) if cands else {}
    picks = []
    for m, lab, folded in cands:
        c = checks.get(m["conditionId"])
        if c is None:
            dropped["could not be checked"] += 1
            continue
        # on an open card if this market or one counted under it was (same event, same news)
        picks.append(pick(m, lab, folded, c, store.open_listed_since(conn, open_idents([m] + folded)),
                          vocabulary(m, conn, folded)))
    shown = choose(picks)
    verdicts = Counter(p["verdict"] for p in picks)
    day = datetime.fromtimestamp(day_ts, timezone.utc).strftime("%Y-%m-%d")
    log(f"  {day}: {len(markets)} closed, {len(cands)} candidates, {len(picks)} checked, "
        f"{verdicts['potential case']} potential, {verdicts['weak']} weak, showing {len(shown)}")
    return {"day": day, "closed_markets": len(markets), "candidates": len(cands), "checked": len(picks),
            "verdicts": dict(verdicts), "dropped": dict(dropped.most_common()), "picks": shown}


def build(days=1, now=None, conn=None, out_path=OUT_PATH, keep_days=KEEP_DAYS, client=None):
    """Recompute the last `days` UTC days (today so far included) and merge
    them into out_path, keeping the last keep_days days."""
    now = now or now_epoch()
    conn = conn or store.default_conn()
    net.install()
    today = now - now % DAY
    since = today - (days - 1) * DAY
    t0 = time.time()
    closed = net.closed_markets(since, now, min_volume=MIN_VOLUME)
    log(f"closed list: {len(closed)} markets of ${MIN_VOLUME:,}+ closed since {iso(since)}")
    by_day = defaultdict(list)
    for m in closed:
        ts = to_epoch(m.get("closedTime"))
        by_day[ts - ts % DAY].append(m)
    old = {}
    if os.path.exists(out_path):
        with open(out_path) as f:
            old = {d["day"]: d for d in json.load(f).get("days", [])}
    doc = None
    for day_ts in range(today, since - 1, -DAY):     # written after every day, so a stalled run keeps the rest
        d = run_day(day_ts, by_day.get(day_ts, []), conn, client)
        old[d["day"]] = d
        doc = _write(old, now, today, out_path, keep_days)
    log(f"closed list -> {out_path} ({time.time() - t0:.0f} s)")
    return doc


def _write(days, now, today, out_path, keep_days):
    doc = {
        "generated_at": iso(now),
        "partial_day": datetime.fromtimestamp(today, timezone.utc).strftime("%Y-%m-%d"),
        "rules": {"min_volume": MIN_VOLUME, "per_day": PER_DAY, "at_least": AT_LEAST,
                  "news_days_before": judge.NEWS_BEFORE_DAYS, "prompt_version": judge.PROMPT_VERSION},
        "days": [d for d in sorted(days.values(), key=lambda d: d["day"], reverse=True)[:keep_days]
                 if "checked" in d],           # days from the old whale-based list are dropped
    }
    tmp = out_path + ".tmp"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp, out_path)
    return doc


def days_since(day, now):
    """How many UTC days from day (YYYY-MM-DD) to now's day, both included
    (build's `days`), or None when day is unreadable or after now."""
    first = to_epoch(f"{day}T00:00:00Z") if re.fullmatch(r"\d{4}-\d\d-\d\d", str(day or "")) else None
    if first is None or first > now:
        return None
    return int((now - now % DAY - first) // DAY) + 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="The closed list: closed markets worth a look as insider cases.")
    ap.add_argument("--days", type=int, default=1, help="UTC days to recompute, today so far included (default 1)")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="recompute every UTC day from this one to today instead (the file keeps them all)")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS, help=f"days kept in the file (default {KEEP_DAYS})")
    args = ap.parse_args(argv)
    now, days, keep_days = now_epoch(), args.days, args.keep_days
    if args.since:
        days = days_since(args.since, now)
        if days is None:
            ap.error(f"--since {args.since!r}: not a YYYY-MM-DD day up to today")
        keep_days = max(keep_days, days)
    build(days=days, now=now, out_path=args.out, keep_days=keep_days)


if __name__ == "__main__":
    main()
