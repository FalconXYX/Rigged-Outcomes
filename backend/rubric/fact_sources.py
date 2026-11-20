"""
Where a market's inputs come from, and the CLI that writes one facts file per market (wallet_facts).

  from_git(entry, ref)      a markets.json market from a git ref's committed CSVs (data/<Output>.csv,
                            _clean.csv, _insiders.csv), e.g. the original-project branch
  from_live(name, source)   a market added on the site: its page (today's flags) from the site's API,
                            its fills from Polymarket (read only; cached like every other request)

CLI:
  python3 -m backend.rubric.fact_sources --out DIR --cache DIR [--market NAME ...]
      [--ref original-project] [--api https://rigged-outcomes-api.parthkj6.workers.dev]
  Every markets.json market from --ref, plus every market the site's index lists that markets.json does
  not, unless --market names some. Writes DIR/<name>.json.
"""
import argparse
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.rubric import wallet_facts as wf

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API = "https://rigged-outcomes-api.parthkj6.workers.dev"
EASTERN = ZoneInfo("America/New_York")      # markets.json importantDates are Eastern wall-clock times


def _git_csv(ref, path, **kw):
    import pandas as pd
    text = subprocess.run(["git", "-C", ROOT, "show", f"{ref}:{path}"], check=True, capture_output=True,
                          text=True).stdout
    return pd.read_csv(io.StringIO(text), **kw)


def _fills(raw):
    """wallet_facts' fills frame from raw-CSV rows (polymarket_trades.format_row's columns)."""
    import pandas as pd
    return pd.DataFrame({
        "wallet": raw["user_id"].astype(str).str.lower(),
        "side": raw["side"].astype(str).str.upper(),
        "outcome": raw["outcome"].astype(str),
        "price": pd.to_numeric(raw["odds"], errors="coerce").fillna(0.0),
        "contracts": pd.to_numeric(raw["contracts"], errors="coerce").fillna(0.0),
        "dollars": pd.to_numeric(raw["dollar_amount"], errors="coerce").fillna(0.0),
        "ts": ((pd.to_datetime(raw["timestamp"], utc=True) - pd.Timestamp(0, tz="UTC"))
               // pd.Timedelta(seconds=1)).astype("int64"),
    })


def _epoch(iso):
    import pandas as pd
    return int(pd.to_datetime(iso, utc=True).timestamp())


def _news(entry):
    out = []
    for k, label in (entry.get("importantDates") or {}).items():
        t = datetime.strptime(k, "%Y-%m-%d-%H:%M").replace(tzinfo=EASTERN)
        out.append((int(t.timestamp()), label))
    return out


def _flags(prior, conc, first_x=None, percent=None):
    import scrape_insiders as si
    first_x = prior <= si.FIRST_X_TRADES if first_x is None else bool(first_x)
    percent = conc >= si.PERCENT_VOLUME if percent is None else bool(percent)
    return {"prior_bet_count": int(prior), "concentration": float(conc), "is_first_x": first_x,
            "is_percent": percent, "is_suspicious": first_x or percent, "category": wf.category(first_x, percent),
            "risk_score": round(wf.risk_score(prior, conc), 3)}


def from_frames(entry, raw, clean, ins, news=None):
    """wallet_facts.Market of a markets.json-style entry ({name, slug, correct_outcome, importantDates?}) from its
    fills (raw-CSV rows as a frame: polymarket_trades.format_row's columns), its clean frame and its insiders
    frame: the whales are the insiders rows (the site's set), each joined to its clean row by wallet and first
    trade."""
    eoa = {}
    if "eoa_address" in clean.columns:
        for u, e in zip(clean["user_id"].astype(str).str.lower(), clean["eoa_address"]):
            if isinstance(e, str) and e.strip():
                eoa.setdefault(u, e.strip().lower())
    whales = []
    for r in ins.to_dict("records"):
        u = str(r["user_id"]).lower()
        whales.append({"wallet": eoa.get(u, u), "user_id": u, "first_trade": str(r["target_trade_time"]),
                       "first_buy_ts": _epoch(r["target_trade_time"]),
                       **_flags(r["prior_bet_count"], r["portfolio_concentration"], r["is_first_x"],
                                r["Is_percent"])})
    return wf.Market(name=entry["name"], slug=entry["slug"], title=str(clean["market_title"].iloc[0]),
                     outcome=entry["correct_outcome"], condition_id=str(clean["market_id"].iloc[0]).lower(),
                     fills=_fills(raw), clean=clean, whales=whales,
                     news=_news(entry) if news is None else news)


def from_git(entry, ref="original-project"):
    """wallet_facts.Market of a markets.json entry from `ref`'s committed CSVs (data/<Output>.csv, _clean.csv,
    _insiders.csv)."""
    out = entry["output"]
    raw = _git_csv(ref, f"data/{out}.csv", usecols=["user_id", "side", "outcome", "odds", "contracts",
                                                    "dollar_amount", "timestamp"])
    return from_frames(entry, raw, _git_csv(ref, f"data/{out}_clean.csv"), _git_csv(ref, f"data/{out}_insiders.csv"))


def from_record(entry, clean_csv, insiders_csv, fills):
    """wallet_facts.Market of a market on the site from its data record (backend.site: the clean and insiders
    CSVs' text) and its fills (an add_market.Fills, as the add steps walk them)."""
    import pandas as pd
    raw = pd.DataFrame(list(fills.rows()), columns=["user_id", "eoa_address", "side", "market_id", "market_title",
                                                    "outcome", "odds", "contracts", "dollar_amount", "win_status",
                                                    "timestamp"])
    return from_frames(entry, raw, pd.read_csv(io.StringIO(clean_csv)), pd.read_csv(io.StringIO(insiders_csv)))


def from_live(name, source, api=API):
    """wallet_facts.Market of a market added on the site: today's flags from its page on the site's API
    (GET only), its fills from Polymarket through `source` (cached), its whales as add_market picks them, joined to the page's rows by wallet."""
    import add_market as A
    import pandas as pd
    import polymarket_api
    import requests

    page = requests.get(f"{api}/api/data/markets/{name}.json", timeout=60)
    page.raise_for_status()
    page = page.json()
    market = polymarket_api.fetch_market(A.bare_slug(page["slug"]))
    entry = {"name": name, "slug": page["slug"], "correct_outcome": page["correctOutcome"]}
    rows, capped = source.pages("trades", A.trade_params(market["conditionId"]), 20_000)
    if capped:
        raise RuntimeError(f"{name}: too many fills to read")
    fills = A.Fills(market)
    fills.add_page(rows)
    clean = A.frame(A.clean_text(fills)[0])
    pairs = A.whale_list(entry, clean)
    traders = (page.get("traders") or {}).get("traders") or []
    if len(pairs) != len(traders):
        raise RuntimeError(f"{name}: {len(pairs)} whales from the fills, {len(traders)} on the site's page")
    by_wallet = {}
    for t in traders:
        by_wallet.setdefault(str(t["id"]).lower(), []).append(t)
    whales = []
    for wallet, cutoff in pairs:                   # the page lists them in the order the steps finished
        mine = by_wallet.get(wallet.lower()) or []
        if len(mine) != 1:
            raise RuntimeError(f"{name}: {len(mine)} rows for whale {wallet} on the site's page")
        t = mine[0]
        whales.append({"wallet": wallet.lower(), "user_id": wallet.lower(),
                       "first_trade": datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),
                       "first_buy_ts": int(cutoff),
                       **_flags(t["priorBets"], t["concentration"], t["isFirstX"], t["isPercent"])})
    raw = pd.DataFrame(list(fills.rows()))
    return wf.Market(name=name, slug=page["slug"], title=page["title"], outcome=page["correctOutcome"],
                     condition_id=market["conditionId"], fills=_fills(raw), clean=clean, whales=whales, news=[])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--market", action="append")
    ap.add_argument("--ref", default="original-project")
    ap.add_argument("--api", default=API)
    ap.add_argument("--rate", type=float, default=5.0)
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args(argv)

    import requests
    with open(os.path.join(ROOT, "markets.json"), encoding="utf-8") as f:
        entries = {e["name"]: e for e in json.load(f)}
    index = requests.get(f"{a.api}/api/data/index.json", timeout=60).json()
    live = [m["name"] for m in index.get("markets", []) if m["name"] not in entries]
    names = a.market or (list(entries) + live)
    source = wf.Polymarket(a.cache, rate=a.rate)
    os.makedirs(a.out, exist_ok=True)
    for name in names:
        before = source.requests
        market = from_git(entries[name], a.ref) if name in entries else from_live(name, source, a.api)
        n = len(market.whales)
        doc = wf.market_facts(market, source, workers=a.workers,
                              on_whale=lambda i: print(f"  {name}: {i + 1}/{n}", end="\r", flush=True))
        path = os.path.join(a.out, f"{name}.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        os.replace(path + ".tmp", path)
        print(f"{name}: {n} whales, {source.requests - before} Polymarket requests -> {path}", flush=True)


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    main()
