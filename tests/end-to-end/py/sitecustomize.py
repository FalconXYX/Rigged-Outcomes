"""
Loaded by every Python process the harness starts (tests/end-to-end/stack.mjs puts this folder on PYTHONPATH,
with E2E_STUB=1). It changes nothing in the repository's code; it only stands in for what a local test must never
reach:

  Gemini     generativelanguage.googleapis.com is answered here, never sent: the models list (the setup check's
             key check) gets one model; a model request gets 429 "quota per day" (each model is then skipped
             for the rest of the process, so a full run's lists end quickly without model answers), or with
             E2E_GEMINI=busy 503 "overloaded" (the job's long busy waits). Each call is one line in
             $E2E_LOG_DIR/gemini.jsonl.
  Decisions  the wallet rubric's scorer (POST .../v1beta/decisions) is answered as the file $E2E_GEMINI_MODE_FILE
             says, read on every call (a scenario switches it while its stack runs): its first word is the mode,
               answer    answers made from the facts in the request's input (a fresh wallet that bought hours
                         before the reveal at long odds scores high, a veteran low), the same for the same request,
                         each after DECIDE_S, in the shape the API answers (scorers.parse_decisions)
               quota     429 "per day": the scorer is out for the day (the fallback then gets the 429 above)
               busy      503 "overloaded" (the step comes back later)
               fallback  429 "per day", and the rubric's fallback request (scorers.InteractionsScorer: "=== wallet
                         w1 ===" sections) answered from each wallet's facts the same way, after FALLBACK_S
             and further words switch on
               polymarket  data-api and gamma-api.polymarket.com answer 503 (Polymarket down)
               refuse      the first input asked while it is on is refused with a plain 400, every time
             With a mode, its calls are logged as kind "decisions" (with the mode and the input's sha1) and the
             answered fallback requests as "model" with "rubric": true; without one (no file, or no mode in it)
             a Decisions call is answered and logged as any model request above ("model"). Only the step
             function gets the file; its Polymarket requests (503s included) are each one line in
             $E2E_LOG_DIR/polymarket.jsonl.
  production parthjain.ca, any *.workers.dev Worker and api.github.com are refused as a connection error, so a
             mistake in the harness (a production address in a setting) fails loudly instead of writing there.

Polymarket, Google News and the stack's own local Worker are reached as usual.
"""
import json
import os
import sys
import time

DECIDE_S = 1.0                       # how long the stand-in takes to answer a Decisions request
FALLBACK_S = 2.0                     # and a fallback request (8 wallets)

if os.environ.get("E2E_STUB") == "1":
    try:
        import requests
        import requests.adapters
        from urllib.parse import urlparse
    except ImportError:              # a Python without requests reaches none of these hosts anyway
        requests = None

    if requests is not None:
        import hashlib
        import re

        _send = requests.adapters.HTTPAdapter.send
        _BLOCKED = ("parthjain.ca", "workers.dev", "api.github.com")

        def _note(kind, request, status, **extra):
            folder = os.environ.get("E2E_LOG_DIR")
            if not folder:
                return
            try:
                with open(os.path.join(folder, "gemini.jsonl"), "a") as f:
                    f.write(json.dumps({"at": time.time(), "kind": kind, "method": request.method,
                                        "path": urlparse(request.url).path, "status": status,
                                        "argv": sys.argv[:3], **extra}) + "\n")
            except OSError:
                pass

        def _pm(request, status):
            """One line per Polymarket request of the step function in $E2E_LOG_DIR/polymarket.jsonl."""
            folder = os.environ.get("E2E_LOG_DIR")
            if not folder:
                return
            u = urlparse(request.url)
            try:
                with open(os.path.join(folder, "polymarket.jsonl"), "a") as f:
                    f.write(json.dumps({"at": time.time(), "host": u.hostname, "path": u.path, "status": status}) + "\n")
            except OSError:
                pass

        def _answer(request, status, body):
            r = requests.models.Response()
            r.status_code = status
            r._content = json.dumps(body).encode()
            r.headers["Content-Type"] = "application/json; charset=UTF-8"
            r.url = request.url
            r.request = request
            r.reason = {200: "OK", 400: "Bad Request", 429: "Too Many Requests"}.get(status, "Service Unavailable")
            r.encoding = "utf-8"
            return r

        # ── the Decisions API stand-in ──

        MODES = ("answer", "quota", "busy", "fallback")
        FLAGS = ("polymarket", "refuse")
        _POLYMARKET = ("data-api.polymarket.com", "gamma-api.polymarket.com")
        # the rubric's factor labels as today's file has them (the rubric itself is read when it loads)
        _LABELS = {"Lead time": "hours_before_reveal", "Time to the big move": "hours_to_big_move",
                   "Entry odds": "entry_price", "Position": "position_usd", "Share of market": "share_of_volume_pct",
                   "Portfolio concentration": "concentration_pct", "Prior trades": "prior_trades",
                   "Wallet age": "wallet_age_days", "Prior markets": "prior_markets", "Fills": "buy_fills",
                   "Build time": "build_span_hours", "Sold before the reveal": "sold_before_reveal_pct",
                   "Fresh wallets alongside": "fresh_whales_same_window"}
        _labels = []
        _FACT = re.compile(r"^\s*-\s+(.+?):\s+(.+?)\.(?:\s|$)")
        _NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
        _WALLET = re.compile(r"=== wallet (\S+) ===\n(.*?)(?=\n\n=== wallet |\Z)", re.S)

        def _mode():
            """(mode or None, {flags}) from the mode file."""
            path = os.environ.get("E2E_GEMINI_MODE_FILE")
            if not path:
                return None, set()
            try:
                with open(path, encoding="utf-8") as f:
                    words = f.read().strip().lower().split()
            except OSError:
                return None, set()
            mode = words[0] if words and words[0] in MODES else None
            return mode, {w for w in words[1:] if w in FLAGS}

        def _refuses(digest):
            """Whether the refuse switch turns down this input: the first one asked while it is on, every time."""
            path = os.environ["E2E_GEMINI_MODE_FILE"] + ".refused"
            try:
                with open(path, encoding="utf-8") as f:
                    first = f.read().strip()
            except OSError:
                first = ""
            if not first:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(digest)
                return True
            return first == digest

        def _factor_labels():
            """{label: factor id} of the rubric the step function runs (today's labels when it does not load)."""
            if not _labels:
                got = dict(_LABELS)
                try:
                    from backend.rubric import spec
                    got.update({f["label"]: f["id"] for f in spec.load_rubric().get("factors") or []})
                except Exception:
                    pass
                _labels.append(got)
            return _labels[0]

        def _facts(text):
            """{factor id: number or None} from the input's fact lines ("- Lead time: 9.5 hours. ...")."""
            labels, out = _factor_labels(), {}
            for line in str(text or "").splitlines():
                m = _FACT.match(line)
                if not m or m.group(1).strip() not in labels:
                    continue
                value = m.group(2).strip()
                n = _NUMBER.search(value)
                out[labels[m.group(1).strip()]] = float(n.group(0).replace(",", "")) if n else None
            return out

        def _levels(f):
            """Each question's likeliest level (0-4) from the facts, as the rubric's criteria read them."""
            lead, move, odds = f.get("hours_before_reveal"), f.get("hours_to_big_move"), f.get("entry_price")
            conc, share = f.get("concentration_pct"), f.get("share_of_volume_pct") or 0.0
            sold, fresh = f.get("sold_before_reveal_pct") or 0.0, f.get("fresh_whales_same_window") or 0.0
            trades, age = f.get("prior_trades"), f.get("wallet_age_days")
            fills, span = f.get("buy_fills"), f.get("build_span_hours")
            near = min((x for x in (lead, move) if x is not None and x >= 0), default=None)
            if lead is None and move is None:
                timing = 1
            elif (lead is not None and lead < 0) or (odds is not None and odds > 0.85):
                timing = 0
            elif (odds is not None and odds >= 0.5) or near is None or near > 720:
                timing = 1
            else:
                timing = 2 if near > 168 else 3 if near > 48 else 4
            if sold > 50:
                conviction = 0
            elif conc is None:
                conviction = 1
            else:
                conviction = 4 if conc >= 90 and share >= 5 else 3 if conc >= 50 else 2 if conc >= 33 else \
                    1 if conc >= 10 else 0
            if trades is None:
                account = 2
            elif trades == 0 or (age is not None and age <= 3):
                account = 4
            else:
                account = 3 if trades <= 10 else 2 if trades <= 50 else 1 if trades <= 300 else 0
            if sold > 50:
                pattern = 0
            elif fresh >= 2:
                pattern = 4
            elif fresh >= 1:
                pattern = 3
            else:
                pattern = 2 if fills is not None and fills <= 5 and (span is None or span <= 24) else 1
            overall = 0 if timing == 0 else min(4, int(0.45 * timing + 0.35 * account + 0.2 * conviction + 0.5))
            if timing == 0:
                profile = "collector"
            elif fills is not None and fills >= 50 and sold > 30:
                profile = "liquidity"
            elif account >= 3 and timing >= 3:
                profile = "early_informed"
            elif account <= 1:
                profile = "analyst" if timing >= 2 else "regular"
            else:
                profile = "momentum"
            single = 0.85 if trades is not None and trades <= 10 and (conc or 0) >= 80 else 0.4 if account >= 3 else 0.1
            return {"timing": timing, "conviction": conviction, "account": account, "pattern": pattern,
                    "overall": overall}, single, profile

        def _peaked(n, top):
            """Probabilities over n ordered levels: 0.7 on top, the rest halving with each step away."""
            others = {i: 2.0 ** -abs(i - top) for i in range(n) if i != top}
            z = sum(others.values()) or 1.0
            return [0.7 if i == top else 0.3 * others[i] / z for i in range(n)] if others else [1.0]

        def _decide(body):
            """The Decisions API's answer to one request (scorers.parse_decisions reads it)."""
            levels, single, profile = _levels(_facts(body.get("input")))
            out = {}
            for qid, q in (body.get("questions") or {}).items():
                kind = q.get("type")
                if kind == "score":
                    n = max(1, len(q.get("criteria") or []))
                    top = round(levels.get(qid, levels["overall"]) * (n - 1) / 4)
                    p = _peaked(n, top)
                    out[qid] = {"type": "score", "score": round(sum(i * x for i, x in enumerate(p)), 4),
                                "probabilities": {str(i): round(x, 4) for i, x in enumerate(p)}, "confidence": 0.7}
                elif kind == "noul":
                    p = single if qid == "single_purpose" else 0.5
                    out[qid] = {"type": "noul", "noul": p, "confidence": round(max(p, 1 - p), 4)}
                else:
                    ids = list(q.get("criteria") or {}) or ["none"]
                    best = profile if profile in ids else ids[0]
                    rest = 0.4 / max(1, len(ids) - 1)
                    out[qid] = {"type": "choice", "choice": best, "confidence": 0.6,
                                "probabilities": {i: (0.6 if i == best else rest) if len(ids) > 1 else 1.0
                                                  for i in ids}}
            size = len(json.dumps(body))
            return {"answers": out, "usage": {"total_tokens": size // 4, "total_output_tokens": 8 * len(out)}}

        def _decisions(request, mode, flags):
            try:
                body = json.loads(request.body or b"{}")
            except ValueError:
                body = {}
            digest = hashlib.sha1(str(body.get("input") or "").encode("utf-8")).hexdigest()[:16]
            # the input's first line names the market (the rubric's [input] template starts with its title)
            first = next((ln.strip() for ln in str(body.get("input") or "").splitlines() if ln.strip()), "")[:200]
            if mode == "busy":
                _note("decisions", request, 503, mode=mode, input=digest, market=first)
                return _answer(request, 503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                        "message": "The model is overloaded. Please try again later."}})
            if mode in ("quota", "fallback"):
                _note("decisions", request, 429, mode=mode, input=digest, market=first)
                return _answer(request, 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                        "message": "You exceeded your current quota: "
                                                                   "decision_requests, limit: 10000 per day"}})
            if "refuse" in flags and _refuses(digest):
                _note("decisions", request, 400, mode=mode, input=digest, market=first)
                return _answer(request, 400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                        "message": "Request contains an invalid argument."}})
            time.sleep(DECIDE_S)
            answer = _decide(body)
            _note("decisions", request, 200, mode=mode, input=digest, market=first,
                  overall=(answer["answers"].get("overall") or {}).get("score"))
            return _answer(request, 200, answer)

        def _fallback(request):
            """The rubric's fallback request (scorers.InteractionsScorer's prompt and schema) answered, or None
            for any other model request."""
            try:
                body = json.loads(request.body or b"{}")
            except ValueError:
                return None
            prompt = str(body.get("input") or "")
            if not prompt.startswith("Rate each wallet") or "=== wallet " not in prompt:
                return None
            items = ((((body.get("response_format") or {}).get("properties") or {}).get("wallets") or {})
                     .get("items") or {}).get("properties") or {}
            keys = {q: list((v.get("properties") or {})) for q, v in items.items() if q != "id"}
            wallets = []
            for alias, text in _WALLET.findall(prompt):
                levels, single, profile = _levels(_facts(text))
                row = {"id": alias}
                for qid, ks in keys.items():
                    if ks and all(re.fullmatch(r"l\d+", k) for k in ks):
                        top = round(levels.get(qid, levels["overall"]) * (len(ks) - 1) / 4)
                        row[qid] = {k: round(x, 4) for k, x in zip(ks, _peaked(len(ks), top))}
                    elif sorted(ks) == ["no", "yes"]:
                        p = single if qid == "single_purpose" else 0.5
                        row[qid] = {"yes": p, "no": round(1 - p, 4)}
                    elif ks:
                        best = profile if profile in ks else ks[0]
                        rest = 0.4 / max(1, len(ks) - 1)
                        row[qid] = {k: (0.6 if k == best else rest) if len(ks) > 1 else 1.0 for k in ks}
                wallets.append(row)
            time.sleep(FALLBACK_S)
            _note("model", request, 200, rubric=True, wallets=len(wallets))
            return _answer(request, 200, {"status": "completed", "model": body.get("model"), "steps": [
                {"type": "model_output", "content": [{"type": "text", "text": json.dumps({"wallets": wallets})}]}]})

        def send(self, request, *args, **kwargs):
            host = (urlparse(request.url).hostname or "").lower()
            if host == "generativelanguage.googleapis.com":
                path = urlparse(request.url).path
                if request.method == "GET" and "/models" in path:
                    _note("models", request, 200)
                    return _answer(request, 200, {"models": [{"name": "models/gemini-flash-latest",
                                                              "displayName": "Gemini Flash Latest (e2e stand-in)"}]})
                mode, flags = _mode()
                if request.method == "POST" and path.endswith("/decisions") and mode:
                    return _decisions(request, mode, flags)
                if request.method == "POST" and mode == "fallback":
                    got = _fallback(request)
                    if got is not None:
                        return got
                if os.environ.get("E2E_GEMINI") == "busy":
                    _note("model", request, 503)
                    return _answer(request, 503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                            "message": "The model is overloaded. Please try again later."}})
                _note("model", request, 429)
                return _answer(request, 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                        "message": "You exceeded your current quota: "
                                                                   "generate_content_free_tier_requests, limit: 0 per day"}})
            if host in _POLYMARKET and os.environ.get("E2E_GEMINI_MODE_FILE"):
                if "polymarket" in _mode()[1]:
                    _pm(request, 503)
                    return _answer(request, 503, {"error": "Service Unavailable"})
                try:
                    r = _send(self, request, *args, **kwargs)
                except Exception:
                    _pm(request, None)
                    raise
                _pm(request, r.status_code)
                return r
            if any(host == b or host.endswith("." + b) for b in _BLOCKED):
                raise requests.exceptions.ConnectionError(f"e2e: {host} is production; the harness never reaches it")
            return _send(self, request, *args, **kwargs)

        requests.adapters.HTTPAdapter.send = send
