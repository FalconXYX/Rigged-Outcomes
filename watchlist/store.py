"""
The watchlist's sqlite store: data/watchlist/watchlist.db.

sqlite because the machine has no pyarrow and because the production plan
is Cloudflare D1, which is sqlite: these tables are meant to move there
as-is. Everything a list learns is written here so every step is resumable
(a market already labelled or checked is never asked again).

Tables (all times are UTC epoch seconds unless the column says _iso/_day):

  ai_labels      AI answers per (market_id, sha1 of the rules text).
  meta           free key/value (resume cursors, run notes).

  The open list ("Open now", approved 2026-11-13; vocabulary in watchlist.kinds):
  open_pool      an open event the first time it entered the pool, with the
                 one 99c check (no outcome at 99c or more then; never re-run:
                 a later jump to 99c may be the very case the list is for).
                 The pass/fail is stored, never the price.
  open_filings   the kind and the names an open event's sentences take, and
                 its story key, from a word rule, the model or a person.
  open_answers   the models' checked answers per (story, headlines_key of the
                 headlines it was given, prompt version, reading 1 or 2): the
                 step reading (open-step-*: which numbered headlines are steps,
                 the second reading in reverse order) and "already out"
                 (open-out-*). A story with no new headlines keeps its
                 answers, so nothing flips overnight by chance.
  open_runs      one row per daily list: updated | not updated today.
  open_cards     the day's cards in place order, each card's JSON as written
                 to open.json.
  open_listed    every event id, event slug and market slug of a shown card,
                 per day: open_listed_since() for the closed list's "On the
                 open list from" line.

One connection per process is shared across threads (check_same_thread is
off) and every statement runs under LOCK, which is simpler than a
connection per thread and fast enough for a few thousand writes a day.
"""
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from watchlist.common import DB_PATH, iso, now_epoch, to_epoch

LOCK = threading.RLock()
_DEFAULT = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_labels (
    market_id    TEXT NOT NULL,
    rules_sha1   TEXT NOT NULL,
    model        TEXT,
    type         TEXT,
    reveal_kind  TEXT,
    reveal_date  TEXT,
    quote        TEXT,
    valid        INTEGER,                    -- 1 when the quote checked out against the rules
    raw          TEXT,                       -- the model's answer as received
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (market_id, rules_sha1)
);
-- the closed list's model check of one market (watchlist.judge): news query, headlines, verdict
CREATE TABLE IF NOT EXISTS closed_checks (
    condition_id TEXT NOT NULL,
    version      TEXT NOT NULL,              -- judge.PROMPT_VERSION: a new prompt checks again
    data         TEXT NOT NULL,              -- the check as JSON
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (condition_id, version)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- the open list ("Open now"): an event's first entry into the pool, with its one 99c check
CREATE TABLE IF NOT EXISTS open_pool (
    event_id       TEXT PRIMARY KEY,
    slug           TEXT,
    title          TEXT,
    first_seen_ts  INTEGER NOT NULL,
    first_seen_day TEXT    NOT NULL,         -- YYYY-MM-DD (UTC)
    under_99c      INTEGER NOT NULL,         -- 1 = no outcome at 99c or more at first entry (it may be in the pool)
    last_seen_ts   INTEGER NOT NULL,
    capped         TEXT                      -- JSON [market id]: markets at 99c or more at first entry (never in it)
);
-- the kind (watchlist.kinds) and the names an open event's sentences take
CREATE TABLE IF NOT EXISTS open_filings (
    event_id   TEXT PRIMARY KEY,
    kind       TEXT,                         -- a kinds.KINDS key; NULL while unfiled (counted in group 2)
    names      TEXT,                         -- JSON {slot: value}, kinds.SLOTS
    story      TEXT,                         -- story key: events one piece of news would settle share one
    source     TEXT    NOT NULL,             -- rule | model:<model> | person
    version    TEXT,                         -- the prompt version, when the model filed it
    text_sha1  TEXT,                         -- sha1 of the text it was filed from (a changed text files again)
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS open_filings_story ON open_filings(story);
-- the models' checked answers for one story (the step reading, already out), as the code checked them
CREATE TABLE IF NOT EXISTS open_answers (
    story          TEXT    NOT NULL,
    headlines_sha1 TEXT    NOT NULL,         -- headlines_key() of the headlines it was given
    version        TEXT    NOT NULL,         -- the frozen prompt's version: a new wording asks again
    run            INTEGER NOT NULL,         -- 1 = the first reading, 2 = the second (stories that would make the list)
    data           TEXT    NOT NULL,         -- JSON
    model          TEXT,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (story, headlines_sha1, version, run)
);
CREATE INDEX IF NOT EXISTS open_answers_latest ON open_answers(story, version, run, created_at);
CREATE TABLE IF NOT EXISTS open_runs (
    day     TEXT PRIMARY KEY,                -- YYYY-MM-DD (UTC) of the list
    status  TEXT    NOT NULL,                -- updated | not updated today
    run_at  INTEGER NOT NULL,
    version TEXT,                            -- the prompt version the list was built with
    info    TEXT                             -- JSON: counts, held back, notes
);
CREATE TABLE IF NOT EXISTS open_cards (
    day          TEXT    NOT NULL,
    rank         INTEGER NOT NULL,           -- 1-based place order on the day's list
    story        TEXT    NOT NULL,           -- the card's id
    place        TEXT    NOT NULL,           -- new step | stays | gone quiet | dated
    label        TEXT    NOT NULL,           -- new | listed | back
    listed_since TEXT    NOT NULL,           -- YYYY-MM-DD the current stay began
    entered      TEXT,                       -- YYYY-MM-DD it last entered with a new step (the 3-day stay counts from it)
    card         TEXT    NOT NULL,           -- JSON: the card as written to open.json
    PRIMARY KEY (day, story)
);
CREATE TABLE IF NOT EXISTS open_listed (
    ident TEXT NOT NULL,                     -- an event id, event slug or market slug of a shown card's markets
    day   TEXT NOT NULL,
    story TEXT NOT NULL,
    PRIMARY KEY (ident, day, story)
);
"""


def connect(path=DB_PATH):
    """Open (creating if needed) the store at `path` and ensure the schema.
    path=":memory:" gives a throwaway store (tests)."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    with LOCK:
        if path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        for table, column, decl in _ADDED_COLUMNS:          # columns added after a table was first made
            if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
    return conn


_ADDED_COLUMNS = (("open_pool", "capped", "TEXT"),)


def default_conn():
    """The process-wide connection to DB_PATH (opened on first use)."""
    with LOCK:
        if "conn" not in _DEFAULT:
            _DEFAULT["conn"] = connect(DB_PATH)
        return _DEFAULT["conn"]


@contextmanager
def transaction(conn):
    """Run a block of statements atomically under the store lock."""
    with LOCK:
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _row(r):
    return dict(r) if r is not None else None


def _json(v):
    return json.dumps(v, separators=(",", ":"), default=str)


# ── AI labels ────────────────────────────────────────────────────────────────

def get_ai_label(conn, market_id, rules_sha1):
    with LOCK:
        return _row(conn.execute("SELECT * FROM ai_labels WHERE market_id=? AND rules_sha1=?",
                                 (str(market_id), rules_sha1)).fetchone())


def save_ai_label(conn, market_id, rules_sha1, label, model, valid, raw):
    """Store one AI label: `label` carries type, reveal_kind, reveal_date, quote."""
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO ai_labels (market_id, rules_sha1, model, type, reveal_kind, reveal_date, quote, "
            "valid, raw, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(market_id), rules_sha1, model, label.get("type"), label.get("reveal_kind"), label.get("reveal_date"),
             label.get("quote"), 1 if valid else 0, raw if isinstance(raw, str) else _json(raw), now_epoch()))


def get_closed_check(conn, condition_id, version):
    with LOCK:
        r = conn.execute("SELECT data FROM closed_checks WHERE condition_id=? AND version=?",
                         (str(condition_id), version)).fetchone()
    return json.loads(r[0]) if r else None


def save_closed_check(conn, condition_id, version, check):
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO closed_checks (condition_id, version, data, created_at) VALUES (?,?,?,?)",
                     (str(condition_id), version, _json(check), now_epoch()))


# ── the open list ────────────────────────────────────────────────────────────

def _day_of(epoch):
    from watchlist.common import local_day
    return local_day(epoch)


def get_pool_entry(conn, event_id):
    """The open_pool row of an event (first_seen_ts, first_seen_day, under_99c, capped, ...), or None."""
    with LOCK:
        r = conn.execute("SELECT * FROM open_pool WHERE event_id=?", (str(event_id),)).fetchone()
    return _pool_row(r) if r else None


def _pool_row(r):
    d = dict(r)
    d["capped"] = json.loads(d.get("capped") or "[]")
    return d


def pool_entries(conn):
    """{event_id: open_pool row} for every event that has entered the pool
    (capped: the market ids at 99c or more at its first entry, a list)."""
    with LOCK:
        return {r["event_id"]: _pool_row(r) for r in conn.execute("SELECT * FROM open_pool").fetchall()}


def record_pool_entry(conn, event_id, slug, title, under_99c, seen_ts=None, capped=None):
    """Note that an event is in today's walk. The first time, its 99c check
    (under_99c: no outcome at 99c or more right now) is stored with the
    first-seen time and the ids of its markets at 99c or more then (capped:
    they never enter the pool); later calls only update the slug, title and
    last seen time, never the check. Returns the stored row, so the caller
    reads the check from it."""
    seen_ts = now_epoch() if seen_ts is None else int(seen_ts)
    with transaction(conn):
        conn.execute(
            "INSERT INTO open_pool (event_id, slug, title, first_seen_ts, first_seen_day, under_99c, last_seen_ts, "
            "capped) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET slug=excluded.slug, "
            "title=excluded.title, last_seen_ts=MAX(open_pool.last_seen_ts, excluded.last_seen_ts)",
            (str(event_id), slug, title, seen_ts, _day_of(seen_ts), 1 if under_99c else 0, seen_ts,
             _json([str(x) for x in capped or []])))
    return get_pool_entry(conn, event_id)


def get_filing(conn, event_id):
    """An open event's filing {kind, names (dict), story, source, version,
    text_sha1, created_at}, or None."""
    with LOCK:
        r = _row(conn.execute("SELECT * FROM open_filings WHERE event_id=?", (str(event_id),)).fetchone())
    if r:
        r["names"] = json.loads(r["names"] or "{}")
    return r


def save_filing(conn, event_id, kind, names, source, story=None, version=None, text_sha1=None):
    """File an open event (replacing an earlier filing). source: "rule",
    "model:<model>" or "person"; kind None leaves it unfiled."""
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO open_filings (event_id, kind, names, story, source, version, text_sha1, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(event_id), kind, _json(names or {}), story, source, version, text_sha1, now_epoch()))


def headlines_key(headlines):
    """sha1 of a set of headlines (title, source, published), order-blind:
    the key the model's answers are cached under."""
    lines = sorted(f"{h.get('title') or ''}\x1f{h.get('source') or ''}\x1f{h.get('published') or ''}"
                   for h in headlines or [])
    return hashlib.sha1("\x1e".join(lines).encode("utf-8")).hexdigest()


def open_answer_history(conn, story, version, since_ts=None):
    """Every reading stored for a story under a prompt version, both runs
    and every headline set, oldest first: [{"headlines_sha1", "run", "data",
    "model", "created_at"}]; since_ts keeps those made at or after it. The
    open list's step history: a step up to 21 days old comes from an earlier
    day's reading, which read that day's last 7 days."""
    q, args = "SELECT headlines_sha1, run, data, model, created_at FROM open_answers WHERE story=? AND version=?", \
        [story, version]       # created_at is the run's own time (save_open_answer), so a replay sees its past
    if since_ts is not None:
        q += " AND created_at >= ?"
        args.append(int(since_ts))
    with LOCK:
        rows = conn.execute(q + " ORDER BY created_at, rowid", args).fetchall()
    return [{"headlines_sha1": r["headlines_sha1"], "run": r["run"], "data": json.loads(r["data"]), "model": r["model"],
             "created_at": r["created_at"]} for r in rows]


def save_open_answer(conn, story, headlines_sha1, version, run, data, model=None, created_at=None):
    """Store one checked answer; created_at: the run's time (default now)."""
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO open_answers (story, headlines_sha1, version, run, data, model, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (story, headlines_sha1, version, int(run), _json(data), model,
                                                int(created_at) if created_at is not None else now_epoch()))


def open_answered(conn):
    """The stories an "already out" reading found answered (data "answered": the answer is public), under any
    version: the open list never shows them again."""
    with LOCK:
        rows = conn.execute("SELECT DISTINCT story FROM open_answers WHERE version LIKE 'open-out-%' "
                            "AND json_extract(data, '$.answered') = 1").fetchall()
    return {r[0] for r in rows}


_CARD_KEYS = ("id", "place", "label", "listed_since")


def save_open_day(conn, day, status, run_at, cards, version=None, info=None):
    """Store one day's list (YYYY-MM-DD), replacing what that day had. Each
    card is a dict with at least id (the story key), place, label and
    listed_since, plus event_ids and slugs (the closed list matches on
    them); an "entered" key (the day it last entered with a new step) goes to
    its own column, the rest is kept as the card's JSON in list order."""
    rows, idents = [], set()
    for i, c in enumerate(cards, 1):
        missing = [k for k in _CARD_KEYS if not c.get(k)]
        if missing:
            raise ValueError(f"open card {c.get('id')!r} without {', '.join(missing)}")
        card = {k: v for k, v in c.items() if k != "entered"}
        rows.append((day, i, c["id"], c["place"], c["label"], c["listed_since"], c.get("entered"), _json(card)))
        for ident in list(c.get("event_ids") or []) + list(c.get("slugs") or []):
            if ident:
                idents.add((str(ident), day, c["id"]))
    with transaction(conn):
        conn.execute("DELETE FROM open_cards WHERE day=?", (day,))
        conn.execute("DELETE FROM open_listed WHERE day=?", (day,))
        conn.execute("INSERT OR REPLACE INTO open_runs (day, status, run_at, version, info) VALUES (?,?,?,?,?)",
                     (day, status, int(to_epoch(run_at)), version, _json(info or {})))
        conn.executemany("INSERT INTO open_cards (day, rank, story, place, label, listed_since, entered, card) "
                         "VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT OR IGNORE INTO open_listed (ident, day, story) VALUES (?,?,?)", sorted(idents))


def get_open_day(conn, day):
    """One stored day: {"day", "status", "run_at" (epoch), "version", "info",
    "cards" (in place order, each with its "entered")}, or None."""
    with LOCK:
        run = conn.execute("SELECT * FROM open_runs WHERE day=?", (day,)).fetchone()
        if not run:
            return None
        rows = conn.execute("SELECT card, entered FROM open_cards WHERE day=? ORDER BY rank", (day,)).fetchall()
    cards = []
    for r in rows:
        c = json.loads(r["card"])
        c["entered"] = r["entered"]
        cards.append(c)
    return {"day": run["day"], "status": run["status"], "run_at": run["run_at"], "version": run["version"],
            "info": json.loads(run["info"] or "{}"), "cards": cards}


def previous_open_day(conn, before_day, status=None):
    """The latest stored day before `before_day` (YYYY-MM-DD), as get_open_day
    gives it, or None; status: only a day with that status ("updated")."""
    q, args = "SELECT MAX(day) AS d FROM open_runs WHERE day < ?", [before_day]
    if status:
        q += " AND status = ?"
        args.append(status)
    with LOCK:
        r = conn.execute(q, args).fetchone()
    return get_open_day(conn, r["d"]) if r and r["d"] else None


def open_days(conn, since_day=None, until_day=None):
    """Stored days in [since_day, until_day] (either open), newest first, as get_open_day gives them."""
    q, args = "SELECT day FROM open_runs WHERE 1=1", []
    if since_day:
        q += " AND day >= ?"
        args.append(since_day)
    if until_day:
        q += " AND day <= ?"
        args.append(until_day)
    with LOCK:
        days = [r["day"] for r in conn.execute(q + " ORDER BY day DESC", args).fetchall()]
    return [get_open_day(conn, d) for d in days]


def open_story_days(conn, story):
    """The days (YYYY-MM-DD, oldest first) a story was on the list, for its
    "new" / "listed" / "back" label and its listed_since."""
    with LOCK:
        return [r["day"] for r in conn.execute("SELECT day FROM open_cards WHERE story=? ORDER BY day",
                                               (story,)).fetchall()]


def open_listed_card(conn, idents):
    """The latest open card (its JSON, as written to open.json) whose markets
    include any of these event ids, event slugs or market slugs, or None."""
    idents = [str(x) for x in idents or [] if x not in (None, "")]
    if not idents:
        return None
    with LOCK:
        r = conn.execute(f"SELECT c.card FROM open_listed l JOIN open_cards c ON c.day = l.day AND c.story = l.story "
                         f"WHERE l.ident IN ({','.join('?' * len(idents))}) ORDER BY l.day DESC LIMIT 1",
                         idents).fetchone()
    return json.loads(r["card"]) if r else None


def open_story_idents(conn, story):
    """(event ids, slugs) of every card a story had, oldest day first, each once: the events it was asked about
    whole and the slugs of their markets, and the one market of each outcome split into its own story (its slug
    alone; opennow.story_idents). ([], []) when it never had a card."""
    with LOCK:
        rows = conn.execute("SELECT card FROM open_cards WHERE story=? ORDER BY day", (story,)).fetchall()
    ids, slugs = [], []
    for r in rows:
        card = json.loads(r["card"])
        for key, out in (("event_ids", ids), ("slugs", slugs)):
            for x in card.get(key) or []:
                if x not in (None, "") and str(x) not in out:
                    out.append(str(x))
    return ids, slugs


def open_listed_since(conn, event_id_or_slug):
    """The first day (YYYY-MM-DD) a market was on an open card, matched by
    its event id, event slug or market slug (or any of several, given as a
    list), or None. The closed list's "On the open list from" line."""
    if isinstance(event_id_or_slug, (list, tuple, set, frozenset)):
        idents = [str(x) for x in event_id_or_slug if x not in (None, "")]
    else:
        idents = [str(event_id_or_slug)] if event_id_or_slug not in (None, "") else []
    if not idents:
        return None
    with LOCK:
        r = conn.execute(f"SELECT MIN(day) AS d FROM open_listed WHERE ident IN ({','.join('?' * len(idents))})",
                         idents).fetchone()
    return r["d"] if r and r["d"] else None


# ── meta ─────────────────────────────────────────────────────────────────────

def get_meta(conn, key, default=None):
    with LOCK:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_meta(conn, key, value):
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, _json(value)))


def stamp(epoch=None):
    """ISO time for log lines and JSON outputs."""
    return iso(epoch if epoch is not None else now_epoch())
