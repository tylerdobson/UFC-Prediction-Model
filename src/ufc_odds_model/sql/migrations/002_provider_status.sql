-- Keep the provider's more detailed state without rewriting settled outcomes.
-- Only not_started/scheduled are eligible for a new event prediction.
ALTER TABLE events ADD COLUMN provider_status TEXT;
ALTER TABLE bouts ADD COLUMN provider_status TEXT;
