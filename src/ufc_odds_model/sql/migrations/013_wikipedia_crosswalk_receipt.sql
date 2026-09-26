-- Bind every research event recount to the exact reviewed crosswalk snapshot
-- used by that import. Existing source receipts remain immutable and unlinked.
ALTER TABLE wikipedia_source_receipts
ADD COLUMN crosswalk_run_id INTEGER REFERENCES ingestion_runs(run_id);
