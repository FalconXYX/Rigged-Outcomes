"""
Scorers: rendered inputs + the rubric's questions -> answers (combine.py's shape). Two behind one interface,
picked by [scorer] in the rubric:

    scorer = get_scorer(rubric, name=None, **kw)
    scorer.name                       "decisions" | "interactions"
    scorer.batch                      wallets per request
    scorer.sent                       requests sent so far (the rubric's daily_cap counts these)
    scorer.score(items, questions) -> {item id: {question id: answer}}
        items: [{"id": "<wallet>@<first buy>", "input": "<render_input text>"}]
        questions: the rubric's question dicts still missing from the cache for these items

  decisions     POST https://generativelanguage.googleapis.com/v1beta/decisions, key in x-goog-api-key
                {"model", "input", "questions": {id: {"type", "instructions", "criteria"}}}
                  score   criteria: [level text, lowest first]   -> {"score", "probabilities": {"0": p}, "confidence"}
                  noul    criteria: {true_description, false_description} -> {"noul": p(true), "confidence"}
                  choice  criteria: {option id: text}            -> {"choice", "probabilities": {id: p}, "confidence"}
                (score verified 2026-11-19; noul and choice as the API reference writes them; one place to fix:
                decisions_question / parse_answer). One wallet per request: its text is the request's input,
                every question it still needs goes with it (the API takes up to 1,000).
                Spacing: rpm and tpm from [scorer.decisions] (free tier: 200 a minute, 50,000 tokens a minute;
                the tokens are the input plus every question's text), measured from each answer's usage.
                429 / 5xx / network: retried with back-off (Retry-After or retryDelay honoured).
  interactions  watchlist.ai.Client(models): regular Gemini, strict JSON, several wallets per request, one
                probability per level / option asked for and normalised here.

Errors (the pipeline's reactions):
  ScorerUnavailable  key refused, endpoint or model gone, daily quota or the rubric's daily_cap reached:
                     switch to [scorer].fallback for the rest of the run
  ScorerBusy         still busy after every retry: stop the run (a step comes back later)
  ScorerRefused      this request refused (a plain 400, input blocked): these wallets stay unscored this run
The key is read at call time by watchlist.ai.api_key() (the environment, else the .env above the repo) and sent
only in the header. Tests pass key=, url= (a local fake server) or session=; no test reaches Google.
"""
import json
import re
import threading
import time
from collections import deque

DECISIONS_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
TIMEOUT = 60
RETRIES = 5
MAX_BACKOFF = 60.0
_DAILY = re.compile(r"per ?day|daily|RequestsPerDay", re.I)
_MODEL_GONE = re.compile(r"\bmodel\b.{0,80}?\b(not found|not supported|unsupported|does not exist|not available)\b",
                         re.I | re.S)


class ScorerUnavailable(RuntimeError):
    """This scorer cannot run now (key refused, endpoint or model gone, cap reached): use the fallback."""


class ScorerBusy(RuntimeError):
    """The scorer kept answering busy (429 a minute, 5xx, no answer) through every retry: try again later."""


class ScorerRefused(RuntimeError):
    """This one request was refused (a plain 400, or the input was blocked): its wallets stay unscored."""


def _sleep(seconds, sleep=time.sleep):
    """A wait that a step's deadline cuts short (watchlist.common.step_sleep: OutOfTime when it would not fit)."""
    if seconds <= 0:
        return
    from watchlist.common import step_sleep
    step_sleep(seconds, "the scorer's spacing", sleep)


def _timeout(seconds):
    from backend import outcomes
    return outcomes.clamp(seconds)


class Pacer:
    """Request starts spaced under rpm requests and tpm tokens a minute (a sliding 60 s window) and at least
    min_interval apart, across threads. wait(tokens) before a request with its estimated tokens; done(tokens)
    after it with what the answer's usage says."""

    def __init__(self, rpm=200, tpm=50_000, min_interval=0.0, clock=time.monotonic, sleep=time.sleep):
        self.rpm, self.tpm, self.min_interval = float(rpm or 0), float(tpm or 0), float(min_interval or 0)
        self.clock, self.sleep = clock, sleep
        self.window = deque()            # [t, tokens] of the last minute's requests
        self.last = None
        self._lock = threading.Lock()

    def _need(self, tokens, now):
        while self.window and now - self.window[0][0] >= 60:
            self.window.popleft()
        wait = 0.0
        if self.last is not None and self.min_interval:
            wait = max(wait, self.last + self.min_interval - now)
        if self.rpm and len(self.window) >= self.rpm:
            wait = max(wait, self.window[0][0] + 60 - now)
        if self.tpm and self.window:
            used = sum(t for _, t in self.window)
            if used + tokens > self.tpm:
                acc = used
                for t0, t in self.window:                # wait until enough of the window has aged out
                    acc -= t
                    if acc + tokens <= self.tpm:
                        wait = max(wait, t0 + 60 - now)
                        break
        return wait

    def wait(self, tokens):
        """Sleep until a request of `tokens` fits; returns the slot to pass to done()."""
        while True:
            with self._lock:
                now = self.clock()
                need = self._need(tokens, now)
                if need <= 0:
                    slot = [now, int(tokens)]
                    self.window.append(slot)
                    self.last = now
                    return slot
            _sleep(min(need, 60.0), self.sleep)

    def done(self, slot, tokens):
        if tokens:
            with self._lock:
                slot[1] = int(tokens)


# ── the Decisions API ───────────────────────────────────────────────────────

def decisions_question(q):
    """A rubric question as the Decisions API takes it."""
    if q["type"] == "score":
        return {"type": "score", "instructions": q["instructions"], "criteria": [lv["criterion"] for lv in q["levels"]]}
    if q["type"] == "noul":
        return {"type": "noul", "instructions": q["instructions"],
                "criteria": {"true_description": q["true_description"], "false_description": q["false_description"]}}
    return {"type": "choice", "instructions": q["instructions"],
            "criteria": {o["id"]: (f"{o['label']}: {o['criterion']}" if o.get("criterion") else o["label"])
                         for o in q["options"]}}


def decisions_body(model, text, questions):
    return {"model": model, "input": text, "questions": {q["id"]: decisions_question(q) for q in questions}}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _ordered(probs, keys):
    """Probabilities by key ({"0": p, ...}, {"id": p} or a list in key order) -> [p per key], normalised to 1."""
    if isinstance(probs, dict):
        vals = [_num(probs.get(str(k), probs.get(k))) or 0.0 for k in keys]
    else:
        probs = list(probs or [])
        vals = [(_num(probs[i]) or 0.0) if i < len(probs) else 0.0 for i in range(len(keys))]
    vals = [max(0.0, v) for v in vals]
    s = sum(vals)
    return [v / s for v in vals] if s > 0 else None


def parse_answer(q, a):
    """One question's answer in the Decisions API's shape -> combine.py's shape; None when unusable."""
    if not isinstance(a, dict):
        return None
    conf = _num(a.get("confidence"))
    if q["type"] == "score":
        n = len(q["levels"])
        p = _ordered(a.get("probabilities"), list(range(n)))
        if p is None:
            return None
        score = _num(a.get("score"))
        if score is None or not 0 <= score <= n - 1:
            score = sum(i * x for i, x in enumerate(p))
        return {"type": "score", "score": round(score, 4), "p": [round(x, 4) for x in p],
                "confidence": None if conf is None else round(conf, 4)}
    if q["type"] == "noul":
        p = _num(a.get("noul", a.get("probability", a.get("p"))))
        if p is None:
            return None
        return {"type": "noul", "p": round(min(1.0, max(0.0, p)), 4),
                "confidence": None if conf is None else round(conf, 4)}
    ids = [o["id"] for o in q["options"]]
    p = _ordered(a.get("probabilities"), ids)
    if p is None:
        return None
    best = a.get("choice") if a.get("choice") in ids else ids[max(range(len(ids)), key=p.__getitem__)]
    return {"type": "choice", "choice": best, "p": {i: round(x, 4) for i, x in zip(ids, p)},
            "confidence": None if conf is None else round(conf, 4)}


def parse_decisions(questions, payload):
    """A Decisions API answer -> {question id: answer}; a question it did not answer is left out. Raises
    ScorerRefused when the input was blocked."""
    payload = payload if isinstance(payload, dict) else {}
    blocked = (payload.get("prompt_feedback") or {}).get("block_reason")
    if blocked:
        raise ScorerRefused(f"decisions: the input was blocked ({blocked})")
    got = payload.get("answers") or {}
    out = {}
    for q in questions:
        a = parse_answer(q, got.get(q["id"]))
        if a is not None:
            out[q["id"]] = a
    return out


def _error_text(resp):
    try:
        j = resp.json()
    except ValueError:
        return (resp.text or "")[:300]
    err = j.get("error") if isinstance(j, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err or j)[:300]


def _retry_after(resp, attempt):
    from watchlist import ai
    try:
        return ai._retry_after(resp, attempt)
    except Exception:
        return min(MAX_BACKOFF, 2.0 * 2 ** attempt)


def _scrub(text):
    from watchlist import ai
    return ai._scrub(text)


class DecisionsScorer:
    name = "decisions"

    def __init__(self, cfg, key=None, url=None, session=None, sleep=time.sleep, clock=time.monotonic, ledger=None):
        self.model = cfg.get("model", "gemini-decision-1")
        self.batch = 1
        self.cap = int(cfg.get("daily_cap", 2500))
        self.retries = int(cfg.get("retries", RETRIES))
        self.url, self._key, self.session = url or DECISIONS_URL, key, session
        self.sleep = sleep
        self.pacer = Pacer(cfg.get("rpm", 200), cfg.get("tpm", 50_000), cfg.get("min_interval", 0.0), clock, sleep)
        self.ledger = ledger
        self.sent = 0
        self.tokens = 0
        self._lock = threading.Lock()

    def _session(self):
        if self.session is None:
            import requests
            self.session = requests.Session()
        return self.session

    def _key_value(self):
        if self._key is not None:
            return self._key
        from watchlist import ai
        try:
            return ai.api_key()
        except ai.GeminiError as e:
            raise ScorerUnavailable(f"decisions: {e}")

    def _spend(self):
        with self._lock:
            used = self.ledger.used(self.name) if self.ledger else self.sent
            if used >= self.cap:
                raise ScorerUnavailable(f"decisions: the rubric's daily_cap of {self.cap} requests is reached")
            self.sent += 1
        if self.ledger:
            self.ledger.add(self.name)

    def request(self, body):
        """One request with retries -> the answer's JSON."""
        import requests
        key = self._key_value()
        est = len(json.dumps(body)) // 3 + 50
        last = None
        for attempt in range(self.retries):
            self._spend()
            slot = self.pacer.wait(est)
            try:
                r = self._session().post(self.url, json=body, timeout=_timeout(TIMEOUT),
                                         headers={"x-goog-api-key": key, "Content-Type": "application/json"})
            except requests.RequestException as e:
                last = f"{type(e).__name__}"
                _sleep(min(MAX_BACKOFF, 2.0 * 2 ** attempt), self.sleep)
                continue
            if r.status_code == 200:
                try:
                    payload = r.json()
                except ValueError:
                    last = "an answer that is not JSON"
                    continue
                used = _num(((payload or {}).get("usage") or {}).get("total_tokens"))
                self.pacer.done(slot, used)
                with self._lock:
                    self.tokens += int(used or 0)
                return payload
            msg = _scrub(_error_text(r))
            status = r.status_code
            if status in (401, 403):
                raise ScorerUnavailable(f"decisions: the key was refused (HTTP {status}: {msg[:120]})")
            if status == 404 or (status == 400 and _MODEL_GONE.search(msg)):
                raise ScorerUnavailable(f"decisions: the endpoint or model is gone (HTTP {status}: {msg[:120]})")
            if status == 429 and _DAILY.search(msg):
                raise ScorerUnavailable(f"decisions: today's quota is spent ({msg[:120]})")
            if status in (408, 429) or status >= 500:
                last = f"HTTP {status}: {msg[:120]}"
                _sleep(_retry_after(r, attempt), self.sleep)
                continue
            raise ScorerRefused(f"decisions: HTTP {status}: {msg[:200]}")
        raise ScorerBusy(f"decisions: no answer after {self.retries} tries ({last})")

    def score(self, items, questions):
        out = {}
        for it in items:
            out[it["id"]] = parse_decisions(questions, self.request(decisions_body(self.model, it["input"], questions)))
        return out


# ── regular Gemini (the fallback) ───────────────────────────────────────────

def _keys(q):
    if q["type"] == "score":
        return [f"l{i}" for i in range(len(q["levels"]))]
    if q["type"] == "noul":
        return ["no", "yes"]
    return [o["id"] for o in q["options"]]


class InteractionsScorer:
    """Regular Gemini through watchlist.ai.Client: the same questions as a strict-JSON form, one probability per
    level / option for each wallet, read back into the Decisions API's shape."""
    name = "interactions"

    def __init__(self, cfg, client=None, ledger=None, sleep=None):
        self.models = tuple(cfg.get("models") or ())
        self.batch = int(cfg.get("wallets_per_request", 8))
        self.temperature = float(cfg.get("temperature", 0.0))
        self.cap = int(cfg.get("daily_cap", 300))
        self.ledger = ledger
        self.sent = 0
        self._client, self._sleep = client, sleep

    def client(self):
        if self._client is None:
            from watchlist import ai
            kw = {"temperature": self.temperature, **({"sleep": self._sleep} if self._sleep else {})}
            self._client = ai.Client(models=self.models, **kw) if self.models else ai.Client(**kw)
        return self._client

    @staticmethod
    def schema(questions):
        props = {q["id"]: {"type": "object", "properties": {k: {"type": "number"} for k in _keys(q)},
                           "required": _keys(q)} for q in questions}
        return {"type": "object", "properties": {"wallets": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "string"}, **props},
            "required": ["id", *props]}}}, "required": ["wallets"]}

    @staticmethod
    def prompt(items, questions):
        qs = []
        for q in questions:
            if q["type"] == "score":
                opts = [f"  l{i}. {lv['label']}: {lv['criterion']}" for i, lv in enumerate(q["levels"])]
            elif q["type"] == "noul":
                opts = [f"  yes: {q['true_description']}", f"  no: {q['false_description']}"]
            else:
                opts = [f"  {o['id']}. {o['label']}: {o.get('criterion', '')}" for o in q["options"]]
            qs.append(f"{q['id']}: {q['instructions']}\n" + "\n".join(opts))
        wallets = "\n\n".join(f"=== wallet {it['id']} ===\n{it['input']}" for it in items)
        return ("Rate each wallet below on every question. For each question give a probability for every option "
                "key, summing to 1: how likely each option is the right description of the wallet.\n\n"
                "QUESTIONS\n" + "\n\n".join(qs) + "\n\nWALLETS\n" + wallets)

    def _ask(self, items, questions):
        from watchlist import ai
        if (self.ledger.used(self.name) if self.ledger else self.sent) >= self.cap:
            raise ScorerUnavailable(f"interactions: the rubric's daily_cap of {self.cap} requests is reached")
        self.sent += 1
        if self.ledger:
            self.ledger.add(self.name)
        client = self.client()
        try:
            text, _model, _payload = client.generate(self.prompt(items, questions), schema=self.schema(questions))
        except ai.GeminiError as e:
            # every model of the chain skipped (gone, or today's quota spent): none can answer today
            chain = tuple(getattr(client, "models", ()) or ())
            spent = bool(chain) and set(chain) <= set(getattr(client, "skip", ()) or ())
            if e.config or e.status in (401, 403) or "no model left" in str(e) or spent:
                raise ScorerUnavailable(f"interactions: {e}")
            if e.refused:
                raise ScorerRefused(f"interactions: {e}")
            if ai.is_busy(e):
                raise ScorerBusy(f"interactions: {e}")
            raise
        try:
            rows = (ai.parse_json(text) or {}).get("wallets") or []
        except (ValueError, AttributeError):
            raise ScorerRefused("interactions: the answer was not the JSON asked for")
        return {str(w.get("id")): w for w in rows if isinstance(w, dict)}

    def score(self, items, questions):
        """Each wallet goes by a short name in the prompt ("w1", "w2", ...): a model copies those back exactly,
        where a long "<wallet>@<time>" id came back changed."""
        alias = {f"w{i + 1}": it for i, it in enumerate(items)}
        try:
            got = self._ask([{"id": k, "input": it["input"]} for k, it in alias.items()], questions)
            rows = {alias[k]["id"]: v for k, v in got.items() if k in alias}
        except ScorerRefused:
            if len(items) == 1:
                raise
            half = len(items) // 2                       # a refused batch: each half once
            out = {}
            for part in (items[:half], items[half:]):
                try:
                    out.update(self.score(part, questions))
                except ScorerRefused:
                    pass
            return out
        out = {}
        for it in items:
            w = rows.get(it["id"]) or {}
            answers = {}
            for q in questions:
                got = w.get(q["id"])
                if not isinstance(got, dict):
                    continue
                p = _ordered(got, _keys(q))
                if p is None:
                    continue
                if q["type"] == "score":
                    a = {"probabilities": {str(i): x for i, x in enumerate(p)}}
                elif q["type"] == "noul":
                    a = {"noul": p[1]}
                else:
                    a = {"probabilities": dict(zip(_keys(q), p))}
                parsed = parse_answer(q, a)
                if parsed is not None:
                    answers[q["id"]] = parsed
            out[it["id"]] = answers
        return out


def get_scorer(rubric, name=None, **kw):
    """The scorer `name` (default: the rubric's scorer.use), configured from [scorer.<name>]. kw: key, url, session,
    clock (decisions); client (interactions); sleep (every wait: back-off and spacing), ledger (both). Unknown kw
    for the other scorer are ignored."""
    sc = rubric.get("scorer") or {}
    name = name or sc.get("use", "decisions")
    cfg = sc.get(name) or {}
    if name == "decisions":
        return DecisionsScorer(cfg, **{k: v for k, v in kw.items() if k in ("key", "url", "session", "sleep", "clock",
                                                                           "ledger")})
    if name == "interactions":
        return InteractionsScorer(cfg, **{k: v for k, v in kw.items() if k in ("client", "ledger", "sleep")})
    raise ValueError(f"unknown scorer {name!r}")
