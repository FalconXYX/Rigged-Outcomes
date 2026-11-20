"""
The Watchlist's two lists published to the site's data (docs/BACKEND.md):
site:closed.json and site:open.json in the database, byte for
byte, which the Worker serves at /api/data/closed.json and
/api/data/open.json. The daily run's publish steps (watchlist.steps) put
them as they make them; nothing is written to web/public/data and no site
build is needed.

    python3 -m watchlist.publish closed|open FILE     publish a list made locally (the CLIs' output)
    python3 -m watchlist.publish get closed|open [--out FILE]

The API is WATCHLIST_API_URL with WATCHLIST_JOB_TOKEN, as for watchlist.sync.
"""
import argparse
import json
import sys

from watchlist import blobs, sync
from watchlist.common import local_iso, log, now_epoch

LISTS = ("closed", "open")


def put_list(c, name, data, made_at=None, by="vercel"):
    """Publish a list's bytes as site:<name>.json (backend.site.put_list); returns the new head."""
    from backend import site
    if name not in LISTS:
        raise ValueError(f"no list {name!r}: closed or open")
    head = site.put_list(c, name, data, {"by": by, "made_at": local_iso(made_at or now_epoch())})
    log(f"{name}.json published ({len(data):,} bytes, version {head['version'][:8]})")
    return head


def get_list(c, name):
    """The published list's bytes, or None before the first publish."""
    h, data = blobs.get(c, f"site:{name}.json")
    return data if h is not None else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Watchlist's lists in the site's data.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in LISTS:
        p = sub.add_parser(name, help=f"publish {name}.json")
        p.add_argument("file")
    p = sub.add_parser("get", help="print a published list")
    p.add_argument("name", choices=LISTS)
    p.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    c = sync.client()
    if c is None:
        print("No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN)", file=sys.stderr)
        return 2
    try:
        if args.cmd == "get":
            data = get_list(c, args.name)
            if data is None:
                print(f"no {args.name} list published yet", file=sys.stderr)
                return 1
            if args.out:
                with open(args.out, "wb") as f:
                    f.write(data)
            else:
                sys.stdout.buffer.write(data)
            return 0
        with open(args.file, "rb") as f:
            data = f.read()
        json.loads(data)                                 # a list that does not parse is never published
        put_list(c, args.cmd, data, by="local")
    except (sync.ApiError, blobs.BlobError, OSError, ValueError) as e:
        print(f"publish {args.cmd}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
