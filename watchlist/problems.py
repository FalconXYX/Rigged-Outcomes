"""
What went wrong in a run of the daily lists on a Mac (scripts/watchlist-mac.sh, the modules' commands), and
whether the run fails for it.
A run ends green unless a person must fix something:

  config   the setup: a secret or variable missing, the job token or the Gemini key refused, the saved state
           damaged or not this code's format, or lost from a database that was used
  crash    the code: an unexpected exception (its traceback is in the step's log), a step that failed before
           it started, or a service saying the code's request is wrong (a model it names gone, a request the
           model, Polymarket or the site's API refuses, an answer in a shape the code does not know,
           requirements.lock failing while PyPI answers)
  outside  everything else: a service that still did not answer after its retries (Polymarket, Google News,
           the model, the site's API, PyPI), a list kept as it was, a step stopped at its time limit. A
           warning, a line in the run's report (python3 -m backend.status shows it) and in the job's summary;
           the run stays green

Each step runs inside guard() (the modules' __main__): the step is recorded as running, then as done, stopped
or crashed, in JOB_DIR/job_run.json. An outside exception (what the services raise once their own retries are
spent) becomes a recorded problem and exit 0, so the step's list keeps its last version and the other steps go
on; anything else is printed with its traceback and recorded as a crash (exit 1). A step records its own
problems with note() (threads too). The report (watchlist.sync push) puts this run's problems in its status and
summary; a config or crash problem makes the run's status failure. Outside a guarded step (a test, a function
called by hand) note() only logs. job_run.json keeps KEEP_DAYS of problems (it lasts between runs on the Mac).
"""
import json
import os
import signal
import sys
import threading
import traceback

from watchlist.common import JOB_DIR, log, now_epoch

OUTSIDE, CONFIG, CRASH = "outside", "config", "crash"
RED = (CONFIG, CRASH)
FILE = "job_run.json"
MESSAGE_CHARS = 240               # one problem, as recorded
SUMMARY_CHARS = 500               # the report's summary (the API keeps this many)
KEEP_DAYS = 7                     # problems older than this leave job_run.json when it is saved
STATUSES = ("success", "partial", "failure")
# the job's steps, as the workflow names them (its step ids) and as a person reads them
LABELS = {"install": "installing the Python packages",
          "pull": "restoring the state", "work": "fetching the work",
          "closed": "the closed list", "open": "the open list",
          "save": "saving the state", "add": "adding the markets", "report": "the report"}
# what stands when a step gives up on a service that does not answer
KEEPS = {"closed": "it keeps its last version", "open": "it keeps its last version",
         "work": "the markets to add wait for the next run",
         "add": "the markets to add wait for the next run", "report": "the last report stands"}

# steps that run no guarded Python, and what their failure means: an outside problem (the run stays green), or,
# when the step said the fault is ours (--broken), a crash
UNGUARDED = {"install": "the Python packages could not be installed after 3 tries (PyPI did not answer, or see the "
                        "step's log); nothing else ran today and the site keeps its lists"}
BROKEN = {"install": "the Python packages could not be installed although PyPI answers: requirements.lock is wrong "
                     "(a hash, or a version with no wheel for Python 3.12; see the step's log)"}

_active = {"step": None, "dir": None}
_lock = threading.Lock()                 # one writer of job_run.json at a time (the steps' threads note problems)


class Stopped(BaseException):
    """The step was told to stop (SIGTERM or SIGINT: its time limit, or the run cancelled)."""


def label(step):
    return LABELS.get(step, step or "the job")


def _path(job_dir=None):
    return os.path.join(job_dir or _active["dir"] or JOB_DIR, FILE)


def _load(path):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("steps"), dict):
        doc["steps"] = {}
    if not isinstance(doc.get("problems"), list):
        doc["problems"] = []
    return doc


def _save(path, doc):
    cut = now_epoch() - KEEP_DAYS * 86400
    doc["problems"] = [p for p in doc["problems"] if isinstance(p, dict) and int(p.get("at") or 0) >= cut]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _one_line(text, n=MESSAGE_CHARS):
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def note(message, kind=OUTSIDE, step=None, echo=True):
    """Record one of this run's problems (and log it). Outside a guarded step
    it is only logged. Safe from the step's threads. Returns the message as recorded."""
    msg = _one_line(message)
    if echo:
        log(f"  {'!' if kind == OUTSIDE else 'X'} {msg}")
    step = step or _active["step"]
    if step is not None and _active["dir"] is not None:
        try:
            with _lock:
                path = _path()
                doc = _load(path)
                doc["problems"].append({"step": step, "kind": kind, "message": msg, "at": now_epoch()})
                _save(path, doc)
        except OSError as e:                                   # never let the record cost the step
            log(f"  ! the problem could not be recorded ({type(e).__name__})")
    return msg


def _mark(step, **fields):
    try:
        with _lock:
            path = _path()
            doc = _load(path)
            rec = {} if fields.get("state") == "running" else dict(doc["steps"].get(step) or {})
            rec.update(fields)
            doc["steps"][step] = rec
            _save(path, doc)
    except OSError as e:
        log(f"  ! the step could not be recorded ({type(e).__name__})")


# ── what an exception means ─────────────────────────────────────────────────

# HTTP statuses a service sends when it is busy, down or turning the machine away for now: waiting may help
BUSY_STATUSES = (403, 408, 425, 429)


def busy_status(status):
    return status is None or status in BUSY_STATUSES or status >= 500


def polymarket_kind(e):
    """OUTSIDE for a PolymarketAPIError that waiting may cure: get_json's retries spent (a 429, a 5xx, a network
    error, a page that was not JSON), or a status that says busy or not now (BUSY_STATUSES, 5xx). CRASH for one
    that says the request or the code is wrong: an endpoint retired (410), another 4xx (a request Polymarket
    cannot read), an error payload, or an answer whose shape the code does not know (the v2 move was one)."""
    from polymarket_api import RetiredEndpointError
    if isinstance(e, RetiredEndpointError):
        return CRASH
    if str(e).startswith("gave up on "):
        return OUTSIDE
    status = getattr(e, "status", None)
    return OUTSIDE if status is not None and busy_status(status) else CRASH


def _kind(e):
    import requests
    from polymarket_api import PolymarketAPIError
    from watchlist import ai, news, sync
    if isinstance(e, PolymarketAPIError):
        return polymarket_kind(e)
    if isinstance(e, news.NewsError):
        return OUTSIDE                                     # Google refusing or not answering: never the code's
    if isinstance(e, ai.GeminiError):
        if e.status in (401, 403) or getattr(e, "config", False):
            return CONFIG
        return CRASH if getattr(e, "refused", False) else OUTSIDE    # refused: a request the API cannot take
    if isinstance(e, sync.ApiError):
        return CONFIG if e.refused else OUTSIDE if e.outage else CRASH
    if isinstance(e, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                      requests.exceptions.ContentDecodingError)):
        return OUTSIDE
    if isinstance(e, requests.HTTPError):
        return OUTSIDE if busy_status(getattr(e.response, "status_code", None)) else CRASH
    return CRASH                                           # InvalidURL, JSONDecodeError, KeyError, ...: the code


def classify(e):
    """OUTSIDE for what a service raises once its retries are spent, CONFIG for a refused token or key, CRASH
    for anything else, including a request a service says is wrong. An error raised from another (raise ... from
    e: a step that wraps what failed under it) is read as the one it wraps."""
    try:
        for _ in range(5):
            kind = _kind(e)
            if kind != CRASH or e.__cause__ is None:
                return kind
            e = e.__cause__
    except Exception:                                          # the check itself failed: the code is broken
        return CRASH
    return CRASH


def describe(e):
    """Which service did not answer, in a few words, with the error's own words after it."""
    name = type(e).__name__
    try:
        import requests
        from polymarket_api import PolymarketAPIError
        from watchlist import ai, news, sync
        who = ("Polymarket did not answer" if isinstance(e, PolymarketAPIError) else
               "Google News did not answer" if isinstance(e, news.NewsError) else
               "the model did not answer" if isinstance(e, ai.GeminiError) else
               "the site's API did not answer" if isinstance(e, sync.ApiError) else
               "a network request failed" if isinstance(e, requests.RequestException) else name)
    except Exception:
        who = name
    return f"{who} ({_one_line(e, 120)})"


# ── the guard ───────────────────────────────────────────────────────────────

def _stop(signum, frame):
    raise Stopped(signal.Signals(signum).name)


def guard(step, fn, job_dir=None):
    """Run fn() (a step's main; it returns an exit code or None) as the job's step `step`, recorded in job_dir
    (default JOB_DIR). Returns the exit code: fn's own, 0 after an outside exception (recorded), 1 after a crash
    or a config exception (recorded, the traceback printed) or when the step was told to stop."""
    _active.update(step=step, dir=job_dir or JOB_DIR)
    _mark(step, state="running", at=now_epoch())
    handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            handlers[sig] = signal.signal(sig, _stop)
        except ValueError:                                      # not the main thread (a test): no handler
            pass
    try:
        try:
            code = fn()
        except SystemExit as e:                                 # argparse, or a sys.exit inside
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if code:
                note(f"{label(step)} stopped with exit code {code} (see its log)", CRASH)
                _mark(step, state="crashed", code=code)
                return code
        code = code if isinstance(code, int) else 0            # None, or what a function returned: done
        _mark(step, state="done", code=code)
        return code
    except (Stopped, KeyboardInterrupt) as e:
        note(f"{label(step)} was stopped part way ({type(e).__name__} {e}: its time limit, or the run was cancelled)")
        _mark(step, state="stopped", code=1)
        return 1
    except Exception as e:                                      # noqa: BLE001 - this is the step's last word
        kind = classify(e)
        if kind == OUTSIDE:
            keeps = KEEPS.get(step)
            note(f"{label(step)} was not finished: {describe(e)}" + (f"; {keeps}" if keeps else ""))
            _mark(step, state="done", code=0)
            return 0
        traceback.print_exc()
        sys.stderr.flush()
        if kind == CONFIG:
            note(f"{label(step)} stopped: {type(e).__name__}: {e}", CONFIG)
        else:
            note(f"{label(step)} crashed: {type(e).__name__}: {e} (the traceback is in this step's log)", CRASH)
        _mark(step, state="crashed", code=1)
        return 1
    finally:
        for sig, old in handlers.items():
            signal.signal(sig, old)
        _active.update(step=None, dir=None)


# ── the run's problems and its status ────────────────────────────────────────

def collect(started_at, outcomes=None, job_dir=None, broken=()):
    """This run's problems ({step, kind, message}): the recorded ones, and one for each step whose outcome is
    "failure" without a record of why (killed at its time limit: outside; never started its guard: a crash; an
    unguarded step: outside, or a crash when it is in broken)."""
    doc = _load(_path(job_dir))
    since = int(started_at or 0)
    out = [p for p in doc["problems"] if isinstance(p, dict) and int(p.get("at") or 0) >= since]
    for step, outcome in (outcomes or {}).items():
        if outcome != "failure":
            continue
        rec = doc["steps"].get(step)
        if step in UNGUARDED:
            if step in (broken or ()):
                out.append({"step": step, "kind": CRASH, "message": BROKEN.get(step, f"{label(step)} failed")})
            else:
                out.append({"step": step, "kind": OUTSIDE, "message": UNGUARDED[step]})
        elif not isinstance(rec, dict) or int(rec.get("at") or 0) < since:
            out.append({"step": step, "kind": CRASH,
                        "message": f"{label(step)} failed before it started (the code could not run; see its log)"})
        elif rec.get("state") == "running":
            out.append({"step": step, "kind": OUTSIDE, "message": f"{label(step)} was stopped at its time limit"})
        elif not any(p.get("step") == step for p in out):
            out.append({"step": step, "kind": CRASH,
                        "message": f"{label(step)} failed with exit code {rec.get('code')} (see its log)"})
    return out


def red(problems):
    return [p for p in problems if p.get("kind") in RED]


def status(given, outcomes, problems):
    """The run's status for its report: failure when the state was not restored or saved or a person must
    fix something, partial when anything else went wrong, else what the caller said."""
    rank = STATUSES.index(given if given in STATUSES else "success")
    outcomes = outcomes or {}
    if problems or "failure" in outcomes.values():
        rank = max(rank, 1)
    if red(problems) or "failure" in (outcomes.get("pull"), outcomes.get("save")):
        rank = 2
    return STATUSES[rank]


def summary_with(summary, problems, limit=SUMMARY_CHARS):
    """The report's one line with this run's problems after it, cut to what the API keeps."""
    if not problems:
        return summary
    text = f"{summary}; problems: " + "; ".join(p["message"] for p in problems)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def parse_outcomes(pairs):
    out = {}
    for pair in pairs or []:
        step, _, outcome = str(pair).partition("=")
        if step:
            out[step.strip()] = outcome.strip()
    return out
