"""
The daily run as steps on Vercel (docs/BACKEND.md):
one handler per job kind of backend.contract's job_kinds under
watchlist.steps. The Worker queues the jobs in D1 and the step function
(backend.runtime) claims one, calls its handler with a StepContext (ctx),
and reports what it returns. Every handler works to the step's deadline and
stops before it: a step that cannot finish its next request returns
partial (its cursor), and the job is claimed again.

  daily.begin     the state pruned and saved; the run's work and time
                  (result daily:work)
  watch.check     each watched card's markets on Polymarket, once a day: one
                  that closed with a result goes to the main page
                  (POST /api/job/requests; the Worker starts its add run)
  closed.scan     the Toronto days to count, their closed markets through the
                  word rules (result closed:candidates, the markets in a
                  scratch blob)
  closed.label    AI labels for the candidates the state has none for
  closed.query    a news query for each market left to check
  closed.news     one Google News search each (news_cache rows)
  closed.judge    the check, five markets to a request
  closed.publish  labels and checks into the state, the day's 3 picks,
                  site:closed.json
  open.walk       the open pool (first-entry 99c checks into the state), its
                  events in a scratch blob (result open:stories)
  open.name       names for unnamed stories, reveal dates
  open.news       one search per story, up to the day's cap
  open.read       one step reading per shortlisted story, up to the cap
  open.out        one "already out" reading per story: the current cards
                  every day, then the candidates, up to the cap
  open.publish    the readings into the state, the day's list, site:open.json
  daily.end       the catalog, the run's report

The state (the state blob, watchlist.state's format 1) is read by any job
into its own folder (ctx.tmp) and saved only by main-lane jobs, with the
version it loaded as expect: a save another job moved first is merged again
onto the fresh state, up to state_save_tries times, then Later
("state_moved"). The other lanes write results, never the state: every row
a step adds to the state's tables (a label, a filing, a reading) goes out as
a result {"table", "row"} in a checkpoint as it is made, and the later jobs
of the run fold those rows into their copy (fold), so each sees what the
earlier ones found and the publish job saves it.
"""
import hashlib
import json
import os
import random
import threading

from backend import contract as C
from backend.outcomes import Later, OutOfTime
from watchlist import ai, blobs, filters, judge, kinds as K, news, opennow, problems, state, store, sync, upsets
from watchlist.common import Budget, local_bounds, local_day, local_iso, log

STEP_TRIES = C.STEP["state_save_tries"]
MODEL_NEED = C.JOB_KINDS["closed.judge"]["need_s"]      # a model request: up to 120 s, and room to report
NEWS_NEED = 40                                          # a search: its 30 s timeout and its retry wait
NEWS_FRESH_S = opennow.NEWS_MAX_AGE_HOURS * 3600        # a search this recent is not made again
NEWS_STORED_S = opennow.STORED_NEWS_HOURS * 3600        # the searches the later steps read
NEWS_FAILS = 3                                          # searches in a row Google refused: the lane waits
TRACKED = {t: state.TABLES[t] for t in ("ai_labels", "closed_checks", "open_filings", "open_answers")}
SITE = {"closed": "site:closed.json", "open": "site:open.json", "index": "site:index.json"}


# ── the context ──────────────────────────────────────────────────────────────

def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def job(ctx, name, default=None):
    """A field of the claimed job (ctx.job)."""
    v = _get(ctx.job, name, default)
    return default if v is None else v


def cap(ctx, name):
    """A cap of the claim's budget (ctx.budget), or None when the claim has none."""
    v = _get(ctx.budget, name)
    return None if v is None else int(v)


def run_id(ctx):
    return job(ctx, "run_id")


def from_run(ctx):
    """The daily run a catch-up reads (its input's from_run), or this run."""
    return (job(ctx, "input", {}) or {}).get("from_run") or run_id(ctx)


def catching_up(ctx):
    return bool((job(ctx, "input", {}) or {}).get("catch_up")) or job(ctx, "run_kind") == "catchup"


def results(ctx, prefix, run=None):
    """{key: value} of a run's results under prefix (this run by default)."""
    return dict(ctx.results(prefix, run_id=run) if run else ctx.results(prefix))


def result(ctx, key, run=None):
    return results(ctx, key, run).get(key)


class _Out:
    """One job's checkpoints, safe from its threads: results and news rows sent as they are made."""

    def __init__(self, ctx):
        self.ctx, self.lock, self.units = ctx, threading.Lock(), 0

    def send(self, results=(), news=(), cursor=None):
        res = [{"key": k, "value": v} for k, v in results]
        rows = [{"key": k, "body": b} for k, b in news]
        if not (res or rows or cursor is not None):
            return
        with self.lock:
            kw = {"results": res, "news": rows}
            if cursor is not None:
                kw["cursor"] = cursor
            self.ctx.checkpoint(**kw)

    def unit(self, n=1):
        with self.lock:
            self.units += n


class StepBudget:
    """common.Budget's face on the step's deadline: left() is the time to it less need seconds (what a request
    must have to end in time), so the lists' code stops starting requests in time."""

    def __init__(self, ctx, need=0.0):
        self.ctx, self.need, self.seconds = ctx, float(need), None

    def left(self):
        return max(0.0, float(self.ctx.left()) - self.need)

    def expired(self, reserve=0.0):
        return self.left() <= reserve


def _short(ctx, need):
    """Fewer than need seconds left in the step."""
    return float(ctx.left()) < need


# ── the state ────────────────────────────────────────────────────────────────

class State:
    """The state blob in a SQLite copy under ctx.tmp. load() reads it (an empty store when there is none yet);
    save(merge) applies merge(conn) and saves with expect=<the version loaded>, merging again onto a fresh copy
    when another job saved first. Returns merge's value."""

    def __init__(self, ctx):
        self.ctx, self.api = ctx, ctx.api
        self.path = os.path.join(ctx.tmp, "watchlist.db")
        self.conn, self.version, self.shas, self.n = None, None, None, 0

    def load(self):
        if self.conn is not None:
            self.conn.close()
        self.n += 1
        path = f"{self.path}.{self.n}"
        for p in (path, path + "-wal", path + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        self.conn = store.connect(path)
        h, doc = blobs.get_json(self.api, state.STATE)
        if h is not None:
            state.load_files(self.conn, state._doc_files(doc), allow_empty=True)
        self.version = h["version"] if h else None
        self.shas = self._shas()
        return self

    def _shas(self):
        return {t: state._sha256(state.table_bytes(self.conn, t)[0]) for t in state.TABLES}

    def save(self, merge):
        for attempt in range(STEP_TRIES):
            if attempt:
                self.load()
            got = merge(self.conn)
            if self._shas() == self.shas:
                return got                                       # nothing changed: nothing to save
            files = state.dump_files(self.conn, by="vercel", made_at=self.ctx.now(), site_dir=self.ctx.tmp)
            manifest = json.loads(files["manifest.json"])
            try:
                h = blobs.put_json(self.api, state.STATE, state._doc(files), manifest, expect=self.version)
            except sync.ApiError as e:
                if e.code == "moved":
                    log(f"  state: saved by another job meanwhile; merging again ({attempt + 1}/{STEP_TRIES})")
                    continue
                raise
            self.version, self.shas = h["version"], self._shas()
            log(f"  state saved: version {h['version'][:8]} ({h['bytes'] / 1e6:.1f} MB)")
            return got
        raise Later(self.ctx.now() + C.RETRY["state_moved_s"], "state_moved")


class Touched:
    """The rows a step adds or replaces in the state's tracked tables (temporary triggers on its own copy):
    take() returns [(table, row)] since the last take."""

    def __init__(self, conn):
        self.conn = conn
        with store.LOCK:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _touched (tbl TEXT, rid INTEGER, PRIMARY KEY (tbl, rid))")
            for t in TRACKED:
                for ev in ("INSERT", "UPDATE"):
                    conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS _t_{t}_{ev.lower()} AFTER {ev} ON main.{t} "
                                 f"BEGIN INSERT OR IGNORE INTO _touched VALUES ('{t}', NEW.rowid); END")
            conn.commit()

    def take(self):
        out = []
        with store.LOCK:
            for t, rid in self.conn.execute("SELECT tbl, rid FROM _touched ORDER BY rowid").fetchall():
                cur = self.conn.execute(f"SELECT * FROM {t} WHERE rowid = ?", (rid,))
                cols = [d[0] for d in cur.description]
                row = cur.fetchone()
                if row is not None:
                    out.append((t, dict(zip(cols, row))))
            self.conn.execute("DELETE FROM _touched")
            self.conn.commit()
        return out


def row_key(kind, table, row):
    pk = json.dumps([row.get(c) for c in TRACKED[table]], ensure_ascii=False)
    return f"{kind}:{table}:{hashlib.sha1(pk.encode('utf-8')).hexdigest()[:24]}"


def row_results(kind, rows):
    return [(row_key(kind, t, r), {"table": t, "row": r}) for t, r in rows]


def fold(conn, ctx, prefixes, run=None):
    """The rows earlier jobs of the run wrote as results (row_results) into this copy of the state; returns how
    many. Replaying a row is harmless: each replaces itself."""
    n = 0
    with store.transaction(conn):
        for prefix in prefixes:
            for _, v in sorted(results(ctx, prefix, run).items()):
                if not (isinstance(v, dict) and v.get("table") in TRACKED and isinstance(v.get("row"), dict)):
                    continue
                cols = sorted(v["row"])
                conn.execute(f"INSERT OR REPLACE INTO {v['table']} ({', '.join(cols)}) "
                             f"VALUES ({', '.join('?' * len(cols))})", [v["row"][c] for c in cols])
                n += 1
    return n


# ── scratch blobs and the published files ───────────────────────────────────

def scratch_name(run, name):
    return f"scratch:{hashlib.sha1(run.encode('utf-8')).hexdigest()[:16]}:{name}"


def put_scratch(ctx, name, doc):
    """A run's document over a result's 200 KB: a scratch blob (pruned after 3 days); returns its reference."""
    bname = scratch_name(run_id(ctx), name)
    h = blobs.put_json(ctx.api, bname, doc, {"run": run_id(ctx), "made_at": local_iso(ctx.now())})
    return {"blob": bname, "version": h["version"]}


def get_scratch(ctx, ref):
    """The document a reference points at, or None when it is gone."""
    if not isinstance(ref, dict) or not ref.get("blob"):
        return None
    try:
        h, doc = blobs.get_json(ctx.api, ref["blob"])
    except blobs.BlobError as e:
        log(f"  ! {ref['blob']}: {e}")
        return None
    return doc if h is not None else None


def get_site(ctx, name):
    """A published file (site:closed.json, site:open.json, site:index.json), parsed, or None."""
    try:
        h, doc = blobs.get_json(ctx.api, SITE[name])
    except blobs.BlobError as e:
        log(f"  ! {SITE[name]} is damaged ({e}); counted as none")
        return None
    return doc if h is not None else None


def put_site(ctx, name, data):
    """Publish a list: site:closed.json or site:open.json, byte for byte (the Worker serves it at /api/data/)."""
    from watchlist import publish
    return publish.put_list(ctx.api, name, data, made_at=ctx.now())


def added_entries(ctx):
    """The added markets' index entries (site:index.json), for the main page's slugs."""
    doc = get_site(ctx, "index") or {}
    return [m for m in doc.get("markets") or [] if isinstance(m, dict)]


# ── the run's work and time ──────────────────────────────────────────────────

def work_of(ctx):
    """daily.begin's daily:work result for this run (a catch-up: its day's run), or a fresh one."""
    w = result(ctx, "daily:work", from_run(ctx))
    if isinstance(w, dict):
        return w
    return {"now": int(ctx.now()), "work": {}}


def run_now(ctx):
    """The daily run's own time: every step of it builds the lists as at this moment (news windows, days)."""
    return int(work_of(ctx).get("now") or ctx.now())


def fresh_work(ctx, fallback):
    """GET /api/job/work now (the markets sent to the main page since the run began), else fallback."""
    try:
        return ctx.api.request("GET", "/api/job/work") or fallback
    except sync.ApiError as e:
        if e.refused:
            raise
        return fallback


def requested_of(work):
    rows = list(work.get("requests") or []) + list(work.get("added") or [])
    return frozenset(str(r["condition_id"]).lower() for r in rows if isinstance(r, dict) and r.get("condition_id"))


def watched_of(work):
    return {str(w["story"]) for w in work.get("watched") or [] if isinstance(w, dict) and w.get("story")}


def note(ctx, message):
    """One line for the run's report (run_meta problems), once a run: two jobs hitting the same wall say it once."""
    problems.note(message)
    got = list((getattr(ctx, "_meta", None) or {}).get("problems") or job(ctx, "run_meta", {}).get("problems") or [])
    line = problems._one_line(message)
    if line not in got:
        ctx.meta(problems=(got + [line])[-10:])


# ── what a failed model or news request means for the job ───────────────────

def model_later(ctx, e, client=None):
    """A model request that failed: Later for a busy model (5-10 minutes, the lane paused) or a spent daily quota
    (until it resets, the lane paused), when that comes before the run's deadline; None (go on without it) for a
    refused key, a refused request, or a reset after the deadline."""
    now = int(ctx.now())
    spent = client is not None and judge._all_spent(client)
    if getattr(e, "config", False) or getattr(e, "status", None) in (401, 403):
        note(ctx, f"the Gemini key was refused ({ai._scrub(str(e))[:120]})")
        return None
    if spent or (getattr(e, "status", None) == 429 and ai._DAILY_QUOTA.search(str(e))):
        reset = C.next_quota_reset(now)
        if reset >= int(job(ctx, "run_deadline", 0) or 0):
            note(ctx, "the model's daily allowance is spent; the rest waits for the next run")
            return None
        return Later(reset, "model_quota", pause_lane_until=reset)
    if ai.is_busy(e):
        wait = now + random.randint(*C.RETRY["model_busy_s"])
        return Later(wait, "model_busy", pause_lane_until=wait)
    return None


def news_later(ctx):
    wait = int(ctx.now()) + C.RETRY["news_limited_s"]
    return Later(wait, "news_limited", pause_lane_until=wait)


def polymarket_later(ctx, e):
    if problems.classify(e) == problems.OUTSIDE:
        return Later(int(ctx.now()) + C.RETRY["polymarket_down_s"], "polymarket_down")
    return None


# ── daily.begin, watch.check ─────────────────────────────────────────────────

def daily_begin(ctx):
    now = int(ctx.now())
    work = ctx.api.request("GET", "/api/job/work") or {}
    work = {k: list(work.get(k) or []) for k in ("requests", "watched", "added")}
    st = State(ctx).load()
    st.save(lambda conn: state.prune(conn, now))
    ctx.checkpoint(results=[{"key": "daily:work", "value": {"now": now, "day": local_day(now), "work": work}}])
    log(f"daily run {local_day(now)}: {len(work['watched'])} watched card(s)")


def watch_check(ctx):
    work = work_of(ctx)["work"]
    stories = sorted(watched_of(work))
    if not stories:
        ctx.checkpoint(results=[{"key": "watch:done", "value": {"finished": []}}])
        return
    st = State(ctx).load()
    on_main = sync.main_page_slugs(added_entries(ctx))
    _, finished = sync.send_watched(ctx.api, stories, on_main, lambda s: sync.story_markets(st.conn, s))
    ctx.checkpoint(results=[{"key": "watch:done", "value": {"finished": sorted(set(finished))}}])


# ── the closed list ──────────────────────────────────────────────────────────

def closed_scan(ctx):
    from pipeline import polymarket_api
    from watchlist import net
    now = run_now(ctx)
    st = State(ctx).load()
    prev = get_site(ctx, "closed")
    days = upsets.counting_days(prev, now, C.LISTS["closed_days"])
    since = local_bounds(days[0])[0]
    try:
        closed = net.closed_markets(since, now, min_volume=upsets.MIN_VOLUME)
    except polymarket_api.PolymarketAPIError as e:
        later = polymarket_later(ctx, e)
        if later is None:
            raise
        raise later
    grouped = upsets.by_day(closed)
    out, n = {}, 0
    for d in days:
        ms = [filters.slim_market(m) for m in grouped.get(d, [])]
        kept, dropped = upsets.word_rules(ms, st.conn)
        out[d] = {"markets": kept, "dropped": dict(dropped), "closed_markets": len(ms)}
        n += len(kept)
    ref = put_scratch(ctx, "closed-scan", {"now": now, "days": out})
    ctx.checkpoint(results=[{"key": "closed:candidates", "value": {"ref": ref, "days": days, "markets": n}}])
    log(f"closed list: {len(closed)} closed since {local_iso(since)}, {n} through the word rules ({', '.join(days)})")


def _scan(ctx):
    """closed.scan's days ({day: {markets, dropped, closed_markets}}), or None when it did not finish."""
    got = result(ctx, "closed:candidates")
    doc = get_scratch(ctx, (got or {}).get("ref"))
    return (doc or {}).get("days") if isinstance(doc, dict) else None


def _closed_state(ctx):
    st = State(ctx).load()
    fold(st.conn, ctx, ("closed.label:",))
    return st


def _checks_done(ctx):
    return results(ctx, "closed.judge:")


def _candidates(scan, conn, today):
    """[(day, market, label, folded)] of the scanned days, with the labels the state (and this run) holds."""
    out = []
    for day, d in sorted(scan.items()):
        labels = upsets.cached_labels(d["markets"], conn, today)
        cands, _ = upsets.after_labels(d["markets"], labels, conn, d.get("dropped") or {})
        out += [(day, m, lab, folded) for m, lab, folded in cands]
    return out


def _unchecked(ctx, conn, cands):
    """The candidates the state has no check for and this run has not judged."""
    done = _checks_done(ctx)
    return [m for _, m, _, _ in cands if not store.get_closed_check(conn, m["conditionId"], judge.PROMPT_VERSION)
            and f"closed.judge:{m['conditionId']}" not in done]


def closed_label(ctx):
    scan = _scan(ctx)
    if not scan:
        return
    st = _closed_state(ctx)
    today = local_day(run_now(ctx))
    cursor = dict(job(ctx, "cursor", {}) or {})
    tried = dict(cursor.get("tried") or {})
    kept = [m for d in scan.values() for m in d["markets"]]
    have = upsets.cached_labels(kept, st.conn, today)
    todo = [m for m in kept if str(m.get("id")) not in have and tried.get(str(m.get("id")), 0) < 2]
    if not todo:
        return
    out, client, seen = _Out(ctx), ai.Client(), Touched(st.conn)
    for i in range(0, len(todo), ai.BATCH_SIZE):
        if _short(ctx, MODEL_NEED):
            return ctx.partial({"tried": tried})
        batch = todo[i:i + ai.BATCH_SIZE]
        try:
            got = ai.label_markets(batch, conn=st.conn, client=client, workers=1, today=today)
        except ai.GeminiError as e:
            out.send(row_results("closed.label", seen.take()))
            later = model_later(ctx, e, client)
            if later is not None:
                raise later
            return
        for m in batch:
            tried[str(m.get("id"))] = tried.get(str(m.get("id")), 0) + 1
        out.send(row_results("closed.label", seen.take()), cursor={"tried": tried})
        out.unit()
        if not got and judge._all_spent(client):
            later = model_later(ctx, ai.GeminiError("no model left today", status=429), client)
            if later is not None:
                raise later
            return


def closed_query(ctx):
    scan = _scan(ctx)
    if not scan:
        return
    st = _closed_state(ctx)
    cands = _candidates(scan, st.conn, local_day(run_now(ctx)))
    done = results(ctx, "closed.query:")
    todo = [m for m in _unchecked(ctx, st.conn, cands) if f"closed.query:{m['conditionId']}" not in done]
    out, client = _Out(ctx), ai.Client()
    for i in range(0, len(todo), judge.QUERY_BATCH):
        if _short(ctx, MODEL_NEED):
            return ctx.partial({"left": len(todo) - i})
        batch = todo[i:i + judge.QUERY_BATCH]
        queries = judge.write_queries(batch, client)         # the question itself stands in for a missing one
        out.send([(f"closed.query:{m['conditionId']}", {"query": queries[judge.short_id(m)]}) for m in batch])
        out.unit()


def closed_news(ctx):
    scan = _scan(ctx)
    if not scan:
        return
    st = _closed_state(ctx)
    cands = _candidates(scan, st.conn, local_day(run_now(ctx)))
    queries, found = results(ctx, "closed.query:"), results(ctx, "closed.news:")
    todo = []
    for m in _unchecked(ctx, st.conn, cands):
        cid = m["conditionId"]
        q = (queries.get(f"closed.query:{cid}") or {}).get("query") or judge.fallback_query(m)
        if f"closed.news:{cid}" not in found:
            after, before = judge.news_window(m)
            todo.append((m, q, f"{q}|{news.build_query('', after, before).strip()}"))
    if not todo:
        return
    stored = {}
    keys = sorted({k for _, _, k in todo})
    for i in range(0, len(keys), C.LIMITS["news_get_keys"]):
        stored.update(_news_bodies(ctx.news_get(keys[i:i + C.LIMITS["news_get_keys"]], NEWS_FRESH_S)))
    news.SPACING.set(news.Spacer())
    out, fails = _Out(ctx), 0
    try:
        for m, q, key in todo:
            raw = stored.get(key)
            if raw is None:
                if _short(ctx, NEWS_NEED):
                    return ctx.partial({"left": len(todo)})
                after, before = judge.news_window(m)
                try:
                    raw = news.search(q, after=after, before=before)
                except news.NewsError as e:
                    log(f"  ! {m.get('question')}: {e}")
                    fails = fails + 1 if e.transient else fails
                    if fails >= NEWS_FAILS:
                        raise news_later(ctx)
                    continue
                fails = 0
                out.unit()
                stored[key] = raw
                out.send([(f"closed.news:{m['conditionId']}", {"query": q, "headlines": judge.judge_headlines(raw)})],
                         news=[(key, raw)])
            else:
                out.send([(f"closed.news:{m['conditionId']}", {"query": q, "headlines": judge.judge_headlines(raw)})])
    finally:
        ctx.units(out.units)


def _news_bodies(got):
    """{key: headlines} from ctx.news_get's answer ({key: headlines}, or the Worker's {key: {day, fetched_at,
    body}})."""
    out = {}
    for k, v in (got or {}).items():
        body = v.get("body") if isinstance(v, dict) else v
        if isinstance(body, list):
            out[k] = body
    return out


def closed_judge(ctx):
    scan = _scan(ctx)
    if not scan:
        return
    st = _closed_state(ctx)
    cands = _candidates(scan, st.conn, local_day(run_now(ctx)))
    found = results(ctx, "closed.news:")
    cursor = dict(job(ctx, "cursor", {}) or {})
    refused = set(cursor.get("refused") or [])
    todo = []
    for m in _unchecked(ctx, st.conn, cands):
        n = found.get(f"closed.news:{m['conditionId']}")
        if isinstance(n, dict) and isinstance(n.get("headlines"), list) and m["conditionId"] not in refused:
            todo.append((m, n["query"], n["headlines"]))
    out = _Out(ctx)
    client = ai.Client(models=judge.JUDGE_MODELS, retries=judge.JUDGE_RETRIES)
    for i in range(0, len(todo), judge.JUDGE_BATCH):
        if _short(ctx, MODEL_NEED):
            return ctx.partial({"refused": sorted(refused)})
        batch = todo[i:i + judge.JUDGE_BATCH]
        try:
            got, model = judge._ask(client, judge.JUDGE_PROMPT, [judge.card(m, h) for m, _, h in batch],
                                    judge.CHECK_SCHEMA, "checks")
        except (ai.GeminiError, ValueError) as e:
            if isinstance(e, ai.GeminiError) and not e.refused:
                later = model_later(ctx, e, client)
                if later is not None:
                    raise later
                return
            refused |= {m["conditionId"] for m, _, _ in batch}          # the same request is refused the same way
            out.send(cursor={"refused": sorted(refused)})
            continue
        rows = []
        for m, q, h in batch:
            c = judge.normalise(got.get(judge.short_id(m)) or {})
            if c is not None:
                c.update(query=q, headlines=h, model=model)
                rows.append((f"closed.judge:{m['conditionId']}", c))
        out.send(rows)
        out.unit()


def closed_publish(ctx):
    scan = _scan(ctx)
    if not scan:
        note(ctx, "the closed list's scan did not finish; the list keeps its last version")
        return
    now = run_now(ctx)
    base = work_of(ctx)["work"]
    work = fresh_work(ctx, base)
    requested, watched = requested_of(work), watched_of(base)
    prev = get_site(ctx, "closed") or {}
    checks = _checks_done(ctx)
    st = State(ctx).load()

    def merge(conn):
        fold(conn, ctx, ("closed.label:",))
        for key, c in sorted(checks.items()):
            if isinstance(c, dict) and c.get("verdict"):
                store.save_closed_check(conn, key.split(":", 1)[1], judge.PROMPT_VERSION, c)
        old = {d["day"]: d for d in prev.get("days") or [] if isinstance(d, dict) and d.get("day")}
        today, missed = local_day(now), 0
        for day, d in sorted(scan.items()):
            labels = upsets.cached_labels(d["markets"], conn, today)
            cands, _ = upsets.after_labels(d["markets"], labels, conn, d.get("dropped") or {})
            have = {}
            for m, _, _ in cands:
                c = store.get_closed_check(conn, m["conditionId"], judge.PROMPT_VERSION)
                if c:
                    have[m["conditionId"]] = judge.settle(c)
            got = upsets.run_day(day, None, conn, watched=watched, before=old.get(day), requested=requested,
                                 labels=labels, checks=have, scanned=d)
            got["counted_at"] = local_iso(now)
            missed += upsets.unchecked(got)
            old[day] = got
        return upsets.make_doc(old, now), missed

    doc, missed = st.save(merge)
    put_site(ctx, "closed", upsets.dumps(doc))
    if missed:
        note(ctx, f"the closed list could not check {missed} market(s); their days keep the picks they had")
    log(f"closed list published: {sum(len(d['picks']) for d in doc['days'][:len(scan)])} pick(s) on "
        f"{len(scan)} day(s)")


# ── the open list ────────────────────────────────────────────────────────────

_EVENT_DROP = frozenset({"markets", "image", "icon", "featuredImage", "imageOptimized", "iconOptimized",
                         "featuredImageOptimized", "clobRewards", "eventCreators", "chats", "templates"})
_MARKET_DROP = frozenset({"events", "image", "icon", "imageOptimized", "iconOptimized", "clobRewards"})


def _small(v):
    return v is None or isinstance(v, (str, int, float, bool))


def _tags(xs):
    return [{k: t.get(k) for k in ("id", "slug", "label")} if isinstance(t, dict) else t for t in xs or []]


def trim_event(e):
    """What the open list's later steps read of a walked event: its and its markets' plain fields (and their
    tags and series, cut down), no nested objects (the walk's 300 MB is mostly those)."""
    out = {k: v for k, v in e.items() if k not in _EVENT_DROP and _small(v)}
    for k in ("tags", "series"):
        if isinstance(e.get(k), list):
            out[k] = _tags(e[k]) if k == "tags" else [{x: s.get(x) for x in ("id", "slug", "title", "recurrence")}
                                                      for s in e[k] if isinstance(s, dict)]
    ms = []
    for m in e.get("markets") or []:
        if m.get("closed"):
            continue
        t = {k: v for k, v in m.items() if k not in _MARKET_DROP and _small(v)}
        if isinstance(m.get("tags"), list):
            t["tags"] = _tags(m["tags"])
        ms.append(t)
    out["markets"] = ms
    return out


def _events(ctx, run=None):
    """open.walk's events (the pool, trimmed) and the walk's time, or (None, None)."""
    got = result(ctx, "open:stories", run or from_run(ctx))
    doc = get_scratch(ctx, (got or {}).get("ref"))
    if not isinstance(doc, dict):
        return None, None
    return doc.get("events") or [], int(doc.get("now") or 0)


class RowNews:
    """opennow.NewsCache's face on the news_cache rows: prefetch(keys) loads the stored searches (fetched within
    max_age_s), get() reads them, put() checkpoints a new one (put_rows) as it is made."""

    def __init__(self, ctx, max_age_s, out=None):
        self.ctx, self.max_age_s, self.out, self.data = ctx, int(max_age_s), out, {}

    def prefetch(self, keys):
        todo = sorted({k for k in keys if k not in self.data and len(k) <= C.LIMITS["news_key_chars"]})
        step = C.LIMITS["news_get_keys"]
        for i in range(0, len(todo), step):
            self.data.update(_news_bodies(self.ctx.news_get(todo[i:i + step], self.max_age_s)))

    def get(self, key, now):
        return self.data.get(key)

    def put(self, key, headlines, now):
        self.data[key] = headlines
        if self.out is not None:
            self.out.unit()
            self.out.send(news=[(key, headlines)])

    def save(self, now=None):
        pass


def _open_state(ctx, prefixes=("open.name:",)):
    st = State(ctx).load()
    fold(st.conn, ctx, prefixes)
    return st


def _no_events(ctx):
    note(ctx, "the open list's walk did not finish; the list keeps its last version")


def open_walk(ctx):
    from pipeline import polymarket_api
    from watchlist import net
    now = run_now(ctx)
    try:
        events = net.walk_open_events(min_volume=K.MIN_VOLUME)
    except polymarket_api.PolymarketAPIError as e:
        later = polymarket_later(ctx, e)
        if later is None:
            raise
        raise later
    st = State(ctx).load()

    def merge(conn):
        r = opennow.Run(conn, now, walk=lambda: events)
        r.build_pool()                                           # first-entry 99c checks recorded, never again
        return {str(e.get("event_id") or e["id"]).split("/")[0] for e in r.events}

    pool = st.save(merge)
    slim = [trim_event(e) for e in events if str(e.get("id")) in pool]
    ref = put_scratch(ctx, "open-events", {"now": now, "events": slim})
    ctx.checkpoint(results=[{"key": "open:stories", "value": {"ref": ref, "events": len(slim)}}])
    log(f"open list: {len(slim)} events in the pool")


def open_name(ctx):
    events, _ = _events(ctx)
    if events is None:
        return
    now = run_now(ctx)
    st = _open_state(ctx)
    out, seen = _Out(ctx), Touched(st.conn)
    r = opennow.Run(st.conn, now, client=opennow.open_client(), walk=lambda: events, record_pool=False,
                    budget=StepBudget(ctx, MODEL_NEED), catch_up=catching_up(ctx))
    try:
        r.build_pool()
        r.build_stories()
        r.name_stories()
        r.reveal_dates()
    finally:
        out.send(row_results("open.name", seen.take()))
        ctx.units(sum(r.requests.values()))
    if r.left.get("names"):
        return ctx.partial({"left": len(r.left["names"])})
    if r.names_down:
        later = model_later(ctx, ai.GeminiError(r.names_down, status=503), r.client)
        if later is not None and int(job(ctx, "laters", 0)) < 3:
            raise later


def _open_run(ctx, st, events, now, cache, **knobs):
    """A Run of the day's open list on this job's state copy, its news from the stored searches (cache)."""
    return opennow.make_run(st.conn, now, walk=lambda: events, news_cache=cache,
                            before_news=lambda r: cache.prefetch([k for _, _, k in r.news_keys()]),
                            record_pool=False, naming=False, label_cache_only=True, catch_up=catching_up(ctx),
                            **knobs)


def open_news(ctx):
    events, _ = _events(ctx)
    if events is None:
        return
    now = run_now(ctx)
    st = _open_state(ctx)
    out = _Out(ctx)
    cache = RowNews(ctx, NEWS_FRESH_S, out)
    news.SPACING.set(news.Spacer())
    searched = []

    def search(q, **kw):
        searched.append(q)
        return news.search(q, **kw)

    r = None
    try:
        r = _open_run(ctx, st, events, now, cache, cached_news=False, search=search, client=None,
                      news_limit=cap(ctx, "open_searches"), budget=StepBudget(ctx, NEWS_NEED),
                      sleep=lambda s: _sleep(ctx, s))
    except opennow.NotStored:
        return
    finally:
        ctx.units(out.units)
    if r.down == opennow.NEWS_LATE:
        return ctx.partial({"searched": len(r.searched)})
    if r.news_down:
        if int(job(ctx, "laters", 0)) < 3:
            raise news_later(ctx)
        note(ctx, f"{opennow.NEWS_DOWN}: the open list is made from the searches stored")
    log(f"open list: news for {len(r.searched)} stories ({len(searched)} new searches)")


def _sleep(ctx, seconds):
    from watchlist.common import step_sleep
    step_sleep(seconds, "a news retry")


def _answers_hook(ctx, out, kind):
    """on_answer for a Run: each reading stored goes out at once as a result row."""
    def hook(story, sha, version, run, data, model):
        row = {"story": story, "headlines_sha1": sha, "version": version, "run": int(run),
               "data": json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str), "model": model,
               "created_at": run_now(ctx)}
        out.send(row_results(kind, [("open_answers", row)]))
        if model != "rule":
            out.unit()
    return hook


def _done_today(ctx, prefix):
    """Readings this day's runs made under prefix (the daily run's and, for a catch-up, its own)."""
    n = len(results(ctx, prefix))
    if catching_up(ctx) and from_run(ctx) != run_id(ctx):
        n += len(results(ctx, prefix, from_run(ctx)))
    return n


def _left_of(r, what):
    return {"left": sorted(r.left.get(what) or ()), "step_failed": sorted(r.step_failed),
            "step_failed_busy": sorted(r.step_failed_busy), "down": r.down, "down_busy": r.down_busy,
            "found": r.found}


def open_read(ctx):
    events, _ = _events(ctx)
    if events is None:
        return
    now = run_now(ctx)
    st = _open_state(ctx, ("open.name:", "open.read:"))
    out = _Out(ctx)
    limit = cap(ctx, "open_reads")
    left = None if limit is None else max(0, limit - _done_today(ctx, "open.read:open_answers:"))
    r = None
    try:
        r = _open_run(ctx, st, events, now, RowNews(ctx, NEWS_STORED_S), cached_news=True, client=None,
                      workers=C.LISTS["open_read_workers"], reads_left=left, out_reserve_minutes=0,
                      budget=StepBudget(ctx, MODEL_NEED), on_answer=_answers_hook(ctx, out, "open.read"))
        prev = store.previous_open_day(st.conn, r.today)
        prev_updated = store.previous_open_day(st.conn, r.today, status=opennow.STATUS_UPDATED)
        r.shortlist(prev_updated if prev_updated is not None else prev)
        r.read_all_steps()
    except opennow.NotStored:
        return
    finally:
        ctx.units(out.units)
        if r is not None:
            out.send([("open.read:left", _left_of(r, "steps 1"))])
    if r.left.get("steps 1") and r.steps_spent():
        return ctx.partial({"read": r.step_stats.get("reading 1: stories read", 0)})
    if r.down and r.down_busy:
        later = model_later(ctx, ai.GeminiError(r.down, status=503), r.step_client)
        if later is not None and int(job(ctx, "laters", 0)) < 3:
            raise later


def open_out(ctx):
    events, _ = _events(ctx)
    if events is None:
        return
    now = run_now(ctx)
    st = _open_state(ctx, ("open.name:", "open.read:", "open.out:"))
    out = _Out(ctx)
    limit = cap(ctx, "open_out")
    left = None if limit is None else max(0, limit - _done_today(ctx, "open.out:open_answers:"))
    r = None
    try:
        r = _open_run(ctx, st, events, now, RowNews(ctx, NEWS_STORED_S), cached_news=True, client=None,
                      read_steps=False, out_left=left, out_reserve_minutes=0, budget=StepBudget(ctx, MODEL_NEED),
                      on_answer=_answers_hook(ctx, out, "open.out"))
        prev = store.previous_open_day(st.conn, r.today)
        prev_updated = store.previous_open_day(st.conn, r.today, status=opennow.STATUS_UPDATED)
        r.build(prev, prev_updated)
    except opennow.NotStored:
        return
    finally:
        ctx.units(out.units)
        if r is not None:
            out.send([("open.out:left", _left_of(r, "already out"))])
    if r.left.get("already out") and r.spent():
        return ctx.partial({"found": r.found})
    if r.down and r.down_busy:
        later = model_later(ctx, ai.GeminiError(r.down, status=503), r.client)
        if later is not None and int(job(ctx, "laters", 0)) < 3:
            raise later


def _carry(ctx):
    """What open.read and open.out left, for the day's record (opennow.daily's carry)."""
    read, out = (result(ctx, k) for k in ("open.read:left", "open.out:left"))
    got = [v for v in (read, out) if isinstance(v, dict)]
    left = {}
    if isinstance(read, dict):
        left["steps 1"] = read.get("left") or []
    if isinstance(out, dict):
        left["already out"] = out.get("left") or []
    down = next((g for g in got if g.get("down")), {})
    return {"left": left, "step_failed": sorted({k for g in got for k in g.get("step_failed") or []}),
            "step_failed_busy": sorted({k for g in got for k in g.get("step_failed_busy") or []}),
            "down": down.get("down"), "down_busy": bool(down.get("down_busy")),
            "found": sum(int(g.get("found") or 0) for g in got)}


def open_publish(ctx):
    events, _ = _events(ctx)
    if events is None:
        _no_events(ctx)
        return
    now = run_now(ctx)
    carry = _carry(ctx)
    st = State(ctx).load()

    def merge(conn):
        fold(conn, ctx, ("open.name:", "open.read:", "open.out:"))
        cache = RowNews(ctx, NEWS_STORED_S)
        try:
            return opennow.daily(conn=conn, now=now, walk=lambda: events, out_path=None, news_cache=cache,
                                 before_news=lambda r: cache.prefetch([k for _, _, k in r.news_keys()]),
                                 cached_news=True, naming=False, label_cache_only=True, record_pool=False,
                                 read_steps=False, budget=Budget(0), catch_up=catching_up(ctx), carry=carry)
        except opennow.NotStored as e:
            note(ctx, f"the catch-up changed nothing: the day's news searches are not all stored ({e})")
            return None

    summary = st.save(merge)
    if summary is None:
        return
    put_site(ctx, "open", _open_bytes(opennow.open_doc(st.conn, now)))
    day = opennow.latest_day(st.conn)
    ctx.meta(left=opennow.left_unread(day))
    log(f"open list published: {summary['day']} {summary['status']}, {len(summary['picks'])} card(s)")


def _open_bytes(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ── daily.end ───────────────────────────────────────────────────────────────

def _catalog(ctx, conn):
    """PUT /api/job/catalog: the main page's markets, the closed list's picks, the open list's cards."""
    paths = {}
    for name in ("closed", "open"):
        doc = get_site(ctx, name)
        if doc is not None:
            paths[f"{name}_path"] = os.path.join(ctx.tmp, f"{name}.json")
            with open(paths[f"{name}_path"], "w") as f:
                json.dump(doc, f)
        else:
            paths[f"{name}_path"] = os.path.join(ctx.tmp, f"no-{name}.json")
    body = sync.catalog_payload(added=added_entries(ctx), **paths)
    if body:
        ctx.api.request("PUT", "/api/job/catalog", body)


def _report(ctx, conn, mode, watched=()):
    meta = job(ctx, "run_meta", {}) or {}
    found = list(meta.get("problems") or [])
    missing = [m for m in job(ctx, "missing", []) or [] if isinstance(m, dict)]
    found += [f"{m.get('kind')} {m.get('state')}" + (f": {m.get('error')}" if m.get("error") else "") for m in missing]
    closed_path = os.path.join(ctx.tmp, "closed.json")
    summary = sync.run_summary(conn, closed_path if os.path.exists(closed_path) else os.path.join(ctx.tmp, "none"),
                               started_at=run_now(ctx))
    status = "partial" if found else "success"
    body = {"requests": [], "watched": sorted(watched),
            "run": {"mode": mode, "status": status, "started_at": run_now(ctx), "finished_at": int(ctx.now()),
                    "summary": "; ".join([summary] + [problems._one_line(p, 160) for p in found])[:500],
                    "url": None}}
    ctx.api.request("POST", "/api/job/done", body)
    log(f"reported ({status}): {summary}")


def daily_end(ctx):
    st = State(ctx).load()
    _catalog(ctx, st.conn)
    if catching_up(ctx):
        log("catch-up: the catalog sent")
        return
    watched = (result(ctx, "watch:done") or {}).get("finished") or []
    _report(ctx, st.conn, "full", watched)


__all__ = ["daily_begin", "watch_check", "closed_scan", "closed_label", "closed_query", "closed_news", "closed_judge",
           "closed_publish", "open_walk", "open_name", "open_news", "open_read", "open_out", "open_publish",
           "daily_end", "OutOfTime"]
