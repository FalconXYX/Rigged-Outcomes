#!/usr/bin/env python3
"""
The step function's runtime (backend/runtime.py) against the fake Worker (tests/test_backend_fakeworker.py), with
stand-in handlers. No network, a few seconds.

    python3 tests/test_backend_runtime.py

Covers: a call claiming and completing jobs until its lane is empty, `spent` and usage going to the ledger, the
deadline (no claim under min_claim_left_s, left_s on the claim, a kind whose need_s does not fit never claimed),
partial from a returned cursor and from OutOfTime at the last checkpoint, later (a handler's, Polymarket not
answering), error text with every secret cut out, a lost lease (no complete), the job's own /tmp folder removed
afterwards, checkpoints and completes split under 256 KB, children and stop_run, continuing, two calls in one
process keeping their deadlines apart, api/step.py's whole path with a signed wake-up, and a background step's
give_way (the flag from a checkpoint's answer and never back, every_s asking with a read-only checkpoint only when
the last answer is that old, an ask the Worker does not answer logged and gone on from) with GiveWay going out as
partial from the last cursor, sent with gave_way (not counted), and the call claiming again after it only with
time for the lane's longest need_s.
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_backend_fakeworker as F  # noqa: E402
from backend import outcomes, runtime, wake  # noqa: E402
from watchlist import sync  # noqa: E402


def fresh(kind="add"):
    w = F.FakeWorker()
    rid = w.add_run(kind, "0x" + "ab" * 32 + ":1")
    return w, rid


def test_a_call_runs_its_lane_dry():
    w, rid = fresh()
    seen = []

    def fetch(ctx):
        seen.append(("fetch", ctx.job["run_id"], ctx.budget["level"], round(ctx.left())))
        assert abs(ctx.now() - w.now()) <= 1 and os.path.isdir(ctx.tmp)
        ctx.child("add.whales", "0", {"whales": [["0x1", 1]]})
        ctx.child("add.whales", "1", {"whales": [["0x2", 2]]})
        ctx.meta(name="n")
        ctx.units(3)
        return ctx.done(results=[("add:fetch", {"entry": 1})])

    def whales(ctx):
        seen.append(("whales", ctx.job["part"]))
        ctx.checkpoint(cursor={"done": [0]}, results=[(f"whale:{ctx.job['part']}", {"x": 1})])

    def export(ctx):
        seen.append(("export", sorted(k for k, _ in ctx.results("whale:")), ctx.job["missing"]))

    got = F.serve(w, "add", handlers={"add.fetch": fetch, "add.whales": whales, "add.export": export})
    assert got == {"lane": "add", "jobs": 4}, got
    assert [s[0] for s in seen] == ["fetch", "whales", "whales", "export"]
    assert seen[0][2] == "ok" and seen[0][3] <= 300 - 10 - 20
    assert seen[-1][1:] == (["whale:0", "whale:1"], [])
    assert w.runs[rid]["state"] == "done" and w.runs[rid]["meta"] == {"name": "n"}
    first = w.completes[0]
    assert first["usage"]["units"] == 3 and first["usage"]["wall_ms"] >= 0 and first["usage"]["mem_mb"] > 0
    assert first["continuing"] is True and len(first["children"]) == 2
    assert len(w.claims) == 5 and all(c["call"] == w.claims[0]["call"] for c in w.claims)   # the last one: 204
    assert all(set(c["spent"]) == {"cpu_ms", "wall_ms"} for c in w.claims)
    assert not os.path.exists(os.path.join(F.SCRATCH, "ro")) or not os.listdir(os.path.join(F.SCRATCH, "ro"))


def test_the_deadline_decides_what_is_claimed():
    w, _ = fresh()
    ran = []
    # 230 s gone: 40 s to the deadline, under min_claim_left_s (45): nothing claimed, the time still reported
    got = F.serve(w, "add", late_s=230, handlers={"add.fetch": lambda ctx: ran.append(1)})
    assert got["jobs"] == 0 and not ran and len(w.claims) == 1 and w.claims[0]["left_s"] == 0
    # 180 s gone: 90 s left, under add.fetch's need_s (120): claimed by nobody
    got = F.serve(w, "add", late_s=180, handlers={"add.fetch": lambda ctx: ran.append(1)})
    assert got["jobs"] == 0 and not ran and w.claims[-1]["left_s"] in (89, 90)
    got = F.serve(w, "add", late_s=100, handlers={"add.fetch": lambda ctx: ran.append(round(ctx.left()))})
    assert ran and 165 <= ran[0] <= 170 and w.claims[-2]["left_s"] in (169, 170)


def test_partial_later_and_errors():
    w, _ = fresh()
    calls = {"n": 0}

    def fetch(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return ctx.partial({"page": 3})
        if calls["n"] == 2:
            assert ctx.cursor == {"page": 3}
            ctx.checkpoint(cursor={"page": 5}, results=[("add:part", {"p": 5})])
            raise outcomes.OutOfTime("the deadline")
        if calls["n"] == 3:
            assert ctx.cursor == {"page": 5}
            raise outcomes.Later(ctx.now() + 600, "model_busy", ctx.now() + 600)
        raise RuntimeError(f"failed with key=abc123&x and Bearer tok-1 and {F.ENV['GEMINI_API_KEY']} "
                           f"{F.ENV['WATCHLIST_JOB_TOKEN']}\nsecond line")

    F.serve(w, "add", handlers={"add.fetch": fetch})              # partial, partial and later in one call
    w.job("add.fetch")["run_after"] = 0
    outs = [c["outcome"] for c in w.completes]
    assert outs[:3] == ["partial", "partial", "later"], outs
    assert w.completes[1]["cursor"] == {"page": 5} and w.results[(w.job("add.fetch")["run_id"], "add:part")]
    assert w.completes[2]["reason"] == "model_busy" and w.completes[2]["pause_lane_until"]
    F.serve(w, "add", handlers={"add.fetch": fetch})
    err = w.completes[-1]
    assert err["outcome"] == "error", err
    for secret in ("abc123", "tok-1", F.ENV["GEMINI_API_KEY"], F.ENV["WATCHLIST_JOB_TOKEN"], "second line"):
        assert secret not in err["error"], err["error"]
    assert err["error"].startswith("RuntimeError: failed with key=[redacted]") and len(err["error"]) <= 500
    assert w.job("add.fetch")["attempt"] == 1


def test_polymarket_not_answering_is_later():
    from pipeline import polymarket_api
    w, _ = fresh()

    def fetch(ctx):
        raise polymarket_api.PolymarketAPIError("HTTP 503 from x", status=503)

    F.serve(w, "add", handlers={"add.fetch": fetch})
    c = w.completes[-1]
    assert c["outcome"] == "later" and c["reason"] == "polymarket_down" and c["run_after"] >= w.now() + 110

    def refused(ctx):
        raise polymarket_api.PolymarketAPIError("HTTP 400 from x", status=400)
    w.job("add.fetch")["run_after"] = 0
    F.serve(w, "add", handlers={"add.fetch": refused})
    assert w.completes[-1]["outcome"] == "error"


def test_a_lost_lease_sends_nothing():
    w, _ = fresh()

    def fetch(ctx):
        ctx.job["lease_token"] = "0" * 32                      # what the Worker sees once the lease expired
        ctx.checkpoint(cursor={"x": 1})

    F.serve(w, "add", handlers={"add.fetch": fetch})
    assert not w.completes and w.job("add.fetch")["state"] == "leased"


def test_big_checkpoints_and_completes_are_split():
    w, rid = fresh()
    big = [(f"k{i}", {"text": "x" * 100_000}) for i in range(7)]

    def fetch(ctx):
        ctx.checkpoint(cursor={"n": 7}, results=big[:4], news=[("q|w", [{"title": "t" * 150_000}])])
        return ctx.done(results=big[4:])

    F.serve(w, "add", handlers={"add.fetch": fetch, "add.export": lambda ctx: None})
    assert len(w.checkpoints) >= 4                               # 4 x 100 KB + 150 KB, then the done's 300 KB
    assert all(c["cursor"] is None for c in w.checkpoints[:2]) and w.checkpoints[2]["cursor"] == {"n": 7}
    assert sum(1 for (r, _k) in w.results if r == rid) == 7 and "q|w" in w.news
    assert w.completes[0]["results"] == [] and w.completes[0]["outcome"] == "done"


def scoring(w):
    """An add run whose fetch and export are done and whose wallets.score (a background job) is queued."""
    rid = w.add_run("add", "0x" + "cd" * 32 + ":1")
    for kind in ("add.fetch", "add.export"):
        w.job(kind, run=rid)["state"] = "done"
    w._insert(rid, "wallets.score", "0", {"name": "m"}, [])
    return rid


def test_a_background_step_hears_when_to_give_way():
    w = F.FakeWorker()
    scoring(w)
    t, logged = [1000.0], []
    claim = w.claim("add", 270)
    ctx = runtime.StepContext(claim, w, t[0] + 270, F.SCRATCH, mono=lambda: t[0], log=logged.append)
    asks = lambda: w.calls.count(("POST", "/api/job/checkpoint"))           # noqa: E731
    assert claim["job"]["kind"] == "wallets.score" and ctx.give_way() is False
    t[0] += 4
    assert ctx.give_way(every_s=5) is False and not w.checkpoints          # 4 s since the claim: not asked
    ctx.checkpoint(cursor={"page": 1})
    assert ctx.give_way() is False                                        # 204: nothing waits
    t[0] += 5
    assert ctx.give_way(every_s=5) is False and len(w.checkpoints) == 2  # its answer 5 s old: asked once
    assert w.checkpoints[-1] == {"job_id": claim["job"]["id"], "lease_token": claim["job"]["lease_token"]}
    assert w.job("wallets.score")["cursor"] == {"page": 1} == ctx.cursor  # a read: the cursor left alone
    assert ctx.give_way(every_s=5) is False and len(w.checkpoints) == 2  # just answered: not again
    t[0] += 5
    w.fail["/api/job/checkpoint"] = [sync.ApiError(503, "error")]         # the Worker not answering the ask
    assert ctx.give_way(every_s=5) is False and asks() == 3 and len(logged) == 1   # logged, the step goes on
    t[0] += 4
    assert ctx.give_way(every_s=5) is False and asks() == 3              # asked again only every_s later
    t[0] += 1
    w.fail["/api/job/checkpoint"] = [sync.ApiError(409, "lost")]
    try:
        ctx.give_way(every_s=5)
        raise AssertionError("a lost lease must raise Lost")
    except outcomes.Lost:
        pass
    t[0] += 5
    assert ctx.give_way(every_s=5) is False and asks() == 5 and len(w.checkpoints) == 3
    other = w.add_run("add", "0x" + "ef" * 32 + ":2")                     # a visitor's add, its fetch ready
    assert ctx.give_way() is False and ctx.give_way(every_s=5) is False   # nobody asked since
    t[0] += 6
    assert ctx.give_way(every_s=5) is True and len(w.checkpoints) == 4
    assert ctx.give_way(every_s=0) is True and len(w.checkpoints) == 4   # once true, no more asking
    for j in w.jobs.values():
        if j["run_id"] == other:
            j["state"] = "done"
    ctx.checkpoint(cursor={"page": 2})                                    # 204 again: it stays true
    assert ctx.give_way() is True and ctx.cursor == {"page": 2}


def test_give_way_goes_out_as_partial_from_the_last_cursor():
    w = F.FakeWorker()
    rid = scoring(w)
    seen = []

    def score(ctx):
        seen.append(ctx.cursor)
        if len(seen) == 1:
            ctx.checkpoint(cursor={"phase": "history"}, results=[("rubric-hist:0x1:1", {"n": 1})])
            assert ctx.give_way() is False
            w.add_run("add", "0x" + "ef" * 32 + ":2")                     # a visitor's add meanwhile
            ctx.checkpoint(results=[("rubric-hist:0x2:2", {"n": 2})])
            if ctx.give_way():
                raise outcomes.GiveWay("another job waits")
        return ctx.done()

    F.serve(w, "add", handlers={"wallets.score": score, "add.fetch": lambda ctx: ctx.done(stop_run=True)})
    outs = [(c["job_id"].split("|")[1], c["outcome"]) for c in w.completes]
    assert outs == [("wallets.score", "partial"), ("add.fetch", "done"), ("wallets.score", "done")], outs
    assert w.completes[0]["cursor"] == {"phase": "history"} and seen == [None, {"phase": "history"}]
    assert [c["gave_way"] for c in w.completes] == [True, False, False]
    assert w.job("wallets.score", run=rid)["partials"] == 0 and w.runs[rid]["state"] == "done"   # not counted
    assert {k for (r, k) in w.results if r == rid} == {"rubric-hist:0x1:1", "rubric-hist:0x2:2"}


def serve_at(w, t, handlers):
    """One call on the add lane against the fake, on the clock t[0] (seconds, which the handlers move)."""
    orig = runtime.load_handler
    runtime.load_handler = lambda kind: handlers[kind] if kind in handlers else orig(kind)
    try:
        return runtime.serve("add", t[0], environ=dict(F.ENV), session=F.FakeSession(w), mono=lambda: t[0],
                             tmp_root=os.path.join(F.SCRATCH, "ro"), log=lambda m: None)
    finally:
        runtime.load_handler = orig


def test_after_a_give_way_the_call_claims_again_only_with_time_for_the_lane():
    """A step that gave way with less left than the lane's longest need_s (add.fetch's 120 s) sends continuing
    false and the call ends: the Worker wakes the lane for a fresh call, since this one could not take the add's
    fetch. With more left, the same call claims the fetch, then the scoring again."""
    assert runtime.lane_need_s("add") == 120 and runtime.lane_need_s("main") == 150
    for spent, more in ((160, False), (60, True)):
        w = F.FakeWorker()
        rid = scoring(w)
        t, calls = [1000.0], []

        def score(ctx):
            calls.append(round(ctx.left()))
            if len(calls) == 1:
                t[0] += spent                                             # the step's work so far
                w.add_run("add", "0x" + "ef" * 32 + ":2")                 # a visitor's add meanwhile
                ctx.checkpoint(cursor={"phase": "score"})
                return ctx.partial(ctx.cursor) if ctx.give_way() else ctx.done()
            return ctx.done()

        handlers = {"wallets.score": score, "add.fetch": lambda ctx: ctx.done(stop_run=True)}
        serve_at(w, t, handlers)
        first = w.completes[0]
        assert first["outcome"] == "partial" and first["gave_way"] is True and first["continuing"] is more, first
        assert w.job("wallets.score", run=rid)["partials"] == 0
        if more:
            outs = [(c["job_id"].split("|")[1], c["outcome"]) for c in w.completes]
            assert outs == [("wallets.score", "partial"), ("add.fetch", "done"), ("wallets.score", "done")], outs
        else:
            assert [c["left_s"] for c in w.claims] == [270, 0] and len(w.completes) == 1   # the call ended
            t[0] += 30                                                    # the fresh call the Worker wakes
            serve_at(w, t, handlers)
            outs = [(c["job_id"].split("|")[1], c["outcome"]) for c in w.completes[1:]]
            assert outs == [("add.fetch", "done"), ("wallets.score", "done")], outs
        assert w.runs[rid]["state"] == "done" and calls[0] == 270


def test_stop_run_cancels_the_rest():
    w, rid = fresh()
    F.serve(w, "add", handlers={"add.fetch": lambda ctx: ctx.done(stop_run=True)})
    assert w.job("add.export")["state"] == "cancelled" and w.runs[rid]["state"] == "partial"


def test_calls_in_one_process_keep_their_deadlines_apart():
    w1, _ = fresh()
    w2, _ = fresh()
    seen = {}

    def slow(ctx):
        time.sleep(0.2)
        seen[threading.current_thread().name] = round(outcomes.left())

    t1 = threading.Thread(target=F.serve, args=(w1, "add"), kwargs={"handlers": {"add.fetch": slow}}, name="early")
    t2 = threading.Thread(target=F.serve, args=(w2, "add"), kwargs={"late_s": 100, "handlers": {"add.fetch": slow}},
                          name="late")
    t1.start(), t2.start(), t1.join(), t2.join()
    assert seen["early"] >= 269 and 168 <= seen["late"] <= 170, seen
    assert outcomes.left() is None


def test_the_whole_function_with_a_signed_wake():
    import api.step as step
    w, _ = fresh()
    env = dict(F.ENV)
    orig_serve = runtime.serve

    def serve(lane, t0, environ=None, cpu0=None, **kw):
        return orig_serve(lane, t0, environ=environ, cpu0=cpu0, session=F.FakeSession(w), log=lambda m: None,
                          tmp_root=os.path.join(F.SCRATCH, "ro"))

    orig = runtime.load_handler
    runtime.serve, runtime.load_handler = serve, (lambda kind: lambda ctx: None)
    try:
        assert step.run("v1,1,add,0123456789abcdef," + "0" * 64, time.monotonic(), env) == (401, {})
        assert not w.claims
        status, body = step.run(wake.sign(env["WAKE_SECRET"], "add"), time.monotonic(), env)
        assert (status, body) == (200, {"lane": "add", "jobs": 2}), (status, body)
    finally:
        runtime.serve, runtime.load_handler = orig_serve, orig


def test_the_worker_client_retries_and_refuses():
    w = F.FakeWorker()
    c = runtime.WorkerClient("http://fake-worker.test", "job-token", session=F.FakeSession(w), sleep=lambda s: None)
    w.fail["/api/job/work"] = [sync.ApiError(503, "busy"), sync.ApiError(429, "busy")]
    assert c.request("GET", "/api/job/work")["requests"] == []
    w.fail["/api/job/work"] = [sync.ApiError(401, "unauthorized")]
    try:
        c.request("GET", "/api/job/work")
        raise AssertionError("no error")
    except sync.ApiError as e:
        assert e.status == 401
    late = runtime.WorkerClient("http://fake-worker.test", "job-token", session=F.FakeSession(w),
                                end=time.monotonic() + 0.5)
    try:
        late.request("GET", "/api/job/work")
        raise AssertionError("no error")
    except sync.ApiError as e:
        assert e.code == "network"


def test_the_dev_server():
    import json
    import random
    import socket
    import subprocess
    import urllib.error
    import urllib.request

    def post(port, header=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/step", data=b"", method="POST",
                                     headers={"x-ro-wake": header} if header else {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def free_port():
        for _ in range(50):
            port = random.randint(9500, 9999)
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)):
                    return port
        raise AssertionError("no free port in 9500-9999")

    secret = "dev-wake-secret"
    for max_s, api_url in (("300", None), ("1", f"http://127.0.0.1:{free_port()}")):
        port = free_port()
        env = {k: v for k, v in os.environ.items() if k not in ("WATCHLIST_API_URL", "WATCHLIST_JOB_TOKEN")}
        env.update(WAKE_SECRET=secret, RO_TMP_ROOT=os.path.join(F.SCRATCH, "dev"))
        if api_url:                                             # a Worker that never answers: the call runs past 1 s
            env.update(WATCHLIST_API_URL=api_url, WATCHLIST_JOB_TOKEN="t")
        p = subprocess.Popen([sys.executable, "-m", "backend.dev_server", "--port", str(port), "--max-s", max_s],
                             cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            assert "listening" in p.stdout.readline()
            assert post(port) == (403, {})
            assert post(port, "x") == (403, {})                    # vercel.json's rule: only a well-formed header
            assert post(port, "v1,1700000000,main,0123456789abcdef," + "0" * 64) == (401, {})
            if api_url:
                assert post(port, wake.sign(secret, "news")) == (504, {"error": "FUNCTION_INVOCATION_TIMEOUT"})
            else:
                assert post(port, wake.sign(secret, "news")) == (200, {"lane": "news", "jobs": 0})
        finally:
            p.terminate()
            p.wait(10)


def test_redaction():
    env = {"WAKE_SECRET": "s3cret-wake", "GEMINI_API_KEY": "", "WATCHLIST_JOB_TOKEN": "abc"}
    got = runtime.redact("x s3cret-wake https://g/?key=AIza123&y=1 bearer zzz abc\nmore", env)
    assert got == "x [redacted] https://g/?key=[redacted]&y=1 bearer [redacted] abc", got
    assert len(runtime.redact("y" * 900, env)) == 500


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
