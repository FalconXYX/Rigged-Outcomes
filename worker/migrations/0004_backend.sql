-- The backend on Cloudflare Free and Vercel Hobby (docs/BACKEND.md): the job queue (runs, jobs, their results),
-- the news searches, the compute ledger and the wake-up lanes, all in D1. Times are epoch seconds; every day
-- is a Toronto calendar day ("YYYY-MM-DD", America/Toronto), never a UTC one. Kinds, lanes, states and limits
-- are worker/src/contract.ts (backend/contract.py holds the same).
--
-- Also: the published files move to site:* blob names (served at /api/data/), the news cache blob goes (its
-- searches are rows of news_cache now), an add request remembers its run, and the GitHub start-up settings go.

-- A run: one daily run, catch-up, add or "Apply decisions now", keyed (kind, slot): the Toronto day for daily
-- and catchup, '<condition id>:<decided_at>' for add, the press's epoch second for apply. id is '<kind>:<slot>'.
CREATE TABLE runs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('daily','catchup','add','apply')),
  slot        TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('queued','running','done','partial','failed','cancelled','missed','skipped')),
  meta        TEXT NOT NULL DEFAULT '{}',            -- JSON object, at most 8 KB: what its jobs report (left, problems, ...)
  created_at  INTEGER NOT NULL,
  started_at  INTEGER,                               -- its first claim
  deadline    INTEGER NOT NULL,                      -- past it, its queued jobs that are not final are cancelled
  finished_at INTEGER,
  UNIQUE (kind, slot)
);
CREATE INDEX runs_state ON runs(state, deadline);

-- A job: one piece of a run, done in one or more steps of a Vercel call. id is '<run id>|<kind>|<part>', so the
-- same job inserted twice is one row. lane, heavy, need_s, final and priority are copied from the contract (the
-- kind's row, the run kind's priority) when it is inserted. after: the kinds (comma-separated, no spaces) whose
-- jobs in the same run must all be done, failed or cancelled before this one can be claimed.
CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL,
  kind        TEXT NOT NULL,
  part        TEXT NOT NULL,                         -- '0', or the batch's own key
  lane        TEXT NOT NULL CHECK (lane IN ('main','news','model','add')),
  heavy       INTEGER NOT NULL DEFAULT 0,            -- 1: at most one heavy job holds a lease, across every lane
  need_s      INTEGER NOT NULL,                      -- claimed only by a call with at least this many seconds left
  final       INTEGER NOT NULL DEFAULT 0,            -- 1: a publishing job: never cancelled by the run's deadline
  priority    INTEGER NOT NULL DEFAULT 2,            -- the run kind's: lower is claimed first
  after       TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL CHECK (state IN ('queued','leased','done','failed','cancelled')),
  run_after   INTEGER NOT NULL,
  attempt     INTEGER NOT NULL DEFAULT 0,            -- errors and expired leases so far
  laters      INTEGER NOT NULL DEFAULT 0,            -- 'later' outcomes so far
  partials    INTEGER NOT NULL DEFAULT 0,            -- 'partial' outcomes so far
  lease_token TEXT,                                  -- kept after the step ends, so a repeated complete is recognised
  lease_until INTEGER,
  input       TEXT NOT NULL DEFAULT '{}',            -- JSON object, at most 16 KB
  cursor      TEXT,                                  -- JSON, at most 64 KB: where the last partial step stopped
  cpu_ms      INTEGER NOT NULL DEFAULT 0,            -- totals over its steps
  wall_ms     INTEGER NOT NULL DEFAULT 0,
  error       TEXT,                                  -- the last error, one line, secrets removed, at most 500 characters
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX jobs_ready ON jobs(state, lane, run_after);
CREATE INDEX jobs_run   ON jobs(run_id, state);

-- What a run's jobs found, one row per item (a label batch, a reading, a whale): written by checkpoint and
-- complete while the job's lease holds, read by later jobs of the run (or a catch-up of the same day).
CREATE TABLE job_results (
  run_id     TEXT NOT NULL,
  key        TEXT NOT NULL,                          -- '<kind>:<item>', at most 300 characters
  job_id     TEXT NOT NULL,
  value      TEXT NOT NULL,                          -- JSON, at most 200 KB (bigger goes to a scratch:* blob)
  created_at INTEGER NOT NULL,
  PRIMARY KEY (run_id, key)
) WITHOUT ROWID;

-- One Google News search per row (opennow's NewsCache key: the query and its window), the latest fetch only.
CREATE TABLE news_cache (
  key        TEXT PRIMARY KEY,                       -- at most 600 characters
  day        TEXT NOT NULL,                          -- the Toronto day it was fetched on
  fetched_at INTEGER NOT NULL,
  body       TEXT NOT NULL                           -- JSON list of headlines as watchlist.news.search returns them, at most 200 KB
) WITHOUT ROWID;

-- The compute ledger: what the Vercel calls used, per Toronto day and job kind ('_wake': each wake-up sent, one
-- invocation; '_overhead': a call's time outside its jobs). gb_ms is wall_ms times the instance's 2 GB. units:
-- whales for add.whales, searches for the news kinds, requests for the model kinds.
CREATE TABLE usage (
  day     TEXT NOT NULL,
  kind    TEXT NOT NULL,
  steps   INTEGER NOT NULL DEFAULT 0,
  cpu_ms  INTEGER NOT NULL DEFAULT 0,
  gb_ms   INTEGER NOT NULL DEFAULT 0,
  wakes   INTEGER NOT NULL DEFAULT 0,
  units   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, kind)
) WITHOUT ROWID;

-- One row per lane: its pause (the model busy, Google News limiting) and its wake-up back-off.
CREATE TABLE lanes (
  lane          TEXT PRIMARY KEY CHECK (lane IN ('main','news','model','add')),
  paused_until  INTEGER NOT NULL DEFAULT 0,          -- no claim from this lane before it
  last_wake_at  INTEGER NOT NULL DEFAULT 0,
  unanswered    INTEGER NOT NULL DEFAULT 0,          -- wake-ups sent since the lane's last claim
  last_claim_at INTEGER NOT NULL DEFAULT 0,
  wake_error    TEXT                                 -- the last wake-up that failed: 'HTTP 503', 'no answer'; NULL once one goes through
) WITHOUT ROWID;
INSERT INTO lanes (lane) VALUES ('main'), ('news'), ('model'), ('add');

-- An add request's run (the latest one made for it).
ALTER TABLE requests ADD COLUMN run_id TEXT;

-- GitHub's start-up bookkeeping is gone with GitHub Actions.
DELETE FROM settings WHERE key IN ('last_dispatch', 'dispatch_seen', 'dispatch_error', 'work_read');

-- The published lists keep their saved versions under the names the Worker serves (/api/data/closed.json, ...).
UPDATE job_blobs         SET name = 'site:closed.json' WHERE name = 'list:closed';
UPDATE job_blob_parts    SET name = 'site:closed.json' WHERE name = 'list:closed';
UPDATE job_blob_versions SET name = 'site:closed.json' WHERE name = 'list:closed';
UPDATE job_blobs         SET name = 'site:open.json'   WHERE name = 'list:open';
UPDATE job_blob_parts    SET name = 'site:open.json'   WHERE name = 'list:open';
UPDATE job_blob_versions SET name = 'site:open.json'   WHERE name = 'list:open';

-- The news cache blob (17 MB a day) is replaced by news_cache's rows.
DELETE FROM job_blob_parts WHERE name = 'cache:news';
DELETE FROM job_blobs      WHERE name = 'cache:news';
