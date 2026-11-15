"""
The daily job's side of the site's API (worker/: a Cloudflare Worker and its
D1 database; docs/DEPLOY.md). Visitors and the site's owner write there; the
job reads what they wrote, acts on it, and reports back:

  pull     GET /api/job/work: the owner's pending decisions on the open
           list's queue, the "Add to main page" requests the owner
           approved, and how many visitors watch each open card
           -> data/watchlist/work.json
  decide   the decisions into the store, as --approve and --reject record
           them (store.save_review), and the ones the owner took back
           ("withdrawn") out of it again (a run may have saved them before
           they were taken back); in "apply" mode the reviewed day is
           rebuilt at once (opennow.rebuild_reviewed), in "full" and "local"
           mode the day's run that follows reads them itself
  add      each approved request through add_market.add (markets.json, the
           trades, clean, score); the workflow commits the files to main.
           Before each one, add_results.json already says it failed ("stopped
           part way"), so a step killed by its timeout still reports it
  catalog  PUT /api/job/catalog: what visitors may write to (the main
           page's markets, the closed list's picks, the cards of the open
           list's two newest days)
  push     PUT /api/job/queue (the latest day's queue, each candidate with
           its key), the catalog, then POST /api/job/done: the decisions
           this run saved, only once the state holding them was pushed; the
           requests added (only once main was pushed) or failed; the run

    python3 -m watchlist.sync pull [--out data/watchlist/work.json]
    python3 -m watchlist.sync decide --mode full|apply|local [--work PATH] [--open-out PATH]
    python3 -m watchlist.sync add [--work PATH] [--dry-run]
    python3 -m watchlist.sync catalog [--closed PATH] [--open PATH]
    python3 -m watchlist.sync push --mode full|apply|local --status success|partial|failure --started-at EPOCH
                                   [--run-url URL]

The API is WATCHLIST_API_URL with WATCHLIST_JOB_TOKEN (the environment
first, then ../.env, like the Gemini key). With neither, every command says
so and does nothing; an API that does not answer is warned about and never
fails a step: the lists run without it, and decisions and requests wait for
the next run. The token goes in one header and is never printed. Steps set
key=value outputs in $GITHUB_OUTPUT when it is set.

The shapes (worker/src, web/src/lib/api.ts):

  Headline   {title, source, published, url}; a Candidate adds key (opennow.headline_key)
  QueueItem  one item of opennow's queue (Run.awaiting): {n, story, place, why, kind, group, question,
             who_knows, polymarket, url, reveal, candidates: [Candidate], model_copy_unmatched}
  Queue      {day, run_at, steps_by, items: [QueueItem]}                         PUT /api/job/queue
  Decision   {story, headline_key, decision: "step" | "not a step" | "card approved" | "withdrawn",
              headline: Headline | null, day, decided_at, applied_at}           (headline_key "card": a dated card;
                                                                                 "withdrawn": taken back, only in work)
  AddRequest {condition_id, slug, question, url, winner, closed, day, status, name, note, requested_at,
              decided_at, done_at}
  work       GET /api/job/work -> {now, decisions: [Decision], requests: [AddRequest], watched: [{story, count}]}
  catalog    PUT /api/job/catalog {markets: [{name, slug}], closed: [{condition_id, slug, question, url, winner,
             closed, day}], open: [{story, question, url}]}
  done       POST /api/job/done {applied: [{story, headline_key, decided_at}], requests: [{condition_id,
             status: "added" | "failed", name, note}], run: {mode, status, started_at, finished_at, summary, url}}

Files under data/watchlist/ (WATCHLIST_JOB_DIR moves them) the steps leave
for each other: work.json, decide_results.json, add_results.json, and
state_pulled.json / state_pushed.json (watchlist.state) and markets_pushed.ok
(the workflow, once main was pushed). A results file older than the run
(--started-at) is never reported.
"""
import argparse
import json
import os
import sys
import time

import requests

from watchlist import ai, store
from watchlist.common import JOB_DIR, ROOT, SITE_DATA_DIR, log, now_epoch, to_epoch

TIMEOUT = 20
RETRY_WAITS = (2, 8, 30)          # seconds before each retry of a 429, a 5xx or a network error
QUEUE_LIMIT = 30                  # items PUT /api/job/queue takes at most
OPEN_DAYS = 2                     # open-list days whose cards visitors may watch
DECIDED_BY = "person (review page)"
DECISIONS = ("step", "not a step", "card approved")
WITHDRAWN = "withdrawn"           # a decision the owner took back: removed from the store, then reported
STOPPED = "stopped part way while adding it (the step timed out or crashed)"
MARKETS_FILE = os.path.join(ROOT, "markets.json")
CLOSED_JSON = os.path.join(SITE_DATA_DIR, "closed.json")
OPEN_JSON = os.path.join(SITE_DATA_DIR, "open.json")
NO_API = "No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN): skipped"


class ApiError(Exception):
    """A failed API call: status None for a network error. The message never carries the token."""

    def __init__(self, status, code, message=""):
        super().__init__(f"{code}" + (f" (HTTP {status})" if status else "") + (f": {message}" if message else ""))
        self.status, self.code = status, code


def _env(name):
    """A setting from the environment, else from ../.env (the repo's parent), read the way ai.api_key reads
    the Gemini key. Never logged."""
    v = (os.environ.get(name) or "").strip()
    if v or not os.path.exists(ai.ENV_FILE):
        return v
    with open(ai.ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line.startswith(f"{name}="):
                v = line.split("=", 1)[1].split(" #", 1)[0].strip().strip('"').strip("'")
    return v


class Client:
    """The job's API client: JSON in and out, the job token in one header, TIMEOUT a call, and RETRY_WAITS
    retries of a 429, a 5xx or a network error. request() returns the parsed body (None for 204) or raises
    ApiError."""

    def __init__(self, base, token, session=None, sleep=time.sleep, timeout=TIMEOUT):
        self.base = str(base).rstrip("/")
        self._token = token
        self.session = session or requests.Session()
        self.sleep, self.timeout = sleep, timeout

    def __repr__(self):
        return f"Client({self.base!r})"

    def request(self, method, path, body=None):
        err = None
        for attempt in range(len(RETRY_WAITS) + 1):
            if attempt:
                self.sleep(RETRY_WAITS[attempt - 1])
            try:
                r = self.session.request(method, self.base + path, json=body, timeout=self.timeout,
                                         headers={"Authorization": f"Bearer {self._token}",
                                                  "Accept": "application/json",
                                                  "User-Agent": "rigged-outcomes-job"})
            except requests.RequestException as e:
                err = ApiError(None, "network", type(e).__name__)
                continue
            if r.status_code < 300:
                if r.status_code == 204 or not (r.content or b"").strip():
                    return None
                try:
                    return r.json()
                except ValueError:
                    raise ApiError(r.status_code, "bad_response", "the answer was not JSON")
            try:
                payload = r.json()
                code, message = payload.get("error") or "error", payload.get("message") or ""
            except (ValueError, AttributeError):
                code, message = "error", ""
            err = ApiError(r.status_code, code, message)
            if r.status_code != 429 and r.status_code < 500:
                raise err
        raise err


def client():
    """The Client for WATCHLIST_API_URL and WATCHLIST_JOB_TOKEN, or None when either is unset."""
    url, token = _env("WATCHLIST_API_URL"), _env("WATCHLIST_JOB_TOKEN")
    return Client(url, token) if url and token else None


def _warn(msg):
    log(f"  ! {msg}")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{msg}", flush=True)


def _output(**kv):
    """key=value lines for $GITHUB_OUTPUT (the workflow's step outputs), when it is set."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={str(v).lower() if isinstance(v, bool) else v}\n")


def _read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def _fresh(doc, started_at):
    """A results file written by this run (its "at" at or after --started-at)."""
    return bool(doc) and started_at is not None and int(doc.get("at") or 0) >= int(started_at)


# ── pull, decide, add ────────────────────────────────────────────────────────

def pull(c, out_path, now=None):
    """GET /api/job/work into work.json; returns it. No API, or one that does not answer: an empty work.json."""
    now = int(now if now is not None else now_epoch())
    doc = {"fetched_at": now, "configured": c is not None, "decisions": [], "requests": [], "watched": []}
    if c is None:
        log(NO_API)
    else:
        try:
            got = c.request("GET", "/api/job/work") or {}
            doc.update({k: list(got.get(k) or []) for k in ("decisions", "requests", "watched")})
            log(f"work: {len(doc['decisions'])} decision(s), {len(doc['requests'])} approved request(s), "
                f"{len(doc['watched'])} watched card(s)")
        except ApiError as e:
            doc["error"] = str(e)
            _warn(f"the API did not give the job its work ({e}); decisions and requests wait for the next run")
    _write(out_path, doc)
    _output(decisions=bool(doc["decisions"]), requests=any(r.get("status") == "approved" for r in doc["requests"]))
    return doc


def decide(conn, work, mode, results_path, open_out=OPEN_JSON, rebuild=None, now=None):
    """Save the owner's decisions into the store (applying one twice is harmless: it replaces itself), and take
    the withdrawn ones out of it (only a decision the review page made); in "apply" mode rebuild the reviewed day
    when anything changed. Writes decide_results.json before the rebuild, so decisions saved still count when
    the rebuild fails."""
    now = int(now if now is not None else now_epoch())
    saved, changed, withdrawn = [], False, 0
    for d in work.get("decisions") or []:
        story, key, decision = d.get("story"), d.get("headline_key"), d.get("decision")
        headline = d.get("headline") if isinstance(d.get("headline"), dict) else None
        if not (isinstance(story, str) and story and isinstance(key, str) and key
                and (decision in DECISIONS or decision == WITHDRAWN) and isinstance(d.get("decided_at"), int)):
            _warn(f"a decision the job cannot read was skipped: {str(d)[:120]}")
            continue
        if decision == WITHDRAWN:
            changed |= store.delete_review(conn, story, key, by=DECIDED_BY)
            withdrawn += 1
            saved.append({"story": story, "headline_key": key, "decided_at": d["decided_at"], "withdrawn": True})
            continue
        if (key == "card") != (headline is None):
            _warn(f"a decision without its headline was skipped ({story!r}, {key!r})")
            continue
        store.save_review(conn, story, key, decision, headline, by=DECIDED_BY, at=d["decided_at"])
        changed = True
        saved.append({"story": story, "headline_key": key, "decided_at": d["decided_at"]})
    doc = {"saved": saved, "rebuilt": False, "at": now}
    _write(results_path, doc)
    log(f"decisions: {len(saved) - withdrawn} saved to the store" + (f", {withdrawn} taken back" if withdrawn else ""))
    if mode == "apply" and changed:
        from watchlist import opennow
        summary = (rebuild or opennow.rebuild_reviewed)(conn, out_path=open_out)
        doc["rebuilt"] = summary is not None
        _write(results_path, doc)
    return doc


def add(work, results_path, dry_run=False, adder=None, now=None):
    """Each approved request through add_market.add. Returns the results; sets markets=true when one was added
    and changed the files (the workflow then commits them to main)."""
    now = int(now if now is not None else now_epoch())
    if adder is None:
        import add_market
        adder = add_market.add
    results = []

    def record(running=None):
        # the request being added is on record as failed until it finishes, so a step killed part way reports it
        if not dry_run:
            stopped = [{"condition_id": running["condition_id"], "status": "failed", "name": running.get("name"),
                        "note": STOPPED, "changed": False}] if running else []
            _write(results_path, {"results": results + stopped, "at": now})

    for r in work.get("requests") or []:
        if r.get("status") != "approved":
            continue
        record(r)
        try:
            got = adder(r["condition_id"], r["slug"], r["name"], outcome=r["winner"], dry_run=dry_run)
        except Exception as e:                       # one request never stops the others
            got = {"status": "failed", "name": r.get("name"), "note": f"unexpected error ({type(e).__name__})",
                   "changed": False}
        results.append({"condition_id": r["condition_id"], "status": got["status"], "name": got.get("name"),
                        "note": got.get("note"), "changed": bool(got.get("changed"))})
        log(f"add {r.get('name')}: {got['status']}" + (f" ({got.get('note')})" if got.get("note") else ""))
    record()
    _output(markets=any(x["status"] == "added" and x["changed"] for x in results) and not dry_run)
    return results


# ── catalog, queue, done ─────────────────────────────────────────────────────

def catalog_payload(markets_path=MARKETS_FILE, closed_path=CLOSED_JSON, open_path=OPEN_JSON):
    """What visitors may write to; a section whose file is missing is left out (the API keeps the last one)."""
    out = {}
    markets = _read(markets_path)
    if isinstance(markets, list):
        out["markets"] = [{"name": m["name"], "slug": m["slug"]} for m in markets if m.get("name") and m.get("slug")]
    closed = _read(closed_path)
    if isinstance(closed, dict):
        seen, rows = set(), []
        for d in closed.get("days") or []:                      # newest day first
            for p in d.get("picks") or []:
                cid = p.get("condition_id")
                if not cid or not p.get("slug") or cid in seen:
                    continue
                seen.add(cid)
                rows.append({"condition_id": cid, "slug": p["slug"], "question": p.get("question") or "",
                             "url": p.get("url") or "", "winner": p.get("winner") or "",
                             "closed": p.get("closed") or "", "day": d.get("day")})
        out["closed"] = rows
    opened = _read(open_path)
    if isinstance(opened, dict):
        seen, rows = set(), []
        for d in (opened.get("days") or [])[:OPEN_DAYS]:
            for card in d.get("cards") or []:
                if card.get("id") and card["id"] not in seen:
                    seen.add(card["id"])
                    rows.append({"story": card["id"], "question": card.get("question") or "",
                                 "url": (card.get("polymarket") or {}).get("url") or ""})
        out["open"] = rows
    return out


def queue_payload(conn):
    """The latest stored day's queue for PUT /api/job/queue, each candidate with its key, or None (no day)."""
    from watchlist import opennow
    day = opennow.latest_day(conn)
    if not day:
        return None
    info = day.get("info") or {}
    items = []
    for it in (info.get("awaiting") or [])[:QUEUE_LIMIT]:
        if not it.get("story") or len(str(it["story"])) > 200:      # the API's limit; story keys run to about 90
            _warn(f"a queue item the API cannot take was left out: {str(it.get('story'))[:80]!r}")
            continue
        cands = [dict(h, key=opennow.headline_key(h)) for h in it.get("candidates") or []]
        items.append(dict(it, candidates=[h for h in cands if 0 < len(h["key"]) <= 500]))
    return {"day": day["day"], "run_at": opennow._iso_z(day["run_at"]),
            "steps_by": info.get("steps_by") or opennow.STEPS_BY, "items": items}


def run_summary(conn, closed_path=CLOSED_JSON, started_at=None, applied=0, added=0, failed=0, withdrawn=0):
    """One line for the run's report on the review page."""
    from watchlist import opennow
    parts = []
    day = opennow.latest_day(conn)
    if day:
        info = day.get("info") or {}
        waiting = len(info.get("awaiting") or [])
        parts.append(f"open list {day['day']}: {day['status']}, {len(day['cards'])} card(s), "
                     f"{int(info.get('new_today') or 0)} new, {waiting} waiting for review")
    closed = _read(closed_path) or {}
    gen = closed.get("generated_at")
    parts.append("closed list: " + ("updated" if gen and started_at and (to_epoch(gen) or 0) >= int(started_at)
                                    else f"as of {gen[:16].replace('T', ' ')} UTC" if gen else "none yet"))
    if applied:
        parts.append(f"{applied} decision(s) applied")
    if withdrawn:
        parts.append(f"{withdrawn} taken back")
    if added or failed:
        parts.append(f"{added} market(s) added" + (f", {failed} failed" if failed else ""))
    return "; ".join(parts)


def done_payload(conn, job_dir, mode, status, started_at, run_url=None, now=None, closed_path=CLOSED_JSON):
    """POST /api/job/done's body from this run's files: the decisions saved, only when this run pushed the
    state (state_pushed.json); the requests added, only when main was pushed (markets_pushed.ok) or the market
    was already there, and the failed ones; the run."""
    now = int(now if now is not None else now_epoch())
    pushed = _read(os.path.join(job_dir, "state_pushed.json"))
    decided = _read(os.path.join(job_dir, "decide_results.json"))
    applied = list(decided["saved"]) if _fresh(decided, started_at) and _fresh(pushed, started_at) and \
        pushed.get("ok") else []
    ok_path = os.path.join(job_dir, "markets_pushed.ok")
    main_pushed = os.path.exists(ok_path) and os.path.getmtime(ok_path) >= int(started_at or 0)
    added = _read(os.path.join(job_dir, "add_results.json"))
    results = added.get("results") or [] if _fresh(added, started_at) else []
    reqs = []
    for r in results:
        if r["status"] == "failed" or (r["status"] == "added" and (main_pushed or not r.get("changed"))):
            reqs.append({"condition_id": r["condition_id"], "status": r["status"], "name": r.get("name"),
                         "note": r.get("note")})
    summary = run_summary(conn, closed_path, started_at, applied=sum(1 for a in applied if not a.get("withdrawn")),
                          withdrawn=sum(1 for a in applied if a.get("withdrawn")),
                          added=sum(1 for r in reqs if r["status"] == "added"),
                          failed=sum(1 for r in reqs if r["status"] == "failed"))
    return {"applied": applied, "requests": reqs,
            "run": {"mode": mode, "status": status, "started_at": int(started_at or now), "finished_at": now,
                    "summary": summary, "url": run_url}}


def catalog(c, **paths):
    if c is None:
        log(NO_API)
        return None
    body = catalog_payload(**paths)
    try:
        got = c.request("PUT", "/api/job/catalog", body)
        log(f"catalog: {got}")
        return got
    except ApiError as e:
        _warn(f"the catalog was not sent ({e}); visitors' controls keep the last one")
        return None


def push(c, conn, job_dir, mode, status, started_at, run_url=None, now=None, closed_path=CLOSED_JSON,
         open_path=OPEN_JSON, markets_path=MARKETS_FILE):
    """The queue, the catalog, then the run's report. Each call that fails is warned about; none fails the step."""
    if c is None:
        log(NO_API)
        return None
    q = queue_payload(conn)
    if q is not None:
        try:
            c.request("PUT", "/api/job/queue", q)
            log(f"queue: {len(q['items'])} item(s) of {q['day']} sent")
        except ApiError as e:
            _warn(f"the queue was not sent ({e}); the review page keeps the last one")
    catalog(c, markets_path=markets_path, closed_path=closed_path, open_path=open_path)
    body = done_payload(conn, job_dir, mode, status, started_at, run_url, now, closed_path)
    try:
        c.request("POST", "/api/job/done", body)
        log(f"reported: {len(body['applied'])} decision(s) applied, {len(body['requests'])} request(s); "
            f"{body['run']['summary']}")
    except ApiError as e:
        _warn(f"the run's report was not sent ({e}); the decisions stay pending and are applied again next run "
              f"(harmless)")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        reported = ", ".join(f"{r['name']} {r['status']}" for r in body["requests"]) or "none"
        with open(path, "a") as f:
            f.write(f"### Watchlist job ({mode}, {status})\n\n{body['run']['summary']}.\n\n"
                    f"Decisions applied: {len(body['applied'])}. Requests reported: {reported}.\n")
    return body


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="The daily job's side of the site's API.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    work_path = os.path.join(JOB_DIR, "work.json")
    p = sub.add_parser("pull")
    p.add_argument("--out", default=work_path)
    p = sub.add_parser("decide")
    p.add_argument("--mode", choices=("full", "apply", "local"), required=True)
    p.add_argument("--work", default=work_path)
    p.add_argument("--open-out", default=OPEN_JSON, help="where an apply-mode rebuild writes open.json")
    p.add_argument("--db", default=None)
    p = sub.add_parser("add")
    p.add_argument("--work", default=work_path)
    p.add_argument("--dry-run", action="store_true", help="check each request on Polymarket only")
    p = sub.add_parser("catalog")
    p.add_argument("--closed", default=CLOSED_JSON)
    p.add_argument("--open", default=OPEN_JSON)
    p = sub.add_parser("push")
    p.add_argument("--mode", choices=("full", "apply", "local"), required=True)
    p.add_argument("--status", choices=("success", "partial", "failure"), required=True)
    p.add_argument("--started-at", type=int, required=True)
    p.add_argument("--run-url", default=None)
    p.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "pull":
        pull(client(), args.out)
    elif args.cmd == "decide":
        work = _read(args.work, {}) or {}
        conn = store.connect(args.db) if args.db else store.default_conn()
        decide(conn, work, args.mode, os.path.join(JOB_DIR, "decide_results.json"), open_out=args.open_out)
    elif args.cmd == "add":
        work = _read(args.work, {}) or {}
        add(work, os.path.join(JOB_DIR, "add_results.json"), dry_run=args.dry_run)
    elif args.cmd == "catalog":
        catalog(client(), closed_path=args.closed, open_path=args.open)
    else:
        conn = store.connect(args.db) if args.db else store.default_conn()
        push(client(), conn, JOB_DIR, args.mode, args.status, args.started_at, args.run_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
