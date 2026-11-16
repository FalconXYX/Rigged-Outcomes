#!/usr/bin/env python3
"""
The job's own client (watchlist/blobs.py, watchlist/state.py) against a real
local Worker, where the offline tests use a stand-in (tests/fake_api.py): its
HTTP, its JSON, the ":" kept in a blob's path, a save in many parts, expect,
and the state's seed, push, pull, kept versions and restore. test/run.mjs runs
it on its throwaway database, after the smoke test. Exits 1 on any failed
check.

    python3 test/client.py http://127.0.0.1:8799
"""
import contextlib
import io
import os
import secrets
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_watchlist_state import NOW, filled, rows  # noqa: E402
from watchlist import blobs, state as S, store, sync  # noqa: E402

passed, failed = 0, []


def check(name, ok, detail=""):
    global passed
    if ok:
        passed += 1
    else:
        failed.append(name)
        print(f"FAIL {name}" + (f"\n     {detail}" if detail else ""), flush=True)


def main(base):
    c = sync.Client(base, "dev-job-token", retry_waits=())
    sent = []
    real = c.request

    def counted(method, path, body=None):
        sent.append((method, path))
        return real(method, path, body)
    c.request = counted

    # a document in many parts, through the real Worker
    name = f"market:py-{secrets.token_hex(4)}"
    data = os.urandom(30_000) + "é ✓".encode() * 1000
    old, blobs.PART_CHARS = blobs.PART_CHARS, 4_000
    try:
        h = blobs.put(c, name, data, {"entry": {"name": name[7:]}}, expect=None)
    finally:
        blobs.PART_CHARS = old
    check("client: a save in many parts", h["parts"] > 5 and h["bytes"] == len(data), h)
    check("client: the name keeps its ':' in the path", all(f"/{name}" in p for _, p in sent), sent[:2])
    check("client: it reads back whole", blobs.get(c, name) == (h, data))
    check("client: listed under its prefix", any(x["name"] == name for x in blobs.heads(c, "market:")))
    try:
        blobs.put(c, name, b"again", {}, expect=None)
        check("client: expect None over a saved blob is refused", False)
    except sync.ApiError as e:
        check("client: expect None over a saved blob is 409 moved", e.status == 409 and e.code == "moved", e)
    check("client: a market keeps no versions", blobs.versions(c, name) == [])
    check("client: removed", blobs.delete(c, name) and blobs.head(c, name) is None and not blobs.delete(c, name))

    # the state: seed, a fresh runner's pull, an unchanged push sending nothing, a save, a restore
    if blobs.head(c, S.STATE) is not None:
        check("client: the state checks need a database with no state (npm test's)", False)
        return
    for name in S.SITE_FILES:                       # the smoke test leaves made-up lists, not gzip: real ones over them
        blobs.put(c, S._list_name(name), b'{"days": []}\n', {"by": "client"})
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        place = {k: os.path.join(tmp, k) for k in ("site_dir", "job_dir")}
        for d in place.values():
            os.makedirs(d)
        src = filled()
        first = S.push(src, c, seed=True, by="local", now=NOW, **place)
        conn = store.connect(":memory:")
        S.pull(conn, c, db_path=":memory:", now=NOW + 1, **place)
        check("state: a fresh store pulls every row", all(rows(conn, t) == rows(src, t) for t in S.TABLES))
        del sent[:]
        again = S.push(conn, c, now=NOW + 2, **place)
        check("state: an unchanged push saves nothing", not again["changed"] and not [m for m, _ in sent if m != "GET"],
              sent)
        store.save_review(conn, "acme", "acme talks", "step", {"title": "Acme talks"}, at=NOW + 3)
        second = S.push(conn, c, now=NOW + 4, **place)
        kept = blobs.versions(c, S.STATE)
        check("state: a changed push saves a new version and keeps the one before",
              second["changed"] and [v["version"] for v in kept] == [first["version"]], kept)
        try:
            S.push(src, c, seed=True, now=NOW + 5, **place)
            check("state: seeding over a saved state is refused", False)
        except S.StateError as e:
            check("state: seeding over a saved state is refused (exit 3)", e.code == 3, e)
        back = blobs.restore(c, S.STATE, first["version"])
        fresh = store.connect(":memory:")
        S.pull(fresh, c, db_path=":memory:", now=NOW + 6, **place)
        check("state: a restore makes the first save live again",
              back["version"] == first["version"] and all(rows(fresh, t) == rows(src, t) for t in S.TABLES))
        check("state: ...and keeps the one it replaced", [v["version"] for v in blobs.versions(c, S.STATE)] ==
              [second["version"]])


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788")
    except Exception as e:                                        # noqa: BLE001
        import traceback
        traceback.print_exc()
        failed.append(f"threw: {e}")
    print(f"{passed}/{passed + len(failed)} checks passed (the job's client)")
    sys.exit(1 if failed else 0)
