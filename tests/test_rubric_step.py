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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_add_pipeline as P  # noqa: E402
import test_backend_fakeworker as F  # noqa: E402
import test_rubric_scoring as S  # noqa: E402

import add_market as A  # noqa: E402
import polymarket_api as api  # noqa: E402
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
    rid = f"apply:rubric-{slot}"
    w.runs[rid] = {"id": rid, "kind": "apply", "slot": f"rubric-{slot}", "state": "queued", "meta": {},
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
