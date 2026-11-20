"""
python3 -m backend.rubric check [--rubric FILE]
    Validate the rubric file: every problem on its own line, exit 1 when there is one.

python3 -m backend.rubric score (--market NAME ... | --all) [--dry-run] [--scorer decisions|interactions]
                                [--max-requests N] [--refresh] [--rubric FILE] [--facts DIR] [--cache DIR] [--out DIR]
    Score whales from their facts files (DIR/<name>.json, written by backend.rubric.fact_sources). Answers cached
    for the same question and input are reused; --dry-run sends nothing and prints how many requests a real run
    would make. Writes OUT/<name>.json (pipeline.score_market's result) and prints one line per market.

python3 -m backend.rubric show [--market NAME] [--wallet ADDR] [--top N] [--out DIR]
    No market: the rubric in one table (questions, weights, bands). A market: its whales by score, beside
    today's flag. A wallet too: the text the model read and every answer.

python3 -m backend.rubric compare [--market NAME ... | --all] [--out DIR] [--top N]
    The rubric against today's yes/no flag: the band counts by today's category, the highest-scored whales the
    flag missed, and the flagged whales it scores lowest.

python3 -m backend.rubric pages --to DIR [--api URL] [--out DIR]
    Local copies of the site's data with the scores attached (GET only from the site's API): DIR/api/data/
    index.json and markets/<name>.json, for a local static server the site's VITE_API_URL points at.

python3 -m backend.rubric publish (--market NAME ... | --all) [--out DIR] [--cache DIR] [--facts DIR]
    Put the scores on the live site: each market's rubric:<name> blob and its page built again from its record
    (WATCHLIST_API_URL with WATCHLIST_JOB_TOKEN, as backend.site uses them).
"""
import argparse
import glob
import json
import os
import sys

from backend.rubric import DEFAULT_PATH, cache as cache_mod, pipeline, spec
from backend.rubric.scorers import get_scorer

DATA = os.path.join(spec.ROOT, "data", "rubric")
if spec.ROOT not in sys.path:
    sys.path.insert(0, spec.ROOT)                 # export.py, add_market.py and the rest live at the root
API = "https://rigged-outcomes-api.parthkj6.workers.dev"


def _names(a, folder):
    return a.market or sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(folder, "*.json")))


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, doc, indent=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=indent, separators=None if indent else (",", ":"))
    os.replace(path + ".tmp", path)


def _fmt_fact(rubric, fid, v):
    from backend.rubric import prompt
    f = next((x for x in rubric["factors"] if x["id"] == fid), {"id": fid})
    return prompt.short(v, spec.factor_format(f) or "count")


def _labels(rubric, ids):
    return [next((f["label"] for f in rubric["factors"] if f["id"] == i), i) for i in ids]


def cmd_check(rubric, a):
    problems = spec.check(rubric)
    for p in problems:
        print(p)
    if not problems:
        print(f"ok: {rubric['version']}, {len(rubric['factors'])} factors, {len(rubric['questions'])} questions, "
              f"{len(rubric['bands'])} bands")
    return 1 if problems else 0


def cmd_score(rubric, a):
    names = _names(a, a.facts)
    if not names:
        print(f"no facts files in {a.facts}", file=sys.stderr)
        return 1
    ledger = cache_mod.Ledger(os.path.join(a.cache, "usage.json"))
    make = lambda name: get_scorer(rubric, name, ledger=ledger)            # noqa: E731
    total = 0
    for name in names:
        facts = _load(os.path.join(a.facts, f"{name}.json"))
        store = cache_mod.AnswerCache(os.path.join(a.cache, f"{name}.json"), market=name)
        left = None if a.max_requests is None else max(0, a.max_requests - total)
        if left == 0 and not a.dry_run:
            print(f"{name}: skipped (--max-requests reached)")
            continue
        res = pipeline.score_market(rubric, facts, store, scorer=make(a.scorer or rubric["scorer"]["use"]),
                                    make_scorer=make, dry_run=a.dry_run, max_requests=left, refresh=a.refresh,
                                    log=lambda m: print(f"  {m}", file=sys.stderr))
        st = res["stats"]
        if a.dry_run:
            print(f"{name}: {st['whales']} whales, {st['cached']} cached, {st['would_ask']} requests to send")
            total += st["would_ask"]
            continue
        total += st["requests"]
        _write(os.path.join(a.out, f"{name}.json"), res)
        bands = {}
        for w in res["whales"]:
            bands[w["band"]] = bands.get(w["band"], 0) + 1
        print(f"{name}: {st['whales']} whales, {st['requests']} requests ({res['scorer']}, {st['tokens']:,} tokens), "
              f"{st['unscored']} unscored, bands {bands}" + (f"; stopped: {st['stopped']}" if st["stopped"] else ""))
        if st["stop"] in ("busy", "unavailable"):
            return 1
    print(f"{'would send' if a.dry_run else 'sent'} {total} requests in all")
    return 0


def _rubric_table(rubric):
    w = (rubric.get("combine") or {}).get("weights") or {}
    print(f"{rubric.get('name')} {rubric['version']}  (scorer {rubric['scorer']['use']}, fallback "
          f"{rubric['scorer'].get('fallback') or 'none'})\n")
    print(f"{"question":<22} {"type":<7} {'weight':>6}  levels / options")
    for q in rubric["questions"]:
        opts = [lv["label"] for lv in q.get("levels") or []] or [o["label"] for o in q.get("options") or []] or \
            ["no", "yes"]
        print(f"{q.get("label", q["id"]):<22} {q["type"]:<7} {w.get(q['id'], 0):>6}  {' · '.join(opts)}")
    print("\nbands: " + "  ".join(f"{b['label']} {b['min']}+{' (flagged)' if b.get('flagged') else ''}"
                                  for b in rubric["bands"]))
    print("factors read: " + ", ".join(f["label"] for f in rubric["factors"] if f.get("send", True)))


def cmd_show(rubric, a):
    if not a.market:
        _rubric_table(rubric)
        return 0
    res = _load(os.path.join(a.out, f"{a.market[0]}.json"))
    whales = res["whales"]
    if a.wallet:
        from backend.rubric import prompt
        facts = _load(os.path.join(a.facts, f"{a.market[0]}.json"))
        for w, fw in zip(whales, facts["whales"]):
            if a.wallet.lower() not in (w["user_id"].lower(), w["wallet"].lower()):
                continue
            print(f"{w['item']}  score {w['score']}  band {w['band']}  today: {w['flags'].get('category')}\n")
            print(prompt.render_input(rubric, facts["market"], fw["facts"], side=fw.get("position_side", "BUY")))
            print()
            for q in rubric["questions"]:
                ans = w["answers"].get(q["id"])
                if not ans:
                    print(f"{q['id']}: not answered")
                elif q["type"] == "score":
                    lv = max(range(len(ans["p"])), key=ans["p"].__getitem__)
                    print(f"{q['id']}: {ans['score']:.2f} of {len(ans['p']) - 1}, likeliest "
                          f"{q['levels'][lv]['label']!r} ({ans['p'][lv]:.0%})")
                elif q["type"] == "noul":
                    print(f"{q['id']}: yes {ans['p']:.0%}")
                else:
                    print(f"{q['id']}: {ans['choice']} ({ans['p'][ans['choice']]:.0%})")
        return 0
    card = [f["id"] for f in rubric["factors"] if f.get("card")][:5]
    rows = sorted(whales, key=lambda w: -(w["score"] if w["score"] is not None else -1))[:a.top]
    print(f"{'score':>5} {'band':<18} {'today':<13} {'wallet':<14} " + " ".join(f"{c[:12]:>12}"
                                                                             for c in _labels(rubric, card)))
    for w in rows:
        print(f"{w['score'] if w['score'] is not None else '-':>5} {str(w['band']):<18} "
              f"{str(w['flags'].get('category')):<13} {w['user_id'][:12]:<14} "
              + " ".join(f"{_fmt_fact(rubric, c, w['facts'].get(c)):>12}" for c in card))
    return 0


def cmd_compare(rubric, a):
    flagged = {b["id"] for b in rubric["bands"] if b.get("flagged")}
    order = [b["id"] for b in rubric["bands"]]
    names = _names(a, a.out)
    every = []
    for name in names:
        res = _load(os.path.join(a.out, f"{name}.json"))
        every += [dict(w, market=name) for w in res["whales"] if w["score"] is not None]
    if not every:
        print("no scored whales")
        return 1
    cats = ["critical", "newAccount", "concentration", "normal"]
    print(f"{len(every)} scored whales in {len(names)} markets\n")
    print(f"{'band':<18}" + "".join(f"{c:>14}" for c in cats) + f"{'all':>8}")
    for b in reversed(order):
        row = [sum(1 for w in every if w["band"] == b and w["flags"].get("category") == c) for c in cats]
        print(f"{b:<18}" + "".join(f"{n:>14}" for n in row) + f"{sum(row):>8}")
    old = [w for w in every if w["flags"].get("is_suspicious")]
    new = [w for w in every if w["band"] in flagged]
    both = [w for w in new if w["flags"].get("is_suspicious")]
    print(f"\nflagged today {len(old)}, by the rubric {len(new)}, by both {len(both)}")
    missed = sorted((w for w in every if not w["flags"].get("is_suspicious")), key=lambda w: -w["score"])[:a.top]
    weak = sorted(old, key=lambda w: w["score"])[:a.top]
    card = [f["id"] for f in rubric["factors"] if f.get("card")][:5]
    for title, rows in (("Highest scores today's flag missed", missed), ("Flagged today, lowest scores", weak)):
        print(f"\n{title}")
        for w in rows:
            print(f"  {w['score']:>5} {w['band']:<18} {w['market']:<24} {w['user_id'][:12]}  "
                  + "  ".join(f"{lab} {_fmt_fact(rubric, c, w['facts'].get(c))}"
                              for c, lab in zip(card, _labels(rubric, card))))
    return 0


def cmd_pages(rubric, a):
    import requests
    from backend.rubric import store
    import export
    index = requests.get(f"{a.api}/api/data/index.json", timeout=60).json()
    out_index = []
    for e in index.get("markets") or []:
        name = e["name"]
        page = requests.get(f"{a.api}/api/data/markets/{name}.json", timeout=120).json()
        e = {k: v for k, v in e.items() if k != "v"}
        path = os.path.join(a.out, f"{name}.json")
        if os.path.exists(path):
            res = _load(path)
            page, n = store.merge_page(page, res, rubric)
            summary = export.spectrum_summary(page.get("traders"))
            if summary is not None:
                ids = {str(t["id"]).lower() for t in page["traders"]["traders"]
                       if t.get("spectrum") and t["spectrum"]["band"] in
                       {b["id"] for b in rubric["bands"] if b.get("flagged")}}
                tl = page.get("timeline") or {}
                won = sum(w.get("profit") or 0 for w in tl.get("whales") or [] if str(w["id"]).lower() in ids)
                total = tl.get("totalMarketWinnings") or 0
                summary["flaggedWinnings"] = round(won)
                summary["flaggedWinningsPct"] = round(won / total * 100, 1) if total else None
                e["spectrum"] = summary
            print(f"{name}: {n} of {len(page['traders']['traders'])} traders scored")
        else:
            print(f"{name}: no scores")
        _write(os.path.join(a.to, "api", "data", "markets", f"{name}.json"), page)
        out_index.append(e)
    _write(os.path.join(a.to, "api", "data", "index.json"), dict(index, markets=out_index))
    print(f"wrote {len(out_index)} markets to {os.path.join(a.to, 'api', 'data')}")
    return 0


def cmd_publish(rubric, a):
    from backend.rubric import store
    from watchlist import sync
    c = sync.client()
    if c is None:
        print("No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN)", file=sys.stderr)
        return 2
    for name in _names(a, a.out):
        res = _load(os.path.join(a.out, f"{name}.json"))
        facts = _load(os.path.join(a.facts, f"{name}.json"))
        answers = cache_mod.AnswerCache(os.path.join(a.cache, f"{name}.json"), market=name)
        answers.keep_only(pipeline.used_keys(rubric, facts))
        part = pipeline.page_part(res, rubric)
        got = store.record_of(c, name)
        if got is None:
            print(f"{name}: not on the site", file=sys.stderr)
            continue
        import time
        store.write(c, name, {"market": name, "condition_id": got[3], "version": rubric["version"],
                              "scored_at": int(time.time()), "scorer": res["scorer"], "stats": res["stats"],
                              "answers": answers.doc["answers"], "facts": facts, "page": part})
        pub = store.publish_page(c, name, part, by="local")
        print(f"{name}: {len(part['whales'])} whales scored, page {pub['v'][:8]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m backend.rubric", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, markets=True, required=False):
        p.add_argument("--rubric", default=os.path.join(spec.ROOT, DEFAULT_PATH))
        p.add_argument("--facts", default=os.path.join(DATA, "facts"))
        p.add_argument("--cache", default=os.path.join(DATA, "cache"))
        p.add_argument("--out", default=os.path.join(DATA, "scores"))
        if markets:
            g = p.add_mutually_exclusive_group(required=required)
            g.add_argument("--market", action="append")
            g.add_argument("--all", action="store_true")

    common(sub.add_parser("check"), markets=False)
    s = sub.add_parser("score")
    common(s, required=True)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--scorer", choices=spec.SCORERS)
    s.add_argument("--max-requests", type=int)
    s.add_argument("--refresh", action="store_true", help="ask again even what is cached")
    s = sub.add_parser("show")
    common(s)
    s.add_argument("--wallet")
    s.add_argument("--top", type=int, default=25)
    s = sub.add_parser("compare")
    common(s)
    s.add_argument("--top", type=int, default=10)
    s = sub.add_parser("pages")
    common(s, markets=False)
    s.add_argument("--to", required=True)
    s.add_argument("--api", default=API)
    s = sub.add_parser("publish")
    common(s, required=True)
    a = ap.parse_args(argv)

    rubric = spec.load_rubric(a.rubric)
    if a.cmd == "check":
        return cmd_check(rubric, a)
    problems = spec.check(rubric)
    if problems:
        print("the rubric has problems; run: python3 -m backend.rubric check", file=sys.stderr)
        return 1
    return {"score": cmd_score, "show": cmd_show, "compare": cmd_compare, "pages": cmd_pages,
            "publish": cmd_publish}[a.cmd](rubric, a)


if __name__ == "__main__":
    sys.exit(main())
