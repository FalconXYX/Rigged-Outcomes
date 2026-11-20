"""
Loaded by every Python process the harness starts (tests/end-to-end/stack.mjs puts this folder on PYTHONPATH,
with E2E_STUB=1). It changes nothing in the repository's code; it only stands in for what a local test must never
reach:

  Gemini     generativelanguage.googleapis.com is answered here, never sent: the models list (the setup check's
             key check) gets one model; a model request gets 429 "quota per day" (each model is then skipped
             for the rest of the process, so a full run's lists end quickly without model answers), or with
             E2E_GEMINI=busy 503 "overloaded" (the job's long busy waits). Each call is one line in
             $E2E_LOG_DIR/gemini.jsonl.
  production parthjain.ca, any *.workers.dev Worker and api.github.com are refused as a connection error, so a
             mistake in the harness (a production address in a setting) fails loudly instead of writing there.

Polymarket, Google News and the stack's own local Worker are reached as usual.
"""
import json
import os
import sys
import time

if os.environ.get("E2E_STUB") == "1":
    try:
        import requests
        import requests.adapters
        from urllib.parse import urlparse
    except ImportError:              # a Python without requests reaches none of these hosts anyway
        requests = None

    if requests is not None:
        _send = requests.adapters.HTTPAdapter.send
        _BLOCKED = ("parthjain.ca", "workers.dev", "api.github.com")

        def _note(kind, request, status):
            folder = os.environ.get("E2E_LOG_DIR")
            if not folder:
                return
            try:
                with open(os.path.join(folder, "gemini.jsonl"), "a") as f:
                    f.write(json.dumps({"at": time.time(), "kind": kind, "method": request.method,
                                        "path": urlparse(request.url).path, "status": status,
                                        "argv": sys.argv[:3]}) + "\n")
            except OSError:
                pass

        def _answer(request, status, body):
            r = requests.models.Response()
            r.status_code = status
            r._content = json.dumps(body).encode()
            r.headers["Content-Type"] = "application/json; charset=UTF-8"
            r.url = request.url
            r.request = request
            r.reason = {200: "OK", 429: "Too Many Requests"}.get(status, "Service Unavailable")
            r.encoding = "utf-8"
            return r

        def send(self, request, *args, **kwargs):
            host = (urlparse(request.url).hostname or "").lower()
            if host == "generativelanguage.googleapis.com":
                if request.method == "GET" and "/models" in urlparse(request.url).path:
                    _note("models", request, 200)
                    return _answer(request, 200, {"models": [{"name": "models/gemini-flash-latest",
                                                              "displayName": "Gemini Flash Latest (e2e stand-in)"}]})
                if os.environ.get("E2E_GEMINI") == "busy":
                    _note("model", request, 503)
                    return _answer(request, 503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                            "message": "The model is overloaded. Please try again later."}})
                _note("model", request, 429)
                return _answer(request, 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                        "message": "You exceeded your current quota: "
                                                                   "generate_content_free_tier_requests, limit: 0 per day"}})
            if any(host == b or host.endswith("." + b) for b in _BLOCKED):
                raise requests.exceptions.ConnectionError(f"e2e: {host} is production; the harness never reaches it")
            return _send(self, request, *args, **kwargs)

        requests.adapters.HTTPAdapter.send = send
