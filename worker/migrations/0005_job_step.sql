-- One column the queue's complete needs beside 0004_backend.sql: the nonce of the complete that ended a job's
-- step. POST /api/job/complete writes it in the statement that moves the job out of 'leased', and every other
-- write of that complete (results, children, run meta, the ledger) is guarded by it, so two copies of one
-- complete that race (a retry while the first was still in flight) count the step once.
ALTER TABLE jobs ADD COLUMN step_nonce TEXT;
