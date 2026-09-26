# Portable decision evidence recovery

An SQLite backup alone cannot reproduce a decision when its ingestion receipts
point to raw files elsewhere. The evidence bundle archives a verified SQLite
snapshot, **every** receipt payload, and the files currently under `models/`
and `reports/`. The archive retains the original database unchanged. Recovery
creates a new database and changes only its receipt paths to the extracted
payloads. The recovery report records each original and new path.

## Create and verify

Stop imports and event jobs while making a bundle so the raw files and model
artifacts represent one reviewed operating point. Use a new output name every
time:

```bash
bundle="backups/ufc-evidence-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen).zip"
python -m ufc_odds_model.evidence_bundle create \
  --db data/ufc.sqlite --project-root . --models models --reports reports \
  --output "$bundle"
python -m ufc_odds_model.evidence_bundle verify --bundle "$bundle"
```

Creation refuses a missing or changed receipt payload. Verification hashes
each archived file, checks that the receipt set matches the archived database,
and drills a SQLite restore with structural and foreign-key checks. The bundle
is a private local file and may contain a manual betting ledger and licensed
source data. Store it outside Git and restrict access. Keep provider secrets
outside the bundle; files named `.env` or `secrets.toml` in artifact directories
cause creation to fail.

## Recover without overwriting

Stop jobs first and preserve the old database plus its WAL/SHM files for
investigation. Recover to a **new** directory:

```bash
python -m ufc_odds_model.evidence_bundle restore \
  --bundle backups/SELECTED.zip --output recovered/ufc-YYYYMMDD
python -m ufc_odds_model.integrity \
  --db recovered/ufc-YYYYMMDD/database.sqlite
ufc-model --db recovered/ufc-YYYYMMDD/database.sqlite audit
```

The directory contains `database.sqlite`, `payloads/`, optional `models/` and
`reports/`, and `restore_report.json`. Receipt immutability triggers are
temporarily removed **only in that new database copy**, then restored in the
same transaction after paths are rebound. The original archived database is
never changed. A recovery of an older schema may still need the explicit
backup-gated migration before the current CLI can operate it. Regenerate the
integrity and evaluation reports for the new database path; archived reports
remain evidence of the earlier run and may be stale after recovery.

This drill proves recoverability and byte integrity. It does not establish
source rights, factual accuracy, executable odds, model calibration, or an
event-day result. Review those separately before any decision.
