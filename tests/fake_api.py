"""
A stand-in for the site's API in the job's offline tests: the blob endpoints
(worker/src/blobs.ts) with the Worker's rules, kept in memory, and a record
of every other call. Use it where the job takes a sync.Client: it has the
same request(method, path, body) and raises the same sync.ApiError.

    api = FakeApi()
    blobs.put(api, "state", b"...", {})
    api.calls["PUT"]                  # calls by method
    api.fail_next(503, "server", method="POST")   # the next POST answers 503 (as the Client's last retry would)

Rules copied from the Worker: names, versions, parts and data checked (400
bad_request), a live or kept version's parts never rewritten (409 live), a
commit needing every part (409 incomplete), `expect` checked against the live
version (409 moved), a commit of the live version again answered with its
head when the sha256 matches (409 live otherwise), the old version's parts
dropped when the pointer moves, except for state and the lists, whose last
KEPT versions are kept (GET .../versions; committing one again restores it),
a new market refused past MAX_MARKETS or MARKET_PARTS (409 full), state and
the lists never deleted (400), 404 no_blob and no_part.
"""
import copy
import json
import os
import re
import sys
import urllib.parse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchlist import sync  # noqa: E402

BLOB_NAME = re.compile(r"^(state|cache:news|list:(closed|open)|market:[a-z0-9][a-z0-9-]{1,31})$")
VERSION = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
PREFIX = re.compile(r"^[a-z:-]{0,40}$")
HISTORY = re.compile(r"^(state|list:(closed|open))$")
PART_CHARS = 1_000_000
MAX_PARTS = 400
META_CHARS = 16_384
KEPT = 3
MAX_MARKETS = 200
MARKET_PARTS = 300


def _bad(message):
    raise sync.ApiError(400, "bad_request", message)


class FakeApi:
    def __init__(self, now=1_800_000_000):
        self.base = "http://fake-api.test"
        self.now = now
        self.blobs = {}                 # name -> row {name, version, parts, bytes, sha256, meta (JSON text), updated_at}
        self.parts = {}                 # (name, version, part) -> base64 text
        self.kept = {}                  # name -> [row + replaced_at], newest first (state and the lists)
        self.calls = Counter()          # by method
        self.log = []                   # (method, path, body)
        self.other = []                 # calls to anything but the blob endpoints: (method, path, body)
        self._fails = []                # (method or None, path test or None, ApiError)

    def __repr__(self):
        return "FakeApi()"

    def fail_next(self, status, code="error", method=None, path=None):
        """Make the next call (of that method, whose path contains `path`) raise ApiError(status, code)."""
        self._fails.append((method, path, sync.ApiError(status, code)))

    # ── the API ──

    def request(self, method, path, body=None):
        self.calls[method] += 1
        self.log.append((method, path, copy.deepcopy(body)))
        for i, (m, p, err) in enumerate(self._fails):
            if (m is None or m == method) and (p is None or p in path):
                del self._fails[i]
                raise err
        url = urllib.parse.urlsplit(path)
        if not url.path.startswith("/api/job/blobs"):
            self.other.append((method, path, copy.deepcopy(body)))
            return {}
        segs = [urllib.parse.unquote(s) for s in url.path.split("/")[4:]]
        if method == "GET" and not segs:
            prefix = urllib.parse.parse_qs(url.query).get("prefix", [""])[0]
            if not PREFIX.match(prefix):
                _bad("prefix")
            return {"blobs": [self._head(self.blobs[n]) for n in sorted(self.blobs) if n.startswith(prefix)][:1000]}
        if not segs or not BLOB_NAME.match(segs[0]):
            _bad("name")
        name = segs[0]
        if len(segs) == 1 and method == "GET":
            if name not in self.blobs:
                raise sync.ApiError(404, "no_blob")
            return self._head(self.blobs[name])
        if len(segs) == 1 and method == "DELETE":
            if HISTORY.match(name):
                _bad("the state and the lists can't be deleted")
            row = self.blobs.pop(name, None)
            if row is None:
                raise sync.ApiError(404, "no_blob")
            for k in [k for k in self.parts if k[0] == name and k[1] == row["version"]]:
                del self.parts[k]
            return None
        if len(segs) == 2 and segs[1] == "versions" and method == "GET":
            return {"versions": [self._head(r) for r in self.kept.get(name, [])]}
        if not VERSION.match(segs[1]):
            _bad("version")
        version = segs[1]
        if len(segs) == 2 and method == "POST":
            return self._commit(name, version, body or {})
        if len(segs) != 3 or not re.fullmatch(r"0|[1-9]\d{0,2}", segs[2]) or int(segs[2]) >= MAX_PARTS:
            _bad("part")
        part = int(segs[2])
        if method == "GET":
            if (name, version, part) not in self.parts:
                raise sync.ApiError(404, "no_part")
            return {"data": self.parts[(name, version, part)]}
        if method == "PUT":
            data = (body or {}).get("data")
            if not isinstance(data, str) or not 1 <= len(data) <= PART_CHARS or not BASE64.match(data):
                _bad("data")
            if (self.blobs.get(name) or {}).get("version") == version or \
                    any(r["version"] == version for r in self.kept.get(name, [])):
                raise sync.ApiError(409, "live")
            self.parts[(name, version, part)] = data
            return None
        raise sync.ApiError(404, "not_found")

    def _head(self, row):
        return dict(row, meta=json.loads(row["meta"]))

    def _commit(self, name, version, body):
        parts, size, sha, meta = body.get("parts"), body.get("bytes"), body.get("sha256"), body.get("meta")
        if not isinstance(parts, int) or isinstance(parts, bool) or not 1 <= parts <= MAX_PARTS:
            _bad("parts")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            _bad("bytes")
        if not isinstance(sha, str) or not SHA256.match(sha):
            _bad("sha256")
        if not isinstance(meta, dict) or len(json.dumps(meta, separators=(",", ":"))) > META_CHARS:
            _bad("meta")
        checked = "expect" in body
        expect = body.get("expect")
        if checked and expect is not None and (not isinstance(expect, str) or not VERSION.match(expect)):
            _bad("expect")
        cur = self.blobs.get(name)
        have = sorted(p for (n, v, p) in self.parts if n == name and v == version)
        if cur is not None and cur["version"] == version:
            if cur["sha256"] == sha:
                return self._head(cur)
            raise sync.ApiError(409, "live")
        if checked and expect != (cur["version"] if cur else None):
            raise sync.ApiError(409, "moved")
        if not (len(have) == parts and have[0] == 0 and have[-1] == parts - 1):
            raise sync.ApiError(409, "incomplete")
        if name.startswith("market:") and cur is None:
            markets = [r for n, r in self.blobs.items() if n.startswith("market:")]
            if len(markets) >= MAX_MARKETS or sum(r["parts"] for r in markets) + parts > MARKET_PARTS:
                raise sync.ApiError(409, "full")
        row = {"name": name, "version": version, "parts": parts, "bytes": size, "sha256": sha,
               "meta": json.dumps(meta, separators=(",", ":")), "updated_at": self.now}
        if cur is not None and HISTORY.match(name):
            kept = [dict(cur, replaced_at=self.now)] + [r for r in self.kept.get(name, []) if r["version"] != version]
            for old in kept[KEPT:]:
                for k in [k for k in self.parts if k[0] == name and k[1] == old["version"]]:
                    del self.parts[k]
            self.kept[name] = kept[:KEPT]
        elif cur is not None:
            for k in [k for k in self.parts if k[0] == name and k[1] == cur["version"]]:
                del self.parts[k]
        self.blobs[name] = row
        return self._head(row)
