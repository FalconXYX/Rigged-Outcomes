#!/usr/bin/env python3
"""
Offline tests for the deploy's step that adds the job's saved data to the
site (watchlist.publish), against the fake API (tests/fake_api.py). No
network.

    python3 tests/test_watchlist_publish.py

Covers the added markets merged after the committed ones (oldest added
first) in index.json and written to markets/ the way export.py writes them,
a name or slug export.py built already left out, a document that is not a
market's page left out, the lists written byte for byte (one not saved
leaving the file as it is), an added market's page built again from its
saved data files with this export.py (the saved page when that fails), the
live site's copies standing in (a stand-in for the site, never the network)
for a damaged document or an API that is down, and the exits: no API 0, half
an API, a refusal, no index.json, or the API and the live site both down 1.
"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_api import FakeApi  # noqa: E402
from watchlist import ai, blobs, publish as P, sync  # noqa: E402

COMMITTED = [{"name": "usiran", "slug": "us-strikes-iran/us-strikes-iran-by-june-30#1", "title": "Will the US strike?",
              "totalVolume": 1.5, "preview": {"odds": [[1, 50.0]]}},
             {"name": "oscars", "slug": "oscars-best-actor", "title": "Best actor é", "totalVolume": 0.1}]


def site(tmp):
    """export.py's output: index.json and the committed markets' pages."""
    out = os.path.join(tmp, "data")
    os.makedirs(os.path.join(out, "markets"))
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump({"markets": COMMITTED}, f, separators=(",", ":"), allow_nan=False)
    for m in COMMITTED:
        with open(os.path.join(out, "markets", f"{m['name']}.json"), "w") as f:
            json.dump({"name": m["name"], "timeline": {}}, f, separators=(",", ":"), allow_nan=False)
    return out


def save_market(api, name, slug, added_at, **over):
    entry = {"name": name, "slug": slug, "output": "X", "correct_outcome": "Yes"}
    doc = {"entry": entry, "index": {"name": name, "slug": slug, "title": f"{name} é?", "totalVolume": 2.25},
           "market": {"name": name, "slug": slug, "timeline": {"t": [1, 2], "v": [0.5, None]}}}
    doc.update(over)
    blobs.put_json(api, f"market:{name}", doc, {"entry": entry, "condition_id": "0x1", "added_at": added_at,
                                                "by": "github"}, expect=None)
    return doc


def read(path):
    with open(path, "rb") as f:
        return f.read()


class FakeSite:
    """A stand-in for the live site's data files: {path under data/: bytes}; down=True: it does not answer."""

    def __init__(self, files=None, down=False):
        self.files, self.down, self.asked = dict(files or {}), down, []

    def fetch(self, url):
        rel = url.split("/data/", 1)[1]
        self.asked.append(rel)
        if self.down:
            raise P.LiveSiteDown(f"the live site did not answer for {url} (ConnectionError)")
        return self.files.get(rel)

    def live(self):
        return P.LiveSite("https://site.test/x/", fetch=self.fetch)


def live_with(lists=True, markets=()):
    """A FakeSite holding the committed markets, the given added ones and, with lists, both lists."""
    index = list(COMMITTED) + [{"name": n, "slug": f"{n}-slug", "title": "live"} for n in markets]
    files = {"index.json": json.dumps({"markets": index}).encode()}
    for m in index:
        files[f"markets/{m['name']}.json"] = json.dumps({"name": m["name"], "from": "live"}).encode()
    if lists:
        files.update({"closed.json": b'{"live": "closed"}', "open.json": b'{"live": "open"}'})
    return FakeSite(files)


class env:
    """No API settings for the block (none in the environment, no .env file), or the given ones."""

    def __init__(self, tmp, **kv):
        self.tmp, self.kv = tmp, kv

    def __enter__(self):
        self.old = {k: os.environ.pop(k, None) for k in ("WATCHLIST_API_URL", "WATCHLIST_JOB_TOKEN")}
        self.old_file, ai.ENV_FILE = ai.ENV_FILE, os.path.join(self.tmp, "no.env")
        os.environ.update(self.kv)

    def __exit__(self, *exc):
        ai.ENV_FILE = self.old_file
        for k, v in self.old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


class api_is:
    """sync.client() answers the given client for the block."""

    def __init__(self, api):
        self.api = api

    def __enter__(self):
        self.real, sync.client = sync.client, lambda *a, **k: self.api

    def __exit__(self, *exc):
        sync.client = self.real


def test_added_markets_after_the_committed_ones():
    api = FakeApi()
    newer = save_market(api, "a-newer", "a-newer-slug", 200)
    older = save_market(api, "z-older", "z-older-slug", 100)
    save_market(api, "usiran", "another-slug", 50)                              # a committed name
    save_market(api, "iran-again", "us-strikes-iran-by-june-30", 60)            # a committed (bare) slug
    save_market(api, "broken", "b", 70, market={"name": "other"})               # not this market's page
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out:
        data = site(tmp)
        before = read(os.path.join(data, "markets", "usiran.json"))
        got = P.publish(api, data)
        assert got == {"lists": [], "markets": ["z-older", "a-newer"], "skipped": ["usiran", "iran-again", "broken"]}
        index = json.loads(read(os.path.join(data, "index.json")))["markets"]
        assert index == COMMITTED + [older["index"], newer["index"]]
        # the form export.py writes: compact, ASCII, no NaN
        assert read(os.path.join(data, "index.json")) == json.dumps({"markets": index}, separators=(",", ":"),
                                                                     allow_nan=False).encode()
        assert read(os.path.join(data, "markets", "z-older.json")) == \
            json.dumps(older["market"], separators=(",", ":"), allow_nan=False).encode()
        assert read(os.path.join(data, "markets", "usiran.json")) == before                 # committed ones win
        assert sorted(os.listdir(os.path.join(data, "markets"))) == ["a-newer.json", "oscars.json", "usiran.json",
                                                                      "z-older.json"]
        assert not os.path.exists(os.path.join(data, "closed.json"))
    assert "No closed list saved yet" in out.getvalue() and "the committed one is kept" in out.getvalue()


def test_no_added_market_leaves_index_json_as_it_was():
    api = FakeApi()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        data = site(tmp)
        before = read(os.path.join(data, "index.json"))
        assert P.publish(api, data)["markets"] == []
        assert read(os.path.join(data, "index.json")) == before


def test_lists_byte_for_byte():
    api = FakeApi()
    closed = b'{"generated_at": "2026-11-16T04:20:00+00:00",\n "days": [{"x": "\xc3\xa9"}]}\n'
    blobs.put(api, "list:closed", closed, {"by": "github"})
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out:
        data = site(tmp)
        with open(os.path.join(data, "open.json"), "w") as f:
            f.write("placeholder")
        got = P.publish(api, data)
        assert got["lists"] == ["closed"] and read(os.path.join(data, "closed.json")) == closed
        assert read(os.path.join(data, "open.json")) == b"placeholder"
    assert "No open list saved yet" in out.getvalue()


def test_exits():
    with tempfile.TemporaryDirectory() as tmp, env(tmp):
        data = site(tmp)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert P.main(["--out", data], live=FakeSite().live()) == 0
        assert out.getvalue().startswith("::notice::No API configured")
        # half an API: one of the two settings missing fails the deploy rather than dropping the lists
        for k in ("WATCHLIST_API_URL", "WATCHLIST_JOB_TOKEN"):
            with env(tmp, **{k: "x"}), contextlib.redirect_stdout(io.StringIO()) as out:
                assert P.main(["--out", data], live=FakeSite().live()) == 1
            assert out.getvalue().startswith(f"::error::Only {k} is set"), out.getvalue()
        # a refusal (a wrong token) or no index.json: exit 1, nothing written. A down API with a down live site
        # (or a damaged list the live site cannot replace) is an outage: exit 0, nothing written, and skip=true
        # leaves the deploy out, so Pages keeps the last site
        before = read(os.path.join(data, "index.json"))
        for case in ("refused", "no index", "both down", "damaged, live down"):
            api = FakeApi()
            save_market(api, "m-1", "s-1", 1)
            site_ = live_with(markets=["m-1"])
            if case == "refused":
                api.fail_next(401, "unauthorized", method="GET")
            elif case == "both down":
                api.fail_next(503, "server", method="GET", path="prefix=market:")
                site_.down = True
            elif case == "damaged, live down":
                blobs.put(api, "list:closed", b"saved closed", {})
                api.blobs["list:closed"]["sha256"] = "0" * 64
                site_.down = True
            gh = os.path.join(tmp, f"out-{case}")
            with api_is(api), contextlib.redirect_stdout(io.StringIO()) as out:
                os.environ["GITHUB_OUTPUT"] = gh
                try:
                    code = P.main(["--out", os.path.join(tmp, "none") if case == "no index" else data],
                                  live=site_.live())
                finally:
                    del os.environ["GITHUB_OUTPUT"]
            if case in ("refused", "no index"):
                assert code == 1 and "::error::" in out.getvalue(), out.getvalue()
                assert not os.path.exists(gh)
            else:
                assert code == 0 and "::warning::" in out.getvalue() and "::error::" not in out.getvalue()
                assert read(gh) == b"skip=true\n" and "Pages keeps the last site" in out.getvalue()
            assert not os.path.exists(os.path.join(data, "markets", "m-1.json"))
            assert read(os.path.join(data, "index.json")) == before and not os.path.exists(os.path.join(data, "closed.json"))
        api = FakeApi()
        save_market(api, "m-1", "s-1", 1)
        site_ = FakeSite()
        with api_is(api), contextlib.redirect_stdout(io.StringIO()):
            assert P.main(["--out", data], live=site_.live()) == 0
        assert os.path.exists(os.path.join(data, "markets", "m-1.json")) and site_.asked == []


def test_api_down_uses_the_live_sites_copies():
    with tempfile.TemporaryDirectory() as tmp, env(tmp):
        data = site(tmp)
        api = FakeApi()
        save_market(api, "m-1", "s-1", 1)
        api.fail_next(503, "server", method="GET", path="prefix=market:")      # after the client's retries
        site_ = live_with(markets=["m-1", "m-2"])
        with api_is(api), contextlib.redirect_stdout(io.StringIO()) as out:
            assert P.main(["--out", data], live=site_.live()) == 0
        assert "::warning::The API did not answer" in out.getvalue()
        assert read(os.path.join(data, "closed.json")) == b'{"live": "closed"}'
        assert read(os.path.join(data, "open.json")) == b'{"live": "open"}'
        index = json.loads(read(os.path.join(data, "index.json")))["markets"]
        assert [m["name"] for m in index] == ["usiran", "oscars", "m-1", "m-2"] and index[:2] == COMMITTED
        assert read(os.path.join(data, "markets", "m-2.json")) == site_.files["markets/m-2.json"]
        assert json.loads(read(os.path.join(data, "markets", "usiran.json"))) == {"name": "usiran", "timeline": {}}
        # a network error is an outage too; a live site with no lists leaves the files as they are
        os.remove(os.path.join(data, "closed.json"))
        api.fail_next(None, "network", method="GET", path="list:closed")
        bare = live_with(lists=False, markets=["m-1"])
        with open(os.path.join(data, "index.json"), "w") as f:
            json.dump({"markets": COMMITTED}, f)
        with api_is(api), contextlib.redirect_stdout(io.StringIO()):
            assert P.main(["--out", data], live=bare.live()) == 0
        assert not os.path.exists(os.path.join(data, "closed.json"))
        assert [m["name"] for m in json.loads(read(os.path.join(data, "index.json")))["markets"]][-1] == "m-1"


def test_a_damaged_document_uses_the_live_copy():
    api = FakeApi()
    save_market(api, "m-1", "s-1", 1)
    save_market(api, "m-2", "s-2", 2)
    save_market(api, "m-3", "s-3", 3)
    blobs.put(api, "list:closed", b"saved closed", {})
    api.blobs["market:m-1"]["sha256"] = "0" * 64                                  # damaged, the live site has it
    api.blobs["market:m-3"]["sha256"] = "0" * 64                                  # damaged, the live site lacks it
    api.blobs["list:closed"]["sha256"] = "0" * 64
    site_ = live_with(markets=["m-1"])
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out:
        data = site(tmp)
        got = P.publish(api, data, live=site_.live())
        assert got["markets"] == ["m-1", "m-2"] and got["skipped"] == ["m-3"] and got["lists"] == ["closed"]
        assert read(os.path.join(data, "markets", "m-1.json")) == site_.files["markets/m-1.json"]
        assert read(os.path.join(data, "closed.json")) == b'{"live": "closed"}'
        assert not os.path.exists(os.path.join(data, "markets", "m-3.json"))
        # with no live site to ask, a damaged market is left out and a damaged list leaves its file
        os.remove(os.path.join(data, "closed.json"))
        with open(os.path.join(data, "index.json"), "w") as f:
            json.dump({"markets": COMMITTED}, f)
        assert P.publish(api, data)["markets"] == ["m-2"] and not os.path.exists(os.path.join(data, "closed.json"))
    assert "the live site's copy is used" in out.getvalue() and "no copy of it: left out" in out.getvalue()


def test_an_added_market_is_built_again_with_this_export():
    """Its page follows today's export.py: built from its saved data files, the saved page only standing in."""
    import export
    entry = {"name": "kristi-again", "slug": "kristi-again-slug", "output": "KristiNoemFiredMarch31",
             "correct_outcome": "Yes"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = {}
    for suffix in ("_clean.csv", "_insiders.csv"):
        with open(os.path.join(root, "data", f"{entry['output']}{suffix}"), encoding="utf-8") as f:
            files[f"{entry['output']}{suffix}"] = f.read()
    with contextlib.redirect_stdout(io.StringIO()):
        want_index, want_page = export.export_one(entry, data_dir=os.path.join(root, "data"))
    api = FakeApi()
    save_market(api, "kristi-again", "kristi-again-slug", 1, entry=entry, data=files)   # an old format's page saved
    bad = dict(files, **{"KristiNoemFiredMarch31_clean.csv": "not,a\nclean,csv\n"})
    save_market(api, "kristi-bad", "kristi-bad-slug", 2, entry=dict(entry, name="kristi-bad"), data=bad)
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out:
        data = site(tmp)
        got = P.publish(api, data)
        assert got["markets"] == ["kristi-again", "kristi-bad"]
        assert read(os.path.join(data, "markets", "kristi-again.json")) == \
            json.dumps(want_page, separators=(",", ":"), allow_nan=False).encode()
        index = json.loads(read(os.path.join(data, "index.json")))["markets"]
        assert index[2] == json.loads(json.dumps(want_index)) and index[3]["title"] == "kristi-bad é?"
        assert json.loads(read(os.path.join(data, "markets", "kristi-bad.json")))["timeline"] == {"t": [1, 2],
                                                                                                "v": [0.5, None]}
    assert "market:kristi-bad could not be built again" in out.getvalue()


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
