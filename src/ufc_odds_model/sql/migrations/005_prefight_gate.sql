-- Every pre-fight decision keeps the exact source receipt and gate inputs.
-- Older ingestion receipts and paper entries predate this gate and remain
-- visible as legacy records; new paper entries require an accepted check.
ALTER TABLE ingestion_runs ADD COLUMN snapshot_at_utc TEXT;

CREATE TRIGGER ingestion_runs_no_update
BEFORE UPDATE ON ingestion_runs
BEGIN
    SELECT RAISE(ABORT, 'ingestion receipts are immutable');
END;

CREATE TRIGGER ingestion_runs_no_delete
BEFORE DELETE ON ingestion_runs
BEGIN
    SELECT RAISE(ABORT, 'ingestion receipts are immutable');
END;

CREATE TABLE prefight_gate_checks (
    gate_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    bout_id TEXT NOT NULL REFERENCES bouts(bout_id),
    prediction_id INTEGER NOT NULL REFERENCES predictions(prediction_id),
    quote_id INTEGER REFERENCES odds_quotes(quote_id),
    snapshot_at_utc TEXT NOT NULL,
    checked_at_utc TEXT NOT NULL,
    event_start_time_utc TEXT NOT NULL,
    model_version TEXT NOT NULL,
    model_cutoff_at_utc TEXT NOT NULL,
    bookmaker TEXT,
    bookmaker_updated_at_utc TEXT,
    quote_captured_at_utc TEXT,
    quoted_decimal_odds REAL,
    model_probability REAL,
    model_decision TEXT NOT NULL,
    gate_decision TEXT NOT NULL CHECK (gate_decision IN ('alert_candidate', 'reject')),
    gate_reason TEXT NOT NULL,
    max_age_seconds REAL NOT NULL CHECK (max_age_seconds > 0),
    decimal_odds_drift REAL NOT NULL CHECK (decimal_odds_drift >= 0),
    min_edge REAL NOT NULL CHECK (min_edge >= 0),
    conservative_decimal_odds REAL,
    conservative_expected_profit_per_dollar REAL,
    CHECK ((gate_decision = 'alert_candidate' AND gate_reason = 'ok' AND quote_id IS NOT NULL)
        OR gate_decision = 'reject')
);

CREATE INDEX ix_prefight_gate_event_checked
    ON prefight_gate_checks(event_id, checked_at_utc, gate_check_id);
CREATE INDEX ix_prefight_gate_prediction ON prefight_gate_checks(prediction_id);

CREATE TRIGGER prefight_gate_checks_no_update
BEFORE UPDATE ON prefight_gate_checks
BEGIN
    SELECT RAISE(ABORT, 'pre-fight gate checks are immutable');
END;

CREATE TRIGGER prefight_gate_checks_no_delete
BEFORE DELETE ON prefight_gate_checks
BEGIN
    SELECT RAISE(ABORT, 'pre-fight gate checks are immutable');
END;

ALTER TABLE paper_bets ADD COLUMN gate_check_id INTEGER REFERENCES prefight_gate_checks(gate_check_id);

CREATE TRIGGER paper_bets_require_accepted_gate
BEFORE INSERT ON paper_bets
WHEN NEW.gate_check_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM prefight_gate_checks g
    WHERE g.gate_check_id = NEW.gate_check_id
      AND g.gate_decision = 'alert_candidate'
      AND g.prediction_id = NEW.prediction_id
      AND g.quote_id = NEW.quote_id
      AND g.event_id = NEW.event_id
      AND g.bout_id = NEW.bout_id
      AND g.bookmaker = NEW.bookmaker
      AND g.quoted_decimal_odds = NEW.quoted_decimal_odds
      AND NEW.selection_fighter_id = (
          SELECT q.selection_fighter_id FROM odds_quotes q WHERE q.quote_id = NEW.quote_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'paper decision requires a matching accepted pre-fight gate check');
END;
