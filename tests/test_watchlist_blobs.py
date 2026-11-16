#!/usr/bin/env python3
"""
Offline tests for the job's blob client (watchlist.blobs) against the fake
API (tests/fake_api.py, the Worker's rules in memory). No network.

    python3 tests/test_watchlist_blobs.py

Covers round trips cut into many parts (an empty document, one byte, exact
multiples of a part), a document that does not match its sha256, a blob
saved again mid-download (it starts again once), the three kinds of expect
(ANY, None, a version), a document too big or a part size that is not a
multiple of 4 refused before anything is sent, the name kept readable in the
path, and the state's and the lists' kept versions (read, restored, never
rewritten or deleted).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_api import FakeApi  # noqa: E402
from watchlist import blobs, sync  # noqa: E402


class small_parts:
    """blobs.PART_CHARS set to n for the block."""

    def __init__(self, n):
        self.n = n

    def __enter__(self):
        self.old, blobs.PART_CHARS = blobs.PART_CHARS, self.n

    def __exit__(self, *exc):
        blobs.PART_CHARS = self.old


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


def test_round_trips_in_many_parts():
    api = FakeApi()
    with small_parts(8):
        for data in (b"", b"x", os.urandom(1000), "é ✓ 中".encode() * 50):
            h = blobs.put(api, "state", data, {"n": len(data)})
            assert h["bytes"] == len(data) and h["meta"] == {"n": len(data)} and h["sha256"] == blobs.sha256(data)
            assert h["parts"] >= 1 and len({k for k in api.parts if k[:2] == ("state", h["version"])}) == h["parts"]
            got, back = blobs.get(api, "state")
            assert back == data and got == h
        # a document whose packed text is an exact multiple of a part: no empty part at the end
        for n in range(40, 200):
            data = os.urandom(n)
            packed = len(blobs.base64.b64encode(blobs.gzip.compress(data, 6, mtime=0)))
            if packed % 8 == 0:
                h = blobs.put(api, "list:open", data, {})
                assert h["parts"] == packed // 8 and blobs.get(api, "list:open")[1] == data
                break
        else:
            raise AssertionError("no exact multiple found")
    assert all(len(v) <= 8 for v in api.parts.values())
    # json, and no blob at all
    h = blobs.put_json(api, "market:abc", {"a": [1, "é"]}, {"entry": {"name": "abc"}})
    assert blobs.get_json(api, "market:abc") == (h, {"a": [1, "é"]})
    assert blobs.get(api, "cache:news") == (None, None) and blobs.get_json(api, "cache:news") == (None, None)
    assert blobs.head(api, "cache:news") is None
    assert [x["name"] for x in blobs.heads(api)] == ["list:open", "market:abc", "state"]
    assert [x["name"] for x in blobs.heads(api, "market:")] == ["market:abc"]
    # a json document refuses NaN (the site's JSON.parse would)
    raises(lambda: blobs.put_json(api, "market:abc", {"x": float("nan")}, {}), ValueError)


def test_a_bad_document_is_refused():
    api = FakeApi()
    h = blobs.put(api, "state", b"hello world", {})
    api.blobs["state"]["sha256"] = "0" * 64
    e = raises(lambda: blobs.get(api, "state"), blobs.BlobError)
    assert "sha256" in str(e)
    api.blobs["state"].update(sha256=h["sha256"], bytes=3)
    raises(lambda: blobs.get(api, "state"), blobs.BlobError)
    api.blobs["state"]["bytes"] = h["bytes"]
    api.parts[("state", h["version"], 0)] = "AAAA"                      # base64, but not gzip
    e = raises(lambda: blobs.get(api, "state"), blobs.BlobError)
    assert "decode" in str(e)


def test_saved_again_mid_download_starts_again_once():
    api = FakeApi()
    with small_parts(8):
        old = blobs.put(api, "cache:news", b"the first version, in several parts", {})
        new_data = b"the second version, saved while the first was read"
        real = api.request
        swapped = []

        def request(method, path, body=None):
            if method == "GET" and path.endswith(f"/{old['version']}/1") and not swapped:
                swapped.append(blobs.put(api, "cache:news", new_data, {}))    # another run saves meanwhile
            return real(method, path, body)
        api.request = request
        h, data = blobs.get(api, "cache:news", head=old)
        assert data == new_data and h["version"] == swapped[0]["version"] != old["version"]

        # the state's replaced version is kept, so a download that began on it ends on it, whole
        api.request = real
        first = blobs.put(api, "state", b"the state, first", {})
        swapped.clear()

        def state_saved(method, path, body=None):
            if method == "GET" and path.endswith(f"/{first['version']}/1") and not swapped:
                swapped.append(blobs.put(api, "state", b"the state, second", {}))
            return real(method, path, body)
        api.request = state_saved
        assert blobs.get(api, "state", head=first) == (first, b"the state, first") and swapped

        # it starts again only once: a second swap mid-download is an error
        def always(method, path, body=None):
            if method == "GET" and "/cache:news/" in path and path.rsplit("/", 1)[1] == "1":
                raise sync.ApiError(404, "no_part")
            return real(method, path, body)
        api.request = always
        e = raises(lambda: blobs.get(api, "cache:news"), sync.ApiError)
        assert e.code == "no_part"


def test_expect():
    api = FakeApi()
    a = blobs.put(api, "state", b"a", {}, expect=None)                   # None: it must not exist yet
    e = raises(lambda: blobs.put(api, "state", b"b", {}, expect=None), sync.ApiError)
    assert e.status == 409 and e.code == "moved"
    b = blobs.put(api, "state", b"b", {}, expect=a["version"])            # a version: it must be the live one
    e = raises(lambda: blobs.put(api, "state", b"c", {}, expect=a["version"]), sync.ApiError)
    assert e.code == "moved" and blobs.get(api, "state")[1] == b"b"
    c = blobs.put(api, "state", b"c", {})                                 # ANY: no check
    assert c["version"] not in (a["version"], b["version"]) and blobs.get(api, "state")[1] == b"c"
    assert "expect" not in [b for m, _, b in api.log if m == "POST"][-1]
    # a market's replaced version's parts are gone (the state's are kept: test_kept_versions)
    m1 = blobs.put(api, "market:m-1", b"one", {})
    blobs.put(api, "market:m-1", b"two", {})
    assert ("market:m-1", m1["version"], 0) not in api.parts
    assert blobs.delete(api, "market:m-1") and blobs.head(api, "market:m-1") is None
    assert not blobs.delete(api, "market:m-1")


def test_too_big_is_refused_before_anything_is_sent():
    api = FakeApi()
    with small_parts(4):
        e = raises(lambda: blobs.put(api, "cache:news", os.urandom(blobs.MAX_PARTS * 3), {}), blobs.BlobError)
    assert "parts" in str(e) and api.calls["PUT"] == 0 and api.calls["POST"] == 0
    # a part size that is not a multiple of 4 could leave a last part of only "=" padding, which the Worker refuses
    assert blobs.PART_CHARS % 4 == 0
    with small_parts(90_001):
        raises(lambda: blobs.put(api, "cache:news", b"x", {}), ValueError)
    assert api.calls["PUT"] == 0


def test_the_name_in_the_path():
    assert blobs._path("market:x") == "/api/job/blobs/market:x"
    assert blobs._path("market:x", "ab", 3) == "/api/job/blobs/market:x/ab/3"
    assert blobs._path("a b/c") == "/api/job/blobs/a%20b%2Fc"
    api = FakeApi()
    blobs.put(api, "market:x1", b"x", {})
    assert {p.split("/")[4] for _, p, _ in api.log} == {"market:x1"}
    blobs.heads(api, "market:")
    assert api.log[-1][1] == "/api/job/blobs?prefix=market:"


def test_kept_versions():
    """The state's and the lists' last 3 replaced versions are kept: readable, restorable, never rewritten."""
    api = FakeApi()
    saved = []
    for i in range(5):
        api.now = 1_800_000_000 + i
        saved.append(blobs.put(api, "state", f"state {i}".encode(), {"by": f"run {i}"}))
    kept = blobs.versions(api, "state")
    assert [v["version"] for v in kept] == [saved[3]["version"], saved[2]["version"], saved[1]["version"]]
    assert kept[0]["replaced_at"] == 1_800_000_004 and kept[0]["meta"] == {"by": "run 3"}
    assert not any(k[1] == saved[0]["version"] for k in api.parts)                  # the 4th one back is gone
    assert blobs.get(api, "state", head=kept[1]) == (kept[1], b"state 2")            # a kept one reads back
    assert blobs.get(api, "state")[1] == b"state 4"
    e = raises(lambda: api.request("PUT", f"/api/job/blobs/state/{kept[0]['version']}/0", {"data": "AAAA"}),
               sync.ApiError)
    assert e.code == "live"                                                         # never rewritten
    # restoring one: it is live again, and the one it replaced is kept in its place
    h = blobs.restore(api, "state", saved[2]["version"])
    assert h["version"] == saved[2]["version"] and blobs.get(api, "state")[1] == b"state 2"
    assert [v["version"] for v in blobs.versions(api, "state")] == [saved[4]["version"], saved[3]["version"],
                                                                     saved[1]["version"]]
    raises(lambda: blobs.restore(api, "state", saved[0]["version"]), KeyError)       # no longer kept
    # restoring over a save made meanwhile is refused; the state and the lists can't be deleted
    real = api.request

    def saved_meanwhile(method, path, body=None):
        if method == "POST" and saved[3]["version"] in path:
            api.request = real
            blobs.put(api, "state", b"meanwhile", {})
        return real(method, path, body)
    api.request = saved_meanwhile
    e = raises(lambda: blobs.restore(api, "state", saved[3]["version"]), sync.ApiError)
    assert e.code == "moved" and blobs.get(api, "state")[1] == b"meanwhile"
    e = raises(lambda: blobs.delete(api, "state"), sync.ApiError)
    assert e.status == 400 and blobs.head(api, "state") is not None
    # the cache and the markets keep nothing
    blobs.put(api, "cache:news", b"a", {})
    blobs.put(api, "cache:news", b"b", {})
    assert blobs.versions(api, "cache:news") == []


def test_retries_are_harmless():
    api = FakeApi()
    h = blobs.put(api, "state", b"one", {})
    path = f"/api/job/blobs/state/{h['version']}"
    again = api.request("POST", path, {"parts": h["parts"], "bytes": h["bytes"], "sha256": h["sha256"], "meta": {},
                                       "expect": "0" * 32})
    assert again == h                                                     # a lost answer, committed again
    e = raises(lambda: api.request("PUT", path + "/0", {"data": "AAAA"}), sync.ApiError)
    assert e.code == "live"


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
