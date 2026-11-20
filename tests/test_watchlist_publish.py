#!/usr/bin/env python3
"""
Offline tests for the lists' publish to the site's data (watchlist.publish),
against the fake API (tests/fake_api.py). No network.

    python3 tests/test_watchlist_publish.py

Covers a list saved byte for byte as site:<name>.json (what the Worker
serves at /api/data/), its kept versions, a name that is not a list
refused, the CLI publishing a local file (and refusing one that does not
parse), and no API configured.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_api  # noqa: E402
from fake_api import FakeApi  # noqa: E402
from backend import contract as C  # noqa: E402
from watchlist import blobs, publish as P, sync  # noqa: E402

fake_api.BLOB_NAME = re.compile("|".join(f"(?:{p})" for p in C.CONTRACT["blob_names"].values()))
fake_api.HISTORY = re.compile(r"^(state|site:(index|closed|open)\.json)$")
fake_api.PREFIX = re.compile(r"^[a-z0-9:._/-]{0,40}$")


def test_a_list_is_saved_byte_for_byte():
    api = FakeApi()
    data = json.dumps({"generated_at": "2026-11-19T04:12:00-05:00", "days": [{"day": "2026-11-18", "picks": []}]},
                      separators=(",", ":")).encode()
    with contextlib.redirect_stdout(io.StringIO()):
        h = P.put_list(api, "closed", data, made_at=1_795_078_800)
        assert P.get_list(api, "closed") == data and h["bytes"] == len(data)
        assert blobs.head(api, "site:closed.json")["meta"]["made_at"] == "2026-11-19T04:00:00-05:00"
        P.put_list(api, "closed", data + b" ")
    assert [v["bytes"] for v in blobs.versions(api, "site:closed.json")] == [len(data)]     # the last one kept
    assert P.get_list(api, "open") is None


def test_only_the_two_lists():
    api = FakeApi()
    try:
        P.put_list(api, "index", b"{}")
        raise AssertionError("published index.json as a list")
    except ValueError as e:
        assert "closed or open" in str(e)


def test_the_cli_publishes_a_local_file():
    api = FakeApi()
    saved = sync.client
    sync.client = lambda *a, **k: api
    try:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            path = os.path.join(tmp, "open.json")
            with open(path, "w") as f:
                f.write('{"days": []}')
            assert P.main(["open", path]) == 0 and P.get_list(api, "open") == b'{"days": []}'
            with open(path, "w") as f:
                f.write("{not json")
            assert P.main(["open", path]) == 1 and "publish open" in err.getvalue()
            assert P.get_list(api, "open") == b'{"days": []}'
        sync.client = lambda *a, **k: None
        with contextlib.redirect_stderr(io.StringIO()):
            assert P.main(["get", "open"]) == 2
    finally:
        sync.client = saved


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for fn in checks:
        try:
            fn()
            print(f"PASS {fn.__name__}", flush=True)
        except Exception as e:
            failed.append(fn.__name__)
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}", flush=True)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)
