"""
One Vercel call of the step function (docs/BACKEND.md): claim the woken lane's jobs from the Worker,
run each one's handler before the call's deadline, report it, and claim again while time is left.

    end      = t0 + function_max_s - cold_start_s        what the platform leaves the call (300 - 10 s)
    deadline = end - reserve_s                            room to report (20 s)

A job is claimed with the seconds left to the deadline, and the Worker only hands out a kind whose need_s fits.
Its handler gets a StepContext (ctx): the job, the budget, the Worker's clock, its own empty /tmp folder, the
API, checkpoints and results, and for a background job (CONTRACT background_kinds) ctx.give_way(): whether
a foreground job (of a kind not listed there) waits for its lane or the heavy slot, from the Worker's answer to
its checkpoints. What the handler does decides the outcome sent:

    returns None or ctx.done(...)      done
    returns ctx.partial(cursor)        partial
    raises Later                       later
    raises OutOfTime                   partial, from the last cursor checkpointed (else the job's own)
    raises GiveWay                     the same (an OutOfTime: a foreground job waits for this background step)
    raises Lost                        nothing: the lease is gone, the call drops the job
    Polymarket not answering           later (polymarket_down, 120 s)
    raises anything else               error, one line with every secret cut out

Usage goes with every complete (cpu_ms, wall_ms, mem_mb, units), and the call's own time outside its jobs goes
with the next claim (`spent`), the last one included, so the Worker's ledger holds every second the call used.
A complete also says whether the call claims again (continuing: min_claim_left_s left, and after a give-way the
longest need_s of the lane, else the Worker wakes the lane for a fresh call and this one ends) and whether the
step gave way (gave_way: the Worker then counts no partial while a foreground job is ready).
Nothing mutable lives at module level: several calls can share one process (Fluid compute).
"""
import contextvars
import hashlib
import importlib
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import urllib.parse

import requests

from backend import outcomes
from backend.contract import BACKGROUND_KINDS, CONTRACT, JOB_KINDS, LIMITS, STEP, handler_path

SECRET_ENV = ("GEMINI_API_KEY", "WAKE_SECRET", "WATCHLIST_JOB_TOKEN")
USER_AGENT = "rigged-outcomes-job"
RETRY_WAITS = (1, 3, 8)             # a Worker call's retries of a 429, a 5xx or a network error, while time is left
BODY_ROOM = 4096                    # kept free in a checkpoint or complete body for its other fields


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def redact(text, environ=None, limit=LIMITS["error_chars"], first_line=True):
    """Text with the values of GEMINI_API_KEY, WAKE_SECRET and WATCHLIST_JOB_TOKEN cut out, `key=` query values and
    bearer tokens cut, and (first_line) only its first line, at most `limit` characters."""
    environ = os.environ if environ is None else environ
    s = str(text)
    for name in SECRET_ENV:
        v = str(environ.get(name) or "")
        if len(v) >= 4:
            s = s.replace(v, "[redacted]")
    s = re.sub(r"(?i)\b(key=)[^&\s'\"]+", r"\1[redacted]", s)
    s = re.sub(r"(?i)\b(bearer\s+)[^\s'\"]+", r"\1[redacted]", s)
    if first_line:
        lines = [x for x in s.strip().splitlines() if x.strip()]
        s = lines[0].strip() if lines else ""
    return s[:limit] if limit else s


def _api_error(status, code, message=""):
    """watchlist.sync's ApiError, which watchlist.blobs and the daily run's code catch."""
    from watchlist.sync import ApiError
    return ApiError(status, code, message)


def _dumps(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _size(doc):
    return len(_dumps(doc).encode("utf-8"))


class WorkerClient:
    """The Worker's job API for one call: JSON in and out, the job token in one header, each try's timeout cut to
    the call's end, and a 429, a 5xx or a network error tried again after each of RETRY_WAITS while the call has
    time. request() returns the parsed body (None for 204) or raises watchlist.sync.ApiError, the same as
    watchlist.sync.Client, so watchlist.blobs works with it."""

    def __init__(self, base, token, end=None, session=None, timeout=STEP["worker_call_timeout_s"],
                 retry_waits=RETRY_WAITS, mono=time.monotonic, sleep=time.sleep):
        self.base = str(base).rstrip("/")
        self._token = token
        self.end, self.timeout, self.retry_waits = end, timeout, tuple(retry_waits)
        self.session = session or requests.Session()
        self.mono, self.sleep = mono, sleep

    def __repr__(self):
        return f"WorkerClient({self.base!r})"

    def _left(self):
        return None if self.end is None else self.end - self.mono()

    def request(self, method, path, body=None):
        data = None if body is None else _dumps(body).encode("utf-8")
        err = None
        for attempt in range(len(self.retry_waits) + 1):
            left = self._left()
            if attempt:
                wait = self.retry_waits[attempt - 1]
                if left is not None and left - wait < 1:
                    break
                self.sleep(wait)
                left = self._left()
            if left is not None and left < 1:
                break
            timeout = self.timeout if left is None else min(self.timeout, left)
            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                       "User-Agent": USER_AGENT}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                r = self.session.request(method, self.base + path, data=data, timeout=timeout, headers=headers)
            except requests.RequestException as e:
                err = _api_error(None, "network", type(e).__name__)
                continue
            if r.status_code < 300:
                if r.status_code == 204 or not (r.content or b"").strip():
                    return None
                try:
                    return r.json()
                except ValueError:
                    raise _api_error(r.status_code, "bad_response", "the answer was not JSON")
            try:
                payload = r.json()
                code, message = payload.get("error") or "error", payload.get("message") or ""
            except (ValueError, AttributeError):
                code, message = "error", ""
            err = _api_error(r.status_code, code, message)
            if not (r.status_code == 429 and code != "locked") and r.status_code < 500:
                raise err
        raise err or _api_error(None, "network", "no time left for the call")


class Outcome:
    """What a handler returns: ctx.done(...) or ctx.partial(...)."""

    def __init__(self, kind, cursor=None, results=(), stop_run=False):
        self.kind, self.cursor, self.results, self.stop_run = kind, cursor, list(results), bool(stop_run)


def _result_item(key, value):
    key = str(key)
    if not key or len(key) > LIMITS["result_key_chars"]:
        raise ValueError(f"a result key must be 1 to {LIMITS['result_key_chars']} characters: {key[:60]!r}")
    if _size(value) > LIMITS["result_value_bytes"]:
        raise ValueError(f"result {key[:60]!r} is over {LIMITS['result_value_bytes']:,} bytes: put it in a scratch "
                         f"blob (ctx.put_scratch)")
    return {"key": key, "value": value}


def _news_item(key, body):
    key = str(key)
    if not key or len(key) > LIMITS["news_key_chars"]:
        raise ValueError(f"a news key must be 1 to {LIMITS['news_key_chars']} characters: {key[:60]!r}")
    if _size(body) > LIMITS["news_body_bytes"]:
        raise ValueError(f"news {key[:60]!r} is over {LIMITS['news_body_bytes']:,} bytes")
    return {"key": key, "body": body}


def _pairs(items, make):
    """[{key, value|body}] from (key, value) pairs or dicts already in that shape."""
    out = []
    for it in items or ():
        if isinstance(it, dict):
            out.append(make(it["key"], it["value"] if "value" in it else it["body"]))
        else:
            out.append(make(*it))
    return out


def chunks(results, news, limit):
    """[(results, news)] each under limit bytes as a body, in order."""
    out, cur_r, cur_n, size = [], [], [], 0
    for kind, item in [("r", x) for x in results] + [("n", x) for x in news]:
        n = _size(item) + 1
        if (cur_r or cur_n) and size + n > limit:
            out.append((cur_r, cur_n))
            cur_r, cur_n, size = [], [], 0
        (cur_r if kind == "r" else cur_n).append(item)
        size += n
    if cur_r or cur_n or not out:
        out.append((cur_r, cur_n))
    return out


class StepContext:
    """What a handler gets (docs/BACKEND.md): ctx.job, ctx.budget, ctx.now(), ctx.deadline,
    ctx.left(), ctx.tmp, ctx.api, ctx.checkpoint(), ctx.give_way(), ctx.results(), ctx.news_get(), ctx.child(),
    ctx.meta(), ctx.units(), ctx.done(), ctx.partial(), and scratch blobs for data over a result's 200 KB."""

    def __init__(self, claim, api, deadline, tmp, mono=time.monotonic, log=_log):
        self.job = dict(claim["job"])
        self.job.setdefault("missing", [])
        self.budget = dict(claim.get("budget") or {})
        self.api, self.deadline, self.tmp = api, deadline, tmp
        self._mono, self._log = mono, log
        self._now0, self._mono0 = int(claim.get("now") or time.time()), mono()
        self._cursor = self.job.get("cursor")
        self._children, self._meta, self._units = [], {}, 0
        self._lock = threading.Lock()
        # the claim is the Worker's first answer: it hands out no background job while a foreground one is ready
        self._give_way, self._answered = False, self._mono0

    # ── the clock ──

    def now(self):
        """The Worker's clock: the claim's now plus the time since (handlers use this, never time.time())."""
        return self._now0 + int(self._mono() - self._mono0)

    def left(self):
        """Seconds left to the step's deadline."""
        return self.deadline - self._mono()

    # ── outcomes ──

    def done(self, results=(), stop_run=False):
        return Outcome("done", results=_pairs(results, _result_item), stop_run=stop_run)

    def partial(self, cursor, results=()):
        return Outcome("partial", cursor=cursor if cursor is not None else (self._cursor or {}),
                       results=_pairs(results, _result_item))

    @property
    def cursor(self):
        """The last cursor checkpointed in this step, else the job's own (None on a fresh job)."""
        return self._cursor

    # ── the Worker ──

    def _body(self, cursor, results, news):
        return {"job_id": self.job["id"], "lease_token": self.job["lease_token"], "cursor": cursor,
                "results": results, "news": news}

    def checkpoint(self, cursor=None, results=(), news=()):
        """Send results (key, value), news searches (key, headlines) and a cursor now, split under 256 KB a call;
        the cursor goes with the last part, so it never runs ahead of what was saved. cursor None keeps the last
        one. Each answer may ask a background step to give way (give_way()). Thread-safe. Raises Lost when the
        lease is gone."""
        rs, ns = _pairs(results, _result_item), _pairs(news, _news_item)
        with self._lock:
            new = self._cursor if cursor is None else cursor
            if new is not None and _size(new) > LIMITS["cursor_bytes"]:
                raise ValueError(f"the cursor is over {LIMITS['cursor_bytes']:,} bytes")
            parts = chunks(rs, ns, LIMITS["checkpoint_body_bytes"] - BODY_ROOM - _size(new))
            for i, (r, n) in enumerate(parts):
                last = i == len(parts) - 1
                self._post_checkpoint(self._body(new if last else self._cursor, r, n))
            self._cursor = new

    def _post_checkpoint(self, body):
        """One checkpoint call (under self._lock): 204, or 200 {"give_way": true}, which sets the flag for good."""
        try:
            got = self.api.request("POST", "/api/job/checkpoint", body)
        except Exception as e:
            if getattr(e, "status", None) == 409:
                raise outcomes.Lost(str(e))
            raise
        self._answered = self._mono()
        if isinstance(got, dict) and got.get("give_way") is True:
            self._give_way = True

    def give_way(self, every_s=None):
        """Whether a foreground job waits for this background step's lane or the heavy slot: the Worker's answer to
        its checkpoints ({"give_way": true}; once true, true for the rest of the step). every_s: when the last
        answer or ask is every_s seconds old or more (the claim counts as one), ask first with a checkpoint of the
        job and its lease only (a read: no cursor, no results). An ask the Worker does not answer is logged and the
        step goes on (it asks again every_s later), so only checkpoints that carry progress can fail a step. The
        step then stops at a safe point: ctx.partial, or raise GiveWay to go out from the last cursor. Thread-safe.
        Raises Lost when the lease is gone."""
        if every_s is not None and not self._give_way:
            with self._lock:
                if not self._give_way and self._mono() - self._answered >= every_s:
                    try:
                        self._post_checkpoint({"job_id": self.job["id"], "lease_token": self.job["lease_token"]})
                    except outcomes.Lost:
                        raise
                    except Exception as e:
                        self._answered = self._mono()
                        self._log(redact(f"{self.job['id']}: no answer on whether to give way ({e}); going on"))
        return self._give_way

    def results(self, prefix, run_id=None):
        """(key, value) of a run's results whose key starts with prefix (this run by default), by key."""
        after = None
        while True:
            q = {"run": run_id or self.job["run_id"], "prefix": prefix}
            if after:
                q["after"] = after
            got = self.api.request("GET", "/api/job/results?" + urllib.parse.urlencode(q)) or {}
            for r in got.get("results") or []:
                yield r["key"], r["value"]
            after = got.get("next")
            if not after:
                return

    def news_get(self, keys, max_age_s):
        """{key: headlines} of the news searches saved within max_age_s; keys not found are absent."""
        todo, out = list(dict.fromkeys(keys)), {}
        n = LIMITS["news_get_keys"]
        while todo:
            batch, todo = todo[:n], todo[n:]
            got = self.api.request("POST", "/api/job/news/get", {"keys": batch, "max_age_s": int(max_age_s)}) or {}
            for k, row in (got.get("news") or {}).items():
                out[k] = row.get("body") if isinstance(row, dict) else row
            more = [k for k in got.get("more") or [] if k in batch and k not in out]
            if more and len(more) == len(batch):
                raise ValueError("the Worker sent none of the news asked for")
            todo = more + todo
        return out

    def child(self, kind, part, input=None, after=(), run_after=None):
        """Spawn a job of this run, sent with a done outcome (at most children_max)."""
        if kind not in JOB_KINDS:
            raise ValueError(f"no job kind {kind!r}")
        if input is not None and _size(input) > LIMITS["input_bytes"]:
            raise ValueError(f"the input of {kind}|{part} is over {LIMITS['input_bytes']:,} bytes")
        self._children.append({"kind": kind, "part": str(part), "input": input or {}, "after": list(after),
                               "run_after": run_after})

    def meta(self, **kv):
        """run_meta sent with the outcome (merged into the run's meta)."""
        self._meta.update(kv)

    def units(self, n):
        """Ledger units this step: whales, searches or requests."""
        with self._lock:
            self._units += int(n)

    # ── scratch blobs (a job's data over a result's 200 KB) ──

    def scratch_name(self, name, run_id=None):
        h = hashlib.sha1(str(run_id or self.job["run_id"]).encode("utf-8")).hexdigest()[:16]
        return f"scratch:{h}:{name}"

    def put_scratch(self, name, data, run_id=None):
        """Save bytes as this run's scratch blob `name`; returns the reference a result can carry."""
        from watchlist import blobs
        blob = self.scratch_name(name, run_id)
        head = blobs.put(self.api, blob, data, {"job": self.job["id"]})
        return {"blob": blob, "version": head["version"]}

    def get_scratch(self, ref):
        """The bytes of a scratch reference (put_scratch's); None when it is gone or was saved again since."""
        from watchlist import blobs
        h, data = blobs.get(self.api, ref["blob"])
        if h is None or h.get("version") != ref.get("version"):
            return None
        return data


# ── the call ─────────────────────────────────────────────────────────────────

def _mem_mb():
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError):
        return 0
    return int(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024)


def tmp_dir(job, root=None):
    """The job's own empty folder: <tmp_root>/<sha1(job id)[:16]>-<lease token[:8]>."""
    root = root or STEP["tmp_root"]
    h = hashlib.sha1(str(job["id"]).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(root, f"{h}-{str(job.get('lease_token') or '')[:8]}")
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


def _polymarket_down(e):
    """Polymarket not answering (after polymarket_api's own retries): a network error, a 429 or a 5xx."""
    pm = sys.modules.get("pipeline.polymarket_api")
    if pm is None or not isinstance(e, pm.PolymarketAPIError) or isinstance(e, pm.RetiredEndpointError):
        return False
    status = getattr(e, "status", None)
    return status is None or status == 429 or status >= 500


def load_handler(kind):
    module, func = handler_path(kind)
    return getattr(importlib.import_module(module), func)


def lane_need_s(lane):
    """The longest need_s of a lane's foreground kinds (CONTRACT background_kinds left out)."""
    return max((k["need_s"] for name, k in JOB_KINDS.items() if k["lane"] == lane and name not in BACKGROUND_KINDS),
               default=0)


def run_job(claim, api, deadline, environ, mono=time.monotonic, cpu=time.process_time, tmp_root=None, log=_log):
    """Run one claimed job's handler and report it. Returns the outcome sent (None when the lease was lost)."""
    job = claim["job"]
    cpu0, wall0 = cpu(), mono()
    tmp = tmp_dir(job, tmp_root)
    ctx = StepContext(claim, api, deadline, tmp, mono=mono, log=log)
    body = {"job_id": job["id"], "lease_token": job["lease_token"], "outcome": None, "cursor": None,
            "run_after": None, "reason": None, "pause_lane_until": None, "error": None, "results": [],
            "children": [], "run_meta": {}, "stop_run": False}
    try:
        def call():
            outcomes.DEADLINE.set(deadline)
            outcomes.CLOCK.set(ctx.now)
            outcomes.JOB.set(outcomes.new_job_objects())
            return load_handler(job["kind"])(ctx)

        try:
            got = contextvars.copy_context().run(call)
            if got is None:
                got = ctx.done()
            if not isinstance(got, Outcome):
                raise TypeError(f"the handler returned {type(got).__name__}, not an outcome")
            body.update(outcome=got.kind, results=got.results)
            if got.kind == "partial":
                body["cursor"] = got.cursor
            else:
                body.update(children=ctx._children, run_meta=ctx._meta, stop_run=got.stop_run)
                if len(ctx._children) > LIMITS["children_max"]:
                    raise ValueError(f"{len(ctx._children)} children, over the {LIMITS['children_max']} a step "
                                     f"may spawn")
        except outcomes.Lost:
            log(f"{job['id']}: the lease is gone; the job is left to whoever holds it")
            return None
        except outcomes.Later as e:
            body.update(outcome="later", run_after=e.run_after, reason=e.reason, pause_lane_until=e.pause_lane_until,
                        results=[], children=[], run_meta={}, stop_run=False)
        except outcomes.OutOfTime:                 # GiveWay too: a foreground job waits for this background step
            body.update(outcome="partial", cursor=ctx.cursor if ctx.cursor is not None else {}, results=[],
                        children=[], run_meta={}, stop_run=False)
        except Exception as e:
            if _polymarket_down(e):
                body.update(outcome="later", run_after=ctx.now() + CONTRACT["retry"]["polymarket_down_s"],
                            reason="polymarket_down", results=[], children=[], run_meta={}, stop_run=False)
            else:
                body.update(outcome="error", error=redact(f"{type(e).__name__}: {e}", environ), results=[],
                            children=[], run_meta={}, stop_run=False)
                log(redact(traceback.format_exc(), environ, limit=20_000, first_line=False))
        body["usage"] = {"cpu_ms": int((cpu() - cpu0) * 1000), "wall_ms": int((mono() - wall0) * 1000),
                         "units": ctx._units, "mem_mb": _mem_mb()}
        # after a give-way the call claims again only with time for any job of the lane, else the job waiting
        # might not fit: the Worker then wakes the lane for a fresh call
        body["gave_way"] = ctx._give_way
        need = max(STEP["min_claim_left_s"], lane_need_s(JOB_KINDS[job["kind"]]["lane"]) if ctx._give_way else 0)
        body["continuing"] = deadline - mono() >= need
        if body["results"] and _size(body) > LIMITS["complete_body_bytes"] - BODY_ROOM:
            try:                                   # the results go ahead in checkpoints, the outcome after them
                ctx.checkpoint(results=body["results"])
            except outcomes.Lost:
                log(f"{job['id']}: the lease is gone; the job is left to whoever holds it")
                return None
            body["results"] = []
        try:
            got = api.request("POST", "/api/job/complete", body)
        except Exception as e:
            if getattr(e, "status", None) == 409:
                log(f"{job['id']}: the lease was gone at the end; the step's outcome is dropped")
                return None
            log(redact(f"{job['id']}: the complete could not be sent ({e}); the lease will expire and the job "
                       f"go again", environ))
            return None
        log(f"{job['id']}: {body['outcome']}" + (f" ({body['reason']})" if body["reason"] else "")
            + (f": {body['error']}" if body["error"] else "") + f", {body['usage']['wall_ms'] / 1000:.1f} s"
            + (" (repeated)" if (got or {}).get("repeated") else ""))
        return body
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def serve(lane, t0, environ=None, cpu0=None, session=None, mono=time.monotonic, cpu=time.process_time,
          tmp_root=None, log=_log):
    """One call for `lane`: claim and run jobs until the deadline. Returns {"lane", "jobs"}."""
    environ = os.environ if environ is None else environ
    cpu0 = cpu() if cpu0 is None else cpu0
    base, token = (environ.get("WATCHLIST_API_URL") or "").strip(), (environ.get("WATCHLIST_JOB_TOKEN") or "").strip()
    if not base or not token:
        log("no API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN): nothing claimed")
        return {"lane": lane, "jobs": 0}
    end = t0 + STEP["function_max_s"] - STEP["cold_start_s"]
    deadline = end - STEP["reserve_s"]
    api = WorkerClient(base, token, end=end, session=session, mono=mono)
    call = secrets.token_hex(8)
    tmp_root = tmp_root or environ.get("RO_TMP_ROOT") or STEP["tmp_root"]
    jobs, mark_cpu, mark_wall, reported = 0, cpu0, t0, False

    def spent():
        return {"cpu_ms": int((cpu() - mark_cpu) * 1000), "wall_ms": int((mono() - mark_wall) * 1000)}

    while True:
        left = deadline - mono()
        if left < STEP["min_claim_left_s"]:
            break
        body = {"lane": lane, "left_s": int(left), "call": call, "spent": spent()}
        try:
            claim = api.request("POST", "/api/job/claim", body)
        except Exception as e:
            log(redact(f"claim on {lane} failed: {e}", environ))
            reported = True
            break
        mark_cpu, mark_wall = cpu(), mono()
        if claim is None or not claim.get("job"):
            reported = True
            break
        sent = run_job(claim, api, deadline, environ, mono=mono, cpu=cpu, tmp_root=tmp_root, log=log)
        jobs += 1
        mark_cpu, mark_wall = cpu(), mono()
        if sent is not None and not sent["continuing"]:
            break                               # the Worker was told so, and wakes the lane for a fresh call
    if not reported:                            # the call's last seconds go to the ledger too
        try:
            api.request("POST", "/api/job/claim", {"lane": lane, "left_s": 0, "call": call, "spent": spent()})
        except Exception as e:
            log(redact(f"the last report on {lane} failed: {e}", environ))
    return {"lane": lane, "jobs": jobs}
