#!/usr/bin/env python3
"""
Offline tests for add_market.py (an approved "Add to main page" request):
fake Polymarket and pipeline steps in a temporary copy of the repo's
markets.json. No network.

    python3 tests/test_add_market.py

Covers the page name and its PascalCase output, Polymarket's market having
to match (condition id, closed, winner), a slug already on the main page
reported as added with no work, a name or output already taken, the size
caps (markets.json put back byte for byte, the new CSVs removed), a dry run
changing nothing, and markets.json written exactly the way it is today.
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import add_market as A  # noqa: E402

CID = "0x" + "ab" * 32
MARKET = {"conditionId": CID, "closed": True, "umaResolutionStatus": "resolved", "question": "Will Gemini Carbon ship?",
          "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'}


class Fake:
    """The pipeline's steps, writing small files; records what ran."""

    def __init__(self, market=MARKET, fills=3, clean_bytes=10):
        self.market, self.fills, self.clean_bytes, self.ran = market, fills, clean_bytes, []

    def steps(self):
        def gamma(slug):
            self.ran.append(("gamma", slug))
            return dict(self.market)

        def download(slug, raw, max_rows):
            self.ran.append(("download", slug, max_rows))
            n = min(self.fills, max_rows)
            with open(raw, "w") as f:
                f.write("h\n" + "r\n" * n)
            return n

        def clean(raw, out):
            self.ran.append(("clean", os.path.basename(raw), os.path.basename(out)))
            with open(out, "w") as f:
                f.write("x" * self.clean_bytes)

        def score(entry, clean, out):
            self.ran.append(("score", entry["output"]))
            with open(out, "w") as f:
                f.write("insiders\n")

        def export():
            self.ran.append(("export", os.getcwd()))

        return {"gamma": gamma, "download": download, "clean": clean, "score": score, "export": export}


def repo():
    """A temporary root holding today's markets.json and an empty data/."""
    tmp = tempfile.mkdtemp()
    shutil.copy(os.path.join(ROOT, "markets.json"), os.path.join(tmp, "markets.json"))
    os.makedirs(os.path.join(tmp, "data"))
    return tmp


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


def test_markets_json_is_written_the_way_it_is():
    raw = read(os.path.join(ROOT, "markets.json")).decode("utf-8")
    assert A.render(json.loads(raw)) == raw                   # re-dumping today's file gives it byte for byte
    entry = {"name": "gemini-carbon", "slug": "s", "output": "GeminiCarbon", "correct_outcome": "Yes"}
    assert A.render(json.loads(raw) + [entry], raw).startswith(raw.rstrip()[:-1].rstrip())
    # a file in another layout keeps its text; the entry goes in before the final "]"
    odd = '[\n    {"name": "a", "slug": "b", "output": "A"}\n]\n'
    out = A.render(json.loads(odd) + [entry], odd)
    assert out.startswith('[\n    {"name": "a", "slug": "b", "output": "A"},\n') and json.loads(out)[1] == entry
    assert json.loads(A.render([entry], "[]\n")) == [entry]


def test_adds_a_market():
    tmp = repo()
    try:
        before = read(os.path.join(tmp, "markets.json")).decode()
        f = Fake()
        got = A.add(CID.upper().replace("0X", "0x"), "event/gemini-carbon-ship#9", "gemini-carbon", "yes",
                    root=tmp, steps=f.steps())
        assert got == {"status": "added", "name": "gemini-carbon", "note": None, "changed": True}, got
        markets = json.loads(read(os.path.join(tmp, "markets.json")))
        assert markets[-1] == {"name": "gemini-carbon", "slug": "gemini-carbon-ship", "output": "GeminiCarbon",
                               "correct_outcome": "Yes"}
        assert read(os.path.join(tmp, "markets.json")).decode() == A.render(json.loads(before) + [markets[-1]])
        for s in ("", "_clean", "_insiders"):
            assert os.path.exists(os.path.join(tmp, "data", f"GeminiCarbon{s}.csv"))
        assert [r[0] for r in f.ran] == ["gamma", "download", "clean", "score", "export"]
        assert f.ran[1] == ("download", "gemini-carbon-ship", A.MAX_FILLS + 1)
        assert os.path.realpath(f.ran[-1][1]) == os.path.realpath(tmp)          # the export runs in the repo
        # again: already on the main page, nothing runs
        f2 = Fake()
        got = A.add(CID, "gemini-carbon-ship", "other-name", "Yes", root=tmp, steps=f2.steps())
        assert got["status"] == "added" and got["name"] == "gemini-carbon" and not got["changed"]
        assert [r[0] for r in f2.ran] == ["gamma"]
    finally:
        shutil.rmtree(tmp)


def test_polymarket_must_match():
    tmp = repo()
    try:
        before = read(os.path.join(tmp, "markets.json"))
        cases = [(dict(MARKET, conditionId="0x" + "cd" * 32), "different condition id"),
                 (dict(MARKET, closed=False), "not closed"),
                 (dict(MARKET, outcomePrices='["0.5", "0.5"]'), "one winner"),
                 (dict(MARKET, outcomePrices='["0", "1"]'), "it resolved No, not Yes")]
        for market, words in cases:
            f = Fake(market=market)
            got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=f.steps())
            assert got["status"] == "failed" and words in got["note"], (words, got)
            assert [r[0] for r in f.ran] == ["gamma"] and read(os.path.join(tmp, "markets.json")) == before
        assert os.listdir(os.path.join(tmp, "data")) == []
    finally:
        shutil.rmtree(tmp)


def test_taken_names_and_files():
    tmp = repo()
    try:
        before = read(os.path.join(tmp, "markets.json"))
        got = A.add(CID, "new-slug", "usiran", "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "failed" and "taken" in got["note"]
        got = A.add(CID, "new-slug", "Bad_Name", "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "failed" and "page name" in got["note"]
        open(os.path.join(tmp, "data", "GeminiCarbon_clean.csv"), "w").close()        # a stray file is never overwritten
        got = A.add(CID, "new-slug", "gemini-carbon", "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "failed" and "nothing was overwritten" in got["note"]
        assert os.path.exists(os.path.join(tmp, "data", "GeminiCarbon_clean.csv"))
        # the slug is listed but its data files are missing
        superbowl = next(m for m in json.loads(before) if m["name"] == "superbowl")
        got = A.add(CID, superbowl["slug"], "another", "Yes", root=tmp, steps=Fake().steps())
        assert got["status"] == "failed" and "without its data files" in got["note"]
        assert read(os.path.join(tmp, "markets.json")) == before
    finally:
        shutil.rmtree(tmp)


def test_size_caps_put_everything_back():
    tmp = repo()
    try:
        before = read(os.path.join(tmp, "markets.json"))
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=Fake(fills=6).steps(), max_fills=5)
        assert got["status"] == "failed" and "over 5 fills" in got["note"], got
        assert read(os.path.join(tmp, "markets.json")) == before and os.listdir(os.path.join(tmp, "data")) == []
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=Fake(clean_bytes=100).steps(),
                    max_clean_bytes=50)
        assert got["status"] == "failed" and "MB" in got["note"], got
        assert read(os.path.join(tmp, "markets.json")) == before and os.listdir(os.path.join(tmp, "data")) == []

        # a step that raises (the trade feed down, a whale's history refused) puts everything back too
        f = Fake()
        steps = f.steps()

        def refused(entry, clean, out):
            with open(out + ".tmp", "w") as fh:
                fh.write("half")
            raise RuntimeError("prior history of 0xabc could not be fetched")
        steps["score"] = refused
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=steps)
        assert got["status"] == "failed" and got["note"].startswith("RuntimeError: prior history")
        assert read(os.path.join(tmp, "markets.json")) == before and os.listdir(os.path.join(tmp, "data")) == []
        # no trades at all
        got = A.add(CID, "s", "gemini-carbon", "Yes", root=tmp, steps=Fake(fills=0).steps())
        assert got["status"] == "failed" and "no trades" in got["note"]
        assert read(os.path.join(tmp, "markets.json")) == before and os.listdir(os.path.join(tmp, "data")) == []
    finally:
        shutil.rmtree(tmp)


def test_dry_run_changes_nothing():
    tmp = repo()
    try:
        before = read(os.path.join(tmp, "markets.json"))
        f = Fake()
        got = A.add(CID, "s", "gemini-carbon", "Yes", dry_run=True, root=tmp, steps=f.steps())
        assert got["status"] == "added" and not got["changed"] and "GeminiCarbon" in got["note"]
        assert [r[0] for r in f.ran] == ["gamma"] and read(os.path.join(tmp, "markets.json")) == before
        assert os.listdir(os.path.join(tmp, "data")) == []
    finally:
        shutil.rmtree(tmp)


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
