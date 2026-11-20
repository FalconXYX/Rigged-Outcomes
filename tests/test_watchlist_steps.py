#!/usr/bin/env python3
"""
Offline tests for the daily run's steps (watchlist.steps) against a fake
step context over the fake API (tests/fake_api.py). No network, no model:
Polymarket, Google News and Gemini are stand-ins.

    python3 tests/test_watchlist_steps.py

Covers every job kind of the contract having its handler; the state saved
with compare-and-swap, merged again onto a fresh copy when another job
saved first, and Later("state_moved") after 3 tries; the rows a step writes
going out as results and folded into a later job's copy; the closed list's
chain (scan, labels, queries, news, judge, publish) giving exactly 3 picks a
Toronto day in site:closed.json, with the news searches stored as rows and
a model step stopping at its deadline; Toronto days across both changes of
clocks; one news search, one step reading and one "already out" reading a
story, with the day's caps and the current cards first; the GTA VI market
dropped from an existing list once the game is out, and never shown again.
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_api  # noqa: E402
from fake_api import FakeApi  # noqa: E402
from backend import contract as C  # noqa: E402
from backend.outcomes import Later  # noqa: E402
from watchlist import ai, blobs, judge, kinds as K, news, opennow as O, state, steps, store, upsets  # noqa: E402
from watchlist.common import local_bounds, local_day, to_epoch  # noqa: E402

# the fake API takes the backend's blob names (site:*, scratch:*) as the Worker does (CONTRACT.blob_names)
fake_api.BLOB_NAME = re.compile("|".join(f"(?:{p})" for p in C.CONTRACT["blob_names"].values()) +
                                r"|^(cache:news|list:(closed|open))$")
fake_api.HISTORY = re.compile(r"^(state|site:(index|closed|open)\.json)$")
fake_api.PREFIX = re.compile(r"^[a-z0-9:._/-]{0,40}$")

_TMP = tempfile.TemporaryDirectory(prefix="ro-steps-")      # every step's folder, removed at exit
NOW = to_epoch("2026-11-19T09:05:00Z")          # 04:05 in Toronto: the daily run's start
TODAY = "2026-11-19"


class FakeWorker:
    """The Worker's results and news_cache rows."""

    def __init__(self):
        self.results, self.news, self.checkpoints = {}, {}, 0


class Ctx:
    """A StepContext stand-in: the claim's job and budget, the clock, the step's time left, checkpoints and reads."""

    def __init__(self, api, worker, kind, run="daily:2026-11-19", left=250.0, now=NOW, budget=None, cursor=None,
                 run_kind="daily", tmp=None, inp=None):
        self.api, self.worker = api, worker
        self.job = {"id": f"{run}|{kind}|0", "run_id": run, "run_kind": run_kind, "kind": kind, "part": "0",
                    "input": inp or {}, "cursor": cursor, "attempt": 0, "laters": 0, "partials": 0,
                    "run_deadline": to_epoch("2026-11-19T14:30:00Z"), "run_meta": {}, "missing": []}
        self.budget = dict({"level": "ok"}, **(budget or C.BUDGET["caps"]["ok"]))
        self._left, self._now, self.tmp = left, now, tmp or tempfile.mkdtemp(prefix="step-", dir=_TMP.name)
        self.meta_sent, self.units_sent, self.cursor = {}, 0, cursor

    def now(self):
        return self._now

    def left(self):
        return self._left() if callable(self._left) else self._left

    def checkpoint(self, cursor=None, results=(), news=()):
        self.worker.checkpoints += 1
        run = self.worker.results.setdefault(self.job["run_id"], {})
        for r in results:
            k, v = (r["key"], r["value"]) if isinstance(r, dict) else r
            run[k] = json.loads(json.dumps(v))
        for n in news:
            k, b = (n["key"], n["body"]) if isinstance(n, dict) else n
            self.worker.news[k] = (self._now, json.loads(json.dumps(b)))
        if cursor is not None:
            self.cursor = cursor

    def results(self, prefix, run_id=None):
        run = self.worker.results.get(run_id or self.job["run_id"], {})
        return iter(sorted((k, v) for k, v in run.items() if k.startswith(prefix)))

    def news_get(self, keys, max_age_s):
        return {k: self.worker.news[k][1] for k in keys
                if k in self.worker.news and self._now - self.worker.news[k][0] <= max_age_s}

    def meta(self, **kv):
        self.meta_sent.update(kv)

    def units(self, n):
        self.units_sent += int(n)

    def partial(self, cursor, results=()):
        return ("partial", cursor)

    def done(self, results=(), stop_run=False):
        return ("done", None)


class Patch:
    """Set attributes for a block: Patch((obj, "name", value), ...)."""

    def __init__(self, *items):
        self.items = items

    def __enter__(self):
        self.saved = [(o, n, getattr(o, n)) for o, n, _ in self.items]
        for o, n, v in self.items:
            setattr(o, n, v)

    def __exit__(self, *exc):
        for o, n, v in reversed(self.saved):
            setattr(o, n, v)


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


# ── the handlers ─────────────────────────────────────────────────────────────

def test_every_job_kind_has_its_handler():
    for kind, row in C.JOB_KINDS.items():
        module, func = C.handler_path(kind)
        if module == "watchlist.steps":
            assert callable(getattr(steps, func)), kind
    assert C.LISTS["closed_per_day"] == upsets.PER_DAY == upsets.AT_LEAST == 3


# ── the state: compare-and-swap, merged again ───────────────────────────────

def _seed_state(api, conn=None):
    conn = conn or store.connect(":memory:")
    files = state.dump_files(conn, by="test", made_at=NOW, site_dir=tempfile.gettempdir())
    blobs.put_json(api, state.STATE, state._doc(files), json.loads(files["manifest.json"]), expect=None)
    return conn


def test_state_saved_again_onto_a_moved_one():
    api, w = FakeApi(), FakeWorker()
    _seed_state(api)
    st = steps.State(Ctx(api, w, "daily.begin")).load()
    merges = []

    def merge(conn):
        merges.append(1)
        if len(merges) == 1:                              # another job saves first, while this one merges
            other = steps.State(Ctx(api, w, "open.walk")).load()
            other.save(lambda c: store.save_filing(c, "other", None, None, "rule"))
        store.save_filing(conn, "mine", None, None, "rule")
        return "merged"
    assert st.save(merge) == "merged" and len(merges) == 2
    fresh = steps.State(Ctx(api, w, "daily.end")).load()
    assert store.get_filing(fresh.conn, "other") and store.get_filing(fresh.conn, "mine")
    # a save that keeps moving waits: Later("state_moved")
    st = steps.State(Ctx(api, w, "daily.begin")).load()

    def always_moved(conn):
        api.fail_next(409, "moved", method="POST", path="/api/job/blobs/state/")
        store.save_filing(conn, f"x{conn.total_changes}", None, None, "rule")
    e = raises(lambda: st.save(always_moved), Later)
    assert e.reason == "state_moved" and e.run_after == NOW + C.RETRY["state_moved_s"]
    # nothing changed: nothing is saved
    puts = api.calls["POST"]
    st = steps.State(Ctx(api, w, "daily.begin")).load()
    st.save(lambda conn: None)
    assert api.calls["POST"] == puts


def test_rows_go_out_as_results_and_fold_into_a_later_copy():
    conn = store.connect(":memory:")
    seen = steps.Touched(conn)
    store.save_filing(conn, "e1", "casting", {"subject": "Bond"}, "rule", story="bond")
    store.save_open_answer(conn, "bond", "sha", O.OUT_VERSION, 1, {"out": None, "answered": False}, "m", NOW)
    rows = steps.row_results("open.name", seen.take())
    assert [v["table"] for _, v in rows] == ["open_filings", "open_answers"] and not seen.take()
    assert all(k.startswith("open.name:") and len(k) <= C.LIMITS["result_key_chars"] for k, _ in rows)
    api, w = FakeApi(), FakeWorker()
    ctx = Ctx(api, w, "open.name")
    ctx.checkpoint(results=rows)
    other = store.connect(":memory:")
    assert steps.fold(other, ctx, ("open.name:",)) == 2 and steps.fold(other, ctx, ("open.name:",)) == 2
    assert store.get_filing(other, "e1")["names"] == {"subject": "Bond"}
    assert store.open_answer_history(other, "bond", O.OUT_VERSION)[-1]["data"] == {"out": None, "answered": False}


# ── the closed list in steps ─────────────────────────────────────────────────

def _closed_market(i, day_ts, vol):
    return {"id": f"m{i}", "conditionId": f"0x{i:02d}" + "ab" * 31, "question": f"Will Project{i} Zeta{i} launch by December 31?",
            "slug": f"m{i}", "endDate": "2026-12-31T00:00:00Z", "closedTime": day_ts, "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]', "umaResolutionStatus": "resolved", "volumeNum": vol, "description": "rules",
            "events": [{"id": f"e{i}", "slug": f"ev{i}", "title": f"Project{i} Zeta{i} launch"}],
            "image": "x" * 500}


def _fake_labels(batch, conn=None, client=None, workers=1, today=None, **kw):
    for m in batch:
        it = ai._item(m)
        answer = {"id": it["id"], "type": "company_product", "reveal_kind": "any_day", "reveal_date": "", "quote": ""}
        ai._save(conn, it, answer, "fake", ai._label(it, answer, "fake", False, NOW, today))
    return {str(m["id"]): {} for m in batch}


def _fake_ask(client, prompt, items, schema, key):
    return ({x["id"]: {"id": x["id"], "what_happened": "launched", "public_at": "", "surprise": "yes",
                       "who_knew_first": "the team", "public_signals": "none", "verdict": "potential case",
                       "reason": "a surprise"} for x in items}, "fake")


def test_the_closed_list_in_steps_is_three_a_toronto_day():
    api, w = FakeApi(), FakeWorker()
    _seed_state(api)
    # 5 markets closed on Nov 18 in Toronto (one at 23:30 EST, which is Nov 19 in UTC), 4 on Nov 19
    closed = [_closed_market(i, "2026-11-18T20:00:00Z", 50_000 + i) for i in range(4)]
    closed += [_closed_market(4, "2026-11-19T04:30:00Z", 60_000)]
    closed += [_closed_market(10 + i, "2026-11-19T07:00:00Z", 70_000 + i) for i in range(4)]
    searched = []

    def search(q, after=None, before=None, limit=15):
        searched.append(q)
        return [{"title": f"{q} launched", "source": "Wire", "published": "2026-11-18T12:00:00Z", "url": "u"}]
    from watchlist import net
    with Patch((net, "closed_markets", lambda since, until=None, min_volume=0: list(closed)),
               (ai, "label_markets", _fake_labels), (judge, "_ask", _fake_ask),
               (judge, "write_queries", lambda ms, client, budget=None: {judge.short_id(m): m["question"][:30]
                                                                         for m in ms}),
               (news, "search", search)):
        ctx = Ctx(api, w, "daily.begin")
        ctx.worker.results["daily:2026-11-19"] = {"daily:work": {"now": NOW, "work": {"watched": [], "requests": [],
                                                                                    "added": []}}}
        steps.closed_scan(Ctx(api, w, "closed.scan"))
        scan = w.results["daily:2026-11-19"]["closed:candidates"]
        assert scan["days"] == ["2026-11-18", "2026-11-19"] and scan["markets"] == 9
        doc = steps.get_scratch(Ctx(api, w, "closed.label"), scan["ref"])
        assert len(doc["days"]["2026-11-18"]["markets"]) == 5 and "image" not in doc["days"]["2026-11-18"]["markets"][0]
        steps.closed_label(Ctx(api, w, "closed.label"))
        labels = [k for k in w.results["daily:2026-11-19"] if k.startswith("closed.label:ai_labels:")]
        assert len(labels) == 9
        steps.closed_query(Ctx(api, w, "closed.query"))
        # the news step stops at its deadline and goes on where it stopped
        clock = iter([300] * 6 + [10] * 100)
        got = steps.closed_news(Ctx(api, w, "closed.news", left=lambda: next(clock)))
        assert got is not None and got[0] == "partial" and 0 < len(searched) < 9, (got, len(searched))
        steps.closed_news(Ctx(api, w, "closed.news"))
        assert len(searched) == 9 and len(w.news) == 9                     # each search once, stored as a row
        steps.closed_news(Ctx(api, w, "closed.news"))
        assert len(searched) == 9                                          # nothing searched twice
        # a model step with no time for one more request stops
        assert steps.closed_judge(Ctx(api, w, "closed.judge", left=60))[0] == "partial"
        steps.closed_judge(Ctx(api, w, "closed.judge"))
        steps.closed_publish(Ctx(api, w, "closed.publish"))
    h, pub = blobs.get_json(api, "site:closed.json")
    days = {d["day"]: d for d in pub["days"]}
    assert set(days) == {"2026-11-18", "2026-11-19"}
    assert [len(days[d]["picks"]) for d in ("2026-11-18", "2026-11-19")] == [3, 3]       # exactly 3, never more
    assert days["2026-11-18"]["checked"] == 5 and days["2026-11-18"]["counted_at"] == "2026-11-19T04:05:00-05:00"
    assert pub["generated_at"].endswith("-05:00") and pub["rules"]["per_day"] == 3
    fresh = steps.State(Ctx(api, w, "daily.end")).load()
    assert store.get_closed_check(fresh.conn, closed[0]["conditionId"], judge.PROMPT_VERSION)["verdict"] == \
        "potential case"


def test_a_spent_quota_waits_only_when_it_resets_before_the_deadline():
    api, w = FakeApi(), FakeWorker()
    ctx = Ctx(api, w, "closed.judge")
    e = ai.GeminiError("m: HTTP 429: quota per day", status=429)
    assert steps.model_later(ctx, e) is None                   # resets after the run's 09:30 deadline: go on
    ctx.job["run_deadline"] = NOW + 2 * 86400
    later = steps.model_later(ctx, e)
    assert later.reason == "model_quota" and later.run_after == C.next_quota_reset(NOW) == later.pause_lane_until
    busy = steps.model_later(ctx, ai.GeminiError("m: HTTP 503: overloaded", status=503))
    assert busy.reason == "model_busy" and NOW + 300 <= busy.run_after <= NOW + 600
    assert steps.model_later(ctx, ai.GeminiError("refused", status=403)) is None


# ── Toronto days ─────────────────────────────────────────────────────────────

def test_toronto_days_across_the_changes_of_clocks():
    start, end = local_bounds("2026-11-01")                    # clocks go back: a 25-hour day
    assert end - start == 25 * 3600 and local_day(start) == "2026-11-01" and local_day(end) == "2026-11-02"
    start, end = local_bounds("2026-03-08")                    # clocks go forward: 23 hours
    assert end - start == 23 * 3600
    # 00:30 EDT and 23:30 EST on Nov 1 are both Nov 1 (the first is Nov 1, the second Nov 2 in UTC)
    ms = [{"closedTime": "2026-11-01T04:30:00Z"}, {"closedTime": "2026-11-02T04:30:00Z"}]
    assert list(upsets.by_day(ms)) == ["2026-11-01"] and len(upsets.by_day(ms)["2026-11-01"]) == 2
    # a day counted at 23:30 EST on the 25-hour day is still partial; one counted the next morning is not
    assert upsets.partial({"day": "2026-11-01", "counted_at": "2026-11-02T04:30:00Z"})
    assert not upsets.partial({"day": "2026-11-01", "counted_at": "2026-11-02T09:00:00Z"})
    assert C.toronto_at("2026-11-19", "04:00") == to_epoch("2026-11-19T09:00:00Z")
    assert C.toronto_at("2026-07-01", "04:00") == to_epoch("2026-07-01T08:00:00Z")
    assert O.Run(store.connect(":memory:"), to_epoch("2026-11-19T04:59:00Z")).today == "2026-11-18"


# ── the lighter Open now ─────────────────────────────────────────────────────

def _story(key, group=1, names=True):
    n = {"subject": "Jane Roe", "search_names": "Jane Roe", "search_event": "Jane Roe resigns"} if names else {}
    return {"key": key, "group": group, "kind": "us_official", "names": n, "place": "any_day",
            "head": {"title": "Will Jane Roe resign?", "rules": "",
                     "markets": [{"question": "Will Jane Roe resign?", "description": ""}]}}


def test_one_search_a_story_current_cards_first_and_capped():
    assert O.story_queries(_story("a", group=1)) == ["Jane Roe resigns"]          # one search, the event's
    conn = store.connect(":memory:")
    store.save_open_day(conn, "2026-11-18", "updated", NOW - 86400, [{"id": "card", "place": "stays", "label": "listed", "listed_since": "2026-11-10"}])
    r = O.Run(conn, NOW, news_limit=2)
    r.stories = {k: _story(k, g) for k, g in (("z", 3), ("b", 1), ("card", 2), ("a", 1))}
    assert [s["key"] for s in r.news_todo()] == ["card", "a"]                     # the day's card first, then group
    assert [k for k, _, _ in r.news_keys()] == ["card", "a"] and all(len(r.news_keys()[0][2]) < 600 for _ in [0])
    assert C.BUDGET["caps"]["ok"]["open_searches"] == 300 and C.LISTS["open_searches_per_story"] == 1


def test_one_step_reading_counts():
    h = {"title": "Board weighs A", "source": "Wire", "published": "2026-11-18T12:00:00Z", "url": "u"}
    readings = [{"headlines_sha1": "s", "run": 1, "data": {"given": [h], "answered": [O._key(h)], "steps": [h]}}]
    assert O.STEP_READINGS == 1 and [x["title"] for x in O.step_verdicts(readings)["both"]] == ["Board weighs A"]
    assert O.OUT_RUNS == (1,)


def test_the_days_caps_hold_back_readings():
    conn = store.connect(":memory:")
    r = O.Run(conn, NOW, out_left=1)
    r.stories = {k: dict(_story(k), match=None) for k in ("a", "b")}
    asked = []

    class Client:
        def generate(self, prompt, schema=None):
            asked.append(prompt)
            return json.dumps({"answers": [{"id": "s0", "already_out": "none", "already_out_status": "none"}]}), \
                "fake", {}
    r.client = Client()
    with Patch((O, "sentences", lambda s, now=None: {"settles": "it"})):
        r.read([{"key": k, "sha": k, "given": [{"title": "t", "source": "s", "published": "2026-11-18T12:00:00Z"}],
                 "run": 1} for k in ("a", "b")])
    assert len(asked) == 1 and r.left["already out"] == {"b"} and r.out_left == 0


# ── answers already public: GTA VI ──────────────────────────────────────────

def test_the_gta_vi_market_leaves_the_list_once_the_game_is_out():
    gta = "will jason or lucia die in gta vi"
    assert K.released_work("Will Jason or Lucia die in GTA VI?") == "GTA VI"
    assert K.released_headline("GTA VI", [{"title": "GTA 6 trailer 3 lands before launch"},
                                           {"title": "GTA 6 delayed again"}]) is None
    out = {"title": "GTA 6 is out now and players are already finding Lucia's fate", "source": "IGN",
           "published": "2026-11-18T15:00:00Z", "url": "u"}
    assert K.released_headline("GTA VI", [out]) is out
    assert K.released_work("Will Trump appear in New York?") is None
    # the full name for the initials (either way), and the signs of a work out: a review, sales, a patch, players
    for title in ("Grand Theft Auto 6 review: Vice City is a triumph", "GTA 6 review: the best Rockstar game, but not "
                  "without flaws", "GTA 6 sales top 50 million in first week", "GTA 6 patch 1.02 fixes crashes",
                  "GTA 6 players are finding Easter eggs", "GTA 6 ending explained"):
        assert K.released_headline("GTA VI", [{"title": title}]), title
    assert K.released_headline("Grand Theft Auto VI", [{"title": "GTA 6 review: stunning"}])
    # still to come: anywhere in the headline, or "will", "expected", "could" next to the name
    for title in ("Will GTA 6 be delayed again?", "GTA 6 release date: everything we know", "What players want from "
                  "GTA 6 before launch", "GTA 6 expected to break sales records", "GTA 6 could top GTA 5 sales",
                  "GTA 5 review ten years on"):
        assert K.released_headline("GTA VI", [{"title": title}]) is None, title
    conn = store.connect(":memory:")
    # yesterday's list had the GTA VI card
    store.save_open_day(conn, "2026-11-18", "updated", NOW - 86400, [{"id": gta, "place": "stays", "label": "listed",
                                                                   "listed_since": "2026-11-10"}])
    saved = []
    r = O.Run(conn, NOW, on_answer=lambda *a: saved.append(a))
    story = {"key": gta, "group": 2, "kind": "personnel_pick", "names": {"subject": "GTA VI"}, "place": "any_day",
             "head": {"title": "Will Jason or Lucia die in GTA VI?", "rules": "Rockstar's game Grand Theft Auto VI",
                      "markets": [{"question": "Will Jason or Lucia die in GTA VI?"}]},
             "headlines": [dict(out, ts=to_epoch(out["published"]))]}
    r.stories = {gta: story}
    with Patch((O, "sentences", lambda s, now=None: {"settles": "the game is out"})):
        assert r.released_works([gta]) == [gta]
    assert gta in store.open_answered(conn) and saved and saved[0][2] == O.OUT_VERSION and saved[0][5] == "rule"
    # the story is never a row of the list again, today or on a later day (the reading is kept by the prune)
    assert r.rows({}) == ([], [], [])
    later = O.Run(conn, NOW + 40 * 86400)
    later.answered = store.open_answered(conn)
    later.stories = {gta: story}
    assert later.rows({}) == ([], [], [])
    removed = state.prune(conn, NOW + 80 * 86400)
    assert removed["open_answers"] == 0 and gta in store.open_answered(conn)
    # the "already out" question now says a work being out counts
    assert "the work being out" in O.OUT_PROMPT and O.OUT_VERSION == "open-out-2"


def test_a_person_filed_dated_card_is_searched_and_dropped_once_the_game_is_out():
    gta = "will jason or lucia die in gta vi"
    review = {"title": "Grand Theft Auto VI review \u2013 gaming's most depraved world", "source": "The Guardian",
              "published": "2026-11-18T15:00:00Z", "url": "u"}
    conn = store.connect(":memory:")
    store.save_open_day(conn, "2026-11-18", "updated", NOW - 86400, [{"id": gta, "place": "stays", "label": "listed",
                                                                   "listed_since": "2026-11-10",
                                                                   "headlines": [review]}])
    story = {"key": gta, "group": 2, "kind": "finished_content", "names": {"question": "Will Jason or Lucia die in "
             "GTA VI?"}, "place": "dated", "head": {"title": "Will Jason or Lucia die in GTA VI?", "rules": "",
             "markets": [{"question": "Will Jason or Lucia die in GTA VI?"}]}}
    # a filing's names give no search: the title's, and its headlines are on topic by the title's words
    assert O.story_queries(story) == ["Jason or Lucia die GTA VI"]
    assert O.on_topic("GTA 6 review: a triumph", {"question": "x"}, ["Jason or Lucia die GTA VI"])
    r = O.Run(conn, NOW, news_limit=1)
    r.stories = {gta: story, "a": _story("a", 1)}
    assert [s["key"] for s in r.news_todo()] == [gta]                   # a dated card not due soon is still searched
    with Patch((O, "sentences", lambda s, now=None: {"settles": "the game is out"})):
        assert r.released_works([gta]) == [gta]                        # the card's own headlines are read too
    assert gta in store.open_answered(conn)


def test_a_model_reading_of_out_is_recorded_for_good():
    conn = store.connect(":memory:")
    r = O.Run(conn, NOW)
    r.stories = {"a": dict(_story("a"), match=None)}
    h = {"title": "Jane Roe resigns", "source": "Wire", "published": "2026-11-18T12:00:00Z"}

    class Client:
        def generate(self, prompt, schema=None):
            return json.dumps({"answers": [{"id": "s0", "already_out": "Jane Roe resigns",
                                            "already_out_status": "out"}]}), "fake", {}
    r.client = Client()
    r.stories["a"]["match"] = None
    with Patch((O, "sentences", lambda s, now=None: {"settles": "it"}),
               (O, "check_out", lambda a, given, names: {"out": h, "out_status": "out", "unmatched": None})):
        r.read([{"key": "a", "sha": "x", "given": [h], "run": 1}])
    assert store.open_answered(conn) == {"a"} and "a" in r.answered


# ── Open now in steps, against one run of the same day ──────────────────────

def test_open_now_in_steps_makes_the_list_one_run_makes():
    import test_watchlist_opennow as TO
    from watchlist import net
    run = "daily:" + local_day(TO.NOW)
    api, w = FakeApi(), FakeWorker()
    _seed_state(api)
    client = TO.FakeClient()
    with Patch((net, "walk_open_events", lambda min_volume=0: list(TO.EVENTS)), (news, "search", TO.fake_search),
               (O, "open_client", lambda: client), (O, "make_step_client", lambda: client)):
        w.results[run] = {"daily:work": {"now": TO.NOW, "work": {}}}
        for kind in ("open.walk", "open.name", "open.news", "open.read", "open.out", "open.publish"):
            got = getattr(steps, kind.replace(".", "_"))(Ctx(api, w, kind, run=run, now=TO.NOW))
            assert got is None, (kind, got)
        assert len(w.news) == len({k for k in w.news})                     # each search stored once
        searched = len(w.news)
        # a second pass of the news step searches nothing: the rows stand
        steps.open_news(Ctx(api, w, "open.news", run=run, now=TO.NOW))
        assert len(w.news) == searched
    h, doc = blobs.get_json(api, "site:open.json")
    stepped = [(c["id"], c["place"]) for c in doc["days"][0]["cards"]]
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        O.daily(conn=store.connect(":memory:"), now=TO.NOW, client=TO.FakeClient(), walk=lambda: TO.EVENTS,
                search=TO.fake_search, out_path=out, news_cache_path=None, sleep=lambda s: None, workers=1)
        with open(out) as f:
            single = [(c["id"], c["place"]) for c in json.load(f)["days"][0]["cards"]]
    assert stepped == single and stepped, (stepped, single)
    assert doc["days"][0]["day"] == local_day(TO.NOW) and doc["generated_at"].endswith("-05:00")
    # the readings went out as results and were folded into the saved state
    assert any(k.startswith("open.read:open_answers:") for k in w.results[run])
    assert any(k.startswith("open.out:open_answers:") for k in w.results[run])
    fresh = steps.State(Ctx(api, w, "daily.end", run=run)).load()
    assert O.latest_day(fresh.conn)["day"] == local_day(TO.NOW)


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
