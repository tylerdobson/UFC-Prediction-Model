-- Simulated decisions are separate from the manual, actually placed bets ledger.
-- Freeze the prediction, quote, and stake used for each paper decision.
CREATE TABLE IF NOT EXISTS paper_bets (
    paper_bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL UNIQUE REFERENCES predictions(prediction_id),
    quote_id INTEGER NOT NULL REFERENCES odds_quotes(quote_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    bout_id TEXT NOT NULL REFERENCES bouts(bout_id),
    selection_fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    bookmaker TEXT NOT NULL,
    decision_at_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    quote_captured_at_utc TEXT NOT NULL,
    quoted_decimal_odds REAL NOT NULL CHECK (quoted_decimal_odds > 1.0),
    model_probability REAL NOT NULL CHECK (model_probability >= 0 AND model_probability <= 1),
    expected_profit_per_unit REAL NOT NULL,
    bankroll_units REAL NOT NULL CHECK (bankroll_units > 0),
    per_bet_cap_units REAL NOT NULL CHECK (per_bet_cap_units > 0),
    event_cap_units REAL NOT NULL CHECK (event_cap_units > 0),
    stake_units REAL NOT NULL CHECK (stake_units > 0),
    settlement_status TEXT NOT NULL DEFAULT 'open'
        CHECK (settlement_status IN ('open', 'won', 'lost', 'pending_review')),
    payout_units REAL CHECK (payout_units >= 0),
    status_updated_at_utc TEXT,
    settled_at_utc TEXT,
    settlement_note TEXT
);

CREATE INDEX IF NOT EXISTS ix_paper_bets_event ON paper_bets(event_id);
CREATE INDEX IF NOT EXISTS ix_paper_bets_bout ON paper_bets(bout_id);
