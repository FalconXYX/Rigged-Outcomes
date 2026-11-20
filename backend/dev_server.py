"""
A local /api/step for development and the end-to-end harness: api/step.py's own handler, served with the standard
library the way Vercel's Fluid compute serves it, and every call stopped at the platform's 300 s whatever it is doing.

    python3 -m backend.dev_server [--host 127.0.0.1] [--port 9600] [--max-s 300] [--concurrency 4] [--idle-s 120]
                                  [--control]

Point the Worker's STEP_URL at http://127.0.0.1:<port>/api/step (DEV=on). As on Vercel:

  - A request without a well-formed x-ro-wake header is refused at the edge (403, vercel.json's rule, read from
    there) and never invoked.
  - Calls run in instances: a Python process serving api/step.py's handler class on threads, up to --concurrency
    calls at once, so several calls share one process and its modules (Fluid compute). A new instance starts when
    every live one is full (a cold start); one left idle --idle-s is stopped.
  - A call still going at --max-s is stopped: its instance is killed, with every call in it (the harsher side of
    what the platform does), and the call is answered 504 FUNCTION_INVOCATION_TIMEOUT.
  - The call keeps running after the Worker stops waiting for its answer.

Each call's outcome is one JSON line on standard output. The calls see this process's environment: WAKE_SECRET,
WATCHLIST_API_URL and WATCHLIST_JOB_TOKEN, as on Vercel.

--control adds the harness's switches (tests/end-to-end/lib/vercel.mjs), never on Vercel:

  POST /__dev/fault {"status": 503, "error": "DEPLOYMENT_PAUSED", "count": n}   answer wake-ups so, invoking nothing
  POST /__dev/fault {"drop": n}          the next n wake-ups are lost: held 10 s unanswered, then cut, nothing runs
  POST /__dev/fault {}                   no fault
  POST /__dev/kill                       kill every instance now (a step killed part way)
  POST /__dev/max {"max_s": 40}          change the per-call limit
  GET  /__dev/calls                      every call so far, and the live instances
"""
import argparse
import http.client
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = "x-ro-wake"


def _edge_rule():
    """vercel.json's edge rule for /api/step: the x-ro-wake value a request must carry to reach the function."""
    with open(os.path.join(ROOT, "vercel.json")) as f:
        routes = json.load(f)["routes"]
    rule = next(r for r in routes if r["src"] == "^/api/step$")
    return re.compile(next(m["value"] for m in rule["missing"] if m["key"] == HEADER))


EDGE = _edge_rule()
DROP_HOLD_S = 10                   # a lost wake-up is held this long unanswered (the Worker gives up at 8 s)


def _instance():
    """Instance mode: api/step.py's handler on threads, on a free port; the port is the first line on stdout."""
    sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location("step", os.path.join(ROOT, "api", "step.py"))
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    server = ThreadingHTTPServer(("127.0.0.1", 0), step.handler)
    server.daemon_threads = True
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    server.serve_forever()


class Instance:
    """One instance process and the calls it is running."""

    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "backend.dev_server", "--instance"], cwd=ROOT,
                                     stdout=subprocess.PIPE, start_new_session=True)
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the instance did not start")
        self.port = json.loads(line)["port"]
        self.inflight, self.idle_since, self.killed = 0, time.monotonic(), False
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):                               # anything a call prints on stdout goes to the log
        for line in self.proc.stdout:
            sys.stderr.buffer.write(line)
            sys.stderr.flush()

    def alive(self):
        return not self.killed and self.proc.poll() is None

    def kill(self):
        self.killed = True
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class Fluid:
    """The instances, the per-call limit, the harness's fault and the call log."""

    def __init__(self, max_s, concurrency, idle_s):
        self.max_s, self.concurrency, self.idle_s = float(max_s), int(concurrency), float(idle_s)
        self.lock = threading.Lock()
        self.instances, self.calls, self.fault = [], [], {}
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self):
        while True:
            time.sleep(2)
            with self.lock:
                for inst in list(self.instances):
                    if not inst.alive() or (inst.inflight == 0 and time.monotonic() - inst.idle_since > self.idle_s):
                        inst.kill()
                        self.instances.remove(inst)

    def acquire(self):
        with self.lock:
            for inst in self.instances:
                if inst.alive() and inst.inflight < self.concurrency:
                    inst.inflight += 1
                    return inst, False
        inst = Instance()                           # a cold start
        with self.lock:
            inst.inflight += 1
            self.instances.append(inst)
        return inst, True

    def release(self, inst):
        with self.lock:
            inst.inflight -= 1
            inst.idle_since = time.monotonic()

    def kill_all(self):
        with self.lock:
            killed = [(i.proc.pid, i.inflight) for i in self.instances if i.alive()]
            for i in self.instances:
                i.kill()
            self.instances = []
        return {"killed": len(killed), "calls": sum(n for _, n in killed)}

    def take_fault(self):
        """The fault for this wake-up ({} for none), counted down."""
        with self.lock:
            f = dict(self.fault)
            if not f:
                return {}
            key = "drop" if "drop" in f else "count"
            if f.get(key) is not None:
                self.fault[key] = int(f[key]) - 1
                if self.fault[key] <= 0:
                    self.fault = {}
            return f

    def invoke(self, header):
        """(status, body, record) for one call of the function."""
        started, t0 = time.time(), time.monotonic()
        inst, cold = self.acquire()
        timer = threading.Timer(self.max_s, inst.kill)
        timer.daemon = True
        timer.start()
        record = {"at": round(started, 3), "lane": (header.split(",") + ["", "", ""])[2], "pid": inst.proc.pid,
                  "cold": cold}
        try:
            conn = http.client.HTTPConnection("127.0.0.1", inst.port, timeout=self.max_s + 30)
            conn.request("POST", "/api/step", body=b"", headers={HEADER: header, "content-length": "0"})
            res = conn.getresponse()
            status, data = res.status, res.read()
            body = json.loads(data or b"{}")
        except (OSError, http.client.HTTPException, ValueError):
            over = time.monotonic() - t0 >= self.max_s - 0.5
            status, body = (504, {"error": "FUNCTION_INVOCATION_TIMEOUT"}) if over else \
                (500, {"error": "FUNCTION_INVOCATION_FAILED"})
        finally:
            timer.cancel()
            self.release(inst)
        record.update(s=round(time.monotonic() - t0, 1), status=status, jobs=body.get("jobs"))
        with self.lock:
            self.calls.append(record)
        return status, body, record


def make_handler(fluid, control):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if control and path.startswith("/__dev/"):
                return self._control(path)
            if path != "/api/step":
                return self._answer(404, {})
            header = self.headers.get(HEADER)
            if not header or not EDGE.search(header):         # vercel.json's edge rule: never reaches the function
                return self._answer(403, {})
            fault = fluid.take_fault() if control else {}
            if "drop" in fault:                               # lost: nothing runs, nothing is answered
                self._log({"at": round(time.time(), 3), "lost": True})
                time.sleep(DROP_HOLD_S)
                self.close_connection = True
                return
            if fault.get("status"):
                self._log({"at": round(time.time(), 3), "status": fault["status"], "fault": fault.get("error")})
                return self._answer(int(fault["status"]), {"error": {"code": fault.get("error") or "FAULT"}})
            status, body, record = fluid.invoke(header)
            self._log(record)
            self._answer(status, body)

        def do_GET(self):
            if control and self.path.split("?", 1)[0] == "/__dev/calls":
                with fluid.lock:
                    return self._answer(200, {"calls": list(fluid.calls), "fault": fluid.fault, "max_s": fluid.max_s,
                                              "instances": [{"pid": i.proc.pid, "calls": i.inflight}
                                                            for i in fluid.instances if i.alive()]})
            self._answer(405, {})

        def _control(self, path):
            n = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._answer(400, {})
            if path == "/__dev/fault":
                with fluid.lock:
                    fluid.fault = {k: body[k] for k in ("status", "error", "count", "drop") if k in body}
                return self._answer(200, {"fault": fluid.fault})
            if path == "/__dev/kill":
                return self._answer(200, fluid.kill_all())
            if path == "/__dev/max":
                fluid.max_s = float(body.get("max_s") or 300)
                return self._answer(200, {"max_s": fluid.max_s})
            return self._answer(404, {})

        def _log(self, record):
            print(json.dumps(record), flush=True)

        def _answer(self, status, body):
            data = json.dumps(body).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):  # the Worker stopped waiting
                pass

        def log_message(self, fmt, *args):
            pass

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="A local /api/step (the Vercel function, Fluid-like), each call stopped "
                                             "at 300 s.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9600)
    ap.add_argument("--max-s", type=float, default=300.0, help="stop a call after this many seconds")
    ap.add_argument("--concurrency", type=int, default=4, help="calls one instance runs at once")
    ap.add_argument("--idle-s", type=float, default=120.0, help="stop an instance idle this long")
    ap.add_argument("--control", action="store_true", help="the harness's /__dev/ switches")
    ap.add_argument("--instance", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.instance:
        return _instance()
    fluid = Fluid(args.max_s, args.concurrency, args.idle_s)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(fluid, args.control))
    server.daemon_threads = True
    print(json.dumps({"listening": f"http://{args.host}:{args.port}/api/step"}), flush=True)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        fluid.kill_all()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
