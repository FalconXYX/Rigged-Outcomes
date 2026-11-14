#!/usr/bin/env python3
"""
Offline tests for the AI labeller (watchlist.ai): no network, no real key.

    python3 -m pytest -q tests/test_watchlist_ai.py     # or
    python3 tests/test_watchlist_ai.py                  # same checks, exits 1 on failure

Covers reading an Interactions response (the model_output step behind a
thought step), decoding the JSON answer, quote matching (whitespace and
typographic folding, nothing looser) and the minimum quote length,
per-market validation (type list, quote in rules, reveal date rules, the
year after a day, date source, a past reveal), the prompt (fallback
clauses, valuation ladders, version), batching, the client's retry /
fallback / key handling against a scripted HTTP session (which errors skip
a model and which do not), and label_markets' cache (id + rules sha1,
prompt version, refresh, save=False, an id the answer left out, a refused
batch split in halves). The key is a fake set in the module; a test that
would reach the network fails instead. Dates are judged against a fixed
TODAY so the checks do not age.
"""
import contextlib
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests  # noqa: E402

from watchlist import ai, store  # noqa: E402
from watchlist.common import to_epoch  # noqa: E402

FAKE_KEY = "AIzaFAKE-test-key-0123456789"
TODAY = "2026-11-10"

RULES = ('This market will resolve to \u201cYes\u201d if the Academy announces the winner at the ceremony on March 15, 2027.\n\n'
         'Otherwise, it will resolve to "No".  The resolution source is the official broadcast.')

# the 2026 Taiwan local-election rules, shortened: the election date, then a fallback clause one year later
TAIWAN = ("Taiwanese local elections are scheduled to be held on November 28, 2026. \n\nThis market will resolve "
          "according to the party whose official candidates win the most head of local government elections. "
          "This market will remain open until a party has won or until the results of all of the relevant elections "
          "are made official. If the results of any of the relevant 2026 Taiwanese local elections aren\u2019t known "
          "by June 30, 2027 11:59 PM ET, the winning party will be determined based on the available results up to "
          "that point.")
TAIWAN_FALLBACK = ("If the results of any of the relevant 2026 Taiwanese local elections aren\u2019t known by "
                   "June 30, 2027 11:59 PM ET, the winning party will be determined based on the available results "
                   "up to that point.")


@contextlib.contextmanager
def fake_key(key=FAKE_KEY):
    old = dict(ai._KEY)
    ai._KEY.clear()
    ai._KEY["key"] = key
    try:
        yield
    finally:
        ai._KEY.clear()
        ai._KEY.update(old)


def interaction(text, status="completed", model="gemini-3.5-flash-lite"):
    """A response shaped like the real one (2026-11-10)."""
    return {"id": "v1_x", "status": status, "model": model, "object": "interaction",
            "usage": {"total_tokens": 36}, "steps": [
                {"type": "thought", "signature": "EmAKXg=="},
                {"type": "model_output", "content": [{"type": "text", "text": text}]}]}


class FakeResp:
    def __init__(self, status, payload=None, headers=None, text=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Answers post() from a script: a list of FakeResp / exceptions, or a
    function (body) -> FakeResp. Records every request."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, params=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout, "params": params})
        if callable(self.script):
            r = self.script(json)
        else:
            r = self.script.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def client(script, **kw):
    kw.setdefault("min_interval", 0)
    return ai.Client(session=FakeSession(script), sleep=lambda s: None, **kw)


def market(mid, rules=RULES, q="Will Film X win Best Picture?", end="2027-03-16T04:59:00Z", event=None):
    m = {"id": mid, "question": q, "description": rules, "endDate": end, "conditionId": "0x" + "a" * 64}
    if event:
        m["events"] = [event]
    return m


def answer(mid, type_="predecided_reveal", kind="scheduled", date="2027-03-15",
           quote="the winner at the ceremony on March 15, 2027"):
    return {"id": str(mid), "type": type_, "reveal_kind": kind, "reveal_date": date, "quote": quote}


def batch_reply(answers, model="gemini-3.5-flash-lite"):
    return FakeResp(200, interaction(json.dumps({"labels": answers}), model=model))


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


# ── response parsing ─────────────────────────────────────────────────────────

def test_output_text_reads_the_model_output_step():
    assert ai.output_text(interaction('{"a": 1}')) == '{"a": 1}'
    two = interaction("ignored")
    two["steps"][1]["content"] = [{"type": "text", "text": '{"a": '}, {"type": "image", "data": "x"},
                                  {"type": "text", "text": "1}"}]
    assert ai.output_text(two) == '{"a": 1}'
    legacy = {"status": "completed", "outputs": [{"type": "thought"}, {"type": "text", "text": "hi"}]}
    assert ai.output_text(legacy) == "hi"


def test_output_text_rejects_unfinished_or_empty_interactions():
    raises(lambda: ai.output_text(interaction("x", status="in_progress")), ai.GeminiError)
    raises(lambda: ai.output_text({"status": "completed", "steps": [{"type": "thought", "signature": "s"}]}),
           ai.GeminiError)
    raises(lambda: ai.output_text({"error": {"message": "bad", "code": "invalid_request"}}), ai.GeminiError)
    raises(lambda: ai.output_text(interaction("   ")), ai.GeminiError)
    raises(lambda: ai.output_text(["not", "a", "dict"]), ai.GeminiError)


def test_parse_json_plain_fenced_and_embedded():
    assert ai.parse_json('{"labels": []}') == {"labels": []}
    assert ai.parse_json('```json\n{"labels": [1]}\n```') == {"labels": [1]}
    assert ai.parse_json('```\n[1, 2]\n```') == [1, 2]
    assert ai.parse_json('Here you go: {"a": {"b": 2}} hope it helps') == {"a": {"b": 2}}
    raises(lambda: ai.parse_json("no json here"), ValueError)
    raises(lambda: ai.parse_json(""), ValueError)


def test_answers_by_id_accepts_both_shapes_and_keeps_the_first():
    got = ai.answers_by_id({"labels": [{"id": 7, "type": "a"}, {"id": "7", "type": "b"}, {"type": "no id"}, "junk"]})
    assert got == {"7": {"id": 7, "type": "a"}}
    assert ai.answers_by_id([{"id": " 9 "}]) == {"9": {"id": " 9 "}}
    assert ai.answers_by_id({"something": "else"}) == {}


# ── quote matching ───────────────────────────────────────────────────────────

def test_quote_matching_folds_whitespace_and_typography_only():
    q = ai.quote_in_rules
    assert q('resolve to "Yes" if the Academy announces', RULES)          # straight quotes vs curly in the rules
    assert q("resolve to \u201cNo\u201d.  The resolution", RULES)
    assert q("Otherwise,   it will\nresolve", RULES)                       # whitespace runs and newlines
    assert q("ceremony on March 15, 2027.", RULES)                         # trailing punctuation
    assert q("announces the winner at the ceremony on March 15, 2027. Otherwise", RULES)  # across the paragraph break
    assert q("non\u00a0breaking", "a non breaking space")
    assert q("A \u2013 B", "A - B") and q("it\u2019s", "it's")
    assert not q("the ceremony on March 16, 2027", RULES)                 # a changed word
    assert not q("the Academy ... the ceremony", RULES)                   # an elision
    assert not q("THE CEREMONY ON MARCH 15", RULES)                       # case is kept
    assert not q("", RULES) and not q("  . ", RULES) and not q(None, RULES)


def test_rules_sha1_ignores_whitespace_but_not_words():
    a = ai.rules_sha1("Resolves Yes if X.\n\nOtherwise No.")
    assert a == ai.rules_sha1("Resolves Yes  if X. Otherwise No. ")
    assert a != ai.rules_sha1("Resolves Yes if Y. Otherwise No.")
    assert ai.rules_text({"description": "  rules  ", "question": "q"}) == "rules"
    assert ai.rules_text({"description": "", "question": "q?"}) == "q?"


def test_mentions_date_forms_and_boundaries():
    from datetime import date
    d = date(2027, 3, 1)
    assert ai.mentions_date("on March 1, 2027", d)
    assert ai.mentions_date("on 1 March 2027", d) and ai.mentions_date("on Mar 1", d)
    assert ai.mentions_date("by 2027-03-01", d) and ai.mentions_date("by 3/1/2027", d)
    assert not ai.mentions_date("on March 15, 2027", d)                   # "March 1" inside "March 15"
    assert not ai.mentions_date("on 11 March 2027", d)                    # "1 March" inside "11 March"
    assert not ai.mentions_date("no date at all", d)
    dec9 = date(2026, 12, 9)
    assert ai.mentions_date("currently scheduled for December 8-9, 2026.", dec9)       # an FOMC meeting
    assert ai.mentions_date("on Dec. 8 \u2013 9", dec9) and ai.mentions_date("December 9 and 10", dec9)
    assert not ai.mentions_date("December 10-11, 2026", dec9) and not ai.mentions_date("November 8-9", dec9)
    assert ai.mentions_date("on Dec. 9, 2026", dec9) and ai.mentions_date("by Sept. 30", date(2026, 9, 30))


def test_mentions_date_checks_a_year_that_follows_the_day():
    from datetime import date
    jun30 = date(2026, 6, 30)
    assert not ai.mentions_date("aren't known by June 30, 2027 11:59 PM ET", jun30)
    assert ai.mentions_date("aren't known by June 30, 2027 11:59 PM ET", date(2027, 6, 30))
    assert ai.mentions_date("aren't known by June 30, 2027 11:59 PM ET", jun30, check_year=False)
    assert not ai.mentions_date("on June 30th, 2027", jun30) and ai.mentions_date("on June 30th, 2026", jun30)
    assert not ai.mentions_date("on 30 June 2027", jun30) and ai.mentions_date("on 30 June 2026.", jun30)
    assert ai.mentions_date("by June 30 11:59 PM ET", jun30)                # no year written: any year
    assert ai.mentions_date("June 30 2027 or June 30, 2026", jun30)         # a later mention can fit
    dec9 = date(2026, 12, 9)
    assert not ai.mentions_date("scheduled for December 8-9, 2027.", dec9)
    assert ai.mentions_date("scheduled for December 8-9, 2026.", dec9)


# ── validation ───────────────────────────────────────────────────────────────

def test_validate_a_good_scheduled_answer():
    lab = ai.validate(answer(1), RULES, to_epoch("2027-03-16T04:59:00Z"), today=TODAY)
    assert lab["valid"] and lab["quote_ok"] and lab["type_ok"] and not lab["problems"]
    assert (lab["type"], lab["reveal_kind"], lab["reveal_date"]) == ("predecided_reveal", "scheduled", "2027-03-15")
    assert lab["date_source"] == "quote" and not lab["excluded"]


def test_validate_quote_not_in_rules_makes_it_unclear():
    lab = ai.validate(answer(1, quote="the Oscars are on March 15"), RULES)
    assert not lab["quote_ok"] and not lab["valid"]
    assert lab["reveal_kind"] == "unclear" and lab["reveal_date"] is None
    assert lab["type"] == "predecided_reveal" and lab["type_ok"]          # the type is checked on its own
    assert lab["quote"] == "the Oscars are on March 15"                  # kept, so the miss can be read
    assert "quote is not in the rules" in lab["problems"]
    empty = ai.validate(answer(1, quote=""), RULES)
    assert empty["reveal_kind"] == "unclear" and "quote is empty" in empty["problems"]


def test_validate_type_and_kind_outside_the_lists():
    lab = ai.validate(answer(1, type_="sports_game", kind="soon"), RULES)
    assert lab["type"] == "other" and not lab["type_ok"] and not lab["valid"]
    assert lab["reveal_kind"] == "unclear" and lab["reveal_date"] is None
    assert ai.validate(answer(1, type_=" Court_Regulator "), RULES)["type"] == "court_regulator"


def test_validate_reveal_date_rules():
    v = ai.validate
    # scheduled without a date -> unclear
    lab = v(answer(1, date=None), RULES)
    assert lab["reveal_kind"] == "unclear" and lab["reveal_date"] is None
    # not YYYY-MM-DD -> dropped, and so unclear
    lab = v(answer(1, date="March 15, 2027"), RULES)
    assert lab["reveal_date"] is None and lab["reveal_kind"] == "unclear"
    assert any("not YYYY-MM-DD" in p for p in lab["problems"])
    assert v(answer(1, date="2027-02-30"), RULES)["reveal_date"] is None  # not a real day
    # any_day carries no date
    lab = v(answer(1, kind="any_day", date="2027-03-15"), RULES)
    assert lab["reveal_kind"] == "any_day" and lab["reveal_date"] is None
    assert v(answer(1, kind="any_day", date="null"), RULES)["problems"] == []
    # "2027-03-15T00:00:00Z" is read as its day
    assert v(answer(1, date="2027-03-15T00:00:00Z"), RULES)["reveal_date"] == "2027-03-15"


def test_validate_date_source_and_scheduled_needs_the_date_in_the_rules():
    end = to_epoch("2027-03-21T03:59:00Z")                                 # "March 20, 11:59 PM ET"
    rules = RULES + " Results are certified by March 18, 2027."
    q = "the winner at the ceremony"
    lab = ai.validate(answer(1, quote=q, date="2027-03-15"), rules, end, today=TODAY)
    assert (lab["reveal_kind"], lab["date_source"]) == ("scheduled", "rules")
    assert ai.validate(answer(1, date="2027-03-15"), rules, end, today=TODAY)["date_source"] == "quote"
    # only the end date, or nowhere: not evidence -> unclear, no date
    for d, why in (("2027-03-20", "end date"), ("2027-03-21", "end date"), ("2027-04-02", "do not write it")):
        lab = ai.validate(answer(1, quote=q, date=d), rules, end, today=TODAY)
        assert (lab["reveal_kind"], lab["reveal_date"], lab["date_source"]) == ("unclear", None, None), d
        assert any(why in p for p in lab["problems"]), lab["problems"]
        assert lab["valid"]                                               # the quote itself checked out


def test_validate_the_taiwan_fallback_clause():
    end = to_epoch("2026-11-29T04:59:00Z")
    # what flash-lite answered under prompt .2: the fallback's day, with the election's year
    bad = ai.validate(answer(1, type_="election_vote", date="2026-06-30", quote=TAIWAN_FALLBACK), TAIWAN, end,
                      today=TODAY)
    assert (bad["reveal_kind"], bad["reveal_date"], bad["date_source"]) == ("unclear", None, None)
    assert any("another year" in p for p in bad["problems"]) and bad["quote_ok"]
    good = ai.validate(answer(1, type_="election_vote", date="2026-11-28",
                              quote="Taiwanese local elections are scheduled to be held on November 28, 2026."),
                       TAIWAN, end, today=TODAY)
    assert (good["reveal_kind"], good["reveal_date"], good["date_source"]) == ("scheduled", "2026-11-28", "quote")
    # the fallback's own date, year and all: the year fits, the clause gives it away
    late = ai.validate(answer(1, date="2027-06-30", quote="aren\u2019t known by June 30, 2027 11:59 PM ET"), TAIWAN,
                       end, today=TODAY)
    assert (late["reveal_kind"], late["reveal_date"]) == ("unclear", None) and late["quote_ok"]
    assert any("fallback / extension clause" in p for p in late["problems"])


def test_fallback_clause_finds_the_sentence_that_holds_the_quote():
    f = ai.fallback_clause
    assert f("June 30, 2027 11:59 PM ET", TAIWAN).startswith("If the results of any of the relevant")
    assert f("scheduled to be held on November 28, 2026", TAIWAN) is None
    quake = ("This market will resolve according to the number of earthquakes between January 1 and December 31, "
             "2026, 11:59 PM ET.\n\nIf an earthquake has occurred but not yet appeared on the resolution source, "
             "this market may remain open until January 7, 2027, 11:59 PM ET.")
    assert f("open until January 7, 2027, 11:59 PM ET", quake) and f("December 31, 2026, 11:59 PM ET", quake) is None
    senate = ("This market will remain open until January 3, 2027, at which point it will resolve based on the first "
              "announcement of the next Senate Majority Leader.")
    assert f("until January 3, 2027", senate)
    spotify = ('If Spotify does not release who their top artist is by January 31, 2027, 11:59 PM ET, this market '
               'will resolve to "Other".')
    assert f("by January 31, 2027, 11:59 PM ET", spotify)
    assert f("this market will resolve to No by December 31", "Otherwise, this market will resolve to No by December 31.")
    # an "If" sentence that is not about lateness, and a paragraph that ends without a full stop
    fed = ("If the rate is changed to a level not expressed in the options, it is rounded up. (e.g. a cut of 12.5 bps "
           "is 25 bps)\n\nThe resolution source is the FOMC's statement after its meeting scheduled for December 8-9, "
           "2026 according to the official calendar.")
    assert f("scheduled for December 8-9, 2026", fed) is None
    assert f("if Kraken completes an IPO by the listed date", 'Resolves "Yes" if Kraken completes an IPO by the listed '
             'date. Otherwise "No".') is None
    assert f("not in these rules at all", TAIWAN) is None and f("", TAIWAN) is None


def test_a_date_the_rules_write_only_in_a_fallback_clause():
    from datetime import date
    box = ('This market will resolve according to the film with the highest 2026 gross once data for December 31 is '
           'made available.\n\nIf there is no final data available by January 7, 2027, 11:59 PM ET, another credible '
           'resolution source will be chosen.')
    assert ai.date_only_in_fallback(date(2027, 1, 7), box).startswith("If there is no final data")
    assert ai.date_only_in_fallback(date(2026, 12, 31), box) is None             # named in the schedule sentence
    assert ai.date_only_in_fallback(date(2026, 11, 28), TAIWAN) is None
    assert ai.date_only_in_fallback(date(2027, 3, 1), box) is None               # named nowhere
    lab = ai.validate(answer(1, type_="data_release", date="2027-01-07",
                             quote="once data for December 31 is made available."), box, today=TODAY)
    assert (lab["reveal_kind"], lab["reveal_date"], lab["date_source"]) == ("unclear", None, None)
    assert any("the date 2027-01-07 comes from a fallback" in p for p in lab["problems"])
    ok = ai.validate(answer(1, type_="data_release", date="2026-12-31",
                            quote="once data for December 31 is made available."), box, today=TODAY)
    assert (ok["reveal_kind"], ok["reveal_date"], ok["date_source"]) == ("scheduled", "2026-12-31", "quote")


def test_validate_a_past_reveal_is_awaiting_resolution():
    rules = "The midterm elections will take place on November 3, 2026. Results are official once certified."
    a = answer(1, type_="election_vote", date="2026-11-03", quote="The midterm elections will take place on November 3")
    lab = ai.validate(a, rules, today="2026-11-10")
    assert lab["reveal_kind"] == ai.REVEALED == "revealed_awaiting_resolution"
    assert lab["reveal_date"] == "2026-11-03" and lab["date_source"] == "quote" and lab["valid"]
    assert ai.validate(a, rules, today="2026-11-03")["reveal_kind"] == "scheduled"     # today is not past
    assert ai.validate(a, rules, today="2026-10-01")["reveal_kind"] == "scheduled"
    from datetime import date, datetime, timezone
    assert ai.validate(a, rules, today=date(2026, 11, 4))["reveal_kind"] == ai.REVEALED
    assert ai.validate(a, rules, today=datetime(2026, 11, 4, 1, tzinfo=timezone.utc))["reveal_kind"] == ai.REVEALED
    assert ai.REVEALED not in ai.REVEAL_KINDS and ai.REVEALED in ai.LABEL_REVEAL_KINDS  # never asked of the model


def test_validate_a_quote_too_short_to_count():
    rules = "Resolves Yes if X happens by December 31, 2026. The election is on Nov 3 in 2026."
    for q in ("2026", "Nov 3", "December", "31, 2026"):
        lab = ai.validate(answer(1, kind="any_day", date=None, quote=q), rules)
        assert not lab["quote_ok"] and not lab["valid"] and lab["reveal_kind"] == "unclear", q
        assert any("too short" in p for p in lab["problems"]) and "quote is not in the rules" not in lab["problems"]
    for q in ("by December 31, 2026", "December 31, 2026", "on Nov 3", "X happens by"):
        assert ai.validate(answer(1, kind="any_day", date=None, quote=q), rules)["quote_ok"], q
    assert ai.quote_long_enough("by Dec 31") and ai.quote_long_enough("31 December 2026")
    assert not ai.quote_long_enough("  2026. ") and not ai.quote_long_enough("- - - -")
    both = ai.validate(answer(1, quote="Nov 4"), rules)                    # short and not in the rules
    assert "quote is not in the rules" in both["problems"] and any("too short" in p for p in both["problems"])


def test_excluded_types():
    for t in ("game_sport", "price_level", "weather"):
        lab = ai.validate(answer(1, type_=t, kind="any_day", date=None, quote="the official broadcast"), RULES)
        assert lab["excluded"] and ai.is_excluded(lab)
    assert not ai.is_excluded(ai.validate(answer(1, type_="other"), RULES))
    assert not ai.is_excluded(None)
    assert set(ai.EXCLUDED_TYPES) <= set(ai.TYPES) and len(ai.TYPES) == 12


def test_schema_enums_match_the_lists():
    item = ai.RESPONSE_SCHEMA["properties"]["labels"]["items"]["properties"]
    assert item["type"]["enum"] == list(ai.TYPES) and item["reveal_kind"]["enum"] == list(ai.REVEAL_KINDS)


# ── prompt and batching ──────────────────────────────────────────────────────

def test_build_prompt_carries_each_market():
    items = [ai._item(market("11", event={"id": "e1", "title": "Best Picture 2027"})),
             ai._item(market("12", rules="Other rules.", q="Q2?"))]
    p = ai.build_prompt(items, today="2026-11-10")
    assert "### Market id=11" in p and "### Market id=12" in p and "Event: Best Picture 2027" in p
    assert RULES in p and "Other rules." in p and "Today is 2026-11-10." in p
    assert "End date (UTC): 2027-03-16T04:59:00+00:00" in p
    for t in ai.TYPES:
        assert f"- {t}:" in p


def test_prompt_on_fallback_clauses_valuations_and_version():
    p = ai.INSTRUCTIONS
    assert "Fallback and extension clauses are NOT the schedule" in p and "aren't known by June 30, 2027" in p
    assert "otherwise this market resolves No by" in p and "may remain open until" in p
    assert "expected to be announced or to happen" in p and "even if it is before today" in p
    price = next(line for line in p.splitlines() if line.startswith("- price_level:"))
    assert "Nasdaq Private Market" in price and "funding round" in price   # NPM valuation ladders: price_level
    assert "at least three words" in p and "never move a date to another year" in p
    assert "Never copy from the question" in p and "the listed date" in p
    assert ai.PROMPT_VERSION > "2026-11-10.2"                              # labels from the older prompt are re-asked


def test_batches_respect_size_and_chars():
    items = [{"id": str(i), "rules": "x" * 100} for i in range(7)]
    assert [len(b) for b in ai.batches(items, size=3, max_chars=10_000)] == [3, 3, 1]
    assert [len(b) for b in ai.batches(items, size=10, max_chars=250)] == [2, 2, 2, 1]
    big = [{"id": "a", "rules": "x" * 50}, {"id": "b", "rules": "x" * 999}, {"id": "c", "rules": "x"}]
    assert [[i["id"] for i in b] for b in ai.batches(big, size=10, max_chars=500)] == [["a"], ["b"], ["c"]]
    assert ai.batches([], size=3) == []


# ── client ───────────────────────────────────────────────────────────────────

def test_client_sends_the_key_in_a_header_and_the_schema():
    with fake_key():
        c = client([batch_reply([])])
        text, model, payload = c.generate("prompt", schema=ai.RESPONSE_SCHEMA)
        call = c.session.calls[0]
    assert json.loads(text) == {"labels": []} and model == "gemini-3.5-flash-lite"
    assert call["url"] == ai.URL and FAKE_KEY not in call["url"] and not call["params"]
    assert call["headers"] == {"x-goog-api-key": FAKE_KEY} and call["timeout"] == 120
    b = call["json"]
    assert b["model"] == ai.MODELS[0] and b["input"] == "prompt" and b["store"] is False
    assert b["response_mime_type"] == "application/json" and b["response_format"] is ai.RESPONSE_SCHEMA
    assert b["generation_config"] == {"temperature": 0.0}


def test_client_retries_503_and_429_then_answers():
    with fake_key():
        c = client([FakeResp(503, {"error": {"message": "high demand"}}),
                    FakeResp(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "2"}),
                    batch_reply([])])
        c.generate("p")
    assert c.stats["requests"] == 3 and c.stats["retries"] == 2 and c.stats["answered"] == {ai.MODELS[0]: 1}
    assert {x["json"]["model"] for x in c.session.calls} == {ai.MODELS[0]}


def test_client_falls_back_on_timeout_quota_and_missing_model():
    with fake_key():
        c = client([requests.Timeout("slow"),
                    FakeResp(429, {"error": {"message": "Quota exceeded: requests per day"}}),
                    FakeResp(404, {"error": {"message": "model not found"}}),
                    batch_reply([], model=ai.MODELS[3])])
        _, model, _ = c.generate("p")
    assert [x["json"]["model"] for x in c.session.calls] == list(ai.MODELS[:4])
    assert model == ai.MODELS[3] and c.stats["timeouts"] == 1 and c.stats["fallbacks"] == 3
    assert c.skip == {ai.MODELS[1], ai.MODELS[2]}                          # quota / 404 models skipped from now on
    with fake_key():
        c.session.script = [batch_reply([])]
        c.generate("again")
    assert c.session.calls[-1]["json"]["model"] == ai.MODELS[0]           # a timeout does not skip the model


def test_client_plain_400_is_raised_at_once_and_skips_no_model():
    with fake_key():
        c = client([FakeResp(400, {"error": {"message": "Request contains an invalid argument."}}), batch_reply([])])
        e = raises(lambda: c.generate("p"), ai.GeminiError)
        assert e.refused and e.status == 400 and len(c.session.calls) == 1   # no fallback model tried
        assert c.skip == set() and c.stats["refused"] == 1 and c.stats["fallbacks"] == 0
        c.generate("next batch")
    assert c.session.calls[-1]["json"]["model"] == ai.MODELS[0]           # the model is still used
    with fake_key():
        c = client([FakeResp(413, {"error": {"message": "payload too large"}})])
        assert raises(lambda: c.generate("p"), ai.GeminiError).refused and c.skip == set()


def test_client_skips_a_model_only_when_it_is_gone_or_out_of_quota():
    gone = ("models/gemini-3.5-flash-lite is not found for API version v1beta, or is not supported for this method.",
            "Unknown model: gemini-3.5-flash-lite", "model gemini-3.5-flash-lite does not exist")
    for msg in gone:
        with fake_key():
            c = client([FakeResp(400, {"error": {"message": msg}}), batch_reply([], model=ai.MODELS[1])])
            _, model, _ = c.generate("p")
        assert model == ai.MODELS[1] and c.skip == {ai.MODELS[0]} and c.stats["refused"] == 0, msg
    with fake_key():
        c = client([FakeResp(429, {"error": {"message": "Quota exceeded: requests per minute. Please retry in 3s"}}),
                    FakeResp(408, {"error": {"message": "request timeout"}}), batch_reply([])])
        _, model, _ = c.generate("p")
    assert model == ai.MODELS[0] and c.skip == set() and c.stats["retries"] == 2   # per-minute 429 / 408: retried


def test_client_default_spacing_fits_the_free_tier():
    assert ai.MIN_INTERVAL == 4.2 and ai.Client(session=FakeSession([])).min_interval == 4.2
    assert 60.0 / ai.MIN_INTERVAL <= 15                                    # 15 requests a minute on flash-lite


def test_retry_wait_reads_header_details_and_message():
    hdr = FakeResp(429, {"error": {"message": "x"}}, headers={"Retry-After": "7"})
    assert ai._retry_after(hdr, 0) == 7.0
    det = FakeResp(429, {"error": {"message": "x", "details": [{"@type": "RetryInfo", "retryDelay": "12s"}]}})
    assert ai._retry_after(det, 0) == 12.0
    msg = FakeResp(429, {"error": {"message": "limit: 15 requests per minute on Free Tier. Please retry in 19s or"}})
    assert ai._retry_after(msg, 0) == 20.0
    assert 2.0 <= ai._retry_after(FakeResp(503, {"error": {"message": "high demand"}}), 0) < 3.0
    assert ai._retry_after(FakeResp(503, None, text="x"), 9) == ai.MAX_BACKOFF


def test_client_spaces_request_starts():
    waits = []
    c = ai.Client(session=FakeSession([]), sleep=waits.append, min_interval=4.2)
    for _ in range(3):
        c._space()
    assert len(waits) == 2 and all(3.5 < w <= 8.4 for w in waits)


def test_client_unusable_200_is_retried():
    with fake_key():
        c = client([FakeResp(200, {"status": "completed", "steps": [{"type": "thought"}]}),
                    FakeResp(200, None, text="<html>"), batch_reply([])])
        c.generate("p")
    assert c.stats["requests"] == 3 and c.stats["retries"] == 2


def test_client_gives_up_and_never_leaks_the_key():
    with fake_key():
        c = client(lambda body: FakeResp(500, {"error": {"message": f"boom key={FAKE_KEY}"}}), retries=2)
        e = raises(lambda: c.generate("p"), ai.GeminiError)
        assert FAKE_KEY not in str(e) and "<key>" in str(e)
        assert all(FAKE_KEY not in m for m in c.stats["errors"])
        assert c.stats["requests"] == 2 * len(ai.MODELS)
        c401 = client([FakeResp(401, {"error": {"message": "API key not valid"}}), batch_reply([])])
        raises(lambda: c401.generate("p"), ai.GeminiError)
        assert len(c401.session.calls) == 1                               # a refused key stops at once


def test_api_key_from_environment_or_env_file():
    old_env = os.environ.pop("GEMINI_API_KEY", None)
    old_file, old_key = ai.ENV_FILE, dict(ai._KEY)
    try:
        ai._KEY.clear()
        with tempfile.TemporaryDirectory() as d:
            ai.ENV_FILE = os.path.join(d, ".env")
            with open(ai.ENV_FILE, "w") as f:
                f.write("# comment\nOTHER=1\nGEMINI_API_KEY=\"from-file\"\n")
            assert ai.api_key() == "from-file"
            ai._KEY.clear()
            os.environ["GEMINI_API_KEY"] = "from-env"
            assert ai.api_key() == "from-env"
            ai._KEY.clear()
            del os.environ["GEMINI_API_KEY"]
            os.remove(ai.ENV_FILE)
            raises(ai.api_key, ai.GeminiError)
    finally:
        ai.ENV_FILE = old_file
        ai._KEY.clear()
        ai._KEY.update(old_key)
        if old_env is not None:
            os.environ["GEMINI_API_KEY"] = old_env
        else:
            os.environ.pop("GEMINI_API_KEY", None)


# ── label_markets ────────────────────────────────────────────────────────────

def replier(per_market=None, skip=()):
    """A session script that answers every market id found in the prompt."""
    import re

    def reply(body):
        ids = re.findall(r"### Market id=(\S+)", body["input"])
        out = [(per_market or {}).get(i) or answer(i) for i in ids if i not in skip]
        return batch_reply(out)
    return reply


def test_label_markets_caches_by_id_and_rules():
    conn = store.connect(":memory:")
    ms = [market("1"), market("2"), market("3", rules="Resolves at the official broadcast. " + RULES)]
    with fake_key():
        c = client(replier())
        got = ai.label_markets(ms, conn=conn, client=c, batch_size=2, workers=1, today="2026-11-10")
        assert set(got) == {"1", "2", "3"} and len(c.session.calls) == 2
        assert all(not lab["cached"] and lab["valid"] for lab in got.values())
        row = store.get_ai_label(conn, "1", ai.rules_sha1(RULES))
        assert row["type"] == "predecided_reveal" and row["valid"] == 1 and row["model"] == ai.MODELS[0]
        assert json.loads(row["raw"])["prompt_version"] == ai.PROMPT_VERSION
        # same rules: no call; changed rules: a new call for that market only
        again = ai.label_markets(ms, conn=conn, client=c, workers=1)
        assert len(c.session.calls) == 2 and all(lab["cached"] for lab in again.values())
        assert again["1"]["reveal_date"] == "2027-03-15" and again["1"]["date_source"] == "quote"
        ms[1] = market("2", rules=RULES + " Amended.")
        ai.label_markets(ms, conn=conn, client=c, workers=1)
        assert len(c.session.calls) == 3 and c.session.calls[-1]["json"]["input"].count("### Market id=") == 1
        # refresh asks everything again
        ai.label_markets(ms, conn=conn, client=c, batch_size=10, workers=1, refresh=True)
        assert len(c.session.calls) == 4


def test_label_markets_save_false_and_prompt_version():
    conn = store.connect(":memory:")
    with fake_key():
        c = client(replier())
        ai.label_markets([market("1")], conn=conn, client=c, save=False)
        assert store.get_ai_label(conn, "1", ai.rules_sha1(RULES)) is None
        ai.label_markets([market("1")], conn=conn, client=c)
        old = ai.PROMPT_VERSION
        try:
            ai.PROMPT_VERSION = "older"
            ai.label_markets([market("1")], conn=conn, client=c)             # stale cached label -> asked again
            assert len(c.session.calls) == 3
        finally:
            ai.PROMPT_VERSION = old


def test_label_markets_reasks_ids_the_answer_left_out_and_validates_each():
    conn = store.connect(":memory:")
    bad = answer("2", quote="words the model made up")
    calls = []

    def reply(body):
        calls.append(body["input"].count("### Market id="))
        skip = ("3",) if len(calls) == 1 else ()
        return replier({"2": bad}, skip)(body)

    with fake_key():
        c = client(reply)
        got = ai.label_markets([market("1"), market("2"), market("3"), market("1")], conn=conn, client=c, workers=1)
    assert calls == [3, 1]                                                  # duplicates dropped; "3" re-asked alone
    assert got["1"]["valid"] and got["3"]["valid"]
    assert not got["2"]["valid"] and got["2"]["reveal_kind"] == "unclear" and got["2"]["reveal_date"] is None
    assert store.get_ai_label(conn, "2", ai.rules_sha1(RULES))["valid"] == 0


def test_label_markets_failed_calls_are_left_out_not_cached():
    conn = store.connect(":memory:")
    with fake_key():
        c = client(lambda body: FakeResp(503, {"error": {"message": "overloaded"}}), retries=1)
        got = ai.label_markets([market("1"), market("2")], conn=conn, client=c, workers=1)
    assert got == {} and store.get_ai_label(conn, "1", ai.rules_sha1(RULES)) is None
    assert c.stats["requests"] == 2 * len(ai.MODELS)                       # the batch was tried twice down the chain


def test_label_markets_uses_the_given_event_and_budget():
    from watchlist.common import Budget
    conn = store.connect(":memory:")
    with fake_key():
        c = client(replier())
        ai.label_markets([market("1")], conn=conn, client=c, events={"1": {"id": "e9", "title": "Event nine"}})
        assert "Event: Event nine" in c.session.calls[0]["json"]["input"]
        got = ai.label_markets([market("5")], conn=conn, client=c, budget=Budget(0))
    assert got == {} and len(c.session.calls) == 1


def test_label_markets_splits_a_refused_batch_once():
    import re
    conn = store.connect(":memory:")
    calls = []

    def reply(body):
        ids = re.findall(r"### Market id=(\S+)", body["input"])
        calls.append(ids)
        if len(calls) == 1 or "3" in ids:
            return FakeResp(400, {"error": {"message": "Request contains an invalid argument."}})
        return replier()(body)

    ms = [market(str(i)) for i in range(1, 5)]
    with fake_key():
        c = client(reply)
        got = ai.label_markets(ms, conn=conn, client=c, workers=1, today=TODAY)
    assert calls == [["1", "2", "3", "4"], ["1", "2"], ["3", "4"]]         # halves asked once, not split again
    assert set(got) == {"1", "2"} and c.skip == set() and c.stats["refused"] == 2
    assert store.get_ai_label(conn, "3", ai.rules_sha1(RULES)) is None      # not cached: asked again next run
    groups = [{"event_id": "e", "event": {"id": "e"}, "markets": ms}]
    kept, excluded = ai.apply_exclusions(groups, got)
    assert [m["id"] for m in kept[0]["markets"]] == ["1", "2", "3", "4"] and excluded == []
    # a one-market batch that is refused is not asked again
    with fake_key():
        c = client([FakeResp(400, {"error": {"message": "Request contains an invalid argument."}}), batch_reply([])])
        assert ai.label_markets([market("9")], conn=conn, client=c, workers=1) == {}
    assert len(c.session.calls) == 1


def test_label_markets_asks_a_failed_batch_once_more():
    conn = store.connect(":memory:")
    calls = []

    def reply(body):
        calls.append(body)
        if len(calls) == 1:
            return FakeResp(200, interaction("Sorry, I cannot answer in JSON."))   # not JSON: ValueError
        return replier()(body)

    with fake_key():
        c = client(reply)
        got = ai.label_markets([market("1")], conn=conn, client=c, workers=1, today=TODAY)
    assert set(got) == {"1"} and len(calls) == 2 and got["1"]["reveal_kind"] == "scheduled"


def test_cached_labels_are_revalidated_with_the_day():
    conn = store.connect(":memory:")
    with fake_key():
        c = client(replier())
        first = ai.label_markets([market("1")], conn=conn, client=c, workers=1, today="2026-11-10")
        later = ai.label_markets([market("1")], conn=conn, client=c, workers=1, today="2027-03-16")
    assert first["1"]["reveal_kind"] == "scheduled" and len(c.session.calls) == 1
    assert later["1"]["cached"] and later["1"]["reveal_kind"] == ai.REVEALED and later["1"]["reveal_date"] == "2027-03-15"


def test_label_markets_raises_on_a_refused_key():
    with fake_key():
        c = client([FakeResp(403, {"error": {"message": "permission denied"}})])
        e = raises(lambda: ai.label_markets([market("1"), market("2")], conn=store.connect(":memory:"), client=c),
                   ai.GeminiError)
    assert e.status == 403 and len(c.session.calls) == 1


def test_label_pool_and_apply_exclusions():
    conn = store.connect(":memory:")
    ev1, ev2 = {"id": "e1", "title": "Gold ladder"}, {"id": "e2", "title": "Ceasefire"}
    gold = market("1", rules=RULES, q="Will gold hit $9k?")
    gold2 = market("2", rules=RULES, q="Will gold hit $10k?")
    cease = market("3", rules=RULES, q="Ceasefire by December 31?")
    unl = market("4", rules=RULES, q="Unlabelled?")
    groups = [{"event_id": "e1", "event": ev1, "markets": [gold, gold2]},
              {"event_id": "e2", "event": ev2, "markets": [cease, unl]}]
    price = answer("x", type_="price_level", kind="any_day", date=None, quote="the official broadcast")
    with fake_key():
        c = client(replier({"1": {**price, "id": "1"}, "2": {**price, "id": "2"}}, skip=("4",)), retries=1)
        labels = ai.label_pool(groups, conn=conn, client=c, workers=1)
    assert "Event: Gold ladder" in c.session.calls[0]["json"]["input"]
    assert set(labels) == {"1", "2", "3"}                                   # "4" was never answered
    kept, excluded = ai.apply_exclusions(groups, labels)
    assert [g["event_id"] for g in kept] == ["e2"] and [m["id"] for m in kept[0]["markets"]] == ["3", "4"]
    assert [(m["id"], lab["type"]) for m, lab in excluded] == [("1", "price_level"), ("2", "price_level")]
    assert len(groups[0]["markets"]) == 2                                   # the input groups are not changed


def test_agreement_counts():
    a = {"1": ai.validate(answer(1), RULES), "2": ai.validate(answer(2), RULES)}
    b = {"1": ai.validate(answer(1), RULES), "2": ai.validate(answer(2, type_="other"), RULES), "3": a["1"]}
    r = ai._agreement(a, b)
    assert r["n"] == 2 and r["agree"]["type"] == 1 and r["agree"]["reveal_date"] == 2
    assert r["agree_pct"]["type"] == 50.0 and [d["market_id"] for d in r["disagreements"]] == ["2"]


def no_network(*a, **k):
    raise AssertionError("network call in an offline test")


if __name__ == "__main__":
    requests.Session.post = no_network       # belt and braces: the fakes never get here
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
