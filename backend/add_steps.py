"""
Adding a market to the main page, as steps of an `add` run (docs/BACKEND.md): the same
pipeline as add_market.add, cut where a Vercel call can stop and carry on.

  add.fetch    Gamma's check and the page name; every fill, page by page into add_market.Fills (past the step's
               time, or the deadline reached inside a page's fetch, the fills and the feed's place go to a scratch
               blob and the step goes out as partial; so do they when the walk ends with under CLEAN_S left); the
               positions cleaned, saved as a scratch blob; the whales listed, spawned as add.whales batches of 15.
               Result add:fetch {entry, condition_id, clean, fills, positions, whales}.
  add.whales   A batch's prior histories, add_threads at a time, each added up page by page and checkpointed as it
               finishes: result whale:<wallet>:<cutoff> {stake, txs, trades, capped}. A whale is started only with
               WHALE_NEED_S left; the day's whale allowance (budget.add_whales_left) used up waits for the next
               04:00 in Toronto. An add of more than add_whales_reserve whales leaves that many of the day's
               allowance to the small ones, so one large add never holds up the rest until the next day.
  add.export   Scores the whales, builds the page with export.py, puts site:markets/<name>.json, market:<name>
               and site:index.json (backend.site.put_market), the catalog, and reports the request added. Then it
               spawns wallets.score for the market (backend/rubric/steps.py: the wallet rubric's insider score)
               where the step function's settings turn it on (WALLET_SCORES=on, with GEMINI_API_KEY) and the
               rubric's [backend] score_on_add is true.

A market that cannot be added (not closed, a different result, too many trades or whales, nothing traded) is
reported failed by the step that finds it, which stops the run. A step that stopped part way (a batch failed
after its retries, the run's deadline) leaves add.export to report the request failed; a visitor may add it
again. Nothing here writes outside ctx.tmp, and nothing is kept at module level.
"""
import gzip
import json

from backend import outcomes
from backend.contract import LISTS, toronto_at, toronto_day

SAVE_S = 30                       # seconds a fetch keeps to save its fills before the deadline
CLEAN_S = 60                      # seconds the positions' clean and its save need (US x Iran's: about 25 s)
WHALE_NEED_S = 40                 # a whale's history is started only with this many seconds left (the slowest: 32 s)
STOPPED = "stopped part way while adding it"
FULL = "the main page holds as many added markets as it can"


def _zip(doc):
    return gzip.compress(json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8"), 6, mtime=0)


def _unzip(data):
    return json.loads(gzip.decompress(data).decode("utf-8"))


def next_daily(now):
    """The next 04:00 in Toronto after now (when the day's budget is fresh)."""
    at = toronto_at(toronto_day(now), "04:00")
    return at if at > now else toronto_at(toronto_day(at + 26 * 3600), "04:00")


def condition_of(ctx):
    """The run's condition id: its input's, else its slot's ('<condition id>:<decided_at>')."""
    cid = (ctx.job.get("input") or {}).get("condition_id") or (ctx.job.get("run_meta") or {}).get("condition_id")
    return str(cid or str(ctx.job.get("run_slot") or "").rsplit(":", 1)[0]).lower()


def request_of(ctx):
    """The add request: the job's input {condition_id, slug, winner, name}, or the approved request of the run's
    condition id from GET /api/job/work. None when there is no such request any more."""
    inp = dict(ctx.job.get("input") or {})
    if inp.get("slug") and inp.get("winner"):
        inp["condition_id"] = condition_of(ctx)
        return inp
    cid = condition_of(ctx)
    for r in (ctx.api.request("GET", "/api/job/work") or {}).get("requests") or []:
        if str(r.get("condition_id") or "").lower() == cid and r.get("status") == "approved":
            return {"condition_id": cid, "slug": r.get("slug"), "winner": r.get("winner"), "name": r.get("name")}
    return None


def report(ctx, cid, status, name, note):
    """The request's end, POST /api/job/done: status "added" or "failed"."""
    ctx.api.request("POST", "/api/job/done", {"applied": [], "watched": [], "requests": [
        {"condition_id": cid, "status": status, "name": name, "note": note}]})


def _stop(ctx, cid, status, name, note):
    report(ctx, cid, status, name, note)
    return ctx.done(stop_run=True)


# ── add.fetch ────────────────────────────────────────────────────────────────

def fetch(ctx):
    import add_market as A
    import polymarket_api
    from backend import site

    cur = ctx.cursor or {}
    saved = None
    if cur.get("fills"):
        data = ctx.get_scratch(cur["fills"])
        saved = _unzip(data) if data is not None else None
    if saved is not None:
        entry, cid, walk = saved["entry"], saved["condition_id"], saved["walk"]
        fills = A.Fills.load(saved["fills"])
        before = int(walk.get("pages") or 0)
    else:
        req = request_of(ctx)
        cid = condition_of(ctx)
        if req is None:
            return ctx.done(stop_run=True)                # taken back, or reported already
        name = req.get("name") or None
        try:
            market = polymarket_api.fetch_market(A.bare_slug(req["slug"]))
        except ValueError:
            return _stop(ctx, cid, "failed", name, "Polymarket no longer has the market")
        try:
            winner = A.check_gamma(market, cid, req["winner"])
            entry, there = A.plan(req["slug"], name, winner, A.read_markets(), site.added_entries(ctx.api))
        except A.AddError as e:
            return _stop(ctx, cid, "failed", name, str(e))
        if there is not None:
            return _stop(ctx, cid, "added", there["name"], there["note"])
        fills, walk, before = A.Fills(market), {}, 0

    def save():
        """The fills so far and the feed's place, for the next step (v2_pages updates walk before each page)."""
        ref = ctx.put_scratch("fills", _zip({"entry": entry, "condition_id": cid, "walk": walk, "fills": fills.dump()}))
        ctx.units(walk["pages"] - before)
        return ctx.partial({"fills": ref, "pages": walk["pages"]})

    try:
        for page in polymarket_api.v2_pages("trades", A.trade_params(cid), walk):
            fills.add_page(page)
            if fills.n > A.MAX_FILLS:
                return _stop(ctx, cid, "failed", entry["name"],
                             f"too many trades to add automatically (over {A.MAX_FILLS:,} fills)")
            if not walk["done"] and ctx.left() < SAVE_S:
                return save()
    except outcomes.OutOfTime:
        if int(walk.get("pages") or 0) <= before:
            raise
        try:                                                # the pages fetched since the last save are kept
            return save()
        except Exception:
            raise outcomes.OutOfTime("no time left to save the fills")
    if ctx.left() < CLEAN_S:                                # the clean goes in the next step, from the saved fills
        return save() if int(walk.get("pages") or 0) > before else ctx.partial(cur)
    ctx.units(walk.get("pages", 0) - before)
    if not fills.n:
        return _stop(ctx, cid, "failed", entry["name"], "Polymarket served no trades for it")
    try:
        csv_clean, positions = A.clean_text(fills)
        whales = A.whale_list(entry, A.frame(csv_clean))
    except A.AddError as e:
        return _stop(ctx, cid, "failed", entry["name"], str(e))
    except ValueError as e:                                 # correct_outcome against the trades' own win status
        return _stop(ctx, cid, "failed", entry["name"], str(e)[:300])
    if len(whales) > LISTS["add_max_whales"]:
        return _stop(ctx, cid, "failed", entry["name"],
                     f"too large to add automatically ({len(whales)} whales, over {LISTS['add_max_whales']})")
    ref = ctx.put_scratch("clean", gzip.compress(csv_clean.encode("utf-8"), 6, mtime=0))
    n = LISTS["add_whale_batch"]
    for i in range(0, len(whales), n):
        ctx.child("add.whales", str(i // n), {"whales": whales[i:i + n]})
    ctx.meta(condition_id=cid, name=entry["name"], whales=len(whales))
    return ctx.done(results=[("add:fetch", {"entry": entry, "condition_id": cid, "clean": ref, "fills": fills.n,
                                            "positions": positions, "whales": len(whales)})])


# ── add.whales ───────────────────────────────────────────────────────────────

def whale_key(wallet, cutoff):
    return f"whale:{wallet}:{int(cutoff)}"


def whales(ctx):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import polymarket_api

    batch = [(str(w), int(t)) for w, t in (ctx.job.get("input") or {}).get("whales") or []]
    done = set((ctx.cursor or {}).get("done") or [])
    todo = [i for i in range(len(batch)) if i not in done]
    if not todo:
        return ctx.done()
    allowed = ctx.budget.get("add_whales_left")
    if allowed is not None and int((ctx.job.get("run_meta") or {}).get("whales") or 0) > LISTS["add_whales_reserve"]:
        allowed = int(allowed) - LISTS["add_whales_reserve"]         # what a large add leaves to the small ones
    if allowed is not None and int(allowed) <= 0:
        raise outcomes.Later(next_daily(ctx.now()), "budget")
    take = todo if allowed is None else todo[:int(allowed)]
    lock = threading.Lock()

    def one(i):
        outcomes.check(WHALE_NEED_S)
        wallet, cutoff = batch[i]
        totals = polymarket_api.history_totals(wallet, cutoff)
        with lock:
            done.add(i)
            ctx.checkpoint(cursor={"done": sorted(done)}, results=[(whale_key(wallet, cutoff), totals)])
        ctx.units(1)

    errors = []
    with ThreadPoolExecutor(max_workers=max(1, min(LISTS["add_threads"], len(take)))) as pool:
        futures = [outcomes.submit(pool, one, i) for i in take]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                errors.append(e)
    for kind in (outcomes.Lost, outcomes.Later):
        for e in errors:
            if isinstance(e, kind):
                raise e
    real = [e for e in errors if not isinstance(e, outcomes.OutOfTime)]
    if real:
        raise real[0]
    if len(done) == len(batch):
        return ctx.done()
    if not errors and len(take) < len(todo):
        raise outcomes.Later(next_daily(ctx.now()), "budget")
    return ctx.partial({"done": sorted(done)})


# ── add.export ───────────────────────────────────────────────────────────────

def export(ctx):
    import add_market as A
    from backend import site
    from watchlist import sync

    cid = condition_of(ctx)
    fetched = dict(ctx.results("add:fetch")).get("add:fetch")
    name = (fetched or {}).get("entry", {}).get("name") or (ctx.job.get("run_meta") or {}).get("name")
    if fetched is None or any(m.get("kind") in ("add.fetch", "add.whales") for m in ctx.job.get("missing") or []):
        report(ctx, cid, "failed", name, STOPPED)
        return ctx.done()
    data = ctx.get_scratch(fetched["clean"])
    if data is None:
        report(ctx, cid, "failed", name, STOPPED)
        return ctx.done()
    csv_clean = gzip.decompress(data).decode("utf-8")
    totals = dict(ctx.results("whale:"))

    def totals_for(wallet, cutoff):
        got = totals.get(whale_key(wallet, cutoff))
        if got is None:
            raise A.AddError(STOPPED)
        return got

    entry = fetched["entry"]
    try:
        csv_insiders = A.insiders_text(A.score(entry, A.frame(csv_clean), totals_for))
        for _ in range(3):
            index, page, files = A.build(entry, csv_clean, csv_insiders)
            try:
                site.put_market(ctx.api, entry, index, page, files, cid, now=ctx.now())
                break
            except site.NameTaken:                        # another add took the name since add.fetch chose it
                taken = A.read_markets() + site.added_entries(ctx.api) + [entry]
                entry = dict(entry, name=A.page_name(entry["slug"], taken))
                entry["output"] = A.pascal(entry["name"])
        else:
            raise A.AddError("no free page name for it")
    except A.AddError as e:
        report(ctx, cid, "failed", entry["name"], str(e))
        return ctx.done()
    except sync.ApiError as e:
        if e.status == 409 and e.code == "full":
            report(ctx, cid, "failed", entry["name"], FULL)
            return ctx.done()
        raise
    markets = [{"name": m["name"], "slug": m["slug"]} for m in A.read_markets() + site.added_entries(ctx.api)
               if m.get("name") and m.get("slug")]
    ctx.api.request("PUT", "/api/job/catalog", {"markets": markets})
    report(ctx, cid, "added", entry["name"], None)
    ctx.meta(name=entry["name"])
    if score_on_add():
        ctx.child("wallets.score", "0", {"name": entry["name"]})
    return ctx.done()


def score_on_add(environ=None):
    """Whether an add goes on to wallets.score: WALLET_SCORES=on and GEMINI_API_KEY in the environment (Vercel's
    settings: set them once the Worker that knows the kind is deployed; a local stack or a test without them never
    spends the quota), and the rubric's [backend] score_on_add."""
    import os
    environ = os.environ if environ is None else environ
    if str(environ.get("WALLET_SCORES") or "").strip().lower() not in ("1", "on", "true", "yes"):
        return False
    if not str(environ.get("GEMINI_API_KEY") or "").strip():
        return False
    try:
        from backend.rubric import spec
        rubric = spec.load_rubric()
    except (OSError, ValueError):
        return False
    return bool((rubric.get("backend") or {}).get("score_on_add")) and not spec.check(rubric, tokens=set())
