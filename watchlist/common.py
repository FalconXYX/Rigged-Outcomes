"""
Shared paths and time helpers for the watchlist package.

Gamma serves times in three shapes: "2026-11-10T00:00:00Z" (endDate),
"2026-11-10 02:59:12.404822+00" (closedTime, a Postgres timestamp with a
bare "+00" offset that datetime.fromisoformat rejects before 3.11 and that
other parsers misread) and a bare "2026-11-10" (some endDate/umaEndDate).
Everything here turns them into epoch seconds, so comparisons never mix
strings and datetimes. The lists' days are Toronto calendar days
(local_day, backend.contract: docs/BACKEND.md); utc_day stays for
/v2/user-volume, which floors its bounds to UTC days.

A step on Vercel runs to a deadline (backend.outcomes.DEADLINE, a
time.monotonic() value): step_left() is the time to it, and out_of_time()
raises the runtime's OutOfTime. Outside a step there is no deadline.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data", "watchlist")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
DEMO_DIR = os.path.join(DATA_DIR, "demo")
# WATCHLIST_DB points every module at another store (a test copy, say); WATCHLIST_JOB_DIR moves the files the
# daily job's steps leave for each other (work.json, the results, the state stamps; watchlist.sync, .state)
DB_PATH = os.environ.get("WATCHLIST_DB") or os.path.join(DATA_DIR, "watchlist.db")
JOB_DIR = os.environ.get("WATCHLIST_JOB_DIR") or DATA_DIR
SITE_DATA_DIR = os.path.join(ROOT, "web", "public", "data")

DAY = 86_400

# The legacy modules (polymarket_api, scrape_insiders, …) live at the repo
# root; make them importable however the package is entered.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import contract as _contract  # noqa: E402  (standard library only)

TZ = _contract.TZ

_BARE_OFFSET = re.compile(r"([+-]\d\d)$")


def log(msg):
    print(msg, flush=True)


def to_epoch(value):
    """UTC epoch seconds (int) for a Gamma/Data time value, or None.

    Accepts epoch numbers (int/float/numeric strings), ISO strings with "Z",
    "+00", "+00:00" or no offset (taken as UTC), a space instead of "T", and
    bare dates. Anything unparseable is None, never an exception: a missing
    endDate is common and callers decide what that means."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return int(float(s))
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    s = _BARE_OFFSET.sub(r"\1:00", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iso(epoch):
    """ISO-8601 UTC string ("2026-11-10T02:59:12+00:00") for epoch seconds, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def utc_day(epoch):
    """UTC day number (epoch // 86400) of epoch seconds."""
    return int(epoch) // DAY


def day_str(day_or_epoch, is_day=True):
    """YYYY-MM-DD for a UTC day number (or for epoch seconds with is_day=False)."""
    d = int(day_or_epoch) if is_day else utc_day(day_or_epoch)
    return datetime.fromtimestamp(d * DAY, tz=timezone.utc).strftime("%Y-%m-%d")


def now_epoch():
    return int(datetime.now(timezone.utc).timestamp())


# ── Toronto days (every day the lists keep) ─────────────────────────────────

def local_day(epoch):
    """Toronto's calendar day, "YYYY-MM-DD", of epoch seconds."""
    return _contract.toronto_day(epoch)


def local_bounds(day):
    """(start, end) epoch seconds of a Toronto day: 23 or 25 hours on a change of clocks."""
    return _contract.toronto_bounds(day)


def local_start(epoch):
    """Epoch seconds of the Toronto midnight that starts epoch's day."""
    return local_bounds(local_day(epoch))[0]


def local_iso(epoch):
    """ISO 8601 with Toronto's offset ("2026-11-19T04:12:00-05:00"), or None: the published files' stamps."""
    return None if epoch is None else _contract.toronto_iso(epoch)


def day_shift(day, n):
    """The Toronto day n days after (before, for n < 0) a "YYYY-MM-DD" day."""
    from datetime import date, timedelta
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def days_from(first, last):
    """Every "YYYY-MM-DD" day from first to last, both included."""
    out, d = [], first
    while d <= last:
        out.append(d)
        d = day_shift(d, 1)
    return out


# ── the step's deadline (Vercel) ────────────────────────────────────────────

def step_left():
    """Seconds left to the running step's deadline, or None outside a step."""
    from backend import outcomes
    return outcomes.left()


def out_of_time(what):
    """Raise the runtime's OutOfTime: what cannot end before the step's deadline does not start."""
    from backend.outcomes import OutOfTime
    raise OutOfTime(what)


def clamp_timeout(timeout, what, floor=5.0):
    """A request's timeout clamped to the step's deadline (unchanged outside a step); OutOfTime when fewer than
    floor seconds would be left for it."""
    from backend import outcomes
    left = outcomes.left()
    if left is not None and left < floor:
        out_of_time(f"{what}: {max(0.0, left):.0f} s left to the step's deadline")
    return outcomes.clamp(timeout, floor)


def step_sleep(seconds, what, sleep=time.sleep):
    """sleep(seconds), or OutOfTime at once when the wait would run past the step's deadline."""
    from backend import outcomes
    left = outcomes.left()
    if left is not None and left - seconds < outcomes.MIN_REQUEST_S:
        out_of_time(f"{what}: a wait of {seconds:.0f} s does not fit in the {max(0.0, left):.0f} s left")
    sleep(seconds)


def submit(pool, fn, *args, **kwargs):
    """pool.submit with the caller's context copied into the thread (the step's deadline, clock and objects)."""
    import contextvars
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)


class Budget:
    """A wall-clock allowance for resumable scripts (--budget-seconds): check
    `budget.expired()` between units of work and stop cleanly; the next run
    resumes from the store. seconds=None never expires. clock: what tells
    the time (a test's fake clock)."""

    def __init__(self, seconds=None, clock=time.monotonic):
        self.seconds, self.clock = seconds, clock
        self.deadline = None if seconds is None else clock() + float(seconds)

    def left(self):
        return float("inf") if self.deadline is None else max(0.0, self.deadline - self.clock())

    def expired(self, reserve=0.0):
        """True once less than `reserve` seconds remain."""
        return self.deadline is not None and self.left() <= reserve
