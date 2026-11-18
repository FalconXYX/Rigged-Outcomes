#!/usr/bin/env python3
"""
Offline tests for how the daily job lives through what goes wrong outside its
code (watchlist.problems, watchlist.preflight, and the retries and fallbacks
in the steps): no network, fake services, fake sleeps.

    python3 tests/test_watchlist_problems.py

A run ends green unless a person must fix something. Covers each failure mode
the job must survive (the model busy for a whole run, Google News down,
Polymarket answering 429s and timing out, the site's API down while the
state is restored, saved and reported, no saved state in a fresh database,
one list failing or crashing, a failed add, a step stopped at its time
limit, PyPI down) and the ones that must fail the run with one clear message
(a missing secret or variable, a refused job token or Gemini key, a used
database that lost its state, a crash with its traceback, a crash while
adding a market, a model the code names gone, a request a service refuses,
requirements.lock failing while PyPI answers), what each error means, the
report's status and summary, a run GitHub started hours late, and the
workflow's wiring of all of it.
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

import polymarket_api  # noqa: E402
from fake_api import FakeApi  # noqa: E402
from watchlist import ai, judge, net, news, opennow as O, preflight as PF, problems as P  # noqa: E402
from watchlist import state as S, store, sync, upsets as up  # noqa: E402
from watchlist.common import DAY, ROOT, Budget, now_epoch, to_epoch  # noqa: E402

NOW = to_epoch("2026-11-16T04:17:00Z")
REAL_CLIENT = ai.Client
TOKEN = "job-token-that-must-never-be-printed"
KEY = "gemini-key-that-must-never-be-printed"


@contextlib.contextmanager
def captured():
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        yield out


@contextlib.contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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


# ── the guard and the verdict ────────────────────────────────────────────────

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
        code, found = P.verdict(NOW - 60, {"closed": "success", "open": "success"}, job)
        assert code == 0 and found == doc["problems"] and "::error::" not in out.getvalue()
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
        # the other list still ran and published; the run fails, with one ::error:: line naming the crash
        assert P.guard("open", lambda: 0, job_dir=job) == 0
        with captured() as out:
            code, found = P.verdict(NOW - 60, {"closed": "failure", "open": "success"}, job)
        assert code == 1 and [p["kind"] for p in found] == ["crash"]
        errors = [x for x in out.getvalue().splitlines() if x.startswith("::error::")]
        assert len(errors) == 1 and "the closed list crashed: KeyError" in errors[0]
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
        assert P.verdict(NOW - 60, {"open": "failure", "save": "failure", "install": "failure"}, job)[0] == 0
        # a step that failed without ever reaching its guard (an import error): the code, so the run fails
        found = P.collect(NOW - 60, {"decide": "failure"}, job)
        assert [(p["step"], p["kind"]) for p in found if p["step"] == "decide"] == [("decide", "crash")]
        assert P.verdict(NOW - 60, {"decide": "failure"}, job)[0] == 1
        # an older run's records never count
        assert P.collect(now_epoch() + 3600, {"open": "success"}, job) == []


def test_note_outside_a_step_only_logs():
    with tempfile.TemporaryDirectory() as job, captured() as out, env(GITHUB_ACTIONS="true"):
        P.note("the news did not answer")
        assert not os.path.exists(os.path.join(job, P.FILE))
        text = out.getvalue()
        assert "  ! the news did not answer" in text and "::warning::the news did not answer" in text


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
    with tempfile.TemporaryDirectory() as job, captured(), swap((ai, "_KEY", {"key": KEY}), (ai, "_GONE", set())):
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
        assert P.verdict(NOW - 60, {"closed": "success"}, job)[0] == 1
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


def test_one_error_line_per_problem_and_the_record_is_safe():
    import threading
    with tempfile.TemporaryDirectory() as job, captured() as out, env(GITHUB_ACTIONS="true"):
        def crash():
            P.note("the news did not answer")                         # a warning annotation
            return {}["missing"]
        assert P.guard("closed", crash, job_dir=job) == 1
        code, found = P.verdict(NOW - 60, {"closed": "failure"}, job)
        lines = out.getvalue().splitlines()
        assert code == 1 and len([x for x in lines if x.startswith("::error::")]) == 1
        assert len([x for x in lines if x.startswith("::warning::")]) == 1
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
    with tempfile.TemporaryDirectory() as job, captured() as out:
        code, found = P.verdict(NOW - 60, {"install": "failure", "pull": "skipped"}, job)
        assert code == 0 and found[0]["kind"] == "outside"
        code, found = P.verdict(NOW - 60, {"install": "failure", "pull": "skipped"}, job, broken=["install"])
        assert code == 1 and found[0]["kind"] == "crash" and "requirements.lock" in found[0]["message"]
        # the CLI as the workflow calls it: --broken "" (not broken) and --broken install
        with env(GITHUB_STEP_SUMMARY=None, WATCHLIST_JOB_DIR=None), swap((P, "JOB_DIR", job)):
            assert P.main(["verdict", "--started-at", str(NOW - 60), "--broken", "", "--outcome", "install=failure"]) == 0
            assert P.main(["verdict", "--started-at", str(NOW - 60), "--broken", "install",
                           "--outcome", "install=failure"]) == 1


def test_the_setup_check_records_its_warnings_and_says_a_crash_once():
    with tempfile.TemporaryDirectory() as job, captured() as out, env(GITHUB_ACTIONS="true", GITHUB_STEP_SUMMARY=None):
        with swap((PF, "run", lambda: ["the site's API did not answer the setup check (server (HTTP 503))"])):
            assert P.guard("preflight", PF.main, job_dir=job) == 0
        [p] = records(job)["problems"]
        assert p["step"] == "preflight" and p["kind"] == "outside" and "did not answer the setup check" in p["message"]
        with swap((PF, "run", lambda: {}["missing"])):
            assert P.guard("preflight", PF.main, job_dir=job) == 1
        lines = out.getvalue().splitlines()
        assert len([x for x in lines if x.startswith("::error::")]) == 1 and "Traceback" in out.getvalue()
        assert [x for x in lines if x.startswith("::error::")][0].startswith("::error::The setup check crashed: KeyError")


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
                  (ai, "_KEY", {"key": KEY}),
                  (judge, "BUSY_WAIT", 0),
                  (news, "search", lambda q, after=None, before=None, limit=15: [])):
            code = P.guard("closed", lambda: up.build(days=2, now=NOW, conn=conn, out_path=out, client=busy),
                           job_dir=job)
        assert code == 0
        doc = json.load(open(out))
        assert [d["day"] for d in doc["days"]] == ["2026-11-16", "2026-11-15"]
        assert doc["days"][1]["dropped"].get("could not be checked") == 3
        msgs = [p["message"] for p in records(job)["problems"]]
        assert any("3 market(s) of the closed list got no AI label" in m for m in msgs), msgs
        assert any("could not check 3 market(s)" in m for m in msgs)
        assert all(p["kind"] == "outside" for p in records(job)["problems"])
        assert P.verdict(NOW - 60, {"closed": "success"}, job)[0] == 0


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
    # the days after the budget ran out keep their last version (a partial one is counted again next run)
    seen = []
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as job, captured():
        out_path = os.path.join(tmp, "closed.json")
        old = {"day": "2026-11-15", "counted_at": "2026-11-15T09:00:00+00:00", "closed_markets": 1, "candidates": 0,
               "checked": 0, "verdicts": {}, "dropped": {}, "picks": []}
        json.dump({"generated_at": "2026-11-15T09:00:00+00:00", "days": [old]}, open(out_path, "w"))
        with swap((up.net, "install", lambda: None), (up.net, "closed_markets", lambda since, now, min_volume=0: []),
                  (up, "run_day", lambda *a, **k: seen.append(a[0]))):
            assert P.guard("closed", lambda: up.build(days=2, now=NOW, conn=conn, out_path=out_path,
                                                       budget=Budget(0)), job_dir=job) == 0
        assert seen == [] and json.load(open(out_path))["days"] == [old]
        [p] = records(job)["problems"]
        assert "ran out of its 0-minute budget: 2 day(s) from 2026-11-15 keep their last version" in p["message"]


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
        args = [cmd, "--db", os.path.join(kw["job_dir"], "w.db"), "--site-dir", kw["site_dir"], "--news-cache",
                os.path.join(kw["job_dir"], "news.json")]
        return P.guard(S.STEP_NAMES[cmd], lambda: S.main(args), job_dir=kw["job_dir"])


def test_the_api_down_while_restoring_saving_and_reporting():
    api = FakeApi()
    with place() as kw, captured():
        seeded = store.connect(":memory:")
        store.save_review(seeded, "acme", "acme talks", "step", {"title": "Acme talks"}, at=NOW - DAY)
        with captured():
            S.push(seeded, api, seed=True, now=NOW - DAY, **kw)
        # restoring: the API still down after its retries -> not restored (the lists are not made), green
        api.fail_next(503, "server", method="GET")
        assert state_cli("pull", api, kw) == 2
        [p] = records(kw["job_dir"])["problems"]
        assert p["kind"] == "outside" and p["step"] == "pull" and "could not be downloaded" in p["message"]
        code, found = P.verdict(NOW - 60, {"pull": "failure", "closed": "skipped", "open": "skipped"}, kw["job_dir"])
        assert code == 0 and P.status("success", {"pull": "failure"}, found) == "failure"
        # restored, then saving: the API down -> not saved, the next run redoes the work, green
        assert state_cli("pull", api, kw) == 0
        api.fail_next(None, "network", method="GET")
        assert state_cli("push", api, kw) == 3
        assert records(kw["job_dir"])["problems"][-1]["kind"] == "outside"
        assert P.verdict(NOW - 60, {"pull": "success", "save": "failure"}, kw["job_dir"])[0] == 0
    # reporting: the queue, the catalog and the report not taken -> warned about and recorded, never raised
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
    # the queue not taken (the API down): the catalog is not tried, so the report gets the step's time
    conn = store.connect(":memory:")
    store.save_open_day(conn, "2026-11-16", "updated", NOW, [], info={"awaiting": [], "new_today": 0})
    with tempfile.TemporaryDirectory() as job, captured() as out:
        api = FakeApi()
        api.fail_next(503, "server", method="PUT", path="/api/job/queue")
        assert P.guard("report", lambda: sync.push(api, conn, job, "full", "success", NOW - 60, now=NOW) and 0,
                       job_dir=job) == 0
        paths = [p for _, p, _ in api.log]
        assert paths == ["/api/job/queue", "/api/job/done"], paths
        msgs = [p["message"] for p in records(job)["problems"]]
        assert any(m.startswith("the queue was not sent") for m in msgs)
        assert any(m.startswith("the catalog was not sent (the API did not answer)") for m in msgs)
        # the report that went through says so: partial, with the problems after its summary
        done = api.log[-1][2]
        assert done["run"]["status"] == "partial" and "problems: the queue was not sent" in done["run"]["summary"]
        assert "problems" not in done and "reported (partial)" in out.getvalue()


def test_a_report_the_api_refuses_is_the_codes_problem():
    # a 4xx that is not about the token (the job and the Worker disagree on a request's shape) is the code's: the
    # run fails, so the review page does not quietly keep an old report
    conn = store.connect(":memory:")
    store.save_open_day(conn, "2026-11-16", "updated", NOW, [], info={"awaiting": [], "new_today": 0})
    for path, method in (("/api/job/done", "POST"), ("/api/job/queue", "PUT"), ("/api/job/catalog", "PUT")):
        with tempfile.TemporaryDirectory() as job, captured():
            api = FakeApi()
            api.fail_next(400, "bad_request", method=method, path=path)
            assert P.guard("report", lambda: sync.push(api, conn, job, "full", "success", NOW - 60, now=NOW) and 0,
                           job_dir=job) == 0
            assert [p["kind"] for p in records(job)["problems"]] == ["crash"], path
            assert P.verdict(NOW - 60, {"report": "success"}, job)[0] == 1
    # a refused token stops the step wherever it comes (a setup problem), the catalog included
    with tempfile.TemporaryDirectory() as job, captured():
        api = FakeApi()
        api.fail_next(401, "unauthorized", method="GET", path="prefix=market:")
        assert P.guard("report", lambda: sync.push(api, conn, job, "full", "success", NOW - 60, now=NOW) and 0,
                       job_dir=job) == 1
        assert records(job)["problems"][-1]["kind"] == "config"
    # the queue taken, then the API down at the catalog's first read: the catalog is not sent, so the report gets
    # the step's time (two calls that wait out an outage fit in the step's 15 minutes, three would not)
    with tempfile.TemporaryDirectory() as job, captured():
        api = FakeApi()
        api.fail_next(503, "server", method="GET", path="prefix=market:")
        assert P.guard("report", lambda: sync.push(api, conn, job, "full", "success", NOW - 60, now=NOW) and 0,
                       job_dir=job) == 0
        assert [(m, p.split("?")[0]) for m, p, _ in api.log] == [("PUT", "/api/job/queue"), ("GET", "/api/job/blobs"),
                                                                  ("POST", "/api/job/done")]
        assert {p["kind"] for p in records(job)["problems"]} == {"outside"}
    worst_call = (len(sync.STEP_WAITS) + 1) * sync.TIMEOUT + sum(sync.STEP_WAITS)
    with open(os.path.join(ROOT, ".github", "workflows", "watchlist.yml")) as f:
        report = f.read().split("- name: Report to the API", 1)[1].split("- name:", 1)[0]
    assert 2 * worst_call + 120 <= int(re.search(r"timeout-minutes: (\d+)", report).group(1)) * 60


def test_the_work_and_a_refused_token_mid_run():
    with tempfile.TemporaryDirectory() as job, captured():
        down = FakeApi()
        down.fail_next(503, "server")
        assert P.guard("work", lambda: sync.pull(down, os.path.join(job, "work.json"), now=NOW) and 0,
                       job_dir=job) == 0
        assert json.load(open(os.path.join(job, "work.json")))["decisions"] == []
        assert records(job)["problems"][0]["kind"] == "outside"
        refused = FakeApi()
        refused.fail_next(401, "unauthorized")
        assert P.guard("work", lambda: sync.pull(refused, os.path.join(job, "work.json"), now=NOW), job_dir=job) == 1
        assert records(job)["problems"][-1]["kind"] == "config"
        assert P.verdict(NOW - 60, {"work": "failure"}, job)[0] == 1


def test_no_saved_state_starts_fresh_under_the_guard():
    api = FakeApi()
    with place() as kw, captured():
        assert state_cli("pull", api, kw) == 0
        [p] = records(kw["job_dir"])["problems"]
        assert p["kind"] == "outside" and "No saved state in the database" in p["message"]
        assert P.verdict(NOW - 60, {"pull": "success"}, kw["job_dir"])[0] == 0
        assert state_cli("push", api, kw) == 0 and S.blobs.head(api, S.STATE) is None    # nothing changed yet


def test_a_used_database_without_its_state_fails_the_run_at_the_restore():
    api = FakeApi()
    blobs = S.blobs
    blobs.put(api, "list:open", b'{"days": []}', {"by": "github"})
    with place() as kw, captured():
        assert state_cli("pull", api, kw) == 2
        [p] = records(kw["job_dir"])["problems"]
        assert p["kind"] == "config" and "but no saved state" in p["message"]
        assert P.verdict(NOW - 60, {"pull": "failure", "closed": "skipped"}, kw["job_dir"])[0] == 1
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
        assert P.verdict(NOW - 60, outcomes, job)[0] == 0
    # a long list of problems is cut to what the API keeps
    many = [{"step": "open", "kind": "outside", "message": "x" * 200}] * 5
    assert len(P.summary_with("open list: updated", many)) == P.SUMMARY_CHARS
    assert P.summary_with("open list: updated", []) == "open list: updated"


def test_a_failed_add_is_reported_for_that_market_and_the_run_stays_green():
    # one market cannot be added (a reason: reported failed), one Polymarket did not answer for (not reported: it
    # stays approved for the next run), one whose page a scorer wrapped Polymarket's failure for (the same), and
    # the others are still added. add_market.add returns its failures with the exception that caused them
    import add_market
    with tempfile.TemporaryDirectory() as job, captured() as out, env(GITHUB_OUTPUT=None):
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
        assert P.verdict(NOW - 60, {"add": "success"}, job)[0] == 0 and "::error::" not in out.getvalue()
    # a crash while adding one (a KeyError in the code): that market is reported failed, its traceback is in the
    # log, and the run fails with one line naming it
    with tempfile.TemporaryDirectory() as job, captured() as out, env(GITHUB_OUTPUT=None, GITHUB_ACTIONS="true"):
        def broken(cid, slug, name, outcome, dry_run=False, added=()):
            return {}["entry"]
        res = os.path.join(job, "add_results.json")
        work = {"requests": [{"condition_id": "0x9", "slug": "s9", "name": "m9", "winner": "Yes",
                              "status": "approved"}]}
        assert P.guard("add", lambda: sync.add(FakeApi(), work, res, adder=broken, now=NOW) and 0, job_dir=job) == 0
        got = sync.done_payload(store.connect(":memory:"), job, "full", "success", NOW - 60, now=NOW)
        assert [(r["condition_id"], r["status"]) for r in got["requests"]] == [("0x9", "failed")]
        assert "Traceback (most recent call last)" in out.getvalue() and "KeyError: 'entry'" in out.getvalue()
        assert "::error::" not in out.getvalue()               # the verdict says it, once
        with captured() as v:
            code, found = P.verdict(NOW - 60, {"add": "success"}, job)
        assert code == 1 and [p["kind"] for p in found] == ["crash"]
        errors = [x for x in v.getvalue().splitlines() if x.startswith("::error::")]
        assert len(errors) == 1 and "adding m9 crashed: KeyError" in errors[0], errors
    # add_market.add itself hands its catch-all failures over with their exception and traceback
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "markets.json"), "w") as f:
            f.write("[]")
        got = add_market.add("0x" + "1" * 64, "s", "gemini-carbon", "Yes", root=tmp,
                             steps={"gamma": lambda slug: (_ for _ in ()).throw(down)})
        assert got["status"] == "failed" and got["error"] is down and "Traceback" in got["traceback"]
        assert P.classify(got["error"]) == "outside"


# ── the setup: missing secrets, a wrong token, a refused key ─────────────────

class Answer:
    def __init__(self, status, body):
        self.status_code = status
        self.content = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)


class GoogleSession:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_missing_secrets_fail_at_the_start_with_one_message():
    for given, words in (({}, "the secret GEMINI_API_KEY, the secret WATCHLIST_JOB_TOKEN and the variable API_URL "
                              "are not set"),
                         ({"GEMINI_API_KEY": KEY, "WATCHLIST_JOB_TOKEN": TOKEN},
                          "the variable API_URL is not set"),
                         ({"GEMINI_API_KEY": KEY, "WATCHLIST_API_URL": "https://api.example"},
                          "the secret WATCHLIST_JOB_TOKEN is not set"),
                         ({"GEMINI_API_KEY": KEY, "WATCHLIST_JOB_TOKEN": TOKEN, "WATCHLIST_API_URL": "api.example"},
                          "the variable API_URL is not a web address")):
        try:
            PF.settings(given.get)
            raise AssertionError(f"no refusal: {words}")
        except PF.SetupError as e:
            assert words in str(e), str(e)
    # the CLI: one ::error:: line, exit 1, and no secret in the output
    with captured() as out, env(GITHUB_ACTIONS="true", GITHUB_STEP_SUMMARY=None), \
            swap((PF, "run", lambda: PF.settings({"GEMINI_API_KEY": KEY}.get))):
        assert PF.main() == 1
    lines = out.getvalue().splitlines()
    assert len([x for x in lines if x.startswith("::error::")]) == 1 and KEY not in out.getvalue()


def test_a_wrong_job_token_fails_at_the_start():
    def client(answer):
        class Session:
            def request(self, *a, **k):
                return answer
        return sync.Client("https://api.example", TOKEN, session=Session(), sleep=lambda s: None)
    for answer, words in ((Answer(401, {"error": "unauthorized"}), "refused the job token"),
                          (Answer(429, {"error": "locked"}), "stays locked until the UTC day ends"),
                          (Answer(200, {"hello": "world"}), "does not answer like the site's API"),
                          (Answer(200, b"<html>a page</html>"), "does not answer like the site's API"),
                          (Answer(404, {"error": "not_found"}), "does not answer like the site's API")):
        try:
            PF.check_api(client(answer))
            raise AssertionError(words)
        except PF.SetupError as e:
            assert words in str(e) and TOKEN not in str(e), str(e)
    # the API down is no setup problem: a warning, and the steps wait for it
    assert "did not answer" in PF.check_api(client(Answer(503, {"error": "server"})))
    assert PF.check_api(client(Answer(200, {"blobs": []}))) is None


def test_a_refused_gemini_key_fails_at_the_start_but_a_busy_google_does_not():
    bad = Answer(400, {"error": {"message": f"API key not valid. Please pass a valid API key. {KEY}"}})
    try:
        PF.check_gemini(KEY, GoogleSession([bad]), sleep=lambda s: None)
        raise AssertionError("not refused")
    except PF.SetupError as e:
        assert "Google refused the Gemini key" in str(e) and KEY not in str(e)
    waits = []
    busy = GoogleSession([Answer(503, {"error": {"message": "overloaded"}}), requests.Timeout("slow"),
                          Answer(503, {"error": {"message": "overloaded"}})])
    assert "did not confirm the Gemini key" in PF.check_gemini(KEY, busy, sleep=waits.append)
    assert waits == list(PF.GEMINI_WAITS)
    # an answer that does not name the key (a 404 for this key's model list, say) is a warning, never a failure
    assert "HTTP 404" in PF.check_gemini(KEY, GoogleSession([Answer(404, {"error": {"message": "Not found"}})]))
    assert PF.check_gemini(KEY, GoogleSession([Answer(200, {"models": []})])) is None


# ── a run GitHub started hours late ──────────────────────────────────────────

def test_a_late_run_counts_the_day_it_is_in():
    seen = []

    def run_day(day_ts, markets, conn, client=None, watched=None):
        seen.append(up.iso(day_ts)[:10])
        return {"day": up.iso(day_ts)[:10], "closed_markets": 0, "candidates": 0, "checked": 0, "verdicts": {},
                "dropped": {}, "picks": []}
    with tempfile.TemporaryDirectory() as tmp, captured(), \
            swap((up.net, "install", lambda: None), (up.net, "closed_markets", lambda *a, **k: []),
                 (up, "run_day", run_day)):
        out = os.path.join(tmp, "closed.json")
        # yesterday's run on time; today's started 9 hours late: yesterday and today, today partial "by 13:17"
        up.build(days=2, now=to_epoch("2026-11-15T04:17:00Z"), conn=store.connect(":memory:"), out_path=out)
        seen.clear()
        doc = up.build(days=2, now=to_epoch("2026-11-16T13:17:00Z"), conn=store.connect(":memory:"), out_path=out)
        assert seen == ["2026-11-15", "2026-11-16"]
        days = {d["day"]: d for d in doc["days"]}
        assert up.partial(days["2026-11-16"]) and not up.partial(days["2026-11-15"])
        assert days["2026-11-16"]["counted_at"] == "2026-11-16T13:17:00+00:00"
        # so late it is the next UTC day: that day, and the one before counted again in full
        seen.clear()
        up.build(days=2, now=to_epoch("2026-11-17T23:50:00Z"), conn=store.connect(":memory:"), out_path=out)
        assert seen == ["2026-11-16", "2026-11-17"]
    # the open list's day is the UTC day the run is in, whatever the hour
    for at, day in (("2026-11-16T13:17:00Z", "2026-11-16"), ("2026-11-16T23:59:00Z", "2026-11-16"),
                    ("2026-11-17T00:01:00Z", "2026-11-17")):
        assert O.Run(store.connect(":memory:"), to_epoch(at)).today == day


# ── the workflow's wiring ────────────────────────────────────────────────────

def test_the_workflow_fails_only_for_the_setup_or_the_code():
    with open(os.path.join(ROOT, ".github", "workflows", "watchlist.yml")) as f:
        text = f.read()
    job = text.split("\n  lists:\n", 1)[1].split("\n  deploy:\n", 1)[0]
    names = re.findall(r"^      - (?:name|id): (.+)$", job, re.M)
    blocks = dict(zip(names, re.split(r"^      - (?:name|id): .+$", job, flags=re.M)[1:]))
    by_id = {}
    for name, block in blocks.items():
        m = re.search(r"^        id: (\S+)$", block, re.M)
        if m:
            by_id[m.group(1)] = block
    assert "Mark the run failed" not in text
    # every step that runs the job's Python goes on when it fails, except the setup check
    guarded = ("pull", "work", "decide", "closed", "open", "save", "add", "report")
    for step in guarded + ("install",):
        assert "continue-on-error: true" in by_id[step], step
    assert "continue-on-error" not in by_id["preflight"] and "python3 -m watchlist.preflight" in by_id["preflight"]
    # the setup check never times out on a slow service (a timeout there would fail the run): every try of both
    # checks timing out still ends well inside its step's limit
    worst = ((len(PF.API_WAITS) + 1) * sync.TIMEOUT + sum(PF.API_WAITS)
             + (len(PF.GEMINI_WAITS) + 1) * PF.TIMEOUT + sum(PF.GEMINI_WAITS))
    limit = int(re.search(r"timeout-minutes: (\d+)", by_id["preflight"]).group(1))
    assert worst + 120 <= limit * 60, (worst, limit)
    # the report and the verdict hear every step's outcome, by the step ids the guards record
    verdict = blocks["Fail the run only for what a person must fix"]
    assert "python3 -m watchlist.problems verdict" in verdict
    for step in guarded + ("install",):
        if step not in ("report", "install"):              # the report runs only once the setup check passed
            assert f"--outcome {step}=${{{{ steps.{step}.outcome }}}}" in by_id["report"], step
        assert f"--outcome {step}=${{{{ steps.{step}.outcome }}}}" in verdict, step
    # each module records itself under the step id the workflow gives it
    commands = {"pull": "watchlist.state pull", "work": "watchlist.sync pull", "decide": "watchlist.sync decide",
                "closed": "watchlist.upsets", "open": "watchlist.opennow", "save": "watchlist.state push",
                "add": "watchlist.sync add", "report": "watchlist.sync push"}
    for step, cmd in commands.items():
        assert f"python3 -m {cmd}" in by_id[step], step
    assert S.STEP_NAMES == {"pull": "pull", "push": "save"} and sync.STEP_NAMES == {"pull": "work", "push": "report"}
    for path, step in (("watchlist/upsets.py", "closed"), ("watchlist/opennow.py", "open")):
        with open(os.path.join(ROOT, path)) as f:
            assert f'problems.guard("{step}", main)' in f.read(), path
    # the deploy keeps the last site instead of failing when the data can't be had
    with open(os.path.join(ROOT, ".github", "workflows", "deploy.yml")) as f:
        deploy = f.read()
    assert "steps.watchlist.outputs.skip != 'true'" in deploy and "needs.build.outputs.skip != 'true'" in deploy
    # a broken requirements.lock (PyPI answering) reaches the verdict as a crash; PyPI down stays an outside one
    assert 'echo "broken=true" >> "$GITHUB_OUTPUT"' in by_id["install"] and "pip download" in by_id["install"]
    assert "BROKEN: ${{ steps.install.outputs.broken == 'true' && 'install' || '' }}" in verdict
    assert '--broken "$BROKEN"' in verdict
    # the open list is stopped by `timeout` (its budget plus the slack), not by the job's own timeout
    assert re.search(r'timeout -k \d+ "\$\(\( budget \+ \d+ \)\)m" \\\n\s+python3 -m watchlist\.opennow', by_id["open"])
    # the Watchlist job's deploy is soft: PyPI or npm not answering leaves it out; the Pages deploy is tried twice
    assert re.search(r"uses: \./\.github/workflows/deploy\.yml\n    with:\n      ref: main\n      soft: true", text)
    assert re.search(r"      soft:\n        description: .+\n        type: boolean\n        default: false", deploy)
    assert deploy.count('if [ "$SOFT" = true ]; then') == 2 and deploy.count("uses: actions/deploy-pages@v5") == 2
    assert "SOFT: ${{ inputs.soft && 'true' || 'false' }}" in deploy


def _jobs(text):
    """{job id: its text} of a workflow file."""
    body = text.split("\njobs:\n", 1)[1]
    names = re.findall(r"^  ([A-Za-z0-9_-]+):$", body, re.M)
    return dict(zip(names, re.split(r"^  [A-Za-z0-9_-]+:$", body, flags=re.M)[1:]))


def test_the_workflows_refer_only_to_steps_and_jobs_they_have():
    # GitHub reads a reference to a step id that does not exist as empty, silently: every steps.X and needs.X
    # must name a step of the same job or a job it needs, and each step id is given once
    for name in ("watchlist.yml", "deploy.yml"):
        with open(os.path.join(ROOT, ".github", "workflows", name)) as f:
            text = f.read()
        jobs = _jobs(text)
        assert jobs, name
        for job, body in jobs.items():
            ids = re.findall(r"^(?: {8}| {6}- )id: ([A-Za-z0-9_-]+)$", body, re.M)
            assert len(ids) == len(set(ids)) and (ids or "    steps:" not in body), (name, job, ids)
            used = set(re.findall(r"\bsteps\.([A-Za-z0-9_-]+)\.", body))
            assert used <= set(ids), (name, job, sorted(used - set(ids)))
            needs = re.search(r"^    needs: \[?([A-Za-z0-9_, -]+)\]?$", body, re.M)
            needed = {x.strip() for x in needs.group(1).split(",")} if needs else set()
            assert needed <= set(jobs), (name, job, needed)
            assert set(re.findall(r"\bneeds\.([A-Za-z0-9_-]+)\.", body)) <= needed, (name, job)
            # every step that runs a script has a time limit of its own, or its job a short one (GitHub's
            # default is 6 hours)
            job_limit = re.search(r"^    timeout-minutes: (\d+)", body, re.M)
            for st in re.split(r"^ {6}- ", body, flags=re.M)[1:]:
                if "run: |" in st:
                    assert "timeout-minutes:" in st or (job_limit and int(job_limit.group(1)) <= 30), \
                        (name, job, st[:60])


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
