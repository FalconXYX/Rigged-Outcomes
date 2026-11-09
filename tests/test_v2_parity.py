#!/usr/bin/env python3
"""
Parity of the Data API v2 pipeline against two committed v1-era markets.

    PARITY_NETWORK=1 python3 -m pytest -q tests     # or
    python3 tests/test_v2_parity.py                 # same checks, prints the report, exits 1 on failure

Needs the network (read-only calls to Polymarket) and 2-3 minutes, so under
pytest the whole module is skipped unless PARITY_NETWORK=1 is set; the
direct entry point always runs. tests/test_offline.py holds the unit tests
that run without the network.
Nothing under data/ is written: outputs go to $PARITY_DIR (default:
<system temp dir>/rigged-outcomes-parity).
PARITY_REUSE=1 reuses a previous run's CSVs in $PARITY_DIR instead of fetching.

What "parity" means here. The committed CSVs were produced by the retired
crawl (subgraph addresses -> one v1 /activity walk per wallet). That walk was
lossy: on a 429 or any error it returned the rows it had so far, newest first,
so a busy wallet kept only its newest fills in this market and a wallet whose
first page failed is missing altogether. The v2 market feed is one walk over
every fill, so the expectation is:
  * same first and last fill timestamps;
  * every committed wallet, position and raw row is present in v2 (v2 is a
    superset: nothing the old crawl saw is missing from the feed);
  * positions agree within tolerance (<= $1 or <= 0.5 %) wherever the old
    crawl was complete, and every disagreement is v2 > v1, i.e. one-sided
    (rows the old crawl dropped), never v2 < v1;
  * the dropped rows are the OLDEST rows of their wallet (the signature of a
    newest-first walk stopping early), not scattered gaps.
Each difference class is counted and printed with examples.
"""
import csv
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if __name__ != "__main__" and os.environ.get("PARITY_NETWORK") != "1":
    # Collected by pytest: stay offline unless asked. (The direct entry point
    # below is the explicit ask.)
    try:
        import pytest
    except ImportError:  # pragma: no cover - imported by something other than pytest
        pass
    else:
        pytest.skip("network parity suite (2-3 min against Polymarket); set PARITY_NETWORK=1 to run it",
                    allow_module_level=True)

import cleandata  # noqa: E402
import polymarket_api  # noqa: E402
import polymarket_trades  # noqa: E402
import scrape_insiders  # noqa: E402

PARITY_DIR = os.environ.get("PARITY_DIR") or os.path.join(tempfile.gettempdir(), "rigged-outcomes-parity")
REUSE = os.environ.get("PARITY_REUSE") == "1"

MARKETS = {
    "fired":  {"slug": "kristi-noem-out-by-march-31", "output": "KristiNoemFiredMarch31", "correct_outcome": "Yes"},
    "openai": {"slug": "openai-browser-in-2025",      "output": "OpenAIBrowser2025",      "correct_outcome": "Yes"},
}
TOL_ABS, TOL_REL = 1.0, 0.005          # <= $1 or <= 0.5 %
MIN_WITHIN_TOL = 0.90                  # share of shared positions that must agree within tolerance
ARTIFACT_WALLET = "0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b"

_CACHE = {}
REPORT = []


def say(line=""):
    REPORT.append(line)
    print(line, flush=True)


def table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    say(fmt.format(*headers))
    say(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        say(fmt.format(*r))


def within(v1, v2):
    return abs(v2 - v1) <= max(TOL_ABS, TOL_REL * abs(v1))


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def secs(iso):
    return int(datetime.fromisoformat(iso).timestamp())


def key3(r):
    return (r["user_id"].lower(), r["outcome"], r["side"])


CONTRACTS_TOL = 0.00015   # v1 wrote a $2.00 fill at 0.29 as 6.8965 shares, v2 serves 6.8966 (2/0.29 = 6.89655…)


def basekey(r):
    """Raw-row identity without contracts and dollars (matched with tolerance)."""
    return (r["user_id"].lower(), r["outcome"], r["side"], r["timestamp"], round(float(r["odds"]), 6))


def match_rows(v1, v2):
    """Multiset match of committed rows to v2 rows: same wallet/outcome/side/
    timestamp/odds and contracts within CONTRACTS_TOL (an exact match is taken
    first). Returns (exact, near, unmatched committed rows, leftover v2 rows,
    dollar pairs of matched rows)."""
    pool = defaultdict(list)
    for r in v2:
        pool[basekey(r)].append((float(r["contracts"]), float(r["dollar_amount"])))
    exact = near = 0
    unmatched, pairs = [], []
    for r in v1:
        lst, c = pool.get(basekey(r)), float(r["contracts"])
        j = None
        if lst:
            j = next((i for i, x in enumerate(lst) if abs(x[0] - c) < 1e-9), None)
            if j is None:
                j = next((i for i, x in enumerate(lst) if abs(x[0] - c) <= CONTRACTS_TOL), None)
        if j is None:
            unmatched.append(r)
            continue
        exact += abs(lst[j][0] - c) < 1e-9
        near += abs(lst[j][0] - c) >= 1e-9
        pairs.append((float(r["dollar_amount"]), lst[j][1]))
        lst.pop(j)
    leftover = [k + (x[0],) for k, lst in pool.items() for x in lst]
    return exact, near, unmatched, leftover, pairs


def positions(rows, contracts="contracts", dollars="dollar_amount"):
    acc = defaultdict(lambda: [0, 0.0, 0.0])
    for r in rows:
        a = acc[key3(r)]
        a[0] += 1
        a[1] += float(r[contracts])
        a[2] += float(r[dollars])
    return acc


def run_market(name):
    """Migrated fetch + clean for one market (cached per process). Returns dict of rows."""
    if name in _CACHE:
        return _CACHE[name]
    m = MARKETS[name]
    os.makedirs(PARITY_DIR, exist_ok=True)
    raw_out = os.path.join(PARITY_DIR, f"{m['output']}.csv")
    clean_out = os.path.join(PARITY_DIR, f"{m['output']}_clean.csv")
    if not (REUSE and os.path.exists(raw_out) and os.path.exists(clean_out)):
        polymarket_trades.main(slug=m["slug"], output=raw_out)
        cleandata.main(input_csv=raw_out, output_csv=clean_out)
    res = {
        "v1_raw": read_csv(f"data/{m['output']}.csv"),
        "v1_clean": read_csv(f"data/{m['output']}_clean.csv"),
        "v2_raw": read_csv(raw_out),
        "v2_clean": read_csv(clean_out),
        "raw_out": raw_out, "clean_out": clean_out,
    }
    _CACHE[name] = res
    return res


# ── Raw-trade parity ─────────────────────────────────────────────────────────

def check_raw(name):
    m, d = MARKETS[name], run_market(name)
    v1, v2 = d["v1_raw"], d["v2_raw"]
    say(f"\n=== {name} ({m['output']}): raw trades, committed v1 crawl vs v2 feed ===")
    say(f"written to {d['raw_out']}")

    # schema
    v1_cols, v2_cols = list(v1[0].keys()), list(v2[0].keys())
    extra = [c for c in v2_cols if c not in v1_cols]
    assert all(c in v2_cols for c in v1_cols), f"v2 raw CSV lost columns: {set(v1_cols) - set(v2_cols)}"
    assert extra in ([], ["eoa_address"]), f"unexpected extra raw columns: {extra}"
    assert v2_cols == list(polymarket_trades.format_row(
        {"proxyWallet": "", "timestamp": 0}, {}).keys()), "raw CSV column order differs from format_row"

    # counts, wallets, timestamps
    u1, u2 = {r["user_id"].lower() for r in v1}, {r["user_id"].lower() for r in v2}
    t1 = (min(r["timestamp"] for r in v1), max(r["timestamp"] for r in v1))
    t2 = (min(r["timestamp"] for r in v2), max(r["timestamp"] for r in v2))
    table(["metric", "committed v1", "v2 feed"], [
        ["raw rows", len(v1), len(v2)],
        ["wallets", len(u1), len(u2)],
        ["first fill", t1[0], t2[0]],
        ["last fill", t1[1], t2[1]],
        ["sum contracts", f"{sum(float(r['contracts']) for r in v1):,.2f}", f"{sum(float(r['contracts']) for r in v2):,.2f}"],
        ["sum dollars", f"{sum(float(r['dollar_amount']) for r in v1):,.2f}", f"{sum(float(r['dollar_amount']) for r in v2):,.2f}"],
        ["identical-row multiplicity>1", sum(c - 1 for c in Counter(tuple(r.values()) for r in v1).values() if c > 1),
         sum(c - 1 for c in Counter(tuple(r.values()) for r in v2).values() if c > 1)],
    ])
    assert t1 == t2, f"first/last fill differ: v1 {t1} v2 {t2}"
    assert u1 <= u2, f"{len(u1 - u2)} committed wallets missing from v2: {sorted(u1 - u2)[:5]}"
    assert len(v2) >= len(v1)

    # per-side totals
    side1, side2 = defaultdict(lambda: [0, 0.0, 0.0]), defaultdict(lambda: [0, 0.0, 0.0])
    for rows, acc in ((v1, side1), (v2, side2)):
        for r in rows:
            a = acc[(r["outcome"], r["side"])]
            a[0] += 1; a[1] += float(r["contracts"]); a[2] += float(r["dollar_amount"])
    table(["outcome/side", "rows v1", "rows v2", "contracts v1", "contracts v2", "dollars v1", "dollars v2"],
          [[f"{o} {s}", side1[(o, s)][0], side2[(o, s)][0], f"{side1[(o, s)][1]:,.2f}", f"{side2[(o, s)][1]:,.2f}",
            f"{side1[(o, s)][2]:,.2f}", f"{side2[(o, s)][2]:,.2f}"] for (o, s) in sorted(side2)])

    # row-level containment: every committed raw row is in the v2 feed (multiset)
    exact, near, unmatched, leftover, pairs = match_rows(v1, v2)
    say(f"\nrow-level: {len(v1):,} committed rows -> {exact:,} found in v2 with identical wallet/outcome/side/timestamp/"
        f"contracts/odds, {near} more within {CONTRACTS_TOL} contracts (4th-decimal rounding of $-denominated orders), "
        f"{len(unmatched):,} committed rows not in v2")
    assert not unmatched, f"committed raw rows absent from v2: {[basekey(r) + (r['contracts'],) for r in unmatched[:3]]}"
    # dollar leg convention on matched rows: v1 usdcSize vs v2 shares x price
    diffs = [abs(a - b) for a, b in pairs]
    say(f"dollar leg on matched rows (v1 usdcSize vs v2 shares x price): max |diff| ${max(diffs):.4f}, "
        f"mean ${sum(diffs) / len(diffs):.6f}, rows > $0.01: {sum(x > 0.01 for x in diffs)}")
    assert max(diffs) <= 0.05, "v1 usdcSize and v2 shares x price disagree by more than 5 cents on a row"

    # position-level: (wallet, outcome, side) contracts & dollars
    p1, p2 = positions(v1), positions(v2)
    shared = [k for k in p1 if k in p2]
    only1 = [k for k in p1 if k not in p2]
    only2 = [k for k in p2 if k not in p1]
    ok = [k for k in shared if within(p1[k][1], p2[k][1]) and within(p1[k][2], p2[k][2])]
    grew = [k for k in shared if k not in ok and p2[k][2] >= p1[k][2] - TOL_ABS and p2[k][1] >= p1[k][1] - TOL_ABS]
    shrank = [k for k in shared if k not in ok and k not in grew]
    say(f"\npositions (wallet, outcome, side): committed {len(p1):,}, v2 {len(p2):,}, shared {len(shared):,}, "
        f"committed-only {len(only1)}, v2-only {len(only2)} ({sum(1 for k in only2 if k[0] in u1)} of wallets the crawl did see)")
    say(f"  shared within tolerance (<= ${TOL_ABS:.0f} or {TOL_REL:.1%}): {len(ok):,} / {len(shared):,} = {len(ok) / len(shared):.1%}")
    say(f"  shared where v2 > v1 beyond tolerance (crawl dropped fills): {len(grew)}")
    say(f"  shared where v2 < v1 beyond tolerance (would be v2 losing fills): {len(shrank)}")
    assert not only1, f"committed positions absent from v2: {only1[:3]}"
    assert not shrank, f"v2 smaller than committed: {[(k, p1[k], p2[k]) for k in shrank[:3]]}"
    assert len(ok) / len(shared) >= MIN_WITHIN_TOL
    if grew:
        table(["wallet", "outcome", "side", "rows v1", "rows v2", "$ v1", "$ v2", "$ diff"],
              [[k[0][:10] + "…", k[1], k[2], p1[k][0], p2[k][0], f"{p1[k][2]:,.2f}", f"{p2[k][2]:,.2f}", f"+{p2[k][2] - p1[k][2]:,.2f}"]
               for k in sorted(grew, key=lambda k: p1[k][2] - p2[k][2])[:6]])
    if only2:
        say(f"  largest v2-only positions ($ {sum(p2[k][2] for k in only2):,.2f} in total):")
        table(["wallet", "outcome", "side", "rows v2", "contracts", "$ v2", "wallet in v1?"],
              [[k[0][:10] + "…", k[1], k[2], p2[k][0], f"{p2[k][1]:,.2f}", f"{p2[k][2]:,.2f}", "yes" if k[0] in u1 else "no"]
               for k in sorted(only2, key=lambda k: -p2[k][2])[:6]])

    # the dropped rows are the oldest rows of their wallet (newest-first walk stopped early)
    oldest1 = {}
    for r in v1:
        u = r["user_id"].lower()
        oldest1[u] = min(oldest1.get(u, r["timestamp"]), r["timestamp"])
    newest1 = {}
    for r in v1:
        u = r["user_id"].lower()
        newest1[u] = max(newest1.get(u, r["timestamp"]), r["timestamp"])
    extra_rows = [k for k in leftover if k[0] in u1]
    older = sum(1 for k in extra_rows if k[3] < oldest1[k[0]])
    newer = sum(1 for k in extra_rows if k[3] > newest1[k[0]])
    interior = len(extra_rows) - older - newer
    say(f"\nv2 rows the crawl dropped for wallets it did see: {len(extra_rows):,} across "
        f"{len({k[0] for k in extra_rows})} wallets; older than the wallet's oldest committed row: {older:,}, "
        f"newer than its newest: {newer:,}, interior gaps: {interior:,}")
    say(f"v2 rows of wallets the crawl never saw: {sum(1 for k in leftover if k[0] not in u1):,} "
        f"across {len(u2 - u1)} wallets")
    if extra_rows:
        assert older / len(extra_rows) >= 0.9, "dropped rows are not predominantly the wallet's oldest rows"
    return {"rows": (len(v1), len(v2)), "wallets": (len(u1), len(u2)), "shared": len(shared), "ok": len(ok),
            "grew": len(grew), "only2": len(only2), "dropped": len(extra_rows), "older": older}


# ── Clean (aggregated positions) parity ──────────────────────────────────────

def check_clean(name):
    m, d = MARKETS[name], run_market(name)
    v1, v2 = d["v1_clean"], d["v2_clean"]
    say(f"\n=== {name}: _clean.csv (cleandata.main on the v2 raw) vs committed ===")
    say(f"written to {d['clean_out']}")
    v1_cols, v2_cols = list(v1[0].keys()), list(v2[0].keys())
    assert [c for c in v2_cols if c != "eoa_address"] == [c for c in v1_cols if c != "eoa_address"], \
        f"clean columns differ: {v1_cols} vs {v2_cols}"
    p1 = {key3(r): r for r in v1}
    p2 = {key3(r): r for r in v2}
    shared = [k for k in p1 if k in p2]
    only1 = [k for k in p1 if k not in p2]
    only2 = [k for k in p2 if k not in p1]
    ok = [k for k in shared if within(float(p1[k]["contracts"]), float(p2[k]["contracts"]))
          and within(float(p1[k]["dollar_amount"]), float(p2[k]["dollar_amount"]))]
    off = [k for k in shared if k not in ok]
    grew = [k for k in off if float(p2[k]["dollar_amount"]) >= float(p1[k]["dollar_amount"])]
    shrank = [k for k in off if k not in grew]
    win_mismatch = [k for k in shared if p1[k]["win_status"] != p2[k]["win_status"]]
    odds_off = [k for k in ok if abs(float(p1[k]["avg_odds"]) - float(p2[k]["avg_odds"])) > 0.01]
    first_same = sum(1 for k in ok if p1[k]["first_trade"] == p2[k]["first_trade"])
    table(["metric", "committed v1", "v2"], [
        ["clean positions", len(v1), len(v2)],
        ["wallets", len({r['user_id'].lower() for r in v1}), len({r['user_id'].lower() for r in v2})],
        ["total dollar_amount", f"{sum(float(r['dollar_amount']) for r in v1):,.2f}", f"{sum(float(r['dollar_amount']) for r in v2):,.2f}"],
        ["first first_trade", min(r["first_trade"] for r in v1), min(r["first_trade"] for r in v2)],
        ["last last_trade", max(r["last_trade"] for r in v1), max(r["last_trade"] for r in v2)],
    ])
    say(f"shared {len(shared):,}: within tolerance {len(ok):,} ({len(ok) / len(shared):.1%}), v2 larger {len(grew)}, "
        f"v2 smaller {len(shrank)}; committed-only {len(only1)}, v2-only {len(only2)}; "
        f"win_status mismatches {len(win_mismatch)}; avg_odds off by > 0.01 among agreeing positions {len(odds_off)}; "
        f"same first_trade among agreeing positions {first_same}/{len(ok)}")
    if shrank:
        # Only legitimate cause: cleandata nets complement SELL dollars out of the BUY row, and v2 holds
        # complement SELL fills the crawl dropped.
        say("  v2-smaller clean positions (BUY rows netted against complement SELLs the crawl dropped):")
        table(["wallet", "outcome", "side", "$ v1", "$ v2", "trades v1", "trades v2"],
              [[k[0][:10] + "…", k[1], k[2], p1[k]["dollar_amount"], p2[k]["dollar_amount"], p1[k]["num_trades"], p2[k]["num_trades"]]
               for k in shrank[:6]])
        for k in shrank:
            assert k[2] == "BUY" and int(p2[k]["num_trades"]) > int(p1[k]["num_trades"]), \
                f"v2 clean position smaller without extra netted trades: {k} {p1[k]} {p2[k]}"
    # A committed clean position can vanish in v2 only one way: it was a standalone SELL row and v2 now
    # holds the complement BUY fills the crawl dropped, so cleandata netted the SELL into that BUY row.
    outs = sorted({k[1] for k in p2})
    comp = {outs[0]: outs[1], outs[1]: outs[0]} if len(outs) == 2 else {}
    netted = [k for k in only1 if k[2] == "SELL" and (k[0], comp.get(k[1]), "BUY") in p2 and (k[0], comp.get(k[1]), "BUY") not in p1]
    if only1:
        say(f"  committed-only clean positions: {len(only1)}, of which standalone SELL rows now netted into a complement BUY "
            f"the crawl had dropped: {len(netted)}")
        table(["wallet", "outcome", "side", "$ v1 (standalone SELL)", "complement BUY in v2 $", "trades v2"],
              [[k[0][:10] + "…", k[1], k[2], p1[k]["dollar_amount"], p2[(k[0], comp[k[1]], "BUY")]["dollar_amount"],
                p2[(k[0], comp[k[1]], "BUY")]["num_trades"]] for k in netted[:6]])
    assert len(netted) == len(only1), f"committed clean positions missing from v2 for another reason: {[k for k in only1 if k not in netted][:3]}"
    assert not win_mismatch
    assert len(ok) / len(shared) >= MIN_WITHIN_TOL

    # whale sets, as scrape_insiders selects them
    def whales(rows):
        total = sum(float(r["dollar_amount"]) for r in rows)
        thr = max(1000.0, min(5000.0, 0.001 * total))
        return thr, {key3(r) for r in rows if r["outcome"].lower() == m["correct_outcome"].lower()
                     and float(r["dollar_amount"]) >= thr}
    thr1, w1 = whales(v1)
    thr2, w2 = whales(v2)
    say(f"whales (winning positions >= threshold): committed {len(w1)} @ ${thr1:,.0f}, v2 {len(w2)} @ ${thr2:,.0f}; "
        f"committed whales also whales in v2: {len(w1 & w2)}, new in v2: {len(w2 - w1)}, lost: {len(w1 - w2)}")
    for k in w1 - w2:
        say(f"  lost whale {k}: committed ${p1[k]['dollar_amount']} -> v2 ${p2[k]['dollar_amount']} (netted SELLs)")
    return {"shared": len(shared), "ok": len(ok), "grew": len(grew), "shrank": len(shrank), "whales": (len(w1), len(w2))}


# ── Prior-history parity (scrape_insiders.analyze_whale) ─────────────────────

def pick_whales(name, wallets=None, n=5):
    """Committed _insiders rows joined to the committed _clean rows they were scored from."""
    m = MARKETS[name]
    ins = read_csv(f"data/{m['output']}_insiders.csv")
    clean = read_csv(f"data/{m['output']}_clean.csv")
    by_key = {(r["user_id"].lower(), round(float(r["dollar_amount"]), 4), r["first_trade"]): r for r in clean}
    picked = []
    for r in ins:
        k = (r["user_id"].lower(), round(float(r["market_position"]), 4), r["target_trade_time"])
        if k in by_key:
            picked.append((r, by_key[k]))
    wanted = [p for p in picked if p[0]["user_id"].lower() in (wallets or [])]
    rest = [p for p in picked if p not in wanted]
    # spread across prior-count bands so the comparison covers new-looking and busy wallets alike
    rest.sort(key=lambda p: int(p[0]["prior_bet_count"]))
    step = max(1, len(rest) // max(1, n - len(wanted)))
    return (wanted + rest[::step])[:n]


def check_insiders(name="fired"):
    """The committed _insiders.csv rows re-scored through the v2 fetcher.

    The v1 crawl's failure signature was prior_bet_count 0 / concentration
    100 / suspicious for a wallet that in fact had history (the fetch failed
    and returned []). Those rows were re-scored in place in November 2026
    (ARTIFACT_WALLET among them), so the committed file must now agree with a
    fresh v2 scoring: no row may be a remaining artifact, no wallet may have
    LESS history in v2 than committed, and the former artifact wallet must
    score as the busy, unsuspicious account it is."""
    say(f"\n=== {name}: analyze_whale (v2 prior history) vs committed _insiders.csv ===")
    ins_all = read_csv(f"data/{MARKETS[name]['output']}_insiders.csv")
    zero = [r for r in ins_all if int(r["prior_bet_count"]) == 0]
    say(f"committed rows: {len(ins_all)}; rows with 0 prior trades (new accounts, or v1 artifacts if v2 finds history): "
        f"{len(zero)} -> {[r['user_id'][:10] + '…' for r in zero]}")
    picks = pick_whales(name, wallets=[ARTIFACT_WALLET] + [r["user_id"].lower() for r in zero], n=5)
    assert any(p[0]["user_id"].lower() == ARTIFACT_WALLET for p in picks)
    rows, verdicts = [], {}
    for committed, clean_row in picks:
        got = scrape_insiders.analyze_whale(clean_row)
        c_prior, g_prior = int(committed["prior_bet_count"]), int(got["prior_bet_count"])
        c_conc, g_conc = float(committed["portfolio_concentration"]), float(got["portfolio_concentration"])
        if c_prior == 0 and g_prior > 0:
            verdict = "v1-ARTIFACT (crawl returned no history)"
        elif c_prior == 0 and g_prior == 0:
            verdict = "genuinely new account"
        elif g_prior == c_prior and abs(g_conc - c_conc) <= 0.05:
            verdict = "identical"
        elif g_prior >= c_prior:
            verdict = "v2 finds more history (crawl partial)"
        else:
            verdict = "v2 finds LESS history"
        verdicts[committed["user_id"].lower()] = (verdict, g_prior, g_conc, got)
        rows.append([committed["user_id"][:12] + "…", committed["target_trade_time"], committed["market_position"],
                     c_prior, g_prior, f"{c_conc:.2f}", f"{g_conc:.2f}",
                     f"{float(committed['lifetime_portfolio']):,.0f}", f"{float(got['lifetime_portfolio']):,.0f}",
                     committed["is_suspicious"], str(got["is_suspicious"]), verdict])
    table(["wallet", "first trade (cutoff)", "position $", "prior committed", "prior v2", "conc% committed", "conc% v2",
           "lifetime committed", "lifetime v2", "susp committed", "susp v2", "verdict"], rows)
    art = verdicts[ARTIFACT_WALLET]
    committed_art = next(r for r in ins_all if r["user_id"].lower() == ARTIFACT_WALLET)
    assert art[1] > scrape_insiders.FIRST_X_TRADES, f"{ARTIFACT_WALLET} still looks new: {art[1]} prior"
    assert art[2] < scrape_insiders.PERCENT_VOLUME and art[3]["is_suspicious"] is False
    assert int(committed_art["prior_bet_count"]) > scrape_insiders.FIRST_X_TRADES and committed_art["is_suspicious"] == "False", \
        f"the committed row of {ARTIFACT_WALLET} is still the v1 artifact: {committed_art}"
    assert art[0] in ("identical", "v2 finds more history (crawl partial)"), f"{ARTIFACT_WALLET}: {art[0]}"
    for u, (verdict, *_rest) in verdicts.items():
        assert not verdict.startswith("v2 finds LESS"), f"{u}: {verdict}"
        assert not verdict.startswith("v1-ARTIFACT"), f"{u}: committed row is still a v1 artifact"
    return verdicts


# ── Endpoint semantics, verified live ────────────────────────────────────────

def check_end_bound_inclusive():
    """/v2/activity end=<ts> returns the rows AT ts (inclusive); end=ts-1 does not."""
    user, cutoff = ARTIFACT_WALLET, 1772728909   # a fill of this wallet at exactly this second
    at = polymarket_api.fetch_user_history_up_to(user, cutoff, max_pages=1)
    before = polymarket_api.fetch_user_history_up_to(user, cutoff - 1, max_pages=1)
    assert at and any(r["timestamp"] == cutoff for r in at), "end bound excluded the row at the cutoff"
    assert all(r["timestamp"] <= cutoff for r in at)
    assert before and all(r["timestamp"] <= cutoff - 1 for r in before)
    assert all(set(r) >= {"usdcSize", "transactionHash", "timestamp", "conditionId", "type"} for r in at)
    say(f"\n/v2/activity end bound: inclusive (row at {cutoff} returned with end={cutoff}, absent with end={cutoff - 1}); "
        f"rows carry usdcSize/transactionHash in v1 key names")


def check_error_payloads_raise():
    """Retired v1 (410 + error payload) raises; a malformed id is refused client-side;
    a well-formed unknown condition is the empty zero-state, not an error."""
    try:
        polymarket_api.get_json("https://data-api.polymarket.com/activity", {"user": ARTIFACT_WALLET, "limit": 1})
    except polymarket_api.RetiredEndpointError as e:
        say(f"/activity (v1): raised RetiredEndpointError (HTTP {e.status}): {e.payload.get('error')}")
    else:
        raise AssertionError("retired v1 endpoint did not raise")
    for bad in ("0xdeadbeef", "", "not-a-condition"):
        try:
            polymarket_api.fetch_trades(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"malformed condition id accepted: {bad!r}")
    try:
        polymarket_api.fetch_user_history_up_to("notanaddress", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed wallet address accepted (the feed would answer 200 + empty)")
    rows, capped = polymarket_api.fetch_trades("0x" + "0" * 64)
    assert rows == [] and capped is False
    say("malformed ids are refused before any request; a well-formed unknown condition id yields [] (zero-state)")


def check_cap_is_announced(capsys=None):
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rows, capped = polymarket_api.fetch_trades(
            "0x8ca781cf3463eba14e964498e90eebaf052ee43d38c9203e9a34f297df042681", max_rows=1000)
    assert capped is True and len(rows) <= 1000 and "capped" in buf.getvalue()
    say("row cap: fetch_trades(max_rows=1000) returned capped=True and announced it on stdout")


# ── pytest entry points ──────────────────────────────────────────────────────

def test_raw_parity_fired():
    check_raw("fired")


def test_raw_parity_openai():
    check_raw("openai")


def test_clean_parity_fired():
    check_clean("fired")


def test_clean_parity_openai():
    check_clean("openai")


def test_insiders_fired():
    check_insiders("fired")


def test_activity_end_bound_inclusive():
    check_end_bound_inclusive()


def test_error_payloads_raise():
    check_error_payloads_raise()


def test_cap_is_announced():
    check_cap_is_announced()


if __name__ == "__main__":
    checks = [test_error_payloads_raise, test_activity_end_bound_inclusive, test_cap_is_announced,
              test_raw_parity_fired, test_clean_parity_fired, test_raw_parity_openai, test_clean_parity_openai,
              test_insiders_fired]
    failed = []
    for fn in checks:
        try:
            fn()
            print(f"\nPASS {fn.__name__}", flush=True)
        except Exception as e:  # keep going so the report is complete
            failed.append(fn.__name__)
            import traceback
            traceback.print_exc()
            print(f"\nFAIL {fn.__name__}: {e}", flush=True)
    with open(os.path.join(PARITY_DIR, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT) + "\n")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed; report -> {os.path.join(PARITY_DIR, 'report.txt')}")
    sys.exit(1 if failed else 0)
