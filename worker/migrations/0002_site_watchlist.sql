-- One watchlist for the whole site, and "Add to main page" adds with no review.
--
-- watches: one row per open card's story on the site's watchlist (no visitor): Watch puts it on for everyone,
-- Stop watching takes it off for everyone. A story some visitor watched under 0001 stays on, from its first watch.
-- requests: a click records the market as 'approved' at once (the status the job adds); one still waiting under
-- 0001's review, or turned down there, is added too. 'requested' and 'declined' are no longer written.

CREATE TABLE watches_site (
  story      TEXT PRIMARY KEY,                       -- the open card's id (opennow story key)
  created_at INTEGER NOT NULL
) WITHOUT ROWID;
INSERT INTO watches_site (story, created_at) SELECT story, min(created_at) FROM watches GROUP BY story;
DROP TABLE watches;
ALTER TABLE watches_site RENAME TO watches;

UPDATE requests SET status = 'approved', decided_at = coalesce(decided_at, requested_at), done_at = NULL, note = NULL
WHERE status IN ('requested', 'declined');
