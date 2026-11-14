#!/usr/bin/env python3
"""
Offline tests for the closed list (watchlist.upsets): no network.

    python3 tests/test_watchlist_upsets.py

Covers date ladders (Yes, and No on "continues through"), "nothing happened
by the deadline", public counts and charts, mention markets, nature, the
election keyword, markets settled by the same news, and the day's list
(potential cases first, topped up to 3), and that every closed market is
counted once, checked or under one reason. And the approved factual changes
(2026-11-13): embargoed releases dropped before any model call, a company's
year-end list and a scripted speech's words checked, the "On the open list
from" link to the open list, and --since.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchlist import kinds, store, upsets as up  # noqa: E402
from watchlist.common import to_epoch  # noqa: E402


def test_ladder_choice():
    def mk(mid, end, winner, vol=200_000):
        prices = '["1", "0"]' if winner == "Yes" else '["0", "1"]'
        return {"id": mid, "endDate": end, "outcomes": '["Yes", "No"]', "outcomePrices": prices,
                "umaResolutionStatus": "resolved", "volumeNum": vol}
    a, b, c = mk("a", "2026-11-30T00:00:00Z", "Yes"), mk("b", "2026-11-15T00:00:00Z", "Yes"), mk("c", "2026-11-08T00:00:00Z", "No")
    kept, folded = up.ladder_choice([a, b, c])
    assert [m["id"] for m in kept] == ["b"] and {m["id"] for m in folded} == {"a", "c"}
    kept, folded = up.ladder_choice([c])
    assert [m["id"] for m in kept] == ["c"] and folded == []
    # "continues through": the event happened when it resolved No
    d, e = mk("d", "2026-11-08T00:00:00Z", "No"), mk("e", "2026-11-30T00:00:00Z", "No")
    for m, q in ((d, "US x Iran ceasefire continues through November 8?"), (e, "US x Iran ceasefire continues through November 30?")):
        m["question"] = q
    kept, folded = up.ladder_choice([e, d])
    assert [m["id"] for m in kept] == ["d"] and [m["id"] for m in folded] == ["e"]


def test_nothing_happened_by_the_deadline():
    assert up.nothing_happened("NATO x Russia military clash by August 31, 2026?", "No")
    assert up.nothing_happened("US x Iran effective ceasefire continues through November 8, 2026?", "Yes")
    assert up.nothing_happened("Plague pandemic in 2026?", "No")
    assert not up.nothing_happened("Putin meets with Iranian officials by August 31?", "Yes")          # it happened
    # "by 25 bps" is not a deadline: a No on a rate decision is a real decision
    assert not up.nothing_happened("Will the Fed increase interest rates by 25 bps after the September 2026 meeting?", "No")
    assert not up.nothing_happened("Will the chopsticks catch SpaceX Starship Flight Test 14 Starship?", "No")
    assert up.nothing_happened("Will GPT Astra 6.1+ be released between November 2 and November 8, 2026?", "No")
    assert up.nothing_happened("Will GPT Astra 6.1+ be released on November 6, 2026?", "No")
    assert up.nothing_happened("Will Strait of Hormuz traffic return to normal in October?", "No")
    assert up.nothing_happened("Will there be no next Google Gemini Pro model release by November 1, 2026?", "Yes")
    assert not up.nothing_happened("Will there be no next Google Gemini Pro model release by November 1, 2026?", "No")
    assert not up.nothing_happened("US x Iran effective ceasefire continues through November 8, 2026?", "No")  # it broke


def test_public_counts():
    assert up.PUBLIC_COUNT.search("Will Elon Musk post 160-179 tweets from August 25 to September 1, 2026?")
    assert up.PUBLIC_COUNT.search("Will MrBeast's next video get at least 100 million views on week 1?")
    for q in ('Will "Wildwood" 2nd Weekend Box Office be between 7m and 9m?',
              'Will "Housewife - Tate McRae" be the #1 US song this week?',
              'Will "Choosin\' Texas - Ella Langley" be the Billboard Hot 100 #1 song this week?',
              "Will OpenRouter process between 185T and 195T tokens the week of October 26?",
              "Will there be at least 5000 measles cases in the U.S. in 2026?",
              "Will there be between 0 and 5 average daily transits of the Strait of Hormuz on October 31?",
              "Will Kai Cenat finish his GTA marathon by November 11, 7:59 AM ET?"):
        assert up.PUBLIC_COUNT.search(q), q
    for q in ("Will Trump post about Iran?", "Trump renames AI by September 30?", "Will 1win launch a token by October 31, 2026?",
              "Will Susie Wiles leave the Trump administration before 2027?"):
        assert not up.PUBLIC_COUNT.search(q), q
    assert up.MENTION.search('Will Trump say "Bibi" this week?') and not up.MENTION.search("Will Trump say he will run?")
    assert up.NATURE.search("Will there be between 35 and 59 tornadoes in the US in October 2026?")


def test_election_keyword():
    assert up.ELECTION.search("Will the Democrats win the Ohio Senate race in 2026?")
    assert up.ELECTION.search("Taipei Mayor Election Winner")
    assert not up.ELECTION.search("US strikes Iran by February 28, 2026?")
    assert not up.ELECTION.search("Maduro out by January 31, 2026?")


def test_same_news():
    assert up.names("Will Susie Wiles leave the Trump administration before 2027?") == {"susie wiles"}
    assert up.names("US x Iran effective ceasefire continues through November 8, 2026?") == set()
    ms = [{"conditionId": "a", "question": "Gemini Carbon released by November 10, 2026?"},
          {"conditionId": "b", "question": "Will Susie Wiles leave the Trump administration before 2027?"},
          {"conditionId": "c", "question": "Will Gemini Carbon be released on November 10, 2026?"},
          {"conditionId": "d", "question": "Susie Wiles out as White House Chief of Staff by December 31?"}]
    kept, folded = up.fold_same_news(ms)
    assert [m["conditionId"] for m in kept] == ["a", "b"]
    assert {k: [m["question"] for m in v] for k, v in folded.items()} == {
        "a": ["Will Gemini Carbon be released on November 10, 2026?"],
        "b": ["Susie Wiles out as White House Chief of Staff by December 31?"]}


def test_choose_at_least_three():
    def p(v, s="unknown", sig="unknown"):
        return {"verdict": v, "surprise": s, "public_signals": sig}
    picks = [p("not a case", "no"), p("weak", "yes"), p("potential case", "somewhat"), p("potential case", "yes", "none"),
             p("not a case", "yes")]
    shown = up.choose(picks)
    assert [(x["verdict"], x["surprise"]) for x in shown] == [("potential case", "yes"), ("potential case", "somewhat"),
                                                              ("weak", "yes")]
    shown = up.choose([p("not a case", "no"), p("weak"), p("not a case", "yes")])   # a short day is topped up to 3
    assert [x["verdict"] for x in shown] == ["weak", "not a case", "not a case"] and shown[1]["surprise"] == "yes"
    many = [p("potential case")] * 7
    assert len(up.choose(many)) == 5
    assert up.choose([p("not a case")]) == [p("not a case")]



def _market(cid, question, title, eid, winner="Yes", vol=50_000):
    prices = '["1", "0"]' if winner == "Yes" else '["0", "1"]'
    return {"id": cid, "conditionId": cid, "question": question, "slug": f"m-{cid}", "endDate": "2026-11-12T00:00:00Z",
            "closedTime": "2026-11-12T20:00:00Z", "outcomes": '["Yes", "No"]', "outcomePrices": prices,
            "umaResolutionStatus": "resolved", "volumeNum": vol, "events": [{"id": eid, "slug": f"ev-{eid}", "title": title}]}


class _Patch:
    """Swap module attributes for a test and put them back."""
    def __init__(self, **swaps):
        self.swaps = swaps

    def __enter__(self):
        self.saved = {k: getattr(*self._at(k)) for k in self.swaps}
        for k, v in self.swaps.items():
            setattr(*self._at(k), v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(*self._at(k), v)

    @staticmethod
    def _at(key):
        mod, name = key.split("__")
        return {"ai": up.ai, "judge": up.judge}[mod], name


EMBARGOED = [("Will monthly inflation increase by 0.4% in October?", "October Inflation US - Monthly"),
             ("Bank of Mexico decreases interest rates by 25 bps after November 2026 meeting?",
              "Bank of Mexico Decision in November?"),
             ("McDonald's (MCD) Q3 global comparable sales growth 1% to 2%?",
              "McDonald's (MCD) Q3 global comparable sales growth?"),
             ("Will Anthropic raise less than $65B in its IPO?", "Anthropic IPO")]
CHECKED = [("Will Taylor Swift be Spotify Wrapped's top artist of 2026?", "Spotify Wrapped 2026"),
           ("Will Charlie Kirk be a top 5 most searched person on Google in 2026?", "Google Year in Search 2026"),
           ('Will Trump say "tariff" during the State of the Union?', "State of the Union mentions"),
           ("Jensen Huang announced resignation or removal by November 12, 2026?", "Jensen Huang out as Nvidia CEO?")]


def test_embargoed_releases_dropped_before_any_model_call():
    ms = [_market(f"x{i}", q, t, f"e{i}") for i, (q, t) in enumerate(EMBARGOED + CHECKED)]
    ms.append(_market("m", 'Will Trump say "Bibi" this week?', "What will Trump say this week?", "em"))
    ms.append(_market("v", "Will MrBeast's next video get at least 100 million views on week 1?", "MrBeast views", "ev"))
    asked = []
    with _Patch(ai__label_markets=lambda kept, conn=None, **k: (asked.extend(m["question"] for m in kept) or {})):
        cands, dropped = up.candidates(ms, conn=None)
    assert dropped[kinds.EMBARGO_REASON] == len(EMBARGOED)                  # the reason the page lists, as given
    assert not set(asked) & {q for q, _ in EMBARGOED}                       # no model call for them, not even a label
    # a company's own year-end list and a scripted speech's words are checked now; unscripted mentions and
    # public counts still are not
    assert [m["question"] for m, _, _ in cands] == [q for q, _ in CHECKED]
    assert dropped["words said in public (mention market)"] == 1
    assert dropped["counted or charted in public (views, box office, charts, ...)"] == 1
    assert sum(dropped.values()) == len(EMBARGOED) + 2


def _ladder(prefix, eid, title, n=3):
    """A date ladder of n markets in one event, the shortest-dated resolved Yes."""
    out = []
    for i in range(n):
        m = _market(f"{prefix}{i}", f"{title} by November {10 + i}, 2026?", f"{title} by...?", eid)
        m["endDate"] = f"2026-11-{10 + i}T00:00:00Z"
        out.append(m)
    return out


def test_dropped_ladder_takes_its_later_dates_along():
    # a date ladder whose kept market a label drops: its later dates are counted under the same reason, not lost
    for label, reason in (("election_vote", "election or vote"), ("game_sport", "game or sport")):
        ladder = _ladder("l", "el", "Project Lumen chosen")
        with _Patch(ai__label_markets=lambda kept, conn=None, **k: {m["id"]: {"type": label} for m in kept}):
            cands, dropped = up.candidates(ladder, conn=None)
        assert cands == [] and dict(dropped) == {reason: 3}, (label, dropped)


def test_every_closed_market_is_counted_once():
    # the page reads "N markets closed ... M were left after taking out the kinds below": every closed market is
    # either checked or under one reason
    kept_ladder = _ladder("k", "ek", "Project Vela launched")
    vote_ladder = _ladder("v", "ev", "Project Lumen chosen")
    embargoed = _market("x", *EMBARGOED[0], "ex")
    unchecked = _market("u", "Will Project Orion ship by November 12, 2026?", "Project Orion ships", "eu", vol=40_000)
    ms = kept_ladder + vote_ladder + [embargoed, unchecked]
    labels = {m["id"]: {"type": "election_vote" if m in vote_ladder else "other"} for m in ms}

    def checks(cands, conn, client=None):            # the unchecked market's news or judgement failed
        return {m["conditionId"]: {"verdict": "weak", "what_happened": "", "surprise": "yes", "who_knew_first": "the board",
                                   "public_signals": "none", "reason": ""} for m in cands if m is not unchecked}
    with _Patch(ai__label_markets=lambda kept, conn=None, **k: {m["id"]: labels[m["id"]] for m in kept},
                judge__check_markets=checks):
        d = up.run_day(to_epoch("2026-11-12T00:00:00Z"), ms, store.connect(":memory:"))
    assert d["closed_markets"] == len(ms) == d["checked"] + sum(d["dropped"].values())
    assert d["dropped"] == {"election or vote": 3, "later date of the same event": 2, kinds.EMBARGO_REASON: 1,
                            "could not be checked": 1}
    assert d["candidates"] == 2 and d["checked"] == 1


def test_on_the_open_list_from():
    conn = store.connect(":memory:")
    card = {"id": "huang-out", "place": "new step", "label": "new", "listed_since": "2026-11-09",
            "event_ids": ["e9"], "slugs": ["ev-e9"]}
    store.save_open_day(conn, "2026-11-09", "updated", "2026-11-09T04:00:00Z", [card])
    store.save_open_day(conn, "2026-11-10", "updated", "2026-11-10T04:00:00Z", [dict(card, label="listed")])
    huang = _market("h", "Jensen Huang announced resignation or removal by November 12, 2026?",
                    "Jensen Huang out as Nvidia CEO?", "e9", vol=20_000)
    others = [_market(f"o{i}", f"Will Project{i} launch a token by November 12, 2026?", f"Project{i} token launch",
                      f"w{i}", vol=900_000 - i) for i in range(6)]
    assert up.open_idents([huang]) == ["e9", "ev-e9", "m-h"]

    def checks(ms, conn, client=None):
        return {m["conditionId"]: {"verdict": "not a case" if m is huang else "potential case", "what_happened": "",
                                   "surprise": "yes", "who_knew_first": "the board", "public_signals": "none",
                                   "reason": ""} for m in ms}
    with _Patch(ai__label_markets=lambda kept, conn=None, **k: {}, judge__check_markets=checks):
        d = up.run_day(to_epoch("2026-11-12T00:00:00Z"), [huang] + others, conn)
    shown = {p["condition_id"]: p for p in d["picks"]}
    # 6 potential cases fill the 5 places, and the market that was on an open card is shown anyway, last
    assert len(shown) == up.PER_DAY + 1 and d["picks"][-1]["condition_id"] == "h"
    assert shown["h"]["on_open_list_from"] == "2026-11-09"                 # the first day it was listed
    assert all("on_open_list_from" not in p for k, p in shown.items() if k != "h")
    # a market counted under another (a later date, the same news) carries the link too
    assert store.open_listed_since(conn, up.open_idents([others[0], huang])) == "2026-11-09"
    assert store.open_listed_since(conn, up.open_idents(others)) is None


def test_choose_keeps_open_listed_picks():
    def p(v, s="unknown", sig="unknown", since=None):
        out = {"verdict": v, "surprise": s, "public_signals": sig}
        return dict(out, on_open_list_from=since) if since else out
    listed = p("not a case", "no", since="2026-11-09")
    shown = up.choose([p("weak", "yes"), p("not a case", "yes"), p("not a case", "somewhat"), p("not a case"), listed])
    assert shown[-1] is listed and len(shown) == 4
    assert up.choose([listed, p("weak")]) == [p("weak"), listed]           # sorted as usual


def test_days_since():
    now = to_epoch("2026-11-13T08:57:00Z")
    assert up.days_since("2026-10-30", now) == 15 and up.days_since("2026-11-13", now) == 1
    assert up.days_since("2026-11-14", now) is None and up.days_since("Oct 30", now) is None
    assert up.days_since("", now) is None


def test_open_listed_markets_get_a_verdict_and_the_shared_words():
    conn = store.connect(":memory:")
    card = {"id": "tesla-spacex", "place": "new step", "label": "new", "listed_since": "2026-11-09",
            "event_ids": ["e7"], "slugs": ["ev-e7"],
            "who_knows": "If it happens, Tesla's board, both boards and their bankers will know before the public does."}
    store.save_open_day(conn, "2026-11-09", "updated", "2026-11-09T04:00:00Z", [card])
    # a filing alone (a market the open list named but never showed) gives no sentence
    store.save_filing(conn, "e8", "merger_acquisition", {"subject": "Acme", "decider": "Acme's board"}, "rule")
    # nothing happened by the deadline, and the label calls it a vote: still checked, because it was on an open card
    listed = _market("t", "Tesla and SpaceX merger announced by November 12?", "Tesla and SpaceX merger announced by...?",
                     "e7", winner="No")
    other = _market("u", "Acme and Globex merger announced by November 12?", "Acme and Globex merger announced by...?",
                    "e8", winner="No")
    labels = {"t": {"type": "election_vote"}, "u": {"type": "other"}}
    with _Patch(ai__label_markets=lambda kept, conn=None, **k: {m["id"]: labels[m["id"]] for m in kept}):
        cands, dropped = up.candidates([listed, other], conn)
    assert [m["id"] for m, _, _ in cands] == ["t"] and dropped["nothing happened by the deadline"] == 1
    words = up.vocabulary(listed, conn)
    assert words["kind"] == "merger_acquisition" and words["group"] == 1
    assert words["who_knows"] == card["who_knows"]                       # the open card's own sentence
    assert "who_knows" not in up.vocabulary(other, conn) and up.vocabulary(other)["kind"] == "merger_acquisition"
    p = up.pick(listed, None, [], {"verdict": "not a case", "what_happened": "", "surprise": "no", "who_knew_first": "",
                                    "public_signals": "none", "reason": ""}, "2026-11-09", words)
    assert p["on_open_list_from"] == "2026-11-09" and p["kind"] == "merger_acquisition" and p["group"] == 1


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for fn in checks:
        try:
            fn()
            print(f"PASS {fn.__name__}", flush=True)
        except Exception as e:
            failed.append(fn.__name__)
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}", flush=True)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)
