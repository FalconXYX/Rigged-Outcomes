#!/usr/bin/env python3
"""
Offline unit tests for the v2 pipeline: no network, a few seconds.

    python3 -m pytest -q tests/test_offline.py     # or
    python3 tests/test_offline.py                  # same checks, exits 1 on failure

polymarket_api.get_json is replaced by a fake that serves scripted pages, so
these cover the client's guards (what a stalled or malformed feed does), the
whale scorer's address fallback, the empty-market _insiders.csv and
export.py's handling of it, the correct_outcome guard, and that importing
run.py does not run the pipeline. The live-endpoint checks are in
tests/test_v2_parity.py.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import export  # noqa: E402
import polymarket_api as api  # noqa: E402
import polymarket_trades  # noqa: E402
import scrape_insiders  # noqa: E402

WALLET = "0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b"


@contextlib.contextmanager
def fake_feed(pages):
    """Serve `pages` (dicts, or callables taking the request params) from get_json, in order."""
    calls = []
    it = iter(pages)

    def get_json(url, params=None, timeout=30, retry=api.RETRY):
        calls.append(dict(params or {}))
        page = next(it)
        return page(params) if callable(page) else page

    orig = api.get_json
    api.get_json = get_json
    try:
        yield calls
    finally:
        api.get_json = orig


def page(rows, has_more, cursor):
    return {"data": rows, "pagination": {"limit": 1000, "offset": 0, "has_more": has_more, "next_cursor": cursor}}


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


# ── v2_walk ──────────────────────────────────────────────────────────────────

def test_walk_follows_cursor_to_the_end():
    with fake_feed([page([{"a": 1}], True, "c1"), page([{"a": 2}], True, "c2"), page([], False, None)]) as calls:
        rows, capped = api.v2_walk("trades", {"limit": 1000})
    assert rows == [{"a": 1}, {"a": 2}] and capped is False
    assert [c.get("cursor") for c in calls] == [None, "c1", "c2"]


def test_walk_empty_result_is_zero_state():
    with fake_feed([page([], False, None)]):
        assert api.v2_walk("trades", {}) == ([], False)


def test_walk_raises_without_pagination():
    with fake_feed([{"data": [{"a": 1}]}]):
        e = raises(lambda: api.v2_walk("trades", {}), api.PolymarketAPIError)
    assert "no pagination" in str(e)


def test_walk_raises_when_has_more_without_cursor():
    with fake_feed([page([{"a": 1}], True, None)]):
        e = raises(lambda: api.v2_walk("trades", {}), api.PolymarketAPIError)
    assert "no next_cursor" in str(e)


def test_walk_raises_on_repeated_cursor():
    with fake_feed([page([{"a": 1}], True, "same")] * 5):
        e = raises(lambda: api.v2_walk("trades", {}), api.PolymarketAPIError)
    assert "repeated" in str(e)


def test_walk_raises_on_empty_pages_that_claim_more():
    pages = [page([], True, f"c{i}") for i in range(10)]
    with fake_feed(pages) as calls:
        e = raises(lambda: api.v2_walk("trades", {}), api.PolymarketAPIError)
    assert "empty pages" in str(e) and len(calls) == api.MAX_EMPTY_PAGES


def test_walk_row_cap_is_announced_once_and_returns_newest():
    with fake_feed([page([{"a": i} for i in range(3)], True, "c1"), page([{"a": 9}], False, None)]):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows, capped = api.v2_walk("trades", {}, max_rows=2)
    assert capped is True and rows == [{"a": 0}, {"a": 1}]
    assert buf.getvalue().count("capped") == 1
    with fake_feed([page([{"a": 1}], True, "c1"), page([{"a": 2}], False, None)]):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows, capped = api.v2_walk("trades", {}, max_pages=1, announce=False)
    assert capped is True and rows == [{"a": 1}] and buf.getvalue() == ""


def test_walk_safety_net_is_announced():
    orig = api.WALK_MAX_PAGES
    api.WALK_MAX_PAGES = 2
    try:
        with fake_feed([page([{"a": i}], True, f"c{i}") for i in range(5)]) as calls:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rows, capped = api.v2_walk("trades", {})
    finally:
        api.WALK_MAX_PAGES = orig
    assert capped is True and len(rows) == 2 and len(calls) == 2 and "safety net" in buf.getvalue()


# ── fetch_user_history_up_to ─────────────────────────────────────────────────

def test_history_filters_type_client_side_and_cap_prints_once():
    rows = [{"proxy_wallet": WALLET, "timestamp": 1700000000, "type": "SPLIT", "size": 1, "price": 0.5, "transaction_hash": "s"},
            {"proxy_wallet": WALLET, "timestamp": 1699999999, "type": "TRADE", "size": 2, "price": 0.5, "usdc_size": 1.0,
             "transaction_hash": "t"},
            {"proxy_wallet": WALLET, "timestamp": 1700000001, "type": "TRADE", "size": 2, "price": 0.5, "transaction_hash": "late"}]
    with fake_feed([page(rows, True, "c1"), page([], False, None)]) as calls:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hist = api.fetch_user_history_up_to(WALLET, 1700000000, max_pages=1)
    assert [h["type"] for h in hist] == ["TRADE"] and hist[0]["usdcSize"] == 1.0 and hist[0]["transactionHash"] == "t"
    assert calls[0]["type"] == "TRADE" and calls[0]["end"] == 1700000000 and calls[0]["start"] == 1
    assert buf.getvalue().count("capped") == 1, buf.getvalue()
    raises(lambda: api.fetch_user_history_up_to("nan", 1), ValueError)
    raises(lambda: api.fetch_user_history_up_to(float("nan"), 1), ValueError)


def test_error_payload_raises_not_empty():
    class Resp:
        status_code = 200
        ok = True
        text = '{"error":"boom"}'
        headers = {}

        def json(self):
            return {"error": "boom"}

    orig = api.SESSION.get
    api.SESSION.get = lambda *a, **k: Resp()
    try:
        e = raises(lambda: api.get_json("https://example.invalid/x"), api.PolymarketAPIError)
    finally:
        api.SESSION.get = orig
    assert "boom" in str(e)


# ── scrape_insiders ──────────────────────────────────────────────────────────

def test_wallet_for_falls_back_to_user_id():
    base = {"user_id": WALLET.upper(), "dollar_amount": 1, "first_trade": "2026-01-01T00:00:00+00:00"}
    assert scrape_insiders.wallet_for(pd.Series({**base, "eoa_address": np.nan})) == WALLET
    assert scrape_insiders.wallet_for(pd.Series({**base, "eoa_address": ""})) == WALLET
    assert scrape_insiders.wallet_for(pd.Series({**base, "eoa_address": " 0xABC "})) == "0xabc"
    assert scrape_insiders.wallet_for(pd.Series(base)) == WALLET
    # through analyze_whale with a scripted feed: NaN eoa no longer aborts
    row = pd.Series({**base, "eoa_address": np.nan, "dollar_amount": 1500.0, "first_trade": "2026-03-05T16:41:49+00:00"})
    rows = [{"proxy_wallet": WALLET, "timestamp": 1772728900 - i, "type": "TRADE", "size": 10, "price": 0.5,
             "usdc_size": 5.0, "transaction_hash": f"tx{i}"} for i in range(12)]
    with fake_feed([page(rows, False, None)]) as calls:
        got = scrape_insiders.analyze_whale(row)
    assert calls[0]["user"] == WALLET
    assert got["prior_bet_count"] == 12 and got["is_first_x"] is False
    assert got["lifetime_portfolio"] == 1500.0 and got["portfolio_concentration"] == 100.0 and got["Is_percent"] is True


def _clean_rows(outcome_win="Yes"):
    return pd.DataFrame([
        {"user_id": WALLET, "eoa_address": "", "market_id": "0x1", "market_title": "T?", "outcome": "Yes", "side": "BUY",
         "avg_odds": 0.5, "contracts": 10, "dollar_amount": 5.0, "win_status": "WIN" if outcome_win == "Yes" else "LOSS",
         "first_trade": "2026-01-01T00:00:00+00:00", "last_trade": "2026-01-01T00:00:00+00:00", "num_trades": 1},
        {"user_id": "0x" + "1" * 40, "eoa_address": "", "market_id": "0x1", "market_title": "T?", "outcome": "No", "side": "BUY",
         "avg_odds": 0.5, "contracts": 10, "dollar_amount": 5.0, "win_status": "LOSS" if outcome_win == "Yes" else "WIN",
         "first_trade": "2026-01-01T00:00:00+00:00", "last_trade": "2026-01-01T00:00:00+00:00", "num_trades": 1},
    ])


def test_correct_outcome_must_match_win_status():
    scrape_insiders.check_correct_outcome(_clean_rows("Yes"), "Yes", "m")
    scrape_insiders.check_correct_outcome(_clean_rows("Yes"), "yes", "m")
    scrape_insiders.check_correct_outcome(_clean_rows("Yes"), None, "m")
    e = raises(lambda: scrape_insiders.check_correct_outcome(_clean_rows("Yes"), "No", "superbowl"), ValueError)
    assert "superbowl" in str(e) and "losing side" in str(e)
    unresolved = _clean_rows("Yes").assign(win_status="UNRESOLVED")
    scrape_insiders.check_correct_outcome(unresolved, "No", "m")


@contextlib.contextmanager
def scratch_repo(markets, clean_frames):
    """A temp repo root with markets.json and data/<Output>_clean.csv files; cwd switched into it."""
    tmp = tempfile.mkdtemp(prefix="rigged-offline-")
    cwd = os.getcwd()
    try:
        os.makedirs(os.path.join(tmp, "data"))
        with open(os.path.join(tmp, "markets.json"), "w") as f:
            json.dump(markets, f)
        for m, df in zip(markets, clean_frames):
            df.to_csv(os.path.join(tmp, "data", f"{m['output']}_clean.csv"), index=False)
        os.chdir(tmp)
        yield tmp
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_zero_whale_market_writes_full_header_and_exports():
    markets = [{"name": "tiny", "slug": "tiny-market", "output": "Tiny", "correct_outcome": "Yes"}]
    with scratch_repo(markets, [_clean_rows("Yes")]) as tmp:
        with fake_feed([]):  # no whale -> no history fetched
            with contextlib.redirect_stdout(io.StringIO()):
                scrape_insiders.main(keyword="tiny")
        ins = os.path.join(tmp, "data", "Tiny_insiders.csv")
        with open(ins) as f:
            text = f.read()
        assert text.strip() == ",".join(scrape_insiders.INSIDER_COLS), text
        assert list(pd.read_csv(ins).columns) == scrape_insiders.INSIDER_COLS
        # export.py: the header-only file (and the old lone-market_title shape) mean "no traders"
        assert export.load_insiders(markets[0]) is None
        with open(ins, "w") as f:
            f.write("market_title\n")
        assert export.load_insiders(markets[0]) is None
        orig_out = export.OUT_DIR
        export.OUT_DIR = os.path.join(tmp, "out")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                export.main()
            with open(os.path.join(export.OUT_DIR, "index.json")) as f:
                idx = json.load(f)["markets"]
            with open(os.path.join(export.OUT_DIR, "markets", "tiny.json")) as f:
                market = json.load(f)
        finally:
            export.OUT_DIR = orig_out
    assert len(idx) == 1 and idx[0]["traderWhales"] is None and idx[0]["flaggedAccounts"] is None
    assert market["traders"] is None and market["timeline"]["positions"] == 2


def test_scoring_a_wrong_sided_market_is_refused():
    markets = [{"name": "wrong", "slug": "wrong-side", "output": "Wrong", "correct_outcome": "No"}]
    with scratch_repo(markets, [_clean_rows("Yes")]):
        with contextlib.redirect_stdout(io.StringIO()):
            e = raises(lambda: scrape_insiders.main(keyword="wrong"), ValueError)
        assert not os.path.exists(os.path.join("data", "Wrong_insiders.csv"))
    assert "correct_outcome='No'" in str(e)


def test_failed_history_aborts_the_market_without_writing():
    markets = [{"name": "fail", "slug": "fail-market", "output": "Fail", "correct_outcome": "Yes"}]
    clean = _clean_rows("Yes")
    clean.loc[0, "dollar_amount"] = 5000.0
    with scratch_repo(markets, [clean]):
        with fake_feed([{"error": "boom"}]):
            orig = api.get_json

            def get_json(url, params=None, timeout=30, retry=api.RETRY):
                raise api.PolymarketAPIError("HTTP 500 boom")
            api.get_json = get_json
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    e = raises(lambda: scrape_insiders.main(keyword="fail"), RuntimeError)
            finally:
                api.get_json = orig
        assert not os.path.exists(os.path.join("data", "Fail_insiders.csv"))
    assert WALLET in str(e) and "boom" in str(e)


# ── module hygiene ───────────────────────────────────────────────────────────

def test_import_run_does_not_run_the_pipeline():
    called = []
    orig = polymarket_trades.main
    polymarket_trades.main = lambda *a, **k: called.append(a)
    try:
        sys.modules.pop("run", None)
        import run  # noqa: F401
        assert hasattr(run, "main")
        with contextlib.redirect_stdout(io.StringIO()):
            assert run.main(["zzz-no-such-market"]) == 1
    finally:
        polymarket_trades.main = orig
    assert called == []


def test_raw_csv_schema_unchanged():
    cols = list(polymarket_trades.format_row({"proxyWallet": "", "timestamp": 0}, {}).keys())
    assert cols == ["user_id", "eoa_address", "side", "market_id", "market_title", "outcome", "odds", "contracts",
                    "dollar_amount", "win_status", "timestamp", "transaction_hash"]
    assert scrape_insiders.INSIDER_COLS[-1] == "market_title"


def test_import_graph_has_no_cycle():
    """Every module imports cleanly in a fresh interpreter, in either order,
    and importing run does not touch the network or data/."""
    import subprocess
    for order in ("run, polymarket_trades, scrape_insiders, export",
                  "scrape_insiders, polymarket_trades, export, run"):
        code = f"import {order}; import polymarket_api; print('ok')"
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


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
