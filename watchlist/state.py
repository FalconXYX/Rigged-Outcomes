"""
The daily job's memory between runs: the store tables the two lists read
and write, saved in the site's database (the API's D1 database, through its
job endpoints: watchlist.blobs). A GitHub Actions runner starts empty every
day, and both lists depend on what earlier runs stored: the closed list's
checks and AI labels (a market is never asked about twice), the open list's
99c check at an event's first entry (it must never run again: a later jump
to 99c may be the very case the list is for), its filings, readings, days
and cards, and a person's decisions. Nothing else in the store is kept
(trade CSVs, wallet look-ups, the earlier scans' results): no list reads it.
The published lists and the news searches cache are saved beside it, and
nothing of the job's is kept in git.

In the database, as blobs (watchlist.blobs):

  state        {"format": 1, "files": {"manifest.json": text, "<table>.jsonl": text, ...}}:
               the manifest says when and by whom it was made, the newest
               stamps (what the pull guard compares), what the latest
               open-list day left unread (the catch-up run's check) and
               each table's row count and sha256; a table's file is one
               JSON object per row, keys sorted, rows in primary key order
  list:closed, list:open   closed.json and open.json, byte for byte (the
               site's deploy fetches them: watchlist.publish)
  cache:news   the news searches cache, which only saves a same-day rebuild
               its searches (best effort: a pull or push never fails over it)

    python3 -m watchlist.state dump DIR | load DIR [--allow-empty]
    python3 -m watchlist.state pull [--force] [--site-dir DIR] [--news-cache PATH]
    python3 -m watchlist.state push [--seed | --onto-head] [--by github|mac|local] [--run-url URL]
                                    [--site-dir DIR] [--news-cache PATH]
    python3 -m watchlist.state prune [--dry-run]
    python3 -m watchlist.state migrate [--ref origin/watchlist-data | --dir DIR] [--force [--replace-run-state]]

Every command takes --db PATH (default WATCHLIST_DB, else
data/watchlist/watchlist.db). The API is WATCHLIST_API_URL with
WATCHLIST_JOB_TOKEN (watchlist.sync), tried again for about 14 minutes
(PATIENT_WAITS) when it does not answer, so a short outage never costs a run
its work; --site-dir defaults to web/public/data and --news-cache to
opennow's. The database keeps the state's and the lists' last 3 replaced
versions: python3 -m watchlist.blobs versions state / restore state VERSION
undoes a bad save.

pull downloads everything before it changes anything, and refuses (exit 2)
with no API, an API that does not answer or refuses the token (a runner
that could not read the saved state must never start from nothing: it would
record every first-entry 99c check again at today's prices), files that do
not match their manifest, and a local store holding anything newer than the
saved state (an open-list day, a decision or a closed check made since):
--force replaces the local copy, after backing it up. A fresh database (no
blob at all but the added markets) is no refusal: the run starts from the
local store as it is (empty on a runner), with a warning, and its push saves
the first state. One that holds what is saved only after a state (a list,
the news cache, a replaced state version) but no state is refused as the
setup's problem: the state was lost, or API_URL points at another database.
push refuses (exit 3) when the saved state changed since this
run's pull, so two writers (the Mac and GitHub) never overwrite each other,
and saves nothing when no table changed; a push that went through (or found
nothing to change) counts as this place's new pull, so pushing again later
from here works. --seed saves the first state (there must be none yet);
--onto-head saves the local store over whatever is saved now. A list is sent
only when it changed. Each refusal says whether it is the setup's (a person
must fix it) or an outside one (the API down, another run saved first),
for the run's report (watchlist.problems).

migrate is the one-off move from the old watchlist-data git branch: it reads
the branch's files with git show (it never fetches, commits or pushes), or a
folder holding state/ and site/ (--dir), checks them against their manifest
and saves them byte for byte. It changes nothing locally. Over a state the
database holds it needs --force, and over one a run saved (not an earlier
migrate) --replace-run-state as well.

prune removes only what no list reads again: readings over 60 days old
(the longest look back is the open list's 22-day step history; sooner, but
never inside 23 days, if the file would grow past ANSWERS_MAX_BYTES), closed
checks over 45 days old, and the bulky notes (the queue, the seen step
headlines, the held-back stories) of open-list days over 16 days old, except
the latest updated day's, which the next run reads.
"""
import argparse
import glob
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

from watchlist import blobs, kinds as K, problems, store, sync
from watchlist.common import DAY, DB_PATH, JOB_DIR, ROOT, SITE_DATA_DIR, day_str, iso, log, now_epoch

FORMAT = 1
STATE, NEWS = "state", "cache:news"                     # blob names (the lists are list:closed, list:open)
MIGRATE_REF = "origin/watchlist-data"
# The tables the lists read and write, with their primary keys (the rows' order in a dump)
TABLES = {
    "ai_labels": ("market_id", "rules_sha1"),
    "closed_checks": ("condition_id", "version"),
    "open_pool": ("event_id",),
    "open_filings": ("event_id",),
    "open_answers": ("story", "headlines_sha1", "version", "run"),
    "open_runs": ("day",),
    "open_cards": ("day", "story"),
    "open_listed": ("ident", "day", "story"),
    "open_reviews": ("story", "headline_key"),
}
# The newest stamps: a pull never replaces a store holding something newer than these (without --force)
LATEST = {"open_runs.run_at": ("open_runs", "run_at"),
          "open_reviews.decided_at": ("open_reviews", "decided_at"),
          "closed_checks.created_at": ("closed_checks", "created_at")}
SITE_FILES = ("closed.json", "open.json")
BACKUPS_KEPT = 3                  # local store copies a pull keeps (watchlist.db.bak-<UTC stamp>)
ANSWERS_DAYS = 60                 # readings kept this long (the open list reads back 22 days at most)
ANSWERS_FLOOR_DAYS = K.LIVE_DAYS + 2
ANSWERS_MAX_BYTES = 30_000_000    # past this, readings go sooner (never inside ANSWERS_FLOOR_DAYS)
CHECKS_DAYS = 45                  # closed checks kept this long (a market closes once; the job recomputes 2 days)
RUN_NOTES_DAYS = 16               # open-list days older than this lose their bulky notes
BULKY_INFO = ("awaiting", "seen_step_headlines", "held_back_stories")
PATIENT_WAITS = (2, 8, 30, 60, 120, 300, 300)  # seconds before each retry of a call here (sync.Client): ~14 min
NO_API = "No API configured (WATCHLIST_API_URL, WATCHLIST_JOB_TOKEN): the job's memory is in the database"
MOVED = "the saved state changed since this run's pull (another run saved first); nothing was saved"


class StateError(Exception):
    """A refusal with the exit code the CLI gives (2: nothing usable was pulled, 3: nothing was saved) and its
    kind (watchlist.problems): CONFIG (a person must fix it) unless it is an outside one."""

    def __init__(self, code, message, kind=problems.CONFIG):
        super().__init__(message)
        self.code, self.kind = code, kind


def _api_kind(e):
    """The kind of a refusal caused by an ApiError: outside when the API is down, else the setup's (a refused
    token) or the code's (a request the API cannot read)."""
    return problems.OUTSIDE if e.outage else problems.CONFIG if e.refused else problems.CRASH


# ── files: dump, verify, load ───────────────────────────────────────────────

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def table_bytes(conn, table):
    """(bytes, rows) of one table as its JSONL file: keys sorted, rows in primary key order."""
    order = ", ".join(TABLES[table])
    with store.LOCK:
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    lines = [json.dumps(dict(zip(cols, r)), sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
             for r in rows]
    return "".join(lines).encode("utf-8"), len(rows)


def latest_stamps(conn):
    """{stamp name: the newest value in the store, or None}."""
    out = {}
    with store.LOCK:
        for name, (table, col) in LATEST.items():
            out[name] = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
    return out


def _site_meta(site_dir):
    """What the manifest says about the published lists: the open list's newest day and run, the closed list's
    generated_at."""
    out = {"open_day": None, "open_run_at": None, "closed_generated_at": None}
    try:
        with open(os.path.join(site_dir, "open.json")) as f:
            days = json.load(f).get("days") or []
        if days:
            out["open_day"], out["open_run_at"] = days[0].get("day"), days[0].get("run_at")
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(os.path.join(site_dir, "closed.json")) as f:
            out["closed_generated_at"] = json.load(f).get("generated_at")
    except (OSError, ValueError, AttributeError):
        pass
    return out


def open_left(conn):
    """{"day", "unread"}: the latest open-list day and what its runs left unread (opennow.left_unread), or None.
    The catch-up run's check reads it from the saved state's head, without downloading the state."""
    from watchlist import opennow
    day = opennow.latest_day(conn)
    return {"day": day["day"], "unread": opennow.left_unread(day)} if day else None


def dump_files(conn, by="local", run_url=None, made_at=None, site_dir=SITE_DATA_DIR):
    """{"manifest.json": bytes, "<table>.jsonl": bytes, ...}: the state as its files."""
    files, tables = {}, {}
    for t in TABLES:
        data, n = table_bytes(conn, t)
        files[f"{t}.jsonl"] = data
        tables[t] = {"rows": n, "sha256": _sha256(data)}
    manifest = dict({"format": FORMAT, "made_at": iso(made_at if made_at is not None else now_epoch()),
                     "by": by, "run_url": run_url}, **_site_meta(site_dir))
    manifest["latest"] = latest_stamps(conn)
    manifest["open_left"] = open_left(conn)
    manifest["tables"] = tables
    files["manifest.json"] = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")
    return files


def _write(path, data):
    """Write bytes through a temporary file, then rename (a stall never leaves half a file)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def dump(conn, out_dir, **kw):
    """Write the state files to out_dir; returns the manifest."""
    files = dump_files(conn, **kw)
    for name, data in files.items():
        _write(os.path.join(out_dir, name), data)
    return json.loads(files["manifest.json"])


def verify(files):
    """The manifest of a state ({file name: bytes}) once every table file matches its row count and sha256;
    raises StateError(2) otherwise."""
    try:
        manifest = json.loads(files["manifest.json"])
    except (KeyError, ValueError) as e:
        raise StateError(2, f"the saved state has no readable manifest ({type(e).__name__})")
    if manifest.get("format") != FORMAT:
        raise StateError(2, f"the saved state is format {manifest.get('format')!r}; this code reads format {FORMAT}")
    for t in TABLES:
        want = (manifest.get("tables") or {}).get(t)
        data = files.get(f"{t}.jsonl")
        if want is None or data is None:
            raise StateError(2, f"the saved state has no {t} table")
        if _sha256(data) != want.get("sha256"):
            raise StateError(2, f"state/{t}.jsonl does not match the manifest's sha256")
        if data.count(b"\n") != want.get("rows"):
            raise StateError(2, f"state/{t}.jsonl holds {data.count(chr(10).encode())} rows, the manifest says "
                                f"{want.get('rows')}")
    return manifest


def load_files(conn, files, allow_empty=False):
    """Replace the store's state tables with the verified files, in one transaction (DELETE, then INSERT, per
    table). Columns are matched by name; one the store lacks is warned about and left out. Refuses a state with
    no row at all unless allow_empty. Returns {table: rows}."""
    manifest = verify(files)
    counts = {t: int(manifest["tables"][t]["rows"]) for t in TABLES}
    if not allow_empty and not any(counts.values()):
        raise StateError(2, "the saved state is empty; load it anyway with --allow-empty")
    with store.transaction(conn):
        for t in TABLES:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            rows = [json.loads(line) for line in files[f"{t}.jsonl"].decode("utf-8").splitlines() if line]
            cols = sorted(set().union(*[r.keys() for r in rows])) if rows else []
            unknown = [c for c in cols if c not in have]
            if unknown:
                log(f"  ! {t}: column(s) {', '.join(unknown)} not in this store; left out")
            cols = [c for c in cols if c in have]
            conn.execute(f"DELETE FROM {t}")
            if rows and cols:
                conn.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 [tuple(r.get(c) for c in cols) for r in rows])
    return counts


def load(conn, in_dir, allow_empty=False):
    files = {}
    for name in ["manifest.json"] + [f"{t}.jsonl" for t in TABLES]:
        p = os.path.join(in_dir, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                files[name] = f.read()
    return load_files(conn, files, allow_empty=allow_empty)


# ── prune ────────────────────────────────────────────────────────────────────

def prune(conn, now=None, dry_run=False, answers_days=ANSWERS_DAYS, max_answer_bytes=ANSWERS_MAX_BYTES):
    """Remove what no list reads again (the module docstring); returns what it removed, by table."""
    now = int(now if now is not None else now_epoch())
    out = {}
    with store.LOCK:
        cutoff = now - int(answers_days) * DAY
        floor = now - ANSWERS_FLOOR_DAYS * DAY
        while cutoff < floor and (conn.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM open_answers "
                                               "WHERE created_at >= ?", (cutoff,)).fetchone()[0] > max_answer_bytes):
            cutoff += DAY
        out["open_answers"] = conn.execute("SELECT COUNT(*) FROM open_answers WHERE created_at < ?",
                                           (cutoff,)).fetchone()[0]
        checks_cut = now - CHECKS_DAYS * DAY
        out["closed_checks"] = conn.execute("SELECT COUNT(*) FROM closed_checks WHERE created_at < ?",
                                            (checks_cut,)).fetchone()[0]
        keep = conn.execute("SELECT MAX(day) FROM open_runs WHERE status = 'updated'").fetchone()[0]
        old_day = day_str(now - RUN_NOTES_DAYS * DAY, is_day=False)
        notes = []
        for day, info in conn.execute("SELECT day, info FROM open_runs WHERE day < ?", (old_day,)).fetchall():
            if day == keep:
                continue
            d = json.loads(info or "{}")
            if any(k in d for k in BULKY_INFO):
                notes.append((json.dumps({k: v for k, v in d.items() if k not in BULKY_INFO},
                                         separators=(",", ":"), default=str), day))
        out["open_runs notes"] = len(notes)
    if dry_run or not any(out.values()):
        return out
    with store.transaction(conn):
        conn.execute("DELETE FROM open_answers WHERE created_at < ?", (cutoff,))
        conn.execute("DELETE FROM closed_checks WHERE created_at < ?", (checks_cut,))
        conn.executemany("UPDATE open_runs SET info = ? WHERE day = ?", notes)
    return out


# ── local store ──────────────────────────────────────────────────────────────

def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _non_empty(conn):
    with store.LOCK:
        return any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in TABLES)


def _backup_db(conn, db_path, now):
    """Copy the local store (sqlite's backup, safe while it is open) to <db>.bak-<UTC stamp>; keep the newest
    BACKUPS_KEPT copies."""
    stamp = iso(now)[:19].replace("-", "").replace(":", "").replace("T", "-")
    path = f"{db_path}.bak-{stamp}"
    dst = sqlite3.connect(path)
    try:
        with store.LOCK:
            conn.backup(dst)
    finally:
        dst.close()
    for old in sorted(glob.glob(f"{db_path}.bak-*"))[:-BACKUPS_KEPT]:
        os.remove(old)
    return path




# ── pull and push ────────────────────────────────────────────────────────────

def _list_name(file_name):
    """"closed.json" -> "list:closed"."""
    return "list:" + file_name.rsplit(".", 1)[0]


def _doc_files(doc):
    """{file name: bytes} of a saved state document; raises StateError(2) when it is not one."""
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
        raise StateError(2, "the saved state is not a state document")
    if doc.get("format") != FORMAT:
        raise StateError(2, f"the saved state is format {doc.get('format')!r}; this code reads format {FORMAT}")
    return {k: v.encode("utf-8") for k, v in files.items()}


def _doc(files):
    """The state document for {file name: bytes}."""
    try:
        return {"format": FORMAT, "files": {k: v.decode("utf-8") for k, v in files.items()}}
    except UnicodeDecodeError as e:
        raise StateError(2, f"a state file is not UTF-8 ({e})")


def _pulled(job_dir, version, made_at, tables, now):
    """This place's pull: the saved version it holds, or (version None) a fresh start, whose push saves the first
    state."""
    doc = {"version": version, "made_at": made_at, "tables": tables, "at": now}
    if version is None:
        doc["fresh"] = True
    _write(os.path.join(job_dir, "state_pulled.json"), json.dumps(doc).encode())


def _pull_news(c, path):
    """The saved news cache into path, when the local file is missing or older than it. Never fails."""
    try:
        h = blobs.head(c, NEWS)
        if h is None:
            return False
        if os.path.exists(path) and os.path.getmtime(path) >= int(h["updated_at"]):
            log("  news cache: the local one is as new; kept")
            return False
        h, data = blobs.get(c, NEWS, h)
        if h is None:
            return False
        _write(path, data)
        log(f"  news cache pulled ({len(data) / 1e6:.1f} MB)")
        return True
    except (sync.ApiError, blobs.BlobError, OSError, KeyError, TypeError, ValueError) as e:
        problems.note(f"the news cache was not pulled ({e}); the open list searches the news again")
        return False


def pull(conn, c, db_path=DB_PATH, force=False, site_dir=SITE_DATA_DIR, job_dir=JOB_DIR, news_cache=None, now=None):
    """Load the saved state into the store and the published lists into site_dir (the closed list merges its
    older days from closed.json); news_cache: the news searches cache's path, or None to leave it. c: the API
    (sync.Client). Returns the manifest."""
    now = int(now if now is not None else now_epoch())
    if c is None:
        raise StateError(2, NO_API)
    try:
        h, doc = blobs.get_json(c, STATE)
        lists = {}
        for name in SITE_FILES:
            got, data = blobs.get(c, _list_name(name))
            if got is not None:
                lists[name] = data
    except sync.ApiError as e:
        raise StateError(2, f"the saved state could not be downloaded ({e}); nothing was loaded", _api_kind(e))
    except blobs.BlobError as e:
        raise StateError(2, f"the saved state could not be downloaded ({e}); nothing was loaded. An earlier save "
                            f"can be made live again: python3 -m watchlist.blobs versions state")
    if h is None:
        _never_saved(c)
        return _start_fresh(conn, c, lists, site_dir, job_dir, news_cache, now)
    files = _doc_files(doc)
    manifest = verify(files)
    mine, theirs = latest_stamps(conn), manifest.get("latest") or {}
    newer = [k for k, v in mine.items() if v is not None and (theirs.get(k) is None or v > theirs[k])]
    if newer and not force:
        raise StateError(2, f"the local store has newer data than the saved state ({', '.join(newer)}): nothing was "
                            f"loaded. Push it first, or replace it with --force (it is backed up first)")
    if _non_empty(conn) and db_path not in (None, ":memory:") and os.path.exists(db_path):
        log(f"  local store backed up to {_backup_db(conn, db_path, now)}")
    counts = load_files(conn, files)
    for name, data in lists.items():
        _write(os.path.join(site_dir, name), data)
    if news_cache:
        _pull_news(c, news_cache)
    _pulled(job_dir, h["version"], manifest.get("made_at"),
            {t: manifest["tables"][t]["sha256"] for t in TABLES}, now)
    log(f"state pulled from the database, version {h['version'][:8]} (made {manifest.get('made_at')} by "
        f"{manifest.get('by')}): {sum(counts.values())} rows in {len(counts)} tables; lists: "
        f"{', '.join(lists) or 'none saved'}")
    return manifest


def _never_saved(c):
    """With no state in the database: raises StateError(2, CONFIG) unless the database is fresh. A list, the news
    cache and a replaced state version are saved only after a state was (push, migrate), and the API never
    deletes a state: holding one of them without a state means the state was lost or API_URL points at another
    database, and starting from nothing would record every first-entry 99c check again at today's prices. The
    added markets do not count: a first run whose save failed may still add one."""
    try:
        held = sorted(str(h.get("name")) for h in blobs.heads(c, "")
                      if not str(h.get("name")).startswith(sync.ADDED_PREFIX))
        try:
            kept = blobs.versions(c, STATE)
        except sync.ApiError as e:
            if e.status != 404:
                raise
            kept = []                                       # a Worker without kept versions
    except sync.ApiError as e:
        raise StateError(2, f"the saved state could not be downloaded ({e}); nothing was loaded", _api_kind(e))
    if held or kept:
        what = ", ".join(held + ([f"{len(kept)} replaced state version(s)"] if kept else []))
        raise StateError(2, f"the database holds {what} but no saved state: it was lost, or API_URL points at "
                            f"another database. Nothing was loaded. Make a kept state live again (python3 -m "
                            f"watchlist.blobs versions state, then restore state VERSION), or save the Mac's (python3 "
                            f"-m watchlist.state push --seed)")


def _start_fresh(conn, c, lists, site_dir, job_dir, news_cache, now):
    """pull with no saved state in the database: the local store stays as it is (empty on a runner) and its push
    saves the first state; the lists the database holds are written. Returns {"fresh": True}."""
    problems.note("No saved state in the database: this run starts from "
                  + ("this store as it is" if _non_empty(conn) else "an empty memory")
                  + ", and its save becomes the first state")
    for name, data in lists.items():
        _write(os.path.join(site_dir, name), data)
    if news_cache:
        _pull_news(c, news_cache)
    _pulled(job_dir, None, None, {t: _sha256(table_bytes(conn, t)[0]) for t in TABLES}, now)
    log(f"state: a fresh start; lists: {', '.join(lists) or 'none saved'}")
    return {"fresh": True}


def _push_lists(c, by, run_url, site_dir, now):
    """Send each published list in site_dir that differs from the saved one; returns the file names sent."""
    present = {}
    for name in SITE_FILES:
        p = os.path.join(site_dir, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                present[name] = f.read()
    sent = []
    if not present:
        return sent
    try:
        have = {h.get("name"): h for h in blobs.heads(c, "list:")}
        for name, data in present.items():
            if (have.get(_list_name(name)) or {}).get("sha256") == blobs.sha256(data):
                continue
            blobs.put(c, _list_name(name), data, {"by": by, "made_at": iso(now), "run_url": run_url})
            sent.append(name)
    except sync.ApiError as e:
        raise StateError(3, f"the state was saved but the lists were not: {e}", _api_kind(e))
    except blobs.BlobError as e:
        raise StateError(3, f"the state was saved but the lists were not: {e}")
    log(f"lists: {', '.join(sent) + ' saved' if sent else 'unchanged'}")
    return sent


def _push_news(c, path, by, now):
    """Send the news cache when it differs from the saved one. Never fails."""
    try:
        if not os.path.exists(path):
            return False
        with open(path, "rb") as f:
            data = f.read()
        h = blobs.head(c, NEWS)
        if h is not None and h.get("sha256") == blobs.sha256(data):
            return False
        got = blobs.put(c, NEWS, data, {"by": by, "made_at": iso(now)})
        log(f"  news cache saved ({len(data) / 1e6:.1f} MB, {got['parts']} part(s))")
        return True
    except (sync.ApiError, blobs.BlobError, OSError, KeyError, TypeError) as e:
        problems.note(f"the news cache was not saved ({e}); the next run searches the news again")
        return False


def push(conn, c, seed=False, onto_head=False, by="github", run_url=None, site_dir=SITE_DATA_DIR, job_dir=JOB_DIR,
         news_cache=None, now=None):
    """Prune, then save the state (only when a table changed since this run's pull), the published lists that
    changed and the news cache. seed: the first state (there must be none); onto_head: over whatever is saved,
    not this run's pull. Returns {"version", "changed", "lists", "news"}."""
    now = int(now if now is not None else now_epoch())
    if c is None:
        raise StateError(3, f"{NO_API}; nothing was saved")
    removed = prune(conn, now)
    if any(removed.values()):
        log(f"  pruned: {', '.join(f'{v} {k}' for k, v in removed.items() if v)}")
    files = dump_files(conn, by=by, run_url=run_url, made_at=now, site_dir=site_dir)
    manifest = json.loads(files["manifest.json"])
    shas = {t: manifest["tables"][t]["sha256"] for t in TABLES}
    try:
        cur = blobs.head(c, STATE)
    except sync.ApiError as e:
        raise StateError(3, f"the database did not answer ({e}); nothing was saved", _api_kind(e))
    pulled = None
    if seed:
        if cur is not None:
            meta = cur.get("meta") or {}
            raise StateError(3, f"the database already holds a state (made {meta.get('made_at')} by {meta.get('by')}): "
                                f"pull it rather than seeding over it; nothing was saved")
        expect = None
    elif onto_head:
        if cur is None:
            raise StateError(3, "No saved state in the database: seed it with --seed; nothing was saved")
        expect = cur["version"]
        log(f"  over the saved state {expect[:8]} (--onto-head): this store replaces it")
    else:
        pulled = _read_json(os.path.join(job_dir, "state_pulled.json"))
        if not pulled or not (pulled.get("version") or pulled.get("fresh")):
            raise StateError(3, "this run has not pulled the state (state_pulled.json is missing); nothing was saved")
        expect = pulled.get("version")              # None after a fresh start: there must still be no state
        if (cur or {}).get("version") != expect:
            raise StateError(3, MOVED, problems.OUTSIDE)
    if pulled is not None and pulled.get("tables") == shas:
        version, changed, made_at = expect, False, pulled.get("made_at")
        log(f"state unchanged since {'the fresh start' if expect is None else 'version ' + expect[:8]}: nothing to "
            f"save")
    else:
        try:
            got = blobs.put_json(c, STATE, _doc(files), manifest, expect=expect)
        except sync.ApiError as e:
            if e.code == "moved":
                raise StateError(3, MOVED, problems.OUTSIDE)
            raise StateError(3, f"the state was not saved ({e}); nothing was saved", _api_kind(e))
        except blobs.BlobError as e:
            raise StateError(3, f"{e}; nothing was saved")
        version, changed, made_at = got["version"], True, manifest["made_at"]
        log(f"state saved to the database as version {version[:8]} ({got['parts']} part(s), "
            f"{got['bytes'] / 1e6:.1f} MB)" + (" (the first)" if seed or expect is None else ""))
    _write(os.path.join(job_dir, "state_pushed.json"),
           json.dumps({"ok": True, "version": version, "changed": changed, "at": now}).encode())
    _pulled(job_dir, version, made_at, shas, now)
    sent = _push_lists(c, by, run_url, site_dir, now)
    news = _push_news(c, news_cache, by, now) if news_cache else False
    return {"version": version, "changed": changed, "lists": sent, "news": news}


# ── migrate: the one-off move from the watchlist-data branch ────────────────

def _git_reader(ref):
    """(read(path) -> bytes | None, "<ref> <sha>") for a commit of this checkout: git show only."""
    def git(*args):
        return subprocess.run(["git", "-C", ROOT, *args], capture_output=True)

    r = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if r.returncode != 0:
        raise StateError(2, f"git knows no {ref} here: fetch it first (git fetch origin watchlist-data)")
    sha = r.stdout.decode().strip()

    def read(path):
        got = git("show", f"{sha}:{path}")
        return got.stdout if got.returncode == 0 else None
    return read, f"{ref} {sha}"


def _dir_reader(folder):
    def read(path):
        p = os.path.join(folder, path)
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read()
    return read, f"dir {os.path.abspath(folder)}"


def migrate(c, ref=MIGRATE_REF, from_dir=None, force=False, reader=None, replace_run_state=False):
    """Save the branch's state/ and site/ files (or a folder's, or reader(path)'s) in the database as they are.
    Refuses (3) when the database already holds a state, unless force; and when a run saved it (its meta has no
    migrated_from), unless replace_run_state too. Returns the state's new head."""
    if c is None:
        raise StateError(3, f"{NO_API}; nothing was saved")
    if reader is not None:
        read, source = reader, ref
    elif from_dir:
        read, source = _dir_reader(from_dir)
    else:
        read, source = _git_reader(ref)
    files = {}
    for name in ["manifest.json"] + [f"{t}.jsonl" for t in TABLES]:
        data = read(f"state/{name}")
        if data is not None:
            files[name] = data
    manifest = verify(files)
    doc = _doc(files)
    lists = {name: read(f"site/{name}") for name in SITE_FILES}
    try:
        cur = blobs.head(c, STATE)
    except sync.ApiError as e:
        raise StateError(3, f"the database did not answer ({e}); nothing was saved")
    meta = (cur or {}).get("meta") or {}
    if cur is not None and not force:
        raise StateError(3, f"the database already holds a state (made {meta.get('made_at')} by {meta.get('by')}); "
                            f"--force replaces it")
    if cur is not None and not meta.get("migrated_from") and not replace_run_state:
        raise StateError(3, f"the database's state was saved by a run (made {meta.get('made_at')} by "
                            f"{meta.get('by')}), not by migrate; the branch's would replace that run's work. Nothing "
                            f"was saved; --replace-run-state replaces it anyway")
    try:
        got = blobs.put_json(c, STATE, doc, dict(manifest, migrated_from=source),
                             expect=cur["version"] if cur else None)
    except sync.ApiError as e:
        raise StateError(3, ("the saved state changed while migrating" if e.code == "moved" else
                             f"the state was not saved ({e})") + "; nothing was saved")
    except blobs.BlobError as e:
        raise StateError(3, f"{e}; nothing was saved")
    log(f"migrated {source} (made {manifest.get('made_at')} by {manifest.get('by')}) into the database: version "
        f"{got['version'][:8]}, {got['parts']} part(s)")
    for t in TABLES:
        log(f"  {t:<14} {manifest['tables'][t]['rows']:>6} rows")
    try:
        for name, data in lists.items():
            if data is None:
                log(f"  site/{name}: not in {source}; left out")
                continue
            h = blobs.put(c, _list_name(name), data, {"by": manifest.get("by"), "made_at": manifest.get("made_at"),
                                                     "run_url": manifest.get("run_url"), "migrated_from": source})
            log(f"  site/{name} -> {_list_name(name)}: {len(data):,} bytes, {h['parts']} part(s)")
    except (sync.ApiError, blobs.BlobError) as e:
        raise StateError(3, f"the state was migrated but the lists were not ({e}); run migrate --force again")
    return got


# ── CLI ──────────────────────────────────────────────────────────────────────

def _news_path(arg):
    if arg:
        return arg
    from watchlist import opennow
    return opennow.NEWS_CACHE


def main(argv=None):
    ap = argparse.ArgumentParser(description="The daily job's memory, saved in the site's database.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("dump", "load", "pull", "push", "prune", "migrate"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=DB_PATH, help="the sqlite store (default WATCHLIST_DB, else data/watchlist/)")
        if name in ("dump", "load"):
            p.add_argument("dir")
        if name in ("pull", "push"):
            p.add_argument("--site-dir", default=SITE_DATA_DIR, help="where the published lists live")
            p.add_argument("--news-cache", default=None, help="the news searches cache (default: opennow's)")
    sub.choices["load"].add_argument("--allow-empty", action="store_true", help="load a state with no row at all")
    sub.choices["pull"].add_argument("--force", action="store_true",
                                     help="replace a local store that holds newer data (it is backed up first)")
    g = sub.choices["push"].add_mutually_exclusive_group()
    g.add_argument("--seed", action="store_true", help="save the first state (the database must hold none)")
    g.add_argument("--onto-head", action="store_true",
                   help="save this store over whatever is saved now, not over this run's pull")
    sub.choices["push"].add_argument("--by", choices=("github", "mac", "local"), default="local")
    sub.choices["push"].add_argument("--run-url", default=None)
    sub.choices["prune"].add_argument("--dry-run", action="store_true", help="say what would go, remove nothing")
    g = sub.choices["migrate"].add_mutually_exclusive_group()
    g.add_argument("--ref", default=MIGRATE_REF, help="the branch's commit to read with git show")
    g.add_argument("--dir", default=None, help="read state/ and site/ from this folder instead")
    sub.choices["migrate"].add_argument("--force", action="store_true", help="replace a state the database holds")
    sub.choices["migrate"].add_argument("--replace-run-state", action="store_true",
                                        help="with --force: replace it even when a run saved it")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "migrate":
            migrate(sync.client(PATIENT_WAITS), ref=args.ref, from_dir=args.dir, force=args.force,
                    replace_run_state=args.replace_run_state)
            return 0
        conn = store.connect(args.db)
        if args.cmd == "dump":
            m = dump(conn, args.dir)
            log(f"state dumped to {args.dir}: " + ", ".join(f"{t} {v['rows']}" for t, v in m["tables"].items()))
        elif args.cmd == "load":
            counts = load(conn, args.dir, allow_empty=args.allow_empty)
            log(f"state loaded from {args.dir}: {sum(counts.values())} rows")
        elif args.cmd == "prune":
            out = prune(conn, dry_run=args.dry_run)
            log(("would remove: " if args.dry_run else "removed: ") + ", ".join(f"{v} {k}" for k, v in out.items()))
        elif args.cmd == "pull":
            pull(conn, sync.client(PATIENT_WAITS), db_path=args.db, force=args.force, site_dir=args.site_dir,
                 news_cache=_news_path(args.news_cache))
        else:
            got = push(conn, sync.client(PATIENT_WAITS), seed=args.seed, onto_head=args.onto_head, by=args.by,
                       run_url=args.run_url, site_dir=args.site_dir, news_cache=_news_path(args.news_cache))
            sync._output(open_list="open.json" in got["lists"])     # the catch-up run deploys only on this
    except StateError as e:
        print(f"state {args.cmd}: {e}", file=sys.stderr, flush=True)
        problems.note(f"state {args.cmd}: {e}", e.kind, echo=False)       # an annotation, and the run's record
        return e.code
    return 0


# the job's step ids (watchlist.problems): pull restores the state, push saves it
STEP_NAMES = {"pull": "pull", "push": "save"}

if __name__ == "__main__":
    _cmd = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
    sys.exit(problems.guard(STEP_NAMES.get(_cmd, _cmd), main))
