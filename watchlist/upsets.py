"""
The closed list: each day's closed markets that are worth a person's look as
a possible insider case (someone knew the answer before the public and bet
on it). A light first filter, by the user's design (2026-11-12): market text,
how it resolved and the news, no prices, trades or wallets. The trade data
is the next stage, for the markets people add to the main page (add_market).

For each Toronto day (America/Toronto, like every day the backend keeps),
every market that closed that day:

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
3. The day's list: exactly 3 picks when the day has 3 checked markets,
   never more (the user, 2026-11-19). First a market sent to the main page
   from the day's list, then a watched one, then potential cases and weak
   ones, the bigger surprise first; a short day is topped up to 3 with the
   next ones (marked as such by their verdict). A pick whose market was
   ever on an "Open now" card carries the first day it was
   (on_open_list_from, from store.open_listed_since), and is checked
   whatever its verdict: of the rules above, only games, prices, weather and
   the embargo still leave it out. Every pick carries the shared vocabulary's kind
   and group (watchlist.kinds.file_event) and, when it was on an open card,
   that card's "who would know first" sentence. A market on the site's
   watchlist is one on an open card (only those can be watched), so it is
   always checked and shown too; with --watched, its pick is marked
   ("watched": true, left out otherwise), as the watchlist stood when the
   day was last recomputed.
4. What a count keeps of the day's last version: only a check that went
   through changes or removes a pick. A pick whose market this count could
   not check (the model or the news did not answer, the time ran out, the
   market it is now counted under could not be checked, Polymarket's walk
   left it out) stays as it was, verdict, words and all; and a market sent
   to the main page (the job's work.json, --watched: approved or added)
   never leaves its day's list, so its Recently closed card stays.

The daily run (watchlist.steps) counts the days in steps and publishes the
list to the site's data (site:closed.json); the CLI writes closed.json
locally, keeping the last KEEP_DAYS days, after every day so a stalled run
keeps what it did. Each day says when it was counted (counted_at, the run's
time, with Toronto's offset): a day counted before it was over is a partial
count, and the site says where it stops ("by 04:17"). A run also counts
again every kept day still partial,
oldest first, so a day whose next run failed is finished by the one after,
and every day of the last CATCH_UP_DAYS with markets that could not be
checked, or that no run counted (the checks already made are cached, so
only those markets cost requests).
AI labels and checks are cached in data/watchlist/, so a re-run only asks
about markets it has not checked.

When Polymarket does not answer, the walk is asked again for about 7.5
minutes (net.patiently); then the run stops and closed.json stays as it was.
What went wrong is recorded for the run's report (watchlist.problems).

    python3 -m watchlist.upsets [--days 7 | --since 2026-10-30] [--out PATH] [--watched work.json]
"""
import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict

from backend.contract import LISTS
from watchlist import ai, filters, judge, kinds, net, problems, store
from watchlist.common import (day_shift, days_from, iso, local_bounds, local_day, local_iso, log, now_epoch,
                              to_epoch)
# The word rules both lists share live in watchlist.kinds (one vocabulary for the closed and the open
# list, 2026-11-13): the embargo, public counts, mention markets, nature, the election keyword,
# "continues through". candidates() applies them through kinds.closed_exclusion; the old names stay
# importable from here, unchanged.
from watchlist.kinds import CONTINUES, ELECTION, MENTION, NATURE, NEGATED, PUBLIC_COUNT, happened_side  # noqa: F401

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO, "web", "public", "data", "closed.json")

MIN_VOLUME = 10_000
PER_DAY = LISTS["closed_per_day"]          # 3 a day, never more
AT_LEAST = LISTS["closed_at_least"]        # topped up to 3
KEEP_DAYS = 14
CATCH_UP_DAYS = 7         # a day of the last week with markets not checked, or never counted, is counted again
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


def _first(p, requested):
    """0 for a pick of a market sent to the main page, 1 for a watched one, 2 for the rest."""
    return 0 if str(p.get("condition_id") or "").lower() in requested else 1 if p.get("watched") else 2


def choose(picks, per_day=PER_DAY, at_least=AT_LEAST, requested=frozenset()):
    """The day's list from checked picks, never more than per_day: first the picks of markets sent to the main
    page (requested: condition ids, lower case), then watched ones, then potential cases and weak ones in
    judge.sort_key order; topped up to at_least with the next ones when there are fewer."""
    ranked = sorted(picks, key=lambda p: (_first(p, requested), _rank(p)))
    shown = [p for p in ranked if p.get("verdict") != "not a case" or _first(p, requested) < 2][:per_day]
    for p in ranked:
        if len(shown) >= min(at_least, per_day):
            break
        if p not in shown:
            shown.append(p)
    return sorted(shown, key=lambda p: (_first(p, requested), _rank(p)))


def kept_picks(before, picks, checked, requested=frozenset()):
    """The picks of the day's last version (before: the day as the list had it) that stay in the running
    although this count did not make them: one whose market this count did not check (checked: the condition
    ids of the markets it checked, each candidate with the ones counted under it) stays as it was; one of a
    market sent to the main page (requested: condition ids, approved or added) stays whatever the count found,
    as its new pick when this count made one (picks: every pick it made), else as it was. choose() then takes
    the day's 3 from these and the new picks."""
    have = {str(p["condition_id"]).lower() for p in picks}
    out = []
    for p in (before or {}).get("picks") or []:
        cid = str(p.get("condition_id") or "").lower()
        if not cid or cid in have:
            continue
        if cid in requested or cid not in checked:
            out.append(p)
        have.add(cid)
    return out


def _rank(p):
    """judge.sort_key, with a pick from an older vocabulary last."""
    try:
        return judge.sort_key(p)
    except (KeyError, ValueError):
        return (len(judge.VERDICTS),)


# ── one day ──────────────────────────────────────────────────────────────────

def word_rules(markets, conn):
    """(kept markets, dropped Counter): one day's closed markets through the rules that cost no model request:
    resolved, not a game, a price or weather, then the shared word rules (a market that was on an "Open now"
    card is kept unless it is weather or an embargoed release)."""
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
    return kept, dropped


def ask_labels(kept, conn, budget=None):
    """The AI labels of the kept markets (ai.label_markets: cached ones, the rest asked); {} when the model is
    unavailable."""
    if not kept:
        return {}
    try:
        labels = ai.label_markets(kept, conn=conn, budget=budget)
    except ai.GeminiError as e:
        problems.note(f"the closed list's AI labels were unavailable ({e}); the election keyword check still "
                      f"applied", problems.classify(e))
        return {}
    # label_markets leaves out the markets it could not label (the model busy, or the time spent): they go on to
    # the check unlabelled, and are labelled at the next run
    missing = len({str(m.get("id")) for m in kept} - set(labels))
    if missing:
        problems.note(f"{missing} market(s) of the closed list got no AI label (the model did not answer, or the "
                      f"time ran out); the election keyword check still applied")
    return labels


def cached_labels(kept, conn, today=None):
    """{market id: label} of the kept markets the store has a label for (no request)."""
    out = {}
    for m in kept:
        it = ai._item(m)
        hit = ai._from_cache(conn, it, today)
        if hit:
            out[it["id"]] = hit
    return out


def after_labels(kept, labels, conn, dropped):
    """(candidates [(market, label, folded markets)], dropped): the kept markets (word_rules) with their AI
    labels: the excluded types left out, one per date ladder and per piece of news, the most traded first."""
    dropped = Counter(dropped)
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


def candidates(markets, conn, budget=None, labels=None):
    """(candidates [(market, label, folded markets)], dropped Counter) for
    one day's closed markets: resolved, not excluded, one per date ladder
    and per piece of news. A market that was on an "Open now" card is
    always checked unless it is a game, a price, weather or an embargoed
    release. budget: the run's time (no label request starts once spent).
    labels: {market id: label} already made (none: ask_labels)."""
    kept, dropped = word_rules(markets, conn)
    labels = ask_labels(kept, conn, budget) if labels is None else labels
    return after_labels(kept, labels, conn, dropped)


def was_open_listed(conn, markets):
    """Whether any of these markets was ever on an "Open now" card."""
    return bool(conn is not None and store.open_listed_since(conn, open_idents(markets)))


def vocabulary(market, conn=None, folded=()):
    """The shared vocabulary's words for a closed market (watchlist.kinds):
    {"kind", "group"} from its event's title and its question (None when the
    open list's rules leave such a market out or cannot place it), plus
    "who_knows": the sentence its "Open now" card carried, when it was on
    one (so the two lists say the same thing)."""
    ev = filters.event_of(market)
    f = kinds.file_event(ev.get("title") or market.get("question"), market.get("question"))
    kind = f.kind if f.how == "rule" else None
    out = {"kind": kind, "group": kinds.KINDS[kind].group if kind else None}
    card = store.open_listed_card(conn, open_idents([market] + list(folded))) if conn is not None else None
    if card and card.get("who_knows"):
        out["who_knows"] = card["who_knows"]
    return out


def pick(market, label, folded, check, open_since=None, words=None, watched=False):
    """One shown market as closed.json has it. folded: the markets counted
    under it (later dates, same news), listed by question. open_since: the
    first day its market was on an "Open now" card, or None (the key is
    then left out). words: vocabulary() (kind, group, who_knows).
    watched: its open card is on the site's watchlist (left out when not)."""
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
    if watched:
        out["watched"] = True
    return out


def on_watchlist(conn, idents, watched):
    """Whether the "Open now" card these markets were last on is on the
    site's watchlist (watched: the card ids on it, from the job's
    work.json)."""
    if not watched or conn is None:
        return False
    card = store.open_listed_card(conn, idents)
    return bool(card) and card["id"] in watched


def load_watched(path):
    """The open card ids on the site's watchlist, from the job's work.json ({"watched": [{"story"}]}); an
    empty set when the file is missing or unreadable (the API was not configured or did not answer)."""
    try:
        with open(path) as f:
            rows = json.load(f).get("watched") or []
    except (OSError, ValueError, AttributeError) as e:
        log(f"  ! no watchlist ({path}: {type(e).__name__}); no pick is marked watched")
        return set()
    return {str(r["story"]) for r in rows if isinstance(r, dict) and r.get("story")}


def load_requested(path):
    """The condition ids of the markets sent to the main page, from the job's work.json: the ones waiting to be
    added ("requests", approved) and the ones added ("added"); an empty set when the file is missing or
    unreadable."""
    try:
        with open(path) as f:
            work = json.load(f)
        rows = list(work.get("requests") or []) + list(work.get("added") or [])
    except (OSError, ValueError, AttributeError, TypeError) as e:
        log(f"  ! no list of the markets sent to the main page ({path}: {type(e).__name__}); only the checks decide "
            f"which picks stay")
        return frozenset()
    return frozenset(str(r["condition_id"]).lower() for r in rows if isinstance(r, dict) and r.get("condition_id"))


def run_day(day, markets, conn, client=None, watched=None, budget=None, before=None, requested=frozenset(),
            labels=None, checks=None, scanned=None):
    """The closed list of one Toronto day ("YYYY-MM-DD", or epoch seconds in it) from the markets that closed in
    it. watched: the open card ids on the site's watchlist (load_watched). budget: the run's time (a
    common.Budget, or None). before: the day as the list had it, and requested: the markets sent to the main
    page (load_requested), for the picks that stay (kept_picks). labels / checks: {market id: label} and
    {condition id: check} already made (the daily run's steps: no model request or news search here); none:
    asked here. scanned: {"markets", "dropped", "closed_markets"}, the day's word_rules already applied."""
    day = day if isinstance(day, str) else local_day(day)
    if scanned is None:
        kept, dropped = word_rules(markets, conn)
        n_closed = len(markets)
    else:
        kept, dropped, n_closed = scanned["markets"], Counter(scanned.get("dropped") or {}), \
            int(scanned.get("closed_markets") or 0)
    labels = ask_labels(kept, conn, budget) if labels is None else labels
    cands, dropped = after_labels(kept, labels, conn, dropped)
    if checks is None:
        within = {"budget": budget} if budget is not None else {}
        checks = judge.check_markets([m for m, _, _ in cands], conn, client, **within) if cands else {}
    picks, checked = [], set()
    for m, lab, folded in cands:
        c = checks.get(m["conditionId"])
        if c is None:
            dropped["could not be checked"] += 1
            continue
        checked.update(str(x["conditionId"]).lower() for x in [m] + folded)
        # on an open card if this market or one counted under it was (same event, same news)
        idents = open_idents([m] + folded)
        picks.append(pick(m, lab, folded, c, store.open_listed_since(conn, idents), vocabulary(m, conn, folded),
                          on_watchlist(conn, idents, watched)))
    kept_ = kept_picks(before, picks, checked, requested)
    shown = choose(picks + kept_, requested=requested)
    verdicts = Counter(p["verdict"] for p in picks)
    stay = sum(1 for p in shown if p in kept_)
    log(f"  {day}: {n_closed} closed, {len(cands)} candidates, {len(picks)} checked, "
        f"{verdicts['potential case']} potential, {verdicts['weak']} weak, showing {len(shown)}"
        + (f", {stay} of them kept from its last count" if stay else ""))
    return {"day": day, "closed_markets": n_closed, "candidates": len(cands), "checked": len(picks),
            "verdicts": dict(verdicts), "dropped": dict(dropped.most_common()), "picks": shown}


def _is_day(d):
    return bool(re.fullmatch(r"\d{4}-\d\d-\d\d", str(d or "")))


def partial(day, generated_at=None):
    """True when a day of the list was counted before it was over: its counted_at (or, in a list from before
    days carried one, the list's generated_at) falls before the Toronto day's end."""
    counted = to_epoch(day.get("counted_at") or generated_at)
    if counted is None or not _is_day(day.get("day")):
        return False
    return counted < local_bounds(day["day"])[1]


def unchecked(day):
    """How many of a day's markets could not be checked (the model or the news did not answer, or the time ran
    out): the day is counted again, within CATCH_UP_DAYS."""
    return int((day.get("dropped") or {}).get("could not be checked") or 0)


def unfinished_since(old, generated_at, today, keep_days):
    """The oldest day before today ("YYYY-MM-DD", Toronto) to count again, or None: a kept day still a partial
    count (its next run failed or stalled); and, within the last CATCH_UP_DAYS, a day with markets that could not
    be checked, or a day no run counted after the oldest kept one."""
    oldest, recent = day_shift(today, -keep_days), day_shift(today, -CATCH_UP_DAYS)
    kept = {d: v for d, v in old.items() if _is_day(d)}
    days = [d for d, v in kept.items() if partial(v, generated_at) and oldest < d < today]
    days += [d for d, v in kept.items() if recent <= d < today and unchecked(v)]
    if kept:
        days += [d for d in days_from(max(min(kept), recent), day_shift(today, -1)) if d not in kept]
    return min(days) if days else None


def counting_days(prev, now, days=1, keep_days=KEEP_DAYS):
    """The Toronto days a count recomputes, oldest first: the last `days` (today so far included), from an
    older kept day still unfinished (unfinished_since) when there is one. prev: the list as published."""
    today = local_day(now)
    first = day_shift(today, -(days - 1))
    old = {d["day"]: d for d in (prev or {}).get("days") or [] if isinstance(d, dict)}
    behind = unfinished_since(old, (prev or {}).get("generated_at"), today, keep_days)
    if behind is not None and behind < first:
        log(f"closed list: {behind} was counted before it was over, missed markets or was missed; counting again "
            f"from it")
        first = behind
    return days_from(first, today)


def by_day(closed):
    """{Toronto day: [markets]} of closed markets, by their closedTime."""
    out = defaultdict(list)
    for m in closed:
        ts = to_epoch(m.get("closedTime"))
        if ts is not None:
            out[local_day(ts)].append(m)
    return out


def build(days=1, now=None, conn=None, out_path=OUT_PATH, keep_days=KEEP_DAYS, client=None, watched=None,
          budget=None, sleep=time.sleep, requested=frozenset()):
    """Recompute the last `days` Toronto days (today so far included), and any older kept day still counted
    only in part, and merge them into out_path, keeping the last keep_days days (the CLI; the daily run counts
    the days in steps, watchlist.steps). watched: the open card ids on the site's watchlist, for the
    recomputed days' picks. requested: the markets sent to the main page (load_requested), whose picks stay.
    Raises PolymarketAPIError when Polymarket does not answer the walk (out_path is then left as it was)."""
    now = now or now_epoch()
    conn = conn or store.default_conn()
    net.install()
    prev = None
    if os.path.exists(out_path):
        with open(out_path) as f:
            prev = json.load(f)
    old = {d["day"]: d for d in (prev or {}).get("days", [])}
    todo = counting_days(prev, now, days, keep_days)
    since = local_bounds(todo[0])[0]
    t0 = time.time()
    closed = net.patiently(lambda: net.closed_markets(since, now, min_volume=MIN_VOLUME), "closed list", sleep=sleep)
    log(f"closed list: {len(closed)} markets of ${MIN_VOLUME:,}+ closed since {local_iso(since)}")
    grouped = by_day(closed)
    doc, missed = None, 0
    # oldest first, written after every day: a stalled run keeps the rest, and an older partial day is finished
    # before today's partial one is counted
    for day in todo:
        d = run_day(day, grouped.get(day, []), conn, client, watched, before=old.get(day), requested=requested,
                    **({"budget": budget} if budget is not None else {}))
        missed += unchecked(d)
        d["counted_at"] = local_iso(now)
        old[d["day"]] = d
        doc = make_doc(old, now, keep_days)
        _write(doc, out_path)
    if missed:
        problems.note(f"the closed list could not check {missed} market(s) (the model or Google News did not "
                      f"answer, or its time ran out); their days keep the picks they had")
    log(f"closed list -> {out_path} ({time.time() - t0:.0f} s)")
    return doc


def make_doc(days, now, keep_days=KEEP_DAYS):
    """The published list (closed.json) from {day: day doc}, newest day first, the last keep_days days, each with
    at most PER_DAY picks (a day an older count published with more keeps its first ones, in choose() order)."""
    return {
        "generated_at": local_iso(now),
        "partial_day": local_day(now),
        "rules": {"min_volume": MIN_VOLUME, "per_day": PER_DAY, "at_least": AT_LEAST,
                  "news_days_before": judge.NEWS_BEFORE_DAYS, "prompt_version": judge.PROMPT_VERSION},
        "days": [dict(d, picks=d["picks"][:PER_DAY]) if len(d.get("picks") or []) > PER_DAY else d
                 for d in sorted(days.values(), key=lambda d: d["day"], reverse=True)[:keep_days]
                 if "checked" in d],           # days from the old whale-based list are dropped
    }


def dumps(doc):
    """The list's bytes, as published."""
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


def _write(doc, out_path):
    tmp = out_path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(dumps(doc))
    os.replace(tmp, out_path)
    return doc


def days_since(day, now):
    """How many Toronto days from day (YYYY-MM-DD) to now's day, both included (build's `days`), or None when day
    is unreadable or after now."""
    if not _is_day(day) or day > local_day(now):
        return None
    return len(days_from(day, local_day(now)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="The closed list: closed markets worth a look as insider cases.")
    ap.add_argument("--days", type=int, default=1, help="Toronto days to recompute, today so far included (default 1)")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="recompute every Toronto day from this one to today instead (the file keeps them all)")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS, help=f"days kept in the file (default {KEEP_DAYS})")
    ap.add_argument("--watched", metavar="PATH", default=None,
                    help="the daily job's work.json: picks whose open card is on the site's watchlist are marked, "
                         "and the picks of markets sent to the main page stay")
    args = ap.parse_args(argv)
    now, days, keep_days = now_epoch(), args.days, args.keep_days
    if args.since:
        days = days_since(args.since, now)
        if days is None:
            ap.error(f"--since {args.since!r}: not a YYYY-MM-DD day up to today")
        keep_days = max(keep_days, days)
    build(days=days, now=now, out_path=args.out, keep_days=keep_days,
          watched=load_watched(args.watched) if args.watched else None,
          requested=load_requested(args.watched) if args.watched else frozenset())


if __name__ == "__main__":
    raise SystemExit(problems.guard("closed", main))
