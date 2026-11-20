#!/usr/bin/env python3
"""
Offline tests for add_market.py (a market sent with "Add to main page"):
fake Polymarket (Gamma, the trade pages, the whales' history totals) in a
temporary copy of the repo's markets.json. No network.

    python3 tests/test_add_market.py

Covers the page name (given, or made from the slug when the job has none)
and its PascalCase output, avoiding the names and outputs of markets.json's and
added markets, Polymarket's market having to match (condition id, closed,
winner), a slug already on the main page (markets.json's, or added before)
reported as added with no work, a name or output already taken, the caps
(fills, the page's size, the data's size), a step that fails, and a dry
run. Whatever happens, markets.json stays byte for byte as it was and no
file is written (tests/test_add_pipeline.py checks the page itself).
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import add_market as A  # noqa: E402

CID = "0x" + "ab" * 32
MARKET = {"conditionId": CID, "closed": True, "umaResolutionStatus": "resolved", "question": "Will Gemini Carbon ship?",
          "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'}


def fill(i, size=10.0, price=0.5, outcome="Yes"):
    return {"proxy_wallet": "0x" + f"{i:040x}", "side": "BUY", "outcome": outcome,
            "outcome_index": 0 if outcome == "Yes" else 1, "size": size, "price": price, "timestamp": 1_790_000_000 + i,
            "transaction_hash": f"0x{i:064x}"}


class Fake:
    """Polymarket's three answers the add needs; records what ran."""

    def __init__(self, market=MARKET, fills=3, whale=True):
        self.market, self.fills, self.whale, self.ran = market, fills, whale, []

    def steps(self):
        def gamma(slug):
            self.ran.append(("gamma", slug))
            return dict(self.market)

        def pages(cid):
            self.ran.append(("pages", cid))
            rows = [fill(i) for i in range(1, self.fills + 1)]
            if self.whale and rows:
                rows.append(fill(999, size=5000.0, price=0.4))         # $2,000 on Yes: a whale
            yield rows[:2]
            yield rows[2:]

        def history(w, cutoff):
            self.ran.append(("history", w))
            return {"stake": 100.0, "txs": 3, "trades": 3, "capped": False}

        return {"gamma": gamma, "pages": pages, "history": history}


class repo:
    """A temporary root holding today's markets.json and an empty data/; yields it, then checks markets.json is
    byte for byte as it was and no scratch folder is left."""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "markets.json"), os.path.join(self.tmp, "markets.json"))
        os.makedirs(os.path.join(self.tmp, "data"))
        self.before = read(os.path.join(self.tmp, "markets.json"))
        return self.tmp

    def __exit__(self, *exc):
        try:
            if exc[0] is None:
                assert read(os.path.join(self.tmp, "markets.json")) == self.before, "markets.json changed"
                assert os.listdir(os.path.join(self.tmp, "data")) == [], os.listdir(os.path.join(self.tmp, "data"))
                assert sorted(os.listdir(self.tmp)) == ["data", "markets.json"], os.listdir(self.tmp)
        finally:
            shutil.rmtree(self.tmp)


def read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


def test_names():
    assert A.pascal("gemini-carbon") == "GeminiCarbon" and A.pascal("us-iran-2") == "UsIran2"
    assert A.pascal("x") == "X" and A.pascal("a--b") == "AB"
    for ok in ("gemini-carbon", "ab", "a1", "9-lives"):
        assert A.NAME.match(ok), ok
    for bad in ("a", "-ab", "Gemini", "gemini_carbon", "a" * 33, "gemini carbon", ""):
        assert not A.NAME.match(bad), bad
    assert A.bare_slug("event/market-1#123") == "market-1" and A.bare_slug("m?x=1") == "m"


def test_page_name_from_the_slug():
    markets = json.loads(read(os.path.join(ROOT, "markets.json")))
    assert A.page_name("event/Gemini_Carbon-Ship#9", markets) == "gemini-carbon-ship"
    cut = A.page_name("will-tit-me-pregunt-be-played-first-at-the-super-bowl", markets)
    assert cut == "will-tit-me-pregunt-be-played" and A.NAME.match(cut)         # cut at a word's end
    assert A.page_name("will-iran-target-oman-by-november-30-2026", markets) == "will-iran-target-oman-by"
    assert A.page_name("usiran", markets) == "usiran-2"                          # another entry has the name
    assert A.page_name("x", markets) == "market-x" and A.page_name("--", markets) == "market"
    assert A.page_name("gemini-carbon", markets + [{"name": "o", "output": "GeminiCarbon"}]) == "gemini-carbon-2"
    long = A.page_name("a" * 40, markets + [{"name": "a" * 32, "output": "A"}])
    assert long == "a" * 30 + "-2" and A.NAME.match(long)
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "data"))
        open(os.path.join(tmp, "data", "GeminiCarbon_clean.csv"), "w").close()     # files of that output name
        assert A.page_name("gemini-carbon", markets, root=tmp) == "gemini-carbon-2"
    finally:
        shutil.rmtree(tmp)


def test_adds_a_market():
    with repo() as tmp:
        f = Fake()
        got = A.add(CID.upper().replace("0X", "0x"), "event/gemini-carbon-ship#9", "gemini-carbon", "yes",
                    root=tmp, steps=f.steps())
        entry = {"name": "gemini-carbon", "slug": "gemini-carbon-ship", "output": "GeminiCarbon",
                 "correct_outcome": "Yes"}
        assert got["status"] == "added" and got["changed"] and got["note"] is None, got
        assert got["entry"] == entry and got["index"]["name"] == "gemini-carbon" == got["market"]["name"]
        assert got["index"]["slug"] == "gemini-carbon-ship" and got["index"]["positions"] == 4
        assert sorted(got["data"]) == ["GeminiCarbon_clean.csv", "GeminiCarbon_insiders.csv"]
        assert got["data"]["GeminiCarbon_clean.csv"].startswith("user_id,eoa_address,market_id,")
        assert "\r" not in got["data"]["GeminiCarbon_clean.csv"]
        assert got["market"]["traders"]["totalWhales"] == 1
        assert [r[0] for r in f.ran] == ["gamma", "pages", "history"]
        assert f.ran[0] == ("gamma", "gemini-carbon-ship") and f.ran[1] == ("pages", MARKET["conditionId"])
        # added before (the database's entries): already on the main page, nothing runs
        f2 = Fake()
        got = A.add(CID, "gemini-carbon-ship", "other-name", "Yes", root=tmp, steps=f2.steps(), added=[entry])
        assert got == {"status": "added", "name": "gemini-carbon", "note": "already on the main page",
                       "changed": False}
        assert [r[0] for r in f2.ran] == ["gamma"]


def test_already_on_the_main_page_through_markets_json():
    with repo() as tmp:
        usiran = next(m for m in json.loads(read(os.path.join(tmp, "markets.json"))) if m["name"] == "usiran")
        f = Fake()
        got = A.add(CID, usiran["slug"], None, "Yes", root=tmp, steps=f.steps())
        assert got == {"status": "added", "name": "usiran", "note": "already on the main page", "changed": False}
        assert [r[0] for r in f.ran] == ["gamma"]


def test_adds_a_market_named_from_its_slug():
    with repo() as tmp:
        got = A.add(CID, "event/gemini-carbon-ship#9", None, "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "added" and got["name"] == "gemini-carbon-ship" and got["changed"]
        assert got["entry"]["output"] == "GeminiCarbonShip"
        # the names and outputs of markets added before are taken too
        before = [got["entry"], {"name": "x9", "slug": "s9", "output": "OtherShip2"}]
        got = A.add(CID, "event2/gemini-carbon-ship", None, "Yes", root=tmp, steps=Fake().steps(),
                    added=[dict(before[0], slug="elsewhere"), {"name": "gemini-carbon-ship-2", "slug": "s2",
                                                               "output": "Q"}])
        assert got["name"] == "gemini-carbon-ship-3" and got["entry"]["output"] == "GeminiCarbonShip3"
        got = A.add(CID, "new-slug", "x9", "Yes", root=tmp, steps=Fake().steps(), added=before)
        assert got["status"] == "failed" and "page name 'x9' is taken" in got["note"]
        got = A.add(CID, "new-slug", "other-ship2", "Yes", root=tmp, steps=Fake().steps(), added=before)
        assert got["status"] == "failed" and "output name 'OtherShip2' is taken" in got["note"]
        # a failure before the name is made reports none
        got = A.add(CID, "other-slug", None, "Yes", root=tmp, steps=Fake(market=dict(MARKET, closed=False)).steps())
        assert got["status"] == "failed" and got["name"] is None and "not closed" in got["note"]


def test_polymarket_must_match():
    with repo() as tmp:
        cases = [(dict(MARKET, conditionId="0x" + "cd" * 32), "different condition id"),
                 (dict(MARKET, closed=False), "not closed"),
                 (dict(MARKET, outcomePrices='["0.5", "0.5"]'), "one winner"),
                 (dict(MARKET, outcomePrices='["0", "1"]'), "it resolved No, not Yes")]
        for market, words in cases:
            f = Fake(market=market)
            got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=f.steps())
            assert got["status"] == "failed" and words in got["note"], (words, got)
            assert [r[0] for r in f.ran] == ["gamma"]


def test_taken_names_and_files():
    with repo() as tmp:
        got = A.add(CID, "new-slug", "usiran", "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "failed" and "taken" in got["note"]
        got = A.add(CID, "new-slug", "Bad_Name", "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "failed" and "page name" in got["note"]
        stray = os.path.join(tmp, "data", "GeminiCarbon_clean.csv")
        open(stray, "w").close()                                        # files of that output name in data/
        got = A.add(CID, "new-slug", "gemini-carbon", "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "failed" and "in data/ already" in got["note"]
        os.remove(stray)


def test_caps_and_failures_leave_nothing_behind():
    with repo() as tmp:
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=Fake(fills=6).steps(), max_fills=5)
        assert got["status"] == "failed" and "over 5 fills" in got["note"], got
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=Fake().steps(), max_page_bytes=1_000)
        assert got["status"] == "failed" and "its page is 0 MB, over the 0 MB" in got["note"], got
        assert A.MAX_PAGE_BYTES == 10_000_000
        # its data files, saved with it so its page can be built again, have a cap too
        got = A.add(CID, "event/gemini-carbon-ship#9", "gemini-carbon", "Yes", root=tmp, steps=Fake().steps(),
                    max_data_bytes=10)
        assert got["status"] == "failed" and "its data files are 0 MB, over the 0 MB" in got["note"], got
        assert A.MAX_DATA_BYTES == 40_000_000

        # a step that raises (the trade feed down, a whale's history refused)
        steps = Fake().steps()

        def refused(w, cutoff):
            raise RuntimeError("Polymarket refused")
        steps["history"] = refused
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=steps)
        assert got["status"] == "failed" and got["note"].startswith("RuntimeError: prior history"), got
        assert isinstance(got["error"], RuntimeError) and "Traceback" in got["traceback"]
        # no trades at all
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=Fake(fills=0, whale=False).steps())
        assert got["status"] == "failed" and "no trades" in got["note"]
        # a page the site could not read (NaN) fails here, not on the site
        from pipeline import export
        orig = export.export_one
        export.export_one = lambda entry, **kw: ({"name": entry["name"]}, {"name": entry["name"], "x": float("nan")})
        try:
            got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=Fake().steps())
        finally:
            export.export_one = orig
        assert got["status"] == "failed" and got["note"].startswith("ValueError")


def test_dry_run_changes_nothing():
    with repo() as tmp:
        f = Fake()
        got = A.add(CID, "s", "gemini-carbon", "Yes", dry_run=True, root=tmp, steps=f.steps())
        assert got["status"] == "added" and not got["changed"] and "GeminiCarbon" in got["note"]
        assert set(got) == {"status", "name", "note", "changed"}
        assert [r[0] for r in f.ran] == ["gamma"] and os.listdir(os.path.join(tmp, "data")) == []


def test_the_command_needs_the_api():
    from watchlist import ai
    old = (os.environ.pop("WATCHLIST_API_URL", None), os.environ.pop("WATCHLIST_JOB_TOKEN", None), ai.ENV_FILE)
    ai.ENV_FILE = os.path.join(tempfile.gettempdir(), "no-such-dir-rigged", ".env")
    try:
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert A.main(["--condition-id", CID, "--slug", "s", "--outcome", "Yes"]) == 2
        assert "No API configured" in err.getvalue()
    finally:
        ai.ENV_FILE = old[2]
        for k, v in zip(("WATCHLIST_API_URL", "WATCHLIST_JOB_TOKEN"), old[:2]):
            if v is not None:
                os.environ[k] = v


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
