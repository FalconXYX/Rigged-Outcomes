#!/usr/bin/env python3
"""
Adding a market, in memory and in steps, against a scripted Polymarket (a small fixture market: 3 trade pages,
ties, the operator's rows, complementary SELLs, near-certain sweeps, prior histories over several pages). No
network; about half a minute.

    python3 tests/test_add_pipeline.py

Covers:
  - parity: the page, the index entry and the data files of add_market.add (Fills, cleandata.clean_rows,
    scrape_insiders.score_frame, export.export_one on frames) byte for byte the same as the file-based pipeline
    as it was at commit 3b883c3 (add_market.add over polymarket_trades, cleandata, scrape_insiders and export,
    with CSVs in a scratch folder), run in its own process from git; its whales scored in the clean file's
    order (the old pool's order was whichever history came back first);
  - the add steps against the fake Worker (add.fetch, the add.whales batches, add.export) putting the same page
    as site:markets/<name>.json, the index entry with its version, the record, the catalog and the report;
  - resuming: add.fetch stopping after every page and add.whales stopping mid-batch give the same page, and no
    whale whose result was saved is fetched again;
  - the day's whale allowance used up waits for 04:00 in Toronto; a market that cannot be added is reported
    failed and stops the run; a second add of the same market is "already on the main page";
  - prior histories added up page by page: the same totals as the rows, under 100 MB for ten wallets at once.
  - the 7 original markets (backend.site originals) saved from their CSVs at 3b883c3 the way added markets are,
    first in the index, each page the one export.py writes; a second run changes nothing.
"""
import contextlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_backend_fakeworker as F  # noqa: E402

import add_market as A  # noqa: E402
import polymarket_api as api  # noqa: E402
from backend import add_steps, contract, outcomes  # noqa: E402

OLD_COMMIT = "3b883c3"
OLD_FILES = ("add_market.py", "polymarket_api.py", "polymarket_trades.py", "cleandata.py", "scrape_insiders.py",
             "export.py")
CID = "0x" + "5e" * 32
SLUG = "will-the-fixture-market-resolve-yes"
OPERATOR = next(iter(api.OPERATORS))


# ── the scripted Polymarket ──────────────────────────────────────────────────

def wallet(i):
    return "0x" + f"{i:04x}" * 10


def fixture(seed=11):
    """{"market", "trades": [pages, newest first], "histories": {wallet: [pages]}}."""
    rng = random.Random(seed)
    base = 1_790_000_000
    rows = []

    def fill(w, side, outcome, size, price, ts):
        rows.append({"proxy_wallet": w, "side": side, "outcome": outcome, "outcome_index": 0 if outcome == "Yes" else 1,
                     "size": size, "price": price, "timestamp": ts, "condition_id": CID, "title": "Fixture",
                     "transaction_hash": "0x" + "%064x" % rng.getrandbits(256)})

    for i in range(1, 41):                                        # the crowd
        for _ in range(rng.randint(3, 12)):
            fill(wallet(i), rng.choice(["BUY", "BUY", "SELL"]), rng.choice(["Yes", "No"]),
                 round(rng.uniform(1, 400), 6), round(rng.uniform(0.02, 0.97), 6), base + rng.randint(0, 9 * 86400))
    for i in range(41, 49):                                       # whales on Yes, a few fills each
        t0 = base + rng.randint(0, 7 * 86400)
        for k in range(rng.randint(2, 5)):
            fill(wallet(i), "BUY", "Yes", round(rng.uniform(2_000, 9_000), 6), round(rng.uniform(0.2, 0.8), 6),
                 t0 + k * rng.randint(1, 3000))
        fill(wallet(i), "SELL", "No", round(rng.uniform(10, 300), 6), round(rng.uniform(0.2, 0.8), 6), t0 + 5)
    fill(wallet(49), "BUY", "Yes", 9000.0, 0.995, base + 8 * 86400)          # a resolution sweep
    fill(wallet(50), "BUY", "Yes", 3000.123457, 0.333333, base + 100)        # a lone early whale (the IQR filter)
    for t in (base + 4000, base + 4000, base + 4000):                        # ties in one second
        fill(wallet(51), "BUY", "Yes", 1234.5678, 0.41, t)
    fill(OPERATOR, "BUY", "Yes", 99999.0, 0.5, base + 50)                    # the CLOB operator: never counted
    fill("", "BUY", "Yes", 5.0, 0.5, base + 51)
    order = sorted(range(len(rows)), key=lambda i: -rows[i]["timestamp"])     # newest first, ties as made
    feed = [rows[i] for i in order]
    pages = [feed[i:i + 150] for i in range(0, len(feed), 150)]

    histories = {}
    for i in list(range(41, 52)):
        w, hist = wallet(i), []
        cutoff = min(r["timestamp"] for r in rows if r["proxy_wallet"] == w)
        n = {41: 0, 42: 2600}.get(i, rng.randint(5, 900))
        for k in range(n):
            ts = cutoff + 10 - k * 37
            h = {"proxy_wallet": w, "type": "TRADE" if k % 17 else "SPLIT", "side": "BUY", "outcome": "Yes",
                 "outcome_index": 0, "size": round(rng.uniform(1, 500), 6), "price": round(rng.uniform(0.01, 0.99), 6),
                 "usdc_size": round(rng.uniform(0.5, 400), 6), "timestamp": ts, "condition_id": "0x" + "11" * 32,
                 "transaction_hash": "0x" + "%064x" % (k // 3)}
            hist.append(h)
        histories[w] = [hist[j:j + 1000] for j in range(0, len(hist), 1000)] or [[]]
    market = {"conditionId": CID, "question": "Will the fixture market resolve Yes?", "closed": True,
              "umaResolutionStatus": "resolved", "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]',
              "slug": SLUG}
    return {"market": market, "trades": pages, "histories": histories}


SERVE = '''
def serve(fx, url, params):
    params = dict(params or {})
    def env(pages, i, tag):
        more = i < len(pages) - 1
        return {"data": pages[i], "pagination": {"limit": 1000, "offset": 0, "has_more": more,
                                                 "next_cursor": f"{tag}{i + 1}" if more else None}}
    def at(tag):
        c = params.get("cursor")
        return 0 if not c else int(str(c)[len(tag):])
    if "/markets/slug/" in url:
        return dict(fx["market"])
    if url.endswith("/v2/trades"):
        return env(fx["trades"], at("t"), "t")
    if url.endswith("/v2/activity"):
        pages = fx["histories"].get(params["user"], [[]])
        tag = params["user"][-6:] + "-"
        return env(pages, at(tag), tag)
    raise AssertionError(url)
'''
exec(SERVE)


@contextlib.contextmanager
def polymarket(fx, counts=None, fail=None):
    """polymarket_api.get_json answered from the fixture; counts: {(kind, user): n} of requests; fail(url, params)
    may raise first."""
    lock = threading.Lock()

    def get_json(url, params=None, timeout=30, retry=api.RETRY):
        if fail is not None:
            fail(url, params or {})
        if counts is not None and url.endswith("/v2/activity"):
            with lock:
                counts[params["user"]] = counts.get(params["user"], 0) + 1
        return serve(fx, url, params)                                # noqa: F821 (defined by exec)

    orig = api.get_json
    api.get_json = get_json
    try:
        yield
    finally:
        api.get_json = orig


def quiet():
    return contextlib.redirect_stdout(open(os.devnull, "w"))


class Root:
    """A temporary root holding today's markets.json and an empty data/."""

    def __enter__(self):
        self.path = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "markets.json"), os.path.join(self.path, "markets.json"))
        os.makedirs(os.path.join(self.path, "data"))
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


# ── the old pipeline, from git ───────────────────────────────────────────────

OLD_DRIVER = SERVE + '''
import json, sys
old, root, fx_path, out_path = sys.argv[1:5]
sys.path.insert(0, old)
fx = json.load(open(fx_path))
import polymarket_api as api
api.get_json = lambda url, params=None, timeout=30, retry=5: serve(fx, url, params)
import scrape_insiders
scrape_insiders.as_completed = lambda futures: list(futures)         # whales in submission (clean file) order
import add_market
got = add_market.add(fx["market"]["conditionId"], fx["market"]["slug"], None, "Yes", root=root)
assert got["status"] == "added" and got["changed"], got
page = json.dumps(got["market"], separators=(",", ":"), allow_nan=False)
json.dump({"page": page, "index": got["index"], "entry": got["entry"], "data": got["data"]}, open(out_path, "w"))
'''


def old_pipeline(fx):
    """What the pipeline at OLD_COMMIT builds for the fixture: {"page" (text), "index", "entry", "data"}; None when
    git cannot give its files."""
    tmp = tempfile.mkdtemp()
    try:
        old = os.path.join(tmp, "old")
        os.makedirs(old)
        for name in OLD_FILES:
            got = subprocess.run(["git", "-C", ROOT, "show", f"{OLD_COMMIT}:{name}"], capture_output=True)
            if got.returncode:
                return None
            with open(os.path.join(old, name), "wb") as f:
                f.write(got.stdout)
        with open(os.path.join(tmp, "fx.json"), "w") as f:
            json.dump(fx, f)
        with open(os.path.join(tmp, "driver.py"), "w") as f:
            f.write(OLD_DRIVER)
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "data"))
        shutil.copy(os.path.join(ROOT, "markets.json"), os.path.join(root, "markets.json"))
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([old, ROOT]))
        run = subprocess.run([sys.executable, os.path.join(tmp, "driver.py"), old, root, os.path.join(tmp, "fx.json"),
                              os.path.join(tmp, "out.json")], capture_output=True, text=True, env=env, timeout=300)
        assert run.returncode == 0, run.stderr[-3000:]
        with open(os.path.join(tmp, "out.json")) as f:
            return json.load(f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_OLD = {}


def old_result():
    if "got" not in _OLD:
        _OLD["got"] = old_pipeline(fixture())
    return _OLD["got"]


def new_result(fx):
    with Root() as root, polymarket(fx), quiet():
        got = A.add(CID, SLUG, None, "Yes", root=root)
    assert got["status"] == "added" and got["changed"], got
    return got


# ── the tests ────────────────────────────────────────────────────────────────

def test_parity_with_the_file_pipeline():
    old = old_result()
    if old is None:
        print("SKIP test_parity_with_the_file_pipeline: git has no commit", OLD_COMMIT)
        return
    new = new_result(fixture())
    assert json.dumps(new["market"], separators=(",", ":"), allow_nan=False) == old["page"]
    assert new["index"] == old["index"] and new["entry"] == old["entry"] and new["data"] == old["data"]
    page = json.loads(old["page"])
    assert page["traders"]["totalWhales"] >= 8 and len(page["timeline"]["whales"]) >= 4      # the fixture is a real one
    assert all(w["id"] != OPERATOR for w in page["timeline"]["whales"])


def steps_run(fx, worker=None, counts=None, fail=None, name=None, slot=1):
    """An add run through its three steps against the fake Worker; returns the worker and the run's id."""
    w = worker or F.FakeWorker()
    rid = w.add_run("add", f"{CID}:{slot}", input={"condition_id": CID, "slug": SLUG, "winner": "Yes", "name": name})
    with polymarket(fx, counts, fail), quiet():
        for _ in range(80):
            F.serve(w, "add")
            if w.runs[rid]["state"] not in ("queued", "running"):
                break
            for j in w.jobs.values():                          # back-offs and laters, at once
                if j["state"] == "queued":
                    j["run_after"] = 0
    return w, rid


def page_of(w, name):
    from watchlist import blobs
    h, data = blobs.get(w.blobs, f"site:markets/{name}.json")
    return h, data


def test_the_steps_put_the_same_page():
    fx = fixture()
    new = new_result(fx)
    w, rid = steps_run(fx)
    assert w.runs[rid]["state"] == "done", [(j["id"], j["state"], j["error"]) for j in w.jobs.values()]
    name = new["name"]
    h, data = page_of(w, name)
    assert data.decode() == json.dumps(new["market"], separators=(",", ":"), allow_nan=False)
    from backend import site
    from watchlist import blobs
    _, index = site.read_index(w.blobs)
    assert index == [dict(new["index"], v=h["version"])]
    rh, record = blobs.get_json(w.blobs, f"market:{name}")
    assert record == {"entry": new["entry"], "index": new["index"], "data": new["data"]}
    assert rh["meta"]["condition_id"] == CID and rh["meta"]["v"] == h["version"]
    assert w.done[-1]["requests"] == [{"condition_id": CID, "status": "added", "name": name, "note": None}]
    assert {"name": name, "slug": SLUG} in w.catalog[-1]["markets"]
    batches = [j for j in w.jobs.values() if j["kind"] == "add.whales"]
    assert len(batches) == 1 and len(batches[0]["input"]["whales"]) == 10           # the sweep is no whale
    fetch = next(c for c in w.completes if c["job_id"].endswith("|add.fetch|0"))
    assert fetch["usage"]["units"] == 3 and fetch["run_meta"]["name"] == name
    # the same market again: already on the main page, nothing fetched
    w2, rid2 = steps_run(fx, worker=w, slot=2)
    assert w2.done[-1]["requests"][0]["status"] == "added" and w2.done[-1]["requests"][0]["note"] == \
        "already on the main page"
    assert w2.runs[rid2]["state"] == "partial" and w.job("add.export", run=rid2)["state"] == "cancelled"


def test_resuming_gives_the_same_page():
    fx = fixture()
    want = json.dumps(new_result(fx)["market"], separators=(",", ":"), allow_nan=False)
    counts, flaky = {}, {"n": 0}
    lock = threading.Lock()

    def fail(url, params):                                     # the deadline in the middle of three whales
        if url.endswith("/v2/activity") and not params.get("cursor"):
            with lock:
                flaky["n"] += 1
                if flaky["n"] in (2, 5, 9):
                    raise outcomes.OutOfTime("the deadline")

    saved = add_steps.SAVE_S
    add_steps.SAVE_S = 10 ** 6                                 # out of time after every trade page
    try:
        w, rid = steps_run(fx, counts=counts, fail=fail)
    finally:
        add_steps.SAVE_S = saved
    assert w.runs[rid]["state"] == "done"
    fetches = [c for c in w.completes if "|add.fetch|" in c["job_id"]]
    assert [c["outcome"] for c in fetches] == ["partial", "partial", "done"], [c["outcome"] for c in fetches]
    assert fetches[0]["cursor"]["fills"]["blob"].startswith("scratch:")
    whales = [c for c in w.completes if "|add.whales|" in c["job_id"]]
    assert [c["outcome"] for c in whales] == ["partial", "done"]
    _, data = page_of(w, json.loads(want)["name"])
    assert data.decode() == want
    pages = {u: len(p) for u, p in fx["histories"].items()}
    assert len(counts) == 10 and flaky["n"] == 13                    # 10 whales, 3 of them cut and asked again
    assert all(counts[u] == pages[u] for u in counts), counts       # (a cut whale's request is not counted)


def test_the_whale_allowance_waits_for_four_in_the_morning():
    fx = fixture()
    w = F.FakeWorker()
    w.budget["add_whales_left"] = 0
    rid = w.add_run("add", f"{CID}:1", input={"condition_id": CID, "slug": SLUG, "winner": "Yes"})
    with polymarket(fx), quiet():
        F.serve(w, "add")
    later = [c for c in w.completes if c["outcome"] == "later"]
    assert later and later[0]["reason"] == "budget"
    when = contract.toronto(later[0]["run_after"])
    assert when.strftime("%H:%M") == "04:00" and later[0]["run_after"] > w.now()
    assert w.runs[rid]["state"] == "running"
    w.budget["add_whales_left"] = 5                            # five today: the rest tomorrow
    w.job("add.whales")["run_after"] = 0
    with polymarket(fx), quiet():
        F.serve(w, "add")
    assert w.completes[-1]["outcome"] == "later" and len(w.job("add.whales")["cursor"]["done"]) == 5


def test_the_deadline_inside_a_trade_page_keeps_the_pages_fetched():
    fx = fixture()
    want = json.dumps(new_result(fx)["market"], separators=(",", ":"), allow_nan=False)
    cut = {"n": 0}

    def fail(url, params):                                     # the deadline while the second trade page loads
        if url.endswith("/v2/trades") and params.get("cursor") == "t1" and not cut["n"]:
            cut["n"] += 1
            raise outcomes.OutOfTime("the deadline")

    w, rid = steps_run(fx, fail=fail)
    assert w.runs[rid]["state"] == "done"
    fetches = [c for c in w.completes if "|add.fetch|" in c["job_id"]]
    assert [c["outcome"] for c in fetches] == ["partial", "done"] and fetches[0]["cursor"]["pages"] == 1
    assert fetches[0]["cursor"]["fills"]["blob"].startswith("scratch:")      # page 1 is not fetched again
    assert page_of(w, json.loads(want)["name"])[1].decode() == want


def test_a_large_add_leaves_whales_to_the_small_ones():
    fx = fixture()
    w = F.FakeWorker()
    w.budget["add_whales_left"] = 8
    saved = add_steps.LISTS["add_whales_reserve"]
    add_steps.LISTS["add_whales_reserve"] = 5                  # the fixture's 10 whales count as a large add
    try:
        rid = w.add_run("add", f"{CID}:1", input={"condition_id": CID, "slug": SLUG, "winner": "Yes"})
        with polymarket(fx), quiet():
            F.serve(w, "add")
    finally:
        add_steps.LISTS["add_whales_reserve"] = saved
    assert w.runs[rid]["meta"]["whales"] == 10
    assert w.completes[-1]["outcome"] == "later" and w.completes[-1]["reason"] == "budget"
    assert len(w.job("add.whales")["cursor"]["done"]) == 3     # 8 left today, 5 of them kept for small adds


def test_a_market_that_cannot_be_added_is_reported():
    fx = fixture()
    fx["market"]["outcomePrices"] = '["0", "1"]'
    w, rid = steps_run(fx)
    assert w.done[-1]["requests"] == [{"condition_id": CID, "status": "failed", "name": None,
                                       "note": "it resolved No, not Yes"}]
    assert w.runs[rid]["state"] == "partial" and w.job("add.export")["state"] == "cancelled"
    # the request read from the Worker when the run's input lacks it; none approved: nothing to do
    w = F.FakeWorker()
    w.requests = [{"condition_id": CID, "slug": SLUG, "winner": "Yes", "name": "fixture", "status": "approved"}]
    rid = w.add_run("add", f"{CID}:7")
    with polymarket(fixture()), quiet():
        F.serve(w, "add")
    assert w.runs[rid]["state"] == "done" and w.done[-1]["requests"][0]["name"] == "fixture"
    w = F.FakeWorker()
    rid = w.add_run("add", f"{CID}:8")
    with polymarket(fixture()), quiet():
        F.serve(w, "add")
    assert not w.done and w.job("add.export")["state"] == "cancelled"


def test_a_failed_batch_fails_the_add():
    fx = fixture()

    def fail(url, params):
        if url.endswith("/v2/activity"):
            raise api.PolymarketAPIError("HTTP 400 from x", status=400)

    w, rid = steps_run(fx, fail=fail)
    assert w.job("add.whales")["state"] == "failed"
    assert w.done[-1]["requests"][0]["status"] == "failed" and w.done[-1]["requests"][0]["note"] == add_steps.STOPPED


def test_history_totals_match_the_rows_and_stay_small():
    fx = fixture()
    w = wallet(42)
    cutoff = min(r["timestamp"] for p in fx["trades"] for r in p if r["proxy_wallet"] == w)
    with polymarket(fx), quiet():
        rows = api.fetch_user_history_up_to(w, cutoff)
        tot = api.history_totals(w, cutoff)
    stake = 0.0
    for h in rows:
        stake += float(h.get("usdcSize", 0))
    assert tot["stake"] == stake and tot["txs"] == len({h["transactionHash"] for h in rows})
    assert tot["trades"] == len(rows) and tot["capped"] is False
    # ten busy wallets at once, 20 pages of 1000 each: a few MB, never the rows
    big = {"market": fx["market"], "trades": [[]], "histories": {}}
    for i in range(10):
        u = wallet(100 + i)
        big["histories"][u] = [[{"proxy_wallet": u, "type": "TRADE", "size": 1.5, "price": 0.5, "usdc_size": 0.75,
                                 "timestamp": 1_000 + k, "transaction_hash": "0x%064x" % (p * 1000 + k),
                                 "side": "BUY", "outcome": "Yes", "outcome_index": 0, "condition_id": CID}
                                for k in range(1000)] for p in range(20)]
    from concurrent.futures import ThreadPoolExecutor
    with polymarket(big), quiet():
        tracemalloc.start()
        with ThreadPoolExecutor(10) as pool:
            got = list(pool.map(lambda u: api.history_totals(u, 10 ** 9), big["histories"]))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert all(g["trades"] == 20_000 and g["txs"] == 20_000 for g in got)
    assert peak < 100 * 2 ** 20, f"{peak / 2 ** 20:.0f} MB"


def test_the_site_blobs():
    """backend.site: a list put byte for byte; a market added before the backend (its page in market:<name>)
    moved to site:markets/<name>.json and the index; a name taken; a market taken off."""
    from backend import site
    from watchlist import blobs
    w = F.FakeWorker()
    c = w.blobs
    site.put_list(c, "closed.json", b'{"days":[]}')
    assert blobs.get(c, "site:closed.json")[1] == b'{"days":[]}'
    new = new_result(fixture())
    name = new["name"]
    blobs.put_json(c, f"market:{name}", {"entry": new["entry"], "index": new["index"], "market": new["market"],
                                         "data": new["data"]},
                   {"entry": new["entry"], "condition_id": CID, "added_at": 5, "by": "github"}, expect=None)
    with quiet():
        assert site.migrate(c, now=1_795_078_800) == [name]
    h, page = blobs.get(c, f"site:markets/{name}.json")
    assert page == site.page_bytes(new["market"])
    ih, index = blobs.get_json(c, site.INDEX)
    assert index == {"markets": [dict(new["index"], v=h["version"])], "updated_at": "2026-11-19T04:00:00-05:00"}
    rh, record = blobs.get_json(c, f"market:{name}")
    assert "market" not in record and rh["meta"]["v"] == h["version"] and rh["meta"]["added_at"] == 5
    try:
        site.put_market(c, new["entry"], new["index"], new["market"], new["data"], "0x" + "77" * 32)
        raise AssertionError("no NameTaken")
    except site.NameTaken:
        pass
    got = site.put_market(c, new["entry"], new["index"], new["market"], new["data"], CID, now=9)   # the same: again
    assert site.read_index(c)[1] == [dict(new["index"], v=got["v"])]
    assert site.remove_market(c, name) and site.read_index(c)[1] == []
    assert blobs.head(c, f"market:{name}") is None and blobs.head(c, f"site:markets/{name}.json") is None


def test_the_originals_served_like_added_markets():
    """backend.site originals: markets.json's markets built from their CSVs at OLD_COMMIT (what the
    original-project branch holds), each page the one export.py writes from the files, put first in the index
    before a market added earlier; a second run changes nothing."""
    import export
    from backend import site
    from watchlist import blobs
    if subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{OLD_COMMIT}:markets.json"]).returncode:
        print(f"SKIP test_the_originals_served_like_added_markets: no commit {OLD_COMMIT}")
        return
    w = F.FakeWorker()
    c = w.blobs
    new = new_result(fixture())
    site.put_market(c, new["entry"], new["index"], new["market"], new["data"], CID, now=5)
    with quiet():
        got = site.put_originals(c, lambda path: site.git_text(OLD_COMMIT, path), now=9)
    markets = A.read_markets()
    assert got == [(m["name"], "saved") for m in markets]
    entries = site.read_index(c)[1]
    assert [e["name"] for e in entries] == [m["name"] for m in markets] + [new["name"]]
    fired = next(m for m in markets if m["name"] == "fired")
    tmp = tempfile.mkdtemp()
    try:
        for suffix in ("_clean", "_insiders"):
            with open(os.path.join(tmp, f"{fired['output']}{suffix}.csv"), "w", encoding="utf-8") as f:
                f.write(site.git_text(OLD_COMMIT, f"data/{fired['output']}{suffix}.csv"))
        with quiet():
            index, page = export.export_one(fired, data_dir=tmp)
    finally:
        shutil.rmtree(tmp)
    h, served = blobs.get(c, site.page_blob("fired"))
    assert served == export.page_bytes(page)
    assert next(e for e in entries if e["name"] == "fired") == json.loads(json.dumps(dict(index, v=h["version"])))
    rh = blobs.head(c, site.record_blob("fired"))
    assert rh["meta"]["entry"] == fired and rh["meta"]["added_at"] == 4 and rh["meta"]["v"] == h["version"]
    before = {x["name"]: x["version"] for x in blobs.heads(c, "")}
    with quiet():
        assert site.put_originals(c, lambda path: site.git_text(OLD_COMMIT, path), now=11) == \
            [(m["name"], "unchanged") for m in markets]
    assert {x["name"]: x["version"] for x in blobs.heads(c, "")} == before
    plan = A.plan(fired["slug"], None, "Yes", markets, site.added_entries(c))
    assert plan == (None, {"status": "added", "name": "fired", "note": "already on the main page", "changed": False})


def test_fills_carry_over_between_steps():
    fx = fixture()
    whole = A.Fills(fx["market"])
    parts = A.Fills(fx["market"])
    for i, page in enumerate(fx["trades"]):
        whole.add_page(page)
        parts.add_page(page)
        parts = A.Fills.load(json.loads(json.dumps(parts.dump())))
    assert list(whole.rows()) == list(parts.rows()) and whole.n == parts.n
    st, seen = {}, []
    with polymarket(fx):
        for page in api.v2_pages("trades", A.trade_params(CID), st):
            seen.append(len(page))
            break
        st = json.loads(json.dumps(st))
        for page in api.v2_pages("trades", A.trade_params(CID), st):
            seen.append(len(page))
    assert seen == [len(p) for p in fx["trades"]] and st["done"] and st["pages"] == len(fx["trades"])


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
