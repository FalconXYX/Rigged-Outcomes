"""
The daily job's memory between runs: the store tables the two lists read
and write, kept as plain files on the repository's `watchlist-data` branch
(docs/DEPLOY.md). A GitHub Actions runner starts empty every day, and both
lists depend on what earlier runs stored: the closed list's checks and AI
labels (a market is never asked about twice), the open list's 99c check at
an event's first entry (it must never run again: a later jump to 99c may be
the very case the list is for), its filings, readings, days and cards, and
a person's decisions. Nothing else in the store is kept (trade CSVs, wallet
look-ups, the earlier scans' results): no list reads it. The news cache,
which only saves a same-day rebuild its searches, goes to the Actions cache.

The branch:

  README.md            what the branch is
  state/manifest.json  the format, when and by whom it was made, the newest
                       stamps (what the pull guard compares) and each
                       table's row count and sha256
  state/<table>.jsonl  one JSON object per row, keys sorted, rows in primary
                       key order, so a day's change is a small diff
  site/closed.json, site/open.json   the published lists (deploy.yml copies
                       them into the site it builds)
  backup/d1.json       the site API's export (GET /api/job/export, never a
                       visitor hash), kept as it was when the API fails

    python3 -m watchlist.state dump DIR | load DIR [--allow-empty]
    python3 -m watchlist.state pull [--remote origin] [--branch watchlist-data] [--rev SHA] [--force]
    python3 -m watchlist.state push [--remote origin] [--branch watchlist-data] [--seed | --onto-head] [--no-push]
                                    [--by github|mac] [--run-url URL]
    python3 -m watchlist.state prune [--dry-run]

Every command takes --db PATH (default WATCHLIST_DB, else
data/watchlist/watchlist.db); pull and push take --repo (the git checkout,
default this one) and --site-dir (default web/public/data).

pull refuses (exit 2) when the branch is missing (a fresh runner must never
start from nothing: it would record every first-entry 99c check again at
today's prices) or its files do not match the manifest, and when the local
store holds anything newer than the branch (an open-list day, a decision or
a closed check made since): --force replaces the local copy, after backing
it up. push refuses (exit 3) when the branch moved since this run's pull,
so two writers (the Mac and GitHub) never overwrite each other; a push that
went through (or found nothing to change) counts as this checkout's new pull,
so pushing again later from here works. --onto-head instead puts the local
store on top of the branch's current head whatever was pulled (the way back
after `pull --rev <older sha> --force`, docs/DEPLOY.md). It is a
plain push, never a merge or a force, and it builds its commit with git's
plumbing (no checkout, nothing left behind in the working tree). The branch
is fetched one commit deep on a shallow checkout (the Actions runner's) and
in full elsewhere, so a full repository never turns shallow.

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
import tempfile

from watchlist import kinds as K, store
from watchlist.common import DAY, DB_PATH, JOB_DIR, ROOT, SITE_DATA_DIR, day_str, iso, log, now_epoch

BRANCH = "watchlist-data"
REMOTE = "origin"
FORMAT = 1
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
BACKUP_PATH = "backup/d1.json"
BACKUPS_KEPT = 3                  # local store copies a pull keeps (watchlist.db.bak-<UTC stamp>)
ANSWERS_DAYS = 60                 # readings kept this long (the open list reads back 22 days at most)
ANSWERS_FLOOR_DAYS = K.LIVE_DAYS + 2
ANSWERS_MAX_BYTES = 30_000_000    # past this, readings go sooner (never inside ANSWERS_FLOOR_DAYS)
CHECKS_DAYS = 45                  # closed checks kept this long (a market closes once; the job recomputes 2 days)
RUN_NOTES_DAYS = 16               # open-list days older than this lose their bulky notes
BULKY_INFO = ("awaiting", "seen_step_headlines", "held_back_stories")
FILE_WARN_BYTES = 45_000_000      # GitHub warns at 50 MB a file and refuses 100 MB
README = """# watchlist-data

Written by the Watchlist job (`.github/workflows/watchlist.yml`, or `scripts/watchlist-mac.sh`):
the job's memory between runs (`state/`, one JSONL file per store table, checked against
`state/manifest.json`), the published lists the site deploy copies in (`site/`), and a backup of
the site API's database without any visitor hash (`backup/d1.json`).

Do not edit by hand: `python3 -m watchlist.state pull` refuses files that do not match the
manifest. See docs/DEPLOY.md on the main branch.
"""


class StateError(Exception):
    """A refusal with the exit code the CLI gives (2: nothing usable to pull, 3: nothing was pushed)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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


def dump_files(conn, by="local", run_url=None, made_at=None, site_dir=SITE_DATA_DIR):
    """{"manifest.json": bytes, "<table>.jsonl": bytes, ...}: the state as its files."""
    files, tables = {}, {}
    for t in TABLES:
        data, n = table_bytes(conn, t)
        files[f"{t}.jsonl"] = data
        tables[t] = {"rows": n, "sha256": _sha256(data)}
        if len(data) > FILE_WARN_BYTES:
            log(f"  ! state/{t}.jsonl is {len(data) / 1e6:.0f} MB: GitHub refuses files over 100 MB; prune harder "
                f"(watchlist/state.py) before it gets there")
    manifest = dict({"format": FORMAT, "made_at": iso(made_at if made_at is not None else now_epoch()),
                     "by": by, "run_url": run_url}, **_site_meta(site_dir))
    manifest["latest"] = latest_stamps(conn)
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


# ── git ──────────────────────────────────────────────────────────────────────

def _git(repo, *args, data=None, check=True, env=None):
    """Run git in repo; stdout as bytes. Errors raise StateError(3) with git's own words (never a token: git
    prints none)."""
    r = subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True,
                       env=dict(os.environ, **(env or {})))
    if check and r.returncode != 0:
        raise StateError(3, f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    return r


def _fetch(repo, remote, target, check=True):
    """Fetch one ref or commit: one commit deep on a shallow checkout (the Actions runner's), in full
    elsewhere, so a full repository (this Mac's) never becomes a shallow one."""
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").stdout.decode().strip() == "true"
    return _git(repo, "fetch", "--no-tags", *(["--depth=1"] if shallow else []), remote, target, check=check)


def remote_head(repo, remote, branch):
    """The branch's commit on the remote, or None when it has no such branch."""
    r = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    line = r.stdout.decode().strip().splitlines()
    return line[0].split()[0] if line else None


def _show(repo, sha, path):
    r = _git(repo, "show", f"{sha}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def _tree_entries(repo, sha):
    """{path: (mode, blob sha)} of every file in a commit."""
    out = {}
    for line in _git(repo, "ls-tree", "-r", "-z", sha).stdout.split(b"\0"):
        if line:
            meta, path = line.split(b"\t", 1)
            mode, _kind, blob = meta.decode().split()
            out[path.decode("utf-8")] = (mode, blob)
    return out


def _mktree(repo, entries):
    """The tree sha of {path: (mode, blob sha)}, built level by level with git mktree."""
    here, below = [], {}
    for path, (mode, blob) in entries.items():
        head, _, rest = path.partition("/")
        if rest:
            below.setdefault(head, {})[rest] = (mode, blob)
        else:
            here.append(f"{mode} blob {blob}\t{head}")
    for name, sub in below.items():
        here.append(f"040000 tree {_mktree(repo, sub)}\t{name}")
    return _git(repo, "mktree", data=("\n".join(sorted(here, key=lambda x: x.split("\t", 1)[1])) + "\n")
                .encode("utf-8")).stdout.decode().strip()


def _identity(repo):
    """Author and committer for the job's commit: the checkout's own git identity, else a fixed one."""
    have = _git(repo, "config", "user.email", check=False).stdout.strip()
    if have:
        return {}
    who = {"NAME": "Rigged Outcomes watchlist job", "EMAIL": "watchlist-job@users.noreply.github.com"}
    return {f"GIT_{role}_{k}": v for role in ("AUTHOR", "COMMITTER") for k, v in who.items()}


# ── pull and push ────────────────────────────────────────────────────────────

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


def pull(conn, db_path=DB_PATH, repo=ROOT, remote=REMOTE, branch=BRANCH, rev=None, force=False,
         site_dir=SITE_DATA_DIR, job_dir=JOB_DIR, now=None):
    """Load the saved state into the store and the published lists into site_dir (the closed list merges its
    older days from closed.json). Returns the manifest."""
    now = int(now if now is not None else now_epoch())
    if rev is None and remote_head(repo, remote, branch) is None:
        raise StateError(2, f"No saved state on {branch}: seed it with `python3 -m watchlist.state push --seed` "
                            f"(docs/DEPLOY.md)")
    target = rev or f"refs/heads/{branch}"
    r = _fetch(repo, remote, target, check=False)
    if r.returncode != 0:
        raise StateError(2, f"could not fetch {target} from {remote}: "
                            f"{r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    sha = _git(repo, "rev-parse", "FETCH_HEAD").stdout.decode().strip()
    files = {}
    for name in ["manifest.json"] + [f"{t}.jsonl" for t in TABLES]:
        data = _show(repo, sha, f"state/{name}")
        if data is not None:
            files[name] = data
    manifest = verify(files)
    mine, theirs = latest_stamps(conn), manifest.get("latest") or {}
    newer = [k for k, v in mine.items() if v is not None and (theirs.get(k) is None or v > theirs[k])]
    if newer and not force:
        raise StateError(2, f"the local store has newer data than {branch} ({', '.join(newer)}): nothing was "
                            f"loaded. Push it first, or replace it with --force (it is backed up first)")
    if _non_empty(conn) and db_path not in (None, ":memory:") and os.path.exists(db_path):
        log(f"  local store backed up to {_backup_db(conn, db_path, now)}")
    counts = load_files(conn, files)
    for name in SITE_FILES:
        data = _show(repo, sha, f"site/{name}")
        if data is not None:
            _write(os.path.join(site_dir, name), data)
    _write(os.path.join(job_dir, "state_pulled.json"),
           json.dumps({"sha": sha, "made_at": manifest.get("made_at"), "at": now}).encode())
    log(f"state pulled from {branch} at {sha[:12]} (made {manifest.get('made_at')} by {manifest.get('by')}): "
        f"{sum(counts.values())} rows in {len(counts)} tables")
    return manifest


def _now_at(job_dir, sha, made_at, now):
    """After a push went through (or found nothing to change), this checkout's store matches the branch at sha:
    record it as the pull and the push, so the next push from here starts from it."""
    _write(os.path.join(job_dir, "state_pushed.json"), json.dumps({"ok": True, "sha": sha, "at": now}).encode())
    _write(os.path.join(job_dir, "state_pulled.json"),
           json.dumps({"sha": sha, "made_at": made_at, "at": now}).encode())


def push(conn, repo=ROOT, remote=REMOTE, branch=BRANCH, seed=False, no_push=False, by="github", run_url=None,
         site_dir=SITE_DATA_DIR, job_dir=JOB_DIR, now=None, export=None, onto_head=False):
    """Prune, then commit the state, the published lists and the API backup to the branch (only when something
    but the manifest changed) and push it. export: the API's export (a dict), or None to keep the branch's
    backup. onto_head: commit on top of the branch's current head instead of this run's pull. Returns
    {"sha", "changed", "pushed"}."""
    now = int(now if now is not None else now_epoch())
    removed = prune(conn, now)
    if any(removed.values()):
        log(f"  pruned: {', '.join(f'{v} {k}' for k, v in removed.items() if v)}")
    head = remote_head(repo, remote, branch)
    if seed:
        if head is not None:
            raise StateError(3, f"{branch} already exists on {remote}: pull it rather than seeding over it")
        parent, entries = None, {}
    else:
        if onto_head:
            if head is None:
                raise StateError(3, f"{branch} does not exist on {remote}: seed it with --seed")
            log(f"  on top of {branch}'s head {head[:12]} (--onto-head): this store replaces the state there")
        else:
            pulled = _read_json(os.path.join(job_dir, "state_pulled.json"))
            if not pulled or not pulled.get("sha"):
                raise StateError(3, "this run has not pulled the state (state_pulled.json is missing); nothing was "
                                    "pushed")
            if head != pulled["sha"]:
                raise StateError(3, "the branch moved since this run's pull; nothing was pushed")
        parent = head
        if _git(repo, "cat-file", "-e", f"{parent}^{{commit}}", check=False).returncode != 0:
            _fetch(repo, remote, f"refs/heads/{branch}")
        entries = _tree_entries(repo, parent)

    files = {f"state/{n}": d for n, d in dump_files(conn, by=by, run_url=run_url, made_at=now,
                                                    site_dir=site_dir).items()}
    files["README.md"] = README.encode("utf-8")
    for name in SITE_FILES:
        p = os.path.join(site_dir, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                files[f"site/{name}"] = f.read()
    if export is not None:
        files[BACKUP_PATH] = (json.dumps(export, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    new = {p: v for p, v in entries.items() if not p.startswith("state/")}      # the state is replaced whole
    for path, data in files.items():
        blob = _git(repo, "hash-object", "-w", "--stdin", data=data).stdout.decode().strip()
        new[path] = ("100644", blob)
    manifest_path = "state/manifest.json"
    changed = {p: v for p, v in new.items() if p != manifest_path} != \
        {p: v for p, v in entries.items() if p != manifest_path}
    if not changed:
        log(f"state unchanged since {parent[:12]}: nothing to commit")
        before = _show(repo, parent, manifest_path)
        _now_at(job_dir, parent, json.loads(before).get("made_at") if before else None, now)
        return {"sha": parent, "changed": False, "pushed": False}
    tree = _mktree(repo, new)
    made = json.loads(files[manifest_path])["made_at"]
    commit = _git(repo, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", f"State and lists, {made}",
                  env=_identity(repo)).stdout.decode().strip()
    if no_push:
        log(f"state committed as {commit[:12]} (not pushed: --no-push)")
        return {"sha": commit, "changed": True, "pushed": False}
    r = _git(repo, "push", remote, f"{commit}:refs/heads/{branch}", check=False)
    if r.returncode != 0:
        raise StateError(3, f"the push to {branch} was refused (did another run push first?); nothing was pushed: "
                            f"{r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    _now_at(job_dir, commit, made, now)
    log(f"state pushed to {branch} as {commit[:12]}" + (" (a new branch)" if seed else ""))
    return {"sha": commit, "changed": True, "pushed": True}


def _export():
    """The API's export for backup/d1.json, or None (no API configured, or it did not answer)."""
    from watchlist import sync
    c = sync.client()
    if c is None:
        return None
    try:
        return c.request("GET", "/api/job/export")
    except sync.ApiError as e:
        log(f"  ! the API export failed ({e}); backup/d1.json stays as it was")
        return None


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="The daily job's state on the watchlist-data branch.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("dump", "load", "pull", "push", "prune"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=DB_PATH, help="the sqlite store (default WATCHLIST_DB, else data/watchlist/)")
        if name in ("dump", "load"):
            p.add_argument("dir")
        if name in ("pull", "push"):
            p.add_argument("--remote", default=REMOTE)
            p.add_argument("--branch", default=BRANCH)
            p.add_argument("--repo", default=ROOT, help="the git checkout (default this one)")
            p.add_argument("--site-dir", default=SITE_DATA_DIR, help="where the published lists live")
    sub.choices["load"].add_argument("--allow-empty", action="store_true", help="load a state with no row at all")
    sub.choices["pull"].add_argument("--rev", default=None, help="pull this commit of the branch instead of its head")
    sub.choices["pull"].add_argument("--force", action="store_true",
                                     help="replace a local store that holds newer data (it is backed up first)")
    sub.choices["push"].add_argument("--seed", action="store_true", help="create the branch (it must not exist yet)")
    sub.choices["push"].add_argument("--onto-head", action="store_true",
                                     help="commit on top of the branch's current head, not this run's pull (after "
                                          "pull --rev <sha> --force: this store replaces the state there)")
    sub.choices["push"].add_argument("--no-push", action="store_true", help="make the commit, do not push it")
    sub.choices["push"].add_argument("--by", choices=("github", "mac", "local"), default="local")
    sub.choices["push"].add_argument("--run-url", default=None)
    sub.choices["prune"].add_argument("--dry-run", action="store_true", help="say what would go, remove nothing")
    args = ap.parse_args(argv)
    conn = store.connect(args.db)
    try:
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
            pull(conn, db_path=args.db, repo=args.repo, remote=args.remote, branch=args.branch, rev=args.rev,
                 force=args.force, site_dir=args.site_dir)
        else:
            if args.seed and args.onto_head:
                ap.error("--seed and --onto-head: one or the other")
            push(conn, repo=args.repo, remote=args.remote, branch=args.branch, seed=args.seed, no_push=args.no_push,
                 by=args.by, run_url=args.run_url, site_dir=args.site_dir, export=_export(), onto_head=args.onto_head)
    except StateError as e:
        print(f"state {args.cmd}: {e}", file=sys.stderr, flush=True)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error::state {args.cmd}: {e}", flush=True)
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
