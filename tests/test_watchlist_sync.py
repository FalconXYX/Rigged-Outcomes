#!/usr/bin/env python3
"""
Offline tests for the daily job's side of the site's API (watchlist.sync):
a fake HTTP layer, temporary files, an in-memory store. No network.

    python3 tests/test_watchlist_sync.py

Covers no API configured (every command a no-op that exits 0, an empty
work.json), retries (a 5xx or a network error, never a 4xx) and the token
never printed, the queue's candidate keys, decisions saved twice harmlessly,
withdrawn ones taken out of the store, and the apply-mode rebuild, each
market to add sent to add_market (a market with no page name sent without
one, so it is named from its slug) with the markets added before, and its
result saved in the database (the fake API, tests/fake_api.py) as a new
market:<name>, one the database did not answer for left for the next run
and one it refused reported failed, a step killed part way through adding a
market still reporting it as failed, the catalog from the published files
and the added markets, and the run's report: decisions counted as applied
only once this run saved the state, added and failed markets, results older
than the run never.
"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from fake_api import FakeApi  # noqa: E402
from watchlist import ai, blobs, opennow as O, store, sync  # noqa: E402
from watchlist.common import to_epoch  # noqa: E402

NOW = to_epoch("2026-11-14T04:17:00Z")
TOKEN = "job-token-that-must-never-be-printed"


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.content = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """Answers from a list of (status, body) or exceptions, in order; records every call."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def request(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        a = self.answers.pop(0) if self.answers else (200, {})
        if isinstance(a, Exception):
            raise a
        return Resp(*a)


def fake_client(answers):
    waits = []
    c = sync.Client("https://api.example/", TOKEN, session=FakeSession(answers), sleep=waits.append)
    return c, waits


@contextlib.contextmanager
def env(**kv):
    """Set (or, with None, unset) environment variables and point ../.env at a missing file."""
    old = {k: os.environ.get(k) for k in kv}
    old_file = ai.ENV_FILE
    ai.ENV_FILE = os.path.join(tempfile.gettempdir(), "no-such-dir-rigged", ".env")
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        ai.ENV_FILE = old_file
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def captured():
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        yield out


def a_day(conn):
    """A stored open-list day with a queue: one item with two candidate headlines, one dated card."""
    card = {"id": "jane roe", "place": "new step", "label": "new", "listed_since": "2026-11-13", "question": "Q?"}
    awaiting = [
        {"n": 1, "story": "jane roe", "place": "new step", "why": "awaiting approval", "kind": "us_official",
         "group": 1, "question": "Will Jane Roe leave?", "who_knows": "The President.", "polymarket": "Roe out?",
         "url": "https://polymarket.com/event/e1", "reveal": None, "model_copy_unmatched": [],
         "candidates": [{"title": "White House  weighs Jane Roe’s future", "source": "Reuters",
                         "published": "2026-11-13T01:00:00Z", "url": "https://r/1"},
                        {"title": "Roe prepares exit", "source": "AP", "published": None, "url": None}]},
        {"n": 2, "story": "turner", "place": "dated", "why": "dated, on the list", "kind": "jury_prize", "group": 2,
         "question": "Who wins?", "who_knows": "The jury.", "polymarket": "Turner?", "url": "https://p/2",
         "reveal": "2026-11-15", "model_copy_unmatched": [], "candidates": []}]
    store.save_open_day(conn, "2026-11-13", "updated", NOW - 3600, [card],
                        info={"awaiting": awaiting, "steps_by": "person", "new_today": 1})
    return awaiting


def test_no_api_is_a_no_op():
    with env(WATCHLIST_API_URL=None, WATCHLIST_JOB_TOKEN=None, GITHUB_OUTPUT=None), \
            tempfile.TemporaryDirectory() as tmp:
        assert sync.client() is None
        out = os.path.join(tmp, "work.json")
        with captured() as text:
            assert sync.main(["pull", "--out", out]) == 0
            assert sync.main(["catalog", "--closed", os.path.join(tmp, "x"), "--open", os.path.join(tmp, "y")]) == 0
        assert "No API configured" in text.getvalue()
        assert json.load(open(out)) == {"fetched_at": json.load(open(out))["fetched_at"], "configured": False,
                                        "decisions": [], "requests": [], "watched": []}
        conn = store.connect(":memory:")
        with captured():
            assert sync.push(None, conn, tmp, "full", "success", NOW) is None
        # a URL without a token (or the reverse) is not an API either
        with env(WATCHLIST_API_URL="https://api.example", WATCHLIST_JOB_TOKEN=None):
            assert sync.client() is None
        with env(WATCHLIST_API_URL="https://api.example", WATCHLIST_JOB_TOKEN=TOKEN):
            c = sync.client()
            assert c is not None and TOKEN not in repr(c)


def test_settings_come_from_the_env_file():
    with tempfile.TemporaryDirectory() as tmp, env(WATCHLIST_API_URL=None, WATCHLIST_JOB_TOKEN=None):
        path = os.path.join(tmp, ".env")
        with open(path, "w") as f:
            f.write("GEMINI_API_KEY=x\nexport WATCHLIST_API_URL='https://api.example'  # the worker\n"
                    f"WATCHLIST_JOB_TOKEN=\"{TOKEN}\"\n")
        ai.ENV_FILE = path
        c = sync.client()
        assert c.base == "https://api.example" and c._token == TOKEN


def test_retries_and_the_token_never_printed():
    c, waits = fake_client([(503, {"error": "server"}), requests.ConnectionError(f"boom {TOKEN}"),
                            (200, {"decisions": [], "requests": [], "watched": [{"story": "s"}]})])
    assert c.request("GET", "/api/job/work")["watched"] == [{"story": "s"}]
    assert waits == [2, 8]
    call = c.session.calls[0]
    assert call["url"] == "https://api.example/api/job/work" and call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    # a 4xx is not retried
    c, waits = fake_client([(401, {"error": "unauthorized", "message": "That token was not accepted."})])
    try:
        c.request("GET", "/api/job/work")
        raise AssertionError("no error")
    except sync.ApiError as e:
        assert e.status == 401 and e.code == "unauthorized" and waits == [] and TOKEN not in str(e)
    # every try failing: pull warns, writes an empty work.json and goes on; nothing prints the token
    with tempfile.TemporaryDirectory() as tmp, captured() as text:
        out = os.path.join(tmp, "work.json")
        c, waits = fake_client([requests.ConnectionError(f"down {TOKEN}")] * 4)
        doc = sync.pull(c, out, now=NOW)
        assert waits == list(sync.RETRY_WAITS) and doc["configured"] and doc["decisions"] == [] and doc["error"]
        conn = store.connect(":memory:")
        a_day(conn)
        c, _ = fake_client([(502, b"<html>error 1027</html>")] * 20)
        body = sync.push(c, conn, tmp, "full", "success", NOW, now=NOW + 60)
        assert body["run"]["status"] == "success"
    assert "the API did not give the job its work" in text.getvalue() and TOKEN not in text.getvalue()
    # a 2xx that is not JSON (an HTML error page) is an error, not an answer
    c, _ = fake_client([(200, b"<html>")])
    try:
        c.request("GET", "/api/job/work")
        raise AssertionError("no error")
    except sync.ApiError as e:
        assert e.code == "bad_response"


def test_pull_writes_work_and_outputs():
    with tempfile.TemporaryDirectory() as tmp:
        gh = os.path.join(tmp, "gh_output")
        with env(GITHUB_OUTPUT=gh), captured():
            c, _ = fake_client([(200, {"now": NOW, "decisions": [{"story": "s"}],
                                       "requests": [{"status": "approved", "condition_id": "0x1"}],
                                       "watched": []})])
            doc = sync.pull(c, os.path.join(tmp, "work.json"), now=NOW)
        assert doc["decisions"] == [{"story": "s"}] and json.load(open(os.path.join(tmp, "work.json")))["requests"]
        assert open(gh).read() == "decisions=true\nrequests=true\n"


def test_queue_payload_carries_each_key():
    conn = store.connect(":memory:")
    assert sync.queue_payload(conn) is None
    awaiting = a_day(conn)
    q = sync.queue_payload(conn)
    assert q["day"] == "2026-11-13" and q["run_at"] == "2026-11-14T03:17:00Z" and q["steps_by"] == "person"
    assert len(q["items"]) == 2 and q["items"][0]["n"] == 1
    keys = [h["key"] for h in q["items"][0]["candidates"]]
    assert keys == [O.headline_key(h) for h in awaiting[0]["candidates"]] == \
        ["white house weighs jane roe's future", "roe prepares exit"]
    assert q["items"][1]["candidates"] == []
    assert set(q["items"][0]) == set(awaiting[0])                       # the QueueItem shape, nothing more


def decisions():
    h = {"title": "White House weighs Jane Roe's future", "source": "Reuters", "published": "2026-11-13T01:00:00Z",
         "url": "https://r/1"}
    return [{"story": "jane roe", "headline_key": O.headline_key(h), "decision": "step", "headline": h,
             "day": "2026-11-13", "decided_at": NOW - 600, "applied_at": None},
            {"story": "turner", "headline_key": "card", "decision": "not a step", "headline": None,
             "day": "2026-11-13", "decided_at": NOW - 500, "applied_at": None},
            {"story": "bad", "headline_key": "", "decision": "maybe", "headline": None, "day": "x",
             "decided_at": 1, "applied_at": None}]


def test_decisions_saved_twice_are_harmless():
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp, captured():
        res = os.path.join(tmp, "decide_results.json")
        first = sync.decide(conn, {"decisions": decisions()}, "full", res, now=NOW)
        reviews = store.open_reviews(conn)
        second = sync.decide(conn, {"decisions": decisions()}, "full", res, now=NOW + 1)
        assert store.open_reviews(conn) == reviews and first["saved"] == second["saved"]
    assert [s["story"] for s in first["saved"]] == ["jane roe", "turner"]          # the unreadable one skipped
    assert reviews["jane roe"]["white house weighs jane roe's future"]["decision"] == "step"
    assert reviews["jane roe"]["white house weighs jane roe's future"]["decided_by"] == sync.DECIDED_BY
    assert reviews["turner"]["card"] == {"decision": "not a step", "headline": None, "decided_by": sync.DECIDED_BY,
                                         "decided_at": NOW - 500}
    assert not first["rebuilt"]


def test_a_withdrawn_decision_leaves_the_store():
    """A decision the owner took back after a run saved it: the next run removes it from the store (only one the
    review page made), reports it so the API drops it, and an apply run rebuilds only when something changed."""
    conn = store.connect(":memory:")
    calls = []
    rebuild = lambda c, out_path: calls.append(out_path) or {"status": "updated"}  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp, captured():
        res = os.path.join(tmp, "decide_results.json")
        sync.decide(conn, {"decisions": decisions()}, "full", res, now=NOW)
        store.save_review(conn, "turner", "by hand", "step", {"title": "By hand"}, by="person", at=NOW - 50)
        key = decisions()[0]["headline_key"]
        gone = [{"story": "jane roe", "headline_key": key, "decision": "withdrawn", "headline": None,
                 "day": "2026-11-13", "decided_at": NOW - 100, "applied_at": None},
                {"story": "turner", "headline_key": "by hand", "decision": "withdrawn", "headline": None,
                 "day": "2026-11-13", "decided_at": NOW - 90, "applied_at": None}]
        got = sync.decide(conn, {"decisions": gone}, "apply", res, rebuild=rebuild, now=NOW + 10)
        reviews = store.open_reviews(conn)
        assert key not in reviews.get("jane roe", {})                    # the review page's decision is gone
        assert reviews["turner"]["by hand"]["decided_by"] == "person"    # one made on the command line stays
        assert got["saved"] == [{"story": "jane roe", "headline_key": key, "decided_at": NOW - 100, "withdrawn": True},
                                {"story": "turner", "headline_key": "by hand", "decided_at": NOW - 90, "withdrawn": True}]
        assert calls == [sync.OPEN_JSON] and got["rebuilt"]
        again = sync.decide(conn, {"decisions": gone}, "apply", res, rebuild=rebuild, now=NOW + 20)
        assert len(again["saved"]) == 2 and len(calls) == 1               # nothing left to remove: no rebuild


def test_apply_mode_rebuilds_the_reviewed_day():
    conn = store.connect(":memory:")
    calls = []
    with tempfile.TemporaryDirectory() as tmp, captured():
        res = os.path.join(tmp, "decide_results.json")
        rebuild = lambda c, out_path: calls.append(out_path) or {"status": "updated"}  # noqa: E731
        got = sync.decide(conn, {"decisions": decisions()}, "apply", res, open_out="/x/open.json", rebuild=rebuild,
                          now=NOW)
        assert calls == ["/x/open.json"] and got["rebuilt"] and json.load(open(res))["rebuilt"]
        sync.decide(conn, {"decisions": []}, "apply", res, rebuild=rebuild, now=NOW)     # nothing saved: no rebuild
        assert len(calls) == 1

        # a rebuild that fails still leaves the saved decisions on record
        def broken(c, out_path):
            raise RuntimeError("model down")
        try:
            sync.decide(conn, {"decisions": decisions()}, "apply", res, rebuild=broken, now=NOW + 5)
            raise AssertionError("no error")
        except RuntimeError:
            pass
        assert len(json.load(open(res))["saved"]) == 2 and json.load(open(res))["at"] == NOW + 5


def _files(job, pushed_at=None, decided_at=None, added_at=None, results=None):
    if pushed_at is not None:
        json.dump({"ok": True, "version": "a" * 32, "changed": True, "at": pushed_at},
                  open(os.path.join(job, "state_pushed.json"), "w"))
    if decided_at is not None:
        json.dump({"saved": [{"story": "jane roe", "headline_key": "k", "decided_at": 5}], "rebuilt": False,
                   "at": decided_at}, open(os.path.join(job, "decide_results.json"), "w"))
    if added_at is not None:
        json.dump({"results": results, "at": added_at}, open(os.path.join(job, "add_results.json"), "w"))


RESULTS = [{"condition_id": "0x1", "status": "added", "name": "gemini-carbon", "note": None, "changed": True,
            "saved": True},
           {"condition_id": "0x2", "status": "added", "name": "already", "note": "already on the main page",
            "changed": False, "saved": False},
           {"condition_id": "0x3", "status": "failed", "name": "too-big", "note": "too many trades", "changed": False,
            "saved": False}]


def test_applied_only_once_this_run_pushed_the_state():
    conn = store.connect(":memory:")
    started = NOW
    with tempfile.TemporaryDirectory() as job:
        body = sync.done_payload(conn, job, "full", "success", started, now=NOW + 60)
        assert body["applied"] == [] and body["requests"] == []
        _files(job, pushed_at=started - 86_400, decided_at=started + 10)          # yesterday's save
        assert sync.done_payload(conn, job, "full", "success", started)["applied"] == []
        _files(job, pushed_at=started + 30)
        body = sync.done_payload(conn, job, "apply", "partial", started, "https://run/1", now=NOW + 60)
        assert body["applied"] == [{"story": "jane roe", "headline_key": "k", "decided_at": 5}]
        assert body["run"] == {"mode": "apply", "status": "partial", "started_at": started, "finished_at": NOW + 60,
                               "summary": body["run"]["summary"], "url": "https://run/1"}
        _files(job, decided_at=started - 1)                                      # an older run's results
        assert sync.done_payload(conn, job, "full", "success", started)["applied"] == []


def test_added_and_failed_are_reported():
    """A market saved in the database, or there already, is reported added; a failed one failed; an older run's
    results never."""
    conn = store.connect(":memory:")
    started = NOW
    with tempfile.TemporaryDirectory() as job:
        _files(job, added_at=started + 5, results=RESULTS)
        body = sync.done_payload(conn, job, "full", "success", started)
        assert {r["condition_id"]: r["status"] for r in body["requests"]} == {"0x1": "added", "0x2": "added",
                                                                              "0x3": "failed"}
        assert set(body["requests"][0]) == {"condition_id", "status", "name", "note"}
        assert "2 market(s) added, 1 failed" in body["run"]["summary"]
        _files(job, added_at=started - 5, results=RESULTS)
        assert sync.done_payload(conn, job, "full", "success", started)["requests"] == []


def test_push_sends_the_queue_the_catalog_and_the_report():
    conn = store.connect(":memory:")
    a_day(conn)
    with tempfile.TemporaryDirectory() as tmp, captured():
        markets = os.path.join(tmp, "markets.json")
        json.dump([{"name": "usiran", "slug": "us-strikes-iran"}, {"name": "x"}], open(markets, "w"))
        closed = os.path.join(tmp, "closed.json")
        pick = {"condition_id": "0xa", "slug": "m-a", "question": "Q?", "url": "https://p/a", "winner": "Yes",
                "closed": "2026-11-13T20:00:00+00:00"}
        json.dump({"generated_at": "2026-11-14T04:30:00+00:00",
                   "days": [{"day": "2026-11-13", "picks": [pick, {"condition_id": "0xb"}]},
                            {"day": "2026-11-12", "picks": [dict(pick, closed="old")]}]}, open(closed, "w"))
        opened = os.path.join(tmp, "open.json")
        card = {"id": "jane roe", "question": "Will Jane Roe leave?", "polymarket": {"url": "https://p/e1"}}
        json.dump({"days": [{"day": "2026-11-14", "cards": [card]}, {"day": "2026-11-13", "cards": [card,
                   dict(card, id="acme")]}, {"day": "2026-11-12", "cards": [dict(card, id="old")]}]}, open(opened, "w"))
        added = {"name": "market:gemini-carbon", "meta": {"entry": {"name": "gemini-carbon", "slug": "gemini-carbon-ship",
                                                                    "output": "GeminiCarbon", "correct_outcome": "Yes"}}}
        c, _ = fake_client([(204, None), (200, {"blobs": [added]}), (200, {"market": 2, "closed": 1, "open": 2}),
                            (204, None)])
        body = sync.push(c, conn, tmp, "full", "success", NOW, run_url="https://run/2", now=NOW + 99,
                         closed_path=closed, open_path=opened, markets_path=markets)
        # a missing file leaves its section out (the API keeps the last one)
        assert set(sync.catalog_payload(markets, os.path.join(tmp, "none.json"), opened)) == {"markets", "open"}
    calls = c.session.calls
    assert [(x["method"], x["url"].split("example")[1]) for x in calls] == [
        ("PUT", "/api/job/queue"), ("GET", "/api/job/blobs?prefix=market:"), ("PUT", "/api/job/catalog"),
        ("POST", "/api/job/done")]
    assert calls[0]["json"]["items"][0]["candidates"][0]["key"] == "white house weighs jane roe's future"
    cat = calls[2]["json"]
    assert cat["markets"] == [{"name": "usiran", "slug": "us-strikes-iran"},
                              {"name": "gemini-carbon", "slug": "gemini-carbon-ship"}]
    assert cat["closed"] == [dict({k: pick[k] for k in ("condition_id", "slug", "question", "url", "winner",
                                                          "closed")}, day="2026-11-13")]
    assert cat["open"] == [{"story": "jane roe", "question": "Will Jane Roe leave?", "url": "https://p/e1"},
                           {"story": "acme", "question": "Will Jane Roe leave?", "url": "https://p/e1"}]
    assert calls[3]["json"] == body and body["run"]["url"] == "https://run/2"
    assert body["run"]["summary"].startswith("open list 2026-11-13: updated, 1 card(s), 1 new, 2 waiting for review")
    assert "closed list: updated" in body["run"]["summary"]


def test_the_catalog_leaves_the_markets_out_when_the_added_ones_cannot_be_read():
    with tempfile.TemporaryDirectory() as tmp, captured() as text:
        markets = os.path.join(tmp, "markets.json")
        json.dump([{"name": "usiran", "slug": "us-strikes-iran"}], open(markets, "w"))
        api = FakeApi()
        api.fail_next(503, "server", method="GET", path="prefix=market:")
        sync.catalog(api, markets_path=markets, closed_path=os.path.join(tmp, "x"), open_path=os.path.join(tmp, "y"))
        assert api.other == [("PUT", "/api/job/catalog", {})]                  # no markets: the API keeps the last
        assert "added before could not be read" in text.getvalue()
        blobs.put_json(api, "market:gemini-carbon", {}, {"entry": {"name": "gemini-carbon", "slug": "g-c"}})
        sync.catalog(api, markets_path=markets, closed_path=os.path.join(tmp, "x"), open_path=os.path.join(tmp, "y"))
        assert api.other[-1][2] == {"markets": [{"name": "usiran", "slug": "us-strikes-iran"},
                                                {"name": "gemini-carbon", "slug": "g-c"}]}


def built(name, slug, outcome="Yes"):
    entry = {"name": name, "slug": slug, "output": name.title().replace("-", ""), "correct_outcome": outcome}
    return {"status": "added", "name": name, "note": None, "changed": True, "entry": entry,
            "index": {"name": name, "slug": slug, "title": "T?"}, "market": {"name": name, "timeline": {"t": [1]}},
            "data": {f"{entry['output']}_clean.csv": "a,b\n1,2\n"}}


def test_a_client_can_wait_longer():
    """The state's commands (watchlist.state) retry for about 14 minutes, so a short outage never costs a run."""
    from watchlist import state as S
    waits = []
    answers = [requests.ConnectionError("down")] * len(S.PATIENT_WAITS) + [(200, {"ok": True})]
    c = sync.Client("https://api.example/", TOKEN, session=FakeSession(answers), sleep=waits.append,
                    retry_waits=S.PATIENT_WAITS)
    assert c.request("GET", "/api/job/blobs/state") == {"ok": True} and waits == list(S.PATIENT_WAITS)
    assert 13 * 60 <= sum(S.PATIENT_WAITS) <= 15 * 60
    with env(WATCHLIST_API_URL="https://api.example", WATCHLIST_JOB_TOKEN=TOKEN):
        assert sync.client().retry_waits == sync.RETRY_WAITS
        assert sync.client(S.PATIENT_WAITS).retry_waits == S.PATIENT_WAITS
    asked = []
    real = sync.client
    sync.client = lambda *a: asked.append(a) or None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            for cmd in (["pull", "--db", ":memory:"], ["push", "--db", ":memory:"], ["migrate", "--dir", "x"]):
                assert S.main(cmd) in (2, 3)
    finally:
        sync.client = real
    assert asked == [(S.PATIENT_WAITS,)] * 3


def test_add_runs_each_market_to_add_and_saves_it():
    seen = []

    def adder(cid, slug, name, outcome, dry_run=False, added=()):
        seen.append((cid, slug, name, outcome, dry_run, [m["name"] for m in added]))
        if name == "boom":
            raise ValueError("unexpected")
        if dry_run:
            return {"status": "added", "name": name or slug, "note": "dry run", "changed": False}
        return built(name or slug, slug)

    # "Add to main page" records a market with no page name (add_market names it from its slug); only the
    # approved ones are added
    work = {"requests": [{"condition_id": "0x1", "slug": "m-1", "name": None, "winner": "Yes", "status": "approved"},
                         {"condition_id": "0x2", "slug": "m-2", "name": "boom", "winner": "No", "status": "approved"},
                         {"condition_id": "0x4", "slug": "m-4", "name": "m-4", "winner": "No", "status": "approved"},
                         {"condition_id": "0x3", "slug": "m-3", "name": "x", "winner": "No", "status": "added"}]}
    with tempfile.TemporaryDirectory() as tmp:
        gh = os.path.join(tmp, "gh")
        res = os.path.join(tmp, "add_results.json")
        api = FakeApi()
        blobs.put_json(api, "market:older", {}, {"entry": {"name": "older", "slug": "o"}})
        with env(GITHUB_OUTPUT=gh, GITHUB_ACTIONS=None), captured():
            got = sync.add(api, work, res, adder=adder, now=NOW)
        assert [x[0] for x in seen] == ["0x1", "0x2", "0x4"] and seen[0] == ("0x1", "m-1", None, "Yes", False, ["older"])
        assert seen[2][-1] == ["older", "m-1"]                                  # a market added this run counts too
        assert [r["status"] for r in got] == ["added", "failed", "added"] and "ValueError" in got[1]["note"]
        assert got[0]["name"] == "m-1" and got[0]["saved"] and not got[1]["saved"]
        h, doc = blobs.get_json(api, "market:m-1")
        assert doc == {k: built("m-1", "m-1")[k] for k in ("entry", "index", "market", "data")}
        assert h["meta"] == {"entry": built("m-1", "m-1")["entry"], "condition_id": "0x1", "added_at": NOW,
                             "by": "local"}
        commits = [b for m, p, b in api.log if m == "POST" and "market:older" not in p]
        assert len(commits) == 2 and all(b["expect"] is None for b in commits)    # never over a market saved before
        assert json.load(open(res))["at"] == NOW and open(gh).read() == "markets=true\n"
        assert json.load(open(res))["results"] == got
        # killed part way through the second market: the first's result and the second on record as failed
        def killed(cid, slug, name, outcome, dry_run=False, added=()):
            if cid == "0x2":
                raise KeyboardInterrupt
            return built(name or "m-9", slug)
        try:
            with env(GITHUB_OUTPUT=gh), captured():
                sync.add(api, {"requests": work["requests"][:2]}, res, adder=killed, now=NOW + 1)
            raise AssertionError("not interrupted")
        except KeyboardInterrupt:
            pass
        left = json.load(open(res))["results"]
        assert [(r["condition_id"], r["status"]) for r in left] == [("0x1", "added"), ("0x2", "failed")]
        assert left[1]["note"] == sync.STOPPED and not left[1]["changed"]
        with tempfile.TemporaryDirectory() as job:
            with open(res) as f, open(os.path.join(job, "add_results.json"), "w") as g:
                g.write(f.read())
            reported = sync.done_payload(store.connect(":memory:"), job, "full", "partial", NOW)["requests"]
        assert [(r["condition_id"], r["status"]) for r in reported] == [("0x1", "added"), ("0x2", "failed")]
        assert reported[1] == {"condition_id": "0x2", "status": "failed", "name": "boom", "note": sync.STOPPED}
        # a dry run writes no results, saves nothing and never asks for a deploy
        os.remove(res)
        puts = api.calls["PUT"]
        with env(GITHUB_OUTPUT=gh), captured():
            sync.add(api, work, res, dry_run=True, adder=adder, now=NOW)
        assert not os.path.exists(res) and open(gh).read().endswith("markets=false\n") and seen[-1][4] is True
        assert api.calls["PUT"] == puts


def test_add_when_the_database_does_not_take_it():
    """A network error or a 5xx leaves the request unreported (it stays approved, the next run tries again); a
    refusal (a 400, or the name taken meanwhile) reports it failed. No API, or the added markets unreadable:
    nothing is tried."""
    work = {"requests": [{"condition_id": "0x1", "slug": "m-1", "name": "m-1", "winner": "Yes", "status": "approved"},
                         {"condition_id": "0x2", "slug": "m-2", "name": "m-2", "winner": "Yes", "status": "approved"},
                         {"condition_id": "0x3", "slug": "m-3", "name": "m-3", "winner": "Yes", "status": "approved"},
                         {"condition_id": "0x4", "slug": "m-4", "name": "m-4", "winner": "Yes", "status": "approved"},
                         {"condition_id": "0x5", "slug": "m-5", "name": "m-5", "winner": "Yes", "status": "approved"}]}
    adder = lambda cid, slug, name, outcome, dry_run=False, added=(): built(name, slug)  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        res, gh = os.path.join(tmp, "add_results.json"), os.path.join(tmp, "gh")
        api = FakeApi()
        api.fail_next(None, "network", method="PUT", path="market:m-1")
        api.fail_next(503, "server", method="POST", path="market:m-2")
        api.fail_next(400, "bad_request", method="POST", path="market:m-3")
        api.fail_next(409, "full", method="POST", path="market:m-5")                          # the Worker's cap
        blobs.put_json(api, "market:m-4", {}, {"entry": {"name": "other", "slug": "o"}})        # the name taken
        with env(GITHUB_OUTPUT=gh), captured() as text:
            got = sync.add(api, work, res, adder=adder, now=NOW)
        assert [(r["condition_id"], r["status"], r["note"]) for r in got] == [
            ("0x3", "failed", "the database refused it (bad_request)"),
            ("0x4", "failed", "the database refused it (moved)"),
            ("0x5", "failed", sync.FULL)]
        assert "tried again next run" in text.getvalue() and open(gh).read() == "markets=false\n"
        assert json.load(open(res))["results"] == got
        with tempfile.TemporaryDirectory() as job:
            _files(job, added_at=NOW, results=got)
            assert {r["condition_id"] for r in sync.done_payload(store.connect(":memory:"), job, "full", "success",
                                                                  NOW)["requests"]} == {"0x3", "0x4", "0x5"}
        calls = []
        counting = lambda *a, **k: calls.append(a) or built("x", "x")  # noqa: E731
        with env(GITHUB_OUTPUT=gh), captured() as text:
            assert sync.add(None, work, res, adder=counting, now=NOW) == []
        assert "No API configured" in text.getvalue() and json.load(open(res))["results"] == [] and calls == []
        api.fail_next(503, "server", method="GET", path="prefix=market:")
        with env(GITHUB_OUTPUT=gh), captured() as text:
            assert sync.add(api, work, res, adder=counting, now=NOW) == []
        assert "wait for the next run" in text.getvalue() and calls == []


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
