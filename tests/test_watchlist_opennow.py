#!/usr/bin/env python3
"""
Offline tests for the "Open now" list (watchlist.opennow): no network, a
fake Gamma walk, a fake news feed and fake model clients.

    python3 tests/test_watchlist_opennow.py

Covers the pool (the rules that use no price, the 99c check at first entry
only, the embargo), the named deadline, story keys and the merge by names,
the names check (the subject in the market's own words, no betting words),
the step reading (its prompt, the numbers mapped to the headlines given and
the "who acts" check, what one reading is given, the verdicts: a step only
when both readings mark it, the first reading of a headline sets it), the
word-for-word "already out" copy check, the odds and betting filter and the
14-day window, news threads, the five places on the proposal's own replay
(final/f03_replay.py, Nov 6-13: Huang's 3-day stay, Syria and Israel held
back, the dated place) and its faster variant (f06), the caps, a short day
never padded, the labels, and whole runs: a first day whose cards need no
approval, a second reading that disagrees, a person's override (a rejected
headline never shows, an approved one always counts), a day the model is
down (yesterday's live cards stay, none added, "not updated today"), one
story's reading failing, a day an answer comes out (held back), a re-run
on the same news asking nothing (headlines over the cap are not new), a
re-run while the model is down keeping the day's earlier list, every step
reading refused counting as down, a person's rebuild of the reviewed day
reading no new step, and the consistency test's files.
"""
import csv
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchlist import ai, kinds as K, opennow as O, store  # noqa: E402
from watchlist.common import DAY, to_epoch  # noqa: E402

NOW = to_epoch("2026-11-13T04:00:00Z")
SHIPPED_STEPS_BY = O.STEPS_BY
O.STEPS_BY = "model"     # the whole runs below test the readings' own steps; test_steps_wait_for_a_person_when_set
                         # tests the approval the module ships with


# ── pool ─────────────────────────────────────────────────────────────────────

def market(mid, question, end="2027-01-01T00:00:00Z", volume=50_000, prices=("0.1", "0.9"), **kw):
    m = {"id": str(mid), "question": question, "slug": f"m{mid}", "endDate": end, "volumeNum": volume,
         "outcomes": '["Yes", "No"]', "outcomePrices": json.dumps(list(prices)), "active": True, "closed": False,
         "description": kw.pop("description", f"This market will resolve to Yes if {question}")}
    m.update(kw)
    return m


def event(eid, title, markets, **kw):
    return dict({"id": str(eid), "slug": f"e{eid}", "title": title, "markets": markets}, **kw)


def test_named_day():
    assert O.named_day("Kraken IPO by December 31, 2026?", to_epoch("2027-01-01T00:00:00Z")) == "2026-12-31"
    assert O.named_day("Lutnick out by December 31?", to_epoch("2027-01-01T00:00:00Z")) == "2026-12-31"
    assert O.named_day("Rubio out by November 30?", to_epoch("2026-12-01T00:00:00Z")) == "2026-11-30"
    assert O.named_day("X in 2026?", to_epoch("2027-01-01T00:00:00Z")) == "2026-12-31"     # midnight: the day before
    assert O.named_day("X before 2027?", to_epoch("2026-12-31T12:00:00Z")) == "2026-12-31"
    # the end of the day in US Eastern time (23:59 EST is 04:59 UTC the next day)
    assert O.named_day("Will Tesla sell a Cybercab for 30k or less in 2026?", to_epoch("2027-01-01T04:59:00Z")) == "2026-12-31"
    assert O.named_day("Jury Winner", to_epoch("2026-11-15T04:59:00Z")) == "2026-11-14"
    assert O.named_day("X by Jan 31?", to_epoch("2027-02-01T00:00:00Z")) == "2027-01-31"
    assert O.named_day("X", None) is None


def test_walk_pool_rules():
    evs = [
        event(1, "Jane Roe out as Secretary of Widgets by...?",
              [market(11, "Jane Roe out as Secretary of Widgets by December 31?"),
               market(12, "Jane Roe out as Secretary of Widgets by November 13?", end="2026-11-13T12:00:00Z"),
               market(13, "Jane Roe out by October 31?", closed=True)]),
        event(2, "Fed decision in December?", [market(21, "Fed decreases interest rates by 25 bps after December 2026 meeting?")]),
        event(3, "Small market?", [market(31, "Will Acme launch a widget by December 31?", volume=5_000)]),
        event(4, "Nearly decided?", [market(41, "Will Acme launch a gadget by December 31?", prices=("0.995", "0.005"))]),
        event(5, "Entered earlier, now at 99c", [market(51, "Will Globex launch a gadget by December 31?",
                                                        prices=("0.999", "0.001"))]),
        event(6, "Lakers vs Celtics", [market(61, "Lakers vs Celtics", tags=[{"slug": "sports"}])]),
    ]
    pool, entering, counts = O.walk_pool(evs, NOW, {"5": {"under_99c": 1}})
    ids = [e["id"] for e in pool]
    assert ids == ["1", "5"], ids
    assert [(e["id"], capped) for e, capped in entering] == [("1", [])]       # 5 entered on an earlier day
    roe = pool[0]
    assert [m["id"] for m in roe["markets"]] == ["11"]                         # due within 12 h, closed: out
    assert roe["markets"][0]["until"] == "2026-12-31"
    assert counts["market: due within 12 hours"] == 1 and counts["market: volume"] == 1
    assert counts["event: an outcome at 99c or more when it first entered"] == 1
    assert any(k.startswith("market: embargoed release") for k in counts)
    assert "price" not in json.dumps({k: v for k, v in roe.items() if k != "markets"})
    # an entry stored as failing the check stays out, whatever the price now
    pool, _, _ = O.walk_pool(evs[:1], NOW, {"1": {"under_99c": 0}})
    assert pool == []
    # a market at 99c or more when its event first entered never enters, whatever its price later
    ladder = event(7, "Acme CEO out by...?", [market(71, "Acme CEO out by November 30?", prices=("0.995", "0.005")),
                                              market(72, "Acme CEO out by December 31?")])
    pool, entering, _ = O.walk_pool([ladder], NOW, {})
    assert entering[0][1] == ["71"] and [m["id"] for m in pool[0]["markets"]] == ["72"]
    ladder["markets"][0]["outcomePrices"] = json.dumps(["0.5", "0.5"])
    pool, _, _ = O.walk_pool([ladder], NOW, {"7": {"under_99c": 1, "capped": ["71"]}})
    assert [m["id"] for m in pool[0]["markets"]] == ["72"]


def test_story_key_and_query():
    assert O.story_key("Howard Lutnick out as Secretary of Commerce by...?") == "howard lutnick out as secretary of commerce"
    assert O.story_key("X by November 30?") == O.story_key("X by December 31, 2026?") == "x"
    assert O.title_query("Israel x Iran ceasefire continues through...?") == "Israel Iran ceasefire continues"
    assert O.title_query("Will Trump visit North Korea by...?") == "Trump visit North Korea"


def _pool_event(eid, title, question, kind_rule=True):
    e = O.pool_event(event(eid, title, [market(eid * 10, question)]), [market(eid * 10, question)])
    e["filing"] = K.file_event(title, question)
    return e


def test_make_stories_merge_by_names():
    a = _pool_event(1, "Pete Hegseth out as Secretary of War by...?", "Pete Hegseth out as Secretary of War by December 31?")
    b = _pool_event(2, "Hegseth out before 2027?", "Hegseth out before 2027?")
    c = _pool_event(3, "Jane Roe out as Secretary of Widgets by...?", "Jane Roe out as Secretary of Widgets by December 31?")
    names = {"subject": "Pete Hegseth", "short": "Hegseth", "post": "the Defense Department", "decider": "Trump"}
    stored = {e["id"]: {"kind": "us_official", "names": names, "source": "rule", "version": O.NAMES_VERSION,
                        "text_sha1": O.text_sha1(e), "story": None} for e in (a, b)}
    st = O.make_stories([a, b, c], stored, {"1": 100, "2": 200, "3": 50})
    assert len(st) == 2, list(st)
    heg = st[O.story_key(a["title"])]
    assert sorted(e["id"] for e in heg["events"]) == ["1", "2"] and heg["names"] == names
    roe = st[O.story_key(c["title"])]
    assert roe["kind"] == "us_official" and roe["names"] is None and roe["group"] == 1
    # a changed text or another names prompt asks again; a person's filing always holds
    stored["1"]["text_sha1"] = "old"
    stored["2"]["version"] = "open-names-0"
    st = O.make_stories([a, b], stored)
    assert all(s["names"] is None for s in st.values())
    stored["1"] = dict(stored["1"], source="person")
    assert O.make_stories([a], stored)[O.story_key(a["title"])]["names"] == names


def test_check_names():
    text = "Howard Lutnick out as Secretary of Commerce by...? || Howard Lutnick out as Secretary of Commerce by December 31?"
    good = {"kind": "us_official", "subject": "Howard Lutnick", "short": "Lutnick", "aliases": ["Commerce Secretary Lutnick"],
            "decider": "Trump", "others": [], "other": "", "post": "the Commerce Department", "what": "", "which": "",
            "question": "", "search_names": "Howard Lutnick", "search_event": "\"Howard Lutnick\" resign OR fired site:x.com"}
    kind, names = O.check_names("us_official", good, text)
    assert kind == "us_official" and names["subject"] == "Howard Lutnick" and "what" not in names
    assert names["search_event"] == "Howard Lutnick resign OR fired"            # quotes and operators out
    assert K.card_text(kind, names)["question"] == "Will Howard Lutnick leave the Commerce Department?"
    assert O.check_names("us_official", dict(good, subject="Gina Raimondo"), text) == (None, None)   # not the market's words
    assert O.check_names("us_official", dict(good, post=""), text) == (None, None)                  # a slot the kind needs
    assert O.check_names("us_official", dict(good, subject="Howard Lutnick odds"), text) == (None, None)
    beto = "Will Beto O’Rourke announce a Presidential run before 2027?"
    assert O.check_names("candidacy_or_party", dict(good, subject="Beto O’Rourke", short="O’Rourke",
                                                    what="a presidential run", post=""), beto)[1]  # "Beto" is no bet
    jury = {"subject": "", "what": "the Eurovision Asia 2026 Jury Prize", "which": "Which act", "decider": "", "post": "",
            "search_names": "Eurovision Asia 2026", "search_event": "Eurovision Asia 2026 jury winner"}
    kind, n = O.check_names("jury_prize", dict(good, **jury), "Eurovision Asia 2026: Jury Winner")
    assert n["subject"] == "Eurovision Asia 2026"                     # its plain name, in the market's title
    bad = O.check_names("us_official", dict(good, short="Lutnick bets"), text)[1]
    assert bad and "short" not in bad                                         # a betting word is dropped, never written
    # ... a needed one too: the kind's usual decider stands in, the word is never written
    assert O.check_names("us_official", dict(good, decider="the bettors"), text)[1]["decider"] == "the President"
    # an unfiled event: the model's kind from the fixed list, or nothing
    assert O.check_names(None, dict(good, kind="us_official"), text)[0] == "us_official"
    assert O.check_names(None, dict(good, kind="none"), text) == (None, None)
    assert O.check_names(None, dict(good, kind="made_up"), text) == (None, None)
    assert O.clean_query("OR Acme Globex merger OR") == "Acme Globex merger"
    # the short form anchors a subject the market writes only in part; a short form of someone else does not
    blank = dict({s: "" for s in O._NAME_SLOTS}, aliases=[], others=[])
    musk = dict(blank, subject="Elon Musk", short="Musk", decider="Tesla's board", post="Tesla's chief executive")
    assert O.check_names("company_exec", musk, "Musk out as Tesla CEO before 2027?")[1]["subject"] == "Elon Musk"
    assert O.check_names("us_official", dict(good, subject="Gina Raimondo", short="Lutnick"), text) == (None, None)
    # a kind whose question takes no subject: the post the market names becomes the subject headlines must name
    bond = O.check_names("casting", dict(blank, decider="the producers", post="James Bond"), "Next James Bond actor?")[1]
    assert bond["subject"] == "James Bond"
    # a market that names the outcome, not who acts: the outcome anchors it and is a name headlines may use
    grok = O.check_names("ai_model_release", dict(blank, subject="xAI", short="xAI", what="Grok 5"),
                         "Grok 5 released by...?")[1]
    assert grok["subject"] == "xAI" and grok["aliases"] == ["Grok 5"]
    assert O.check_names("us_official", dict(good, subject="Gina Raimondo", short="",
                                             post="Secretary of Commerce"), text) == (None, None)


def test_reads_well_and_fallback():
    assert O.reads_well("us_official", {"subject": "Howard Lutnick", "post": "the Commerce Department", "decider": "Trump"})
    assert O.reads_well("merger_acquisition", {"subject": "Tesla and SpaceX", "decider": "Elon Musk"})   # the kind's default
    assert O.reads_well("planned_strike_or_op", {"subject": "Russia", "what": "an invasion of a NATO country"})
    assert not O.reads_well("planned_strike_or_op", {"subject": "Russia", "what": "invade a NATO country"})
    assert not O.reads_well("ceasefire_peace", {"subject": "Ukraine", "what": "agrees not to join NATO"})
    assert not O.reads_well("us_executive", {"subject": "Trump", "what": "eliminating capital gains tax"})
    assert not O.reads_well("meeting_or_visit", {"subject": "Trump", "what": "Mojtaba Khamenei"})      # a person
    assert not O.reads_well("meeting_or_visit", {"subject": "Mojtaba Khamenei", "what": "a US x Iran meeting"})
    assert O.reads_well("meeting_or_visit", {"subject": "Trump", "what": "a meeting with Mojtaba Khamenei"})
    assert not O.reads_well("elected_head", {"subject": "Netanyahu", "post": "Prime Minister"})
    assert O.reads_well("elected_head", {"subject": "Netanyahu", "post": "office as prime minister"})
    assert not O.reads_well("us_politician", {"subject": "Mitch McConnell", "post": "Senate"})
    assert not O.reads_well("company_exec", {"subject": "Ari Weinstein", "post": "OpenAI", "decider": "OpenAI's board"})
    # after "leave", a title becomes an office and a bare body takes "the" before the check
    for post, want in (("Secretary of Commerce", "office as Secretary of Commerce"), ("Senate", "the Senate"),
                       ("Israel's prime minister", "office as Israel's prime minister"),
                       ("the Commerce Department", "the Commerce Department"), ("his post", "his post")):
        t = O.tidy_names("us_official", {"subject": "Jane Roe", "post": post, "decider": "Trump"})
        assert t["post"] == want and O.reads_well("us_official", t), (post, t)
    # "x" between two sides is written "and" when the names are checked
    text = "China x India military clash by...? || China x India military clash by December 31?"
    ans = {"kind": "clash", "subject": "China x India", "short": "", "aliases": [], "decider": "", "others": [],
           "other": "", "post": "", "what": "", "which": "", "question": "", "search_names": "China India",
           "search_event": "China India clash"}
    kind, names = O.check_names("clash", ans, text)
    assert names["subject"] == "China and India" and O.reads_well(kind, names)
    # a question that does not read well: the card and the model get Polymarket's own question
    ev = _pool_event("e9", "Russia x NATO military clash by...?", "Will Russia invade a NATO country by December 31?")
    story = {"kind": "planned_strike_or_op", "names": {"subject": "Russia", "what": "invade a NATO country"},
             "events": [ev], "head": ev}
    t = O.sentences(story, NOW)
    assert t["question"] == "Will Russia invade a NATO country by December 31?", t
    assert t["settles"] == 'a Yes to the market\'s own question, "Will Russia invade a NATO country by December 31?"'
    story["names"]["what"] = "an invasion of a NATO country"
    assert O.sentences(story, NOW)["question"] == "Will Russia carry out an invasion of a NATO country?"
    # a launch template for a market about a sale asks something else: Polymarket's question
    cab = {"subject": "Tesla", "what": "a Cybercab for 30k or less"}
    assert O.reads_well("product_launch", cab)
    assert not O.reads_well("product_launch", cab, "Will Tesla sell a Cybercab for 30k or less in 2026?")
    assert O.tidy_names("product_launch", cab)["what"] == "a Cybercab for $30,000 or less"
    # an outcome that repeats the whole subject is the kind's usual one
    m = O.tidy_names("merger_acquisition", {"subject": "Tesla and SpaceX", "what": "a merger of Tesla and SpaceX",
                                            "decider": "Tesla's board"})
    assert K.card_text("merger_acquisition", m)["question"] == "Will Tesla and SpaceX announce a merger?"


def test_ceasefire_continues_names_by_rule():
    model = {"subject": "Israel and Iran", "what": "a ceasefire", "decider": "", "search_names": "Israel Iran ceasefire",
             "search_event": "Israel Iran ceasefire continues", "aliases": ["Iranian"]}
    n = O.rule_names({"title": "Israel x Iran ceasefire continues through...?"}, "planned_strike_or_op", model)
    t = K.card_text("planned_strike_or_op", n)
    assert t["question"] == "Will Israel or Iran carry out a strike on the other?" and O.reads_well("planned_strike_or_op", n)
    assert t["settles"] == "Israel or Iran carries out a strike on the other"
    assert n["search_event"] == "Israel Iran strike attack" and n["aliases"] == ["Iranian"]
    us = O.rule_names({"title": "US x Iran effective ceasefire continues through November 30?"}, "planned_strike_or_op",
                      model)
    assert K.card_text("planned_strike_or_op", us)["question"] == "Will the US or Iran carry out a strike on the other?"
    # other kinds and other titles keep the model's names
    assert O.rule_names({"title": "Russia x Ukraine ceasefire in 2026?"}, "ceasefire_peace", model) is model
    assert O.rule_names({"title": "Israel strikes Lebanon by...?"}, "planned_strike_or_op", model) is model
    assert not O.reads_well("jury_prize", {"what": "the Jury Winner", "which": "Which country", "decider": "the juries"})


def test_names_prompt():
    p = O.names_prompt([{"id": "n0", "title": "Jane Roe out by...?", "kind": None,
                         "questions": ["Jane Roe out by December 31?", "Jane Roe out by December 31?"]},
                        {"id": "n1", "title": "Acme IPO?", "kind": "ipo_filing", "questions": ["Acme IPO?"]}])
    assert "Kind: choose" in p and "Kind: ipo_filing" in p and p.count("Jane Roe out by December 31?") == 1
    assert "- us_official [any_day]: Will {subject} leave {post}?" in p and "{question}" in p
    assert not re.search(r"\b(volume|outcomePrices|price of|\d+%)", p)


# ── copies, headlines ────────────────────────────────────────────────────────

GIVEN = [
    {"title": "White House discussing Howard Lutnick's future as Commerce secretary, people familiar say",
     "source": "Reuters", "published": "2026-11-12T18:19:11Z", "url": "u1"},
    {"title": "Trump ‘considering sacking Lutnick’ in wake of Nvidia scandal", "source": "The Telegraph",
     "published": "2026-11-12T21:36:00Z", "url": "u2"},
    {"title": "Commerce secretary faces bipartisan pressure", "source": "NBC News", "published": "2026-11-12T10:00:00Z",
     "url": "u3"},
    {"title": "Trump Backs Lutnick Despite Uproar Over Nvidia Chip Controls", "source": "Bloomberg",
     "published": "2026-11-12T15:29:23Z", "url": "u4"},
]
LUT = {"subject": "Howard Lutnick", "short": "Lutnick"}


def test_check_steps():
    given = [dict(h) for h in GIVEN]
    parsed = {"headlines": [
        {"n": 1, "who_acts": "White House", "what": "discussing Lutnick's future", "category": "step"},
        {"n": 2, "who_acts": "the President", "what": "considering sacking Lutnick", "category": "step"},  # not in it
        {"n": 2, "who_acts": "Trump", "what": "again", "category": "step"},                  # given twice: ignored
        {"n": 7, "who_acts": "Commerce", "what": "no such number", "category": "step"},      # not given: ignored
        {"n": 3, "who_acts": "", "what": "pressure", "category": "not_deciders"},
        {"n": 4, "who_acts": "Trump", "what": "backs Lutnick", "category": "denial"},
        {"n": "x", "who_acts": "Trump", "what": "?", "category": "step"},
    ]}
    d = O.check_steps(parsed, given)
    assert [h["url"] for h in d["steps"]] == ["u1"], d
    assert [h["url"] for h in d["denials"]] == ["u4"] and d["already_out"] == []
    assert d["answered"] == [O._key(given[i]) for i in (0, 1, 2, 3)] and d["categories"]["2"] == "step"
    assert d["notes"] == {"a step whose 'who acts' is not in its headline": 1,
                          "a number not given, or given twice": 2, "no number": 1}, d["notes"]
    assert O.check_steps(None, given)["answered"] == [] and O.check_steps({"headlines": "x"}, given)["steps"] == []
    # "who acts" must be words of that very headline (a number pointing at the wrong headline fails it)
    assert O.actor_in_headline("Nvidia's board", "Nvidia board weighs CEO Huang's future")
    assert not O.actor_in_headline("Nvidia's board", "SpaceX and Tesla talks progress")
    assert not O.actor_in_headline("", "Anything")


def test_step_prompt_and_candidates():
    item = {"question": "Howard Lutnick out as Secretary of Commerce by December 31?",
            "settles": "Howard Lutnick leaves office as Secretary of Commerce", "deciders": "the President",
            "subject": "Howard Lutnick", "rules": "This market will resolve to Yes if ..."}
    p = O.step_prompt(item, GIVEN[:2])
    assert p.startswith("You read news headlines") and "Who decides whether it happens: the President" in p
    assert '"step": the President, or people acting for them' in p and "this exact event: Howard Lutnick leaves" in p
    assert "[1] 2026-11-12 18:19 UTC · Reuters · White House discussing" in p and "[2] 2026-11-12 21:36 UTC" in p
    assert "[3]" not in p and "price" not in p.split("Market question:")[1].split("Numbered")[0].lower()
    # one reading is given the step-word headlines of the last 7 days: unread first, then leaks, agencies, newest
    hs = [_h("Board weighs plan", 2, "Blog"), _h("Board weighs plan, sources say", 30, "Blog"),
          _h("Board weighs offer", 20, "Reuters"), _h("Plain report", 1, "Reuters"), _h("Board weighs old", 24 * 8)]
    hs += [_h(f"Board weighs option {i}", 40 + i) for i in range(10)]
    got = O.step_candidates(hs, set(), NOW)
    assert len(got) == O.STEP_HEADLINES and "Plain report" not in [h["title"] for h in got]
    assert [h["title"] for h in got[:3]] == ["Board weighs plan, sources say", "Board weighs offer", "Board weighs plan"]
    assert "Board weighs old" not in [h["title"] for h in got]
    read = {O._key(h) for h in got}
    again = O.step_candidates(hs, read, NOW)            # the two not read yet lead the next reading
    assert [h["title"] for h in again[:5]] == [f"Board weighs option {i}" for i in range(5, 10)], again
    assert O.step_candidates(hs, read | {O._key(h) for h in again}, NOW) == []        # nothing new: no request
    # headlines an earlier reading had to pick from but was not given (over the cap) are not new: a re-run on
    # the same news asks nothing; a new headline is read first, then the left-out ones, then the answered
    pool = {O._key(h) for h in O.step_word_headlines(hs, NOW)}
    assert len(pool) == 13 and O.step_candidates(hs, read, NOW, considered=pool) == []
    fresh = hs + [_h("Board weighs a new offer", 0.5, "Blog")]
    nxt = O.step_candidates(fresh, read, NOW, considered=pool)
    assert nxt[0]["title"] == "Board weighs a new offer" and len(nxt) == O.STEP_HEADLINES
    assert all(O._key(h) not in read for h in nxt[:6]) and O._key(nxt[-1]) in read, nxt
    assert O.considered_keys([{"run": 1, "data": {"considered": ["x", "y"]}}, {"run": 2, "data": {}}]) == {"x", "y"}


def test_step_verdicts():
    a, b, c, d = (O._bare(_h(t, 5)) for t in ("Board weighs A", "Board weighs B", "Board denies C", "Board weighs D"))

    def rd(sha, run, given, steps, denials=(), at=1):
        return {"headlines_sha1": sha, "run": run, "created_at": at,
                "data": {"given": given, "answered": [O._key(h) for h in given], "steps": list(steps),
                         "denials": list(denials)}}

    e = O._bare(_h("Board weighs E", 6))
    rs = [rd("s1", 1, [a, b, c, e], [a, b], [c]), rd("s1", 2, [e, c, b, a], [a, e], [c]),
          rd("s2", 1, [d, a], [d, a], at=2)]                 # a again: its verdict was set by s1
    v = O.step_verdicts(rs)
    assert [h["title"] for h in v["both"]] == ["Board weighs A"], v
    # one reading only: B (the second did not mark it), E (only the second did), D (no second reading yet)
    assert sorted(h["title"] for h in v["one"]) == ["Board weighs B", "Board weighs D", "Board weighs E"]
    assert [h["title"] for h in v["denials"]] == ["Board denies C"]
    assert O.read_keys(rs, 1) == {O._key(x) for x in (a, b, c, d, e)}
    assert O.read_keys(rs, 2) == {O._key(x) for x in (a, b, c, e)}


def test_story_headlines_filter():
    raw = GIVEN + [
        {"title": "Lutnick exit odds jump on Polymarket", "source": "Yahoo", "published": "2026-11-12T11:00:00Z", "url": "o1"},
        {"title": "Lutnick out at 35.5%? Traders weigh in", "source": "CoinDesk", "published": "2026-11-12T11:00:00Z",
         "url": "o2"},
        {"title": "Lutnick weighs his options", "source": "Kalshi News", "published": "2026-11-12T11:00:00Z", "url": "o3"},
        {"title": "Lutnick talks tariffs in Detroit", "source": "AP News", "published": "2026-10-20T11:00:00Z", "url": "old"},
        {"title": "Lutnick plans trip to Asia", "source": "Local Paper", "published": "2026-11-13T07:00:00Z",
         "url": "date-only"},                                       # date-only today: counts from 23:59, not yet
        {"title": "White House discussing Howard Lutnick's future as Commerce secretary, people familiar say",
         "source": "Some Blog", "published": "2026-11-12T17:00:00Z", "url": "copy"},
        {"title": "Tariffs rise again", "source": "Reuters", "published": "2026-11-12T11:00:00Z", "url": "off"},
    ]
    hs = O.story_headlines(raw, LUT, ["Howard Lutnick"], NOW)
    urls = [h["url"] for h in hs]
    assert urls == ["u2", "u1", "u4"], urls          # newest first; odds, betting source, old, off-topic, not-yet out
    assert next(h for h in hs if h["url"] == "u1")["source"] == "Reuters"     # the agency copy wins over the blog
    # without names a word of the query decides what is on topic
    assert [h["url"] for h in O.story_headlines(raw, None, ["Tariffs"], NOW)] == ["off"]


def _h(title, hours_ago, source="Wire"):
    t = NOW - int(hours_ago * 3600)
    return {"title": title, "source": source, "published": f"{O._day(t)}T{(t % DAY) // 3600:02d}:{(t % 3600) // 60:02d}:00Z",
            "url": title, "ts": t}


def test_threads():
    shared = _h("Nvidia scandal widens", 5)
    st = {"lutnick": {"headlines": [shared, _h("Lutnick a", 2)]}, "nvidia ceo": {"headlines": [shared]},
          "huang": {"headlines": [_h("Huang b", 3), _h("Old shared", 24 * 9)]}, "x": {"headlines": [_h("Old shared", 24 * 9)]}}
    th = O.threads(st, NOW)
    assert th["lutnick"] == th["nvidia ceo"] and th["huang"] != th["lutnick"] and th["x"] != th["huang"]


def test_readings_and_person_steps():
    a, b = _h("Board weighs A", 5), _h("Plain report B", 6)
    rs = [{"headlines_sha1": "s1", "run": 1, "data": {"given": [a, b], "out": None}},
          {"headlines_sha1": "s1", "run": 2, "data": {"given": [a], "out": None}}]
    r = O.Run(store.connect(":memory:"), NOW)
    assert r.covered(rs, 1, [a, b]) and not r.covered(rs, 2, [a, b]) and r.covered(rs, 2, [a])
    # a headline published before the last reading but given only now is new: by its words, not its time
    late = _h("Board weighs C", 30)
    assert not r.covered(rs, 1, [a, b, late])
    rv = {O._key(a): {"decision": O.STEP, "headline": O._bare(a)},
          O._key(late): {"decision": O.NOT_STEP, "headline": O._bare(late)},
          "card": {"decision": O.NOT_STEP, "headline": None}}
    assert [h["title"] for h in O.person_steps(rv)] == ["Board weighs A"]
    assert O.rejected_keys(rv) == {O._key(late)}                   # a rejected dated card is not a headline
    held = [{"headlines_sha1": "x", "run": 1, "data": {"out": O._bare(_h("It happened", 10)), "out_status": "unclear"}}]
    assert O.is_held(held, NOW) and not O.is_held(rs, NOW)
    old = [{"headlines_sha1": "x", "run": 1, "data": {"out": O._bare(_h("It happened", 24 * 8)), "out_status": "out"}}]
    assert not O.is_held(old, NOW)
    # offered to a person in the card's order: agencies first, a leak first within a rank, then the newest
    hs = [_h("Board weighs it", 2, "Blog"), _h("Board weighs it, sources say", 9, "Blog"), _h("Board weighs plan", 20, "Reuters")]
    assert [h["title"] for h in O.offer_order(hs)] == ["Board weighs plan", "Board weighs it, sources say", "Board weighs it"]


def test_already_out_reads_every_headline():
    # a report that the event happened seldom has a step word: the question reads every on-topic headline
    hs = [_h(f"Board weighs option {i}", i) for i in range(1, 16)] + [_h("Ruritania strikes Freedonia's capital", 15.5)]
    hs += [_h(f"Plain report {i}", 16 + i) for i in range(40)] + [_h("Too old report", 24 * 8)]
    hs.sort(key=lambda h: -h["ts"])
    got = [h["title"] for h in O.read_headlines(hs, NOW)]
    assert len(got) == O.OUT_HEADLINES and got[15] == "Ruritania strikes Freedonia's capital"
    assert "Too old report" not in got
    p = O.out_prompt([{"id": "s0", "question": "Q?", "settles": "Ruritania carries out a strike on Freedonia",
                       "rules": "", "headlines": [O._bare(h) for h in hs[:2]]}])
    assert p.startswith(O.OUT_PROMPT) and "steps" not in p.split("Markets:")[1] and "[2]" in p
    given = [O._bare(h) for h in hs[:20]]
    d = O.check_out({"already_out": "Ruritania strikes Freedonia's capital", "already_out_status": "out"}, given,
                    {"subject": "Ruritania"})
    assert d["out"]["title"] == "Ruritania strikes Freedonia's capital" and d["out_status"] == "out"
    d = O.check_out({"already_out": "Ruritania attacked", "already_out_status": "out"}, given, {"subject": "Ruritania"})
    assert d["out"] is None and d["unmatched"] == "out"                       # counted as none, shown to the person


def test_outcome_shapes_split_and_whole():
    pardon = event(8, "Who will Trump pardon before 2027?",
                   [market(81, "Will Trump pardon Jane Roe before 2027?", groupItemTitle="Jane Roe"),
                    market(82, "Will Trump pardon John Doe before 2027?", groupItemTitle="John Doe")])
    rules = "This market resolves to the actor announced as the next James Bond by December 31, 2026, 11:59 PM ET."
    bond = event(9, "Next James Bond actor?",
                 [market(91, "Joe Alwyn announced as next James Bond?", end="2026-12-31T00:00:00Z", description=rules,
                         groupItemTitle="Joe Alwyn"),
                  market(92, "Paul Mescal announced as next James Bond?", end="2026-12-31T00:00:00Z", description=rules,
                         groupItemTitle="Paul Mescal")], negRisk=True, description=rules)
    ladder = event(10, "Acme CEO out by...?", [market(101, "Acme CEO out by November 30?", groupItemTitle="November 30"),
                                               market(102, "Acme CEO out by December 31?", groupItemTitle="December 31")])
    assert [O.outcome_shape(e) for e in (pardon, bond, ladder)] == ["several", "one of", "one"]
    parts = O.split_event(O.pool_event(pardon, pardon["markets"]))
    assert [p["id"] for p in parts] == ["8/81", "8/82"] and parts[0]["title"] == "Will Trump pardon Jane Roe before 2027?"
    assert O.story_idents({"events": parts[:1]}) == ([], ["m81"])        # another outcome closing is not this card's
    be = O.pool_event(bond, bond["markets"])
    assert O.split_event(be) == [be] and O.name_questions(be) == []      # never one outcome's question
    assert be["markets"][0]["until"] == "2026-12-31"                     # the rules' date, not the day before
    story = {"events": [be], "head": be, "kind": "casting", "names": {"post": "James Bond", "decider": "the studio"},
             "place": "any_day"}
    pm = O.polymarket_block(story, NOW)
    assert pm["question"] == "Next James Bond actor?" and pm["until"] == "2026-12-31" and pm["other_dates"] == []
    r = O.Run(store.connect(":memory:"), NOW)
    r.stories = {"bond": dict(story, key="bond")}
    item = r.item(r.stories["bond"], [], "s0")
    assert item["question"] == "Next James Bond actor?" and "Joe Alwyn" not in item["rules"]
    # a rung closing is not a new text: the names stay
    both, one = O.pool_event(ladder, ladder["markets"]), O.pool_event(ladder, ladder["markets"][1:])
    assert O.text_sha1(both) == O.text_sha1(one)
    assert O.same_deadlines(["2026-12-30", "2026-12-31", "2027-01-31"]) == {
        "2026-12-30": "2026-12-31", "2026-12-31": "2026-12-31", "2027-01-31": "2027-01-31"}


def test_who_knows_names_are_people():
    assert not O.person_ok("the Eurovision Asia Grand Final", "Eurovision Asia 2026: Jury Winner")
    assert not O.person_ok("the GRAMMY Awards", "Grammys 2027: Best New Artist Winner")
    for name in ("the Turner Prize jury", "the President", "Nvidia's board", "the White House", "Trump"):
        assert O.person_ok(name, "Some market"), name
    assert O.person_ok("Elon Musk", "Will Elon Musk leave Tesla?")
    assert not O.person_ok("Elon Musk", "Tesla and SpaceX merger announced")     # not the market's words
    v = O.verify_people("merger_acquisition", {"subject": "Tesla and SpaceX", "decider": "Elon Musk",
                                               "others": ["the bankers", "Anthropic"]}, "Tesla and SpaceX merger")
    assert v["decider"] == "Tesla and SpaceX" and v["others"] == ["the bankers"]     # the subject stands in
    ans = {"kind": "charge_or_arrest", "subject": "Adam Schiff, Anthony Fauci and Barack Obama", "short": "", "aliases": [],
           "decider": "", "others": [], "other": "", "post": "", "what": "", "which": "", "question": "",
           "search_names": "Schiff", "search_event": "Schiff charged"}
    text = "Who will be arrested before 2027? || Adam Schiff, Anthony Fauci and Barack Obama"
    assert O.check_names("charge_or_arrest", ans, text) == (None, None)
    ans["subject"] = "Adam Schiff and Anthony Fauci and Barack Obama"
    assert O.check_names("charge_or_arrest", ans, text) == (None, None)


# ── the five places on the proposal's replay (final/f03_replay.py) ───────────

# name, group, decider, thread, created, deadline, resolved, steps [(time, source, title)], held [(time, source, title)];
# the steps and "already out" headlines f03 graded by hand. Times "T07:00" / "T08:00" are date-only reports.
REPLAY = [
    ("Israel strikes Iran (the ceasefire ends)", 1, "Israel", "iran_war", "2026-07-16", "2026-12-31", None, [
        ("2026-10-22T07:00", "ABC News", "US and Israel drawing up plans to resume attacks on Iran post-election, sources say"),
        ("2026-10-26T07:00", "The Jerusalem Post", "US ready to strike Iran if Donald Trump gives order as Israel pushes for joint attack"),
        ("2026-11-11T20:28", "Anadolu Agency", "Israeli security cabinet orders military to prepare for possible attack on Iran: Report"),
        ("2026-11-12T14:22", "Bloomberg", "Netanyahu and Eisenkot Clash Over Whether Israel Should Strike Iran Again"),
        ("2026-11-12T20:46", "National Security Journal", "Israel Is Preparing to Strike Iran Again ...")],
     [("2026-11-12T19:29", "The Times of Israel", "Tehran says city in southern Iran hit by 'an enemy strike'")]),
    ("Next White House press secretary", 1, "Trump", "white_house", "2026-10-16", "2026-12-01", None, [
        ("2026-11-10T19:17", "Fox News", "Trump closing in on new press secretary as administration officials push for pick who could break the mold"),
        ("2026-11-10T20:30", "Yahoo", "Trump Offers UFC Boss Dana White White House Press Secretary Job"),
        ("2026-11-11T19:03", "NJ.com", "Trump apparently narrows down White House press secretary search")], []),
    ("Variational token launch", 1, "Variational's founders", "variational", "2025-11-05", "2026-12-01", None, [
        ("2026-11-11T15:41", "Crypto Briefing", "Variational launches on mainnet ahead of $VAR token event"),
        ("2026-11-11T17:16", "The Defiant", "Variational Ends Omni Beta, Removes Invite-Code Gate")], []),
    ("Lutnick out at Commerce", 1, "Trump", "nvidia", "2026-03-30", "2026-12-31", None, [
        ("2026-11-12T18:19", "Reuters", "White House discussing Howard Lutnick's future as Commerce secretary, people familiar say"),
        ("2026-11-12T21:36", "The Telegraph", "Trump 'considering sacking Lutnick' in wake of Nvidia scandal"),
        ("2026-11-13T00:44", "WSJ", "Lutnick Fights for His Job as Trump Aides Blame Him for Nvidia Fallout")], []),
    ("New Nvidia CEO named", 1, "Nvidia's board", "nvidia", "2026-11-10", "2026-12-31", None, [
        ("2026-11-11T23:48", "bloomingbit", "Nvidia Seeks CEO Successor, Weighs Interim Chief as Board Prepares to Oust Jensen Huang"),
        ("2026-11-12T10:41", "Storyboard18", "Nvidia board explores CEO succession amid reports of Jensen Huang's alleged affairs"),
        ("2026-11-12T16:48", "Bloomberg", "Nvidia Drama Triggers Difficult Search for Huang's Successor"),
        ("2026-11-12T23:12", "Crypto Briefing", "Nvidia evaluates CEO succession, considers internal and external candidates")], []),
    ("Jensen Huang out as Nvidia CEO", 1, "Nvidia's board", "nvidia", "2026-10-01", "2026-12-31", "2026-11-11T23:12", [
        ("2026-11-08T20:01", "The New York Times", "Nvidia Opens Investigation Into Jensen Huang's Relationship With Suspected Chinese Spy")], []),
    ("Tesla-SpaceX merger announced", 1, "Musk", "musk", "2026-01-30", "2026-12-31", None, [
        ("2026-10-22T07:00", "Yahoo Finance", "Tesla-SpaceX Merger Could Be Announced in 2027"),
        ("2026-10-31T09:05", "Crypto Briefing", "SpaceX and Tesla form committees to evaluate potential merger"),
        ("2026-11-12T20:22", "Bloomberg", "SpaceX and Tesla Talks on Record Merger Are Said to Progress")], []),
    ("Next James Bond named", 1, "Amazon MGM", "bond", "2026-05-18", "2026-12-31", None, [
        ("2026-10-22T07:00", "The Guardian", "James Bond finalists to screen-test for role this week - reports"),
        ("2026-11-07T02:10", "The Hollywood Reporter", "James Bond Surprise: Robert Pattinson Screen Tested to Play 007"),
        ("2026-11-09T07:55", "Riviera Radio", "Robert Pattinson joins James Bond shortlist as search for next 007 nears its end"),
        ("2026-11-09T13:30", "Culted", "Robert Pattinson has reportedly screen tested to become the next James Bond"),
        ("2026-11-12T04:40", "Deadline", "Denis Villeneuve Shares Update On James Bond Casting: 'I Am Pushing The Gas'")], []),
    ("Stripe buys PayPal", 1, "Stripe", "paypal", "2026-02-24", "2026-12-31", None, [
        ("2026-10-23T07:00", "Startup Fortune", "Stripe and Advent Return to the Table With a Richer Bid for PayPal"),
        ("2026-11-12T02:47", "scanx.trade", "Stripe, Advent negotiate higher PayPal bid after $53 billion offer rejected")], []),
    ("US diesel export ban", 1, "Trump", "diesel", "2026-09-21", "2026-12-31", None, [
        ("2026-11-12T21:58", "EnergyNow", "Trump Says He is Still Considering Diesel Export Ban"),
        ("2026-11-12T22:00", "EnergyNow", "Trump Says He Backs the Idea of a Ban on Diesel Exports")], []),
    ("Syrian troops enter Yemen", 1, "Syria", "yemen", "2026-10-08", "2026-12-31", None, [
        ("2026-10-29T08:22", "South24", "Reports Point to Possible Syrian Troop Deployment to Yemen; Official Denies Saudi Request"),
        ("2026-11-04T08:00", "The New York Times", "Syria Weighs Joining Saudi Mutual Defense Pact as Yemen War Rages"),
        ("2026-11-07T16:38", "The Straits Times", "Thousands of Syrian troops to be deployed to Yemen, Saudi Arabia: Sources"),
        ("2026-11-10T16:43", "The New York Times", "Syria Weighs Sending Troops to Aid Saudi War With Houthis")],
     [("2026-11-07T13:34", "Kurdistan24", "Thousands Of Syrian Troops Deploy to Saudi Arabia and Yemen"),
      ("2026-11-12T05:10", "Palestine Chronicle", "Why Syria Chose to Enter the Yemen War instead of Fighting Israel")]),
    ("Foreign troops enter Gaza", 1, "the US", "gaza", "2025-11-05", "2026-12-31", None, [
        ("2026-10-23T07:00", "RTSH", "Albania Signs Agreement to Join International Stabilization Force in Gaza"),
        ("2026-10-26T07:00", "The Times of Israel", "Albania formalizes Gaza stabilization force role, as Kosovo tells ToI its troops are ready to deploy"),
        ("2026-11-09T10:31", "The Jerusalem Post", "US prepares to deploy 250 foreign troops in Gaza as Israel opposes Turkish involvement"),
        ("2026-11-10T10:22", "Yabiladi", "Israel approves first 250 troops for Gaza force Morocco has pledged to join")], []),
    ("Rubio out as Secretary of State", 1, "Rubio", "rubio", "2026-11-05", "2026-12-01", None, [
        ("2026-11-06T15:28", "Democracy Now!", "Marco Rubio Reportedly Considers Resigning as Secretary of State Ahead of Presidential Run")], []),
    ("Rubio announces a presidential run", 2, "Rubio", "rubio", "2026-11-05", "2027-03-31", None, [
        ("2026-11-06T15:28", "Democracy Now!", "Marco Rubio Reportedly Considers Resigning as Secretary of State Ahead of Presidential Run")], []),
    ("James Comey arrested", 2, "the Justice Department", "comey", "2026-04-29", "2026-12-31", None, [
        ("2026-10-30T22:13", "Crypto Briefing", "DOJ seeks grand jury indictment against former FBI Director Comey")], []),
    ("Russia-Ukraine energy ceasefire", 1, "Russia and Ukraine", "ukraine", "2026-11-11", "2026-11-15", None, [
        ("2026-11-10T18:06", "CBC", "Russia, Ukraine engage in 4-hour session on first day of U.S.-mediated talks in Abu Dhabi"),
        ("2026-11-11T09:19", "Euronews", "Ukraine-Russia peace talks enter second day with territory in focus")], []),
    ("Iran leaves the nuclear treaty", 2, "Iran", "iran_war", "2025-11-05", "2026-12-31", None, [
        ("2026-11-12T12:14", "AzerNews", "Iran considers leaving nuclear non-proliferation treaty")], []),
    ("US-Canada tariff deal", 1, "Trump", "canada", "2026-08-18", "2026-12-31", None, [
        ("2026-11-12T22:38", "EnergyNow", "Trump Pauses 50% Tariffs on Canada as Trade Talks Advance")], []),
]
REPLAY_DATED = [("Eurovision Asia jury winner", "2026-11-03", "2026-11-14"), ("CMA New Artist of the Year", "2026-09-17", "2026-11-18"),
                ("Will Jason or Lucia die in GTA VI?", "2026-11-02", "2026-11-19"), ("Who is on GTA VI: The Album?", "2026-09-17", "2026-11-19")]

# final/replay_final.json: the cards of each list (G = gone quiet, S = stays, D = dated, else a new step)
EXPECTED = {
    "2026-11-06": ["Syrian troops enter Yemen", "G:Tesla-SpaceX merger announced"],
    "2026-11-07": ["S:Syrian troops enter Yemen", "Rubio out as Secretary of State", "Next James Bond named",
                   "G:Tesla-SpaceX merger announced"],
    "2026-11-08": ["S:Rubio out as Secretary of State", "S:Next James Bond named", "G:Tesla-SpaceX merger announced"],
    "2026-11-09": ["S:Rubio out as Secretary of State", "S:Next James Bond named", "Jensen Huang out as Nvidia CEO",
                   "G:Tesla-SpaceX merger announced"],
    "2026-11-10": ["S:Jensen Huang out as Nvidia CEO", "Next James Bond named", "Foreign troops enter Gaza",
                   "G:Tesla-SpaceX merger announced"],
    "2026-11-11": ["S:Jensen Huang out as Nvidia CEO", "S:Foreign troops enter Gaza", "Next James Bond named",
                   "G:Tesla-SpaceX merger announced", "D:Eurovision Asia jury winner"],
    "2026-11-12": ["S:Foreign troops enter Gaza", "Israel strikes Iran (the ceasefire ends)", "Next James Bond named",
                   "G:Tesla-SpaceX merger announced", "D:Eurovision Asia jury winner"],
    "2026-11-13": ["Lutnick out at Commerce", "Tesla-SpaceX merger announced", "New Nvidia CEO named",
                   "G:Rubio out as Secretary of State", "D:Eurovision Asia jury winner"],
}
EXPECTED_HELD = {"2026-11-06": [], "2026-11-07": [], "2026-11-12": ["Syrian troops enter Yemen"],
                 "2026-11-13": ["Israel strikes Iran (the ceasefire ends)", "Syrian troops enter Yemen"]}
# final/replay_faster.json, Nov 11: without the stay, Huang's market drops the day he resigned
FASTER_NOV11 = ["Next James Bond named", "Next White House press secretary", "Russia-Ukraine energy ceasefire",
                "G:Tesla-SpaceX merger announced", "D:Eurovision Asia jury winner"]
PLACE_MARK = {"gone quiet": "G:", "stays": "S:", "dated": "D:", "new step": ""}


def _hd(t, source, title):
    return {"title": title, "source": source, "published": t}


def replay(stay_days=K.STAY_DAYS):
    """The list each day Nov 6-13 at 04:00 UTC from REPLAY, through story_row and select, as f03 builds it."""
    prev, out, held_out = [], {}, {}
    for d in range(6, 14):
        now = to_epoch(f"2026-11-{d:02d}T04:00:00Z")
        rows = []
        for name, group, dec, thread, created, deadline, resolved, steps, held in REPLAY:
            if to_epoch(created + "T00:01:00Z") > now or to_epoch(deadline + "T23:00:00Z") <= now + 12 * 3600:
                continue
            if resolved and to_epoch(resolved) <= now:
                continue
            r = O.story_row(name, group, O.decider_key(dec), thread, deadline, [_hd(*s) for s in steps],
                            [_hd(*h) for h in held], now)
            if r:
                rows.append(r)
        dated = [{"id": n, "reveal": rev, "held": False} for n, created, rev in REPLAY_DATED
                 if to_epoch(created + "T00:01:00Z") <= now]
        picks = O.select(rows, dated, prev, now, stay_days=stay_days)
        day = O._day(now)
        out[day] = [PLACE_MARK[p["place"]] + p["id"] for p in picks]
        held_out[day] = sorted(r["id"] for r in rows if r["held"])
        prev = picks
    return out, held_out


def test_replay_five_places():
    got, held = replay()
    for day, want in EXPECTED.items():
        assert got[day] == want, (day, got[day], want)
    for day, want in EXPECTED_HELD.items():
        assert held[day] == want, (day, held[day])
    # Huang's market entered Nov 9 with the board's investigation and stayed through Nov 11, the day he resigned
    assert "S:Jensen Huang out as Nvidia CEO" in got["2026-11-11"]
    new = [len(set(map(_bare_name, got[b])) - set(map(_bare_name, got[a])))
           for a, b in zip(sorted(got)[:-1], sorted(got)[1:])]
    assert new == [2, 0, 1, 1, 1, 1, 3], new                       # 1.3 new cards a day, as the proposal says


def _bare_name(x):
    return x.split(":", 1)[1] if x[:2] in ("G:", "S:", "D:") else x


def test_replay_faster_drops_huang():
    got, _ = replay(stay_days=0)
    assert got["2026-11-11"] == FASTER_NOV11, got["2026-11-11"]
    assert not any("Huang" in c for c in got["2026-11-11"])


def _row(sid, group=1, dec="trump", thread=None, hours=5, leak=False, steps=1):
    title = ("Officials say board weighs it" if leak else "Board weighs it") + f" {sid}"
    st = [_hd(f"{O._day(NOW - int((hours + 24 * i) * 3600))}T00:30:00Z", "Wire", title + f" {i}") for i in range(steps)]
    return O.story_row(sid, group, dec, thread or sid, "2026-12-31", st, [], NOW)


def test_caps_and_order():
    rows = [_row("t1", hours=2), _row("t2", hours=3), _row("t3", hours=1, leak=True),
            _row("b1", dec="acme's board", thread="acme", hours=4), _row("b2", dec="acme's board", thread="acme", hours=6),
            _row("g2", group=2, dec="court", hours=1, leak=True)]
    picks = O.select(rows, [], [], NOW)
    ids = [p["id"] for p in picks]
    assert ids[0] == "t3"                                     # a leak first within the group
    assert sum(1 for i in ids if i.startswith("t")) == 2       # at most 2 cards per decider
    assert not ("b1" in ids and "b2" in ids)                   # 1 per thread and decider
    assert ids.index("g2") > max(ids.index(i) for i in ids if i != "g2")   # group 2 after group 1
    assert all(p["place"] == "new step" and p["entered"] == "2026-11-13" for p in picks)


def test_short_day_is_not_padded():
    rows = [_row("only", hours=5), None, O.story_row("dead", 1, "x", "x", "2026-12-31",
                                                   [_hd("2026-10-01T10:00:00Z", "W", "Old step")], [], NOW)]
    assert [p["id"] for p in O.select([r for r in rows if r], [], [], NOW)] == ["only"]
    assert O.select([], [], [], NOW) == []


def test_held_back_window():
    st = [_hd("2026-11-12T10:00:00Z", "Wire", "Board weighs it")]
    near = O.story_row("a", 1, "x", "a", "2026-12-31", st, [_hd("2026-11-10T10:00:00Z", "W", "It happened")], NOW)
    far = O.story_row("b", 1, "y", "b", "2026-12-31", st, [_hd("2026-11-01T10:00:00Z", "W", "It happened")], NOW)
    assert near["held"] and not far["held"]
    assert [p["id"] for p in O.select([near, far], [], [], NOW)] == ["b"]
    dated = [{"id": "d", "reveal": "2026-11-15", "held": True}, {"id": "e", "reveal": "2026-11-17", "held": False}]
    assert [p["id"] for p in O.select([], dated, [], NOW)] == []         # the 17th is more than 3 days away
    dated[1]["reveal"] = "2026-11-16"
    assert [p["place"] for p in O.select([], dated, [], NOW)] == ["dated"]


def test_labels_and_deciders():
    prev = [{"id": "a", "listed_since": "2026-11-11"}]
    assert O.label_of("a", prev, ["2026-11-11", "2026-11-12"], "2026-11-13") == ("listed", "2026-11-11")
    assert O.label_of("b", prev, ["2026-11-07", "2026-11-13"], "2026-11-13") == ("back", "2026-11-13")
    assert O.label_of("c", prev, ["2026-11-13"], "2026-11-13") == ("new", "2026-11-13")       # a re-run today
    assert O.decider_key("Donald Trump") == O.decider_key("President Trump") == O.decider_key("Trump") == "trump"
    assert O.decider_key("Nvidia's board") == "nvidia's board" != O.decider_key("Tesla's board")
    assert O.decider_key("the US") == O.decider_key("US") == "us"
    # the President by any name is one decider for the caps
    assert O.decider_key("the President") == O.decider_key("the White House") == O.decider_key("Donald Trump") == "trump"
    assert O.decider_keys("Nvidia's board") == {"nvidia"} and O.decider_keys("the authorities") == {"authorities"}
    assert O.decider_keys("Tesla and SpaceX management") & O.decider_keys("Tesla")


def test_one_piece_of_news_fills_one_card():
    shared = _hd(f"{O._day(NOW - 5 * 3600)}T00:30:00Z", "Bloomberg",
                 "SpaceX and Tesla Talks on Record Merger Are Said to Progress")
    a = O.story_row("merger", 1, O.decider_keys("Tesla and SpaceX management"), "t1", "2026-12-31", [shared], [], NOW)
    b = O.story_row("cybercab", 1, O.decider_keys("Elon Musk"), "t2", "2026-12-31", [shared], [], NOW)
    assert len(O.select([a, b], [], [], NOW)) == 1
    # deciders sharing a name, in one news thread: one card
    c = O.story_row("x", 1, O.decider_keys("Tesla"), "t3", "2026-12-31",
                    [_hd(f"{O._day(NOW - 5 * 3600)}T01:00:00Z", "AP", "Tesla weighs it")], [], NOW)
    d = O.story_row("y", 1, O.decider_keys("Tesla and SpaceX management"), "t3", "2026-12-31",
                    [_hd(f"{O._day(NOW - 5 * 3600)}T02:00:00Z", "AP", "Tesla and SpaceX weigh it")], [], NOW)
    assert len(O.select([c, d], [], [], NOW)) == 1


def test_stays_leave_place_four_and_a_quiet_card_starts_a_new_stay():
    prev = [{"id": f"s{i}", "entered": "2026-11-12", "place": "new step"} for i in range(4)]
    rows = [_row(f"s{i}", dec=f"d{i}", hours=30 + i) for i in range(4)] + [_row("q", dec="dq", hours=24 * 5)]
    places = [(p["id"], p["place"]) for p in O.select(rows, [], prev, NOW)]
    assert ("q", "gone quiet") in places and sum(1 for _, pl in places if pl == "stays") == 4, places
    p = O.select([_row("a", hours=5)], [], [{"id": "a", "entered": None, "place": "gone quiet"}], NOW)[0]
    assert p["place"] == "new step" and p["entered"] == "2026-11-13"


def test_dated_answer_shows_on_its_reveal_day():
    assert O.due_dated("2026-11-13", "2026-11-13") and O.due_dated("2026-11-16", "2026-11-13")
    assert not O.due_dated("2026-11-17", "2026-11-13") and not O.due_dated("2026-11-12", "2026-11-13")
    assert [p["place"] for p in O.select([], [{"id": "d", "reveal": "2026-11-13", "held": False}], [], NOW)] == ["dated"]


# ── whole runs with a fake walk, feed and model ──────────────────────────────

def ts_iso(hours_from_now, base=NOW):
    t = base + int(hours_from_now * 3600)
    return O._iso_z(t)


EVENTS = [
    event(1, "Jane Roe out as Secretary of Widgets by...?",
          [market(11, "Jane Roe out as Secretary of Widgets by December 31?")]),
    event(2, "Acme and Globex merger announced by...?", [market(21, "Acme and Globex merger announced by December 31?")]),
    event(3, "Ruritania x Freedonia ceasefire continues through...?",
          [market(31, "Ruritania x Freedonia ceasefire continues through December 31?")]),
    event(4, "Turner Prize 2026 winner?",
          [market(41, "Will Sam Artist win the Turner Prize 2026?", end="2026-11-16T00:00:00Z",
                  description="The winner of the Turner Prize 2026 will be announced on November 15, 2026. "
                              "This market resolves to the artist named the winner.")]),
    event(5, "Fed decision in December?", [market(51, "Fed decreases interest rates by 25 bps after December 2026 meeting?")]),
    event(6, "Nearly settled?", [market(61, "Will Acme open a store by December 31?", prices=("0.995", "0.005"))]),
]
NAMES = {
    "Jane Roe out as Secretary of Widgets by...?": dict(
        kind="us_official", subject="Jane Roe", short="Roe", decider="the President", post="the Widgets Department",
        search_names="Jane Roe", search_event="Jane Roe resign fired"),
    "Acme and Globex merger announced by...?": dict(
        kind="merger_acquisition", subject="Acme and Globex", decider="Acme's board", search_names="Acme Globex",
        search_event="Acme Globex merger"),
    "Ruritania x Freedonia ceasefire continues through...?": dict(
        kind="planned_strike_or_op", subject="Ruritania or Freedonia", what="a strike on the other",
        aliases=["Ruritanian"], search_names="Ruritania Freedonia", search_event="Ruritania Freedonia strike"),
    "Turner Prize 2026 winner?": dict(
        kind="jury_prize", subject="Turner Prize", what="the Turner Prize 2026", decider="the Turner Prize jury",
        which="Who", search_names="Turner Prize", search_event="Turner Prize shortlist winner"),
}
NEWS = {   # query -> [(hours from Nov 13 04:00, source, title)]
    "Jane Roe": [(-10, "Reuters", "White House weighs Jane Roe's future, officials say"),
                 (-6, "Yahoo", "Jane Roe exit odds rise to 40%"),
                 (-30, "Op-Ed Daily", "Jane Roe should go"),
                 (+20, "AP News", "Jane Roe prepares exit plan, sources say"),          # day 2
                 (+44, "AP News", "Jane Roe resigns as widgets secretary")],             # day 3
    "Jane Roe resign fired": [(-10, "Reuters", "White House weighs Jane Roe's future, officials say")],
    "Acme Globex": [(-19.5, "Bloomberg", "Acme and Globex in merger talks, people familiar say"),
                    (-50, "Trade Blog", "What an Acme-Globex deal could mean")],
    "Acme Globex merger": [(-19.5, "Bloomberg", "Acme and Globex in merger talks, people familiar say")],
    "Ruritania Freedonia": [(-12, "AP News", "Ruritania cabinet orders army to prepare strike on Freedonia"),
                            (-3, "Local Wire", "Freedonia says city hit by 'enemy strike'")],
    "Ruritania Freedonia strike": [(-12, "AP News", "Ruritania cabinet orders army to prepare strike on Freedonia")],
}


def fake_search(query, after=None, before=None, limit=None):
    """The headlines NEWS keeps for the query; a query from a title alone (no names yet) gets every headline
    that has one of its words."""
    found = NEWS.get(query)
    if found is None:
        words = {w.lower() for w in query.split() if len(w) >= 4}
        found = {x for hs in NEWS.values() for x in hs if words & set(re.findall(r"\w+", x[2].lower()))}
    return [{"title": t, "source": s, "published": ts_iso(h), "url": f"https://news/{t}"} for h, s, t in sorted(found)]


STEP = re.compile(r"weighs|talks|prepare", re.I)
OUT = re.compile(r"resigns|hit by", re.I)


def step_lines(prompt):
    """The numbered headlines of a step prompt: [(n, title)]."""
    return [(int(n), t) for n, t in re.findall(r"^\[(\d+)\] [^·]+ · [^·]+ · (.*)$", prompt, re.M)]


class FakeClient:
    """Answers the four prompts the run sends (names, reveal labels, the step reading, "already out") from the
    prompt's own text. The step reading marks a headline with a step word ("weighs", "talks", "prepare") as a
    step, naming its first two words as who acts; mark(n, title) changes that."""

    models = ("fake-lite",)

    def __init__(self, down=False, mark=None, fail_steps=None):
        self.down, self.prompts, self.skip = down, [], set()
        self.mark = mark or (lambda n, t: bool(STEP.search(t)))
        self.fail_steps = fail_steps            # a step prompt containing this text fails (busy)

    def steps_asked(self):
        return [p for p in self.prompts if "Market question:" in p]

    def outs_asked(self):
        return [p for p in self.prompts if p.startswith(O.OUT_PROMPT)]

    def generate(self, prompt, schema=None):
        self.prompts.append(prompt)
        if self.down or (self.fail_steps and "Market question:" in prompt and self.fail_steps in prompt):
            raise ai.GeminiError("fake-lite: HTTP 503: busy", status=503, model="fake-lite")
        if prompt.startswith("You fill in names"):
            out = []
            for mid, title in re.findall(r"### Market id=(\S+)\nEvent: (.*)", prompt):
                n = dict(NAMES[title])
                out.append(dict({s: "" for s in O._NAME_SLOTS}, id=mid, aliases=n.pop("aliases", []), others=[], **n))
            return json.dumps({"names": out}), "fake-lite", {}
        if prompt.startswith("You label prediction markets"):
            out = [{"id": mid, "type": "predecided_reveal", "reveal_kind": "scheduled", "reveal_date": "2026-11-15",
                    "quote": "will be announced on November 15, 2026"} for mid in re.findall(r"### Market id=(\S+)", prompt)]
            return json.dumps({"labels": out}), "fake-lite", {}
        assert prompt.startswith("You read news headlines"), prompt[:80]
        if "Market question:" in prompt:                                    # the step reading
            out = [{"n": n, "who_acts": " ".join(t.split()[:2]), "what": t[:40],
                    "category": "step" if self.mark(n, t) else "other_event"} for n, t in step_lines(prompt)]
            return json.dumps({"headlines": out}), "fake-gemma", {}
        out = []
        for block in prompt.split("### Market id=")[1:]:
            mid = block.split("\n", 1)[0].strip()
            titles = re.findall(r"^\[\d+\] [^·]+ · [^·]+ · (.*)$", block, re.M)
            out_t = next((t for t in titles if OUT.search(t)), None)
            out.append({"id": mid, "already_out": out_t or "none",
                        "already_out_status": ("out" if "resigns" in (out_t or "") else "unclear") if out_t else "none"})
        return json.dumps({"answers": out}), "fake-lite", {}


def _run(conn, now, client, out, search=fake_search):
    return O.daily(conn=conn, now=now, client=client, walk=lambda: EVENTS, search=search, out_path=out,
                   news_cache_path=None, sleep=lambda s: None, workers=1)


CARD_KEYS = {"id", "label", "listed_since", "place", "kind", "group", "question", "timing", "headline", "who_knows",
             "why_watched", "other_side", "polymarket", "headlines", "checked_at", "event_ids", "slugs"}
HEADLINE_KEYS = {"title", "source", "published", "url", "step", "on_card"}
ROE_K, ACME_K, TURNER_K, RUR_K = (O.story_key(t) for t in (
    "Jane Roe out as Secretary of Widgets by...?", "Acme and Globex merger announced by...?",
    "Turner Prize 2026 winner?", "Ruritania x Freedonia ceasefire continues through...?"))


def test_whole_runs():
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        client = FakeClient()
        s1 = _run(conn, NOW, client, out)
        # the step reading confirms the steps: the cards show with nobody's approval
        doc = json.load(open(out))
        assert doc["rules"]["steps_by"] == "model" and doc["rules"]["prompt_version"].startswith(O.PROMPT_VERSION)
        assert s1["status"] == "updated" and s1["steps_by"] == "model" and "no longer wait" in s1["approval"]
        assert s1["counts"]["events in the pool (pass the closed list's rules)"] == 4      # the Fed and the 99c one out
        day = doc["days"][0]
        assert day["day"] == "2026-11-13" and day["status"] == "updated" and day["awaiting"] == 0, day
        assert [c["id"] for c in day["cards"]] == [ROE_K, ACME_K, TURNER_K]
        assert day["new_today"] == 3 and day["held_back"] == 1 and s1["new_today"] == 3
        assert RUR_K in [h["id"] for h in s1["held_back"]]                      # "hit by 'enemy strike'": unclear
        assert [p["steps_from"] for p in s1["picks"][:2]] == [{"model": 1, "person": 0, "card_headline_by": "model"}] * 2
        # one step prompt per any-day story with a step-word headline, then the second reading of each that
        # marked a step, in reverse order; the dated story is not read for steps
        steps = client.steps_asked()
        assert s1["requests"]["steps 1"] == 3 and s1["requests"]["steps 2"] == 3 and len(steps) == 6, s1["requests"]
        assert not any("Turner" in p for p in steps)
        assert s1["step_reading"]["reading 2: stories where it also found a step"] == 3
        roe_p = [p for p in steps if "Jane Roe" in p]
        assert "Who decides whether it happens: the President" in roe_p[0]
        assert [t for _, t in step_lines(roe_p[0])] == ["White House weighs Jane Roe's future, officials say"]
        # no model reads a price, odds or a volume
        for p in client.prompts:
            assert "40%" not in p and "odds rise" not in p and "outcomePrices" not in p and "volume" not in p.lower()
            assert "0.995" not in p and "50000" not in p
        assert s1["requests"].get("reading 2", 0) >= 1 and s1["requests"]["names"] == 1
        roe, acme, turner = day["cards"]
        for c in day["cards"]:
            assert set(c) == CARD_KEYS and c["label"] == "new" and c["listed_since"] == "2026-11-13"
            for h in c["headlines"] + [c["headline"]] * bool(c["headline"]):
                assert set(h) == HEADLINE_KEYS
        assert roe["question"] == "Will Jane Roe leave the Widgets Department?"
        assert roe["who_knows"] == "If Roe goes, the President, Roe and a few aides will know before the public does."
        assert roe["headline"]["title"] == "White House weighs Jane Roe's future, officials say" and roe["headline"]["on_card"]
        assert roe["headline"]["step"]
        assert roe["timing"] == {"kind": "any_day", "until": "2026-12-31", "last_step": None, "reveal": None,
                                 "reveal_note": None}
        assert [h["title"] for h in roe["headlines"]] == ["White House weighs Jane Roe's future, officials say",
                                                         "Jane Roe should go"]          # the odds headline is gone
        assert roe["checked_at"] == "2026-11-13T04:00:00Z" and roe["other_side"] is None
        assert acme["question"] == "Will Acme and Globex announce a merger?" and acme["group"] == 1
        assert turner["place"] == "dated" and turner["headline"] is None and turner["checked_at"] is None
        assert turner["timing"]["reveal"] == "2026-11-15" and turner["question"] == "Who wins the Turner Prize 2026?"
        assert turner["who_knows"].endswith("so the jurors and the vote counters will know it first.")
        assert roe["polymarket"] == {"question": "Jane Roe out as Secretary of Widgets by December 31?", "other_dates": [],
                                     "url": "https://polymarket.com/event/e1"}
        assert store.open_listed_since(conn, "e1") == "2026-11-13" and store.open_listed_since(conn, "m21") == "2026-11-13"
        # the queue a person may override: the shown cards' steps, then the dated card
        assert [(w["n"], w["story"], w["why"]) for w in s1["awaiting"]] == [
            (1, ROE_K, O.SHOWN), (2, ACME_K, O.SHOWN), (3, TURNER_K, O.DATED_SHOWN)], s1["awaiting"]
        text = O.review_text(store.get_open_day(conn, "2026-11-13"))
        assert "1. Will Jane Roe leave the Widgets Department?" in text and "cards need no approval" in text

        # the same day again: every headline was read, nothing is asked again
        again = FakeClient()
        _run(conn, NOW, again, out)
        assert not again.steps_asked() and not again.outs_asked()

        # day 2: a new step for Roe, but the model is down: yesterday's live cards stay, none added
        down = FakeClient(down=True)
        s2 = _run(conn, NOW + DAY, down, out)
        doc = json.load(open(out))
        d2 = doc["days"][0]
        assert d2["status"] == "not updated today" and s2["model_down"], s2["model_down"]
        assert [c["id"] for c in d2["cards"]] == [ROE_K, ACME_K, TURNER_K] and d2["new_today"] == 0
        assert all(c["label"] == "listed" and c["listed_since"] == "2026-11-13" for c in d2["cards"])
        assert len(down.prompts) == O.MODEL_TRIES and [d["day"] for d in doc["days"]] == ["2026-11-14", "2026-11-13"]

        # day 3: the model is back and a headline says Roe resigned: held back; Acme stays; Turner on its reveal day
        day3 = FakeClient()
        s3 = _run(conn, NOW + 2 * DAY, day3, out)
        d3 = json.load(open(out))["days"][0]
        assert d3["status"] == "updated"
        got = {c["id"]: c["place"] for c in d3["cards"]}
        assert ROE_K not in got and got[ACME_K] == "stays" and got[TURNER_K] == "dated", got
        assert ROE_K in [h["id"] for h in s3["held_back"]]
        # the new headline ("Jane Roe prepares exit plan, sources say") was read once, with the new one first
        roe3 = [p for p in day3.steps_asked() if "Jane Roe" in p]
        assert roe3 and step_lines(roe3[0])[0][1] == "Jane Roe prepares exit plan, sources say", roe3[:1]


def plus_search(query, after=None, before=None, limit=None):
    """fake_search with a second Acme step headline."""
    hs = fake_search(query, after, before, limit)
    if query.startswith("Acme Globex"):
        hs.append({"title": "Globex weighs Acme bid, people familiar say", "source": "Bloomberg",
                   "published": ts_iso(-5), "url": "https://news/globex-weighs"})
    return hs


def test_second_reading_must_agree():
    # a model that marks only the headline it is shown first: the second reading (reverse order) disagrees on
    # Acme's two headlines, so neither counts; a person may approve one
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        first_only = FakeClient(mark=lambda n, t: n == 1)
        s = _run(conn, NOW, first_only, out, plus_search)
        acme = [p for p in first_only.steps_asked() if "Acme" in p.split("Numbered")[0]]
        assert len(acme) == 2 and [t for _, t in step_lines(acme[1])] == [t for _, t in reversed(step_lines(acme[0]))]
        assert s["step_reading"]["reading 2: steps of reading 1 it did not mark"] == 1
        day = json.load(open(out))["days"][0]
        assert [c["id"] for c in day["cards"]] == [ROE_K, TURNER_K] and day["awaiting"] == 1, day
        item = next(w for w in s["awaiting"] if w["story"] == ACME_K)
        assert item["why"] == O.ONE_READING and len(item["candidates"]) == 2
        assert "Acme and Globex announce a merger" in O.review_text(store.get_open_day(conn, "2026-11-13"))
        O.decide(conn, [f"{item['n']}.2"], True, now=NOW)
        s2 = _run(conn, NOW, FakeClient(mark=lambda n, t: n == 1), out, plus_search)
        day = json.load(open(out))["days"][0]
        acme_card = next(c for c in day["cards"] if c["id"] == ACME_K)
        assert acme_card["headline"]["title"] == item["candidates"][1]["title"]
        assert next(p for p in s2["picks"] if p["id"] == ACME_K)["steps_from"]["card_headline_by"] == "person"


def test_a_persons_decisions_override_the_reading():
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        s = _run(conn, NOW, FakeClient(), out)
        # a rejected headline never shows: Roe's step taken off, and the Turner card
        n_roe = next(w["n"] for w in s["awaiting"] if w["story"] == ROE_K)
        n_turner = next(w["n"] for w in s["awaiting"] if w["story"] == TURNER_K)
        done = O.decide(conn, [str(n_roe), str(n_turner)], False, now=NOW)
        assert len(done) == 2
        s2 = _run(conn, NOW, FakeClient(), out)
        day = json.load(open(out))["days"][0]
        assert [c["id"] for c in day["cards"]] == [ACME_K], day["cards"]
        assert ROE_K not in [w["story"] for w in s2["awaiting"]] and TURNER_K not in [w["story"] for w in s2["awaiting"]]
        for c in day["cards"]:
            assert "White House weighs Jane Roe's future, officials say" not in [h["title"] for h in c["headlines"]]
        try:
            O.decide(conn, ["7"], True)
            raise AssertionError("an item not in the queue was accepted")
        except ValueError:
            pass
    # an approved headline always counts, even when the reading marks no step (today's approvals stand)
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        never = FakeClient(mark=lambda n, t: False)
        s = _run(conn, NOW, never, out)
        assert [c["id"] for c in json.load(open(out))["days"][0]["cards"]] == [TURNER_K] and not [
            w for w in s["awaiting"] if w["why"] != O.DATED_SHOWN]
        h = {"title": "White House weighs Jane Roe's future, officials say", "source": "Reuters",
             "published": ts_iso(-10), "url": "https://news/White House weighs Jane Roe's future, officials say"}
        store.save_review(conn, ROE_K, O._key(h), O.STEP, h, "person", NOW)
        s = _run(conn, NOW, FakeClient(mark=lambda n, t: False), out)
        cards = json.load(open(out))["days"][0]["cards"]
        assert [c["id"] for c in cards] == [ROE_K, TURNER_K] and cards[0]["headline"]["title"] == h["title"]
        assert s["picks"][0]["steps_from"] == {"model": 0, "person": 1, "card_headline_by": "person"}


def test_one_failed_reading_leaves_its_story_unread():
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        busy_roe = FakeClient(fail_steps="Jane Roe")
        s = _run(conn, NOW, busy_roe, out)
        assert s["status"] == "updated" and not s["model_down"] and s["step_failed"] == [ROE_K], s["step_failed"]
        assert [c["id"] for c in json.load(open(out))["days"][0]["cards"]] == [ACME_K, TURNER_K]
        info = store.get_open_day(conn, "2026-11-13")["info"]
        assert not [x for x in info["seen_step_headlines"] if x.startswith(ROE_K + "\x1f")]
        # the next run reads it (its headlines count as new) and lists it
        s2 = _run(conn, NOW + 3600, FakeClient(), out)
        assert ROE_K in [p["id"] for p in s2["picks"]] and s2["step_reading"]["reading 1: stories read"] == 1, s2["picks"]
    # every step reading failing is the model being down: no new cards
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        s = _run(conn, NOW, FakeClient(fail_steps="Market question:"), out)
        assert s["status"] == "not updated today" and s["model_down"].startswith("step reading"), s["model_down"]
        assert s["picks"] == []


def many_search(query, after=None, before=None, limit=None):
    """fake_search with 12 more Acme step headlines: more than one step reading is given."""
    hs = fake_search(query, after, before, limit)
    if query.startswith("Acme Globex"):
        hs += [{"title": f"Acme and Globex weigh deal option {i}", "source": "Blog", "published": ts_iso(-30 - i),
                "url": f"https://news/acme-{i}"} for i in range(12)]
    return hs


def test_a_rerun_on_the_same_news_asks_nothing():
    # Acme has 14 step-word headlines, more than one reading is given: the left-out ones were in the pool the
    # reading picked from, so a re-run of the day on the same news asks nothing and lists the same cards
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        first = FakeClient()
        _run(conn, NOW, first, out, many_search)
        acme = [p for p in first.steps_asked() if "Acme" in p.split("Numbered")[0]]
        assert len(step_lines(acme[0])) == O.STEP_HEADLINES, acme[:1]
        cards = json.load(open(out))["days"][0]["cards"]
        again = FakeClient()
        _run(conn, NOW + 3600, again, out, many_search)
        assert not again.steps_asked() and not again.outs_asked(), [p[:200] for p in again.steps_asked()]
        assert json.load(open(out))["days"][0]["cards"] == cards


def test_a_down_rerun_keeps_the_days_list():
    # a good list, then a re-run the same day with a new headline while the model is down: the list made
    # earlier today stands (it is not replaced by yesterday's, or by none)
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        _run(conn, NOW, FakeClient(), out)
        day = json.load(open(out))["days"][0]
        s = _run(conn, NOW + 2 * 3600, FakeClient(down=True), out, plus_search)
        assert s["model_down"] and s["kept_earlier_list"] == "2026-11-13T04:00:00Z" and s["status"] == "updated"
        again = json.load(open(out))["days"][0]
        assert again["status"] == "updated" and again["cards"] == day["cards"] and again["run_at"] == day["run_at"]
        assert store.get_open_day(conn, "2026-11-13")["run_at"] == NOW
    # every step reading refused (or answering nothing) is the model being down too: no new cards
    for answer in ("refused", "empty"):
        class Bad(FakeClient):
            def generate(self, prompt, schema=None):
                if "Market question:" in prompt:
                    self.prompts.append(prompt)
                    if answer == "refused":
                        raise ai.GeminiError("fake: HTTP 400: bad", status=400, model="fake", refused=True)
                    return json.dumps({"headlines": []}), "fake", {}
                return super().generate(prompt, schema)
        conn = store.connect(":memory:")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "open.json")
            _run(conn, NOW, FakeClient(), out)
            s = _run(conn, NOW + DAY, Bad(), out, plus_search)
            assert s["status"] == "not updated today" and "all" in s["model_down"], (answer, s["model_down"])
            d2 = json.load(open(out))["days"][0]
            assert d2["day"] == "2026-11-14" and d2["new_today"] == 0 and d2["status"] == "not updated today"


def test_a_persons_rebuild_reads_no_new_step():
    # --approve / --reject rebuild the reviewed day at its own run time without a new step reading: a later
    # step headline is not read, the rejected card goes, the rest stay as they were
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        s = _run(conn, NOW, FakeClient(), out)
        n_acme = next(w["n"] for w in s["awaiting"] if w["story"] == ACME_K)
        O.decide(conn, [str(n_acme)], False, now=NOW)
        reviewed = store.get_open_day(conn, "2026-11-13")
        c = FakeClient()
        s2 = O.daily(conn=conn, now=reviewed["run_at"], client=c, walk=lambda: EVENTS, search=plus_search,
                     out_path=out, news_cache_path=None, sleep=lambda s: None, workers=1, read_steps=False,
                     review_of=reviewed)
        assert not c.steps_asked() and s2["status"] == "updated", s2["step_reading"]
        day = json.load(open(out))["days"][0]
        assert [x["id"] for x in day["cards"]] == [ROE_K, TURNER_K] and day["run_at"] == "2026-11-13T04:00:00Z"
        # a day the model was down for stays "not updated today" when a person rebuilds it
        _run(conn, NOW + DAY, FakeClient(down=True), out)
        stale = store.get_open_day(conn, "2026-11-14")
        c = FakeClient()
        s3 = O.daily(conn=conn, now=stale["run_at"], client=c, walk=lambda: EVENTS, search=fake_search,
                     out_path=out, news_cache_path=None, sleep=lambda s: None, workers=1, read_steps=False,
                     review_of=stale)
        assert s3["status"] == "not updated today" and not c.prompts
        assert [x["id"] for x in json.load(open(out))["days"][0]["cards"]] == [ROE_K, TURNER_K]


def test_steps_wait_for_a_person_when_set():
    # STEPS_BY "person" (final.md's fallback when the bars are not met, as they were not out of sample on
    # 2026-11-14): the readings' steps are only proposed; a card shows once a person approves its step, and a
    # dated card still shows on its own
    assert SHIPPED_STEPS_BY == "person"
    conn = store.connect(":memory:")
    old = O.STEPS_BY
    O.STEPS_BY = "person"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "open.json")
            s = _run(conn, NOW, FakeClient(), out)
            doc = json.load(open(out))
            day = doc["days"][0]
            assert [c["id"] for c in day["cards"]] == [TURNER_K] and doc["rules"]["steps_by"] == "person", day
            waiting = [w for w in s["awaiting"] if w["why"] == O.AWAITING]
            assert [w["story"] for w in waiting] == [ROE_K, ACME_K] and day["awaiting"] == 2, s["awaiting"]
            assert waiting[0]["candidates"][0]["title"] == "White House weighs Jane Roe's future, officials say"
            assert "a card shows once a person approves its step" in O.review_text(store.get_open_day(conn, "2026-11-13"))
            O.decide(conn, [str(waiting[0]["n"])], True, now=NOW)
            c = FakeClient()
            s2 = O.daily(conn=conn, now=NOW, client=c, walk=lambda: EVENTS, search=fake_search, out_path=out,
                         news_cache_path=None, sleep=lambda s: None, workers=1, read_steps=False,
                         review_of=store.get_open_day(conn, "2026-11-13"))
            assert not c.steps_asked()
            assert [x["id"] for x in json.load(open(out))["days"][0]["cards"]] == [ROE_K, TURNER_K]
            assert [w["story"] for w in s2["awaiting"] if w["why"] == O.AWAITING] == [ACME_K]
            # day 2: the readings mark a newer step for Roe's card: it is offered so the card can stay fresh
            s3 = _run(conn, NOW + DAY, FakeClient(), out)
            shown = [w for w in s3["awaiting"] if w["why"] == O.SHOWN]
            assert [w["story"] for w in shown] == [ROE_K], s3["awaiting"]
            assert shown[0]["candidates"][0]["title"] == "Jane Roe prepares exit plan, sources say"
            assert json.load(open(out))["days"][0]["awaiting"] == len([w for w in s3["awaiting"]
                                                                       if w["why"] == O.AWAITING])
    finally:
        O.STEPS_BY = old


def test_naming_down_does_not_cost_the_day():
    class NamesBusy(FakeClient):
        def generate(self, prompt, schema=None):
            if prompt.startswith("You fill in names"):
                self.prompts.append(prompt)
                raise ai.GeminiError("fake-lite: HTTP 503: busy", status=503, model="fake-lite")
            return super().generate(prompt, schema)

    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        busy = NamesBusy()
        s = _run(conn, NOW, busy, out)
        assert s["names_down"] and not s["model_down"] and s["status"] == "updated", s
        assert len([p for p in busy.prompts if p.startswith("You fill in names")]) == O.MODEL_TRIES
        assert s["stories"]["named"] == 0 and s["awaiting"] == [] and s["picks"] == []
        # the same day's re-run names them, reads them and lists them
        s2 = _run(conn, NOW, FakeClient(), out)
        assert not s2["names_down"] and s2["status"] == "updated" and len(s2["picks"]) == 3, s2["picks"]


def test_a_changed_event_is_read_again():
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        _run(conn, NOW, FakeClient(), out)
        again = FakeClient()
        _run(conn, NOW, again, out)                     # the same headlines and event: nothing is read again
        assert not again.steps_asked() and not again.outs_asked()
        key = ROE_K
        f = store.get_filing(conn, "1")
        # a new wording of the post is the same event (a person leaving): its readings stand
        store.save_filing(conn, "1", f["kind"], dict(f["names"], post="the Gadgets Department"), "person", story=key)
        same = FakeClient()
        _run(conn, NOW, same, out)
        assert not same.steps_asked() and not same.outs_asked()
        # a person changed who it is about: the new event is read, steps and "already out"
        moved_names = dict(f["names"], subject="Jane Q. Roe", post="the Gadgets Department")
        store.save_filing(conn, "1", f["kind"], moved_names, "person", story=key)
        moved = FakeClient()
        _run(conn, NOW, moved, out)
        outs = moved.outs_asked()
        assert outs and "Jane Q. Roe leaves the Gadgets Department" in outs[0], outs[:1]
        steps = moved.steps_asked()
        assert steps and "this exact event: Jane Q. Roe leaves the Gadgets Department" in steps[0]
        r = O.Run(conn, NOW)
        r.stories = {key: {"kind": "us_official", "names": moved_names, "events": []}}
        hist = r.history(key)
        assert hist and all(h["data"]["settles"] == "Jane Q. Roe leaves the Gadgets Department" for h in hist)
        sh = r.step_history(key)
        assert sh and all(h["data"]["settles"] == "Jane Q. Roe leaves the Gadgets Department" for h in sh)


def test_dry_run_asks_nothing():
    conn = store.connect(":memory:")

    class NoModel:
        def generate(self, *a, **k):
            raise AssertionError("a dry run asked the model")

    s = O.daily(conn=conn, now=NOW, client=NoModel(), walk=lambda: EVENTS, search=fake_search, out_path=None,
                dry_run=True, news_cache_path=None, sleep=lambda s: None)
    assert s["would_ask"]["names (stories)"] == 4 and s["would_ask"]["reveal dates (markets)"] == 1
    assert store.pool_entries(conn) == {} and store.open_days(conn) == []          # nothing stored
    assert s["shortlist"] and all("queries" in x for x in s["shortlist"])


def test_consistency_files():
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        rep = O.consistency(tmp, conn=conn, now=NOW, client=FakeClient(), walk=lambda: EVENTS, search=fake_search,
                            news_cache_path=None, sleep=lambda s: None, seed=1, workers=1)
        assert rep["both_read"] == 3 and rep["step_agree"] == 3 and rep["step_bar_met"]   # Turner: dated, not read
        assert rep["pair_agree"] == rep["pairs_both_answered"] == 3 and rep["prompt_version"] == O.PROMPT_VERSION
        for f in ("answers.jsonl", "agreement.json", "agreement.txt", "grade_sheet.csv", "grade_key.json",
                  "grades_builder.csv"):
            assert os.path.exists(os.path.join(tmp, f)), f
        with open(os.path.join(tmp, "grade_sheet.csv")) as f:
            rows = list(csv.reader(f))
        assert rows[0][:4] == ["item", "question", "settles", "headline"] and len(rows) == 1 + rep["graded_sample"]
        assert rep["graded_sample"] == 3 and all(r[-2] == "" for r in rows[1:])      # blind: no grade filled in
        assert "reading" not in ",".join(rows[0])
        # a re-run resumes from answers.jsonl: nothing is asked again
        again = FakeClient(down=True)
        rep2 = O.consistency(tmp, conn=conn, now=NOW, client=again, walk=lambda: EVENTS, search=fake_search,
                             news_cache_path=None, sleep=lambda s: None, seed=1, workers=1)
        assert again.prompts == [] and rep2["step_agree"] == rep["step_agree"]
        # the daily cache is untouched by the test (names are kept)
        for v in (O.PROMPT_VERSION, O.OUT_VERSION):
            assert store.open_answer_history(conn, ROE_K, v) == []


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
