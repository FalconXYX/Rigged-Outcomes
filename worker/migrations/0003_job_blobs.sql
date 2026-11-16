-- The daily job's own data, which until now lived on a git branch: its memory between runs
-- (the store tables the lists read and write), the two published lists, the news searches cache, and the
-- markets added with "Add to main page" (each one's built page). Nothing of the job's is kept in git.
--
-- Each is one blob: a gzip-compressed JSON document as base64 text, cut into parts (the job sends 500,000
-- characters a part; D1 holds up to 2 MB a row). A new version's parts are written beside the live ones and
-- job_blobs moves to it last, in one statement, so a reader never sees half a save and a failed save changes
-- nothing. The state's and the lists' last 3 replaced versions are kept (job_blob_versions), so a bad save can
-- be undone on its own (python3 -m watchlist.blobs restore). Every table is WITHOUT ROWID: a row written is one
-- row, with no separate index to update.

CREATE TABLE job_blobs (
  name       TEXT PRIMARY KEY,                       -- 'state' | 'list:closed' | 'list:open' | 'cache:news' | 'market:<page name>'
  version    TEXT NOT NULL,                          -- the live version: 32 hex characters the job chose
  parts      INTEGER NOT NULL,
  bytes      INTEGER NOT NULL,                       -- the document's size, uncompressed
  sha256     TEXT NOT NULL,                          -- of the uncompressed document (the job checks it on download)
  meta       TEXT NOT NULL,                          -- JSON object: the state's manifest, a market's entry, ...
  updated_at INTEGER NOT NULL
) WITHOUT ROWID;

-- created_at comes before data so the daily prune of abandoned uploads reads it without walking each part's
-- overflow pages (a column stored after a large one is only reached through them).
CREATE TABLE job_blob_parts (
  name       TEXT NOT NULL,
  version    TEXT NOT NULL,
  part       INTEGER NOT NULL,                       -- 0, 1, ...
  created_at INTEGER NOT NULL,
  data       TEXT NOT NULL,                          -- base64
  PRIMARY KEY (name, version, part)
) WITHOUT ROWID;

-- The versions a save replaced, for state and the lists only (their parts stay in job_blob_parts): the newest 3
-- of each name; an older one goes, with its parts, when a save pushes it out.
CREATE TABLE job_blob_versions (
  name        TEXT NOT NULL,
  version     TEXT NOT NULL,
  seq         INTEGER NOT NULL,                      -- 1, 2, ... per name, in the order they were replaced
  parts       INTEGER NOT NULL,
  bytes       INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  meta        TEXT NOT NULL,
  updated_at  INTEGER NOT NULL,                      -- when it went live
  replaced_at INTEGER NOT NULL,
  PRIMARY KEY (name, version)
) WITHOUT ROWID;
