#!/usr/bin/env python3
"""
Offline tests for the watchlist foundation (watchlist.filters, .store,
.net): no network, a few seconds.

    python3 -m pytest -q tests/test_watchlist_core.py     # or
    python3 tests/test_watchlist_core.py                  # same checks, exits 1 on failure

Covers the exclusion rules (including the hit/reach-a-price pattern and
the questions it must leave alone), resolution and the open pool, event
grouping and main-market choice, series keys, the store's round trips, and
net's keyset and closed-market walks, rate-limit buckets and install()
over scripted pages. Network calls are replaced by fakes; a test that
would reach the network fails instead.
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket_api  # noqa: E402
from watchlist import filters, net, store  # noqa: E402
from watchlist.common import DAY, to_epoch  # noqa: E402

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


# ── store ────────────────────────────────────────────────────────────────────

def test_store_meta_and_ai_labels_round_trip():
    conn = store.connect(":memory:")
    store.set_meta(conn, "cursor", {"a": 1})
    assert store.get_meta(conn, "cursor") == {"a": 1} and store.get_meta(conn, "nope", 5) == 5
    store.save_ai_label(conn, "1", "sha", {"type": "other", "reveal_kind": "unclear"}, "m", True, {"x": 1})
    assert store.get_ai_label(conn, "1", "sha")["type"] == "other"


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
    net._INSTALLED.pop("orig", None)
    orig = polymarket_api.get_json
    polymarket_api.get_json = lambda url, params=None, timeout=30, retry=5: seen.append(url) or {"ok": 1}
    try:
        net.install()
        net.install()   # idempotent
        before = net.BUCKETS["data_trades"].taken
        assert polymarket_api.get_json(f"{polymarket_api.DATA_API_V2}/trades") == {"ok": 1}
        assert net.BUCKETS["data_trades"].taken == before + 1 and len(seen) == 1
    finally:
        net._INSTALLED.pop("orig", None)
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
