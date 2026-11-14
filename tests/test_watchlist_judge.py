#!/usr/bin/env python3
"""
Offline tests for the closed list's check (watchlist.judge) and its news
search (watchlist.news): no network, a fake Gemini client and a fake feed.

    python3 tests/test_watchlist_judge.py

Covers the RSS parsing (CDATA, entities, the " - Source" cut, dates to UTC),
the after: / before: operators, what the model sees of a market (no trading
numbers), normalising its answer, the sort order, the cache, a market
whose news search fails being left for the next run, and the prompt's
approved factual changes (closed-check-5, 2026-11-13) with its judgement
tests left as they were.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchlist import judge, kinds, news, store  # noqa: E402
from watchlist.common import to_epoch  # noqa: E402

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Mexico Unexpectedly Cuts Key Rate to 6.25% - Bloomberg.com</title>
<link>https://news.google.com/rss/articles/abc</link><pubDate>Thu, 05 Nov 2026 20:12:04 GMT</pubDate>
<source url="https://www.bloomberg.com">Bloomberg.com</source></item>
<item><title><![CDATA[Banxico &amp; the peso: what&#39;s next]]></title><link>https://news.google.com/x</link>
<pubDate>Fri, 06 Nov 2026 01:12:37 +0000</pubDate></item>
<item><title></title><pubDate>bad</pubDate></item>
</channel></rss>"""

MARKET = {"conditionId": "0xabc123456789ffff", "id": "1", "question": "Bank of Mexico decreases interest rates by 25 bps?",
          "description": "This market will resolve according to the Bank of Mexico's decision.",
          "createdAt": "2026-08-06T19:14:55Z", "closedTime": "2026-11-05 21:02:05+00",
          "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]', "volumeNum": 203129.05,
          "lastTradePrice": 0.999, "umaResolutionStatus": "resolved",
          "events": [{"title": "Bank of Mexico Decision in November?", "slug": "banxico-nov"}]}


def test_parse_rss():
    hs = news.parse(RSS)
    assert len(hs) == 2
    assert hs[0] == {"title": "Mexico Unexpectedly Cuts Key Rate to 6.25%", "source": "Bloomberg.com",
                     "published": "2026-11-05T20:12:04Z", "url": "https://news.google.com/rss/articles/abc"}
    assert hs[1]["title"] == "Banxico & the peso: what's next" and hs[1]["published"] == "2026-11-06T01:12:37Z"
    assert news.parse("") == [] and news.parse(None) == []


def test_build_query():
    assert news.build_query("Banxico  rate decision", 1_790_000_000, "2026-11-07T00:00:00Z") == \
        "Banxico rate decision after:2026-09-21 before:2026-11-07"
    assert news.build_query("x") == "x"


def test_card_has_no_trading_numbers():
    c = judge.card(MARKET, [{"title": "b", "source": "s", "published": "2026-11-06T00:00:00Z"},
                            {"title": "a", "source": "s", "published": "2026-11-05T00:00:00Z"}])
    assert c["id"] == "0xabc1234567" and c["resolved"] == "Yes" and c["opened"] == "2026-08-06"
    assert c["event"] == "Bank of Mexico Decision in November?"
    assert [h["title"] for h in c["headlines"]] == ["a", "b"]                 # oldest first
    text = json.dumps(c)
    assert "203129" not in text and "0.999" not in text and "volume" not in text.lower()


def test_normalise_and_sort():
    assert judge.normalise({"verdict": "maybe"}) is None
    c = judge.normalise({"verdict": "Potential Case", "surprise": "YES", "public_signals": "lots", "reason": " r "})
    assert c["verdict"] == "potential case" and c["surprise"] == "yes" and c["public_signals"] == "unknown"
    assert c["reason"] == "r" and c["who_knew_first"] == ""
    a = {"verdict": "weak", "surprise": "yes", "public_signals": "none"}
    b = {"verdict": "potential case", "surprise": "no", "public_signals": "clear"}
    d = {"verdict": "potential case", "surprise": "yes", "public_signals": "some"}
    assert sorted([a, b, d], key=judge.sort_key) == [d, b, a]
    assert judge.fallback_query(MARKET) == "Bank Mexico decreases interest rates 25 bps"


def test_settle_and_window():
    base = {"verdict": "potential case", "surprise": "yes", "public_signals": "none", "who_knew_first": "Banxico's board"}
    assert judge.settle(base)["verdict"] == "potential case"
    assert judge.settle(dict(base, public_signals="some"))["verdict"] == "weak"          # MSCI: flagged months before
    assert judge.settle(dict(base, surprise="somewhat"))["verdict"] == "weak"                # a phone call, a CPI print
    assert judge.settle(dict(base, public_signals="unknown"))["verdict"] == "potential case"
    assert judge.settle(dict(base, who_knew_first="nobody"))["verdict"] == "weak"
    assert judge.settle(dict(base, who_knew_first="Unknown"))["verdict"] == "weak"          # names no one either
    assert judge.settle(dict(base, verdict="not a case", surprise="no"))["verdict"] == "not a case"
    late = dict(MARKET, endDate="2026-10-05T00:00:00Z", closedTime="2026-11-12T00:00:00Z")   # settled weeks later
    end, closed = to_epoch("2026-10-05T00:00:00Z"), to_epoch("2026-11-12T00:00:00Z")
    assert judge.news_window(late) == (end - judge.NEWS_BEFORE_DAYS * 86400, closed + judge.NEWS_AFTER_DAYS * 86400)
    early = dict(late, endDate="2026-12-31T00:00:00Z")                                        # closed before its end date
    assert judge.news_window(early)[0] == closed - judge.NEWS_BEFORE_DAYS * 86400



def test_prompt_after_the_factual_changes():
    p = judge.JUDGE_PROMPT
    assert judge.PROMPT_VERSION not in ("closed-check-3", "closed-check-4")     # 4 is the untested judgement draft
    # who could know first: the shared examples, no central bank board or statistics body
    assert f"before the public: {kinds.KNOW_FIRST_EXAMPLES}. Name them." in p
    for gone in ("central bank", "compiles a figure", "a live show or stream"):
        assert gone not in p.lower(), gone
    # a show that only reveals a settled answer, a company's year-end list and a scripted speech: not built up in public
    for kept in ("a setlist, a taped finale, a sealed result", "a company's own year-end list",
                 "the words of a scripted speech or a produced video", "a game or contest decided while viewers watched"):
        assert kept in p, kept
    # no example drawn from a market of the days the page shows (no Huang-shaped "board investigation is talk")
    for name in ("Huang", "Nvidia", "investigation", "Banxico", "Mexico", "Dancing", "Gemini", "Fable", "Iran"):
        assert name not in p, name
    # the judgement tests are unchanged (talk, timing surprise, weaker case, public long before close wait for a test)
    for same in ("A potential case has all three of these:", "2. It was unlikely beforehand.",
                 "without clear public signs beforehand (leaks, reports that it was coming, an official schedule or warning)",
                 "the headlines show it was public well before the market closed and the market only settled later",
                 '"potential case" when all three points clearly hold'):
        assert same in p, same
    assert "{" not in p and "}" not in p                                        # the f-string left nothing unfilled
    assert "search query" not in p                                              # FakeClient tells the prompts apart by it

class FakeClient:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, schema=None):
        self.prompts.append(prompt)
        if "search query" in prompt:
            return json.dumps({"queries": [{"id": "0xabc1234567", "query": "Banxico rate cut"}]}), "fake", {}
        return json.dumps({"checks": [{"id": "0xabc1234567", "what_happened": "Banxico cut by surprise.",
                                       "public_at": "2026-11-05T20:12:04Z", "surprise": "yes",
                                       "who_knew_first": "Banxico's board", "public_signals": "none",
                                       "verdict": "potential case", "reason": "Few expected it."}]}), "fake", {}


def test_check_markets_and_cache(monkey=None):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(store.SCHEMA)
    seen = []
    orig = news.search
    news.search = lambda q, after=None, before=None, limit=15: (seen.append((q, after, before)) or news.parse(RSS))
    try:
        client = FakeClient()
        out = judge.check_markets([MARKET], conn, client)
        c = out[MARKET["conditionId"]]
        assert c["verdict"] == "potential case" and c["query"] == "Banxico rate cut" and len(c["headlines"]) == 2
        q, after, before = seen[0]
        assert q == "Banxico rate cut" and before - after == (judge.NEWS_BEFORE_DAYS + judge.NEWS_AFTER_DAYS) * 86400
        assert "Mexico Unexpectedly Cuts Key Rate" in client.prompts[1] and "203129" not in client.prompts[1]
        n = len(client.prompts)
        assert judge.check_markets([MARKET], conn, client)[MARKET["conditionId"]]["reason"] == "Few expected it."
        assert len(client.prompts) == n                                      # cached: no new calls

        def fail(*a, **k):
            raise news.NewsError("news search: HTTP 503")
        news.search = fail
        other = dict(MARKET, conditionId="0xdef000000000aaaa")
        assert judge.check_markets([other], conn, client) == {}             # tried again next run
        assert store.get_closed_check(conn, other["conditionId"], judge.PROMPT_VERSION) is None
    finally:
        news.search = orig


def test_headlines_leave_out_odds():
    orig = news.search
    found = [{"title": "Mexico Unexpectedly Cuts Key Rate", "source": "Bloomberg", "published": "2026-11-12T19:00:00Z"},
             {"title": "Banxico cut odds jump on Polymarket", "source": "Yahoo", "published": "2026-11-12T10:00:00Z"},
             {"title": "Will Banxico cut? Traders weigh in", "source": "Kalshi News", "published": "2026-11-12T11:00:00Z"},
             {"title": "Anthropic cuts prices 20%", "source": "Quartz", "published": "2026-11-12T12:00:00Z"}]
    news.search = lambda q, after=None, before=None, limit=15: list(found)
    try:
        got = [h["title"] for h in judge.headlines_for(MARKET, "Banxico rate cut")]
    finally:
        news.search = orig
    assert got == ["Mexico Unexpectedly Cuts Key Rate", "Anthropic cuts prices 20%"], got


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
