-- Rigged Outcomes API (Cloudflare D1). Times: UTC epoch seconds, except markers.t (the site's
-- convention, export.py: the US/Eastern wall clock read as UTC). visitor = the first 32 hex chars of
-- HMAC-SHA256(VISITOR_SALT, address), where the address is CF-Connecting-IP for IPv4 and its /64 network
-- for IPv6 (one subscriber holds a whole /64); no IP address is ever stored.

CREATE TABLE markers (
  id          TEXT PRIMARY KEY,                      -- crypto.randomUUID()
  market      TEXT NOT NULL,                         -- markets.json name ("usiran")
  t           INTEGER NOT NULL,
  label       TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 40),
  source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','ai')),
  visitor     TEXT,                                  -- NULL for 'ai'; cleared after 30 days
  undo_sha    TEXT,                                  -- sha-256 hex of the undo token given on create
  created_at  INTEGER NOT NULL,
  deleted_at  INTEGER,
  deleted_by  TEXT CHECK (deleted_by IN ('owner','visitor'))
);
CREATE INDEX markers_live   ON markers(market, deleted_at, t);
CREATE INDEX markers_recent ON markers(created_at);

CREATE TABLE watches (
  story      TEXT NOT NULL,                          -- the open card's id (opennow story key)
  visitor    TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (story, visitor)
) WITHOUT ROWID;

CREATE TABLE requests (                              -- "Add to main page", one per market
  condition_id TEXT PRIMARY KEY,
  slug         TEXT NOT NULL,                        -- these six are copied from the catalog, never the browser
  question     TEXT NOT NULL,
  url          TEXT NOT NULL,
  winner       TEXT NOT NULL,
  closed       TEXT NOT NULL,                        -- ISO close time
  day          TEXT NOT NULL,                        -- the closed list's day
  status       TEXT NOT NULL DEFAULT 'requested'
               CHECK (status IN ('requested','approved','declined','added','failed')),
  name         TEXT,                                 -- markets.json name the owner chose
  note         TEXT,                                 -- the job's reason when adding failed
  requested_at INTEGER NOT NULL,
  decided_at   INTEGER,
  done_at      INTEGER
);
CREATE INDEX requests_status ON requests(status, requested_at);

CREATE TABLE open_queue (                            -- the open list's review queue, one row per item
  day       TEXT NOT NULL,
  story     TEXT NOT NULL,
  n         INTEGER NOT NULL,
  item      TEXT NOT NULL,                           -- JSON QueueItem (4.4)
  run_at    TEXT NOT NULL,
  steps_by  TEXT NOT NULL,
  pushed_at INTEGER NOT NULL,
  PRIMARY KEY (day, story)
);

CREATE TABLE open_decisions (
  story        TEXT NOT NULL,
  headline_key TEXT NOT NULL,                        -- opennow.headline_key(h), or 'card'
  -- 'withdrawn': the owner took the decision back before a run reported it applied. The row stays, as a
  -- marker the next run reads to remove the decision from its store too (a run may already have saved it),
  -- and goes once that run reports it.
  decision     TEXT NOT NULL CHECK (decision IN ('step','not a step','card approved','withdrawn')),
  headline     TEXT,                                 -- JSON {title,source,published,url} from the queue; NULL for 'card'
  day          TEXT NOT NULL,
  decided_at   INTEGER NOT NULL,
  applied_at   INTEGER,
  PRIMARY KEY (story, headline_key)
);
CREATE INDEX open_decisions_pending ON open_decisions(applied_at, decided_at);

CREATE TABLE catalog (                               -- what may be written to, as the job last listed it
  kind      TEXT NOT NULL CHECK (kind IN ('market','closed','open')),
  id        TEXT NOT NULL,                           -- market name | condition id | story
  slug      TEXT,                                    -- normalised slug, for 'market' and 'closed'
  data      TEXT NOT NULL,                           -- the entry as pushed (JSON)
  synced_at INTEGER NOT NULL,
  PRIMARY KEY (kind, id)
);
CREATE INDEX catalog_slug ON catalog(kind, slug);

CREATE TABLE quota (
  day     TEXT NOT NULL,                             -- UTC YYYY-MM-DD
  kind    TEXT NOT NULL,                             -- marker | marker:<market> | watch | request | auth_fail | dispatch
  visitor TEXT NOT NULL,                             -- hash, or '*' for the whole site
  n       INTEGER NOT NULL,
  PRIMARY KEY (day, kind, visitor)
) WITHOUT ROWID;

CREATE TABLE job_runs (
  id          INTEGER PRIMARY KEY,
  mode        TEXT NOT NULL CHECK (mode IN ('full','apply','local')),
  status      TEXT NOT NULL CHECK (status IN ('success','partial','failure')),
  started_at  INTEGER NOT NULL,
  finished_at INTEGER NOT NULL,
  summary     TEXT,
  url         TEXT
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
INSERT INTO settings (key, value, updated_at) VALUES ('writes', 'on', 0), ('last_dispatch', '0', 0);
