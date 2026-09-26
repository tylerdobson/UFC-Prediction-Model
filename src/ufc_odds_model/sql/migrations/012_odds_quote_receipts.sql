-- A quote may be observed in more than one immutable API response. Preserve
-- every exact response used to admit it, including idempotent reimports.
CREATE TABLE IF NOT EXISTS odds_quote_receipts (
    quote_id INTEGER NOT NULL REFERENCES odds_quotes(quote_id),
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (quote_id, ingestion_run_id)
);

CREATE INDEX IF NOT EXISTS ix_odds_quote_receipts_run
    ON odds_quote_receipts(ingestion_run_id, quote_id);

CREATE TRIGGER IF NOT EXISTS odds_quote_receipts_no_update
BEFORE UPDATE ON odds_quote_receipts
BEGIN
    SELECT RAISE(ABORT, 'odds quote receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS odds_quote_receipts_no_delete
BEFORE DELETE ON odds_quote_receipts
BEGIN
    SELECT RAISE(ABORT, 'odds quote receipts are immutable');
END;
