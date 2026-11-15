#!/usr/bin/env python3
"""
Add one closed market to the main page: the job's step for a market a
visitor sent there with "Add to main page" (watchlist.sync add,
docs/DEPLOY.md), and a command a person can run too.

    python3 add_market.py --condition-id 0x... --slug SLUG [--name NAME] --outcome Yes [--dry-run]

1. Polymarket. Gamma's market for the slug must have this condition id, be
   closed, and have resolved to --outcome (watchlist.filters.winning_outcome).
2. Already there. When the slug is in markets.json and both its CSVs exist,
   there is nothing to do: it reports "added", so a run after a failed
   report is harmless. A name another slug uses fails.
3. The entry. With no --name, the page name comes from the slug
   (page_name: "will-gemini-carbon-ship" -> "will-gemini-carbon-ship", cut
   to 32 characters at a word's end, "-2" when it is taken). Its output is the name in
   PascalCase ("gemini-carbon" -> "GeminiCarbon"), which no entry may use
   already; markets.json gains
   {"name", "slug", "output", "correct_outcome"}, written the way the file
   is (json, indent 2, a final newline).
4. run.py's two steps for it: every fill (polymarket_trades; over MAX_FILLS
   fails, too many to add without a person) to data/<Output>.csv, then
   cleandata to data/<Output>_clean.csv (over MAX_CLEAN_BYTES fails: GitHub
   warns at 50 MB a file).
5. scrape_insiders.score_market to data/<Output>_insiders.csv, then
   export.main() as a check that the site still builds with it.
6. Any failure puts markets.json back byte for byte and removes the CSVs it
   made, then says why in one line (exit 2).

--dry-run does step 1 and the naming only. The job commits markets.json and
the two CSVs (the raw data/<Output>.csv is gitignored) to main.
"""
import argparse
import contextlib
import itertools
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_FILLS = 1_000_000
MAX_CLEAN_BYTES = 45_000_000
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")         # a page name (the site's API checks the same)
SUFFIXES = ("", "_clean", "_insiders")                  # the data files of an output name


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


def render(markets, original=None):
    """markets.json's text for a list of entries: json with indent 2 and a final newline, which is how the file
    is written. If `original` is not in that form, the new entries are inserted as text before its final "]"
    instead, so the rest of the file stays as it was."""
    text = json.dumps(markets, indent=2, ensure_ascii=False) + "\n"
    if original is None:
        return text
    old = json.loads(original)
    if json.dumps(old, indent=2, ensure_ascii=False) + "\n" == original:
        return text
    added = markets[len(old):]
    end = original.rstrip().rfind("]")
    body = original[:end].rstrip()
    extra = "".join(",\n" + "\n".join("  " + line for line in json.dumps(m, indent=2, ensure_ascii=False).split("\n"))
                    for m in added)
    return (body + extra if old else body + extra.lstrip(",")) + "\n" + original[end:].lstrip()


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


@contextlib.contextmanager
def _cwd(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


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
            "score": scrape_insiders.score_market, "export": export.main}


def add(condition_id, slug, name, outcome, dry_run=False, root=ROOT, steps=None, max_fills=MAX_FILLS,
        max_clean_bytes=MAX_CLEAN_BYTES):
    """Add the market (the module docstring); name None: page_name. Returns {"status": "added" | "failed",
    "name", "note", "changed"}: changed is False when it was there already (or in a dry run)."""
    steps = steps or {}
    gamma = steps.get("gamma")
    if gamma is None:
        import polymarket_trades
        gamma = polymarket_trades.get_market
    markets_path = os.path.join(root, "markets.json")
    with open(markets_path, "rb") as f:
        original = f.read()
    made = []

    def failed(why):
        with open(markets_path, "wb") as f:
            f.write(original)
        for p in made:
            for q in (p, p + ".tmp"):
                if os.path.exists(q):
                    os.remove(q)
        return {"status": "failed", "name": name, "note": why, "changed": False}

    try:
        if name is not None and not NAME.match(str(name)):
            raise AddError("the page name must be 2 to 32 lower-case letters, digits or dashes")
        markets = json.loads(original.decode("utf-8"))
        winner = check_gamma(gamma(bare_slug(slug)), condition_id, outcome)
        mine = bare_slug(slug)
        same = next((m for m in markets if bare_slug(m.get("slug")) == mine), None)
        if same is not None:
            files = [os.path.join(root, "data", f"{same['output']}{s}.csv") for s in SUFFIXES[1:]]
            if all(os.path.exists(p) for p in files):
                return {"status": "added", "name": same["name"], "note": "already on the main page", "changed": False}
            raise AddError(f"it is in markets.json as {same['name']!r} without its data files; run run.py for it")
        if name is None:
            name = page_name(mine, markets, root)
        if any(m.get("name") == name for m in markets):
            raise AddError(f"the page name {name!r} is taken by another market")
        out = pascal(name)
        if any(str(m.get("output") or "").lower() == out.lower() for m in markets):
            raise AddError(f"the output name {out!r} is taken by another market")
        raw, clean, insiders = (os.path.join(root, "data", f"{out}{s}.csv") for s in SUFFIXES)
        if any(os.path.exists(p) for p in (raw, clean, insiders)):
            raise AddError(f"data files named {out} are there already; nothing was overwritten")
        if dry_run:
            return {"status": "added", "name": name, "note": f"dry run: checked on Polymarket, would add it as {out}",
                    "changed": False}
        real = None if all(k in steps for k in ("download", "clean", "score", "export")) else _steps()
        step = {k: steps.get(k) or real[k] for k in ("download", "clean", "score", "export")}
        entry = {"name": name, "slug": mine, "output": out, "correct_outcome": winner}
        with open(markets_path, "w", encoding="utf-8") as f:
            f.write(render(markets + [entry], original.decode("utf-8")))
        made += [raw, clean, insiders]
        with _cwd(root):
            n = step["download"](mine, raw, max_fills + 1)
            if n > max_fills:
                raise AddError(f"too many trades to add automatically (over {max_fills:,} fills)")
            if not n or not os.path.exists(raw):
                raise AddError("Polymarket served no trades for it")
            step["clean"](raw, clean)
            if os.path.getsize(clean) > max_clean_bytes:
                raise AddError(f"its clean data is {os.path.getsize(clean) / 1e6:.0f} MB, over the "
                               f"{max_clean_bytes / 1e6:.0f} MB an automatic add may commit")
            step["score"](entry, clean, insiders)
            step["export"]()
    except AddError as e:
        return failed(str(e))
    except Exception as e:                                   # a network or pipeline error: say which step's kind
        return failed(f"{type(e).__name__}: {str(e)[:200]}")
    return {"status": "added", "name": name, "note": None, "changed": True}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add one closed market to the main page (markets.json and its data).")
    ap.add_argument("--condition-id", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--name", default=None, help="the page name: lower-case letters, digits and dashes (default: "
                                                 "made from the slug)")
    ap.add_argument("--outcome", required=True, help="the outcome it resolved to, as Polymarket names it")
    ap.add_argument("--dry-run", action="store_true", help="check it on Polymarket and the name only")
    args = ap.parse_args(argv)
    got = add(args.condition_id, args.slug, args.name, args.outcome, dry_run=args.dry_run)
    if got["status"] == "added":
        print(f"added {got['name']}" + (f" ({got['note']})" if got.get("note") else ""))
        return 0
    print(f"could not add {got.get('name') or args.slug}: {got['note']}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
