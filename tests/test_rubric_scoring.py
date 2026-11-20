#!/usr/bin/env python3
"""
The wallet rubric's scoring, offline: a fake Decisions API on a local port (9900-9999), a fake regular-Gemini
client, made-up whales. Nothing reaches Google; the key sent is "test-key".

    python3 tests/test_rubric_scoring.py

Covers:
  - the rubric file's check: the real file passes; typos, a missing noul description, bands out of order, an
    unknown factor or question, a colour that is no token each named;
  - a full scoring through the HTTP client: every question type parsed, scores and bands, the page fields;
  - editing only the rubric file: a question's wording re-asks that question alone; weights, bands, a level's
    site label and a version bump cost no call and change the results; removing a factor re-asks everything
    with the factor gone from the text; a version bump with new wording re-asks only that question;
  - cache hits skip calls; --refresh asks again;
  - 429 and 503 retried with back-off; a daily quota answer switches to the fallback scorer, whose answers are
    cached and reused; a 400 leaves only that wallet unscored; the daily cap; the pacer's token window;
  - export.py attaching the scores to a page and the index entry, and leaving a page without them as it was.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.rubric import cache as cache_mod, combine, pipeline, prompt, scorers, spec  # noqa: E402

RUBRIC_TEXT = open(os.path.join(ROOT, "rubric", "wallet_rubric.toml"), encoding="utf-8").read()
KEY = "test-key"


# ── the fake Decisions API ───────────────────────────────────────────────────

def _h(*parts):
    return int(hashlib.sha1("|".join(parts).encode()).hexdigest()[:8], 16)


def fake_answer(body):
    """A deterministic answer: each question's likeliest level / option from a hash of the input and the
    question's own text, so new wording gives a new answer."""
    out = {}
    for qid, q in body["questions"].items():
        h = _h(body["input"], qid, json.dumps(q, sort_keys=True))
        if q["type"] == "score":
            n = len(q["criteria"])
            top = h % n
            probs = {str(i): (0.7 if i == top else 0.3 / (n - 1)) for i in range(n)}
            out[qid] = {"type": "score", "score": sum(i * p for i, p in ((int(k), v) for k, v in probs.items())),
                        "probabilities": probs, "confidence": 0.7, "legend": {str(i): c for i, c in enumerate(q["criteria"])}}
        elif q["type"] == "noul":
            assert set(q["criteria"]) == {"true_description", "false_description"}
            p = 0.8 if h % 2 else 0.2
            out[qid] = {"type": "noul", "noul": p, "confidence": max(p, 1 - p)}
        else:
            keys = sorted(q["criteria"])
            best = keys[h % len(keys)]
            out[qid] = {"type": "choice", "choice": best, "confidence": 0.6,
                        "probabilities": {k: (0.6 if k == best else 0.4 / (len(keys) - 1)) for k in keys}}
    return {"answers": out, "usage": {"total_tokens": len(json.dumps(body)) // 4, "total_output_tokens": 0}}


class FakeDecisions:
    """POST /v1beta/decisions on 127.0.0.1:99xx. script: (status, payload) answers given first, in order; bodies: every
    request body; blocked: inputs answered with prompt_feedback.block_reason; refuse: inputs answered 400;
    answer_only: after this many answers, every request is answered 503 (busy)."""

    def __init__(self):
        self.bodies, self.script, self.blocked, self.refuse = [], [], set(), set()
        self.answer_only, self.answered = None, 0
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("x-goog-api-key") != KEY:
                    return self._send(401, {"error": {"message": "API key not valid"}})
                with fake.lock:
                    fake.bodies.append(body)
                    scripted = fake.script.pop(0) if fake.script else None
                if scripted:
                    return self._send(*scripted)
                if any(s in body["input"] for s in fake.refuse):
                    return self._send(400, {"error": {"message": "invalid input", "code": "invalid_request"}})
                if any(s in body["input"] for s in fake.blocked):
                    return self._send(200, {"answers": {}, "prompt_feedback": {"block_reason": "other"}})
                with fake.lock:
                    if fake.answer_only is not None and fake.answered >= fake.answer_only:
                        busy = True
                    else:
                        busy, fake.answered = False, fake.answered + 1
                if busy:
                    return self._send(503, {"error": {"message": "The service is overloaded."}})
                self._send(200, fake_answer(body))

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        for port in range(9971, 10000):
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        else:
            raise RuntimeError("no free port in 9971-9999")
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1beta/decisions"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def asked(self):
        """[(input, sorted question ids)] of every request."""
        return [(b["input"], sorted(b["questions"])) for b in self.bodies]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


FAKE = None


def fake():
    global FAKE
    if FAKE is None:
        FAKE = FakeDecisions()
    FAKE.bodies.clear()
    FAKE.script.clear()
    FAKE.blocked.clear()
    FAKE.refuse.clear()
    FAKE.answer_only, FAKE.answered = None, 0
    return FAKE


# ── made-up whales ───────────────────────────────────────────────────────────

def facts_doc(n=3):
    whales = []
    for i in range(n):
        f = {k: None for k in spec.registry()}
        f.update(hours_before_reveal=[9.0, 400.0, -2.0, 30.0][i % 4], entry_price=[0.08, 0.55, 0.97, 0.2][i % 4],
                 position_usd=12_400.0 + i, share_of_volume_pct=0.8, concentration_pct=[94.0, 4.0, 60.0, 99.0][i % 4],
                 prior_trades=[3, 900, 40, 0][i % 4], wallet_age_days=[2.0, 700.0, 90.0, 0.1][i % 4], prior_markets=1,
                 buy_fills=2, build_span_hours=0.5, sold_before_close_pct=0.0, fresh_whales_same_window=i)
        whales.append({"wallet": f"0x{i:040x}", "user_id": f"0x{i:040x}", "first_buy": f"2026-02-27T2{i}:00:00+00:00",
                       "position_side": "BUY", "current_rules": True, "facts": f,
                       "flags": {"category": "critical" if i == 0 else "normal", "is_suspicious": i == 0}})
    market = {"name": "t", "title": "Will it happen?", "outcome": "Yes", "volume_usd": 1_500_000.0, "whales": n,
              "reveal": "2026-02-28T06:45:00+00:00", "close": "2026-02-28T20:00:00+00:00"}
    return {"market": market, "factors": {}, "whales": whales}


def rubric_from(text):
    path = os.path.join(TMP, "r.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return spec.load_rubric(path)


def run(rubric, store=None, doc=None, **kw):
    fk = FAKE
    store = store if store is not None else cache_mod.AnswerCache()
    sc = scorers.get_scorer(rubric, "decisions", key=KEY, url=fk.url, sleep=lambda s: None)
    return pipeline.score_market(rubric, doc or facts_doc(), store, scorer=sc,
                                 make_scorer=kw.pop("make_scorer", None), **kw), store


TMP = tempfile.mkdtemp()


# ── the tests ────────────────────────────────────────────────────────────────

def test_the_rubric_file_checks():
    assert spec.check(spec.load_rubric()) == []
    bad = RUBRIC_TEXT.replace('label = "Timing"', 'lable = "Timing"', 1)
    bad = bad.replace('false_description = "The wallet has a life of its own beyond this bet."', "")
    bad = bad.replace("min = 40", "min = 10")
    bad = bad.replace('id = "buy_fills"', 'id = "buy_filz"')
    bad = bad.replace("overall = 0.40", "overal = 0.40")
    bad = bad.replace('color = "--c-critical"', 'color = "red"')
    got = "\n".join(spec.check(rubric_from(bad)))
    for want in ("question timing: unknown key 'lable'", "single_purpose: a noul question needs false_description",
                 "band mins must be numbers, rising", "factor buy_filz: no factor of that name",
                 "uses unknown factor 'buy_fills'", "combine.weights: unknown question 'overal'",
                 "band highly_suspicious: colour must be a --c-* token"):
        assert want in got, (want, got)


def test_scoring_through_the_api():
    fk = fake()
    rubric = spec.load_rubric()
    res, store = run(rubric)
    st = res["stats"]
    assert st["requests"] == 3 and st["unscored"] == 0 and st["stop"] is None, st
    assert all(sorted(q) == sorted(x["id"] for x in rubric["questions"]) for _, q in fk.asked())
    body = fk.bodies[0]
    assert body["model"] == "gemini-decision-1" and "Lead time: 9.0 hours" in body["input"]
    assert body["questions"]["profile"]["criteria"]["analyst"].startswith("Sharp analyst: ")
    for w in res["whales"]:
        a = w["answers"]
        assert {x["id"] for x in rubric["questions"]} == set(a)
        assert a["timing"]["type"] == "score" and len(a["timing"]["p"]) == 5 and abs(sum(a["timing"]["p"]) - 1) < 1e-3
        assert a["single_purpose"]["p"] in (0.2, 0.8) and a["profile"]["choice"] in {o["id"] for o in
                                                                                      rubric["questions"][-1]["options"]}
        score, band = combine.spectrum(rubric, a)
        assert (w["score"], w["band"]) == (score, band) and 0 <= score <= 100
        f = pipeline.trader_fields(w, rubric)
        assert set(f) == {"score", "band", "q", "facts"} and f["q"]["timing"]["level"] in range(5)
        assert set(f["facts"]) == {x["id"] for x in rubric["factors"] if x.get("card")}
    part = pipeline.page_part(res, rubric)
    assert part["rubric"]["bands"][-1]["max"] == 100 and len(part["whales"]) == 3
    assert next(iter(part["whales"])) == f"0x{0:040x}@{pipeline.epoch('2026-02-27T20:00:00+00:00')}"


def test_cache_hits_skip_calls():
    fk = fake()
    rubric = spec.load_rubric()
    res, store = run(rubric)
    fk.bodies.clear()
    again, _ = run(rubric, store)
    assert fk.bodies == [] and again["stats"]["cached"] == 3 and again["stats"]["requests"] == 0
    assert [w["score"] for w in again["whales"]] == [w["score"] for w in res["whales"]]
    path = os.path.join(TMP, "cache.json")
    store.path, store.dirty = path, True
    store.save()
    third, _ = run(rubric, cache_mod.AnswerCache(path))
    assert fk.bodies == [] and third["stats"]["requests"] == 0
    fresh, _ = run(rubric, cache_mod.AnswerCache(path), refresh=True, max_requests=1)
    assert len(fk.bodies) == 1 and fresh["stats"]["stop"] == "max_requests"


def test_editing_only_the_rubric_file():
    fk = fake()
    base = spec.load_rubric()
    res, store = run(base)
    before = {w["key"]: w for w in res["whales"]}

    # a question's wording: that question alone, for every wallet
    fk.bodies.clear()
    worded = RUBRIC_TEXT.replace("Judge how established the wallet was before its first trade in this market.",
                                 "Judge how long and how widely the wallet had traded before this market.")
    r2, _ = run(rubric_from(worded), store)
    assert [q for _, q in fk.asked()] == [["account"]] * 3, fk.asked()
    assert r2["stats"]["requests"] == 3

    # weights, bands, a level's site label and the version: no call, other results
    fk.bodies.clear()
    cheap = RUBRIC_TEXT.replace("overall = 0.40, timing = 0.20", "overall = 0.10, timing = 0.50")
    cheap = cheap.replace("min = 60", "min = 45").replace('{ label = "Brand new",', '{ label = "First bet",')
    cheap = cheap.replace('version = "', 'version = "9-', 1)
    r3, _ = run(rubric_from(cheap), store)
    assert fk.bodies == [] and r3["stats"]["requests"] == 0
    rub3 = rubric_from(cheap)
    assert r3["version"].startswith("9-") and r3["rubric"]["version"] == r3["version"]
    for w in r3["whales"]:
        old = before[w["key"]]
        assert w["answers"] == old["answers"]
        assert w["score"] == combine.spectrum(rub3, old["answers"])[0]
        assert w["band"] == spec.band_of(rub3, w["score"])
    assert any(w["score"] != before[w["key"]]["score"] for w in r3["whales"])
    assert r3["rubric"]["questions"][2]["levels"][-1] == "First bet"
    assert [b["min"] for b in r3["rubric"]["bands"]] == [0, 20, 40, 45, 80]

    # a version bump with new wording: only the reworded question
    fk.bodies.clear()
    bumped = cheap.replace("Judge how much of itself the wallet committed to this one outcome.",
                           "Judge how much of its money the wallet put on this one outcome.")
    run(rubric_from(bumped), store)
    assert [q for _, q in fk.asked()] == [["conviction"]] * 3

    # a factor taken out (block and uses): every question again, and the text no longer has it
    fk.bodies.clear()
    gone = re.sub(r'\[\[factors\]\]\nid = "buy_fills"\n(?:[^\n]+\n)+?card = false\n\n', "", RUBRIC_TEXT)
    gone = gone.replace('"buy_fills", ', "").replace(', "buy_fills"', "")
    rg = rubric_from(gone)
    assert spec.check(rg) == [] and "buy_fills" not in {f["id"] for f in rg["factors"]}
    r4, _ = run(rg, store)
    assert r4["stats"]["requests"] == 3 and all(len(q) == len(rg["questions"]) for _, q in fk.asked())
    assert all("- Fills:" not in i for i, _ in fk.asked()) and "- Fills:" in prompt.render_input(
        base, facts_doc()["market"], facts_doc()["whales"][0]["facts"])


def test_retries_fallback_and_refusals():
    fk = fake()
    rubric = spec.load_rubric()
    slept = []
    fk.script += [(429, {"error": {"message": "Resource exhausted. Please retry in 2s."}}),
                  (503, {"error": {"message": "The service is overloaded."}})]
    sc = scorers.get_scorer(rubric, "decisions", key=KEY, url=fk.url, sleep=slept.append)
    res = pipeline.score_market(rubric, facts_doc(1), cache_mod.AnswerCache(), scorer=sc)
    assert res["stats"]["requests"] == 1 and res["whales"][0]["score"] is not None and len(fk.bodies) == 3
    assert slept and slept[0] == 3.0                        # "retry in 2s" + 1 s

    # today's quota spent: the rest goes to the fallback, whose answers are cached under its own hash
    fk.bodies.clear()
    fk.script += [(429, {"error": {"message": "Quota exceeded for quota metric 'RequestsPerDay'"}})]

    class Client:
        calls = 0

        def generate(self, text, schema=None):
            Client.calls += 1
            ids = re.findall(r"=== wallet (\S+) ===", text)
            qs = schema["properties"]["wallets"]["items"]["properties"]
            rows = [{"id": i, **{q: {k: (1.0 if j == 0 else 0.0) for j, k in enumerate(v["properties"])}
                                 for q, v in qs.items() if q != "id"}} for i in ids]
            return json.dumps({"wallets": rows}), "fake-model", {}

    make = lambda name: scorers.get_scorer(rubric, name, key=KEY, url=fk.url, sleep=lambda s: None,  # noqa: E731
                                           client=Client())
    store = cache_mod.AnswerCache()
    res = pipeline.score_market(rubric, facts_doc(3), store, scorer=make("decisions"), make_scorer=make)
    assert res["scorer"] == "interactions" and Client.calls == 1 and len(fk.bodies) == 1
    assert all(w["score"] == 0.0 and w["band"] == "typical" for w in res["whales"])     # every level-0 answer
    fk.bodies.clear()
    again = pipeline.score_market(rubric, facts_doc(3), store, scorer=make("decisions"), make_scorer=make)
    assert fk.bodies == [] and Client.calls == 1 and again["stats"]["cached"] == 3

    # a 400 or a blocked input leaves that wallet unscored, the others scored
    fk.bodies.clear()
    doc = facts_doc(3)
    fk.refuse.add("Position: $12,401")
    fk.blocked.add("Position: $12,402")
    res = pipeline.score_market(rubric, doc, cache_mod.AnswerCache(), scorer=make("decisions"), make_scorer=make)
    assert [w["score"] is None for w in res["whales"]] == [False, True, True] and res["stats"]["refused"] == 2

    # the key refused: no fallback configured -> the run stops, nothing raised
    norub = rubric_from(RUBRIC_TEXT.replace('fallback = "interactions"', 'fallback = ""'))
    sc = scorers.get_scorer(norub, "decisions", key="wrong", url=fk.url, sleep=lambda s: None)
    res = pipeline.score_market(norub, facts_doc(2), cache_mod.AnswerCache(), scorer=sc)
    assert res["stats"]["stop"] == "unavailable" and res["stats"]["unscored"] == 2

    # the daily cap, across runs of one day (the ledger)
    led = cache_mod.Ledger(os.path.join(TMP, "usage.json"))
    capped = rubric_from(RUBRIC_TEXT.replace("daily_cap = 2500", "daily_cap = 2").replace(
        'fallback = "interactions"', 'fallback = ""'))
    sc = scorers.get_scorer(capped, "decisions", key=KEY, url=fk.url, sleep=lambda s: None, ledger=led)
    res = pipeline.score_market(capped, facts_doc(3), cache_mod.AnswerCache(), scorer=sc)
    assert res["stats"]["requests"] == 2 and res["stats"]["stop"] == "unavailable"
    assert cache_mod.Ledger(os.path.join(TMP, "usage.json")).used("decisions") == 2


def test_the_pacer_keeps_the_token_window():
    t = [0.0]
    waits = []
    p = scorers.Pacer(rpm=100, tpm=1000, clock=lambda: t[0], sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)))
    for _ in range(3):
        p.done(p.wait(400), 400)
    assert t[0] >= 60 and waits                 # the third request of 400 tokens waits for the first to age out
    t[0], waits[:] = 0.0, []
    q = scorers.Pacer(rpm=2, tpm=0, clock=lambda: t[0], sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)))
    for _ in range(3):
        q.wait(1)
    assert round(t[0]) == 60


def test_export_attaches_the_scores():
    import pandas as pd

    import export
    fake()
    rubric = spec.load_rubric()
    res, _ = run(rubric)
    part = pipeline.page_part(res, rubric)
    rows = [{"user_id": w["user_id"], "market_position": 1000.0 * (i + 1), "lifetime_portfolio": 5000.0,
             "prior_bet_count": 3, "portfolio_concentration": 50.0, "is_first_x": True, "Is_percent": False,
             "is_suspicious": True, "target_trade_time": w["first_buy"], "market_title": "T"}
            for i, w in enumerate(facts_doc()["whales"])]
    rows.append(dict(rows[0], user_id="0x" + "f" * 40))                # a row the rubric did not score
    df = export.load_insiders({"output": "x", "name": "t"}, df=pd.DataFrame(rows))
    _, plain = export.build_traders(df)
    _, scored = export.build_traders(df, part)
    assert "rubric" not in plain and all("spectrum" not in t for t in plain["traders"])
    assert scored["rubric"] == part["rubric"]
    assert [("spectrum" in t) for t in scored["traders"]] == [True, True, True, False]
    assert {k: v for k, v in scored.items() if k not in ("rubric", "traders")} == \
        {k: v for k, v in plain.items() if k != "traders"}
    for t, p in zip(scored["traders"], plain["traders"]):
        assert {k: v for k, v in t.items() if k != "spectrum"} == p
    s = export.spectrum_summary(scored)
    assert [b["id"] for b in s["bands"]] == [b["id"] for b in rubric["bands"]]
    assert sum(b["count"] for b in s["bands"]) == 3 and sum(b["stake"] for b in s["bands"]) == 6000
    assert export.spectrum_summary(plain) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    try:
        for t in tests:
            t()
            print(f"PASS {t.__name__}")
            ok += 1
    finally:
        if FAKE is not None:
            FAKE.close()
    print(f"\n{ok}/{len(tests)} checks passed")
