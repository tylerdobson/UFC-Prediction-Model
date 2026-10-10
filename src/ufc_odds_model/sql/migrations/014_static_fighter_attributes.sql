-- Static research attributes never replace dated observations or fighter names.
CREATE TABLE fighter_static_attributes (
    static_attribute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    attribute TEXT NOT NULL CHECK (attribute IN ('birth_date','height_cm','ufc_athlete_id','wikidata_qid')),
    value TEXT NOT NULL CHECK (length(value) > 0),
    source TEXT NOT NULL CHECK (source = 'wikidata'),
    license TEXT NOT NULL CHECK (license = 'https://creativecommons.org/publicdomain/zero/1.0/'),
    ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(run_id),
    UNIQUE (fighter_id, attribute, source, ingestion_run_id)
);
CREATE INDEX ix_static_fighter ON fighter_static_attributes(fighter_id, attribute);
CREATE TRIGGER static_attributes_no_update BEFORE UPDATE ON fighter_static_attributes
BEGIN SELECT RAISE(ABORT, 'static fighter attributes are immutable'); END;
CREATE TRIGGER static_attributes_no_delete BEFORE DELETE ON fighter_static_attributes
BEGIN SELECT RAISE(ABORT, 'static fighter attributes are immutable'); END;

CREATE TABLE wikidata_profile_queries (
    ingestion_run_id INTEGER PRIMARY KEY REFERENCES ingestion_runs(run_id),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json TEXT NOT NULL
);
CREATE INDEX ix_profile_query_hash ON wikidata_profile_queries(request_sha256);
CREATE TRIGGER profile_queries_no_update BEFORE UPDATE ON wikidata_profile_queries
BEGIN SELECT RAISE(ABORT, 'Wikidata query receipts are immutable'); END;
CREATE TRIGGER profile_queries_no_delete BEFORE DELETE ON wikidata_profile_queries
BEGIN SELECT RAISE(ABORT, 'Wikidata query receipts are immutable'); END;
