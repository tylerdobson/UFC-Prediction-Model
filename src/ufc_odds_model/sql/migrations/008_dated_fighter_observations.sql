-- Reviewed, source-dated observations are separate from mutable fighter names
-- and results. An observation is eligible only before a prediction decision.
ALTER TABLE predictions ADD COLUMN feature_coverage_json TEXT;

CREATE TABLE fighter_profile_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    source TEXT NOT NULL CHECK (length(source) > 0),
    source_evidence_uri TEXT NOT NULL CHECK (length(source_evidence_uri) > 0),
    source_license_uri TEXT NOT NULL CHECK (length(source_license_uri) > 0),
    observed_at_utc TEXT NOT NULL,
    birth_date TEXT,
    reach_cm REAL CHECK (reach_cm IS NULL OR (reach_cm >= 100 AND reach_cm <= 250)),
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    CHECK (birth_date IS NOT NULL OR reach_cm IS NOT NULL),
    UNIQUE (fighter_id, source, observed_at_utc)
);
CREATE INDEX ix_fighter_profile_asof
    ON fighter_profile_observations(fighter_id, observed_at_utc);

CREATE TABLE fight_stat_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bout_id TEXT NOT NULL REFERENCES bouts(bout_id),
    fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    source TEXT NOT NULL CHECK (length(source) > 0),
    source_evidence_uri TEXT NOT NULL CHECK (length(source_evidence_uri) > 0),
    source_license_uri TEXT NOT NULL CHECK (length(source_license_uri) > 0),
    observed_at_utc TEXT NOT NULL,
    sig_strikes_landed INTEGER NOT NULL CHECK (sig_strikes_landed >= 0),
    sig_strikes_attempted INTEGER NOT NULL CHECK (
        sig_strikes_attempted >= 0 AND sig_strikes_landed <= sig_strikes_attempted
    ),
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    UNIQUE (bout_id, fighter_id, source, observed_at_utc)
);
CREATE INDEX ix_fight_stats_asof
    ON fight_stat_observations(bout_id, fighter_id, observed_at_utc);

CREATE TRIGGER fighter_profile_observations_no_update
BEFORE UPDATE ON fighter_profile_observations
BEGIN
    SELECT RAISE(ABORT, 'fighter profile observations are immutable');
END;
CREATE TRIGGER fighter_profile_observations_no_delete
BEFORE DELETE ON fighter_profile_observations
BEGIN
    SELECT RAISE(ABORT, 'fighter profile observations are immutable');
END;
CREATE TRIGGER fight_stat_observations_no_update
BEFORE UPDATE ON fight_stat_observations
BEGIN
    SELECT RAISE(ABORT, 'fight stat observations are immutable');
END;
CREATE TRIGGER fight_stat_observations_no_delete
BEFORE DELETE ON fight_stat_observations
BEGIN
    SELECT RAISE(ABORT, 'fight stat observations are immutable');
END;

CREATE TRIGGER fight_stat_observations_participant
BEFORE INSERT ON fight_stat_observations
WHEN NOT EXISTS (
    SELECT 1 FROM bouts b
    WHERE b.bout_id = NEW.bout_id
      AND NEW.fighter_id IN (b.fighter_a_id, b.fighter_b_id)
      AND b.status = 'completed'
)
BEGIN
    SELECT RAISE(ABORT, 'fight stat fighter must participate in a completed bout');
END;
