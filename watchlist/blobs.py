"""
The daily job's data in the site's database (worker/src/blobs.ts, the D1
tables job_blobs and job_blob_parts): everything the job keeps between runs,
and nothing of it in git. Each item is a blob, one document saved whole:

  state                 the job's memory (watchlist.state)
  site:closed.json, site:open.json   the published lists, byte for byte
  site:index.json       the added markets' index, which the Worker serves
  site:markets/<name>.json   an added market's page (backend.site)
  market:<page name>    an added market's data record (backend.site,
                        watchlist.sync), to rebuild its page from
  scratch:<run>:<name>  a step's data too big for a result row (pruned)

The news searches are rows of the database's news_cache table, not blobs.

A document is gzip-compressed, base64-encoded and sent in parts of
PART_CHARS characters (D1 holds up to 2 MB a row, the Worker takes up to
1,000,000 characters a part). A save sends every part of a new version, then
commits it: the Worker moves the blob's pointer to it in one statement and
drops the old version's parts, so a reader never sees half a save and a save
that stops part way changes nothing (its parts are pruned a day later). The
state's and the lists' last 3 replaced versions are kept instead: `restore`
makes one live again (the one it replaces is kept in turn). A commit can
carry `expect`: None (the blob must not exist yet) or the version it must
replace, else the Worker answers 409 "moved". Every call goes through
sync.Client (the job token, its retries); a part sent twice and a commit sent
twice are harmless, so retries are safe. A download checks the document's
size and sha256 end to end (the Worker never decodes anything).

    python3 -m watchlist.blobs ls [--prefix P]
    python3 -m watchlist.blobs get NAME [--version V] [--out FILE]   the document, decompressed
    python3 -m watchlist.blobs versions NAME              the kept versions of state, site:closed.json or site:open.json
    python3 -m watchlist.blobs restore NAME VERSION       make a kept version live again (undo a bad save)
    python3 -m watchlist.blobs rm NAME                    market:<name>: take a market off the main page (its
                                                          request then shows "taken off the main page")

The API is WATCHLIST_API_URL with WATCHLIST_JOB_TOKEN, as for watchlist.sync.
"""
import argparse
import base64
import binascii
import gzip
import hashlib
import json
import secrets
import sys
import urllib.parse
import zlib
from urllib.parse import urlparse

from watchlist import sync
from watchlist.common import iso, log

PART_CHARS = 500_000              # base64 characters a part (the Worker takes up to 1,000,000); a multiple of 4
MAX_PARTS = 400                   # the Worker's limit
ANY = object()                    # put(expect=ANY): no check of what the save replaces


class BlobError(Exception):
    """A document that does not match its size or sha256, or one too big to save."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _path(name, *rest):
    return "/".join(["/api/job/blobs", urllib.parse.quote(name, safe=":")] + [str(x) for x in rest])


def head(c, name):
    """The blob's head {name, version, parts, bytes, sha256, meta, updated_at}, or None when there is none."""
    try:
        return c.request("GET", _path(name))
    except sync.ApiError as e:
        if e.status == 404 and e.code == "no_blob":
            return None
        raise


_head = head


def heads(c, prefix=""):
    """The heads of every blob whose name starts with prefix, ordered by name."""
    got = c.request("GET", "/api/job/blobs?prefix=" + urllib.parse.quote(prefix, safe=":")) or {}
    return list(got.get("blobs") or [])


def _part(c, name, version, i):
    data = (c.request("GET", _path(name, version, i)) or {}).get("data")
    if not isinstance(data, str):
        raise BlobError(f"{name}: part {i} came back without its data")
    return data


def versions(c, name):
    """The kept versions of a blob (state and the lists only), newest first: heads with "replaced_at"."""
    return list((c.request("GET", _path(name, "versions")) or {}).get("versions") or [])


def get(c, name, head=None):
    """(head, the document's bytes), or (None, None) when there is no such blob. head: the head to read (the live
    one, or a kept one from versions()); none: the live one. A live version's part gone mid-download (the blob
    was saved again meanwhile) makes it read the new head and start again, once."""
    h = head
    for attempt in range(2):
        if h is None:
            h = _head(c, name)
            if h is None:
                return None, None
        try:
            text = "".join(_part(c, name, h["version"], i) for i in range(int(h["parts"])))
        except sync.ApiError as e:
            if attempt == 0 and e.status == 404 and e.code == "no_part" and "replaced_at" not in h:
                h = None                     # (a kept version's parts going means it is no longer kept: an error)
                continue
            raise
        try:
            data = gzip.decompress(base64.b64decode(text, validate=True))
        except (binascii.Error, ValueError, OSError, EOFError, zlib.error) as e:
            raise BlobError(f"{name}: the saved parts do not decode ({type(e).__name__})")
        if len(data) != h.get("bytes") or sha256(data) != h.get("sha256"):
            raise BlobError(f"{name}: the downloaded document does not match its size and sha256")
        return h, data
    raise AssertionError("unreachable")


def get_json(c, name, head=None):
    """(head, the parsed document), or (None, None)."""
    h, data = get(c, name, head)
    if h is None:
        return None, None
    try:
        return h, json.loads(data.decode("utf-8"))
    except ValueError as e:
        raise BlobError(f"{name}: the document is not JSON ({e})")


def put(c, name, data, meta, expect=ANY):
    """Save data (bytes) as the blob's new version with meta (a JSON object); returns the new head. expect: None
    (the blob must not exist yet), a version (it must be the live one), or ANY. Raises BlobError before sending
    anything when it would need over MAX_PARTS parts, and sync.ApiError when the API refuses (e.code "moved":
    expect did not hold, and nothing changed)."""
    if PART_CHARS % 4:                   # else a last part could be only "=" padding, which the Worker refuses
        raise ValueError(f"PART_CHARS must be a multiple of 4, not {PART_CHARS}")
    packed = base64.b64encode(gzip.compress(data, 6, mtime=0)).decode("ascii")
    parts = [packed[i:i + PART_CHARS] for i in range(0, len(packed), PART_CHARS)] or [packed]
    if len(parts) > MAX_PARTS:
        raise BlobError(f"{name} is {len(data) / 1e6:.0f} MB, {len(parts)} parts of {PART_CHARS:,} characters "
                        f"compressed: over the {MAX_PARTS} a blob may have")
    version = secrets.token_hex(16)
    for i, p in enumerate(parts):
        c.request("PUT", _path(name, version, i), {"data": p})
    body = {"parts": len(parts), "bytes": len(data), "sha256": sha256(data), "meta": meta}
    if expect is not ANY:
        body["expect"] = expect
    return c.request("POST", _path(name, version), body)


def put_json(c, name, doc, meta, expect=ANY):
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return put(c, name, data, meta, expect=expect)


def restore(c, name, version):
    """Make a kept version of the blob live again, over the live one (which is kept in turn); returns the new
    head. Raises KeyError when the version is not kept, sync.ApiError when the API refuses (moved: saved again
    meanwhile)."""
    kept = next((v for v in versions(c, name) if v.get("version") == version), None)
    if kept is None:
        raise KeyError(f"{name} keeps no version {version}")
    cur = head(c, name)
    body = {k: kept[k] for k in ("parts", "bytes", "sha256", "meta")}
    body["expect"] = cur["version"] if cur else None
    return c.request("POST", _path(name, version), body)


def delete(c, name):
    """Remove the blob; False when there was none."""
    try:
        c.request("DELETE", _path(name))
        return True
    except sync.ApiError as e:
        if e.status == 404 and e.code == "no_blob":
            return False
        raise


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="The daily job's data in the site's database.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ls")
    p.add_argument("--prefix", default="")
    p = sub.add_parser("get")
    p.add_argument("name")
    p.add_argument("--version", default=None, help="a kept version (see versions) instead of the live one")
    p.add_argument("--out", default=None, help="write the document here (default: standard output)")
    p = sub.add_parser("versions")
    p.add_argument("name")
    p = sub.add_parser("restore")
    p.add_argument("name")
    p.add_argument("version")
    p = sub.add_parser("rm")
    p.add_argument("name")
    args = ap.parse_args(argv)
    c = sync.client()
    if c is None:
        print("No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN)", file=sys.stderr)
        return 2
    try:
        if args.cmd == "ls":
            for h in heads(c, args.prefix):
                print(f"{h['name']:<40} {h['parts']:>3} part(s) {h['bytes'] / 1e6:>8.2f} MB  "
                      f"{iso(h['updated_at'])}  {h['version'][:8]}")
        elif args.cmd == "versions":
            for v in versions(c, args.name):
                meta = v.get("meta") or {}
                print(f"{v['version']}  live from {iso(v['updated_at'])} to {iso(v['replaced_at'])}  "
                      f"{v['bytes'] / 1e6:.2f} MB" + (f"  made by {meta['by']}" if meta.get("by") else ""))
        elif args.cmd == "restore":
            h = restore(c, args.name, args.version)
            log(f"{args.name}: {h['version'][:8]} is live again on {urlparse(c.base).netloc} (the one it replaced is "
                f"kept: python3 -m watchlist.blobs versions {args.name})")
        elif args.cmd == "get":
            at = None
            if args.version:
                at = next((v for v in versions(c, args.name) if v.get("version") == args.version), None)
                if at is None:
                    print(f"{args.name} keeps no version {args.version}", file=sys.stderr)
                    return 1
            h, data = get(c, args.name, head=at)
            if h is None:
                print(f"no blob {args.name}", file=sys.stderr)
                return 1
            if args.out:
                with open(args.out, "wb") as f:
                    f.write(data)
                log(f"{args.name}: {len(data):,} bytes to {args.out}")
            else:
                sys.stdout.buffer.write(data)
        elif delete(c, args.name):
            log(f"{args.name} removed from {urlparse(c.base).netloc}")
        else:
            print(f"no blob {args.name}", file=sys.stderr)
            return 1
    except (sync.ApiError, BlobError, KeyError) as e:
        print(f"blobs {args.cmd}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
