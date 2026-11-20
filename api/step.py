"""
The Vercel step function (docs/BACKEND.md): POST /api/step, woken by the Worker.

The wake-up's signature is checked first, with the standard library only (backend.wake). A call that fails it is
answered 401 {} and does nothing else. Only then is backend.runtime imported, which claims the lane's jobs from
the Worker and runs them until the call's deadline; pandas and the handlers load per job. The answer goes out at
the end, 200 {"lane", "jobs"}. The Worker has stopped waiting by then: Python keeps running after its caller
leaves, which is what the wake-up relies on.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import wake  # noqa: E402  (standard library only)

HEADER = "x-ro-wake"


def run(header, t0, environ=None, cpu0=None):
    """(status, body) for one call: 401 {} unless the header verifies, else the runtime's result."""
    environ = os.environ if environ is None else environ
    lane = wake.verify(header, environ.get("WAKE_SECRET", ""))
    if lane is None:
        return 401, {}
    from backend import runtime
    return 200, runtime.serve(lane, t0, environ=environ, cpu0=cpu0)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        t0, cpu0 = time.monotonic(), time.process_time()
        status, body = run(self.headers.get(HEADER), t0, cpu0=cpu0)
        self._answer(status, body)

    def do_GET(self):
        self._answer(405, {})

    def _answer(self, status, body):
        data = json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):      # the Worker stopped waiting long ago
            pass

    def log_message(self, fmt, *args):                       # no request lines in Vercel's log
        pass
