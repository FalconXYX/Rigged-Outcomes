"""
What the backend publishes for the site, saved as blobs in D1 (watchlist.blobs) and served by the Worker at
/api/data/ (docs/BACKEND.md). The site reads every market from here (web/src/lib/data.ts): the 7 original
markets (markets.json's) and the ones added with "Add to main page", stored the same way.

  site:closed.json, site:open.json   the published lists, byte for byte          put_list
  site:markets/<name>.json           a market's page, as export.py writes it
  market:<name>                      its data record: {"entry", "index", "data"} (the clean and insiders CSVs)
  site:index.json                    the markets' index: {"markets": [index entry + "v"], "updated_at"}

put_market saves the page, then the record, then the index entry carrying "v", the page's version (the site
asks for /api/data/markets/<name>.json?v=<v>, cached for good). The index is built here and saved with
compare-and-swap (expect the version read), read again and rebuilt when another save came first, so two adds
never lose each other. Entries stay oldest added first; the originals carry added_at 1 to 7 (markets.json's
order), so they come first.

    python3 -m backend.site ls                 the markets in the index
    python3 -m backend.site originals          the 7 original markets, from the original-project branch's CSVs
    python3 -m backend.site migrate            today's market:* records into site:markets/* and site:index.json
    python3 -m backend.site index              build site:index.json again from the market:* records
    python3 -m backend.site rm NAME            take a market off the main page (also: remove NAME)

The API is WATCHLIST_API_URL with WATCHLIST_JOB_TOKEN, as for watchlist.sync.
"""
import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time

from backend.contract import toronto_iso

INDEX = "site:index.json"
LISTS = ("closed", "open")
INDEX_TRIES = 6
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGINALS_REF = "original-project"      # the branch holding the original class project and its data/*.csv


class NameTaken(Exception):
    """The page name is another market's (its market:<name> record has another condition id)."""


def page_blob(name):
    return f"site:markets/{name}.json"


def record_blob(name):
    return f"market:{name}"


def page_bytes(page):
    """A market's page as export.py writes markets/<name>.json, byte for byte (export.page_bytes)."""
    return json.dumps(page, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _moved(e):
    return getattr(e, "status", None) == 409 and getattr(e, "code", None) == "moved"


def _now(now):
    return int(time.time() if now is None else now)


def put_list(c, name, data, meta=None):
    """Publish a list: name "closed" or "open" ("closed.json" works too); data its bytes (a str is UTF-8 encoded, a
    dict or list is dumped compact), saved byte for byte as site:<name>.json. Returns the new head."""
    from watchlist import blobs
    name = str(name).removesuffix(".json")
    if name not in LISTS:
        raise ValueError(f"no list {name!r}: closed or open")
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return blobs.put(c, f"site:{name}.json", bytes(data), dict(meta or {}, by=(meta or {}).get("by", "vercel")))


def read_index(c):
    """(head, [entries]) of site:index.json; (None, []) before the first add."""
    from watchlist import blobs
    h, doc = blobs.get_json(c, INDEX)
    entries = list((doc or {}).get("markets") or []) if isinstance(doc, dict) else []
    return h, [e for e in entries if isinstance(e, dict) and e.get("name")]


def update_index(c, change, now=None, by="vercel"):
    """Apply change(entries) -> entries to site:index.json with compare-and-swap, again on a fresh copy each time
    another save came first. Returns the new entries."""
    from watchlist import blobs
    for _ in range(INDEX_TRIES):
        h, entries = read_index(c)
        new = change([dict(e) for e in entries])
        doc = {"markets": new, "updated_at": toronto_iso(_now(now))}
        try:
            blobs.put_json(c, INDEX, doc, {"by": by, "markets": len(new)}, expect=h["version"] if h else None)
            return new
        except Exception as e:
            if not _moved(e):
                raise
    raise RuntimeError(f"{INDEX} kept moving: {INDEX_TRIES} saves in a row came after another one")


def _upsert(entry):
    def change(entries):
        for i, e in enumerate(entries):
            if e.get("name") == entry["name"]:
                entries[i] = entry
                return entries
        return entries + [entry]
    return change


def added_entries(c):
    """The entries ({name, slug, output, correct_outcome}) of the markets added before, from the market:* heads."""
    from watchlist import blobs
    return [h["meta"]["entry"] for h in blobs.heads(c, "market:")
            if isinstance(h.get("meta"), dict) and isinstance(h["meta"].get("entry"), dict)]


def put_market(c, entry, index, page, data, condition_id, now=None, by="vercel", added_at=None):
    """Put a market on the main page: its page, its data record, its index entry. The same market saved again (a
    step run twice) replaces itself and keeps its added_at (or takes the one given). Returns {"name", "v"}.
    Raises NameTaken when the name is another market's, watchlist.sync.ApiError when the API refuses (409 full:
    the database holds as many added markets as it may)."""
    from watchlist import blobs
    name, cid, now = entry["name"], str(condition_id).lower(), _now(now)
    if not (index.get("name") == name == page.get("name")):
        raise ValueError("the entry, the index entry and the page name different markets")
    head = blobs.head(c, record_blob(name))
    if head is not None and (head.get("meta") or {}).get("condition_id") != cid:
        raise NameTaken(name)
    added_at = int(added_at or (head or {}).get("meta", {}).get("added_at") or now)
    v = blobs.put(c, page_blob(name), page_bytes(page), {"name": name, "condition_id": cid, "at": now})["version"]
    meta = {"entry": entry, "condition_id": cid, "added_at": added_at, "by": by, "v": v}
    record = {"entry": entry, "index": index, "data": data or {}}
    try:
        blobs.put_json(c, record_blob(name), record, meta, expect=head["version"] if head else None)
    except Exception as e:
        if not _moved(e):
            raise
        again = blobs.head(c, record_blob(name))           # saved meanwhile: by this market's other step, or not
        if again is None or (again.get("meta") or {}).get("condition_id") != cid:
            raise NameTaken(name)
        blobs.put_json(c, record_blob(name), record, meta, expect=again["version"])
    update_index(c, _upsert(dict(index, v=v)), now, by)
    return {"name": name, "v": v}


def git_text(ref, path):
    """A file's text as commit `ref` of this repository holds it (git show; GIT_DIR names another repository)."""
    got = subprocess.run(["git", "-C", ROOT, "show", f"{ref}:{path}"], capture_output=True)
    if got.returncode:
        raise RuntimeError(f"git has no {path} at {ref}: {got.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return got.stdout.decode("utf-8")


def condition_of(clean_csv):
    """The one condition id (market_id) of a clean CSV's rows, lower case."""
    ids = {str(r.get("market_id") or "").lower() for r in csv.DictReader(io.StringIO(clean_csv))}
    if len(ids) != 1 or not next(iter(ids)).startswith("0x"):
        raise ValueError(f"the clean CSV's rows are not one market's ({sorted(ids)[:3]})")
    return ids.pop()


def _record_sha(record):
    from watchlist import blobs
    return blobs.sha256(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _served(c, entry, index, page, data, cid, added_at, served):
    """Whether the page, the record and the index entry saved for `entry` are these already."""
    from watchlist import blobs
    ph, rh = blobs.head(c, page_blob(entry["name"])), blobs.head(c, record_blob(entry["name"]))
    if ph is None or rh is None or ph["sha256"] != blobs.sha256(page_bytes(page)):
        return False
    meta = rh.get("meta") or {}
    return (meta.get("entry") == entry and meta.get("condition_id") == cid and meta.get("added_at") == added_at
            and meta.get("v") == ph["version"]
            and rh["sha256"] == _record_sha({"entry": entry, "index": index, "data": data})
            and served == json.loads(json.dumps(dict(index, v=ph["version"]), allow_nan=False)))


def put_originals(c, read=None, now=None, by="local"):
    """Serve the original markets (markets.json's) from the database as the added ones are: each page built with
    add_market.build from its clean and insiders CSVs (read(path) -> text; default: the original-project
    branch's data/), the page byte for byte what export.py writes, saved with put_market with added_at 1 to 7,
    then the index ordered with them first. A market whose page, record and index entry are these already is left
    as it is, so a second run changes nothing. Returns [(name, "saved" | "unchanged")]."""
    from pipeline import add_market
    read = read or (lambda path: git_text(ORIGINALS_REF, path))
    markets = add_market.read_markets()
    order = {m["name"]: i for i, m in enumerate(markets)}
    served = {e["name"]: e for e in read_index(c)[1]}
    done = []
    for i, m in enumerate(markets, 1):
        clean, insiders = (read(f"data/{m['output']}{s}.csv") for s in ("_clean", "_insiders"))
        cid = condition_of(clean)
        index, page, data = add_market.build(m, clean, insiders)
        if _served(c, m, index, page, data, cid, i, served.get(m["name"])):
            done.append((m["name"], "unchanged"))
        else:
            put_market(c, m, index, page, data, cid, now, by, added_at=i)
            done.append((m["name"], "saved"))
        print(f"  {m['name']}: {done[-1][1]}", flush=True)

    def first(entries):
        return sorted(entries, key=lambda e: order.get(e["name"], len(order)))     # stable: the added keep theirs
    entries = read_index(c)[1]
    if first(entries) != entries:
        update_index(c, first, now, by)
    return done


def remove_market(c, name, now=None, by="local"):
    """Take a market off the main page: its index entry, its page and its record. False when it was not
    there."""
    from watchlist import blobs
    there = any(e.get("name") == name for e in read_index(c)[1])
    update_index(c, lambda entries: [e for e in entries if e.get("name") != name], now, by)
    gone = [blobs.delete(c, page_blob(name)), blobs.delete(c, record_blob(name))]
    return there or any(gone)


def _records(c):
    """(head, record) of every market:* blob, oldest added first."""
    from watchlist import blobs
    heads = sorted(blobs.heads(c, "market:"), key=lambda h: (int((h.get("meta") or {}).get("added_at") or 0),
                                                              h["name"]))
    for h in heads:
        got, doc = blobs.get_json(c, h["name"], head=h)
        if got is not None and isinstance(doc, dict):
            yield got, doc


def rebuild_index(c, now=None, by="local"):
    """site:index.json again from the market:* records and their pages' versions. Returns its entries."""
    from watchlist import blobs
    entries = []
    for h, doc in _records(c):
        name = h["name"].split(":", 1)[1]
        page = blobs.head(c, page_blob(name))
        if page is None or not isinstance(doc.get("index"), dict):
            print(f"  ! {h['name']}: no page saved for it; left out of the index (python3 -m backend.site migrate)",
                  flush=True)
            continue
        entries.append(dict(doc["index"], v=page["version"]))
    return update_index(c, lambda _: entries, now, by)


def migrate(c, now=None):
    """Move the markets added before the backend (market:* records holding their page) to what the Worker
    serves: each page built again from its data files with this export.py (the saved page when that fails), put
    as site:markets/<name>.json, its record kept without the page, then site:index.json. Safe to run again."""
    from pipeline import add_market
    from watchlist import blobs
    done = []
    for h, doc in _records(c):
        name = h["name"].split(":", 1)[1]
        meta = dict(h.get("meta") or {})
        entry, data = doc.get("entry") or meta.get("entry"), doc.get("data") or {}
        index, page = doc.get("index"), doc.get("market")
        clean, insiders = (data.get(f"{(entry or {}).get('output')}{s}.csv") for s in ("_clean", "_insiders"))
        if isinstance(entry, dict) and isinstance(clean, str) and isinstance(insiders, str):
            try:
                index, page, _ = add_market.build(entry, clean, insiders)
            except Exception as e:
                print(f"  ! {h['name']}: could not be built again ({type(e).__name__}: {str(e)[:160]}); its saved page "
                      f"is used", flush=True)
        if not (isinstance(index, dict) and isinstance(page, dict) and index.get("name") == name == page.get("name")):
            print(f"  ! {h['name']}: no page to serve; left as it is", flush=True)
            continue
        v = blobs.put(c, page_blob(name), page_bytes(page), {"name": name, "condition_id": meta.get("condition_id"),
                                                             "at": _now(now)})["version"]
        blobs.put_json(c, h["name"], {"entry": entry, "index": index, "data": data}, dict(meta, v=v),
                       expect=h["version"])
        done.append(name)
        print(f"  {name}: page served from the database", flush=True)
    rebuild_index(c, now)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description="The markets the Worker serves from the database.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls")
    sub.add_parser("originals").add_argument("--ref", default=ORIGINALS_REF, help="the commit holding data/*.csv "
                                                                                 "(default: original-project)")
    sub.add_parser("migrate")
    sub.add_parser("index")
    p = sub.add_parser("rm", aliases=["remove"])
    p.add_argument("name")
    args = ap.parse_args(argv)
    from watchlist import blobs, sync
    c = sync.client()
    if c is None:
        print("No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN)", file=sys.stderr)
        return 2
    try:
        if args.cmd == "ls":
            for e in read_index(c)[1]:
                print(f"{e['name']:<34} {str(e.get('v'))[:8]}  {e.get('title')}")
        elif args.cmd == "originals":
            got = put_originals(c, lambda path: git_text(args.ref, path))
            print(f"{len(got)} original market(s) served from the database "
                  f"({sum(1 for _, s in got if s == 'saved')} saved, the rest unchanged)")
        elif args.cmd == "migrate":
            print(f"{len(migrate(c))} market(s) moved; site:index.json built")
        elif args.cmd == "index":
            print(f"site:index.json holds {len(rebuild_index(c))} market(s)")
        elif remove_market(c, args.name):
            print(f"{args.name} taken off the main page")
        else:
            print(f"no market {args.name} on the main page", file=sys.stderr)
            return 1
    except (sync.ApiError, blobs.BlobError, RuntimeError, ValueError, NameTaken) as e:
        print(f"site {args.cmd}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
