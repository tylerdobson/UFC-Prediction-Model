-- Commands leave a durable status even when a source fetch or decision run
-- fails before it can create an ingestion receipt. Error categories are safe
-- to display; provider responses and credentials are never stored here.
CREATE TABLE operator_job_runs (
    job_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    event_id TEXT,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    error_category TEXT,
    CHECK ((status = 'started' AND finished_at_utc IS NULL AND error_category IS NULL)
        OR (status = 'succeeded' AND finished_at_utc IS NOT NULL AND error_category IS NULL)
        OR (status = 'failed' AND finished_at_utc IS NOT NULL AND error_category IS NOT NULL))
);

CREATE INDEX ix_operator_job_runs_started
    ON operator_job_runs(started_at_utc DESC, job_run_id DESC);

CREATE TRIGGER operator_job_runs_finished_no_update
BEFORE UPDATE ON operator_job_runs
WHEN OLD.status <> 'started'
BEGIN
    SELECT RAISE(ABORT, 'finished operator jobs are immutable');
END;

CREATE TRIGGER operator_job_runs_no_delete
BEFORE DELETE ON operator_job_runs
BEGIN
    SELECT RAISE(ABORT, 'operator jobs are immutable');
END;
