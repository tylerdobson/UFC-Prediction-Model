-- The forecast ledger itself is append-only JSONL; these tables retain live inputs.
ALTER TABLE predictions ADD COLUMN metadata_json TEXT;
ALTER TABLE prefight_gate_checks ADD COLUMN max_market_disagreement REAL NOT NULL DEFAULT 0.10
    CHECK (max_market_disagreement >= 0 AND max_market_disagreement <= 1);

CREATE TABLE odds_schedule_snapshots (
    event_id TEXT NOT NULL REFERENCES events(event_id),
    slot TEXT NOT NULL CHECK (slot IN ('t7d','t24h','t2h')),
    due_at_utc TEXT NOT NULL,
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    remaining_requests INTEGER NOT NULL CHECK (remaining_requests >= 50),
    PRIMARY KEY (event_id, slot)
);
CREATE TRIGGER odds_schedule_no_update BEFORE UPDATE ON odds_schedule_snapshots
BEGIN SELECT RAISE(ABORT, 'scheduled odds receipts are immutable'); END;
CREATE TRIGGER odds_schedule_no_delete BEFORE DELETE ON odds_schedule_snapshots
BEGIN SELECT RAISE(ABORT, 'scheduled odds receipts are immutable'); END;
