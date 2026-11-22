#!/usr/bin/env python3
"""
The wallets.score step against the fake Worker (tests/test_backend_fakeworker.py), the scripted Polymarket of
tests/test_add_pipeline.py and the fake Decisions API of tests/test_rubric_scoring.py. No network, no Gemini.

    python3 tests/test_rubric_step.py

Covers:
  - an add run with a Gemini key set: add.export spawns wallets.score, which walks the fills, reads every whale's
    history, scores every whale and publishes the page again: traders.rubric, a spectrum on every trader, the
    index entry's spectrum, the record and the rubric:<name> blob (answers, facts, page part); the page is the
    one add_market.build makes with that page part;
  - no WALLET_SCORES=on and key where the step runs (or score_on_add off): no wallets.score, the page as before;
  - the scorer busy part way: the step comes back later and asks only the whales not answered yet;
  - a visitor's add made while the step walks the fills, reads the histories and asks its questions: each time the
    step gives way (partial from that phase's cursor), the add's fetch goes next, and the step resumes from what it
    saved: no Polymarket request made twice and no whale asked twice; one made while the busy scorer waits to try
    again: the step gives way there, not after its retries; one made after a resumed step's claim, before its
    facts: it asks first, and the facts are built once;
  - both scorers out for the day: the step comes back when the quota resets, the lane left running;
  - a backfill job for a market already scored: the saved facts reused (no fill or history request), no
    Decisions request, the page left alone; after a question is reworded: only that question asked, the page
    published again with the new version;
  - backend.site rebuilding a page (put_originals' and migrate's path) keeps the scores.
"""
import contextlib
import json
import os
import sys
import threading
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_add_pipeline as P  # noqa: E402
import test_backend_fakeworker as F  # noqa: E402
import test_rubric_scoring as S  # noqa: E402

from pipeline import add_market as A  # noqa: E402
from pipeline import polymarket_api as api  # noqa: E402
from backend import add_steps  # noqa: E402
from backend.rubric import scorers, spec, steps, store  # noqa: E402
from watchlist import ai, blobs  # noqa: E402


@contextlib.contextmanager
def world(fx, counts=None, key="test-key", rubric=None):
    """The scripted Polymarket (with the history endpoints the facts read), the fake Decisions API, a Gemini key
    in the environment (or none), no waits."""
    fk = S.fake()
    lock = threading.Lock()

    def get_json(url, params=None, timeout=30, retry=api.RETRY):
        params = dict(params or {})
        with lock:
            if counts is not None:
                k = url.rsplit("/", 1)[-1]
                counts[k] = counts.get(k, 0) + 1
        if url.endswith("/v2/positions"):
            return {"data": [], "pagination": {"has_more": False}}
        if url.endswith("/v2/user-stats"):
            return {"data": {"join_date": 1_780_000_000}}
        if url.endswith("/v2/activity") and params.get("sort_direction") == "ASC":
            return {"data": [{"timestamp": 1_780_000_000}]}
        return P.serve(fx, url, params)                                      # noqa: F821 (exec'd there)

    saved = (api.get_json, os.environ.get("GEMINI_API_KEY"), os.environ.get("WALLET_SCORES"), scorers.DECISIONS_URL,
             steps.POLYMARKET_RATE, ai.MAX_BACKOFF, spec.load_rubric)
    api.get_json = get_json
    if key:
        os.environ["GEMINI_API_KEY"], os.environ["WALLET_SCORES"] = key, "on"
    else:
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("WALLET_SCORES", None)
    scorers.DECISIONS_URL, steps.POLYMARKET_RATE, ai.MAX_BACKOFF = fk.url, 0, 0.01
    if rubric is not None:
        spec.load_rubric = lambda path=None: rubric
    try:
        with P.quiet():
            yield fk
    finally:
        api.get_json, key0, on0, scorers.DECISIONS_URL, steps.POLYMARKET_RATE, ai.MAX_BACKOFF, spec.load_rubric = saved
        for k, v in (("GEMINI_API_KEY", key0), ("WALLET_SCORES", on0)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def drive(w, rid, rounds=80):
    for _ in range(rounds):
        F.serve(w, "add")
        if w.runs[rid]["state"] not in ("queued", "running"):
            break
        for j in w.jobs.values():
            if j["state"] == "queued":
                j["run_after"] = 0


def add_run(w, slot=1):
    return w.add_run("add", f"{P.CID}:{slot}", input={"condition_id": P.CID, "slug": P.SLUG, "winner": "Yes",
                                                     "name": None})


def score_run(w, name, slot):
    rid = f"add:rubric-{slot}"                    # a re-score is add-lane work (the apply run kind is gone)
    w.runs[rid] = {"id": rid, "kind": "add", "slot": f"rubric-{slot}", "state": "queued", "meta": {},
                   "deadline": w.now() + 3600}
    w._insert(rid, "wallets.score", "0", {"name": name}, [])
    return rid


def page(w, name):
    h, data = blobs.get(w.blobs, f"site:markets/{name}.json")
    return h, json.loads(data)


def test_an_add_scores_its_whales():
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx) as fk:
        rid = add_run(w)
        drive(w, rid)
    jobs = {j["kind"]: j for j in w.jobs.values() if j["run_id"] == rid}
    assert w.runs[rid]["state"] == "done", [(j["id"], j["state"], j["error"]) for j in w.jobs.values()]
    name = w.runs[rid]["meta"]["name"]
    assert jobs["wallets.score"]["state"] == "done" and jobs["wallets.score"]["input"] == {"name": name}
    h, pg = page(w, name)
    traders = pg["traders"]["traders"]
    assert pg["traders"]["rubric"]["version"] == spec.load_rubric()["version"]
    assert len(traders) >= 8 and all(t.get("spectrum") for t in traders), [t.get("spectrum") for t in traders]
    assert len(fk.bodies) == len(traders)                                   # one request a whale
    rb_h, rb = store.read(w.blobs, name)
    assert rb["version"] == pg["traders"]["rubric"]["version"] and len(rb["facts"]["whales"]) == len(traders)
    assert len(rb["answers"]) == len(traders) * len(spec.load_rubric()["questions"])
    entry, clean, insiders, cid, added_at = store.record_of(w.blobs, name)
    index, built, _ = A.build(entry, clean, insiders, spectrum=rb["page"])
    assert built == pg and cid == P.CID
    from backend import site
    _, idx = site.read_index(w.blobs)
    assert idx == [dict(index, v=h["version"])] and idx[0]["spectrum"]["name"] == "Insider score"
    assert sum(b["count"] for b in idx[0]["spectrum"]["bands"]) == len(traders)
    assert w.runs[rid]["meta"]["rubric"]["scored"] == len(traders)

    # a backfill of the same market: facts reused, nothing asked, the page left alone
    counts = {}
    with world(fx, counts) as fk:
        rid2 = score_run(w, name, 2)
        drive(w, rid2)
    assert w.runs[rid2]["state"] == "done" and counts == {} and fk.bodies == []
    assert page(w, name)[0]["version"] == h["version"]

    # one question reworded: only it is asked, the page goes out again
    text = S.RUBRIC_TEXT.replace("Judge how established the wallet was before its first trade in this market.",
                                 "Judge how long and how widely the wallet had traded before this market.")
    text = text.replace('version = "', 'version = "2-', 1)
    with world(fx, counts, rubric=S.rubric_from(text)) as fk:
        rid3 = score_run(w, name, 3)
        drive(w, rid3)
    assert w.runs[rid3]["state"] == "done" and counts == {}
    assert [q for _, q in fk.asked()] == [["account"]] * len(traders)
    h3, pg3 = page(w, name)
    assert h3["version"] != h["version"] and pg3["traders"]["rubric"]["version"].startswith("2-")

    # a page built again by backend.site keeps the scores
    from backend import site
    assert site._scores(w.blobs, name) == store.read(w.blobs, name)[1]["page"]


def test_no_key_no_scoring():
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx, key=None) as fk:
        rid = add_run(w)
        drive(w, rid)
    assert w.runs[rid]["state"] == "done" and not any(j["kind"] == "wallets.score" for j in w.jobs.values())
    name = w.runs[rid]["meta"]["name"]
    assert "rubric" not in page(w, name)[1]["traders"] and fk.bodies == []
    off = S.rubric_from(S.RUBRIC_TEXT.replace("score_on_add = true", "score_on_add = false"))
    with world(fx, rubric=off):
        assert add_steps.score_on_add() is False
    with world(fx):
        assert add_steps.score_on_add() is True and add_steps.score_on_add({}) is False
        assert add_steps.score_on_add({"GEMINI_API_KEY": "k"}) is False          # the e2e stack's settings
        assert add_steps.score_on_add({"GEMINI_API_KEY": "k", "WALLET_SCORES": "on"}) is True


def test_busy_comes_back_for_the_rest():
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx, key=None):
        rid = add_run(w)
        drive(w, rid)
    name = w.runs[rid]["meta"]["name"]
    n = len(page(w, name)[1]["traders"]["traders"])
    with world(fx) as fk:
        rid2 = score_run(w, name, 2)
        fk.answer_only = 3                                  # three answers, then busy through every retry
        F.serve(w, "add")
        job = w.job("wallets.score", run=rid2)
        assert job["state"] == "queued" and job["laters"] == 1 and len(fk.bodies) == 3 + scorers.RETRIES, job
        answered = [b["input"] for b in fk.bodies[:3]]
        fk.bodies.clear()
        fk.answer_only = None
        job["run_after"] = 0
        drive(w, rid2)
    assert w.runs[rid2]["state"] == "done"
    inputs = [b["input"] for b in fk.bodies]
    assert len(inputs) == n - 3 and not set(inputs) & set(answered)
    assert all(t.get("spectrum") for t in page(w, name)[1]["traders"]["traders"])


def test_a_scoring_gives_way_to_an_add():
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx, key=None):
        rid = add_run(w)
        drive(w, rid)
    name = w.runs[rid]["meta"]["name"]
    n = len(page(w, name)[1]["traders"]["traders"])
    # a visitor's add made at the fills walk's first ask, the 3rd history saved and the 2nd answers saved: that
    # checkpoint's answer is {"give_way": true}
    plan, hits, adds = [(None, 1), ("rubric-hist:", 3), ("rubric-ans:", 2)], [0], []
    real_checkpoint, real_complete = w.checkpoint, w.complete

    def checkpoint(body):
        keys = [r["key"] for r in body.get("results") or []]
        if plan and (not keys if plan[0][0] is None else any(k.startswith(plan[0][0]) for k in keys)):
            hits[0] += 1
            if hits[0] == plan[0][1]:
                plan.pop(0)
                hits[0] = 0
                adds.append(w.add_run("add", f"0x{'cd' * 32}:{len(adds)}"))
        return real_checkpoint(body)

    steps_seen = []                       # each wallets.score step: outcome, cursor phase, histories, requests

    def complete(body):
        if "|wallets.score|" in body["job_id"]:
            steps_seen.append((body["outcome"], (body.get("cursor") or {}).get("phase"),
                               sum(1 for (r, k) in w.results if k.startswith("rubric-hist:")), len(fk.bodies)))
        return real_complete(body)

    w.checkpoint, w.complete = checkpoint, complete
    saved = steps.ASK_S, steps.WHALE_THREADS
    steps.ASK_S, steps.WHALE_THREADS = 0, 1                 # ask after every fills page; one whale at a time
    asked, lock = [], threading.Lock()
    try:
        with world(fx) as fk:
            inner = api.get_json

            def get_json(url, params=None, timeout=30, retry=api.RETRY):
                with lock:
                    asked.append((url.rsplit("/", 1)[-1], json.dumps(params or {}, sort_keys=True)))
                return inner(url, params, timeout, retry)

            api.get_json = get_json
            start = len(w.completes)
            rid2 = score_run(w, name, 2)
            drive(w, rid2)
    finally:
        steps.ASK_S, steps.WHALE_THREADS = saved
    order = [f"{c['job_id'].split('|')[1]} {c['outcome']}" for c in w.completes[start:]]
    assert order == ["wallets.score partial", "add.fetch done"] * 3 + ["wallets.score done"], order
    assert len(adds) == 3 and not plan
    hist = steps_seen[-1][2]
    assert steps_seen == [("partial", "fills", 0, 0), ("partial", "history", 3, 0), ("partial", "score", hist, 2),
                          ("done", None, hist, n)], steps_seen
    assert w.completes[start]["cursor"]["fills"]["blob"].startswith("scratch:")
    assert hist == n and len(asked) == len(set(asked)) and sum(1 for k, _ in asked if k == "trades") >= 2
    inputs = [b["input"] for b in fk.bodies]
    assert len(set(inputs)) == len(inputs) == n                              # no whale asked twice
    assert w.runs[rid2]["state"] == "done" and all(t.get("spectrum") for t in page(w, name)[1]["traders"]["traders"])


def test_a_busy_scorer_gives_way_in_its_back_off():
    """The Decisions API busy (503) and a visitor's add made while the scorer waits to try again: the step gives
    way there (partial from the score phase, not counted) instead of after its retries; the add's fetch goes next
    and the step comes back for the whales not answered yet."""
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx, key=None):
        rid = add_run(w)
        drive(w, rid)
    name = w.runs[rid]["meta"]["name"]
    n = len(page(w, name)[1]["traders"]["traders"])
    adds, seen = [], []
    real_checkpoint, real_complete = w.checkpoint, w.complete

    def checkpoint(body):                       # the scorer's first wait to try again asks: the add comes then
        if not adds and "results" not in body and len(fk.bodies) == 3:
            adds.append(w.add_run("add", f"0x{'cd' * 32}:9"))
            fk.answer_only = None                                   # and the API answers again
        return real_checkpoint(body)

    def complete(body):
        if "|wallets.score|" in body["job_id"]:
            seen.append((body["outcome"], (body.get("cursor") or {}).get("phase"), len(fk.bodies)))
        return real_complete(body)

    w.checkpoint, w.complete = checkpoint, complete
    saved = steps.ASK_S
    steps.ASK_S = 0
    try:
        with world(fx) as fk:
            start = len(w.completes)
            rid2 = score_run(w, name, 2)
            fk.answer_only = 2                                      # two answers, then busy
            drive(w, rid2)
    finally:
        steps.ASK_S = saved
    order = [f"{c['job_id'].split('|')[1]} {c['outcome']}" for c in w.completes[start:]]
    assert order == ["wallets.score partial", "add.fetch done", "wallets.score done"], order
    assert seen == [("partial", "score", 3), ("done", None, n + 1)], seen          # 3: no retry after the 503
    assert w.job("wallets.score", run=rid2)["partials"] == 0 and w.runs[rid2]["state"] == "done"
    answered = [b["input"] for i, b in enumerate(fk.bodies) if i != 2]
    assert len(set(answered)) == len(answered) == n                                 # no whale asked twice


def test_a_resumed_scoring_asks_before_the_facts():
    """A scoring resumed with every history saved asks the Worker before it builds the facts, so an add made
    since its claim goes first and the facts are built once, after it."""
    from backend.rubric import wallet_facts as wf
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx, key=None):
        rid = add_run(w)
        drive(w, rid)
    name = w.runs[rid]["meta"]["name"]
    n = len(page(w, name)[1]["traders"]["traders"])
    adds, reads, seen, built = [], [], [], []
    real_checkpoint, real_complete, real_request, real_facts = w.checkpoint, w.complete, w.request, wf.market_facts

    def checkpoint(body):                       # the last history saved: an add (the step stops before the facts)
        if not adds and sum(k.startswith("rubric-hist:") for (r, k) in w.results if r == rid2) == n - 1 and \
                any(r["key"].startswith("rubric-hist:") for r in body.get("results") or []):
            adds.append(w.add_run("add", f"0x{'cd' * 32}:1"))
        return real_checkpoint(body)

    def request(method, path, body=None):       # the resumed step reads its histories: another add, after its claim
        if "rubric-hist" in urllib.parse.unquote(path):
            reads.append(path)
            if len(reads) == 2:
                adds.append(w.add_run("add", f"0x{'cd' * 32}:2"))
        return real_request(method, path, body)

    def complete(body):
        if "|wallets.score|" in body["job_id"]:
            seen.append((body["outcome"], (body.get("cursor") or {}).get("phase")))
        return real_complete(body)

    def facts(*a, **kw):
        built.append(1)
        return real_facts(*a, **kw)

    w.checkpoint, w.request, w.complete, wf.market_facts = checkpoint, request, complete, facts
    saved = steps.ASK_S, steps.WHALE_THREADS
    steps.ASK_S, steps.WHALE_THREADS = 0, 1
    try:
        with world(fx) as fk:
            start = len(w.completes)
            rid2 = score_run(w, name, 2)
            drive(w, rid2)
    finally:
        steps.ASK_S, steps.WHALE_THREADS = saved
        wf.market_facts = real_facts
    order = [f"{c['job_id'].split('|')[1]} {c['outcome']}" for c in w.completes[start:]]
    assert order == ["wallets.score partial", "add.fetch done"] * 2 + ["wallets.score done"], order
    assert seen == [("partial", "history"), ("partial", "history"), ("done", None)], seen
    assert len(adds) == 2 and len(built) == 1 and len(fk.bodies) == n and w.runs[rid2]["state"] == "done"


def test_out_for_the_day_waits_for_the_reset():
    """Decisions' daily quota spent and every fallback model answering 429 "per day": the step comes back when the
    quota resets (later, model_quota, the add lane not paused), then asks every whale and publishes."""
    from backend import contract
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx, key=None):
        rid = add_run(w)
        drive(w, rid)
    name = w.runs[rid]["meta"]["name"]
    n = len(page(w, name)[1]["traders"]["traders"])
    models = spec.load_rubric()["scorer"]["interactions"]["models"]
    day = {"error": {"message": "You exceeded your current quota: requests, limit: 10000 per day"}}
    saved_url = ai.URL
    with world(fx) as fk:
        ai.URL = fk.url                                     # the fallback's requests reach the fake, never Google
        try:
            rid2 = score_run(w, name, 2)
            w.runs[rid2]["deadline"] = w.now() + 36 * 3600   # an add run's deadline
            fk.script += [(429, day)] * (1 + len(models))
            F.serve(w, "add")
            job = w.job("wallets.score", run=rid2)
            later = w.completes[-1]
            assert job["state"] == "queued" and job["laters"] == 1, job
            assert later["outcome"] == "later" and later["reason"] == "model_quota" and later["pause_lane_until"] is None
            assert job["run_after"] == contract.next_quota_reset(w.now()) and len(fk.bodies) == 1 + len(models)
            assert "rubric" not in page(w, name)[1]["traders"]
            fk.bodies.clear()
            job["run_after"] = 0                            # the reset
            drive(w, rid2)
        finally:
            ai.URL = saved_url
    assert w.runs[rid2]["state"] == "done" and len(fk.bodies) == n
    assert all(t.get("spectrum") for t in page(w, name)[1]["traders"]["traders"])


def test_the_cli_scores_and_publishes():
    import tempfile

    from backend.rubric import __main__ as cli, fact_sources, wallet_facts as wf
    from watchlist import sync
    fx = P.fixture()
    w = F.FakeWorker()
    with world(fx, key=None):
        rid = add_run(w)
        drive(w, rid)
    name = w.runs[rid]["meta"]["name"]
    d = tempfile.mkdtemp()
    facts, cache, out = (os.path.join(d, k) for k in ("facts", "cache", "scores"))
    with world(fx) as fk:
        entry, clean, insiders, cid, _ = store.record_of(w.blobs, name)
        fills = A.Fills(api.fetch_market(P.SLUG))
        for pg in api.v2_pages("trades", A.trade_params(cid)):
            fills.add_page(pg)
        doc = wf.market_facts(fact_sources.from_record(entry, clean, insiders, fills),
                              wf.Polymarket(None, rate=0, getter=api.get_json))
        os.makedirs(facts)
        with open(os.path.join(facts, f"{name}.json"), "w") as f:
            json.dump(doc, f)
        args = ["--facts", facts, "--cache", cache, "--out", out]
        assert cli.main(["score", "--market", name, "--dry-run"] + args) == 0 and fk.bodies == []
        assert cli.main(["score", "--all"] + args) == 0 and len(fk.bodies) == len(doc["whales"])
        assert cli.main(["score", "--all"] + args) == 0 and len(fk.bodies) == len(doc["whales"])    # cached
        assert cli.main(["show"] + args) == 0 and cli.main(["show", "--market", name] + args) == 0
        assert cli.main(["show", "--market", name, "--wallet", doc["whales"][0]["user_id"]] + args) == 0
        assert cli.main(["compare", "--all"] + args) == 0
        saved = sync.client
        sync.client = lambda: w.blobs
        try:
            assert cli.main(["publish", "--market", name] + args) == 0
        finally:
            sync.client = saved
    h, pg = page(w, name)
    assert all(t.get("spectrum") for t in pg["traders"]["traders"]) and pg["traders"]["rubric"]
    rb = store.read(w.blobs, name)[1]
    assert rb["facts"] == doc and len(rb["answers"]) == len(doc["whales"]) * len(spec.load_rubric()["questions"])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    try:
        for t in tests:
            t()
            print(f"PASS {t.__name__}", flush=True)
            ok += 1
    finally:
        if S.FAKE is not None:
            S.FAKE.close()
    print(f"\n{ok}/{len(tests)} checks passed")
