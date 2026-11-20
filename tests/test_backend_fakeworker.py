#!/usr/bin/env python3
"""
A stand-in for the Worker's job API (docs/BACKEND.md) for the backend's offline tests: the queue
(claim, checkpoint, complete, results, news), the job endpoints the add steps call (work, done, catalog), and
the blob endpoints (tests/fake_api.FakeApi, with the backend's blob names). Kept in memory, with the contract's
rules: one live lease per lane, need_s, barriers (failed and cancelled count as finished, and come back as
`missing`), the heavy rule, outcomes and their counters, body caps. FakeSession puts it behind
backend.runtime.WorkerClient, so the tests go through the real HTTP client code.

    python3 tests/test_backend_fakeworker.py      # the fake's own checks
"""
import copy
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_api  # noqa: E402
from backend.contract import CONTRACT, JOB_KINDS, LIMITS, RUN_GRAPHS  # noqa: E402
from watchlist import sync  # noqa: E402

NAMES = re.compile("|".join(f"(?:{p})" for p in CONTRACT["blob_names"].values()))
BUDGET = {"level": "ok", "add_whales_left": 800, "open_searches": 300, "open_reads": 30, "open_out": 60}


class Blobs(fake_api.FakeApi):
    """fake_api.FakeApi with the backend's blob names (state, site:*, market:*, scratch:*)."""

    _swap = threading.Lock()

    def request(self, method, path, body=None):
        with Blobs._swap:
            saved = fake_api.BLOB_NAME
            fake_api.BLOB_NAME = NAMES
            try:
                return super().request(method, path, body)
            finally:
                fake_api.BLOB_NAME = saved


class FakeWorker:
    def __init__(self, now=None):
        self.offset = 0 if now is None else int(now - time.time())
        self.runs, self.jobs, self.results, self.news = {}, {}, {}, {}
        self.budget = dict(BUDGET)
        self.blobs = Blobs()
        self.requests, self.done, self.catalog, self.calls = [], [], [], []
        self.claims, self.completes, self.checkpoints = [], [], []
        self.results_page = 500
        self.lock = threading.RLock()
        self.fail = {}                           # path -> [ApiError, ...] raised on the next calls

    def now(self):
        return int(time.time()) + self.offset

    # ── runs and jobs ──

    def add_run(self, kind, slot, input=None, deadline_s=36 * 3600):
        rid = f"{kind}:{slot}"
        self.runs[rid] = {"id": rid, "kind": kind, "slot": slot, "state": "queued", "meta": {},
                          "deadline": self.now() + deadline_s}
        for g in RUN_GRAPHS[kind]:
            self._insert(rid, g["kind"], "0", input or {}, g["after"])
        return rid

    def _insert(self, rid, kind, part, input, after, run_after=None):
        jid = f"{rid}|{kind}|{part}"
        if jid in self.jobs:
            return
        spec = JOB_KINDS[kind]
        self.jobs[jid] = {"id": jid, "run_id": rid, "kind": kind, "part": str(part), "lane": spec["lane"],
                          "heavy": spec["heavy"], "need_s": spec["need_s"], "final": spec["final"],
                          "after": list(after), "state": "queued", "run_after": run_after or 0, "attempt": 0,
                          "laters": 0, "partials": 0, "lease_token": None, "lease_until": None,
                          "input": copy.deepcopy(input or {}), "cursor": None, "error": None, "seq": len(self.jobs)}

    def job(self, kind, part="0", run=None):
        return next(j for j in self.jobs.values() if j["kind"] == kind and j["part"] == str(part)
                    and (run is None or j["run_id"] == run))

    def _blocking(self, j):
        return [d for d in self.jobs.values() if d["run_id"] == j["run_id"] and d["kind"] in j["after"]
                and d["state"] in ("queued", "leased")]

    def claim(self, lane, left_s):
        now = self.now()
        live = [j for j in self.jobs.values() if j["state"] == "leased" and j["lease_until"] > now]
        if any(j["lane"] == lane for j in live):
            return None
        ready = [j for j in self.jobs.values()
                 if j["state"] == "queued" and j["lane"] == lane and j["run_after"] <= now and j["need_s"] <= left_s
                 and self.runs[j["run_id"]]["state"] in ("queued", "running") and not self._blocking(j)
                 and not (j["heavy"] and any(x["heavy"] for x in live))]
        if not ready:
            return None
        j = min(ready, key=lambda x: (CONTRACT["run_priority"][self.runs[x["run_id"]]["kind"]], x["run_after"],
                                      x["seq"]))
        j.update(state="leased", lease_token=secrets.token_hex(16),
                 lease_until=now + left_s + CONTRACT["step"]["lease_slack_s"])
        run = self.runs[j["run_id"]]
        if run["state"] == "queued":
            run["state"] = "running"
        missing = [{"kind": d["kind"], "part": d["part"], "state": d["state"], "error": d["error"]}
                   for d in self.jobs.values() if d["run_id"] == j["run_id"] and d["kind"] in j["after"]
                   and d["state"] in ("failed", "cancelled")]
        out = {k: copy.deepcopy(j[k]) for k in ("id", "run_id", "kind", "part", "input", "cursor", "attempt", "laters",
                                                "partials", "lease_token", "lease_until")}
        out.update(run_kind=run["kind"], run_slot=run["slot"], run_deadline=run["deadline"],
                   run_meta=copy.deepcopy(run["meta"]), missing=missing)
        return {"now": now, "job": out, "budget": dict(self.budget)}

    def _leased(self, body):
        j = self.jobs.get(body.get("job_id"))
        if j is None or j["lease_token"] != body.get("lease_token"):
            raise sync.ApiError(409, "lost")
        return j

    def _save_results(self, j, results):
        for r in results or []:
            assert len(r["key"]) <= LIMITS["result_key_chars"]
            assert len(json.dumps(r["value"])) <= LIMITS["result_value_bytes"]
            self.results[(j["run_id"], r["key"])] = (j["id"], copy.deepcopy(r["value"]))

    def checkpoint(self, body):
        assert len(json.dumps(body).encode()) <= LIMITS["checkpoint_body_bytes"], "checkpoint body over 256 KB"
        j = self._leased(body)
        if j["state"] != "leased" or j["lease_until"] <= self.now():
            raise sync.ApiError(409, "lost")
        self.checkpoints.append(copy.deepcopy(body))
        self._save_results(j, body.get("results"))
        for n in body.get("news") or []:
            self.news[n["key"]] = {"day": "today", "fetched_at": self.now(), "body": copy.deepcopy(n["body"])}
        j["cursor"] = copy.deepcopy(body.get("cursor"))

    def complete(self, body):
        assert len(json.dumps(body).encode()) <= LIMITS["complete_body_bytes"], "complete body over 256 KB"
        j = self._leased(body)
        if j["state"] != "leased":
            return {"ok": True, "state": j["state"], "repeated": True}
        self.completes.append(copy.deepcopy(body))
        now, r = self.now(), CONTRACT["retry"]
        out = body["outcome"]
        if out == "done":
            j["state"] = "done"
            self._save_results(j, body.get("results"))
            assert len(body.get("children") or []) <= LIMITS["children_max"]
            for c in body.get("children") or []:
                self._insert(j["run_id"], c["kind"], c["part"], c.get("input"), c.get("after") or [],
                             c.get("run_after"))
            self.runs[j["run_id"]]["meta"].update(body.get("run_meta") or {})
            if body.get("stop_run"):
                for d in self.jobs.values():
                    if d["run_id"] == j["run_id"] and d["state"] in ("queued", "leased") and d is not j:
                        d.update(state="cancelled", error="the run was stopped")
        elif out == "partial":
            assert body.get("cursor") is not None, "a partial needs its cursor"
            j.update(state="queued", run_after=now, cursor=copy.deepcopy(body["cursor"]), partials=j["partials"] + 1)
            if j["partials"] > r["partials_max"]:
                j.update(state="failed", error="no end in sight")
        elif out == "later":
            assert body.get("reason") in CONTRACT["later_reasons"], body.get("reason")
            j.update(state="queued", run_after=min(int(body["run_after"]), now + 2 * 86400), laters=j["laters"] + 1)
            if j["laters"] > r["laters_max"]:
                j.update(state="failed", error=f"waited {r['laters_max']} times: {body['reason']}")
        else:
            j.update(attempt=j["attempt"] + 1, error=body.get("error"))
            j.update(state="failed" if j["attempt"] >= r["max_attempts"] else "queued",
                     run_after=now + min(r["base_s"] * 2 ** (j["attempt"] - 1), r["cap_s"]))
        run = self.runs[j["run_id"]]
        if all(d["state"] in CONTRACT["job_terminal"] for d in self.jobs.values() if d["run_id"] == run["id"]):
            states = {d["state"] for d in self.jobs.values() if d["run_id"] == run["id"]}
            run["state"] = "done" if states == {"done"} else "partial"
        return {"ok": True, "state": j["state"], "repeated": False}

    # ── the API ──

    def request(self, method, path, body=None):
        with self.lock:
            self.calls.append((method, path))
            url = urllib.parse.urlsplit(path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            for p in list(self.fail):
                if url.path == p and self.fail[p]:
                    raise self.fail[p].pop(0)
            if url.path.startswith("/api/job/blobs"):
                return self.blobs.request(method, path, body)
            if url.path == "/api/job/claim":
                assert len(json.dumps(body)) <= LIMITS["claim_body_bytes"]
                self.claims.append(copy.deepcopy(body))
                return self.claim(body["lane"], body["left_s"])
            if url.path == "/api/job/checkpoint":
                return self.checkpoint(body)
            if url.path == "/api/job/complete":
                return self.complete(body)
            if url.path == "/api/job/results":
                rows = sorted((k, v) for (rid, k), v in self.results.items()
                              if rid == q["run"] and k.startswith(q.get("prefix", "")) and k > q.get("after", ""))
                page = rows[:self.results_page]
                nxt = page[-1][0] if len(rows) > len(page) else None
                return {"results": [{"key": k, "job_id": v[0], "value": copy.deepcopy(v[1])} for k, v in page],
                        "next": nxt}
            if url.path == "/api/job/news/get":
                assert len(body["keys"]) <= LIMITS["news_get_keys"]
                found = {k: self.news[k] for k in body["keys"] if k in self.news}
                return {"news": copy.deepcopy(found), "more": []}
            if url.path == "/api/job/work":
                return {"now": self.now(), "decisions": [], "requests": copy.deepcopy(self.requests), "watched": [],
                        "added": []}
            if url.path == "/api/job/done":
                self.done.append(copy.deepcopy(body))
                return {"ok": True}
            if url.path == "/api/job/catalog":
                self.catalog.append(copy.deepcopy(body))
                return {"market": len(body.get("markets") or [])}
            raise sync.ApiError(404, "not_found")


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self.content = b"" if payload is None else json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """A requests.Session that answers from a FakeWorker (what WorkerClient sends: method, URL, data, headers)."""

    def __init__(self, worker, token="job-token"):
        self.worker, self.token, self.sent = worker, token, []

    def request(self, method, url, data=None, timeout=None, headers=None):
        assert timeout is not None and timeout > 0
        assert headers["Authorization"] == f"Bearer {self.token}" and headers["User-Agent"] == "rigged-outcomes-job"
        path = url.split("://", 1)[1].split("/", 1)[1]
        body = json.loads(data) if data else None
        self.sent.append((method, "/" + path, len(data or b"")))
        try:
            got = self.worker.request(method, "/" + path, body)
        except sync.ApiError as e:
            return _Response(e.status or 500, {"error": e.code, "message": ""})
        return _Response(204, None) if got is None else _Response(200, got)


ENV = {"WATCHLIST_API_URL": "http://fake-worker.test", "WATCHLIST_JOB_TOKEN": "job-token",
       "WAKE_SECRET": "wake-secret-value", "GEMINI_API_KEY": "gemini-key-value"}


def serve(worker, lane, late_s=0, environ=None, handlers=None, tmp_root=None):
    """One step-function call on `lane` against the fake (late_s: the call started that many seconds ago).
    handlers: {kind: function} instead of the contract's."""
    from backend import runtime
    env = dict(ENV, **(environ or {}))
    orig = runtime.load_handler
    if handlers is not None:
        runtime.load_handler = lambda kind: handlers[kind] if kind in handlers else orig(kind)
    try:
        return runtime.serve(lane, time.monotonic() - late_s, environ=env, session=FakeSession(worker),
                             tmp_root=tmp_root or os.path.join(SCRATCH, "ro"), log=lambda m: None)
    finally:
        runtime.load_handler = orig


SCRATCH = os.environ.get("RO_TEST_TMP") or os.path.join(tempfile.gettempdir(), "ro-backend-tests")


# ── the fake's own checks ────────────────────────────────────────────────────

def test_claim_follows_the_contract():
    w = FakeWorker()
    rid = w.add_run("add", "0xabc:1")
    assert w.claim("add", 30) is None                                  # add.fetch needs 120 s
    got = w.claim("add", 200)
    assert got["job"]["kind"] == "add.fetch" and got["job"]["run_slot"] == "0xabc:1"
    assert w.claim("add", 200) is None                                 # one live lease per lane
    w.complete({"job_id": got["job"]["id"], "lease_token": got["job"]["lease_token"], "outcome": "done",
                "children": [{"kind": "add.whales", "part": "0", "input": {"whales": []}}]})
    nxt = w.claim("add", 200)
    assert nxt["job"]["kind"] == "add.whales"                          # add.export waits on the batch
    w.complete({"job_id": nxt["job"]["id"], "lease_token": nxt["job"]["lease_token"], "outcome": "error",
                "error": "x"})
    w.job("add.whales").update(attempt=6, state="failed")
    last = w.claim("add", 200)
    assert last["job"]["kind"] == "add.export" and last["job"]["missing"][0]["kind"] == "add.whales"
    assert w.runs[rid]["state"] == "running"


if __name__ == "__main__":
    test_claim_follows_the_contract()
    print("PASS test_claim_follows_the_contract")
