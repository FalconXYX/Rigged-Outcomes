"""
How a step handler ends other than by returning, and the step's clock (docs/BACKEND.md). Standard
library only, so watchlist/net.py, news.py, ai.py and pipeline/polymarket_api.py can raise these without importing the
runtime.

  Later(run_after, reason, pause_lane_until=None)   come back at run_after (epoch seconds, the Worker's clock)
  OutOfTime                                         the step's deadline: it goes out as partial, from the last cursor
  GiveWay                                           an OutOfTime: a foreground job waits for this background step's
                                                    lane or the heavy slot (StepContext.give_way), so partial the same way
  Lost                                              the job's lease is gone: the call drops this job quietly

While a step runs, the runtime sets three context variables (a thread pool copies them into its threads with
contextvars.copy_context().run, or with submit() here):

  DEADLINE   the step's deadline, a time.monotonic() value: left(), check(), clamp() and sleep() read it
  CLOCK      the Worker's clock (StepContext.now): now() reads it, so a Later's time is the Worker's
  JOB        the job's own objects (per_job): what used to be module-level state, one per job

Outside a step (a command, a test) none is set: left() is None, nothing is clamped, now() is time.time().
"""
import contextvars
import threading
import time

DEADLINE = contextvars.ContextVar("ro_deadline", default=None)
CLOCK = contextvars.ContextVar("ro_clock", default=None)
JOB = contextvars.ContextVar("ro_job", default=None)

MIN_REQUEST_S = 3.0               # a request with less than this left is not started


class Later(Exception):
    """Come back at run_after (epoch seconds): the model busy, its quota used up, Polymarket or Google News not
    answering, the day's budget, the state moved. reason is one of CONTRACT.later_reasons. pause_lane_until also
    pauses the job's lane until then."""

    def __init__(self, run_after, reason, pause_lane_until=None):
        super().__init__(f"later ({reason}) at {int(run_after)}")
        self.run_after, self.reason = int(run_after), reason
        self.pause_lane_until = None if pause_lane_until is None else int(pause_lane_until)

    @classmethod
    def after(cls, seconds, reason, pause=False):
        """Later at now() + seconds; pause: the lane too."""
        at = now() + int(seconds)
        return cls(at, reason, at if pause else None)


class OutOfTime(Exception):
    """The step's deadline came before the next operation could finish."""


class GiveWay(OutOfTime):
    """A foreground job waits for this background step's lane or the heavy slot (the Worker's answer to a
    checkpoint, StepContext.give_way): the step goes out as partial from the last cursor, exactly like OutOfTime."""


class Lost(Exception):
    """The job's lease is gone (the Worker answered 409 lost): another call may hold it now."""


def now():
    """The Worker's clock while a step runs, else this machine's (epoch seconds)."""
    clock = CLOCK.get()
    return int(clock() if clock is not None else time.time())


def left():
    """Seconds left to the step's deadline, or None outside a step."""
    d = DEADLINE.get()
    return None if d is None else d - time.monotonic()


def check(need_s=MIN_REQUEST_S):
    """Raise OutOfTime when less than need_s seconds are left."""
    s = left()
    if s is not None and s < need_s:
        raise OutOfTime(f"{max(0.0, s):.0f} s left, {need_s:.0f} s needed")


def clamp(timeout, floor=MIN_REQUEST_S):
    """A request timeout cut to the time left; OutOfTime when under floor seconds are left."""
    s = left()
    if s is None:
        return timeout
    if s < floor:
        raise OutOfTime(f"{max(0.0, s):.0f} s left for a request")
    return min(float(timeout), s)


def sleep(seconds, floor=MIN_REQUEST_S):
    """time.sleep, unless waiting would leave under floor seconds: then OutOfTime (the step goes out as partial)."""
    s = left()
    if s is not None and s - seconds < floor:
        raise OutOfTime(f"a wait of {seconds:.0f} s does not fit in the {max(0.0, s):.0f} s left")
    time.sleep(seconds)


def per_job(name, factory, default=None):
    """The job's own object `name`, made by factory() the first time it is asked for while a step runs; outside a
    step, `default` (a command's module-level object). Several calls can share one process, so nothing mutable
    may live at module level while a step runs."""
    objs = JOB.get()
    if objs is None:
        return default
    with objs["_lock"]:
        if name not in objs:
            objs[name] = factory()
        return objs[name]


def new_job_objects():
    """The JOB value for a new job."""
    return {"_lock": threading.Lock()}


def submit(pool, fn, *args, **kwargs):
    """pool.submit with the caller's context (deadline, clock, job objects) copied into the thread."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)
