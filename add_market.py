#!/usr/bin/env python3
"""
Add one closed market to the main page: the job's step for a market a
visitor sent there with "Add to main page" (watchlist.sync add,
docs/DEPLOY.md), and a command a person can run too. Nothing goes to git:
the market's built page and its data files are saved in the site's database
as the blob market:<name> (watchlist.blobs), and the site's deploy builds
its page again from them and adds it to the main page after the committed
ones (watchlist.publish). markets.json is never changed.

    python3 add_market.py --condition-id 0x... --slug SLUG [--name NAME] --outcome Yes [--dry-run]

1. Polymarket. Gamma's market for the slug must have this condition id, be
   closed, and have resolved to --outcome (watchlist.filters.winning_outcome).
2. Already there. When the slug is in markets.json and both its CSVs exist,
   or it is among the markets added before (`added`, the database's
   market:* entries), there is nothing to do: it reports "added", so a run
   after a failed report is harmless. A name another slug uses fails.
3. The entry. With no --name, the page name comes from the slug
   (page_name: "will-gemini-carbon-ship" -> "will-gemini-carbon-ship", cut
   to 32 characters at a word's end, "-2" when it is taken). Its output is
   the name in PascalCase ("gemini-carbon" -> "GeminiCarbon"), which no
   entry may use already: {"name", "slug", "output", "correct_outcome"}.
4. The pipeline in a scratch folder under data/watchlist/add/ (gitignored,
   removed afterwards whatever happens): every fill (polymarket_trades; over
   MAX_FILLS fails, too many to add without a person) to <Output>.csv,
   cleandata to <Output>_clean.csv, scrape_insiders.score_market to
   <Output>_insiders.csv, then export.export_one for its index.json entry
   and its page (over MAX_PAGE_BYTES fails). The clean and insiders CSVs
   are returned with the page (together over MAX_DATA_BYTES fails).
5. A failure says why in one line (exit 2).

--dry-run does step 1 and the naming only. The command needs the API
(WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN, as watchlist.sync reads them) for
everything but --dry-run: it saves the page there.
"""
import argparse
import itertools
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_FILLS = 1_000_000
MAX_PAGE_BYTES = 10_000_000                             # the exported page, as the site loads it
MAX_DATA_BYTES = 40_000_000                             # its clean and insiders CSVs (about 19 parts saved)
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")         # a page name (the site's API checks the same)
SUFFIXES = ("", "_clean", "_insiders")                  # the data files of an output name
WORK_DIR = os.path.join("data", "watchlist", "add")     # under the root: where a market is built (gitignored)


class AddError(Exception):
    """Why a market could not be added, in plain words."""


def pascal(name):
    """The output name for a page name: "gemini-carbon" -> "GeminiCarbon"."""
    return "".join(p[:1].upper() + p[1:] for p in str(name).split("-") if p)


def bare_slug(slug):
    """The market part of a slug (markets.json may hold "event/market#id"), as polymarket_api reads it."""
    return str(slug or "").split("#", 1)[0].split("?", 1)[0].rstrip("/").split("/")[-1].strip()


def _cut(name, n):
    """`name` cut to n characters, at the last dash when the cut would split a word (unless that leaves under 2)."""
    if len(name) <= n:
        return name
    head = name[:n]
    if name[n] != "-" and len(head.rsplit("-", 1)[0]) >= 2:
        head = head.rsplit("-", 1)[0]
    return head.strip("-")


def page_name(slug, markets, root=ROOT):
    """A free page name for a slug: its market part in lower case, each run of other characters one dash, cut to
    32 characters at a word's end ("market-x" when that leaves one character); "-2", "-3"... when another entry
    has the name or its output name, or data files of that output name are there."""
    base = _cut(re.sub(r"[^a-z0-9]+", "-", bare_slug(slug).lower()).strip("-"), 32)
    if len(base) < 2:
        base = f"market-{base}".strip("-")
    names = {m.get("name") for m in markets}
    outputs = {str(m.get("output") or "").lower() for m in markets}
    for i in itertools.count(1):
        name = base if i == 1 else f"{_cut(base, 31 - len(str(i)))}-{i}"
        out = pascal(name)
        if name not in names and out.lower() not in outputs and \
                not any(os.path.exists(os.path.join(root, "data", f"{out}{s}.csv")) for s in SUFFIXES):
            return name


def check_gamma(market, condition_id, outcome):
    """The winner's name once Gamma's market is the one asked for: this condition id, closed, resolved to
    `outcome`. Raises AddError."""
    from watchlist import filters
    if str(market.get("conditionId") or "").lower() != str(condition_id).lower():
        raise AddError("Polymarket's market for that slug has a different condition id")
    if not market.get("closed"):
        raise AddError("the market is not closed on Polymarket")
    win = filters.winning_outcome(market)
    if not win:
        raise AddError("the market has not resolved to one winner on Polymarket")
    if win[1].strip().lower() != str(outcome).strip().lower():
        raise AddError(f"it resolved {win[1]}, not {outcome}")
    return win[1]


def _steps():
    """The pipeline's own modules (imported only when a market is really added)."""
    import cleandata
    import export
    import polymarket_trades
    import scrape_insiders

    def download(slug, raw, max_rows):
        trades = polymarket_trades.main(slug=slug, output=None, max_rows=max_rows)
        if len(trades) > max_rows - 1:
            return len(trades)
        polymarket_trades.save_csv(trades, raw)
        return len(trades)

    return {"gamma": polymarket_trades.get_market, "download": download,
            "clean": lambda raw, clean: cleandata.main(input_csv=raw, output_csv=clean),
            "score": scrape_insiders.score_market,
            "export": lambda entry, data_dir: export.export_one(entry, data_dir=data_dir)}


def add(condition_id, slug, name, outcome, dry_run=False, root=ROOT, steps=None, max_fills=MAX_FILLS,
        max_page_bytes=MAX_PAGE_BYTES, added=(), work_dir=None, max_data_bytes=MAX_DATA_BYTES):
    """Build the market's page (the module docstring); name None: page_name. added: the entries of the markets
    added before (the database's). Returns {"status": "added" | "failed", "name", "note", "changed"}, and when
    changed (built, not there already, not a dry run) also "entry" (its markets.json-style entry), "index" (its
    index.json entry), "market" (its markets/<name>.json) and "data" ({"<Output>_clean.csv": text,
    "<Output>_insiders.csv": text}, what export.export_one builds them from). Writes nothing outside its scratch
    folder."""
    steps = steps or {}
    gamma = steps.get("gamma")
    if gamma is None:
        import polymarket_trades
        gamma = polymarket_trades.get_market
    added = [m for m in added or () if isinstance(m, dict)]
    scratch = None

    def failed(why):
        return {"status": "failed", "name": name, "note": why, "changed": False}

    try:
        if name is not None and not NAME.match(str(name)):
            raise AddError("the page name must be 2 to 32 lower-case letters, digits or dashes")
        with open(os.path.join(root, "markets.json"), encoding="utf-8") as f:
            markets = json.load(f)
        winner = check_gamma(gamma(bare_slug(slug)), condition_id, outcome)
        mine = bare_slug(slug)
        same = next((m for m in markets if bare_slug(m.get("slug")) == mine), None)
        if same is not None:
            files = [os.path.join(root, "data", f"{same['output']}{s}.csv") for s in SUFFIXES[1:]]
            if all(os.path.exists(p) for p in files):
                return {"status": "added", "name": same["name"], "note": "already on the main page", "changed": False}
            raise AddError(f"it is in markets.json as {same['name']!r} without its data files; run run.py for it")
        same = next((m for m in added if bare_slug(m.get("slug")) == mine), None)
        if same is not None:
            return {"status": "added", "name": same.get("name"), "note": "already on the main page", "changed": False}
        every = markets + added
        if name is None:
            name = page_name(mine, every, root)
        if any(m.get("name") == name for m in every):
            raise AddError(f"the page name {name!r} is taken by another market")
        out = pascal(name)
        if any(str(m.get("output") or "").lower() == out.lower() for m in every):
            raise AddError(f"the output name {out!r} is taken by another market")
        if any(os.path.exists(os.path.join(root, "data", f"{out}{s}.csv")) for s in SUFFIXES):
            raise AddError(f"data files named {out} are in data/ already; give it another name")
        if dry_run:
            return {"status": "added", "name": name, "note": f"dry run: checked on Polymarket, would add it as {out}",
                    "changed": False}
        real = None if all(k in steps for k in ("download", "clean", "score", "export")) else _steps()
        step = {k: steps.get(k) or real[k] for k in ("download", "clean", "score", "export")}
        entry = {"name": name, "slug": mine, "output": out, "correct_outcome": winner}
        base = work_dir or os.path.join(root, WORK_DIR)
        os.makedirs(base, exist_ok=True)
        scratch = tempfile.mkdtemp(prefix=f"{name}-", dir=base)
        raw, clean, insiders = (os.path.join(scratch, f"{out}{s}.csv") for s in SUFFIXES)
        n = step["download"](mine, raw, max_fills + 1)
        if n > max_fills:
            raise AddError(f"too many trades to add automatically (over {max_fills:,} fills)")
        if not n or not os.path.exists(raw):
            raise AddError("Polymarket served no trades for it")
        step["clean"](raw, clean)
        step["score"](entry, clean, insiders)
        index, market = step["export"](entry, scratch)
        size = len(json.dumps(market, separators=(",", ":"), allow_nan=False))
        if size > max_page_bytes:
            raise AddError(f"its page is {size / 1e6:.0f} MB, over the {max_page_bytes / 1e6:.0f} MB an automatic add "
                           f"may save")
        json.dumps(index, allow_nan=False)
        data = {}
        for path in (clean, insiders):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data[os.path.basename(path)] = f.read()
        size = sum(len(v.encode("utf-8")) for v in data.values())
        if size > max_data_bytes:
            raise AddError(f"its data files are {size / 1e6:.0f} MB, over the {max_data_bytes / 1e6:.0f} MB an automatic "
                           f"add may save")
    except AddError as e:
        return failed(str(e))
    except Exception as e:                                   # a network or pipeline error: say which step's kind
        return failed(f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return {"status": "added", "name": name, "note": None, "changed": True, "entry": entry, "index": index,
            "market": market, "data": data}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add one closed market to the main page (its page, saved in the site's "
                                             "database).")
    ap.add_argument("--condition-id", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--name", default=None, help="the page name: lower-case letters, digits and dashes (default: "
                                                 "made from the slug)")
    ap.add_argument("--outcome", required=True, help="the outcome it resolved to, as Polymarket names it")
    ap.add_argument("--dry-run", action="store_true", help="check it on Polymarket and the name only")
    args = ap.parse_args(argv)
    from watchlist import blobs, sync
    c = sync.client()
    if c is None and not args.dry_run:
        print("No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN): an added market is saved in the site's "
              "database", file=sys.stderr)
        return 2
    added = []
    if c is not None:
        try:
            added = [(h.get("meta") or {}).get("entry") for h in blobs.heads(c, "market:")]
        except sync.ApiError as e:
            print(f"could not read the markets added before: {e}", file=sys.stderr)
            return 2
    got = add(args.condition_id, args.slug, args.name, args.outcome, dry_run=args.dry_run, added=added)
    if got["status"] == "added" and got.get("changed"):
        try:
            sync.store_market(c, got, args.condition_id)
        except (sync.ApiError, blobs.BlobError, ValueError) as e:
            print(f"could not save {got['name']}: {e}", file=sys.stderr)
            return 2
        host = c.base.split("://", 1)[-1]
        print(f"added {got['name']}: saved to {host}; the next deploy puts it on the main page")
        return 0
    if got["status"] == "added":
        print(f"added {got['name']}" + (f" ({got['note']})" if got.get("note") else ""))
        return 0
    print(f"could not add {got.get('name') or args.slug}: {got['note']}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
