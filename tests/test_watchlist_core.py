#!/usr/bin/env python3
"""
Offline tests for the watchlist foundation (watchlist.filters, .method,
.store, .net): no network, a few seconds.

    python3 -m pytest -q tests/test_watchlist_core.py     # or
    python3 tests/test_watchlist_core.py                  # same checks, exits 1 on failure

Covers the exclusion rules (including the hit/reach-a-price pattern and
the questions it must leave alone), resolution and the open pool, event
grouping and main-market choice, series keys, the method's flag maths and
price-jump detection, an end-to-end run of method.analyze_market on a
scripted market (the whale set must be exactly scrape_insiders'), the exact
cutoff (the one-call bracket, exact checks only where it cannot decide,
net.exact_prior), the too_large guard on a capped fill walk, the store
round trips and the user-volume cache (null / non-numeric answers raise),
and net's keyset, closed-market and recent-trade walks over scripted pages.
Network calls are replaced by fakes; a test that would reach the network
fails instead.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket_api  # noqa: E402
import polymarket_trades  # noqa: E402
from watchlist import filters, method, net, store  # noqa: E402
from watchlist.common import DAY, now_epoch, to_epoch  # noqa: E402

NOW = 1_794_300_000  # 2026-11-10T08:40:00Z, fixed so the tests never depend on the clock


def W(n):
    """A valid wallet address."""
    return "0x" + f"{n:040x}"


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


def market(q="Will X happen?", tags=(), event=None, **kw):
    m = {"id": "1", "conditionId": "0x" + "a" * 64, "question": q, "outcomes": '["Yes", "No"]',
         "outcomePrices": '["0.5", "0.5"]', "volumeNum": 150_000, "closed": False, "active": True,
         "endDate": "2026-11-20T00:00:00Z", "tags": [{"slug": t} for t in tags]}
    if event is not None:
        m["events"] = [event]
    m.update(kw)
    return m


@contextlib.contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def no_network(*a, **k):
    raise AssertionError(f"network call in an offline test: {a[:1]}")


# ── filters: exclusions ──────────────────────────────────────────────────────

def test_games_are_excluded_by_tag_sport_gameid_type_and_pattern():
    ex = filters.exclusion
    assert ex(market(tags=["sports", "nba"]))[0] == "game"
    assert ex(market(event={"id": "e", "title": "t", "tags": [{"slug": "esports"}]}))[0] == "game"
    assert ex(market(), event={"title": "t", "tags": ["games"]})[0] == "game"
    assert ex(market(event={"id": "e", "title": "t", "sport": {"sport": "cfb"}}))[0] == "game"
    assert ex(market(event={"id": "e", "title": "t", "gameId": 90117197}))[0] == "game"
    assert ex(market(sportsMarketType="spreads"))[0] == "game"
    for q in ("Wizards vs. Pistons", "your end vs Damajuana", "Lakers O/U 220.5", "Spread: Pistons (-8.5)",
              "Will Arsenal win on 2026-11-08?", "Counter-Strike: A - B (BO3)"):
        assert ex(market(q=q))[0] == "game", q
    # Gamma sends gameId -1 on non-game events ("Blue wave in 2026?")
    assert ex(market(q="Blue wave in 2026?", event={"id": "e", "title": "Blue wave", "gameId": -1})) is None
    assert ex(market(q="Blue wave in 2026?", event={"id": "e", "title": "Blue wave", "gameId": "-1"})) is None
    # "versus" inside words, and "v." court captions, are not games
    assert ex(market(q="Will the Supreme Court rule in Trump v. CASA?")) is None
    assert ex(market(q="Will Canvas launch?")) is None


def test_price_and_weather_markets_are_excluded():
    ex = filters.exclusion
    assert ex(market(tags=["crypto", "crypto-prices"]))[0] == "price"
    assert ex(market(tags=["up-or-down"]))[0] == "price"
    assert ex(market(tags=["hit-price"]))[0] == "price"
    for q in ("Bitcoin Up or Down - November 9, 3PM ET", "Will the price of gold exceed it?",
              "Will Bitcoin be above $90,000 on November 10?", "Will oil be below $80 on November 2?",
              "Will ETH close between $2,000 and $2,100?", "Will Gold (GC) dip to (LOW) $3,500 by December?",
              "1win FDV above $70M one day after launch?"):
        assert ex(market(q=q))[0] == "price", q
    for q in ("Highest temperature in NYC on November 9?", "Will it be 70°F in Miami?", "Will Paris hit 30 °C?"):
        assert ex(market(q=q))[0] == "weather", q


def test_keyword_layer_keeps_real_candidates():
    ex = filters.exclusion
    # Gamma tags pandemic markets "weather"; only weather WORDS exclude
    assert ex(market(q="Ebola pandemic in 2026?", tags=["weather", "pandemics"])) is None
    # valuation ladders resolve on a funding round (a company decision)
    assert ex(market(q="Will OpenAI's valuation hit (HIGH) $2.0T by December 31?",
                     tags=["finance", "privates"])) is None
    for q in ("US x Iran ceasefire continues through November 6?", "Kristi Noem out by March 31?",
              "Will Michael B. Jordan win Best Actor at the 98th Academy Awards?",
              "Will the total domestic gross for Resident Evil be between 150m and 160m by October 31?"):
        assert ex(market(q=q, tags=["politics"])) is None, q


def test_hit_or_reach_a_price_is_a_price_market():
    ex = filters.exclusion
    # questions seen on Gamma (2026-11-10), none of them carrying a price tag
    for q in ("Will STRC hit $100 by December 31?", "Will Bitcoin reach $150k by December 31?",
              "When will Ethereum hit $3k?", "Will Gold (GC) hit (HIGH) $5,000 by end of December?",
              "Will S&P 500 (SPX) hit $9,000 (HIGH) in December?", "Will US gas hit $4.45 (High) in November?",
              "Will the Ornn H100 Index hit $2.25 (LOW) by December 31, 2026?", "Tesla hits $500 before 2027?",
              "Will XRP reach $3.00 in November?", "Will Pump.fun reach $0.0150 by December 31, 2026?",
              "Will Bitcoin reach $1,000,000 before 2030?"):
        assert ex(market(q=q, tags=["finance"])) == ("price", f"pattern {filters.PRICE_LEVEL_LABEL}"), q
    # the event title counts too
    assert ex(market(q="December 31?"), event={"title": "When will Hyperliquid hit $100?"})[0] == "price"


def test_hit_or_reach_without_a_price_stays():
    ex = filters.exclusion
    for q in (
            # sizes in M/B/T: valuation ladders (a funding round decides them), debt, revenue, open interest
            "Will ByteDance's valuation hit (HIGH) $1.5T by December 31?",
            "Will Perplexity's valuation hit (LOW) $17.5B by December 31?",
            "Will the U.S. national debt hit $39 trillion before 2027?",
            "Will Based Polymarket revenue hit $1M before 2027?",
            "Will Hyperliquid HIP-3 open interest hit $1B in 2026?",
            "Will Wicked: For Good hit $150M opening weekend?", "Will Kalshi hit a $10B valuation?",
            "Will Elon Musk's net worth hit $1T?", "Will the Powerball jackpot hit $900 million?",
            # an amount without a suffix, but the question names a size
            "Will the federal minimum wage reach $15 by 2027?", "Will the reward for his capture reach $50,000?",
            "Will the FTC fine hit $500 per violation?",
            # no dollar amount at all
            "Will Trump's approval hit 50%?", "Will Zelensky reach a deal with Putin by June?",
            "Will MrBeast hit 500M subscribers?", "Will the hurricane hit Florida?",
            "Will Taylor Swift hit #1 on the Billboard Hot 100?", "Will Ukraine reach the Dnipro?"):
        assert ex(market(q=q, tags=["politics"])) is None, q
        assert not filters.price_level(q), q
    # the size word counts only in the same part (question or event title) as the amount
    assert filters.price_level("Will the minimum wage reach $15? | Minimum wage hikes") is False
    assert filters.price_level("Will STRC hit $100 by December 31? | STRC revenue and price") is True


# ── filters: resolution, pool ────────────────────────────────────────────────

def test_winning_outcome_needs_resolution_and_one_price_of_one():
    w = filters.winning_outcome
    assert w(market(outcomePrices='["1", "0"]', umaResolutionStatus="resolved")) == (0, "Yes")
    assert w(market(outcomePrices='["0", "1"]', umaResolutionStatus="resolved")) == (1, "No")
    assert w(market(outcomePrices=["0", "1"], outcomes=["Up", "Down"], umaResolutionStatus="resolved")) == (1, "Down")
    assert w(market(outcomePrices='["0.5", "0.5"]', umaResolutionStatus="resolved")) is None
    assert w(market(outcomePrices='["1", "0"]', umaResolutionStatus="proposed")) is None
    assert w(market(outcomePrices='["1", "0"]')) is None
    assert w(market(outcomePrices="not json", umaResolutionStatus="resolved")) is None


def test_pool_keeps_long_shots_and_drops_only_decided_markets():
    end = "2026-11-20T00:00:00Z"
    r = lambda **kw: filters.pool_reason(market(**{"endDate": end, **kw}), now=NOW)  # noqa: E731
    assert r(outcomePrices='["0.04", "0.96"]') is None          # 4 cents before a war: kept
    assert r(outcomePrices='["0.985", "0.015"]') is None
    assert r(outcomePrices='["0.99", "0.01"]') == "decided"     # >= 99 cents: only arbitrage left
    assert r(outcomePrices='["0.995", "0.005"]') == "decided"
    assert r(closed=True) == "closed"
    assert r(active=False) == "inactive"
    assert r(volumeNum=99_999) == "volume"
    assert r(volumeNum=100_000) is None
    assert r(endDate=None) == "no_end_date"
    assert r(endDate="2027-03-01T00:00:00Z") == "ends_later"
    assert r(endDate="2026-11-01T00:00:00Z") is None             # past its end, still open: kept
    assert r(outcomePrices="[]") == "no_prices"
    assert r(tags=["sports"]) == "game"
    assert r(q="Bitcoin Up or Down?") == "price"
    # the 60-day horizon is inclusive
    assert filters.pool_reason(market(endDate=NOW + 60 * DAY), now=NOW) is None
    assert filters.pool_reason(market(endDate=NOW + 60 * DAY + 1), now=NOW) == "ends_later"


def test_open_pool_groups_by_event_and_counts_reasons():
    e1 = {"id": 1, "title": "Iran", "tags": [{"slug": "politics"}],
          "markets": [market(id="a", volumeNum=200_000), market(id="b", volumeNum=500_000),
                      market(id="c", closed=True)]}
    e2 = {"id": 2, "title": "NBA", "tags": [{"slug": "sports"}], "markets": [market(id="d")]}
    e3 = {"id": 3, "title": "Small", "tags": [], "markets": [market(id="e", volumeNum=10)]}
    groups, counts = filters.open_pool([e1, e2, e3], now=NOW)
    assert [g["event_id"] for g in groups] == ["1"]
    assert [m["id"] for m in groups[0]["markets"]] == ["b", "a"]   # highest volume first
    assert counts == {"kept": 2, "closed": 1, "game": 1, "volume": 1}


# ── filters: events ──────────────────────────────────────────────────────────

def resolved(mid, vol, yes_won, event_id="E", neg_risk=False):
    return {"id": mid, "conditionId": "0x" + mid * 64, "question": mid, "volumeNum": vol, "negRisk": neg_risk,
            "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]' if yes_won else '["0", "1"]',
            "umaResolutionStatus": "resolved", "events": [{"id": event_id, "title": "T"}]}


def test_group_by_event_keeps_first_seen_order():
    ms = [resolved("a", 1, True, "E1"), resolved("b", 1, True, "E2"), resolved("c", 1, True, "E1"),
          {"id": "z", "question": "no event"}]
    groups = filters.group_by_event(ms)
    assert [g["event_id"] for g in groups] == ["E1", "E2", "m:z"]
    assert [m["id"] for m in groups[0]["markets"]] == ["a", "c"]
    assert groups[0]["event"]["title"] == "T"


def test_main_market_is_the_yes_market_of_a_multi_market_event():
    yes_small, no_big = resolved("a", 150_000, True, neg_risk=True), resolved("b", 900_000, False, neg_risk=True)
    main, others, rule = filters.choose_main_market([no_big, yes_small])
    assert main is yes_small and others == [no_big] and rule == "resolved_yes"
    # a date ladder that resolved several Yes: the highest-volume Yes
    l1, l2, l3 = resolved("a", 200_000, True), resolved("b", 300_000, True), resolved("c", 900_000, False)
    main, others, rule = filters.choose_main_market([l1, l2, l3])
    assert main is l2 and others == [l3, l1] and rule == "resolved_yes"
    # no Yes among them: highest volume
    n1, n2 = resolved("a", 200_000, False), resolved("b", 300_000, False)
    assert filters.choose_main_market([n1, n2])[:1] == (n2,)
    # a single (non-negRisk) market: itself, even when it resolved No
    single = resolved("a", 200_000, False)
    assert filters.choose_main_market([single]) == (single, [], "highest_volume")
    # a single negRisk market that resolved No: itself
    single_nr = resolved("a", 200_000, False, neg_risk=True)
    assert filters.choose_main_market([single_nr])[0] is single_nr
    raises(lambda: filters.choose_main_market([]), ValueError)


def test_series_key_prefers_gamma_series_then_stripped_title():
    assert filters.series_key({"seriesSlug": "nba-2026", "title": "x"}) == "nba-2026"
    assert filters.series_key({"series": [{"slug": "fed-decisions"}], "title": "x"}) == "fed-decisions"
    a = filters.series_key({"title": "US x Iran ceasefire continues through November 6, 2026?"})
    b = filters.series_key({"title": "US x Iran ceasefire continues through December 31?"})
    assert a == b == "title:us x iran ceasefire continues"
    assert filters.title_key("Kristi Noem out by...?") == "kristi noem out"
    assert filters.title_key("Elon Musk # tweets November 3 - November 10, 2026?") == "elon musk tweets"
    assert filters.title_key("Oscars 2026: Best Actor Winner") == "oscars best actor winner"
    assert filters.series_key({}) is None


# ── method: flag maths, jump ─────────────────────────────────────────────────

def test_flags_match_analyze_whale():
    f = method.flag_whale
    assert f(1500, 10, 50_000)["is_first_x"] is True          # <= 10 prior trades
    assert f(1500, 11, 50_000)["is_first_x"] is False
    r = f(1000, 50, 1200)                                      # 1000 / 1200 = 83 %
    assert r["is_percent"] and r["flagged"] and round(r["concentration"], 2) == 83.33
    r = f(1000, 50, 1250)                                      # exactly 80 % counts
    assert r["is_percent"] and r["concentration"] == 80.0
    r = f(1000, 50, 1251)
    assert not r["is_percent"] and not r["flagged"]
    r = f(1000, 50, 0)                                         # no prior dollars: lifetime = position
    assert r["lifetime_portfolio"] == 1000 and r["concentration"] == 100.0
    assert f(0, 50, 0)["concentration"] == 100.0
    assert method.lookup_end(1_771_000_000, "same_day") == 1_771_000_000
    assert method.lookup_end(1_771_000_000, "day_before") == 1_771_000_000 - DAY
    raises(lambda: method.lookup_end(1, "hour_before"), ValueError)


def test_price_jump_needs_to_hold_above_the_floor():
    j = method.price_jump
    pts = [(100, 0.05), (200, 0.06), (300, 0.95), (400, 0.55), (500, 0.07), (600, 0.91), (700, 0.97), (800, 0.999)]
    assert j(pts) == (600, 0.91)                    # the 300 spike fell back below 0.80
    assert j(list(reversed(pts))) == (600, 0.91)    # order does not matter
    assert j([(1, 0.5), (2, 0.89), (3, 0.85)]) is None   # never reached 0.90
    assert j([(1, 0.2), (2, 0.92), (3, 0.79)]) is None   # fell below the floor at the end
    assert j([(1, 0.92), (2, 0.85), (3, 0.99)]) == (1, 0.92)
    # trades in the same second count together: a 0.95 and a 0.5 at t=5 is no jump at 5
    assert j([(5, 0.95), (5, 0.5), (6, 0.93)]) == (6, 0.93)
    assert j([]) is None


def test_dust_fills_do_not_move_the_jump():
    def row(ts, price, usd, outcome="Yes"):
        return {"outcome": outcome, "odds": str(price), "dollar_amount": str(usd), "timestamp": ts}
    rows = [row("2026-09-07T01:00:45+00:00", 0.91, 50), row("2026-10-20T05:10:14+00:00", 0.689, 0.81),
            row("2026-10-21T00:00:00+00:00", 0.95, 100), row("2026-09-08T00:00:00+00:00", 0.10, 500, "No")]
    pts = method.winning_points(rows, "yes")
    assert len(pts) == 2   # the $0.81 fill and the losing side are left out
    assert method.price_jump(pts)[0] == to_epoch("2026-09-07T01:00:45+00:00")
    assert method.price_jump(method.winning_points(rows, "Yes", min_usd=0))[0] == to_epoch("2026-10-21T00:00:00Z")


def test_summarize_totals_and_before_jump():
    ws = [{"position": 3000.0, "flagged": True, "is_first_x": True, "is_percent": False, "before_jump": True},
          {"position": 1500.0, "flagged": False, "is_first_x": False, "is_percent": False, "before_jump": True},
          {"position": 1200.0, "flagged": True, "is_first_x": False, "is_percent": True, "before_jump": False}]
    s = method.summarize(ws)
    assert s["whale_count"] == 3 and s["whale_capital"] == 5700.0
    assert s["flagged_count"] == 2 and s["flagged_capital"] == 4200.0 and s["flagged_pct"] == 73.68
    assert s["first_x_count"] == 1 and s["percent_count"] == 1
    assert s["whale_capital_before_jump"] == 4500.0 and s["flagged_capital_before_jump"] == 3000.0
    assert s["flagged_count_before_jump"] == 1
    assert method.summarize([])["flagged_pct"] == 0.0


# ── method: end to end on a scripted market ──────────────────────────────────

CID = "0x" + "c" * 64
T0 = to_epoch("2026-10-01T12:00:00Z")
JUMP = T0 + 20 * DAY


def scripted_market():
    return {"id": "777", "conditionId": CID, "question": "Will X happen by November 30?", "slug": "will-x",
            "closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]',
            "umaResolutionStatus": "resolved", "closedTime": "2026-10-25 00:00:00+00", "endDate": "2026-11-30",
            "volumeNum": 150_000, "negRisk": False, "events": [{"id": "55", "title": "X by...?", "slug": "x"}]}


def scripted_trades(m):
    """/v2/trades rows (what polymarket_api.fetch_trades returns) for a market where:
    A buys Yes $3000 @0.30 early (new account -> flagged), D buys Yes $1500 @0.50
    (busy wallet), F buys Yes $1200 @0.95 after the jump (concentrated -> flagged),
    B sweeps Yes @0.995 (left out), C bets No (losing side), E is below the
    $1000 threshold, G only SELLs Yes (an exit row, left out). The Yes price
    spikes to 0.95 on day 5, falls back, then jumps for good on day 20."""
    def act(wallet, ts, side, outcome, price, usd, idx):
        return {"proxy_wallet": wallet, "timestamp": ts, "side": side, "outcome": outcome,
                "outcome_index": idx, "price": price, "size": usd / price, "condition_id": m["conditionId"],
                "transaction_hash": f"0x{ts:x}{wallet[-4:]}"}
    acts = [
        act(W(0xA), T0, "BUY", "Yes", 0.30, 3000.0, 0),
        act(W(0xD), T0 + DAY, "BUY", "Yes", 0.50, 1500.0, 0),
        act(W(0xC), T0 + 2 * DAY, "BUY", "No", 0.60, 5000.0, 1),
        act(W(0xE), T0 + 3 * DAY, "BUY", "Yes", 0.40, 500.0, 0),
        act(W(0x51), T0 + 5 * DAY, "BUY", "Yes", 0.95, 20.0, 0),     # spike ...
        act(W(0x52), T0 + 6 * DAY, "BUY", "Yes", 0.40, 20.0, 0),     # ... that falls back
        act(W(0x9), JUMP, "BUY", "Yes", 0.92, 50.0, 0),              # the jump
        act(W(0x53), JUMP + 3600, "BUY", "Yes", 0.50, 1.0, 0),       # dust after the jump: ignored
        act(W(0xF), JUMP + DAY, "BUY", "Yes", 0.95, 1200.0, 0),
        act(W(0xB), JUMP + 2 * DAY, "BUY", "Yes", 0.995, 2000.0, 0),
        act(W(0x6), JUMP + 2 * DAY, "SELL", "Yes", 0.97, 2000.0, 0),
    ]
    return acts


PRIOR = {W(0xA): (2, 3000.0), W(0xD): (500, 1_000_000.0), W(0xF): (50, 1300.0)}


@contextlib.contextmanager
def scripted_env(prior=None, day_fills=None):
    """Temp cache dir, in-memory store, fake trade download, user-volume feed
    and /v2/activity feed. prior(wallet, end) -> (trade_count, usdc) replaces
    PRIOR; day_fills = {wallet: [activity rows]} serves the exact check.
    calls["capped"] makes the fake fetch_trades report a capped walk."""
    tmp = tempfile.mkdtemp(prefix="watchlist-core-")
    conn = store.connect(":memory:")
    calls = {"trades": 0, "uv": [], "activity": [], "capped": False}

    def fetch_trades(condition_id, max_rows=None, on_progress=None):
        calls["trades"] += 1
        calls["trades_max_rows"] = max_rows
        return scripted_trades(scripted_market()), calls["capped"]

    def get_json(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        assert url.endswith("/user-volume"), url
        calls["uv"].append(dict(params))
        n, usd = prior(params["user"], params["end"]) if prior else PRIOR[params["user"]]
        return {"data": {"volume": usd * 2, "volume_usdc": usd, "trade_count": n}}

    def legacy_get_json(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        if not url.endswith("/activity") or day_fills is None:
            return no_network(url)
        calls["activity"].append(dict(params))
        rows = day_fills.get(params["user"], [])
        return {"data": rows, "pagination": {"has_more": False, "next_cursor": None}}

    try:
        with patched(polymarket_api, "fetch_trades", fetch_trades), \
                patched(polymarket_trades, "fetch_market_trades", no_network), \
                patched(net, "get_json", get_json), patched(net, "install", lambda: None), \
                patched(polymarket_api, "get_json", legacy_get_json), contextlib.redirect_stdout(io.StringIO()):
            yield tmp, conn, calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_analyze_market_end_to_end():
    with scripted_env() as (tmp, conn, calls):
        res = method.analyze_market(scripted_market(), "same_day", conn=conn, cache_dir=tmp)
        assert res["status"] == "ok" and res["winner"] == "Yes" and res["threshold"] == 1000.0
        assert [w["user_id"] for w in res["whales"]] == [W(0xA), W(0xD), W(0xF)]   # by position
        assert res["sweeps"]["count"] == 1 and res["exits"]["count"] == 1
        flagged = {w["user_id"]: (w["is_first_x"], w["is_percent"]) for w in res["whales"] if w["flagged"]}
        assert flagged == {W(0xA): (True, True), W(0xF): (False, True)}
        assert res["whale_capital"] == 5700.0 and res["flagged_capital"] == 4200.0 and res["flagged_pct"] == 73.68
        assert res["jump_ts"] == JUMP and res["reveal_basis"] == "price_jump"
        assert res["flagged_capital_before_jump"] == 3000.0 and res["whale_capital_before_jump"] == 4500.0
        by = {w["user_id"]: w for w in res["whales"]}
        assert by[W(0xA)]["before_jump"] is True and by[W(0xF)]["before_jump"] is False
        assert by[W(0xA)]["lookup_end"] == T0 and by[W(0xA)]["hours_before_jump"] == 480.0
        assert {c["end"] for c in calls["uv"]} == {T0, T0 + DAY, JUMP + DAY}
        assert all(c["start"] == 1 for c in calls["uv"])
        # files: gzip'd raw + clean in the cache dir; nothing else left behind
        assert sorted(os.listdir(tmp)) == [f"{CID}.csv.gz", f"{CID}_clean.csv"]
        assert store.get_result(conn, CID, "same_day")["flagged_capital"] == 4200.0
        assert len(store.get_whales(conn, CID, "same_day")) == 3

        # day_before: reuses the cached trades, looks up one day earlier
        calls["uv"].clear()
        res2 = method.analyze_market(scripted_market(), "day_before", conn=conn, cache_dir=tmp)
        assert res2["trades_source"] == "cache" and calls["trades"] == 1
        assert {c["end"] for c in calls["uv"]} == {T0 - DAY, T0, JUMP}
        assert [w["user_id"] for w in res2["whales"]] == [W(0xA), W(0xD), W(0xF)]
        assert store.analysed_conditions(conn, "day_before") == {CID}

        # the same variant again: every look-up is now a cache hit
        calls["uv"].clear()
        res3 = method.analyze_market(scripted_market(), "same_day", conn=conn, cache_dir=tmp)
        assert res3["lookups"] == {"cached": 3, "fetched": 0, "exact_checks": 0, "exact_requests": 0}
        assert calls["uv"] == [] and res3["decided_by"] == {"one_call": 3}


def test_analyze_market_guards():
    with scripted_env() as (tmp, conn, calls):
        big = method.analyze_market(scripted_market(), conn=conn, cache_dir=tmp, max_rows=3)
        assert big["status"] == "too_large" and os.listdir(tmp) == [] and big["trades_capped"] is False
        assert calls["trades_max_rows"] == 4    # one row over the cap, to tell "exactly max_rows" from "more"
        vol = method.analyze_market(scripted_market(), conn=conn, cache_dir=tmp, max_volume=100_000)
        assert vol["status"] == "too_large" and calls["trades"] == 1   # not even fetched
        open_m = {**scripted_market(), "umaResolutionStatus": None, "outcomePrices": '["0.5", "0.5"]'}
        assert method.analyze_market(open_m, conn=conn, cache_dir=tmp)["status"] == "unresolved"
        raises(lambda: method.analyze_market(scripted_market(), "week_before", conn=conn, cache_dir=tmp), ValueError)
        # a wrong correct_outcome is refused, exactly like score_market
        e = raises(lambda: method.analyze_market(scripted_market(), conn=conn, cache_dir=tmp, correct_outcome="No"),
                   ValueError)
        assert "losing side" in str(e)


def test_a_capped_fill_walk_is_too_large_even_under_max_rows():
    # fetch_trades drops CLOB-operator rows AFTER the walk's row cap, so a
    # capped walk can hand back <= max_rows rows: the capped flag decides
    with scripted_env() as (tmp, conn, calls):
        calls["capped"] = True
        for max_rows in (1000, None):     # None: polymarket_api's page safety net capped it
            res = method.analyze_market(scripted_market(), conn=conn, cache_dir=tmp, max_rows=max_rows)
            assert res["status"] == "too_large" and res["trades_capped"] is True, res
            assert res["n_trades"] == 11 and "capped" in res["too_large"]
        assert os.listdir(tmp) == [] and store.get_result(conn, CID, "exact")["status"] == "too_large"
        calls["capped"] = False
        assert method.analyze_market(scripted_market(), conn=conn, cache_dir=tmp, max_rows=1000)["status"] == "ok"


def test_a_failed_lookup_aborts_the_market():
    with scripted_env() as (tmp, conn, calls):
        def boom(url, params=None, timeout=30, retry=polymarket_api.RETRY):
            raise polymarket_api.PolymarketAPIError("HTTP 500 boom")
        with patched(net, "get_json", boom):
            for v in method.VARIANTS:
                raises(lambda: method.analyze_market(scripted_market(), v, conn=conn, cache_dir=tmp),
                       polymarket_api.PolymarketAPIError)
                assert store.get_result(conn, CID, v) is None


# ── method: the exact cutoff ─────────────────────────────────────────────────

def test_bracket_decides_only_when_both_flags_agree_and_the_answers_are_ordered():
    b = method.bracket
    # 0 prior trades before the day, 14 fills / $2,000 by its end: is_first_x flips -> check
    r = b(3000, {"trade_count": 0, "volume_usdc": 0}, {"trade_count": 14, "volume_usdc": 2000})
    assert not r["agree"] and r["monotone"] and r["before"]["is_first_x"] and not r["same_day"]["is_first_x"]
    # flagged on both, but is_first_x differs: still checked (the counts are reported)
    r = b(3000, {"trade_count": 2, "volume_usdc": 100}, {"trade_count": 30, "volume_usdc": 200})
    assert r["before"]["flagged"] and r["same_day"]["flagged"] and not r["agree"]
    # a busy wallet: nothing flips, the bracket decides
    r = b(1500, {"trade_count": 500, "volume_usdc": 1e6}, {"trade_count": 510, "volume_usdc": 1.01e6})
    assert r["agree"] and not r["before"]["flagged"]
    # a brand-new wallet that only built this position on its first day: flagged either way
    r = b(5000, {"trade_count": 0, "volume_usdc": 0}, {"trade_count": 3, "volume_usdc": 5000})
    assert r["agree"] and r["before"]["flagged"] and r["same_day"]["flagged"]
    # is_percent flips at 80 % between the bounds -> check
    r = b(1200, {"trade_count": 50, "volume_usdc": 1300}, {"trade_count": 60, "volume_usdc": 1600})
    assert not r["agree"] and r["before"]["is_percent"] and not r["same_day"]["is_percent"]
    # not ordered (before > same_day): the argument does not hold -> check, even though flags agree
    r = b(1500, {"trade_count": 500, "volume_usdc": 1e6}, {"trade_count": 490, "volume_usdc": 1e6})
    assert not r["monotone"] and not r["agree"]
    assert b(1500, {"trade_count": 5, "volume_usdc": 100.004}, {"trade_count": 5, "volume_usdc": 100.0})["agree"]


def _fill(wallet, ts, usd, tx, type_="TRADE"):
    return {"type": type_, "proxy_wallet": wallet, "timestamp": ts, "size": usd * 2, "price": 0.5, "usdc_size": usd,
            "transaction_hash": tx, "condition_id": "0x" + "e" * 64}


def test_analyze_market_exact_checks_only_the_disagreements():
    firsts = {W(0xA): T0, W(0xD): T0 + DAY, W(0xF): JUMP + DAY}
    answers = {   # (day_before, same_day) user-volume answers
        W(0xA): ((2, 0.0), (14, 2000.0)),            # is_first_x flips -> exact check
        W(0xD): ((500, 1e6), (510, 1.01e6)),         # nothing flips -> agree
        W(0xF): ((50, 1300.0), (60, 1600.0)),        # is_percent flips -> exact check
    }

    def prior(wallet, end):
        before, same = answers[wallet]
        assert end in (firsts[wallet], firsts[wallet] - DAY), (wallet, end)
        return same if end == firsts[wallet] else before

    d0 = lambda ts: ts // DAY * DAY  # noqa: E731
    fills = {
        # A: 5 fills (2 transactions) up to and including its first trade
        W(0xA): [_fill(W(0xA), d0(T0) + 60, 100, "0x1"), _fill(W(0xA), d0(T0) + 60, 100, "0x1"),
                 _fill(W(0xA), d0(T0) + 90, 100, "0x1"), _fill(W(0xA), T0, 100, "0x2"),
                 _fill(W(0xA), T0, 100, "0x2")],
        # F: 4 fills / $200 that count; a later fill, an earlier-day fill and a REDEEM that do not
        W(0xF): [_fill(W(0xF), d0(JUMP + DAY) + 5, 50, "0xf1"), _fill(W(0xF), d0(JUMP + DAY) + 6, 50, "0xf2"),
                 _fill(W(0xF), JUMP + DAY, 50, "0xf3"), _fill(W(0xF), JUMP + DAY, 50, "0xf3"),
                 _fill(W(0xF), JUMP + DAY + 1, 9999, "0xlate"), _fill(W(0xF), d0(JUMP + DAY) - 1, 9999, "0xearly"),
                 _fill(W(0xF), JUMP + DAY - 5, 9999, "0xr", type_="REDEEM")],
    }
    with scripted_env(prior=prior, day_fills=fills) as (tmp, conn, calls):
        res = method.analyze_market(scripted_market(), conn=conn, cache_dir=tmp)    # exact is the default
        assert res["status"] == "ok" and res["cutoff_variant"] == "exact"
        assert res["decided_by"] == {"agree": 1, "exact_check": 2}
        by = {w["user_id"]: w for w in res["whales"]}
        a, d, f = by[W(0xA)], by[W(0xD)], by[W(0xF)]
        # A: 2 before its day + 5 fills of the day up to the trade = 7 <= 10, $500 -> both flags
        assert a["decided_by"] == "exact_check" and a["prior_trade_count"] == 7 and a["prior_volume_usdc"] == 500.0
        assert a["is_first_x"] and a["is_percent"] and a["flagged"] and a["prior_counts_exact"] is True
        assert a["exact_check"]["day_rows"] == 5 and a["exact_check"]["day_tx"] == 2
        assert a["prior_bounds"]["day_before"] == {"trade_count": 2, "volume_usdc": 0.0}
        # F: 54 fills, $1,500 -> 1200 / 1500 = 80 % -> is_percent
        assert f["decided_by"] == "exact_check" and f["prior_trade_count"] == 54 and f["prior_volume_usdc"] == 1500.0
        assert f["is_percent"] and not f["is_first_x"] and f["flagged"]
        # D: decided by the bracket; numbers are the day_before answer, marked as a bound
        assert d["decided_by"] == "agree" and not d["flagged"] and d["prior_trade_count"] == 500
        assert d["prior_counts_exact"] is False and d["exact_check"] is None and d["lookup_end"] == T0
        assert d["prior_bounds"]["same_day"]["trade_count"] == 510
        assert res["flagged_capital"] == 4200.0 and res["first_x_count"] == 1 and res["percent_count"] == 2
        # one /v2/activity walk per disagreement, from the first-trade day's midnight to the trade
        assert sorted((c["user"], c["start"], c["end"], c["type"]) for c in calls["activity"]) == sorted(
            [(W(0xA), d0(T0), T0, "TRADE"), (W(0xF), d0(JUMP + DAY), JUMP + DAY, "TRADE")])
        # both one-call bounds for every whale (the exact check's day_before is a cache hit)
        assert len(calls["uv"]) == 6
        assert res["lookups"]["exact_checks"] == 2 and res["lookups"]["fetched"] == 3
        assert store.get_result(conn, CID, "exact")["decided_by"] == {"agree": 1, "exact_check": 2}

        # again: every look-up and every exact check is a cache hit
        calls["uv"].clear()
        calls["activity"].clear()
        res2 = method.analyze_market(scripted_market(), "exact", conn=conn, cache_dir=tmp)
        assert calls["uv"] == [] and calls["activity"] == [] and res2["lookups"]["cached"] == 3
        assert [w["flagged"] for w in res2["whales"]] == [w["flagged"] for w in res["whales"]]

        # the one-call variants still answer from one bound each, and they bracket the exact flags
        sd = {w["user_id"]: w for w in method.analyze_market(scripted_market(), "same_day", conn=conn,
                                                              cache_dir=tmp)["whales"]}
        db = {w["user_id"]: w for w in method.analyze_market(scripted_market(), "day_before", conn=conn,
                                                              cache_dir=tmp)["whales"]}
        for u, w in by.items():
            assert sd[u]["decided_by"] == db[u]["decided_by"] == "one_call"
            assert (not sd[u]["flagged"] or w["flagged"]) and (not w["flagged"] or db[u]["flagged"])


def test_exact_prior_is_day_before_volume_plus_the_first_day():
    conn = store.connect(":memory:")
    first = T0
    seen = []

    def uv(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        seen.append(("uv", dict(params)))
        return {"data": {"volume": 10.0, "volume_usdc": 175.0, "trade_count": 3}}

    def legacy(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        seen.append(("activity", dict(params)))
        d0 = first // DAY * DAY
        return {"data": [_fill(W(1), d0 + 10, 10.0, "0xc"), _fill(W(1), first, 1000.0, "0xe"),
                         _fill(W(1), first, 500.0, "0xe")], "pagination": {"has_more": False, "next_cursor": None}}

    with patched(net, "get_json", uv), patched(polymarket_api, "get_json", legacy), \
            patched(net, "install", lambda: None):
        got = net.exact_prior(W(1), first, conn=conn)
        again = net.exact_prior(W(1), first, conn=conn)
    assert got["trade_count"] == 6 and got["volume_usdc"] == 1685.0 and got["day_rows"] == 3 and got["day_tx"] == 2
    assert got["base_trade_count"] == 3 and got["capped"] is False and got["cached"] is False
    assert seen[0] == ("uv", {"user": W(1), "start": 1, "end": first - DAY})
    assert seen[1][0] == "activity" and seen[1][1]["start"] == first // DAY * DAY and seen[1][1]["end"] == first
    assert seen[1][1]["type"] == "TRADE" and len(seen) == 2          # the second call: all from the store
    assert again["cached"] is True and again["trade_count"] == 6 and again["requests"] == 0
    raises(lambda: net.day_trades("0x12", first, conn=conn), ValueError)


def test_pick_verify_sample_and_compare_with_crawl():
    ws = [{"user_id": W(i), "position": 10_000 - i, "decided_by": "exact_check" if i % 3 == 0 else "agree"}
          for i in range(30)]
    s = method.pick_verify_sample(ws, 16)
    assert len(s) == 16 and len({w["user_id"] for w in s}) == 16
    assert sum(1 for w in s if w["decided_by"] == "exact_check") == 10   # every check (10 of them), then agree rows
    assert s[-1]["user_id"] == W(29)                                     # agree rows spread to the smallest
    w = {"user_id": W(1), "position": 1200.0, "first_trade": "t", "decided_by": "exact_check", "is_first_x": False,
         "is_percent": True, "flagged": True, "prior_trade_count": 54, "prior_volume_usdc": 1500.0,
         "prior_counts_exact": True, "prior_bounds": {"day_before": {"trade_count": 50}, "same_day": {"trade_count": 60}}}
    hist = [{"transactionHash": f"0x{i // 2}", "usdcSize": 1500.0 / 54} for i in range(54)]
    rec = method.compare_with_crawl(w, hist, capped=False)
    assert rec["flags_match_crawl_fills"] and rec["crawl"]["transactions"] == 27 and rec["crawl_fills_within_bounds"]
    assert rec["exact_minus_crawl"] == {"fills": 0, "volume_usdc": 0.0}
    # a capped crawl decides only when its floors rule both flags out
    assert not method.compare_with_crawl(w, hist, capped=True)["crawl"]["determined"]
    big = [{"transactionHash": f"0x{i}", "usdcSize": 100.0} for i in range(50)]
    assert method.compare_with_crawl({**w, "is_percent": False, "flagged": False}, big, capped=True)["crawl"]["determined"]


# ── store / user-volume cache ────────────────────────────────────────────────

def test_user_volume_cache_settled_and_unsettled_days():
    conn = store.connect(":memory:")
    calls = []

    def get_json(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        calls.append(params)
        return {"data": {"volume": 10.0, "volume_usdc": 5.0, "trade_count": 3}}

    with patched(net, "get_json", get_json):
        past = to_epoch("2026-02-13T16:06:57Z")
        a = net.user_volume(W(1).upper().replace("0X", "0x"), past, conn=conn)
        b = net.user_volume(W(1), past + 3600, conn=conn)              # same UTC day: cached
        assert (a["cached"], b["cached"]) == (False, True) and b["trade_count"] == 3 and len(calls) == 1
        assert calls[0] == {"user": W(1), "start": 1, "end": past}
        net.user_volume(W(1), past - DAY, conn=conn)                  # previous day: its own row
        assert len(calls) == 2
        today = now_epoch()
        net.user_volume(W(2), today, conn=conn)
        net.user_volume(W(2), today, conn=conn)                       # unsettled but fresh: cached
        assert len(calls) == 3
        conn.execute("UPDATE wallet_volume SET fetched_at = fetched_at - 3600 WHERE wallet = ?", (W(2),))
        net.user_volume(W(2), today, conn=conn)                       # unsettled and stale: re-fetched
        assert len(calls) == 4
        assert store.get_wallet_volume(conn, W(1), past // DAY)["complete"] == 1
        assert store.get_wallet_volume(conn, W(2), today // DAY)["complete"] == 0
        for bad in ("nan", "0x123", None, W(1) + "00"):
            raises(lambda: net.user_volume(bad, past, conn=conn), ValueError)
        assert len(calls) == 4


def test_user_volume_rejects_a_malformed_answer():
    conn = store.connect(":memory:")
    with patched(net, "get_json", lambda *a, **k: {"data": {"volume": 1}}):
        raises(lambda: net.user_volume(W(3), 1_771_000_000, conn=conn), polymarket_api.PolymarketAPIError)
    assert store.get_wallet_volume(conn, W(3), 1_771_000_000 // DAY) is None


def test_user_volume_null_or_non_numeric_is_an_error_never_a_new_wallet():
    conn = store.connect(":memory:")
    good = {"volume": 10.0, "volume_usdc": 5.0, "trade_count": 3}
    bad = [{**good, "trade_count": None}, {**good, "volume_usdc": None}, {**good, "volume": None},
           {**good, "trade_count": "n/a"}, {**good, "volume_usdc": "abc"}, {**good, "trade_count": True},
           {**good, "trade_count": 2.5}, {**good, "volume_usdc": -1}, {**good, "volume_usdc": float("nan")},
           {**good, "volume": [1]}, {"volume": 1.0, "volume_usdc": 1.0}, {**good, "volume_usdc": {}}]
    for i, data in enumerate(bad):
        with patched(net, "get_json", lambda *a, _d=data, **k: {"data": _d}):
            e = raises(lambda: net.user_volume(W(10 + i), 1_771_000_000, conn=conn), polymarket_api.PolymarketAPIError)
        assert "null, missing or not a number" in str(e), data
        assert store.get_wallet_volume(conn, W(10 + i), 1_771_000_000 // DAY) is None   # nothing cached
    # real zeros are zeros (a new wallet), numeric strings are numbers
    with patched(net, "get_json", lambda *a, **k: {"data": {"volume": 0, "volume_usdc": 0.0, "trade_count": 0}}):
        z = net.user_volume(W(50), 1_771_000_000, conn=conn)
    assert (z["trade_count"], z["volume_usdc"], z["cached"]) == (0, 0.0, False)
    assert store.get_wallet_volume(conn, W(50), 1_771_000_000 // DAY)["trade_count"] == 0
    with patched(net, "get_json", lambda *a, **k: {"data": {"volume": "12.5", "volume_usdc": "7", "trade_count": "4"}}):
        s = net.user_volume(W(51), 1_771_000_000, conn=conn)
    assert (s["trade_count"], s["volume_usdc"], s["volume"]) == (4, 7.0, 12.5)
    assert net.parse_user_volume({"data": {"volume": 1, "volume_usdc": 2, "trade_count": 3.0}})["trade_count"] == 3


def test_store_events_keep_their_type():
    conn = store.connect(":memory:")
    ev = {"id": 9, "title": "Kristi Noem out by...?", "slug": "noem", "tags": [{"slug": "politics"}]}
    store.save_event(conn, ev, main_condition_id="0xm", other_condition_ids=["0xo"], closed_ts=100)
    store.set_event_type(conn, 9, "person_leaves_role", "ai:test")
    store.save_event(conn, ev, main_condition_id="0xm2", closed_ts=100)
    got = store.get_event(conn, 9)
    assert got["type"] == "person_leaves_role" and got["main_condition_id"] == "0xm2"
    assert got["series_key"] == "title:kristi noem out" and got["tags"] == ["politics"]
    assert [e["event_id"] for e in store.events_closed_between(conn, 0, 101)] == ["9"]
    store.save_market(conn, resolved("a", 150_000, True, "9"), "main", winner="Yes")
    assert store.get_market(conn, "0x" + "a" * 64)["role"] == "main"
    store.set_meta(conn, "cursor", {"a": 1})
    assert store.get_meta(conn, "cursor") == {"a": 1} and store.get_meta(conn, "nope", 5) == 5
    store.save_ai_label(conn, "1", "sha", {"type": "other", "reveal_kind": "unclear"}, "m", True, {"x": 1})
    assert store.get_ai_label(conn, "1", "sha")["type"] == "other"


def test_closed_results_view_joins_series_and_type():
    with scripted_env() as (tmp, conn, calls):
        m = scripted_market()
        method.analyze_market(m, "same_day", conn=conn, cache_dir=tmp)
        store.save_event(conn, filters.event_of(m), main_condition_id=CID, closed_ts=to_epoch(m["closedTime"]))
        store.set_event_type(conn, "55", "court_regulator", "manual")
        rows = store.closed_results(conn, "same_day", since_ts=0, until_ts=NOW)
        assert len(rows) == 1 and rows[0]["type"] == "court_regulator" and rows[0]["series_key"] == "title:x"
        assert rows[0]["flagged_capital"] == 4200.0
        assert store.closed_results(conn, "day_before") == []
        assert store.closed_results(conn, "same_day", since_ts=NOW) == []


def test_budget():
    from watchlist.common import Budget
    assert not Budget().expired() and Budget().left() == float("inf")
    b = Budget(0)
    assert b.expired() and b.left() == 0.0
    assert not Budget(60).expired(reserve=10) and Budget(5).expired(reserve=10)


# ── net: scripted walks ──────────────────────────────────────────────────────

def test_closed_markets_walks_updated_at_and_rereads_the_top():
    def mk(mid, closed, updated, vol=150_000):
        return {"id": mid, "closedTime": closed, "updatedAt": updated, "volumeNum": vol}
    since, until = to_epoch("2026-11-01T00:00:00Z"), to_epoch("2026-11-05T00:00:00Z")
    walk = [
        {"markets": [mk("1", "2026-11-06 00:00:00+00", "2026-11-06T00:00:00Z"),       # after until
                     mk("2", "2026-11-04 10:00:00.5+00", "2026-11-04T10:00:01Z"),
                     mk("3", "2026-11-03 00:00:00+00", "2026-11-03T00:00:00Z", vol=50)], "next_cursor": "c1"},
        {"markets": [mk("4", "2026-11-01 00:00:00+00", "2026-11-02T00:00:00Z"),
                     mk("5", "2026-10-30 00:00:00+00", "2026-10-30T00:00:00Z")], "next_cursor": "c2"},
    ]
    # the re-read of the top finds 6, updated while the walk ran
    top = [{"markets": [mk("6", "2026-11-02 00:00:00+00", "2099-01-01T00:00:00Z"),
                        mk("2", "2026-11-04 10:00:00.5+00", "2026-11-04T10:00:01Z")], "next_cursor": None}]
    pages, seen = iter(walk + top), []

    def get_json(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        seen.append(dict(params))
        return next(pages)

    with patched(net, "get_json", get_json):
        out = net.closed_markets(since, until)
    assert [m["id"] for m in out] == ["2", "6", "4"]    # newest close first, deduped, volume-filtered
    assert seen[0]["order"] == "updatedAt" and seen[0]["ascending"] == "false" and seen[0]["include_tag"] == "true"
    assert seen[1]["after_cursor"] == "c1" and "after_cursor" not in seen[2] and len(seen) == 3


def test_keyset_repeated_cursor_raises():
    with patched(net, "get_json", lambda *a, **k: {"events": [{"id": 1}], "next_cursor": "same"}):
        raises(lambda: net.walk_open_events(), polymarket_api.PolymarketAPIError)


def _scripted_keyset(pages):
    seen, it = [], iter(pages)

    def get_json(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        seen.append(dict(params))
        return next(it)
    return get_json, seen


def test_keyset_empty_page_with_a_cursor_is_retried_once_then_raises():
    out = io.StringIO()
    # empty with a cursor, then the retry serves the rows: the walk goes on
    fake, seen = _scripted_keyset([{"events": [{"id": 1}], "next_cursor": "c1"},
                                   {"events": [], "next_cursor": "c2"},
                                   {"events": [{"id": 2}], "next_cursor": "c2"},
                                   {"events": [{"id": 3}]}])
    with patched(net, "get_json", fake), contextlib.redirect_stdout(out):
        evs = net.walk_open_events()
    assert [e["id"] for e in evs] == [1, 2, 3] and len(seen) == 4
    assert seen[1].get("after_cursor") == seen[2].get("after_cursor") == "c1"   # the same page asked again
    assert seen[3]["after_cursor"] == "c2" and "asking again" in out.getvalue()
    # empty with a cursor twice: raise, never a silently truncated list
    fake, seen = _scripted_keyset([{"events": [{"id": 1}], "next_cursor": "c1"},
                                   {"events": [], "next_cursor": "c2"}, {"events": [], "next_cursor": "c2"}])
    with patched(net, "get_json", fake), contextlib.redirect_stdout(io.StringIO()):
        e = raises(lambda: net.walk_open_events(), polymarket_api.PolymarketAPIError)
    assert "empty twice" in str(e) and len(seen) == 3
    # the real end at an exact multiple of the limit: a full page with a cursor, then empty WITHOUT one
    fake, seen = _scripted_keyset([{"events": [{"id": 1}], "next_cursor": "c1"}, {"events": []}])
    with patched(net, "get_json", fake):
        assert [e["id"] for e in net.walk_open_events()] == [1] and len(seen) == 2
    # the closed-markets walk's stop() never sees an empty page
    fake, seen = _scripted_keyset([{"markets": [], "next_cursor": None}, {"markets": [], "next_cursor": None}])
    with patched(net, "get_json", fake):
        assert net.closed_markets(0) == [] and len(seen) == 2


def test_recent_trades_stops_at_the_window_start():
    cid = "0x" + "d" * 64

    def page(rows, more, cursor):
        return {"data": rows, "pagination": {"has_more": more, "next_cursor": cursor}}

    def t(ts, wallet=W(1)):
        return {"timestamp": ts, "proxy_wallet": wallet}
    pages = iter([page([t(500), t(450), t(400, next(iter(polymarket_api.OPERATORS)))], True, "c1"),
                  page([t(300), t(200), t(100)], True, "c2"),
                  page([t(50)], False, None)])
    seen = []

    def get_json(url, params=None, timeout=30, retry=polymarket_api.RETRY):
        seen.append(dict(params))
        return next(pages)

    with patched(net, "get_json", get_json):
        rows, capped = net.recent_trades(cid, since=250, until=480)
    assert [r["timestamp"] for r in rows] == [450, 300] and capped is False
    assert len(seen) == 2 and seen[0]["taker_only"] == "false" and seen[1]["cursor"] == "c1"
    raises(lambda: net.recent_trades("0x12", 0), ValueError)


def test_rate_limit_families_and_bucket():
    assert net.family(f"{polymarket_api.GAMMA_API}/markets/keyset") == "gamma_markets"
    assert net.family(f"{polymarket_api.GAMMA_API}/events/keyset") == "gamma_events"
    assert net.family(f"{polymarket_api.DATA_API_V2}/trades") == "data_trades"
    assert net.family(f"{polymarket_api.DATA_API_V2}/activity") == "data_activity"
    assert net.family(f"{polymarket_api.DATA_API_V2}/user-volume") == "data_general"
    # 40 % of the documented limits
    assert abs(net.BUCKETS["data_general"].rate - 32.0) < 1e-9 and abs(net.BUCKETS["gamma_markets"].rate - 12.0) < 1e-9
    b = net.TokenBucket(rate=200.0, burst=2)
    import time
    t = time.monotonic()
    for _ in range(12):
        b.acquire()
    assert time.monotonic() - t >= 10 / 200.0 * 0.9    # 2 free, 10 paced at 200/s


def test_install_routes_legacy_calls_through_the_buckets():
    seen = []
    net.uninstall()
    orig = polymarket_api.get_json
    polymarket_api.get_json = lambda url, params=None, timeout=30, retry=5: seen.append(url) or {"ok": 1}
    try:
        net.install()
        net.install()   # idempotent
        before = net.BUCKETS["data_trades"].taken
        assert polymarket_api.get_json(f"{polymarket_api.DATA_API_V2}/trades") == {"ok": 1}
        assert net.BUCKETS["data_trades"].taken == before + 1 and len(seen) == 1
    finally:
        net.uninstall()
        polymarket_api.get_json = orig


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
