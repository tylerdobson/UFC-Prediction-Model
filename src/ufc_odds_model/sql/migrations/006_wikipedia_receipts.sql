-- A source revision and license are auditable alongside every research import.
CREATE TABLE IF NOT EXISTS wikipedia_source_receipts (
    run_id INTEGER PRIMARY KEY REFERENCES ingestion_runs(run_id),
    event_page_id INTEGER NOT NULL,
    event_title TEXT NOT NULL,
    page_url TEXT NOT NULL,
    revision_id INTEGER NOT NULL,
    revision_timestamp_utc TEXT NOT NULL,
    license_title TEXT NOT NULL,
    license_url TEXT NOT NULL,
    imported_bouts INTEGER NOT NULL CHECK (imported_bouts >= 0),
    skipped_unresolved_bouts INTEGER NOT NULL CHECK (skipped_unresolved_bouts >= 0)
);
