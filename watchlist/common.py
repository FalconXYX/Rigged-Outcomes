"""
Shared paths and time helpers for the watchlist package.

Gamma serves times in three shapes: "2026-11-10T00:00:00Z" (endDate),
"2026-11-10 02:59:12.404822+00" (closedTime, a Postgres timestamp with a
bare "+00" offset that datetime.fromisoformat rejects before 3.11 and that
other parsers misread) and a bare "2026-11-10" (some endDate/umaEndDate).
Everything here turns them into UTC epoch seconds, so comparisons never mix
strings and datetimes. Days are UTC days (epoch // 86400), which is also how
/v2/user-volume floors its bounds.
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
DB_PATH = os.path.join(DATA_DIR, "watchlist.db")

DAY = 86_400

# The legacy modules (polymarket_api, scrape_insiders, …) live at the repo
# root; make them importable however the package is entered.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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


class Budget:
    """A wall-clock allowance for resumable scripts (--budget-seconds): check
    `budget.expired()` between units of work and stop cleanly; the next run
    resumes from the store. seconds=None never expires."""

    def __init__(self, seconds=None):
        self.deadline = None if seconds is None else time.monotonic() + float(seconds)

    def left(self):
        return float("inf") if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def expired(self, reserve=0.0):
        """True once less than `reserve` seconds remain."""
        return self.deadline is not None and self.left() <= reserve
