PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS fighters (
    fighter_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    source TEXT,
    source_fighter_id TEXT,
    UNIQUE (source, source_fighter_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT,
    source_event_id TEXT,
    name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    start_time_utc TEXT,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'completed', 'cancelled')),
    UNIQUE (source, source_event_id)
);

CREATE TABLE IF NOT EXISTS bouts (
    bout_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    source TEXT,
    source_bout_id TEXT,
    fighter_a_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    fighter_b_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    weight_class TEXT,
    scheduled_rounds INTEGER,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'completed', 'cancelled')),
    CHECK (fighter_a_id <> fighter_b_id),
    UNIQUE (source, source_bout_id)
);

CREATE TABLE IF NOT EXISTS results (
    bout_id TEXT PRIMARY KEY REFERENCES bouts(bout_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('win', 'draw', 'no_contest')),
    winner_fighter_id TEXT REFERENCES fighters(fighter_id),
    method TEXT,
    recorded_at_utc TEXT NOT NULL,
    CHECK ((outcome = 'win' AND winner_fighter_id IS NOT NULL)
        OR (outcome <> 'win' AND winner_fighter_id IS NULL))
);

CREATE TABLE IF NOT EXISTS odds_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bout_id TEXT NOT NULL REFERENCES bouts(bout_id),
    bookmaker TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market = 'h2h'),
    selection_fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    decimal_odds REAL NOT NULL CHECK (decimal_odds > 1.0),
    bookmaker_updated_at_utc TEXT,
    captured_at_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE (bout_id, bookmaker, selection_fighter_id, captured_at_utc, decimal_odds)
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bout_id TEXT NOT NULL REFERENCES bouts(bout_id),
    model_version TEXT NOT NULL,
    feature_cutoff_at_utc TEXT NOT NULL,
    generated_at_utc TEXT NOT NULL,
    p_fighter_a REAL NOT NULL CHECK (p_fighter_a >= 0 AND p_fighter_a <= 1),
    UNIQUE (bout_id, model_version, feature_cutoff_at_utc)
);

CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(prediction_id),
    quote_id INTEGER NOT NULL REFERENCES odds_quotes(quote_id),
    selection_fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    placed_at_utc TEXT NOT NULL,
    actual_decimal_odds REAL NOT NULL CHECK (actual_decimal_odds > 1.0),
    stake REAL NOT NULL CHECK (stake > 0),
    settlement_status TEXT NOT NULL DEFAULT 'open'
        CHECK (settlement_status IN ('open', 'won', 'lost', 'push', 'void')),
    payout REAL CHECK (payout >= 0),
    settled_at_utc TEXT
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    payload_path TEXT NOT NULL,
    sha256 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_events_date ON events(event_date);
CREATE INDEX IF NOT EXISTS ix_bouts_event ON bouts(event_id);
CREATE INDEX IF NOT EXISTS ix_quotes_lookup
    ON odds_quotes(bout_id, selection_fighter_id, captured_at_utc);
CREATE INDEX IF NOT EXISTS ix_predictions_bout ON predictions(bout_id, generated_at_utc);
