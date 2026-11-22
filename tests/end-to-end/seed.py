"""
The harness's seed lists, made from live data in a stack's workspace (tests/end-to-end/stack.mjs runs it, then
saves what it wrote to the stack's database the way the job does: `python3 -m watchlist.state push --seed` and
`python3 -m watchlist.sync catalog`, with the stack's WATCHLIST_API_URL and WATCHLIST_JOB_TOKEN).

  closed.json  real Polymarket markets that closed in the last --days Toronto days (net.closed_markets, the
               closed list's own walk), past the closed list's word rules (filters.exclusion,
               kinds.closed_exclusion) with one winner, not on the main page, one per event, with --min to
               --max dollars traded (small, so add_market builds one in seconds; --min is the closed list's own
               floor by default, so a daily run in the stack counts the same markets again and keeps the
               picks, their checks being saved); --picks of them, each made
               with upsets.pick from a canned check (the model's verdict is the only stand-in; the headlines
               are Google News's, as the check reads them), on their own close days. Each check is also saved
               in the workspace's store (store.save_closed_check, as the check does), so the state the stack
               saves holds what a real one holds (a pull refuses an empty state)
  open.json    --cards open Polymarket events the shared vocabulary files under a kind (kinds.file_event), each
               an "Open now" card with its Google News headlines; with --open-days-ago N, on the list of N days
               ago, also saved in the store (store.save_open_day), as an earlier daily run left it

Writes both into --out (the workspace's web/public/data) and prints one JSON line last: {"picks": [...],
"cards": [...]}. Gemini is never called (the cards and verdicts are canned); E2E_STUB=1 stubs it anyway.

    python3 tests/end-to-end/seed.py --root WORKSPACE --out WORKSPACE/web/public/data [--picks 3] [--cards 2]
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict


def main(argv=None):
    ap = argparse.ArgumentParser(description="The harness's seed lists, from live Polymarket and Google News data.")
    ap.add_argument("--root", required=True, help="the workspace whose watchlist package is used")
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--picks", type=int, default=3)
    ap.add_argument("--cards", type=int, default=2)
    ap.add_argument("--min", type=float, default=None, help="least dollars traded for a pick (default: the closed "
                                                             "list's own floor, upsets.MIN_VOLUME)")
    ap.add_argument("--max", type=float, default=60_000, help="most dollars traded for a pick (a quick add)")
    ap.add_argument("--big", type=int, default=0, help="more picks of $150,000 to $800,000 traded (an add that "
                                                        "takes a while: whale batches)")
    ap.add_argument("--crowd", type=int, default=0, help="that many picks on one day, all published (a day an "
                                                          "older count listed with more than 3)")
    ap.add_argument("--open-days-ago", type=int, default=0, help="the Open now list that many days ago, also saved "
                                                                  "in the store, as an earlier daily run left it "
                                                                  "(default 0: today's, in open.json only)")
    ap.add_argument("--no-news", action="store_true", help="no Google News headlines")
    args = ap.parse_args(argv)
    if args.open_days_ago < 0:
        ap.error("--open-days-ago must be 0 or more")

    sys.path.insert(0, args.root)
    os.chdir(args.root)
    from watchlist import filters, judge, kinds, net, news, opennow, store, upsets
    from watchlist.common import DAY, day_shift, local_bounds, local_day, local_iso, now_epoch, to_epoch

    net.install()
    if args.min is None:
        args.min = upsets.MIN_VOLUME
    now = now_epoch()
    today = local_day(now)
    with open(os.path.join(args.root, "markets.json"), encoding="utf-8") as f:
        on_main = {str(m.get("slug") or "").split("#")[0].split("/")[-1] for m in json.load(f)}

    def headlines(query, before=None):
        if args.no_news:
            return []
        try:
            got = news.search(query, after=(before or now) - 21 * DAY, before=(before or now) + 2 * DAY, limit=8)
        except news.NewsError:
            return []
        return [{"title": h.get("title"), "source": h.get("source") or "", "published": h.get("published"),
                 "url": h.get("url") or ""} for h in got if h.get("title") and h.get("url")]

    # ── closed picks ──
    t0 = time.time()
    closed = net.closed_markets(local_bounds(day_shift(today, -(args.days - 1)))[0], now, min_volume=args.min)
    seen_events, fit = set(), []
    for m in closed:
        win = filters.winning_outcome(m)
        vol = filters.volume(m)
        ev = filters.event_of(m)
        if not win or vol > max(args.max, 800_000 if args.big else 0) or not m.get("slug") or m.get("slug") in on_main:
            continue
        if filters.exclusion(m) or kinds.closed_exclusion(m.get("question"), ev.get("title")):
            continue
        if upsets.nothing_happened(m.get("question"), win[1]):
            continue
        key = str(ev.get("id") or m.get("id"))
        if key in seen_events:
            continue
        seen_events.add(key)
        fit.append(m)
    fit.sort(key=lambda m: filters.volume(m))
    day_of = lambda m: local_day(to_epoch(m.get("closedTime")))

    def take(pool, n, per_day=upsets.PER_DAY, taken=()):
        """n markets of pool, at most per_day on one day (each day's list shows 3)."""
        out, count = [], defaultdict(int)
        for m in taken:
            count[day_of(m)] += 1
        for m in pool:
            if len(out) >= n:
                break
            if count[day_of(m)] < per_day and m not in taken:
                out.append(m)
                count[day_of(m)] += 1
        return out

    small = [m for m in fit if filters.volume(m) <= args.max]
    if args.crowd:
        by = defaultdict(list)
        for m in small:
            by[day_of(m)].append(m)
        crowd_day = max(by, key=lambda d: (len(by[d]), d)) if by else None
        chosen = by.get(crowd_day, [])[:args.crowd]
        chosen += take(small, args.picks, taken=chosen)
    else:
        chosen = take(small, args.picks)
    big = take([m for m in fit if 150_000 <= filters.volume(m) <= 800_000], args.big, taken=chosen)
    chosen += big
    if not chosen:
        print(f"no closed market between ${args.min:,.0f} and ${args.max:,.0f} in the last {args.days} day(s)",
              file=sys.stderr)
        return 2
    by_day = defaultdict(list)
    for m in chosen:
        win = filters.winning_outcome(m)[1]
        closed_at = to_epoch(m.get("closedTime"))
        heads = headlines(m.get("question"), closed_at)
        check = {
            "verdict": "weak",
            "what_happened": f"{m.get('question')} It resolved {win}.",
            "public_at": None,
            "surprise": "somewhat",
            "who_knew_first": "the people deciding it",
            "public_signals": "unknown",
            "reason": "A small group decided the outcome, and the news does not show when it became public.",
            "headlines": heads,
        }
        store.save_closed_check(store.default_conn(), m["conditionId"], judge.PROMPT_VERSION, check)
        by_day[local_day(closed_at)].append(upsets.pick(m, None, [], check))
    days = {}
    for day, picks in by_day.items():
        days[day] = {"day": day, "closed_markets": len(closed), "candidates": len(fit), "checked": len(picks),
                     "verdicts": {"weak": len(picks)}, "dropped": {}, "picks": picks, "counted_at": local_iso(now)}
    os.makedirs(args.out, exist_ok=True)
    doc = upsets.make_doc(days, now)
    if args.crowd:                               # the crowded day as an older count published it: every pick
        for d in doc["days"]:
            d["picks"] = days[d["day"]]["picks"]
    upsets._write(doc, os.path.join(args.out, "closed.json"))

    # ── open cards ──
    open_day, run_at = day_shift(today, -args.open_days_ago), local_iso(now - args.open_days_ago * DAY)
    cards = []
    if args.cards:
        events = net.walk_open_events(min_volume=250_000)
        events.sort(key=lambda e: -float(e.get("volume24hr") or 0))
        for e in events:
            if len(cards) >= args.cards:
                break
            open_ms = [m for m in e.get("markets") or [] if not m.get("closed") and m.get("question")]
            if not open_ms:
                continue
            m = sorted(open_ms, key=lambda x: x.get("endDate") or "")[0]
            filing = kinds.file_event(e.get("title"), m.get("question"))
            if filing is None or filing.place != "any_day" or kinds.open_exclusion(e.get("title"), m.get("question")):
                continue
            kind = kinds.KINDS[filing.kind]
            heads = headlines(e.get("title"))
            if not heads:
                continue
            heads = [dict(h, step=i == 0, on_card=i == 0) for i, h in enumerate(heads)]
            cards.append({
                "id": opennow.story_key(e.get("title")),
                "label": "new",
                "listed_since": open_day,
                "place": "new step",
                "kind": filing.kind,
                "group": filing.group,
                "question": m.get("question"),
                "timing": {"kind": "any_day", "until": (m.get("endDate") or "")[:10] or None, "last_step": None,
                           "reveal": None, "reveal_note": None},
                "headline": heads[0],
                "who_knows": "If it happens, the few people deciding it will know before the public does.",
                "why_watched": kind.why,
                "other_side": None,
                "polymarket": {"question": m.get("question"), "other_dates": [],
                               "url": f"https://polymarket.com/event/{e.get('slug')}"},
                "headlines": heads,
                "checked_at": run_at,
                "event_ids": [str(e.get("id"))],
                "slugs": [s for s in (e.get("slug"), m.get("slug")) if s],
            })
    # that day's list in the store too, as its run saved it ("entered", the day a card entered with a new step, is
    # kept in the store only, not in open.json)
    if args.open_days_ago and cards:
        store.save_open_day(store.default_conn(), open_day, "updated", run_at,
                            [dict(c, entered=open_day) for c in cards], version=opennow.PROMPT_VERSION,
                            info={"new_today": len(cards), "held_back": 0})
    doc = {
        "generated_at": local_iso(now),
        "rules": {"min_volume": kinds.MIN_VOLUME, "per_day": kinds.PER_DAY, "at_least": kinds.AT_LEAST,
                  "live_days": kinds.LIVE_DAYS, "fresh_days": kinds.FRESH_DAYS, "stay_days": kinds.STAY_DAYS,
                  "dated_days": kinds.DATED_DAYS, "held_back_days": kinds.HELD_BACK_DAYS,
                  "prompt_version": f"{opennow.PROMPT_VERSION} / {opennow.OUT_VERSION}",
                  "model": f"{kinds.STEP_MODEL} / {kinds.OPEN_MODEL}"},
        "days": [{"day": open_day, "status": "updated",
                  "run_at": run_at, "cards": cards, "new_today": len(cards), "held_back": 0}]
        if cards else [],
    }
    with open(os.path.join(args.out, "open.json"), "w") as f:
        json.dump(doc, f, separators=(",", ":"))

    big_ids = {m["conditionId"] for m in big}
    crowd_ids = {m["conditionId"] for m in chosen[:args.crowd]} if args.crowd else set()
    picks = [{"condition_id": p["condition_id"], "slug": p["slug"], "question": p["question"], "winner": p["winner"],
              "day": d["day"], "volume": round(filters.volume(next(m for m in chosen
                                                                  if m["conditionId"] == p["condition_id"]))),
              "big": p["condition_id"] in big_ids, "crowd": p["condition_id"] in crowd_ids}
             for d in days.values() for p in d["picks"]]
    picks.sort(key=lambda p: (p["big"], p["volume"]))
    print(f"seed: {len(picks)} closed pick(s), {len(cards)} open card(s) in {time.time() - t0:.0f} s", file=sys.stderr)
    print(json.dumps({"picks": picks, "cards": [{"story": c["id"], "question": c["question"]} for c in cards]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
