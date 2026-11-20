#!/usr/bin/env python3
"""
Offline tests for the daily job's side of the site's API (watchlist.sync):
a fake HTTP layer, temporary files, an in-memory store. No network.

    python3 tests/test_watchlist_sync.py

Covers no API configured (every command a no-op that exits 0, an empty
work.json), retries (a 5xx or a network error, never a 4xx) and the token
never printed, each market to add sent to add_market (a market with no page name sent without
one, so it is named from its slug) with the markets added before, and its
result saved in the database (the fake API, tests/fake_api.py) as a new
market:<name>, one the database did not answer for left for the next run
and one it refused reported failed, a step killed part way through adding a
market still reporting it as failed, the catalog from the published files
and the added markets, and the run's report: added and failed markets,
results older than the run never, and a catch-up run's report (no run sent).
And how an add that comes in during a run is never left for the next day:
the API's time of each read of the work (read_at), the markets to add read
again right before they are added (add --refresh).
"""
import contextlib
import copy
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
    """A stored open-list day with one card."""
    card = {"id": "jane roe", "place": "new step", "label": "new", "listed_since": "2026-11-13", "question": "Q?"}
    store.save_open_day(conn, "2026-11-13", "updated", NOW - 3600, [card], info={"new_today": 1})


def test_no_api_is_a_no_op():
    with env(WATCHLIST_API_URL=None, WATCHLIST_JOB_TOKEN=None), \
            tempfile.TemporaryDirectory() as tmp:
        assert sync.client() is None
        out = os.path.join(tmp, "work.json")
        with captured() as text:
            assert sync.main(["pull", "--out", out]) == 0
            assert sync.main(["catalog", "--closed", os.path.join(tmp, "x"), "--open", os.path.join(tmp, "y")]) == 0
        assert "No API configured" in text.getvalue()
        assert json.load(open(out)) == {"fetched_at": json.load(open(out))["fetched_at"], "configured": False,
                                        "requests": [], "watched": [], "added": []}
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
                            (200, {"requests": [], "watched": [{"story": "s"}]})])
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
        assert waits == list(sync.RETRY_WAITS) and doc["configured"] and doc["requests"] == [] and doc["error"]
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


def test_pull_writes_work():
    with tempfile.TemporaryDirectory() as tmp:
        with captured():
            c, _ = fake_client([(200, {"now": NOW,
                                       "requests": [{"status": "approved", "condition_id": "0x1"}],
                                       "watched": [], "added": [{"condition_id": "0x2", "day": "2026-11-12"}]})])
            doc = sync.pull(c, os.path.join(tmp, "work.json"), now=NOW)
        assert "decisions" not in doc and json.load(open(os.path.join(tmp, "work.json")))["requests"]
        # the markets added to the main page, for the closed list (their picks stay on their day)
        from watchlist import upsets
        assert upsets.load_requested(os.path.join(tmp, "work.json")) == {"0x1", "0x2"}
        # the API's time of the read (what came in after it waits for the next read)
        assert doc["read_at"] == NOW


def _files(job, added_at=None, results=None):
    if added_at is not None:
        json.dump({"results": results, "at": added_at}, open(os.path.join(job, "add_results.json"), "w"))


RESULTS = [{"condition_id": "0x1", "status": "added", "name": "gemini-carbon", "note": None, "changed": True,
            "saved": True},
           {"condition_id": "0x2", "status": "added", "name": "already", "note": "already on the main page",
            "changed": False, "saved": False},
           {"condition_id": "0x3", "status": "failed", "name": "too-big", "note": "too many trades", "changed": False,
            "saved": False}]


def test_the_report_names_the_run():
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as job:
        body = sync.done_payload(conn, job, "local", "partial", NOW, "https://run/1", now=NOW + 60)
        assert set(body) == {"requests", "watched", "run", "problems"} and body["requests"] == []
        assert body["run"] == {"mode": "local", "status": "partial", "started_at": NOW, "finished_at": NOW + 60,
                               "summary": body["run"]["summary"], "url": "https://run/1"}


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


def test_push_sends_the_catalog_and_the_report():
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
        c, _ = fake_client([(200, {"blobs": [added]}), (200, {"market": 2, "closed": 1, "open": 2}), (204, None)])
        body = sync.push(c, conn, tmp, "full", "success", NOW, run_url="https://run/2", now=NOW + 99,
                         closed_path=closed, open_path=opened, markets_path=markets)
        # a missing file leaves its section out (the API keeps the last one)
        assert set(sync.catalog_payload(markets, os.path.join(tmp, "none.json"), opened)) == {"markets", "open"}
    calls = c.session.calls
    assert [(x["method"], x["url"].split("example")[1]) for x in calls] == [
        ("GET", "/api/job/blobs?prefix=market:"), ("PUT", "/api/job/catalog"), ("POST", "/api/job/done")]
    cat = calls[1]["json"]
    assert cat["markets"] == [{"name": "usiran", "slug": "us-strikes-iran"},
                              {"name": "gemini-carbon", "slug": "gemini-carbon-ship"}]
    assert cat["closed"] == [dict({k: pick[k] for k in ("condition_id", "slug", "question", "url", "winner",
                                                          "closed")}, day="2026-11-13")]
    assert cat["open"] == [{"story": "jane roe", "question": "Will Jane Roe leave?", "url": "https://p/e1"},
                           {"story": "acme", "question": "Will Jane Roe leave?", "url": "https://p/e1"}]
    assert calls[2]["json"] == body and body["run"]["url"] == "https://run/2" and "applied" not in body
    assert body["run"]["summary"].startswith("open list 2026-11-13: updated, 1 card(s), 1 new;")
    assert "closed list: updated" in body["run"]["summary"]


def test_a_catch_up_run_sends_the_catalog_but_no_run():
    # the API keeps runs of the full and local modes only: a catch-up run sends the catalog, and the daily run's
    # report stands
    conn = store.connect(":memory:")
    a_day(conn)
    with tempfile.TemporaryDirectory() as tmp, captured():
        c, _ = fake_client([(200, {"blobs": []}), (200, {"market": 0, "closed": 0, "open": 0})])
        body = sync.push(c, conn, tmp, "catchup", "success", NOW, now=NOW + 99,
                         closed_path=os.path.join(tmp, "none.json"), open_path=os.path.join(tmp, "none.json"),
                         markets_path=os.path.join(tmp, "none.json"))
    assert [(x["method"], x["url"].split("example")[1]) for x in c.session.calls] == [
        ("GET", "/api/job/blobs?prefix=market:"), ("PUT", "/api/job/catalog")]
    assert body["run"]["mode"] == "catchup" and body["requests"] == []


def test_an_add_run_reports_only_its_markets():
    # the workflow's add run (no saved state restored; another run may be making the lists meanwhile): the catalog's main-page markets only (closed.json and open.json on its disk are old copies), and a summary
    # of what it added
    conn = store.connect(":memory:")
    a_day(conn)
    with tempfile.TemporaryDirectory() as tmp, captured():
        markets = os.path.join(tmp, "markets.json")
        json.dump([{"name": "usiran", "slug": "us-strikes-iran"}], open(markets, "w"))
        stale = os.path.join(tmp, "stale.json")
        json.dump({"days": [{"day": "2026-11-01", "picks": [{"condition_id": "0xa", "slug": "old"}],
                             "cards": [{"id": "old"}]}]}, open(stale, "w"))
        json.dump({"results": RESULTS[:1], "at": NOW + 5}, open(os.path.join(tmp, "add_results.json"), "w"))
        added = {"name": "market:gemini-carbon", "meta": {"entry": {"name": "gemini-carbon", "slug": "gemini-carbon-ship",
                                                                    "output": "GeminiCarbon", "correct_outcome": "Yes"}}}
        c, _ = fake_client([(200, {"blobs": [added]}), (200, {"market": 2, "closed": 1, "open": 1}), (204, None)])
        body = sync.push(c, conn, tmp, "local", "success", NOW, now=NOW + 99, closed_path=stale, open_path=stale,
                         markets_path=markets, adds_only=True)
    calls = c.session.calls
    assert [(x["method"], x["url"].split("example")[1]) for x in calls] == [
        ("GET", "/api/job/blobs?prefix=market:"), ("PUT", "/api/job/catalog"), ("POST", "/api/job/done")]
    assert calls[1]["json"] == {"markets": [{"name": "usiran", "slug": "us-strikes-iran"},
                                            {"name": "gemini-carbon", "slug": "gemini-carbon-ship"}]}
    assert body["run"]["summary"] == "1 market(s) added" and body["requests"][0]["condition_id"] == "0x1"


def test_the_catalog_leaves_the_markets_out_when_the_added_ones_cannot_be_read():
    with tempfile.TemporaryDirectory() as tmp, captured() as text:
        markets = os.path.join(tmp, "markets.json")
        json.dump([{"name": "usiran", "slug": "us-strikes-iran"}], open(markets, "w"))
        api = FakeApi()
        # the API did not answer the read (after the client's waits): nothing is sent, its waits go to the report
        api.fail_next(503, "server", method="GET", path="prefix=market:")
        sync.catalog(api, markets_path=markets, closed_path=os.path.join(tmp, "x"), open_path=os.path.join(tmp, "y"))
        assert api.other == [] and "added before could not be read" in text.getvalue()
        assert "the catalog was not sent (the API did not answer)" in text.getvalue()
        # the read refused as a bad request (the job and the Worker disagree): the catalog goes without its markets
        # (the API keeps the last), and the code's problem is said
        api.fail_next(400, "bad_request", method="GET", path="prefix=market:")
        sync.catalog(api, markets_path=markets, closed_path=os.path.join(tmp, "x"), open_path=os.path.join(tmp, "y"))
        assert api.other == [("PUT", "/api/job/catalog", {})]
        assert "X the markets added before could not be read (bad_request (HTTP 400))" in text.getvalue()
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
            for cmd in (["pull", "--db", ":memory:"], ["push", "--db", ":memory:"]):
                assert S.main(cmd) in (2, 3)
    finally:
        sync.client = real
    assert asked == [(S.PATIENT_WAITS,)] * 2


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
        res = os.path.join(tmp, "add_results.json")
        api = FakeApi()
        blobs.put_json(api, "market:older", {}, {"entry": {"name": "older", "slug": "o"}})
        with captured():
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
        assert json.load(open(res))["at"] == NOW
        assert json.load(open(res))["results"] == got
        # killed part way through the second market: the first's result and the second on record as failed
        def killed(cid, slug, name, outcome, dry_run=False, added=()):
            if cid == "0x2":
                raise KeyboardInterrupt
            return built(name or "m-9", slug)
        try:
            with captured():
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
        with captured():
            sync.add(api, work, res, dry_run=True, adder=adder, now=NOW)
        assert not os.path.exists(res) and seen[-1][4] is True
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
        res = os.path.join(tmp, "add_results.json")
        api = FakeApi()
        api.fail_next(None, "network", method="PUT", path="market:m-1")
        api.fail_next(503, "server", method="POST", path="market:m-2")
        api.fail_next(400, "bad_request", method="POST", path="market:m-3")
        api.fail_next(409, "full", method="POST", path="market:m-5")                          # the Worker's cap
        blobs.put_json(api, "market:m-4", {}, {"entry": {"name": "other", "slug": "o"}})        # the name taken
        with captured() as text:
            got = sync.add(api, work, res, adder=adder, now=NOW)
        assert [(r["condition_id"], r["status"], r["note"]) for r in got] == [
            ("0x3", "failed", "the database refused it (bad_request)"),
            ("0x4", "failed", "the database refused it (moved)"),
            ("0x5", "failed", sync.FULL)]
        assert "tried again next run" in text.getvalue()
        assert json.load(open(res))["results"] == got
        with tempfile.TemporaryDirectory() as job:
            _files(job, added_at=NOW, results=got)
            assert {r["condition_id"] for r in sync.done_payload(store.connect(":memory:"), job, "full", "success",
                                                                  NOW)["requests"]} == {"0x3", "0x4", "0x5"}
        calls = []
        counting = lambda *a, **k: calls.append(a) or built("x", "x")  # noqa: E731
        with captured() as text:
            assert sync.add(None, work, res, adder=counting, now=NOW) == []
        assert "No API configured" in text.getvalue() and json.load(open(res))["results"] == [] and calls == []
        api.fail_next(503, "server", method="GET", path="prefix=market:")
        with captured() as text:
            assert sync.add(api, work, res, adder=counting, now=NOW) == []
        assert "wait for the next run" in text.getvalue() and calls == []


def test_a_market_another_run_saved_meanwhile_is_reported_added():
    # an add run beside a daily run can build the same market: the second save is refused (409 moved, the name
    # taken), and when the market there is this one (its slug) it is reported added, not failed, and the deploy
    # is the other run's; a name taken by another market stays a failure
    work = {"requests": [{"condition_id": "0x1", "slug": "m-1", "name": None, "winner": "Yes", "status": "approved"},
                         {"condition_id": "0x2", "slug": "m-2", "name": None, "winner": "Yes", "status": "approved"}]}
    with tempfile.TemporaryDirectory() as tmp:
        res = os.path.join(tmp, "add_results.json")
        api = FakeApi()

        def adder(cid, slug, name, outcome, dry_run=False, added=()):
            got = built(slug, slug)
            # meanwhile the other run saves m-1 itself, and another market under the name m-2
            other = dict(got["entry"]) if slug == "m-1" else {"name": "m-2", "slug": "someone-else"}
            blobs.put_json(api, f"market:{slug}", {}, {"entry": other, "condition_id": cid})
            return got
        with captured() as text:
            got = sync.add(api, work, res, adder=adder, now=NOW)
        assert "another run saved it meanwhile" in text.getvalue()
    assert [(r["condition_id"], r["status"], r["name"], r["note"], r["saved"]) for r in got] == [
        ("0x1", "added", "m-1", "already on the main page", False),
        ("0x2", "failed", "m-2", "the database refused it (moved)", False)]


# ── what comes in during a run ───────────────────────────────────────────────

class WorkApi(FakeApi):
    """The fake API, also answering GET /api/job/work with `work` (as it is when asked)."""

    def __init__(self, work):
        super().__init__(now=NOW)
        self.work = work

    def request(self, method, path, body=None):
        got = super().request(method, path, body)          # records the call, raises a fail_next
        return copy.deepcopy(self.work) if (method, path) == ("GET", "/api/job/work") else got


def test_add_refresh_reads_the_markets_to_add_again():
    # the add step asks for the markets to add again, so one sent during the run's first hours is added too, one
    # another run added meanwhile is not, and the read's time is the step's read_at; the API not answering that
    # read: the ones the run read first are added
    seen = []

    def adder(cid, slug, name, outcome, dry_run=False, added=()):
        seen.append(cid)
        return built(name or slug, slug)

    first = {"requests": [{"condition_id": "0x1", "slug": "m-1", "name": None, "winner": "Yes", "status": "approved"}]}
    later = {"now": NOW + 3600, "watched": [],
             "requests": [{"condition_id": "0x2", "slug": "m-2", "name": None, "winner": "No", "status": "approved"}]}
    with tempfile.TemporaryDirectory() as tmp:
        res = os.path.join(tmp, "add_results.json")
        api = WorkApi(later)
        with captured():
            got = sync.add(api, first, res, adder=adder, now=NOW + 3600, refresh=True)
        assert seen == ["0x2"] and [(r["condition_id"], r["status"]) for r in got] == [("0x2", "added")]
        assert ("GET", "/api/job/work", None) in api.other
        # the API down for that read: work.json's are added, and no read_at
        seen.clear()
        api = WorkApi(later)
        api.fail_next(503, "server", method="GET", path="/api/job/work")
        with captured() as text:
            got = sync.add(api, first, res, adder=adder, now=NOW + 3600, refresh=True)
        assert seen == ["0x1"] and "could not be read again" in text.getvalue()
        # nothing approved when asked again: nothing is tried and the added markets are not even read
        seen.clear()
        api = WorkApi(dict(later, requests=[dict(later["requests"][0], status="added")]))
        with captured():
            assert sync.add(api, first, res, adder=adder, now=NOW + 3600, refresh=True) == []
        assert seen == [] and api.calls["GET"] == 1
        assert json.load(open(res))["results"] == []
        # the CLI passes --refresh through
        calls = []
        real_add, real_client = sync.add, sync.client
        sync.add = lambda *a, **k: calls.append(k) or []
        sync.client = lambda *a, **k: None
        try:
            with captured():
                sync.main(["add", "--work", os.path.join(tmp, "none.json"), "--refresh"])
                sync.main(["add", "--work", os.path.join(tmp, "none.json")])
        finally:
            sync.add, sync.client = real_add, real_client
        assert [k["refresh"] for k in calls] == [True, False]


# ── the site's watchlist: a watched market that closed goes to the main page ─

def gm(cid, question, end, result=None, closed_at=None, event="e1", vol=50_000):
    """A Gamma market: open with no closed_at; once closed, result "Yes" or "No", "50-50" (resolved with no
    winner) or "pending" (closed, not resolved yet)."""
    m = {"conditionId": cid, "question": question, "slug": f"m-{cid[2:]}", "endDate": end, "volumeNum": vol,
         "outcomes": '["Yes", "No"]', "events": [{"id": event, "slug": f"ev-{event}", "title": "T"}]}
    if closed_at is None:
        return dict(m, closed=False, outcomePrices='["0.4", "0.6"]')
    prices = {"Yes": '["1", "0"]', "No": '["0", "1"]', "50-50": '["0.5", "0.5"]', "pending": '["0.97", "0.03"]'}
    return dict(m, closed=True, closedTime=closed_at, outcomePrices=prices[result],
                umaResolutionStatus="proposed" if result == "pending" else "resolved")


D13, D14, D15 = "2026-11-13 12:08:10.08+00", "2026-11-14 17:50:42.09+00", "2026-11-15 09:00:00+00"


def test_a_watched_card_sends_the_market_it_is_about():
    choose = sync.watched_choice
    ids = lambda m: m and m["conditionId"]  # noqa: E731
    # one market: its result, whichever way it went; still open, closed with no clear result (a 50-50
    # resolution, or not resolved yet): it waits
    yes = gm("0x1", "Will Acme be sold?", "2026-12-01", "Yes", D14)
    no = gm("0x1", "Will Acme be sold?", "2026-12-01", "No", D14)
    assert ids(choose([yes])) == "0x1" and ids(choose([no])) == "0x1"
    for waits in ([gm("0x1", "Will Acme be sold?", "2026-12-01")], [gm("0x1", "Q?", "2026-12-01", "50-50", D14)],
                  [gm("0x1", "Q?", "2026-12-01", "pending", D14)], []):
        assert choose(waits) is None
    # a date ladder (Lutnick, 2026-11-14): November 13 passed with nothing happening while November 30 and
    # December 31 are open, and an outcome of another event split into the story is open too: the card goes on
    lutnick = [gm("0x13", "Howard Lutnick out by November 13?", "2026-11-14", "No", D13),
               gm("0x30", "Howard Lutnick out by November 30?", "2026-12-01"),
               gm("0x31", "Howard Lutnick out by December 31?", "2027-01-01"),
               gm("0xaa", "Will Howard Lutnick leave the Trump administration before 2027?", "2027-01-01",
                  event="e2")]
    assert choose(lutnick) is None
    # ...he leaves: the dates from November 30 resolve Yes on the same news; the shortest date is the real event
    # (the closed list's ladder rule: the same pick, so its Recently closed card follows it)
    left = lutnick[:1] + [gm(m["conditionId"], m["question"], m["endDate"], "Yes", D15, m["events"][0]["id"])
                          for m in lutnick[1:3]] + lutnick[3:]
    assert ids(choose(left)) == "0x30"
    from watchlist import upsets as up
    assert [m["conditionId"] for m in up.ladder_choice(left[:3])[0]] == ["0x30"]
    # ...and the split outcome of the other event too, on the same day: the most money traded, as the closed list
    # counts markets settled by the same news once
    both = left[:3] + [gm("0xaa", lutnick[3]["question"], "2027-01-01", "Yes", D15, "e2", vol=900_000)]
    assert ids(choose(both)) == "0xaa"
    # every date passed with nothing happening: once the last one has closed, its market (the card's final
    # answer); while one is still open, it waits; the last one without a clear result waits too
    nothing = [gm("0x13", "Acme deal by November 13?", "2026-11-14", "No", D13),
               gm("0x14", "Acme deal by November 14?", "2026-11-15", "No", D14)]
    assert ids(choose(nothing)) == "0x14" and choose(nothing + [gm("0x15", "Acme deal by Dec?", "2026-12-31")]) is None
    assert choose(nothing[:1] + [gm("0x14", "Acme deal by November 14?", "2026-11-15", "pending", D14)]) is None
    # "continues through": No means it happened, so the shortest date that resolved No
    truce = [gm("0x8", "US x Iran ceasefire continues through November 8?", "2026-11-09", "No", D14),
             gm("0x9", "US x Iran ceasefire continues through November 30?", "2026-12-01", "No", D14)]
    assert ids(choose(truce)) == "0x8"
    # an event where exactly one outcome wins (Eurovision Asia's jury winner, 2026-11-14): the winner, whatever
    # closed when
    jury = [gm(f"0x{i}", f"Will {c} be the jury winner?", "2026-11-15", "Yes" if c == "the Philippines" else "No",
               "2026-11-14 17:58:37+00" if c == "the Philippines" else D14) for i, c in
            enumerate(("Vietnam", "Thailand", "the Philippines", "Laos"), 1)]
    assert ids(choose(jury)) == "0x3"
    # the closed list's $10,000 floor first: a thin date that resolved Yes yields to one over it
    thin = [gm("0x1", "Acme deal by November 13?", "2026-11-14", "Yes", D14, vol=900),
            gm("0x2", "Acme deal by November 30?", "2026-12-01", "Yes", D14)]
    assert ids(choose(thin)) == "0x2" and ids(choose(thin[:1])) == "0x1"
    # what it sends: the closed pick's fields, from Polymarket's own
    req = sync.watched_request(left[1])
    assert req == {"condition_id": "0x30", "slug": "m-30", "question": "Howard Lutnick out by November 30?",
                   "url": "https://polymarket.com/event/ev-e1", "winner": "Yes", "closed": "2026-11-15T09:00:00+00:00",
                   "day": "2026-11-15"}


def test_a_watched_card_is_looked_up_by_its_events_and_split_outcomes():
    from pipeline import polymarket_api
    conn = store.connect(":memory:")
    base = {"place": "stays", "label": "listed", "listed_since": "2026-11-12"}
    store.save_open_day(conn, "2026-11-12", "updated", "2026-11-12T04:00:00Z",
                        [dict(base, id="lutnick", event_ids=["326604"], slugs=["ev-326604", "m-13", "m-30"]),
                         dict(base, id="other", event_ids=[], slugs=["solo"])])
    # the next day's card lists another outcome split into the story, and the events' own slugs again
    store.save_open_day(conn, "2026-11-13", "updated", "2026-11-13T04:00:00Z",
                        [dict(base, id="lutnick", event_ids=["326604", "404"], slugs=["ev-326604", "m-30", "leave",
                                                                                     "gone"])])
    assert store.open_story_idents(conn, "lutnick") == (["326604", "404"], ["ev-326604", "m-13", "m-30", "leave",
                                                                            "gone"])
    assert store.open_story_idents(conn, "nobody") == ([], [])
    asked = []

    def event(eid):
        asked.append(("event", eid))
        if eid == "404":
            raise polymarket_api.PolymarketAPIError("HTTP 404", status=404)
        return {"id": eid, "slug": "ev-326604", "title": "Lutnick out by...?",
                "markets": [{"conditionId": "0x13", "slug": "m-13"}, {"conditionId": "0x30", "slug": "m-30"},
                            {"conditionId": "0x31", "slug": "m-31"}]}

    def market(slug):
        asked.append(("market", slug))
        if slug == "gone":
            raise ValueError("Market not found on Gamma: gone")
        return {"conditionId": "0xaa", "slug": slug, "events": [{"id": "73897", "slug": "who-will-leave"}]}

    with captured() as text:
        got = sync.story_markets(conn, "lutnick", fetch_event=event, fetch_market=market)
    # every market of its event (December 31 too, which no card listed), the split outcome by its slug alone,
    # each event's slug on its markets (the link); an event or a market Polymarket no longer has is left out
    assert [m["conditionId"] for m in got] == ["0x13", "0x30", "0x31", "0xaa"]
    assert asked == [("event", "326604"), ("event", "404"), ("market", "leave"), ("market", "gone")]
    assert got[0]["events"] == [{"id": "326604", "slug": "ev-326604", "title": "Lutnick out by...?"}]
    assert got[3]["events"][0]["slug"] == "who-will-leave" and "no longer has its event 404" in text.getvalue()
    assert sync.story_markets(conn, "nobody", fetch_event=event, fetch_market=market) == []
    # Polymarket not answering raises (the story waits for the next run)
    def down(eid):
        raise polymarket_api.PolymarketAPIError("gave up on /events after 4 attempts", status=503)
    try:
        sync.story_markets(conn, "lutnick", fetch_event=down, fetch_market=market)
        raise AssertionError("not raised")
    except polymarket_api.PolymarketAPIError:
        pass


class RequestsApi(WorkApi):
    """The fake API, also holding the requests with the Worker's rules: POST /api/job/requests records each market
    sent as approved once (one there already is left as it is) and answers with their rows; GET /api/job/work
    lists the approved ones."""

    def __init__(self, work, rows=()):
        super().__init__(work)
        self.rows = {r["condition_id"]: dict(r) for r in rows}

    def request(self, method, path, body=None):
        got = super().request(method, path, body)
        if (method, path) == ("POST", "/api/job/requests"):
            for r in body["requests"]:
                self.rows.setdefault(r["condition_id"], dict(r, status="approved", name=None, note=None,
                                                             requested_at=self.now, decided_at=self.now, done_at=None))
            return {"requests": [copy.deepcopy(self.rows[r["condition_id"]]) for r in body["requests"]], "skipped": []}
        if (method, path) == ("GET", "/api/job/work"):
            return dict(got, requests=[copy.deepcopy(r) for r in self.rows.values() if r["status"] == "approved"])
        return got


def test_the_daily_run_adds_the_watched_markets_that_closed():
    from pipeline import polymarket_api
    seen = []

    def adder(cid, slug, name, outcome, dry_run=False, added=()):
        seen.append((cid, slug, outcome))
        return built(slug, slug, outcome)

    closed = {"won": [gm("0xa1", "Will Acme be sold?", "2026-12-01", "Yes", D14)],
              "lost": [gm("0xb1", "Acme deal by November 13?", "2026-11-14", "No", D13),
                       gm("0xb2", "Acme deal by November 14?", "2026-11-15", "No", D14)],
              "ladder": [gm("0xc1", "Globex out by November 13?", "2026-11-14", "No", D13),
                         gm("0xc2", "Globex out by November 30?", "2026-12-01")],
              "pending": [gm("0xd1", "Will Initech merge?", "2026-11-14", "pending", D14)],
              "main": [gm("0xe1", "Will Hooli be sold?", "2026-11-14", "Yes", D14)],
              "added": [gm("0xf1", "Will Pied Piper be sold?", "2026-11-14", "Yes", D14)],
              "failed": [gm("0x01", "Will Umbrella be sold?", "2026-11-14", "Yes", D14)],
              "same": [gm("0xa1", "Will Acme be sold?", "2026-12-01", "Yes", D14)]}
    looked = []

    def markets_of(story):
        looked.append(story)
        if story == "down":
            raise polymarket_api.PolymarketAPIError("gave up on /events/9 after 4 attempts", status=503)
        return copy.deepcopy(closed[story])

    stories = ["won", "lost", "ladder", "pending", "main", "added", "failed", "same", "down"]
    work = {"requests": [{"condition_id": "0x99", "slug": "visitor", "name": None, "winner": "Yes",
                          "status": "approved"}], "watched": [{"story": s} for s in stories] + [{"count": 2}]}
    # a visitor's add of 0x99 waits; the "failed" story's market was sent before and failed: left as it is
    rows = [dict(work["requests"][0], decided_at=NOW - 60),
            {"condition_id": "0x01", "slug": "m-01", "name": "umbrella", "winner": "Yes", "status": "failed"}]
    with tempfile.TemporaryDirectory() as tmp:
        res, markets = (os.path.join(tmp, n) for n in ("add_results.json", "markets.json"))
        json.dump([{"name": "hooli", "slug": "event-x/m-e1#3", "output": "Hooli"}], open(markets, "w"))
        api = RequestsApi({"now": NOW + 5, "watched": work["watched"]}, rows)
        blobs.put_json(api, "market:pied", {}, {"entry": {"name": "pied", "slug": "m-f1"}})
        real = sync.MARKETS_FILE
        sync.MARKETS_FILE = markets
        try:
            with captured() as text:
                got = sync.add(api, work, res, adder=adder, now=NOW + 5, refresh=True, watched=True,
                               markets_of=markets_of)
        finally:
            sync.MARKETS_FILE = real
        log = text.getvalue()
        # each story looked up once; sent: the one that resolved and the dated one whose last date passed (the
        # card's final answer), once each, with the closed pick's fields
        assert sorted(looked) == sorted(stories)
        sends = [b for m, p, b in api.log if (m, p) == ("POST", "/api/job/requests")]
        assert len(sends) == 1 and [r["condition_id"] for r in sends[0]["requests"]] == ["0x01", "0xb2", "0xa1"]
        assert sends[0]["requests"][2] == sync.watched_request(closed["won"][0])
        # ...before the markets to add are read again, so that read lists them with the visitor's, and all of them
        # are added in this run; the one that failed before is not added twice
        reads = [i for i, (m, p, _) in enumerate(api.log) if (m, p) == ("GET", "/api/job/work")]
        assert reads and reads[0] > api.log.index(("POST", "/api/job/requests", sends[0]))
        assert sorted(seen) == [("0x99", "visitor", "Yes"), ("0xa1", "m-a1", "Yes"), ("0xb2", "m-b2", "No")]
        assert {r["condition_id"]: r["status"] for r in got} == {"0x99": "added", "0xa1": "added", "0xb2": "added"}
        assert blobs.head(api, "market:m-a1")["meta"]["condition_id"] == "0xa1"
        # the stories done with: sent (a request of any status now) or on the main page already (markets.json's
        # and the added ones'); the open ladder, the one not resolved yet and the one Polymarket did not answer for
        # wait for the next day's look
        doc = json.load(open(res))
        assert sorted(doc["watched"]) == ["added", "failed", "lost", "main", "same", "won"]
        assert "the watched market 'down' could not be looked up" in log and "looked up again next run" in log
        assert "9 watched, 3 closed with a result to send to the main page, 2 on it already, 2 still open" in log
        with tempfile.TemporaryDirectory() as job:
            with open(os.path.join(job, "add_results.json"), "w") as f:
                json.dump(doc, f)
            body = sync.done_payload(store.connect(":memory:"), job, "full", "success", NOW)
        assert sorted(body["watched"]) == sorted(doc["watched"])
        assert {r["condition_id"] for r in body["requests"]} == {"0x99", "0xa1", "0xb2"}
        # the next day (the report marked them added): sent again, the API keeps each row as it is, so nothing is
        # added twice; they are still done with, for the report that takes them off once Open now drops them
        for r in body["requests"]:
            api.rows[r["condition_id"]]["status"] = r["status"]
        seen.clear()
        with captured():
            sync.MARKETS_FILE = markets
            try:
                again = sync.add(api, work, res, adder=adder, now=NOW + 86400, refresh=True, watched=True,
                                 markets_of=markets_of)
            finally:
                sync.MARKETS_FILE = real
        assert seen == [] and again == [] and sorted(json.load(open(res))["watched"]) == sorted(doc["watched"])
        assert {cid: r["status"] for cid, r in api.rows.items()} == {"0x99": "added", "0x01": "failed",
                                                                     "0xb2": "added", "0xa1": "added"}
        # without --refresh the rows the API answered are added; a dry run sends nothing
        api2 = RequestsApi({"now": NOW}, [])
        seen.clear()
        with captured():
            sync.add(api2, {"watched": [{"story": "won"}]}, res, adder=adder, now=NOW, watched=True,
                     markets_of=markets_of)
        assert seen == [("0xa1", "m-a1", "Yes")]
        api3 = RequestsApi({"now": NOW}, [])
        with captured():
            sync.add(api3, {"watched": [{"story": "won"}]}, res, adder=adder, now=NOW, watched=True,
                     markets_of=markets_of, dry_run=True)
        assert not [p for m, p, _ in api3.log if m == "POST"] and seen[-1][0] == "0xa1"
        # the API not taking them: the visitor's adds still go, and the next daily run sends them
        api4 = RequestsApi({"now": NOW}, [])
        api4.fail_next(503, "server", method="POST", path="/api/job/requests")
        seen.clear()
        with captured() as text:
            got = sync.add(api4, {"requests": work["requests"], "watched": [{"story": "won"}]}, res, adder=adder,
                           now=NOW, watched=True, markets_of=markets_of)
        assert seen == [("0x99", "visitor", "Yes")] and "the next daily run sends them" in text.getvalue()
        assert json.load(open(res))["watched"] == []
        # the added markets unreadable: nothing is looked up or sent
        api5 = RequestsApi({"now": NOW}, [])
        api5.fail_next(503, "server", method="GET", path="prefix=market:")
        looked.clear()
        with captured() as text:
            assert sync.add(api5, work, res, adder=adder, now=NOW, watched=True, markets_of=markets_of) == []
        assert looked == [] and "wait for the next run" in text.getvalue()
        # the CLI: --watched with the store it reads the cards from
        calls = []
        real_add, real_client = sync.add, sync.client
        sync.add = lambda *a, **k: calls.append(k) or []
        sync.client = lambda *a, **k: None
        try:
            with captured():
                sync.main(["add", "--work", os.path.join(tmp, "none.json"), "--refresh", "--watched", "--db",
                           os.path.join(tmp, "s.db")])
                sync.main(["add", "--work", os.path.join(tmp, "none.json"), "--refresh"])
        finally:
            sync.add, sync.client = real_add, real_client
        assert [(k["watched"], k["conn"] is not None) for k in calls] == [(True, True), (False, False)]


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
