-- Explicit reviewed cross-provider identity links. Never merge fighters by name.
CREATE TABLE IF NOT EXISTS fighter_external_ids (
    source TEXT NOT NULL,
    source_fighter_id TEXT NOT NULL,
    fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    PRIMARY KEY (source, source_fighter_id)
);
CREATE INDEX IF NOT EXISTS ix_fighter_external_ids_fighter
    ON fighter_external_ids(fighter_id);
