#!/usr/bin/env python3
"""
Add one closed market to the main page: what "Add to main page" (and a watched market that closed) does, in the
backend's add steps (backend/add_steps.py: add.fetch, add.whales, add.export), and a command a person can run too.
Nothing goes to git and nothing goes to disk: the market's page and its data record are saved in the site's
database (backend.site.put_market), and the Worker serves the page and the added markets' index from there, so it
is on the main page with no site rebuild. markets.json is never changed.

    python3 -m pipeline.add_market --condition-id 0x... --slug SLUG [--name NAME] --outcome Yes [--dry-run]

1. Polymarket. Gamma's market for the slug must have this condition id, be
   closed, and have resolved to --outcome (watchlist.filters.winning_outcome).
2. Already there. When the slug is in markets.json (the 7 original
   markets, served from the database: backend.site originals), or among the
   markets added before (`added`, the database's market:* entries), there is
   nothing to do: it reports "added", so a run after a failed report is
   harmless. A name another slug uses fails.
3. The entry. With no --name, the page name comes from the slug
   (page_name: "will-gemini-carbon-ship" -> "will-gemini-carbon-ship", cut
   to 32 characters at a word's end, "-2" when it is taken). Its output is
   the name in PascalCase ("gemini-carbon" -> "GeminiCarbon"), which no
   entry may use already: {"name", "slug", "output", "correct_outcome"}.
4. The pipeline, in memory. Every fill comes off the trade feed page by page
   into Fills (a few numbers a fill, never the feed's rows; over MAX_FILLS
   fails), cleandata.clean_rows makes the positions, scrape_insiders scores
   the whales (each history added up page by page), and export.export_one
   builds its index.json entry and its page (over MAX_PAGE_BYTES fails).
   The clean and insiders CSVs' text is returned with the page (together
   over MAX_DATA_BYTES fails). The page is byte for byte the one the
   file-based pipeline (polymarket_trades, cleandata, scrape_insiders and
   export over data/*.csv) builds.
5. A failure says why in one line (exit 2).

--dry-run does step 1 and the naming only. The command needs the API
(WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN, as watchlist.sync reads them) for
everything but --dry-run: it saves the page there.
"""
import argparse
import array
import base64
import io
import itertools
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the repo root (markets.json, data/)
MAX_FILLS = 400_000                                     # about twice US x Iran's, inside the 2 GB step (its clean: 377 MB)
MAX_PAGE_BYTES = 10_000_000                             # the exported page, as the site loads it
MAX_DATA_BYTES = 40_000_000                             # its clean and insiders CSVs
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")         # a page name (the site's API checks the same)
SUFFIXES = ("", "_clean", "_insiders")                  # the data files of an output name
MARKET_FIELDS = ("conditionId", "question", "closed", "outcomePrices")   # what a fill's row reads of the market


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


def read_markets(root=ROOT):
    with open(os.path.join(root, "markets.json"), encoding="utf-8") as f:
        return json.load(f)


def plan(slug, name, winner, markets, added=(), root=ROOT):
    """(entry, None) for a market to build, or (None, result) when it is on the main page already. markets:
    markets.json's entries; added: the markets added before. Raises AddError."""
    added = [m for m in added or () if isinstance(m, dict)]
    mine = bare_slug(slug)
    same = next((m for m in markets + added if bare_slug(m.get("slug")) == mine), None)
    if same is not None:
        return None, {"status": "added", "name": same.get("name"), "note": "already on the main page",
                      "changed": False}
    every = markets + added
    if name is None:
        name = page_name(mine, every, root)
    if not NAME.match(str(name)):
        raise AddError("the page name must be 2 to 32 lower-case letters, digits or dashes")
    if any(m.get("name") == name for m in every):
        raise AddError(f"the page name {name!r} is taken by another market")
    out = pascal(name)
    if any(str(m.get("output") or "").lower() == out.lower() for m in every):
        raise AddError(f"the output name {out!r} is taken by another market")
    if any(os.path.exists(os.path.join(root, "data", f"{out}{s}.csv")) for s in SUFFIXES):
        raise AddError(f"data files named {out} are in data/ already; give it another name")
    return {"name": name, "slug": mine, "output": out, "correct_outcome": winner}, None


# ── the fills, page by page ──────────────────────────────────────────────────

def _iso(ts, cache):
    s = cache.get(ts)
    if s is None:
        s = cache[ts] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return s


class Fills:
    """A market's fills as they come off /v2/trades: each one's numbers exactly as polymarket_trades.format_row
    rounds them, and its wallet, side, outcome and win status as one small index, so USxIran's 218,000 fills
    take about 8 MB where the feed's rows took 650. rows() gives them back as the raw CSV's rows, oldest first in
    the order polymarket_trades.to_trade_rows sorts them (ties in the feed's order), for cleandata.clean_rows.
    dump() and load() carry them from one step to the next."""

    def __init__(self, market):
        self.market = {k: market.get(k) for k in MARKET_FIELDS}
        self.kinds, self._index = [], {}
        self.kind, self.ts = array.array("i"), array.array("q")
        self.odds, self.contracts, self.dollars = array.array("d"), array.array("d"), array.array("d")

    @property
    def n(self):
        return len(self.ts)

    def add_page(self, rows):
        """A page of /v2/trades rows (polymarket_api.v2_pages); the CLOB operator's rows are left out, as
        polymarket_api.fetch_trades leaves them."""
        from pipeline import polymarket_api
        from pipeline import polymarket_trades
        for r in rows:
            w = str(r.get("proxy_wallet") or "").lower()
            if not w or w in polymarket_api.OPERATORS:
                continue
            v1 = polymarket_api.as_v1(r)
            t = polymarket_trades.format_row(v1, self.market, eoa_address=w)
            key = (t["user_id"], t["eoa_address"], t["side"], t["outcome"], t["win_status"])
            i = self._index.get(key)
            if i is None:
                i = self._index[key] = len(self.kinds)
                self.kinds.append(key)
            self.kind.append(i)
            self.ts.append(int(v1["timestamp"]))
            self.odds.append(t["odds"])
            self.contracts.append(t["contracts"])
            self.dollars.append(t["dollar_amount"])

    def rows(self):
        """The raw CSV's rows (polymarket_trades.format_row's keys), oldest first; made one at a time."""
        mid, title = self.market.get("conditionId", "") or "", self.market.get("question", "") or ""
        cache = {}
        for i in sorted(range(self.n), key=self.ts.__getitem__):
            user, eoa, side, outcome, status = self.kinds[self.kind[i]]
            yield {"user_id": user, "eoa_address": eoa, "side": side, "market_id": mid, "market_title": title,
                   "outcome": outcome, "odds": self.odds[i], "contracts": self.contracts[i],
                   "dollar_amount": self.dollars[i], "win_status": status, "timestamp": _iso(self.ts[i], cache)}

    def dump(self):
        """A JSON-ready copy (load() reads it back)."""
        def b64(a):
            return base64.b64encode(a.tobytes()).decode("ascii")
        return {"market": self.market, "kinds": [list(k) for k in self.kinds], "byteorder": sys.byteorder,
                **{k: b64(getattr(self, k)) for k in ("kind", "ts", "odds", "contracts", "dollars")}}

    @classmethod
    def load(cls, doc):
        f = cls(doc["market"])
        f.kinds = [tuple(k) for k in doc["kinds"]]
        f._index = {k: i for i, k in enumerate(f.kinds)}
        for k in ("kind", "ts", "odds", "contracts", "dollars"):
            a = getattr(f, k)
            a.frombytes(base64.b64decode(doc[k]))
            if doc.get("byteorder", sys.byteorder) != sys.byteorder:
                a.byteswap()
        return f


def trade_params(condition_id):
    """/v2/trades' query for every fill of a market, maker and taker rows (polymarket_api.fetch_trades')."""
    from pipeline import polymarket_api
    return {"condition": str(condition_id), "taker_only": "false", "limit": polymarket_api.PAGE_SIZE}


# ── clean, score, export, in memory ──────────────────────────────────────────

def clean_text(fills):
    """(the clean CSV's text, positions) of the fills: byte for byte what cleandata.main writes from the raw CSV."""
    from pipeline import cleandata
    clean, _ = cleandata.clean_rows(fills.rows())
    if not clean:
        raise AddError("Polymarket served no trades for it")
    return cleandata.to_text(clean), len(clean)


def frame(text):
    """A CSV's text as pandas reads the file (the frame the file-based pipeline's next step reads)."""
    import pandas as pd
    return pd.read_csv(io.StringIO(text))


def insiders_text(df):
    """The insiders frame as scrape_insiders.score_market writes its file."""
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()


def saved_text(text):
    """A CSV's text as reading its file in text mode gives it (each line ending one "\\n"): what the data record
    keeps, as the add did when it read its files back."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def whale_list(entry, clean):
    """[[wallet, first-trade epoch]] of the whales to score, in scoring order, each pair once."""
    from pipeline import scrape_insiders
    whales, _ = scrape_insiders.select_whales(entry, clean.copy(), quiet=True)
    out = []
    for _, row in whales.iterrows():
        pair = [scrape_insiders.wallet_for(row), scrape_insiders.whale_cutoff(row)]
        if pair not in out:
            out.append(pair)
    return out


def score(entry, clean, totals_for):
    """The insiders frame from the clean frame and each whale's history totals: totals_for(wallet, cutoff) ->
    {"stake", "txs", ...}, raising when it has none. scrape_insiders.score_frame's frame, whales in order."""
    from pipeline import scrape_insiders
    whales, title = scrape_insiders.select_whales(entry, clean.copy(), quiet=True)
    results = [scrape_insiders.score_whale(row, totals_for(scrape_insiders.wallet_for(row),
                                                           scrape_insiders.whale_cutoff(row)))
               for _, row in whales.iterrows()]
    return scrape_insiders.insiders_frame(results, title)


def build(entry, clean_csv, insiders_csv, max_page_bytes=MAX_PAGE_BYTES, max_data_bytes=MAX_DATA_BYTES):
    """(index entry, page, data) from the clean and insiders CSVs' text: export.export_one's entry and page, and
    the data record ({"<Output>_clean.csv": text, "<Output>_insiders.csv": text}). Raises AddError over a cap,
    ValueError for a page the site could not read (NaN)."""
    from pipeline import export
    index, page = export.export_one(entry, clean=frame(clean_csv), insiders=frame(insiders_csv))
    size = len(export.page_bytes(page))
    if size > max_page_bytes:
        raise AddError(f"its page is {size / 1e6:.0f} MB, over the {max_page_bytes / 1e6:.0f} MB an automatic add "
                       f"may save")
    json.dumps(index, allow_nan=False)
    data = {f"{entry['output']}_clean.csv": saved_text(clean_csv),
            f"{entry['output']}_insiders.csv": saved_text(insiders_csv)}
    size = sum(len(v.encode("utf-8")) for v in data.values())
    if size > max_data_bytes:
        raise AddError(f"its data files are {size / 1e6:.0f} MB, over the {max_data_bytes / 1e6:.0f} MB an automatic "
                       f"add may save")
    return index, page, data


def _real_steps():
    from pipeline import polymarket_api
    from pipeline import scrape_insiders
    return {"gamma": polymarket_api.fetch_market,
            "pages": lambda cid: polymarket_api.v2_pages("trades", trade_params(cid)),
            "history": scrape_insiders.history_totals}


def add(condition_id, slug, name, outcome, dry_run=False, root=ROOT, steps=None, max_fills=MAX_FILLS,
        max_page_bytes=MAX_PAGE_BYTES, added=(), max_data_bytes=MAX_DATA_BYTES, **_):
    """Build the market's page in one process (the module docstring); name None: page_name. added: the entries of
    the markets added before (the database's). steps: stand-ins for Polymarket, {"gamma": slug -> market,
    "pages": condition id -> /v2/trades pages, "history": (wallet, cutoff) -> history totals}. Returns {"status":
    "added" | "failed", "name", "note", "changed"}, and when changed (built, not there already, not a dry run)
    also "entry" (its markets.json-style entry), "index" (its index.json entry), "market" (its
    markets/<name>.json) and "data" ({"<Output>_clean.csv": text, "<Output>_insiders.csv": text}, what its page
    is built from). A failure an exception caused (not a reason the market cannot be added) also carries "error"
    (the exception) and "traceback", so a caller tells Polymarket not answering from a crash. Writes no file."""
    steps = dict(steps or {})
    real = None if all(k in steps for k in ("gamma", "pages", "history")) else _real_steps()
    step = {k: steps.get(k) or real[k] for k in ("gamma", "pages", "history")}

    def failed(why):
        return {"status": "failed", "name": name, "note": why, "changed": False}

    try:
        if name is not None and not NAME.match(str(name)):
            raise AddError("the page name must be 2 to 32 lower-case letters, digits or dashes")
        markets = read_markets(root)
        market = step["gamma"](bare_slug(slug))
        winner = check_gamma(market, condition_id, outcome)
        entry, there = plan(slug, name, winner, markets, added, root)
        if there is not None:
            return there
        name = entry["name"]
        if dry_run:
            return {"status": "added", "name": name, "note": f"dry run: checked on Polymarket, would add it as "
                                                             f"{entry['output']}", "changed": False}
        fills = Fills(market)
        for page in step["pages"](market["conditionId"]):
            fills.add_page(page)
            if fills.n > max_fills:
                raise AddError(f"too many trades to add automatically (over {max_fills:,} fills)")
        if not fills.n:
            raise AddError("Polymarket served no trades for it")
        csv_clean, _ = clean_text(fills)
        from pipeline import scrape_insiders
        csv_insiders = insiders_text(scrape_insiders.score_frame(entry, frame(csv_clean), totals_for=step["history"]))
        index, page, data = build(entry, csv_clean, csv_insiders, max_page_bytes, max_data_bytes)
    except AddError as e:
        return failed(str(e))
    except Exception as e:                                   # a network or pipeline error: say which step's kind
        return dict(failed(f"{type(e).__name__}: {str(e)[:200]}"), error=e, traceback=traceback.format_exc())
    return {"status": "added", "name": name, "note": None, "changed": True, "entry": entry, "index": index,
            "market": page, "data": data}


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
    from backend import site
    from watchlist import blobs, sync
    c = sync.client()
    if c is None and not args.dry_run:
        print("No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN): an added market is saved in the site's "
              "database", file=sys.stderr)
        return 2
    added = []
    if c is not None:
        try:
            added = site.added_entries(c)
        except sync.ApiError as e:
            print(f"could not read the markets added before: {e}", file=sys.stderr)
            return 2
    got = add(args.condition_id, args.slug, args.name, args.outcome, dry_run=args.dry_run, added=added)
    if got["status"] == "added" and got.get("changed"):
        try:
            site.put_market(c, got["entry"], got["index"], got["market"], got["data"], args.condition_id, by="local")
        except (sync.ApiError, blobs.BlobError, site.NameTaken, ValueError) as e:
            print(f"could not save {got['name']}: {e}", file=sys.stderr)
            return 2
        host = c.base.split("://", 1)[-1]
        print(f"added {got['name']}: saved to {host}; it is on the main page now")
        return 0
    if got["status"] == "added":
        print(f"added {got['name']}" + (f" ({got['note']})" if got.get("note") else ""))
        return 0
    print(f"could not add {got.get('name') or args.slug}: {got['note']}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
