#!/usr/bin/env python3
"""
Offline tests for how the daily job lives through what goes wrong outside its
code (watchlist.problems, and the retries and fallbacks in the steps): no
network, fake services, fake sleeps.

    python3 tests/test_watchlist_problems.py

A run ends green unless a person must fix something. Covers each failure mode
the job must survive (the model busy for a whole run, Google News down,
Polymarket answering 429s and timing out, the site's API down while the
state is restored, saved and reported, no saved state in a fresh database,
one list failing or crashing, a failed add, a step stopped at its time
limit, PyPI down) and the ones that must fail the run (a refused job token, a
used database that lost its state, a crash with its traceback, a crash while
adding a market, a model the code names gone, a request a service refuses,
requirements.txt failing while PyPI answers), what each error means, the
report's status and summary, and a run started hours late.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from pipeline import polymarket_api  # noqa: E402
import fake_api  # noqa: E402
from fake_api import FakeApi  # noqa: E402
from backend import contract as C  # noqa: E402

# the fake API takes the backend's blob names (site:*, scratch:*) as the Worker does (CONTRACT.blob_names)
fake_api.BLOB_NAME = re.compile("|".join(f"(?:{p})" for p in C.CONTRACT["blob_names"].values()))
fake_api.HISTORY = re.compile(r"^(state|site:(index|closed|open)\.json)$")
fake_api.PREFIX = re.compile(r"^[a-z0-9:._/-]{0,40}$")
from watchlist import ai, judge, net, news, opennow as O, problems as P  # noqa: E402
from watchlist import state as S, store, sync, upsets as up  # noqa: E402
from watchlist.common import DAY, Budget, now_epoch, to_epoch  # noqa: E402

NOW = to_epoch("2026-11-16T04:17:00Z")
REAL_CLIENT = ai.Client
TOKEN = "job-token-that-must-never-be-printed"
KEY = "gemini-key-that-must-never-be-printed"


@contextlib.contextmanager
def captured():
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        yield out


os.environ["GEMINI_API_KEY"] = KEY            # a fake key: no test reaches Gemini


class swap:
    """Set module attributes for a block and put them back: swap((module, "name", value), ...)."""

    def __init__(self, *swaps):
        self.swaps = swaps

    def __enter__(self):
        self.saved = [(m, n, getattr(m, n)) for m, n, _ in self.swaps]
        for m, n, v in self.swaps:
            setattr(m, n, v)

    def __exit__(self, *exc):
        for m, n, v in reversed(self.saved):
            setattr(m, n, v)


def records(job):
    with open(os.path.join(job, P.FILE)) as f:
        return json.load(f)


def verdict(started_at, outcomes, job_dir=None, broken=()):
    """(1 when a person must fix something, else 0; this run's problems), as the report reads them."""
    found = P.collect(started_at, outcomes, job_dir, broken)
    return (1 if P.red(found) else 0), found


def market(cid, question="Will Jane Roe resign as Secretary of Widgets by November 30?"):
    return {"id": cid, "conditionId": cid, "question": question, "slug": f"m-{cid}",
            "endDate": "2026-11-30T00:00:00Z", "closedTime": "2026-11-15T20:00:00Z", "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]', "umaResolutionStatus": "resolved", "volumeNum": 50_000,
            "events": [{"id": f"e-{cid}", "slug": f"ev-{cid}", "title": question}]}


class Google503:
    """A session on which every Gemini request answers 503 (the real ai.Client's retries then give up)."""

    def __init__(self, status=503, message="The model is overloaded. Please try again later."):
        self.status, self.message, self.calls = status, message, 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        status, message = self.status, self.message

        class R:
            status_code, headers, text = status, {}, message

            def json(self):
                return {"error": {"message": message}}
        return R()


class BusyModel:
    """Every model request answers 503 (a whole run's "high demand")."""

    models = ("fake",)

    def __init__(self, status=503):
        self.status, self.prompts, self.skip = status, [], set()

    def generate(self, prompt, schema=None):
        self.prompts.append(prompt)
        raise ai.GeminiError(f"fake: HTTP {self.status}: busy", status=self.status, model="fake")


# ── the guard and the run's problems ────────────────────────────────────────────────

def test_an_outside_exception_is_a_handled_problem():
    with tempfile.TemporaryDirectory() as job, captured() as out:
        def walk():
            raise polymarket_api.PolymarketAPIError("gave up on https://gamma-api.polymarket.com after 5 attempts: "
                                                   "HTTP 429", status=429)
        assert P.guard("closed", walk, job_dir=job) == 0
        doc = records(job)
        assert doc["steps"]["closed"]["state"] == "done" and doc["steps"]["closed"]["code"] == 0
        [p] = doc["problems"]
        assert p["kind"] == "outside" and p["step"] == "closed"
        assert p["message"].startswith("the closed list was not finished: Polymarket did not answer")
        assert p["message"].endswith("it keeps its last version")
        code, found = verdict(NOW - 60, {"closed": "success", "open": "success"}, job)
        assert code == 0 and found == doc["problems"]
    for e in (news.NewsError("news search: HTTP 503", transient=True), requests.ConnectionError("reset"),
              ai.GeminiError("fake: HTTP 503", status=503), sync.ApiError(503, "server"),
              sync.ApiError(None, "network")):
        assert P.classify(e) == "outside", e


def test_a_crash_fails_the_run_with_its_traceback():
    with tempfile.TemporaryDirectory() as job, captured() as out:
        def broken():
            return {}["missing"]
        assert P.guard("closed", broken, job_dir=job) == 1
        text = out.getvalue()
        assert "Traceback (most recent call last)" in text and "KeyError: 'missing'" in text
        assert records(job)["steps"]["closed"]["state"] == "crashed"
        # the other list still ran and published; the run fails, with one problem naming the crash
        assert P.guard("open", lambda: 0, job_dir=job) == 0
        code, found = verdict(NOW - 60, {"closed": "failure", "open": "success"}, job)
        assert code == 1 and [p["kind"] for p in found] == ["crash"]
        assert "the closed list crashed: KeyError" in found[0]["message"]
    for e in (KeyError("x"), polymarket_api.RetiredEndpointError("410 retired"), sync.ApiError(400, "bad_request")):
        assert P.classify(e) == "crash", e
    for e in (ai.GeminiError("fake: HTTP 403", status=403), ai.GeminiError("not set", config=True),
              sync.ApiError(401, "unauthorized"), sync.ApiError(429, "locked")):
        assert P.classify(e) == "config", e


def test_steps_stopped_never_started_or_not_installed():
    with tempfile.TemporaryDirectory() as job, captured():
        # told to stop (the step's time limit sends SIGINT, then SIGTERM): recorded, an outside problem
        def slow():
            raise P.Stopped("SIGTERM")
        assert P.guard("open", slow, job_dir=job) == 1
        assert records(job)["steps"]["open"]["state"] == "stopped"
        # killed with no time to record (SIGKILL): "running" with a failed outcome is the time limit too
        P._active.update(step=None, dir=job)
        P._mark("save", state="running", at=NOW)
        P._active.update(step=None, dir=None)
        found = P.collect(NOW - 60, {"open": "failure", "save": "failure", "install": "failure",
                                     "closed": "skipped"}, job)
        assert {p["step"]: p["kind"] for p in found} == {"open": "outside", "save": "outside", "install": "outside"}
        assert any("stopped at its time limit" in p["message"] for p in found)
        assert verdict(NOW - 60, {"open": "failure", "save": "failure", "install": "failure"}, job)[0] == 0
        # a step that failed without ever reaching its guard (an import error): the code, so the run fails
        found = P.collect(NOW - 60, {"catalog": "failure"}, job)
        assert [(p["step"], p["kind"]) for p in found if p["step"] == "catalog"] == [("catalog", "crash")]
        assert verdict(NOW - 60, {"catalog": "failure"}, job)[0] == 1
        # an older run's records never count
        assert P.collect(now_epoch() + 3600, {"open": "success"}, job) == []


def test_note_outside_a_step_only_logs():
    with tempfile.TemporaryDirectory() as job, captured() as out:
        P.note("the news did not answer")
        assert not os.path.exists(os.path.join(job, P.FILE))
        assert "  ! the news did not answer" in out.getvalue()


def test_what_each_error_means():
    PE = polymarket_api.PolymarketAPIError
    # Polymarket: retries spent, busy or turning the machine away now -> outside; a request it calls wrong, an
    # error payload or an answer the code cannot read -> the code
    for e in (PE("gave up on https://gamma-api.polymarket.com after 5 attempts: timed out"),
              PE("gave up on https://x after 5 attempts: https://x answered with non-JSON: <html>", status=200),
              PE("HTTP 503", status=503), PE("HTTP 429", status=429), PE("HTTP 403 (Cloudflare)", status=403)):
        assert P.classify(e) == "outside", e
    for e in (PE("HTTP 400 from https://x: bad param", status=400), PE("HTTP 404 from https://x", status=404),
              PE("https://x answered with an error payload: nope", status=200),
              PE("/v2/trades: unexpected response shape: []"), polymarket_api.RetiredEndpointError("410", status=410)):
        assert P.classify(e) == "crash", e
    # the network: a connection or a timeout -> outside; a URL or an answer the code reads wrong -> the code
    for e in (requests.ConnectionError("reset"), requests.Timeout("slow"), requests.exceptions.SSLError("eof"),
              requests.exceptions.ChunkedEncodingError("cut")):
        assert P.classify(e) == "outside", e
    for e in (requests.exceptions.InvalidURL("x"), requests.exceptions.MissingSchema("x"),
              requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)):
        assert P.classify(e) == "crash", e
    # the model: busy -> outside; a request it refused (a plain 400) -> the code; the key -> the setup
    assert P.classify(ai.GeminiError("fake: HTTP 503", status=503)) == "outside"
    assert P.classify(ai.GeminiError("fake: HTTP 400: Unknown parameter", status=400, refused=True)) == "crash"
    # an error raised from another (a step that wraps what failed under it) is read as the one it wraps
    for cause, kind in ((PE("gave up on https://x after 5 attempts: HTTP 502", status=502), "outside"),
                        (KeyError("price"), "crash")):
        try:
            try:
                raise cause
            except Exception as c:
                raise RuntimeError("prior history of 0xabc could not be fetched") from c
        except RuntimeError as e:
            assert P.classify(e) == kind, (cause, kind)
    # a walk is not asked again when Polymarket says the request is wrong (waiting will not change that)
    calls, sleeps = [], []

    def wrong():
        calls.append(1)
        raise PE("HTTP 404 from https://gamma-api.polymarket.com/markets/keyset", status=404)
    with captured():
        try:
            net.patiently(wrong, sleep=sleeps.append)
            raise AssertionError("no error")
        except PE:
            pass
    assert calls == [1] and sleeps == []


def test_a_gone_model_is_the_codes_problem_and_said_once():
    class Gone:
        def __init__(self, status, message):
            self.status, self.message, self.calls = status, message, []

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append(json["model"])
            status, message = self.status, self.message

            class R:
                status_code, headers, text = status, {}, message

                def json(self):
                    return {"error": {"message": message}}
            return R()
    with tempfile.TemporaryDirectory() as job, captured(), swap((ai, "_GONE", set())):
        def run():
            for _ in range(2):                   # two clients in one step (the judge makes two): said once
                c = REAL_CLIENT(models=("gemini-retired",), session=Gone(404, "models/gemini-retired is not found"),
                                sleep=lambda s: None, min_interval=0)
                try:
                    c.generate("hello")
                except ai.GeminiError:
                    pass
            # a spent day's quota is no fault of the code
            c = REAL_CLIENT(models=("gemini-busy",), session=Gone(429, "Quota exceeded: requests per day"),
                            sleep=lambda s: None, min_interval=0)
            try:
                c.generate("hello")
            except ai.GeminiError:
                pass
            assert c.skip == {"gemini-busy"}
        assert P.guard("closed", run, job_dir=job) == 0
        found = records(job)["problems"]
        assert [(p["kind"], p["message"][:32]) for p in found] == [("crash", "the model gemini-retired is gone")]
        assert verdict(NOW - 60, {"closed": "success"}, job)[0] == 1
    # the open list: every step reading refused, or every one of 3 or more answering no headline, is the code's
    assert O.down_kind("step reading: the request was refused (all 4 readings failed)") == "crash"
    assert O.down_kind("step reading: the answer answered no headline (all 6 readings failed)") == "crash"
    assert O.down_kind("step reading: the answer answered no headline (all 2 readings failed)") == "outside"
    assert O.down_kind("step reading: the model was busy or down") == "outside"
    assert O.down_kind("readings: the key was refused: HTTP 403") == "config"
    # the judge: a refused request is not asked again (the same request is refused the same way), nor is a
    # client whose every model is gone or spent for the day
    conn = store.connect(":memory:")
    for model, kind in ((BusyModel(400), "crash"), (BusyModel(429), None)):
        if kind is None:
            model.skip = set(model.models)             # every model given up on for the day
        else:
            model.generate = lambda prompt, schema=None, m=model: (
                m.prompts.append(prompt),
                (_ for _ in ()).throw(ai.GeminiError("fake: HTTP 400: bad", status=400, model="fake", refused=True)))[1]
        sleeps = []
        with tempfile.TemporaryDirectory() as job, captured(), \
                swap((news, "search", lambda q, after=None, before=None, limit=15: [])):
            P.guard("closed", lambda: judge.check_markets([market(f"0x{i:064x}") for i in range(5)], conn, model,
                                                          sleep=sleeps.append) and 0, job_dir=job)
            judged = [p for p in model.prompts if "search query" not in p]
            assert len(judged) == 1 and sleeps == [], (kind, len(judged), sleeps)
            kinds = [p["kind"] for p in records(job)["problems"]] if os.path.exists(os.path.join(job, P.FILE)) else []
            assert kinds == ([kind] if kind else []), kinds


def test_each_problem_is_recorded_once_and_the_record_is_safe():
    import threading
    with tempfile.TemporaryDirectory() as job, captured():
        def crash():
            P.note("the news did not answer")
            return {}["missing"]
        assert P.guard("closed", crash, job_dir=job) == 1
        code, found = verdict(NOW - 60, {"closed": "failure"}, job)
        assert code == 1 and [p["kind"] for p in found] == ["outside", "crash"]
    # many threads noting at once (the open list's readers): every problem is kept
    with tempfile.TemporaryDirectory() as job, captured():
        def many():
            ts = [threading.Thread(target=lambda i=i: [P.note(f"problem {i}.{j}") for j in range(10)])
                  for i in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        assert P.guard("open", many, job_dir=job) == 0
        assert len(records(job)["problems"]) == 80
        assert not [f for f in os.listdir(job) if f.endswith(".tmp")]
        # a week-old problem leaves the file when it is next saved (it lasts between runs on the Mac)
        doc = records(job)
        doc["problems"].append({"step": "open", "kind": "outside", "message": "old", "at": now_epoch() - 8 * DAY})
        P._save(os.path.join(job, P.FILE), doc)
        assert "old" not in [p["message"] for p in records(job)["problems"]]


def test_a_broken_install_fails_the_run_but_pypi_down_does_not():
    with tempfile.TemporaryDirectory() as job, captured():
        code, found = verdict(NOW - 60, {"install": "failure", "pull": "skipped"}, job)
        assert code == 0 and found[0]["kind"] == "outside"
        code, found = verdict(NOW - 60, {"install": "failure", "pull": "skipped"}, job, broken=["install"])
        assert code == 1 and found[0]["kind"] == "crash" and "requirements.txt" in found[0]["message"]


# ── the model busy for a whole run ───────────────────────────────────────────

def test_a_model_503_storm_leaves_the_closed_lists_markets_for_later():
    conn = store.connect(":memory:")
    sleeps = []
    found = [{"title": "Jane Roe resigns", "source": "AP", "published": "2026-11-15T19:00:00Z"}]
    ms = [market(f"0x{i:064x}") for i in range(30)]                          # 6 batches of 5
    with swap((news, "search", lambda q, after=None, before=None, limit=15: list(found))), captured() as out:
        model = BusyModel()
        got = judge.check_markets(ms, conn, model, sleep=sleeps.append)
    assert got == {} and not any(store.get_closed_check(conn, m["conditionId"], judge.PROMPT_VERSION) for m in ms)
    # the first batch asked JUDGE_TRIES times with growing waits, the second once more after the run's one long
    # pause; still busy then, the model is down and nothing more is asked
    judged = [p for p in model.prompts if "search query" not in p]
    assert len(judged) == 2 * judge.JUDGE_TRIES
    assert sleeps[:3] == [judge.BUSY_WAIT, 3 * judge.BUSY_WAIT, judge.BUSY_WAIT] and sleeps[3] >= 300, sleeps
    assert "the model is down" in out.getvalue()
    # the queries fell back to the questions themselves: the news was still searched
    assert judge.fallback_query(ms[0]) in judge.write_queries(ms[:1], BusyModel()).values()


def test_a_closed_list_run_in_a_storm_is_written_and_reported():
    conn = store.connect(":memory:")
    ms = [market(f"0x{i:064x}", f"Will Person{i} Name{i} resign by November 30?") for i in range(3)]
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as job, captured():
        out = os.path.join(tmp, "closed.json")
        busy = BusyModel()
        with swap((up.net, "install", lambda: None),
                  (up.net, "closed_markets", lambda since, now, min_volume=0: list(ms)),
                  (up.ai, "Client", lambda *a, **k: REAL_CLIENT(session=Google503(), sleep=lambda s: None,
                                                                 min_interval=0)),
                  (judge, "BUSY_WAIT", 0),
                  (news, "search", lambda q, after=None, before=None, limit=15: [])):
            code = P.guard("closed", lambda: up.build(days=2, now=NOW, conn=conn, out_path=out, client=busy),
                           job_dir=job)
        assert code == 0
        doc = json.load(open(out))
        # NOW is 23:17 on the 15th in Toronto: the 15th (the markets' day) and the 14th
        assert [d["day"] for d in doc["days"]] == ["2026-11-15", "2026-11-14"]
        assert doc["days"][0]["dropped"].get("could not be checked") == 3
        msgs = [p["message"] for p in records(job)["problems"]]
        assert any("3 market(s) of the closed list got no AI label" in m for m in msgs), msgs
        assert any("could not check 3 market(s)" in m for m in msgs)
        assert all(p["kind"] == "outside" for p in records(job)["problems"])
        assert verdict(NOW - 60, {"closed": "success"}, job)[0] == 0


def test_the_open_list_down_for_the_model_is_reported_not_failed():
    import test_watchlist_opennow as T
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "open.json")
        with captured():
            first = T._run(conn, T.NOW, T.FakeClient(), out)
            s = T._run(conn, T.NOW + DAY, T.FakeClient(down=True), out)          # the next day, the model busy
        assert first["status"] == O.STATUS_UPDATED and O.summary_problems(first) == []
        assert s["status"] == O.STATUS_STALE
        [(kind, msg)] = O.summary_problems(s)
        assert kind == "outside" and msg.startswith("the open list was not updated today (")
        assert json.load(open(out))["days"][0]["status"] == O.STATUS_STALE       # yesterday's live cards, kept
        # a first run with the model busy: the naming stops part way, which is reported too
        with captured():
            fresh = T._run(store.connect(":memory:"), T.NOW, T.FakeClient(down=True), os.path.join(tmp, "o2.json"))
        [(kind, msg)] = O.summary_problems(fresh)
        assert kind == "outside" and "naming stopped part way" in msg
    # a refused key is the setup's (ModelDown's own words); a spent allowance is not
    refused = {"status": O.STATUS_STALE, "model_down": "readings: the key was refused: HTTP 403"}
    assert O.summary_problems(refused)[0][0] == "config"
    assert O.summary_problems({"status": O.STATUS_STALE,
                               "model_down": "step reading: the key was refused or no model is left today"})[0][0] \
        == "outside"
    assert O.summary_problems({"status": O.STATUS_UPDATED, "model_down": None}) == []


# ── Google News down ─────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_news_errors_say_whether_they_pass():
    class Session:
        def __init__(self, answer):
            self.answer = answer

        def get(self, url, params=None, timeout=None):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
    with swap((news, "MIN_INTERVAL", 0)):
        for answer, transient in ((Resp(503), True), (Resp(429), True), (requests.Timeout("slow"), True),
                                  (Resp(404), False), (Resp(200, "<html>consent</html>"), False)):
            try:
                news.search("q", session=Session(answer))
                raise AssertionError("no error")
            except news.NewsError as e:
                assert e.transient is transient, (answer, e)


def test_news_down_is_asked_once_more_then_given_up():
    conn = store.connect(":memory:")
    asked, sleeps = [], []

    def down(q, after=None, before=None, limit=15):
        asked.append(q)
        raise news.NewsError("news search: HTTP 503", transient=True)

    ms = [market(f"0x{i:064x}") for i in range(judge.NEWS_DOWN_AFTER + 5)]
    with swap((news, "search", down)), captured() as out:
        got = judge.check_markets(ms, conn, BusyModel(), sleep=sleeps.append)
    assert got == {}
    # each market's search asked twice, NEWS_RETRY_WAIT apart, until NEWS_DOWN_AFTER markets in a row failed
    assert len(asked) == 2 * judge.NEWS_DOWN_AFTER and sleeps == [judge.NEWS_RETRY_WAIT] * judge.NEWS_DOWN_AFTER
    assert "5 market(s) not searched this run" in out.getvalue() and "the news is down" in out.getvalue()
    # a search that fails for a moment and then answers costs one wait
    answers = [news.NewsError("news search failed: ConnectionError", transient=True), []]

    def flaky(q, after=None, before=None, limit=15):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    sleeps.clear()
    with swap((news, "search", flaky)), captured():
        assert judge._headlines(ms[0], "q", sleeps.append) == [] and sleeps == [judge.NEWS_RETRY_WAIT]


def test_the_closed_list_budget_stops_new_requests():
    conn = store.connect(":memory:")
    asked = []
    with swap((news, "search", lambda *a, **k: asked.append(a) or [])), captured() as out:
        model = BusyModel()
        assert judge.check_markets([market("0x" + "a" * 64)], conn, model, budget=Budget(0)) == {}
    assert not asked and not model.prompts and "time budget is spent" in out.getvalue()


# ── Polymarket 429s and timeouts ─────────────────────────────────────────────

def test_polymarket_get_json_waits_out_429s_and_timeouts():
    class Session:
        def __init__(self, answers):
            self.answers = list(answers)

        def get(self, url, params=None, timeout=None):
            a = self.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

    class R:
        def __init__(self, status, body, headers=None):
            self.status_code, self._body, self.headers = status, body, headers or {}
            self.ok, self.text = status < 400, json.dumps(body)

        def json(self):
            return self._body

    sleeps = []
    with swap((polymarket_api, "SESSION", Session([requests.Timeout("slow"), R(429, {"error": "rate limited"},
                                                                              {"Retry-After": "3"}),
                                                  R(503, {"error": "busy"}), R(200, {"markets": []})])),
              (polymarket_api.time, "sleep", sleeps.append)):
        assert polymarket_api.get_json("https://gamma-api.polymarket.com/markets") == {"markets": []}
    assert sleeps[1] == 3.0 and len(sleeps) == 3


def test_a_walk_polymarket_keeps_refusing_leaves_the_list_as_it_was():
    sleeps, calls = [], []

    def refusing():
        calls.append(1)
        raise polymarket_api.PolymarketAPIError("gave up after 5 attempts: HTTP 429", status=429)
    with captured():
        try:
            net.patiently(refusing, "closed list", waits=(30, 120, 300), sleep=sleeps.append)
            raise AssertionError("no error")
        except polymarket_api.PolymarketAPIError:
            pass
    assert len(calls) == 4 and sleeps == [30, 120, 300]
    # it comes back on the third try: the walk goes on
    answers = [polymarket_api.PolymarketAPIError("timeout"), polymarket_api.PolymarketAPIError("503"), ["ok"]]

    def flaky():
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    sleeps.clear()
    with captured():
        assert net.patiently(flaky, sleep=sleeps.append) == ["ok"] and sleeps == list(net.WALK_WAITS[:2])
    # an endpoint Polymarket retired raises at once (a code change, not an outage)
    with captured():
        try:
            net.patiently(lambda: (_ for _ in ()).throw(polymarket_api.RetiredEndpointError("410")),
                          sleep=sleeps.append)
            raise AssertionError("no error")
        except polymarket_api.RetiredEndpointError:
            pass
    # the closed list: closed.json is left exactly as it was, and the run stays green
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as job, captured():
        out = os.path.join(tmp, "closed.json")
        last = '{"generated_at": "2026-11-15T04:30:00+00:00", "days": [{"day": "2026-11-14", "picks": []}]}'
        with open(out, "w") as f:
            f.write(last)
        with swap((up.net, "install", lambda: None), (up.net, "closed_markets",
                                                      lambda *a, **k: refusing()), (net, "WALK_WAITS", (0, 0))):
            assert P.guard("closed", lambda: up.build(days=2, now=NOW, conn=store.connect(":memory:"),
                                                       out_path=out, sleep=lambda s: None), job_dir=job) == 0
        assert open(out).read() == last
        assert "Polymarket did not answer" in records(job)["problems"][0]["message"]
    # the open list: Run's walk asks again, and a walk that never answers changes nothing
    events = [{"id": "1"}]
    tries = [polymarket_api.PolymarketAPIError("HTTP 500"), events]

    def walk(min_volume=None):
        a = tries.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    waited = []
    with swap((net, "install", lambda: None), (net, "walk_open_events", walk)), captured():
        assert O.Run(store.connect(":memory:"), NOW, sleep=waited.append)._walk_gamma() == events
    assert waited == [net.WALK_WAITS[0]]
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as job, captured():
        out = os.path.join(tmp, "open.json")
        with open(out, "w") as f:
            f.write('{"days": ["last good"]}')
        code = P.guard("open", lambda: O.daily(conn=conn, now=NOW, client=BusyModel(), walk=refusing, out_path=out,
                                               news_cache_path=None, sleep=lambda s: None), job_dir=job)
        assert code == 0 and open(out).read() == '{"days": ["last good"]}' and not store.open_days(conn)
        assert records(job)["problems"][0]["message"].startswith("the open list was not finished: Polymarket")


# ── the site's API down: restoring, saving, reporting ────────────────────────

class place:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        for d in ("site", "job"):
            os.makedirs(os.path.join(t, d))
        return {"site_dir": os.path.join(t, "site"), "job_dir": os.path.join(t, "job")}

    def __exit__(self, *exc):
        self.tmp.cleanup()


def state_cli(cmd, api, kw):
    """watchlist.state's CLI as the job runs it (inside the guard), against api, its files (state_pulled.json,
    state_pushed.json) in kw's job folder, never the real data/watchlist/."""
    pull, push = S.pull, S.push
    with swap((sync, "client", lambda *a, **k: api), (S, "pull", lambda *a, **k: pull(*a, **dict(k, job_dir=kw["job_dir"]))),
              (S, "push", lambda *a, **k: push(*a, **dict(k, job_dir=kw["job_dir"])))):
        args = [cmd, "--db", os.path.join(kw["job_dir"], "w.db"), "--site-dir", kw["site_dir"]]
        return P.guard(S.STEP_NAMES[cmd], lambda: S.main(args), job_dir=kw["job_dir"])


def test_the_api_down_while_restoring_saving_and_reporting():
    api = FakeApi()
    with place() as kw, captured():
        seeded = store.connect(":memory:")
        store.save_filing(seeded, "acme", None, None, "rule")
        with captured():
            S.push(seeded, api, seed=True, now=NOW - DAY, **kw)
        # restoring: the API still down after its retries -> not restored (the lists are not made), green
        api.fail_next(503, "server", method="GET")
        assert state_cli("pull", api, kw) == 2
        [p] = records(kw["job_dir"])["problems"]
        assert p["kind"] == "outside" and p["step"] == "pull" and "could not be downloaded" in p["message"]
        code, found = verdict(NOW - 60, {"pull": "failure", "closed": "skipped", "open": "skipped"}, kw["job_dir"])
        assert code == 0 and P.status("success", {"pull": "failure"}, found) == "failure"
        # restored, then saving: the API down -> not saved, the next run redoes the work, green
        assert state_cli("pull", api, kw) == 0
        api.fail_next(None, "network", method="GET")
        assert state_cli("push", api, kw) == 3
        assert records(kw["job_dir"])["problems"][-1]["kind"] == "outside"
        assert verdict(NOW - 60, {"pull": "success", "save": "failure"}, kw["job_dir"])[0] == 0
    # reporting: the catalog and the report not taken -> warned about and recorded, never raised
    conn = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as job, captured() as out:
        down = FakeApi()
        for _ in range(4):
            down.fail_next(503, "server")
        body = P.guard("report", lambda: sync.push(down, conn, job, "full", "success", NOW - 60, now=NOW) and 0,
                       job_dir=job)
        assert body == 0 and "the run's report was not sent" in out.getvalue()
        assert {p["kind"] for p in records(job)["problems"]} == {"outside"}
    # the client waits STEP_WAITS for each call before giving up, and a refused token is never retried
    waits = []

    class S503:
        status_code, content = 503, b'{"error": "server"}'

        def json(self):
            return {"error": "server"}

    class Locked:
        status_code, content = 429, b'{"error": "locked"}'

        def json(self):
            return {"error": "locked"}

    class Session:
        def __init__(self, answer):
            self.answer, self.calls = answer, 0

        def request(self, *a, **k):
            self.calls += 1
            return self.answer
    c = sync.Client("https://api.example", TOKEN, session=Session(S503()), sleep=waits.append,
                    retry_waits=sync.STEP_WAITS)
    try:
        c.request("GET", "/api/job/work")
    except sync.ApiError as e:
        assert e.outage and not e.refused
    assert waits == list(sync.STEP_WAITS) and c.session.calls == len(sync.STEP_WAITS) + 1
    c = sync.Client("https://api.example", TOKEN, session=Session(Locked()), sleep=waits.append)
    try:
        c.request("GET", "/api/job/work")
    except sync.ApiError as e:
        assert e.refused and not e.outage and c.session.calls == 1


def test_a_report_with_the_api_down_spends_its_waits_on_the_report():
    # the added markets not read (the API down): the catalog is not sent, so the report gets the step's time
    conn = store.connect(":memory:")
    store.save_open_day(conn, "2026-11-16", "updated", NOW, [], info={"new_today": 0})
    with tempfile.TemporaryDirectory() as job, captured() as out:
        api = FakeApi()
        api.fail_next(503, "server", method="GET", path="/api/job/blobs")
        assert P.guard("report", lambda: sync.push(api, conn, job, "full", "success", NOW - 60, now=NOW) and 0,
                       job_dir=job) == 0
        paths = [p for _, p, _ in api.log]
        assert paths == ["/api/job/blobs?prefix=market:", "/api/job/done"], paths
        msgs = [p["message"] for p in records(job)["problems"]]
        assert any(m.startswith("the markets added before could not be read") for m in msgs)
        assert any(m.startswith("the catalog was not sent (the API did not answer)") for m in msgs)
        # the report that went through says so: partial, with the problems after its summary
        done = api.log[-1][2]
        assert done["run"]["status"] == "partial" and "problems: the markets added before" in done["run"]["summary"]
        assert "problems" not in done and "reported (partial)" in out.getvalue()


def test_the_work_and_a_refused_token_mid_run():
    with tempfile.TemporaryDirectory() as job, captured():
        down = FakeApi()
        down.fail_next(503, "server")
        assert P.guard("work", lambda: sync.pull(down, os.path.join(job, "work.json"), now=NOW) and 0,
                       job_dir=job) == 0
        assert json.load(open(os.path.join(job, "work.json")))["requests"] == []
        assert records(job)["problems"][0]["kind"] == "outside"
        refused = FakeApi()
        refused.fail_next(401, "unauthorized")
        assert P.guard("work", lambda: sync.pull(refused, os.path.join(job, "work.json"), now=NOW), job_dir=job) == 1
        assert records(job)["problems"][-1]["kind"] == "config"
        assert verdict(NOW - 60, {"work": "failure"}, job)[0] == 1


def test_no_saved_state_starts_fresh_under_the_guard():
    api = FakeApi()
    with place() as kw, captured():
        assert state_cli("pull", api, kw) == 0
        [p] = records(kw["job_dir"])["problems"]
        assert p["kind"] == "outside" and "No saved state in the database" in p["message"]
        assert verdict(NOW - 60, {"pull": "success"}, kw["job_dir"])[0] == 0
        assert state_cli("push", api, kw) == 0 and S.blobs.head(api, S.STATE) is None    # nothing changed yet


def test_a_used_database_without_its_state_fails_the_run_at_the_restore():
    api = FakeApi()
    blobs = S.blobs
    blobs.put(api, "site:open.json", b'{"days": []}', {"by": "mac"})
    with place() as kw, captured():
        assert state_cli("pull", api, kw) == 2
        [p] = records(kw["job_dir"])["problems"]
        assert p["kind"] == "config" and "but no saved state" in p["message"]
        assert verdict(NOW - 60, {"pull": "failure", "closed": "skipped"}, kw["job_dir"])[0] == 1
        assert not os.path.exists(os.path.join(kw["job_dir"], "state_pulled.json"))


# ── one list failing, a failed add ───────────────────────────────────────────

def test_one_list_failing_leaves_a_green_partial_run():
    with tempfile.TemporaryDirectory() as job, captured():
        def closed():
            raise polymarket_api.PolymarketAPIError("gave up on https://gamma-api.polymarket.com/markets after 5 "
                                                   "attempts: HTTP 502", status=502)
        assert P.guard("closed", closed, job_dir=job) == 0
        assert P.guard("open", lambda: None, job_dir=job) == 0
        outcomes = {"pull": "success", "work": "success", "closed": "success", "open": "success", "save": "success"}
        body = sync.done_payload(store.connect(":memory:"), job, "full", "success", NOW - 60, now=NOW,
                                 outcomes=outcomes)
        assert body["run"]["status"] == "partial"
        assert "; problems: the closed list was not finished: Polymarket did not answer" in body["run"]["summary"]
        assert verdict(NOW - 60, outcomes, job)[0] == 0
    # a long list of problems is cut to what the API keeps
    many = [{"step": "open", "kind": "outside", "message": "x" * 200}] * 5
    assert len(P.summary_with("open list: updated", many)) == P.SUMMARY_CHARS
    assert P.summary_with("open list: updated", []) == "open list: updated"


def test_a_failed_add_is_reported_for_that_market_and_the_run_stays_green():
    # one market cannot be added (a reason: reported failed), one Polymarket did not answer for (not reported: it
    # stays approved for the next run), one whose page a scorer wrapped Polymarket's failure for (the same), and
    # the others are still added. add_market.add returns its failures with the exception that caused them
    from pipeline import add_market
    with tempfile.TemporaryDirectory() as job, captured():
        api = FakeApi()
        down = polymarket_api.PolymarketAPIError("gave up on https://data-api.polymarket.com/v2/trades after 5 "
                                                 "attempts: HTTP 503", status=503)

        def adder(cid, slug, name, outcome, dry_run=False, added=()):
            if cid == "0x2":
                raise down
            if cid == "0x3":                           # add_market.add's own catch-all, around a wrapped outage
                try:
                    try:
                        raise down
                    except polymarket_api.PolymarketAPIError as e:
                        raise RuntimeError("prior history of 0xabc could not be fetched") from e
                except RuntimeError as e:
                    return {"status": "failed", "name": name, "note": f"RuntimeError: {e}", "changed": False,
                            "error": e, "traceback": "Traceback ...\n"}
            return {"status": "failed", "name": name, "note": "it resolved No, not Yes", "changed": False}
        work = {"requests": [{"condition_id": f"0x{i}", "slug": f"s{i}", "name": f"m{i}", "winner": "Yes",
                              "status": "approved"} for i in (1, 2, 3)]}
        res = os.path.join(job, "add_results.json")
        assert P.guard("add", lambda: sync.add(api, work, res, adder=adder, now=NOW) and 0, job_dir=job) == 0
        got = sync.done_payload(store.connect(":memory:"), job, "full", "success", NOW - 60, now=NOW,
                                outcomes={"add": "success"})
        assert [(r["condition_id"], r["status"]) for r in got["requests"]] == [("0x1", "failed")]
        msgs = [p["message"] for p in records(job)["problems"]]
        assert any(m.startswith("m2 could not be added now (Polymarket did not answer") for m in msgs), msgs
        assert any(m.startswith("m3 could not be added now") for m in msgs), msgs
        assert {p["kind"] for p in records(job)["problems"]} == {"outside"}
        assert verdict(NOW - 60, {"add": "success"}, job)[0] == 0
    # a crash while adding one (a KeyError in the code): that market is reported failed, its traceback is in the
    # log, and the run fails with one problem naming it
    with tempfile.TemporaryDirectory() as job, captured() as out:
        def broken(cid, slug, name, outcome, dry_run=False, added=()):
            return {}["entry"]
        res = os.path.join(job, "add_results.json")
        work = {"requests": [{"condition_id": "0x9", "slug": "s9", "name": "m9", "winner": "Yes",
                              "status": "approved"}]}
        assert P.guard("add", lambda: sync.add(FakeApi(), work, res, adder=broken, now=NOW) and 0, job_dir=job) == 0
        got = sync.done_payload(store.connect(":memory:"), job, "full", "success", NOW - 60, now=NOW)
        assert [(r["condition_id"], r["status"]) for r in got["requests"]] == [("0x9", "failed")]
        assert "Traceback (most recent call last)" in out.getvalue() and "KeyError: 'entry'" in out.getvalue()
        code, found = verdict(NOW - 60, {"add": "success"}, job)
        assert code == 1 and [p["kind"] for p in found] == ["crash"]
        assert "adding m9 crashed: KeyError" in found[0]["message"], found
    # add_market.add itself hands its catch-all failures over with their exception and traceback
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "markets.json"), "w") as f:
            f.write("[]")
        got = add_market.add("0x" + "1" * 64, "s", "gemini-carbon", "Yes", root=tmp,
                             steps={"gamma": lambda slug: (_ for _ in ()).throw(down)})
        assert got["status"] == "failed" and got["error"] is down and "Traceback" in got["traceback"]
        assert P.classify(got["error"]) == "outside"


# ── a run started hours late ─────────────────────────────────────────────────

def test_a_late_run_counts_the_day_it_is_in():
    seen = []

    def run_day(day, markets, conn, client=None, watched=None, **kw):
        seen.append(day)
        return {"day": day, "closed_markets": 0, "candidates": 0, "checked": 0, "verdicts": {}, "dropped": {},
                "picks": []}
    with tempfile.TemporaryDirectory() as tmp, captured(), \
            swap((up.net, "install", lambda: None), (up.net, "closed_markets", lambda *a, **k: []),
                 (up, "run_day", run_day)):
        out = os.path.join(tmp, "closed.json")
        # yesterday's run on time (04:17 Toronto); today's started 9 hours late: yesterday and today, today partial
        up.build(days=2, now=to_epoch("2026-11-15T09:17:00Z"), conn=store.connect(":memory:"), out_path=out)
        seen.clear()
        doc = up.build(days=2, now=to_epoch("2026-11-16T18:17:00Z"), conn=store.connect(":memory:"), out_path=out)
        assert seen == ["2026-11-15", "2026-11-16"]
        days = {d["day"]: d for d in doc["days"]}
        assert up.partial(days["2026-11-16"]) and not up.partial(days["2026-11-15"])
        assert days["2026-11-16"]["counted_at"] == "2026-11-16T13:17:00-05:00"
        # so late it is nearly the next Toronto day: that day, and the one before counted again in full
        seen.clear()
        up.build(days=2, now=to_epoch("2026-11-18T04:50:00Z"), conn=store.connect(":memory:"), out_path=out)
        assert seen == ["2026-11-16", "2026-11-17"]
    # the open list's day is the Toronto day the run is in, whatever the hour
    for at, day in (("2026-11-16T13:17:00Z", "2026-11-16"), ("2026-11-17T04:59:00Z", "2026-11-16"),
                    ("2026-11-17T05:01:00Z", "2026-11-17")):
        assert O.Run(store.connect(":memory:"), to_epoch(at)).today == day


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
