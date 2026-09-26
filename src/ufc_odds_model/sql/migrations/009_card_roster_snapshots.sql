-- A receipt records when this application imported a source. These rows
-- preserve what that source said about a card at its separately attested
-- observation time. A NULL observation time cannot prove a pre-fight roster.
CREATE TABLE card_event_snapshots (
    event_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    source_observed_at_utc TEXT,
    observation_basis TEXT NOT NULL
        CHECK (observation_basis IN ('reviewed_csv', 'local_fetch', 'unknown')),
    source_url TEXT,
    source_revision_id TEXT,
    license_name TEXT,
    license_url TEXT,
    reviewed_by TEXT,
    event_name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    start_time_utc TEXT,
    event_status TEXT NOT NULL
        CHECK (event_status IN ('scheduled', 'completed', 'cancelled')),
    event_provider_status TEXT,
    CHECK ((source_observed_at_utc IS NULL AND observation_basis = 'unknown')
        OR (source_observed_at_utc IS NOT NULL AND observation_basis <> 'unknown')),
    UNIQUE (ingestion_run_id, event_id)
);

CREATE TABLE card_bout_snapshots (
    bout_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_snapshot_id INTEGER NOT NULL REFERENCES card_event_snapshots(event_snapshot_id),
    bout_id TEXT NOT NULL REFERENCES bouts(bout_id),
    fighter_a_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    fighter_a_name TEXT NOT NULL,
    fighter_b_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    fighter_b_name TEXT NOT NULL,
    bout_status TEXT NOT NULL
        CHECK (bout_status IN ('scheduled', 'completed', 'cancelled')),
    bout_provider_status TEXT,
    weight_class TEXT,
    outcome TEXT CHECK (outcome IN ('win', 'draw', 'no_contest')),
    winner_fighter_id TEXT REFERENCES fighters(fighter_id),
    method TEXT,
    CHECK (fighter_a_id <> fighter_b_id),
    CHECK ((outcome IS NULL AND winner_fighter_id IS NULL)
        OR (outcome = 'win' AND winner_fighter_id IS NOT NULL)
        OR (outcome IN ('draw', 'no_contest') AND winner_fighter_id IS NULL)),
    UNIQUE (event_snapshot_id, bout_id)
);

CREATE INDEX ix_card_event_snapshots_event_time
    ON card_event_snapshots(event_id, source_observed_at_utc, event_snapshot_id);
CREATE INDEX ix_card_bout_snapshots_bout
    ON card_bout_snapshots(bout_id, event_snapshot_id);

CREATE TRIGGER card_event_snapshots_no_update
BEFORE UPDATE ON card_event_snapshots
BEGIN
    SELECT RAISE(ABORT, 'card event snapshots are immutable');
END;

CREATE TRIGGER card_event_snapshots_no_delete
BEFORE DELETE ON card_event_snapshots
BEGIN
    SELECT RAISE(ABORT, 'card event snapshots are immutable');
END;

CREATE TRIGGER card_bout_snapshots_no_update
BEFORE UPDATE ON card_bout_snapshots
BEGIN
    SELECT RAISE(ABORT, 'card bout snapshots are immutable');
END;

CREATE TRIGGER card_bout_snapshots_no_delete
BEFORE DELETE ON card_bout_snapshots
BEGIN
    SELECT RAISE(ABORT, 'card bout snapshots are immutable');
END;
