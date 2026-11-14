"""
The AI labeller: one Gemini answer per open market, read off its rules.

Keywords catch most games, price markets and weather, but not all ("What
will Gold (GC) hit by end of December?", a valuation ladder settled by a
secondary-market price index). They also cannot say WHEN a market's answer
becomes known, which the open-list ranking needs (a scheduled reveal within
14 days goes first on ties). So every pool market gets a label:

  type         what decides the outcome, from the fixed list TYPES.
               game_sport / price_level / weather are excluded from the pool
               (EXCLUDED_TYPES): insider knowledge cannot explain them.
  reveal_kind  scheduled (the answer comes out at a set moment that has not
               passed yet), any_day (whenever it happens, up to a deadline),
               unclear, or revealed_awaiting_resolution (scheduled, but the
               date is before today: the election or meeting has happened
               and the market waits for its result to be made official).
               The model answers only the first three (REVEAL_KINDS); the
               fourth is set by validate() from the date.
  reveal_date  YYYY-MM-DD of a scheduled (or past scheduled) reveal, else null.
  quote        the words of the rules that show when the answer comes out,
               copied exactly.

Valuation ladders ("Will Anthropic's valuation hit $2T by December 31?")
are price_level, so excluded, for now: their rules resolve on the NPM Price
that Nasdaq Private Market publishes from secondary-market trading on any
day, not on a funding round. filters.py's keyword layer keeps them (its
docstring gives the funding-round reading); which reading wins is the
user's decision, still open. Changing it means editing the price_level line
of INSTRUCTIONS and bumping PROMPT_VERSION.

The quote is the evidence that the model read THIS market's rules rather
than guessing from the question. validate() checks, per market:
  - the quote is at least 3 words or 15 characters (a bare "2026" or
    "June 30" proves nothing) and a substring of the rules after
    normalise() (whitespace collapsed, typographic quotes/dashes folded to
    ASCII, NFKC); otherwise reveal_kind "unclear" with no date. The type is
    kept (it is checked against the list on its own).
  - a scheduled date must be written in the quote or elsewhere in the rules
    (date_source "quote" / "rules"); a date found only as the market's end
    date or nowhere becomes "unclear". mentions_date() reads a year that
    follows the day ("June 30, 2027") and refuses it when it differs from
    the date's year: the model once turned a fallback clause ("if the
    results aren't known by June 30, 2027 ...") into 2026-06-30 for an
    election held on November 28, 2026.
  - a scheduled answer whose quote sits in a fallback / extension clause
    (a sentence starting "If ..." about something missing or late, one
    starting "Otherwise", or one that keeps the market "open until" a date),
    or whose date the rules write only in such clauses, becomes "unclear":
    the prompt says such clauses are not the schedule, and
    fallback_clause() / date_only_in_fallback() check that the model listened.
  - a scheduled date before today becomes revealed_awaiting_resolution. The answers are strict JSON (the request carries a JSON
schema with the two enums), parsed and validated per market even when
several markets share one call.

Gemini (verified 2026-11-10, see ../memory note on the key):
  POST https://generativelanguage.googleapis.com/v1beta/interactions with
  {"model", "input", "response_mime_type": "application/json",
  "response_format": <JSON schema>, "generation_config": {"temperature"},
  "store": false} and the key in the x-goog-api-key header (never in the
  URL, so it cannot leak into an exception message or a log line). The
  answer is {"status": "completed", "model", "usage", "steps": [{"type":
  "thought", "signature"}, {"type": "model_output", "content": [{"type":
  "text", "text": "<the JSON>"}]}]}; output_text() reads the model_output
  step(s). Unknown request fields are a 400 ("Unknown parameter …"), so
  every field above is really applied. response_mime_type without
  response_format is a 400. generateContent 404s for this key.
  gemini-3.5-flash-lite answers a 10-market batch in a few seconds;
  gemini-flash-lite-latest is an alias of it. The free tier allows it 15
  requests a minute, hence MIN_INTERVAL (4.2 s between request starts,
  across threads). The bigger models have timed out at 60 s before, hence
  the 120 s timeout and the fallback chain MODELS.
  Errors: 429 / 408 / 5xx are retried with backoff (Retry-After honoured);
  a timeout moves this request to the next model. A model is skipped for
  the rest of the process only when it is gone (404, or a 400 that says the
  model is not found / not supported) or its daily quota is spent (a 429
  that says per day). Any other 4xx (a plain 400) is about the request, not
  the model: generate() raises at once without skipping or falling back,
  and _run_batch() splits the batch in two and asks each half once. 401 /
  403 (key refused) stops the run.

Cache: the store's ai_labels table, keyed by (market id, sha1 of the
normalised rules). A changed rules text gets a new label; an unchanged one
is never sent again. The raw column keeps the model's own answer plus the
prompt version, and labels are re-validated from it on every read (with
that day's date, so a reveal that has passed turns into
revealed_awaiting_resolution without a call); a change to the validation
costs no calls; a change to PROMPT_VERSION relabels.

Public API:
  label_markets(markets, conn=None, events=None, refresh=False, client=None,
                batch_size=BATCH_SIZE, workers=WORKERS, budget=None, today=None)
      -> {market_id: label}
  label_pool(groups, **kw)          label_markets for filters.open_pool groups
  apply_exclusions(groups, labels)  -> (kept_groups, [(market, label)])
  is_excluded(label) / EXCLUDED_TYPES / TYPES / REVEAL_KINDS / LABEL_REVEAL_KINDS
  Client(models=MODELS).generate(prompt, schema=None) -> (text, model, payload)
  validate(answer, rules, end_ts, today) / normalise / quote_in_rules /
  quote_long_enough / mentions_date / fallback_clause / date_only_in_fallback /
  rules_text / rules_sha1 / output_text / parse_json / build_prompt

CLI (the demo, output 6):
  python3 -m watchlist.ai --demo [--budget-seconds 480] [--recheck 30]
  builds today's open pool, labels every market (resumable: cached labels
  are reused), re-asks 30 of them with fresh calls and writes
  data/watchlist/demo/ai_labels.json.
"""
import hashlib
import json
import os
import random
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from watchlist import filters, store
from watchlist.common import DEMO_DIR, ROOT, Budget, iso, log, now_epoch, to_epoch

URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
# First answer wins. flash-lite first (fast, ~1-5 s); the others are the
# fallbacks the key's ListModels showed, cheapest first.
MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-latest",
          "gemini-3.8-flash")
TIMEOUT = 120
RETRIES_PER_MODEL = 4       # attempts on one model for 429 / 5xx / network errors
MAX_BACKOFF = 60.0
MIN_INTERVAL = 4.2          # seconds between request starts, across threads: the free tier allows
                            # 15 requests a minute on gemini-3.5-flash-lite (its 429 says so)
TEMPERATURE = 0.0

TYPES = ("military_gov_action", "person_leaves_role", "company_product", "predecided_reveal", "court_regulator",
         "data_release", "person_behavior", "election_vote", "game_sport", "price_level", "weather", "other")
REVEAL_KINDS = ("scheduled", "any_day", "unclear")     # what the model may answer (the schema's enum)
REVEALED = "revealed_awaiting_resolution"             # set by validate(): a scheduled date before today
LABEL_REVEAL_KINDS = REVEAL_KINDS + (REVEALED,)       # what a validated label may carry
EXCLUDED_TYPES = frozenset({"game_sport", "price_level", "weather"})
MIN_QUOTE_WORDS = 3         # a quote counts when it has this many words ...
MIN_QUOTE_CHARS = 15        # ... or this many characters

# .1 called deadlines "scheduled"; .2 says a deadline is not a schedule; .3 says fallback / extension
# clauses are not the schedule either, and names valuation ladders read off a secondary-market price;
# .4 forbids taking the quote's date from the question (.3's "date inside the quote" made the model copy
# "by December 31, 2026" from the question when the rules say "the listed date": 52 quotes missed);
# .5 says counts of earthquakes / eruptions are weather (.4 moved 6 of them to data_release)
PROMPT_VERSION = "2026-11-10.5"
BATCH_SIZE = 15             # markets per call (a 10-market call took ~3 s on flash-lite)
MAX_BATCH_CHARS = 24_000    # rules text per call (median rules ~900 chars, max ~6k)
WORKERS = 2

ENV_FILE = os.path.join(os.path.dirname(ROOT), ".env")
DEMO_PATH = os.path.join(DEMO_DIR, "ai_labels.json")

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": list(TYPES)},
                    "reveal_kind": {"type": "string", "enum": list(REVEAL_KINDS)},
                    "reveal_date": {"type": ["string", "null"]},
                    "quote": {"type": "string"},
                },
                "required": ["id", "type", "reveal_kind", "reveal_date", "quote"],
            },
        },
    },
    "required": ["labels"],
}


class GeminiError(RuntimeError):
    """A Gemini call that failed (message never carries the key).
    refused: the request itself was rejected (a plain 400 or other 4xx that
    is not about the key, the model or a rate limit), so asking the same
    request of another model would not help; _run_batch splits the batch."""

    def __init__(self, message, status=None, model=None, refused=False):
        super().__init__(_scrub(message))
        self.status = status
        self.model = model
        self.refused = refused


# ── key ──────────────────────────────────────────────────────────────────────

_KEY = {}


def api_key():
    """GEMINI_API_KEY from the environment, else from ../.env (the repo's
    parent). Read at call time; never logged, printed or stored."""
    if "key" in _KEY:
        return _KEY["key"]
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key and os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith("GEMINI_API_KEY="):
                    key = line.split("=", 1)[1].split(" #", 1)[0].strip().strip('"').strip("'")
    if not key:
        raise GeminiError(f"GEMINI_API_KEY is not set (environment or {ENV_FILE})")
    _KEY["key"] = key
    return key


def _scrub(text):
    s = str(text)
    k = _KEY.get("key")
    return s.replace(k, "<key>") if k else s


# ── response parsing ─────────────────────────────────────────────────────────

def output_text(payload):
    """The answer text of an Interactions response: the text parts of its
    model_output step(s), joined. Thought steps carry only a signature and
    are skipped. Raises GeminiError when the interaction did not complete or
    carries no output text (an older `outputs` list is read too)."""
    if not isinstance(payload, dict):
        raise GeminiError(f"interaction: not a JSON object: {str(payload)[:200]}")
    if payload.get("error"):
        raise GeminiError(f"interaction error: {str(payload['error'])[:300]}")
    status = payload.get("status")
    if status not in (None, "completed"):
        raise GeminiError(f"interaction status {status!r}, not completed")
    parts = []
    for step in payload.get("steps") or []:
        if isinstance(step, dict) and step.get("type") == "model_output":
            for c in step.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                    parts.append(c["text"])
    if not parts:
        for c in payload.get("outputs") or []:
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
    text = "".join(parts).strip()
    if not text:
        kinds = [s.get("type") for s in payload.get("steps") or [] if isinstance(s, dict)]
        raise GeminiError(f"interaction carries no output text (steps: {kinds})")
    return text


_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?(.*?)\n?```\s*$", re.S)


def parse_json(text):
    """Decode a model's JSON answer: plain JSON, a ```json fenced block, or
    the outermost {...} / [...] inside other text. Raises ValueError."""
    s = str(text or "").strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        i, j = s.find(open_), s.rfind(close)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                continue
    raise ValueError(f"not JSON: {s[:200]!r}")


# ── client ───────────────────────────────────────────────────────────────────

def _retry_after(resp, attempt):
    wait = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
    if re.fullmatch(r"\d+(\.\d+)?", wait):
        return min(MAX_BACKOFF, float(wait))
    try:  # google.rpc.RetryInfo: {"error": {"details": [{"retryDelay": "17s"}]}}
        for d in (resp.json().get("error") or {}).get("details") or []:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(d.get("retryDelay") or ""))
            if m:
                return min(MAX_BACKOFF, float(m.group(1)))
    except (ValueError, AttributeError):
        pass
    m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", _error_text(resp) if resp is not None else "", re.I)
    if m:                                       # "… Please retry in 19s …"
        return min(MAX_BACKOFF, float(m.group(1)) + 1.0)
    return min(MAX_BACKOFF, 2.0 * 2 ** attempt + random.random())


# A 400 that is about the model rather than the request ("models/x is not
# found for API version v1beta", "model ... is not supported", "unknown model").
_MODEL_GONE = re.compile(r"\bmodels?\b.{0,80}?\b(?:not found|not supported|unsupported|does not exist|"
                         r"is not available|no longer available)\b|\b(?:unknown|invalid|unsupported) model\b",
                         re.I | re.S)
_DAILY_QUOTA = re.compile(r"per ?day|daily", re.I)


def _error_text(resp):
    try:
        j = resp.json()
    except ValueError:
        return resp.text[:300]
    err = j.get("error") if isinstance(j, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err or j)[:300]


class Client:
    """Gemini Interactions client with a model fallback chain, retries and
    a shared request spacing (min_interval between request starts, across
    the threads using this client). `stats` counts what happened (for run
    reports): requests (HTTP attempts), answered (by model), retries,
    timeouts, fallbacks, refused (requests a plain 4xx turned down), errors
    (last few), seconds spent waiting on answers."""

    def __init__(self, models=MODELS, timeout=TIMEOUT, retries=RETRIES_PER_MODEL, min_interval=MIN_INTERVAL,
                 temperature=TEMPERATURE, session=None, sleep=time.sleep):
        self.models = tuple(models)
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.temperature = temperature
        self.session = session or requests.Session()
        self.sleep = sleep
        self.skip = set()             # models given up on for this process
        self._lock = threading.Lock()
        self._next_start = 0.0
        self.stats = {"requests": 0, "answered": Counter(), "retries": 0, "timeouts": 0, "fallbacks": 0,
                      "refused": 0, "errors": [], "seconds": 0.0}

    def _space(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self.min_interval
        if start > now:
            self.sleep(start - now)

    def _note(self, key, msg=None):
        with self._lock:
            if msg is None:
                self.stats[key] += 1
            else:
                self.stats[key] += 1
                self.stats["errors"] = (self.stats["errors"] + [_scrub(msg)])[-20:]

    def body(self, model, prompt, schema=None):
        b = {"model": model, "input": prompt, "store": False,
             "generation_config": {"temperature": self.temperature}}
        if schema is not None:
            b["response_mime_type"] = "application/json"
            b["response_format"] = schema
        return b

    def generate(self, prompt, schema=None):
        """(text, model, payload) from the first model in the chain that
        answers. Raises GeminiError when none does, and at once, with
        refused=True and no model skipped, when the request itself is
        refused (a plain 400 or another 4xx; see the module docstring)."""
        key = api_key()
        last = None
        tried = 0
        for model in self.models:
            if model in self.skip:
                continue
            if tried:
                self._note("fallbacks")
            tried += 1
            for attempt in range(self.retries):
                self._space()
                with self._lock:
                    self.stats["requests"] += 1
                t0 = time.monotonic()
                try:
                    resp = self.session.post(URL, json=self.body(model, prompt, schema),
                                             headers={"x-goog-api-key": key}, timeout=self.timeout)
                except requests.Timeout:
                    with self._lock:
                        self.stats["seconds"] += time.monotonic() - t0
                    last = GeminiError(f"{model}: no answer within {self.timeout} s", model=model)
                    self._note("timeouts", str(last))
                    break                                  # a slow model stays slow: next model
                except requests.RequestException as e:
                    last = GeminiError(f"{model}: {type(e).__name__}: {e}", model=model)
                    self._note("retries", str(last))
                    self.sleep(min(MAX_BACKOFF, 2.0 * 2 ** attempt))
                    continue
                with self._lock:
                    self.stats["seconds"] += time.monotonic() - t0
                status = resp.status_code
                if status == 200:
                    try:
                        payload = resp.json()
                        text = output_text(payload)
                    except (ValueError, GeminiError) as e:
                        last = GeminiError(f"{model}: unusable answer: {e}", status=status, model=model)
                        self._note("retries", str(last))
                        continue
                    with self._lock:
                        self.stats["answered"][model] += 1
                    return text, payload.get("model") or model, payload
                msg = _error_text(resp)
                last = GeminiError(f"{model}: HTTP {status}: {msg}", status=status, model=model)
                if status in (401, 403):
                    raise last                             # the key itself is refused: no model will do
                if status == 404 or (status == 400 and _MODEL_GONE.search(msg)) or \
                        (status == 429 and _DAILY_QUOTA.search(msg)):
                    self.skip.add(model)                   # model gone, or today's quota for it spent
                    self._note("retries", str(last))
                    break
                if status in (408, 429) or status >= 500:
                    self._note("retries", str(last))
                    self.sleep(_retry_after(resp, attempt))
                    continue
                # any other 4xx: this request is refused, not this model. Skipping the model would send
                # every later batch down the slower fallbacks; the caller splits the batch instead.
                self._note("refused", str(last))
                raise GeminiError(str(last), status=status, model=model, refused=True)
        raise last or GeminiError("no model left to try (all skipped)")

    def report(self):
        s = dict(self.stats)
        s["answered"] = dict(s["answered"])
        s["seconds"] = round(s["seconds"], 1)
        s["skipped_models"] = sorted(self.skip)
        return s


# ── rules text, normalisation, validation ────────────────────────────────────

_TYPO = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",       # single quotes, prime
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',       # double quotes
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",  # dashes, minus
    "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u200b": "",                     # odd spaces
})


def normalise(text):
    """Text for quote matching: NFKC, typographic quotes/dashes folded to
    ASCII, every run of whitespace one space, trimmed. Case is kept."""
    s = unicodedata.normalize("NFKC", str(text or "")).translate(_TYPO)
    return re.sub(r"\s+", " ", s).strip()


def rules_text(m):
    """A market's resolution rules: its Gamma description (the question
    when there is none)."""
    return str(m.get("description") or "").strip() or str(m.get("question") or "").strip()


def rules_sha1(rules):
    """The cache key half that changes when the rules do (whitespace-only
    edits do not count)."""
    return hashlib.sha1(normalise(rules).encode("utf-8")).hexdigest()


def _quote_core(quote):
    return normalise(quote).strip(" .,;:'\"")


def quote_in_rules(quote, rules):
    """True when the (non-empty) quote is a substring of the rules after
    normalise()."""
    q = _quote_core(quote)
    return bool(q) and q in normalise(rules)


def quote_long_enough(quote):
    """True when the quote (normalised, edge punctuation stripped) has at
    least MIN_QUOTE_WORDS words or MIN_QUOTE_CHARS characters: "by Nov 3"
    and "December 31, 2026" count, "2026" and "Nov 3" prove nothing."""
    q = _quote_core(quote)
    words = [w for w in q.split(" ") if re.search(r"\w", w)]
    return len(words) >= MIN_QUOTE_WORDS or len(q) >= MIN_QUOTE_CHARS


def _date_forms(d):
    """Ways rules write a date, as (day forms a year may follow: "March 15",
    "Mar 15", "Mar. 15", "15 March"; forms that carry the year: "2027-03-15",
    "3/15/2027", "3/15/27")."""
    day = [f"{d:%B} {d.day}", f"{d:%b} {d.day}", f"{d:%b}. {d.day}", f"{d.day} {d:%B}"]
    if d.month == 9:
        day += [f"Sept {d.day}", f"Sept. {d.day}"]
    return day, [f"{d:%Y-%m-%d}", f"{d.month}/{d.day}/{d:%Y}", f"{d.month}/{d.day}/{d:%y}"]


_MONTH_NUM = {name: i for i in range(1, 13)
              for name in (datetime(2000, i, 1).strftime("%B").lower(), datetime(2000, i, 1).strftime("%b").lower())}
_MONTH_NUM["sept"] = 9
_DAY_RANGE = re.compile(r"\b(" + "|".join(sorted(_MONTH_NUM, key=len, reverse=True)) +
                        r")\.?\s+(\d{1,2})\s*(?:-|to|and|&)\s*(\d{1,2})\b")
# a year written right after a day: "June 30, 2027", "June 30th 2027", "30 June 2027", "December 8-9, 2026"
_YEAR_AFTER = re.compile(r"(?:st|nd|rd|th)?\.?,?\s*((?:19|20)\d\d)\b")


def _year_fits(t, end, d):
    m = _YEAR_AFTER.match(t, end)
    return m is None or int(m.group(1)) == d.year


def mentions_date(text, d, check_year=True):
    """True when `text` names the date `d` (a datetime.date) in a common form,
    or a day range that holds it ("December 8-9" for an FOMC meeting).
    "March 1" does not match inside "March 15", nor "1 March" inside "11 March".
    A year written right after the day must be d's year ("June 30, 2027" is
    not 2026-06-30); a day written without a year matches any year.
    check_year=False skips that test (to tell a wrong year from a missing day)."""
    t = normalise(text).lower()
    day_forms, full_forms = _date_forms(d)
    for form in day_forms + full_forms:
        for m in re.finditer(re.escape(form.lower()), t):
            before, after = t[m.start() - 1:m.start()], t[m.end():m.end() + 1]
            if before.isdigit() or after.isdigit():
                continue
            if check_year and form in day_forms and not _year_fits(t, m.end(), d):
                continue
            return True
    for m in _DAY_RANGE.finditer(t):
        if _MONTH_NUM[m.group(1)] == d.month and int(m.group(2)) <= d.day <= int(m.group(3)):
            if not check_year or _year_fits(t, m.end(), d):
                return True
    return False


# Fallback / extension clauses: what happens when the answer is late, not when it is due. A sentence
# that starts "If ..." / "In the event ..." and is about something missing or late, one that starts
# "Otherwise", or one that keeps the market open past its schedule.
_LATE = re.compile(r"\b(?:not|no|none|never|unknown|isn't|aren't|hasn't|haven't|wasn't|weren't|doesn't|don't|"
                   r"didn't|fails?|failed|ceases?|ceased|cancell?ed|postponed|delayed|unavailable)\b", re.I)
_IF_START = re.compile(r"[\"'(]?(?:if|in the event|in case|should)\b", re.I)
_OTHERWISE_START = re.compile(r"[\"'(]?otherwise\b", re.I)
_KEPT_OPEN = re.compile(r"\b(?:remains?|stays?|be kept|kept) open until\b|\bopen until\b|"
                        r"\b(?:postponed|delayed|extended) (?:after|beyond|past|until|to)\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z])")


def _sentence_around(quote, rules):
    """The sentence(s) of the rules that hold the quote (normalised), or
    None when the quote is not in them. Paragraph breaks end a sentence
    too ("... 25 bps)" followed by a new paragraph has no full stop)."""
    q = _quote_core(quote)
    if not q:
        return None
    paragraphs = [normalise(p) for p in re.split(r"\s*\n\s*", str(rules or ""))]
    for t in [p for p in paragraphs if q in p] or [normalise(rules)]:
        i = t.find(q)
        if i < 0:
            continue
        ends = list(_SENTENCE_END.finditer(t))
        start = max([0] + [m.end() for m in ends if m.end() <= i])
        stop = min([len(t)] + [m.start() for m in ends if m.start() >= i + len(q)])
        return t[start:stop]
    return None


def _sentences(rules):
    """The rules as normalised sentences (paragraph breaks end one too)."""
    out = []
    for p in re.split(r"\s*\n\s*", str(rules or "")):
        t = normalise(p)
        start = 0
        for m in _SENTENCE_END.finditer(t):
            out.append(t[start:m.start()])
            start = m.end()
        out.append(t[start:])
    return [x for x in out if x]


def _is_fallback(sentence):
    s = sentence or ""
    return bool((_IF_START.match(s) and _LATE.search(s)) or _OTHERWISE_START.match(s) or _KEPT_OPEN.search(s))


def fallback_clause(quote, rules):
    """The sentence holding the quote when it is a fallback / extension
    clause ("If the results aren't known by June 30, 2027 ...", "If Spotify
    does not release ... by January 31, 2027 ...", "this market may remain
    open until January 7, 2027 ...", "Otherwise ..."), else None. A date in
    such a sentence says when the market gives up waiting, not when the
    answer is due."""
    s = _sentence_around(quote, rules)
    return s if s and _is_fallback(s) else None


def date_only_in_fallback(d, rules):
    """The first sentence naming date `d` when every sentence of the rules
    that names it is a fallback / extension clause (box office: quote "once
    data for December 31 is made available", date 2027-01-07 taken from "If
    there is no final data available by January 7, 2027 ..."), else None."""
    hits = [x for x in _sentences(rules) if mentions_date(x, d)]
    return hits[0] if hits and all(_is_fallback(x) for x in hits) else None


def _end_dates(end_ts):
    """The UTC date of the end time and the date before it (Polymarket's
    "11:59 PM ET" deadlines are 04:59 UTC the next day)."""
    if end_ts is None:
        return set()
    d = datetime.fromtimestamp(int(end_ts), tz=timezone.utc).date()
    return {d, d - timedelta(days=1)}


def _parse_date(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "n/a"):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date() if re.fullmatch(r"\d{4}-\d\d-\d\d", s[:10]) else None
    except ValueError:
        return None


def _today(today=None):
    """today as a datetime.date: a date, a datetime, "YYYY-MM-DD", or None
    for the current UTC day."""
    if isinstance(today, datetime):
        return today.astimezone(timezone.utc).date() if today.tzinfo else today.date()
    if today is not None and not isinstance(today, str):
        return today
    return _parse_date(today) or datetime.now(timezone.utc).date()


def validate(answer, rules, end_ts=None, today=None):
    """One market's label from the model's answer item, checked against its
    rules. Returns {type, reveal_kind, reveal_date, quote, type_ok, quote_ok,
    valid, excluded, date_source, problems}:
      - type not in TYPES -> "other" (type_ok False)
      - reveal_kind not in REVEAL_KINDS -> "unclear"
      - quote shorter than MIN_QUOTE_WORDS words and MIN_QUOTE_CHARS
        characters, or not a substring of the rules (normalised) ->
        reveal_kind "unclear", reveal_date None (quote_ok False); the quote
        is kept so the miss can be read
      - reveal_date not YYYY-MM-DD -> None; a date on a non-scheduled answer
        is dropped; "scheduled" without a date -> "unclear"
      - a scheduled date that neither the quote nor the rules write (with
        its year, when a year follows the day) -> "unclear", no date: an
        end date or the model's own knowledge is not evidence
      - a scheduled answer whose quote sits in a fallback / extension
        clause (fallback_clause()), or whose date the rules write only in
        such clauses (date_only_in_fallback()) -> "unclear", no date
      - a scheduled date before `today` (default: the current UTC day) ->
        REVEALED ("revealed_awaiting_resolution"), the date kept
    date_source says where a kept date was found: "quote" or "rules".
    valid = type_ok and quote_ok."""
    a = answer if isinstance(answer, dict) else {}
    problems = []
    type_ = str(a.get("type") or "").strip().lower()
    type_ok = type_ in TYPES
    if not type_ok:
        problems.append(f"type {a.get('type')!r} not in the list")
        type_ = "other"
    kind = str(a.get("reveal_kind") or "").strip().lower()
    if kind not in REVEAL_KINDS:
        problems.append(f"reveal_kind {a.get('reveal_kind')!r} not in the list")
        kind = "unclear"
    quote = str(a.get("quote") or "").strip()
    in_rules, long_enough = quote_in_rules(quote, rules), quote_long_enough(quote)
    quote_ok = in_rules and long_enough
    raw_date = a.get("reveal_date")
    d = _parse_date(raw_date)
    if raw_date not in (None, "") and d is None and str(raw_date).strip().lower() not in ("null", "none", "n/a"):
        problems.append(f"reveal_date {raw_date!r} is not YYYY-MM-DD")
    if not quote_ok:
        if not _quote_core(quote):
            problems.append("quote is empty")
        else:
            if not in_rules:
                problems.append("quote is not in the rules")
            if not long_enough:
                problems.append(f"quote is too short to count (needs >= {MIN_QUOTE_WORDS} words or "
                                f">= {MIN_QUOTE_CHARS} characters)")
        kind, d = "unclear", None
    if kind != "scheduled" and d is not None:
        problems.append(f"{kind} answer carried a date; dropped")
        d = None
    if kind == "scheduled" and d is None:
        problems.append("scheduled without a date; made unclear")
        kind = "unclear"
    source = None
    if d is not None:
        if mentions_date(quote, d):
            source = "quote"
        elif mentions_date(rules, d):
            source = "rules"
        else:
            if mentions_date(rules, d, check_year=False):
                why = "the rules write that day with another year"
            elif d in _end_dates(end_ts):
                why = "it is only the market's end date"
            else:
                why = "the rules do not write it"
            problems.append(f"scheduled date {d.isoformat()} is not in the quote or the rules ({why}); made unclear")
            kind, d = "unclear", None
    if d is not None:
        clause, what = fallback_clause(quote, rules), "the quote"
        if not clause and source == "rules":
            clause, what = date_only_in_fallback(d, rules), f"the date {d.isoformat()}"
        if clause:
            problems.append(f"{what} comes from a fallback / extension clause, not the schedule "
                            f"({clause[:120]}{'...' if len(clause) > 120 else ''}); made unclear")
            kind, d, source = "unclear", None, None
    if d is not None and d < _today(today):
        kind = REVEALED
    return {"type": type_, "reveal_kind": kind, "reveal_date": d.isoformat() if d else None, "quote": quote,
            "type_ok": type_ok, "quote_ok": quote_ok, "valid": type_ok and quote_ok,
            "excluded": type_ in EXCLUDED_TYPES, "date_source": source, "problems": problems}


def is_excluded(label):
    """True when an AI label removes its market from the pool."""
    return bool(label) and label.get("type") in EXCLUDED_TYPES


# ── prompt ───────────────────────────────────────────────────────────────────

INSTRUCTIONS = """You label prediction markets for a research project on insider trading. For each market below, read its question and its resolution rules, then give four fields.

type: what decides the outcome. Pick exactly one:
- military_gov_action: a government, head of state, minister or military acts: strikes, invasions, troop moves, captures of territory, ceasefires, peace deals, treaties, sanctions, tariffs, executive orders, pardons, nominations and appointments, calling an election, diplomatic meetings.
- person_leaves_role: a named person leaves, is fired from or is removed from a job or office (resigns, is ousted, is out as CEO, minister or leader). Who wins an election is election_vote, not this.
- company_product: a company's own decision or product: launches, releases, AI model releases, announcements, acquisitions, IPOs, funding rounds, layoffs.
- predecided_reveal: a winner or answer already chosen by a small group before it is announced: awards (Oscars, Nobel, Grammys, Person of the Year, sports awards decided by a vote), pre-recorded shows, casting, names, set lists, selections.
- court_regulator: a court ruling; criminal charges, indictments, arrests or convictions; a regulator's decision or approval (SEC, FDA, FCC, antitrust); or a central bank's decision (interest rates).
- data_release: an official statistic or a published count, poll or ranking: inflation, jobs, GDP, approval ratings, box office, charts and leaderboards, case counts, launch counts.
- person_behavior: what a person says or does on their own: posts, tweet counts, words said in a speech, appearances, visits, a streamer's stream.
- election_vote: an election, referendum or primary (who wins it, or who becomes leader or holds a seat after it, even when that is the sitting leader), or a vote by a legislature, party, board or committee (confirmations, bills passing).
- game_sport: the result of a sports or esports game, match, race, fight, season or tournament, or an athlete's or team's performance in one (someone streaming a video game is person_behavior).
- price_level: the level of a market-set price: crypto, stocks, commodities, indexes, bond yields, exchange rates, market caps, rich lists driven by share prices, or a company valuation read off secondary-market trading on any day (such as the NPM Price that Nasdaq Private Market publishes), even when a funding round or an IPO could also move it. A valuation that only an announced funding round sets is company_product.
- weather: weather and natural hazards, counts of them included: temperature, rain, snow, storms, hurricanes, earthquakes, volcanic eruptions (how many earthquakes or eruptions a source records is weather, not data_release).
- other: none of the above.

reveal_kind: how the answer comes out.
- scheduled: one event at a set, known time fixes the answer, and the answer comes out then: a ceremony, an election or vote day, a scheduled meeting (such as a central bank meeting), a court's scheduled ruling, a scheduled report or data release, a game, or a reading taken at one set time (such as a leaderboard checked on a given date).
- any_day: the answer comes out whenever something happens, which can be any day up to the deadline: an attack, a capture, a deal, a resignation, a launch, a ceasefire starting or breaking, an announcement with no set date, a count or a price reaching a level. A deadline is not a schedule: "by December 31", "before 2027", "in 2026" and "continues through November 15" are any_day.
- unclear: you cannot tell from the rules.

The schedule is when the answer is expected to be announced or to happen: the election day, the meeting, the ceremony, the release, the moment a reading is taken. Fallback and extension clauses are NOT the schedule: sentences such as "If the results aren't known by June 30, 2027, ...", "If no winner is announced by ..., this market will resolve to \"Other\"", "this market may remain open until ...", "if the event is postponed after ..." or "otherwise this market resolves No by ..." only say what happens when the answer is late. Never take reveal_kind, reveal_date or the quote from them. When the rules date the expected event, use that date (and give it even if it is before today: the market is then waiting for official results). When they date only a fallback, the answer is any_day or unclear.

reveal_date: only for scheduled, the calendar date of that event as the rules state it, day, month and year, as YYYY-MM-DD (a meeting on "December 8-9, 2026" is 2026-12-09, its last day; "December 31, 2026, 11:59 PM ET" is 2026-12-31; never move a date to another year). null for any_day and unclear.

quote: the words that show when the answer comes out (for scheduled, the words that name the event and its date; for any_day, the deadline), copied from that market's Rules text only. Never copy from the question or the event title, and never add a date the rules do not write: when the rules say only "the listed date", "the specified date" or "the date in the title", quote those words as they stand (such as "between market creation and the listed date"). Copy exactly, character for character, as one continuous piece of the rules, keeping their quotation marks and punctuation: no paraphrase, no "...", no words added, at least three words ("scheduled for December 8-9, 2026", not "December 8-9"), at most 200 characters.

Answer with JSON only: {"labels": [{"id": "<market id>", "type": "...", "reveal_kind": "...", "reveal_date": "YYYY-MM-DD" or null, "quote": "..."}]}, exactly one entry per market, in any order, using each market's id."""


def build_prompt(items, today=None):
    """The prompt for one batch. items: [{id, question, rules, end_ts, event}]."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blocks = []
    for it in items:
        end = iso(it.get("end_ts")) if it.get("end_ts") is not None else "none"
        lines = [f"### Market id={it['id']}"]
        if it.get("event") and normalise(it["event"]) != normalise(it.get("question")):
            lines.append(f"Event: {it['event']}")
        lines += [f"Question: {it.get('question') or ''}", f"End date (UTC): {end}", "Rules:", it["rules"]]
        blocks.append("\n".join(lines))
    return f"{INSTRUCTIONS}\n\nToday is {today}.\n\n" + "\n\n".join(blocks) + "\n"


def answers_by_id(parsed):
    """{id: answer item} from a decoded batch answer ({"labels": [...]} or a
    bare list). The first answer for an id wins."""
    items = parsed.get("labels") if isinstance(parsed, dict) else parsed
    out = {}
    for a in items if isinstance(items, list) else []:
        if isinstance(a, dict) and a.get("id") is not None:
            out.setdefault(str(a["id"]).strip(), a)
    return out


# ── labelling ────────────────────────────────────────────────────────────────

def _item(m, event=None):
    ev = event if event is not None else filters.event_of(m)
    rules = rules_text(m)
    return {"id": str(m.get("id")), "question": m.get("question"), "rules": rules, "sha1": rules_sha1(rules),
            "end_ts": to_epoch(m.get("endDate")), "event": (ev or {}).get("title"),
            "event_id": str((ev or {}).get("id") or "") or None}


def _label(item, answer, model, cached, created_at=None, today=None):
    lab = validate(answer, item["rules"], item["end_ts"], today)
    lab.update({"market_id": item["id"], "model": model, "cached": cached, "prompt_version": PROMPT_VERSION,
                "answer": answer})
    if created_at is not None:
        lab["labelled_at"] = iso(created_at)
    return lab


def _from_cache(conn, item, today=None):
    row = store.get_ai_label(conn, item["id"], item["sha1"])
    if not row:
        return None
    try:
        raw = json.loads(row["raw"] or "{}")
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("prompt_version") != PROMPT_VERSION or not isinstance(raw.get("answer"), dict):
        return None
    return _label(item, raw["answer"], row["model"], True, row["created_at"], today)


def _save(conn, item, answer, model, label):
    raw = {"prompt_version": PROMPT_VERSION, "answer": answer, "question": item["question"]}
    store.save_ai_label(conn, item["id"], item["sha1"], label, model, label["valid"], json.dumps(raw))


def batches(items, size=BATCH_SIZE, max_chars=MAX_BATCH_CHARS):
    """Consecutive chunks of at most `size` items and about `max_chars` of
    rules (an item longer than that goes alone). Order is kept, so a
    caller that passes markets event by event keeps events together."""
    out, cur, chars = [], [], 0
    for it in items:
        n = len(it["rules"])
        if cur and (len(cur) >= size or chars + n > max_chars):
            out.append(cur)
            cur, chars = [], 0
        cur.append(it)
        chars += n
    if cur:
        out.append(cur)
    return out


def ask_batch(client, items, today=None):
    """One call for a batch: ({id: answer item}, model). Raises GeminiError
    / ValueError on a failed call or an answer that is not JSON."""
    text, model, _ = client.generate(build_prompt(items, today), schema=RESPONSE_SCHEMA)
    return answers_by_id(parse_json(text)), model


def _run_batch(client, conn, batch, today, save):
    """Label one batch, every market in at most two calls.

    Round 1 asks the whole batch in one call. Then, once only:
      - the call was refused (GeminiError.refused: a plain HTTP 400 or
        other 4xx): the batch is split into two halves and each half is
        asked in its own call (a one-market batch is not asked again: the
        same request would be refused the same way);
      - the call failed otherwise (every model in the chain gave up, or the
        answer was not JSON): the whole batch is asked again;
      - the answer left some ids out: those ids are asked again together.
    Whatever is still unanswered after that is returned as failed: not
    cached, and so kept by apply_exclusions and asked again on the next run.
    A refused key (HTTP 401/403) raises. Returns ({market_id: label},
    [failed ids], [errors])."""
    got, errors, failed = {}, [], []
    queue = [(list(batch), 1)]
    while queue:
        todo, rnd = queue.pop(0)
        try:
            answers, model = ask_batch(client, todo, today)
        except (GeminiError, ValueError) as e:
            if getattr(e, "status", None) in (401, 403):
                raise                         # the key is refused: stop the run, do not log it as a failed batch
            errors.append(_scrub(e))
            if rnd == 2 or (getattr(e, "refused", False) and len(todo) == 1):
                failed += todo
            elif getattr(e, "refused", False):
                half = len(todo) // 2
                queue += [(todo[:half], 2), (todo[half:], 2)]
            else:
                queue.append((todo, 2))       # once more (may land on a fallback model)
            continue
        missing = []
        for it in todo:
            a = answers.get(it["id"])
            if a is None:
                missing.append(it)
                continue
            lab = _label(it, a, model, False, now_epoch(), today)
            if save:
                _save(conn, it, a, model, lab)
            got[it["id"]] = lab
        if missing:
            errors.append(f"answer left out {len(missing)} of {len(todo)} market(s)")
            if rnd == 1:
                queue.append((missing, 2))
            else:
                failed += missing
    return got, [it["id"] for it in failed], errors


def label_markets(markets, conn=None, events=None, refresh=False, client=None, batch_size=BATCH_SIZE,
                  workers=WORKERS, budget=None, today=None, save=True, on_batch=None):
    """{market_id: label} for Gamma market dicts. Cached labels (same id,
    same rules) are reused unless refresh=True; the rest are asked in
    batches of `batch_size`, `workers` calls at a time, and cached as they
    arrive (save=False asks without touching the cache: the consistency
    check). events: optional {market_id: event} for markets nested in an
    /events object (default: the market's events[0]). budget: a
    common.Budget; no new batch starts once it has expired. today:
    "YYYY-MM-DD" (default: the current UTC day), written into the prompt
    and used to tell a scheduled reveal from a past one, cached labels
    included. Markets whose calls failed (see _run_batch) are left out of
    the result (client.stats says why); a refused key (HTTP 401/403)
    raises GeminiError.

    A label: market_id, type, reveal_kind, reveal_date, quote, type_ok,
    quote_ok, valid, excluded (type in EXCLUDED_TYPES), date_source,
    problems, model (the one that answered), cached, labelled_at,
    prompt_version, answer (the model's item as received)."""
    conn = conn or store.default_conn()
    client = client or Client()
    events = events or {}
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out, todo, seen = {}, [], set()
    for m in markets:
        mid = str(m.get("id") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        it = _item(m, events.get(mid))
        hit = None if refresh else _from_cache(conn, it, today)
        if hit:
            out[mid] = hit
        else:
            todo.append(it)
    if not todo:
        return out
    chunks = batches(todo, batch_size)
    log(f"  ai: {len(out)} cached, {len(todo)} to label in {len(chunks)} call(s)")
    lock = threading.Lock()

    def work(batch):
        if budget is not None and budget.expired():
            return {}, [it["id"] for it in batch], ["budget expired before this batch"]
        got, failed, errs = _run_batch(client, conn, batch, today, save)
        with lock:
            out.update(got)
        if on_batch:
            on_batch(batch, got, failed, errs)
        return got, failed, errs

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks)))) as pool:
        results = list(pool.map(work, chunks))
    failed = [i for _, f, _ in results for i in f]
    if failed:
        log(f"  ai: {len(failed)} market(s) not labelled this run")
    return out


def label_pool(groups, **kw):
    """label_markets for filters.open_pool groups (their markets are nested
    in /events objects, so each one's event is passed along)."""
    markets, events = [], {}
    for g in groups:
        for m in g["markets"]:
            markets.append(m)
            events[str(m.get("id"))] = g["event"]
    return label_markets(markets, events=events, **kw)


def apply_exclusions(groups, labels):
    """(kept_groups, excluded) after the AI exclusions: kept_groups are the
    groups with their excluded markets removed (an event left with none is
    dropped); excluded = [(market, label)]. A market without a label is
    kept: a failed call must not remove a market."""
    kept_groups, excluded = [], []
    for g in groups:
        kept = []
        for m in g["markets"]:
            lab = labels.get(str(m.get("id")))
            if is_excluded(lab):
                excluded.append((m, lab))
            else:
                kept.append(m)
        if kept:
            kept_groups.append({**g, "markets": kept})
    return kept_groups, excluded


# ── demo (output 6) ──────────────────────────────────────────────────────────

def _agreement(first, second):
    """Run-to-run agreement of two {market_id: label} maps on their common ids."""
    ids = sorted(set(first) & set(second))
    rows, n = [], len(ids)
    agree = Counter()
    for i in ids:
        a, b = first[i], second[i]
        same = {"type": a["type"] == b["type"], "reveal_kind": a["reveal_kind"] == b["reveal_kind"],
                "reveal_date": a["reveal_date"] == b["reveal_date"], "excluded": a["excluded"] == b["excluded"],
                "quote": normalise(a["quote"]) == normalise(b["quote"])}
        same["type_and_date"] = same["type"] and same["reveal_date"]
        for k, v in same.items():
            agree[k] += int(v)
        if not (same["type"] and same["reveal_kind"] and same["reveal_date"]):
            rows.append({"market_id": i, "first": {k: a[k] for k in ("type", "reveal_kind", "reveal_date", "quote")},
                         "second": {k: b[k] for k in ("type", "reveal_kind", "reveal_date", "quote")}})
    pct = {k: round(100.0 * v / n, 1) if n else None for k, v in agree.items()}
    return {"n": n, "agree": dict(agree), "agree_pct": pct, "disagreements": rows}


def demo(budget_seconds=480, recheck=30, batch_size=BATCH_SIZE, workers=WORKERS, refresh=False, seed=None,
         conn=None, out_path=DEMO_PATH):
    """Output 6: label today's open pool and check run-to-run consistency.
    Resumable: labels are cached, so a run cut short by the budget is
    finished by the next one; the recheck answers are kept in the store's
    meta table for the day so a rerun does not ask them again."""
    from watchlist import net  # here, not at import: the labeller itself needs no Polymarket access

    t0 = time.monotonic()
    budget = Budget(budget_seconds)
    conn = conn or store.default_conn()
    now = now_epoch()
    today = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    log("open events …")
    events = net.walk_open_events()
    groups, counts = filters.open_pool(events, now=now)
    markets, ev_of = [], {}
    for g in groups:
        for m in g["markets"]:
            markets.append(m)
            ev_of[str(m.get("id"))] = g["event"]
    log(f"  {len(events)} events, pool {len(groups)} events / {len(markets)} markets")

    client = Client()
    labels = label_pool(groups, conn=conn, refresh=refresh, client=client, batch_size=batch_size, workers=workers,
                        budget=budget, today=today)
    first_report = client.report()

    # consistency: the same 30 markets again, fresh calls (cache bypassed,
    # nothing saved), re-batched in a different order than the first run
    check = None
    meta_key = f"ai_recheck:{today}:{PROMPT_VERSION}"
    stored = store.get_meta(conn, meta_key)
    check_client = Client()
    if recheck and len(labels) >= recheck:
        rng = random.Random(seed if seed is not None else today)
        if stored and all(i in labels for i in stored.get("ids", [])) and len(stored.get("ids", [])) == recheck:
            ids = stored["ids"]
            answers = stored["answers"]
            second = {}
            for i in ids:
                if i in answers:
                    it = _item(next(m for m in markets if str(m.get("id")) == i), ev_of.get(i))
                    second[i] = _label(it, answers[i]["answer"], answers[i]["model"], False, today=today)
            check_seconds = stored.get("seconds")
            check_calls = stored.get("client")
        elif not budget.expired(reserve=30):
            ids = rng.sample(sorted(labels), recheck)
            sample = [m for m in markets if str(m.get("id")) in set(ids)]
            rng.shuffle(sample)
            t1 = time.monotonic()
            second = label_markets(sample, conn=conn, events=ev_of, refresh=True, client=check_client,
                                   batch_size=batch_size, workers=workers, today=today, save=False)
            check_seconds = round(time.monotonic() - t1, 1)
            check_calls = check_client.report()
            # keep the raw answers so a rerun today reuses them
            store.set_meta(conn, meta_key, {
                "ids": ids, "seconds": check_seconds, "client": check_calls,
                "answers": {i: {"answer": lab["answer"], "model": lab["model"]} for i, lab in second.items()}})
        else:
            ids, second, check_seconds, check_calls = [], {}, None, None
        if ids:
            check = _agreement({i: labels[i] for i in ids if i in labels}, second)
            check.update({"sample_ids": ids, "seconds": check_seconds, "client": check_calls,
                          "method": "the same markets asked again in fresh calls (cache bypassed, answers not "
                                    "saved), shuffled into different batches than the first run; same model "
                                    "chain, prompt and temperature"})

    # pool before / after the AI exclusions
    kept_groups, excluded = apply_exclusions(groups, labels)
    ai_excluded = Counter(lab["type"] for _, lab in excluded)
    unlabelled = sum(1 for m in markets if str(m.get("id")) not in labels)

    rows = []
    for g in groups:
        for m in g["markets"]:
            mid = str(m.get("id"))
            lab = labels.get(mid)
            row = {"market_id": mid, "event_id": g["event_id"], "event_title": g["event"].get("title"),
                   "question": m.get("question"), "slug": m.get("slug"), "end_date": m.get("endDate"),
                   "volume": round(filters.volume(m)), "top_price": max(filters.outcome_prices(m)),
                   "rules_sha1": rules_sha1(rules_text(m))}
            if lab is None:
                row["label"] = None
            else:
                row.update({k: lab[k] for k in ("type", "reveal_kind", "reveal_date", "quote", "type_ok", "quote_ok",
                                                "valid", "excluded", "date_source", "problems", "model", "cached")})
                row["labelled_at"] = lab.get("labelled_at")
            rows.append(row)

    labelled = [r for r in rows if r.get("type")]
    report = {
        "generated_at": iso(now),
        "today": today,
        "prompt_version": PROMPT_VERSION,
        "model_chain": list(MODELS),
        "settings": {"batch_size": batch_size, "workers": workers, "timeout_s": TIMEOUT, "temperature": TEMPERATURE,
                     "min_interval_s": MIN_INTERVAL, "excluded_types": sorted(EXCLUDED_TYPES),
                     "reveal_kinds": list(LABEL_REVEAL_KINDS),
                     "quote_minimum": f">= {MIN_QUOTE_WORDS} words or >= {MIN_QUOTE_CHARS} characters"},
        "pool": {
            "open_events_walked": len(events),
            "pool_reasons": dict(counts.most_common()),
            "before_ai": {"events": len(groups), "markets": len(markets)},
            "after_ai": {"events": len(kept_groups), "markets": sum(len(g["markets"]) for g in kept_groups)},
            "ai_excluded": dict(ai_excluded),
            "unlabelled_kept": unlabelled,
        },
        "summary": {
            "labelled": len(labelled),
            "by_type": dict(Counter(r["type"] for r in labelled).most_common()),
            "by_reveal_kind": dict(Counter(r["reveal_kind"] for r in labelled).most_common()),
            "date_source": dict(Counter(r["date_source"] for r in labelled if r["date_source"]).most_common()),
            "quote_not_in_rules": sum(1 for r in labelled if not r["quote_ok"]),
            "type_not_in_list": sum(1 for r in labelled if not r["type_ok"]),
            "answered_by_model": dict(Counter(r["model"] for r in labelled).most_common()),
            "from_cache_this_run": sum(1 for r in labelled if r["cached"]),
        },
        "run": {"labelling_client": first_report, "seconds_total": round(time.monotonic() - t0, 1),
                "calls_this_run": first_report["requests"] + (check_client.stats["requests"]),
                "note": "requests = HTTP attempts (retries included); labels already cached cost no call"},
        "consistency": check,
        "ai_excluded_markets": [r for r in rows if r.get("excluded")],
        "labels": rows,
    }
    runs = store.get_meta(conn, f"ai_demo_runs:{today}", []) or []
    runs.append({"at": iso(now), "prompt_version": PROMPT_VERSION, "seconds": report["run"]["seconds_total"],
                 "requests": report["run"]["calls_this_run"], "retries": first_report["retries"],
                 "labelled_new": len(labelled) - report["summary"]["from_cache_this_run"]})
    store.set_meta(conn, f"ai_demo_runs:{today}", runs)
    cur = [r for r in runs if r.get("prompt_version") == PROMPT_VERSION]
    report["run"]["runs_today"] = runs
    report["run"]["requests_today"] = sum(r["requests"] for r in runs)
    report["run"]["this_prompt_version"] = {
        "runs": len(cur), "requests": sum(r["requests"] for r in cur), "seconds": round(sum(r["seconds"] for r in cur), 1),
        "labelled": sum(r["labelled_new"] for r in cur),
        "note": "what the current labels cost, over every run with this prompt version (later runs reuse the cache)"}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    log(f"wrote {out_path}: {len(labelled)}/{len(rows)} labelled, AI excluded {dict(ai_excluded)}, "
        f"{report['run']['calls_this_run']} request(s), {report['run']['seconds_total']} s")
    if check:
        log(f"  consistency on {check['n']}: {check['agree_pct']}")
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="AI labels for open markets (demo output 6).")
    ap.add_argument("--demo", action="store_true", help="label today's open pool and write ai_labels.json")
    ap.add_argument("--budget-seconds", type=float, default=480)
    ap.add_argument("--recheck", type=int, default=30, help="markets to ask twice for the consistency check")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--refresh", action="store_true", help="ignore cached labels")
    ap.add_argument("--slug", help="label one market by slug and print the label")
    args = ap.parse_args()
    if args.slug:
        import polymarket_api
        m = polymarket_api.fetch_market(args.slug)
        print(json.dumps(label_markets([m], refresh=args.refresh), indent=1, ensure_ascii=False))
    elif args.demo:
        demo(budget_seconds=args.budget_seconds, recheck=args.recheck, batch_size=args.batch_size,
             workers=args.workers, refresh=args.refresh)
    else:
        ap.print_help()
