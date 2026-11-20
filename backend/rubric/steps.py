"""
The wallets.score step (docs/BACKEND.md, docs/WALLET_RUBRIC.md): one market's whales scored by the rubric and
its page published again with the scores, in phases a Vercel call can stop between.

  input   {"name": page name}; none: the add run's market (its add:fetch result). add.export spawns it right
          after it puts a market on the main page; any step may spawn one for a market already there (a
          backfill after a rubric change).
  fills   Gamma's market, then every fill page by page into add_market.Fills (past the step's time the fills and
          the feed's place go to a scratch blob, the step out as partial), saved as scratch "rubric-fills".
  history each whale's history as of its first trade (wallet_facts.wallet_history, Polymarket's free Data API),
          WHALE_THREADS at a time, each checkpointed as it finishes: result rubric-hist:<wallet>:<cutoff>.
  facts   once every history is in: the facts document (wallet_facts.market_facts), saved as scratch
          "rubric-facts". A market whose rubric:<name> blob already holds facts with every registered factor
          starts here with those (input "refresh_facts": true walks again).
  score   the rubric's questions for every whale whose answers are not cached (the rubric:<name> blob's answers),
          each request's answers checkpointed as result rubric-ans:<n>. The scorer busy after its retries: later
          (model_busy, 5-10 minutes). Both scorers unavailable (key refused, today's quota spent): the step goes
          on with what is cached; the unscored whales are asked at the next run.
  publish the page and index entry built again from the market's record with the scores (store.publish_page),
          then rubric:<name> saved (answers, facts, page part). Nothing scored: no page.

Nothing here writes outside ctx.tmp, and nothing is kept at module level.
"""
import gzip
import hashlib
import json
import random

from backend import outcomes

SAVE_S = 30                 # seconds a fills walk keeps to save before the deadline
WHALE_NEED_S = 40           # a whale's history is started only with this many seconds left
WHALE_THREADS = 4
POLYMARKET_RATE = 5.0       # Polymarket requests a second (the facts CLI uses the same)
FACTS_NEED_S = 60           # building the facts (pandas over the fills) needs this much
PUBLISH_NEED_S = 60         # the blob, the page and the index


def _zip(doc):
    return gzip.compress(json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8"), 6, mtime=0)


def _unzip(data):
    return json.loads(gzip.decompress(data).decode("utf-8"))


def market_name(ctx):
    inp = ctx.job.get("input") or {}
    if inp.get("name"):
        return str(inp["name"])
    fetched = dict(ctx.results("add:fetch")).get("add:fetch") or {}
    return (fetched.get("entry") or {}).get("name") or (ctx.job.get("run_meta") or {}).get("name")


def hist_key(wallet, cutoff):
    return f"rubric-hist:{str(wallet).lower()}:{int(cutoff)}"


def _ans_key(answers):
    return "rubric-ans:" + hashlib.sha1(json.dumps(sorted(answers), separators=(",", ":")).encode()).hexdigest()[:24]


def score(ctx):
    from pipeline import add_market as A
    from pipeline import polymarket_api
    from backend.rubric import cache as cache_mod, fact_sources, pipeline, spec, store, wallet_facts as wf
    from backend.rubric.scorers import get_scorer

    rubric = spec.load_rubric()
    problems = spec.check(rubric, tokens=set())
    if problems:
        raise ValueError(f"the rubric has problems ({len(problems)}): {problems[0]}")
    name = market_name(ctx)
    got = store.record_of(ctx.api, name) if name else None
    if got is None:
        return ctx.done()                                   # taken off the main page, or never put there
    entry, clean_csv, insiders_csv, cid, _added = got
    inp = ctx.job.get("input") or {}
    cur = dict(ctx.cursor or {})
    _h, saved = store.read(ctx.api, name)
    saved = saved or {}

    # ── facts already saved, with every factor the code has ──
    facts_doc = None
    if cur.get("facts"):
        data = ctx.get_scratch(cur["facts"])
        facts_doc = _unzip(data) if data is not None else None
    if facts_doc is None and not inp.get("refresh_facts") and isinstance(saved.get("facts"), dict) and \
            saved.get("condition_id") == cid and set(wf.FACTORS) <= set(saved["facts"].get("factors") or {}):
        facts_doc = saved["facts"]

    if facts_doc is None:
        # ── fills ──
        fills, walk, before = None, {}, 0
        if cur.get("fills"):
            data = ctx.get_scratch(cur["fills"])
            if data is not None:
                doc = _unzip(data)
                fills, walk = A.Fills.load(doc["fills"]), doc["walk"]
                before = int(walk.get("pages") or 0)
        if fills is None:
            fills, walk = A.Fills(polymarket_api.fetch_market(A.bare_slug(entry["slug"]))), {}

        def save_fills(cursor_extra=None):
            ref = ctx.put_scratch("rubric-fills", _zip({"walk": walk, "fills": fills.dump()}))
            ctx.units(int(walk.get("pages") or 0) - before)
            return dict(phase="history" if walk.get("done") else "fills", fills=ref, **(cursor_extra or {}))

        if not walk.get("done"):
            try:
                for page in polymarket_api.v2_pages("trades", A.trade_params(cid), walk):
                    fills.add_page(page)
                    if fills.n > A.MAX_FILLS:
                        raise ValueError(f"{name}: over {A.MAX_FILLS:,} fills")
                    if not walk["done"] and ctx.left() < SAVE_S:
                        return ctx.partial(save_fills())
            except outcomes.OutOfTime:
                if int(walk.get("pages") or 0) <= before:
                    raise
                return ctx.partial(save_fills())
            cur = save_fills()
            ctx.checkpoint(cursor=cur)

        # ── history ──
        market = fact_sources.from_record(entry, clean_csv, insiders_csv, fills)
        hists = dict(ctx.results("rubric-hist:"))
        todo = [wh for wh in market.whales if hist_key(wh["wallet"], wh["first_buy_ts"]) not in hists]
        if todo:
            _histories(ctx, market, todo, hists, cur)
        if ctx.left() < FACTS_NEED_S:
            return ctx.partial(cur)
        histories = [hists[hist_key(wh["wallet"], wh["first_buy_ts"])] for wh in market.whales]

        # ── facts ──
        facts_doc = wf.market_facts(market, histories=histories)
        cur = {"phase": "score", "facts": ctx.put_scratch("rubric-facts", _zip(facts_doc))}
        ctx.checkpoint(cursor=cur)

    # ── score ──
    cache = cache_mod.AnswerCache(doc={"market": name, "answers": saved.get("answers") or {}})
    for _k, answers in ctx.results("rubric-ans:"):
        cache.update(answers)
    def keep(answers):
        if answers:
            ctx.checkpoint(results=[(_ans_key(answers), answers)])
        ctx.units(1)

    budget_left = max(0, int(ctx.left() - PUBLISH_NEED_S))
    res = pipeline.score_market(rubric, facts_doc, cache, scorer=get_scorer(rubric), on_answers=keep,
                                max_requests=max(1, budget_left))
    st = res["stats"]
    if st["stop"] == "busy":
        raise outcomes.Later.after(random.randint(300, 600), "model_busy")
    if st["stop"] == "max_requests" or ctx.left() < PUBLISH_NEED_S:
        return ctx.partial(dict(cur, phase="score"))

    # ── publish: the page first, then the blob (a step stopped between the two publishes again next time) ──
    part = pipeline.page_part(res, rubric)
    if part["whales"] and part != saved.get("page"):
        store.publish_page(ctx.api, name, part, now=ctx.now())
    cache.keep_only(pipeline.used_keys(rubric, facts_doc))
    doc = {"market": name, "condition_id": cid, "version": rubric["version"], "scored_at": ctx.now(),
           "scorer": res["scorer"], "stats": {k: st[k] for k in ("whales", "requests", "unscored", "refused")},
           "answers": cache.doc["answers"], "facts": facts_doc, "page": part}
    store.write(ctx.api, name, doc)
    ctx.meta(rubric={"name": name, "version": rubric["version"], "scored": len(part["whales"]),
                     "unscored": st["unscored"], "requests": st["requests"], "scorer": res["scorer"],
                     **({"stopped": st["stopped"][:200]} if st["stopped"] else {})})
    return ctx.done()


def _histories(ctx, market, todo, hists, cur):
    """Each missing whale's history, WHALE_THREADS at a time, checkpointed as it finishes (into hists too).
    Raises OutOfTime when the step's time runs out (the runtime sends partial from the last cursor)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from pipeline import polymarket_api
    from backend.rubric import wallet_facts as wf

    source = wf.Polymarket(None, rate=POLYMARKET_RATE, getter=polymarket_api.get_json)
    lock = threading.Lock()

    def one(wh):
        outcomes.check(WHALE_NEED_S)
        h = wf.wallet_history(source, wh["wallet"], wh["first_buy_ts"], market.condition_id)
        k = hist_key(wh["wallet"], wh["first_buy_ts"])
        with lock:
            hists[k] = h
            ctx.checkpoint(cursor=cur, results=[(k, h)])

    errors = []
    with ThreadPoolExecutor(max_workers=max(1, min(WHALE_THREADS, len(todo)))) as pool:
        for f in [outcomes.submit(pool, one, wh) for wh in todo]:
            try:
                f.result()
            except Exception as e:
                errors.append(e)
    ctx.units(source.requests)
    for kind in (outcomes.Lost, outcomes.Later, outcomes.OutOfTime):
        for e in errors:
            if isinstance(e, kind):
                raise e
    if errors:
        raise errors[0]
