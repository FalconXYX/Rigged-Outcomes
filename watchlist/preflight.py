"""
The daily job's first step (.github/workflows/watchlist.yml): the setup, checked before anything runs, so a
setup problem fails the run at its start with one clear message and nothing later has to fail for it:

  - the secrets GEMINI_API_KEY and WATCHLIST_JOB_TOKEN and the variable API_URL are set (the step gets the
    variable as WATCHLIST_API_URL, like the others);
  - API_URL is a web address that answers like the site's API and accepts the job token (a 401, or a 429
    "locked" after 10 wrong tokens in a UTC day, is a refusal);
  - Google does not call the Gemini key invalid (a list of its models: no request of the free allowance is
    spent). Only an answer that names the key counts: any other one is a warning.

A service that does not answer here is no setup problem: a warning, recorded for the run's report, and the
steps wait for it as they always do (watchlist.problems).

    python3 -m watchlist.preflight

Exit 1 with one ::error:: line when the setup is wrong, else 0. It runs as the job's step "preflight" (a crash
is recorded with its traceback). Nothing secret is ever printed.
"""
import os
import re
import sys
import time
import traceback

import requests

from watchlist import ai, problems, sync
from watchlist.common import log

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = 20
GEMINI_WAITS = (5, 20)            # seconds before asking Google again when it did not answer
API_WAITS = (5, 20)               # the same for the site's API (sync.Client): with 20 s a try, under 3 minutes
                                  # for both, well inside the step's 8 (a timeout there would fail the run)
SETTINGS = (("GEMINI_API_KEY", "the secret GEMINI_API_KEY"),
            ("WATCHLIST_JOB_TOKEN", "the secret WATCHLIST_JOB_TOKEN"),
            ("WATCHLIST_API_URL", "the variable API_URL"))
_KEY_REFUSED = re.compile(r"api[ _-]?key|credential|unauthenticated", re.I)
_URL = re.compile(r"^https?://[^/\s?#]+", re.I)
WHERE = "GitHub: Settings, Secrets and variables, Actions"


class SetupError(Exception):
    """What is wrong with the setup, in one sentence a person can act on."""


def settings(get=sync._env):
    """{name: value} of the three settings; raises SetupError naming every missing one."""
    got = {name: (get(name) or "").strip() for name, _ in SETTINGS}
    missing = [what for name, what in SETTINGS if not got[name]]
    if missing:
        raise SetupError(f"{' and '.join([', '.join(missing[:-1]), missing[-1]] if len(missing) > 1 else missing)} "
                         f"{'is' if len(missing) == 1 else 'are'} not set ({WHERE}); the job cannot run without "
                         f"{'it' if len(missing) == 1 else 'them'}")
    if not _URL.match(got["WATCHLIST_API_URL"]):
        raise SetupError(f"the variable API_URL is not a web address (it must be the Worker's, starting with "
                         f"https://; {WHERE})")
    return got


def check_api(c):
    """A warning (str) when the site's API did not answer, None when it accepted the token; raises SetupError
    when it refused the token or is not the site's API."""
    try:
        got = c.request("GET", "/api/job/blobs?prefix=state")
    except sync.ApiError as e:
        if e.refused:
            raise SetupError("the site's API refused the job token: the secret WATCHLIST_JOB_TOKEN must be the "
                             "Worker's JOB_TOKEN" + (" (after 10 wrong tokens it stays locked until the UTC day "
                                                      "ends)" if e.code == "locked" else ""))
        if e.outage:
            return f"the site's API did not answer the setup check ({e}); the steps wait for it"
        raise SetupError(f"the variable API_URL does not answer like the site's API ({e}); it must be the Worker's "
                         f"address")
    if not isinstance(got, dict) or not isinstance(got.get("blobs"), list):
        raise SetupError("the variable API_URL does not answer like the site's API; it must be the Worker's address")
    return None


def check_gemini(key, session=None, sleep=time.sleep):
    """A warning (str) when Google did not answer or answered something that does not name the key, None when
    it listed the models; raises SetupError when it said the key is not valid."""
    session = session or requests.Session()
    why = None
    for attempt in range(len(GEMINI_WAITS) + 1):
        if attempt:
            sleep(GEMINI_WAITS[attempt - 1])
        try:
            r = session.get(MODELS_URL, params={"pageSize": 1}, headers={"x-goog-api-key": key}, timeout=TIMEOUT)
        except requests.RequestException as e:
            why = type(e).__name__
            continue
        if r.status_code == 200:
            return None
        msg = ai._error_text(r).replace(key, "<key>")
        if r.status_code in (400, 401, 403) and _KEY_REFUSED.search(msg):
            raise SetupError(f"Google refused the Gemini key (HTTP {r.status_code}: {msg[:160]}); the secret "
                             f"GEMINI_API_KEY must be a working key")
        why = f"HTTP {r.status_code}: {msg[:160]}"
        if r.status_code != 429 and r.status_code < 500:
            break                                    # an answer that does not name the key: not worth asking again
    return f"Google did not confirm the Gemini key ({why}); the lists find out when they ask the model"


def run(get=sync._env, client=None, session=None, sleep=time.sleep):
    """Check the setup; returns the warnings (strs). Raises SetupError."""
    got = settings(get)
    c = client or sync.Client(got["WATCHLIST_API_URL"], got["WATCHLIST_JOB_TOKEN"], retry_waits=API_WAITS)
    warnings = [w for w in (check_api(c), check_gemini(got["GEMINI_API_KEY"], session, sleep)) if w]
    return warnings


def _stop(msg):
    print(f"::error::{msg}" if os.environ.get("GITHUB_ACTIONS") else msg, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(f"### Watchlist job: stopped at the setup check\n\n{msg}.\n")
    return 1


def main(argv=None):
    try:
        warnings = run()
    except SetupError as e:
        return _stop(f"Setup: {e}")
    except Exception as e:                                # the check's own code: its traceback, then one line
        traceback.print_exc()
        sys.stderr.flush()
        return _stop(f"The setup check crashed: {type(e).__name__}: {str(e)[:200]} (the traceback is in its log)")
    for w in warnings:
        problems.note(w)                                  # a warning, and a line in the run's report
    log("setup: the secrets and the variable are set" + ("" if warnings else "; the API and the Gemini key answer"))
    return 0


if __name__ == "__main__":
    sys.exit(problems.guard("preflight", main))
