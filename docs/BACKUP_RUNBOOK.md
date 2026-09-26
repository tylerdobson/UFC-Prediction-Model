# SQLite backup and restore drill

Run these commands from the repository root after installing the project. The backup module uses SQLite's online backup API, so committed rows still in a write-ahead log (WAL) are included. It saves a **single** SQLite file, restores that file to a temporary location, and checks `integrity_check`, `foreign_key_check`, the schema, and counts for every application table. A failed check leaves no published backup. It never replaces an existing backup or recovery file.

## Rehearse without real source data

This fixture has the current schema and no fight or odds data. It is safe to create and remove:

```bash
drill_dir=$(mktemp -d)
ufc-model --db "$drill_dir/fixture.sqlite" init-db
python -m ufc_odds_model.backup create --db "$drill_dir/fixture.sqlite" --output "$drill_dir/fixture-backup.sqlite"
python -m ufc_odds_model.backup verify --backup "$drill_dir/fixture-backup.sqlite"
python -m ufc_odds_model.backup restore --backup "$drill_dir/fixture-backup.sqlite" --output "$drill_dir/fixture-restored.sqlite"
python -m ufc_odds_model.backup verify --backup "$drill_dir/fixture-restored.sqlite"
```

All four backup commands must report `"ok": true`. The reports include a SHA-256, table counts, and `temporary_restore_matches` or `backup_counts_match`. The fixture proves the backup mechanism, not source rights, model quality, or event readiness. Remove the temporary directory after reviewing the reports.

The focused automated drill is:

```bash
python -m unittest discover -s tests -p test_backup.py -v
```

It also checks an uncheckpointed WAL row, corrupt backup rejection, broken foreign key rejection, and refusal to overwrite existing files.

## Back up the operating database

Use a unique output path before each source import, migration, or event-day session. The destination is created with private file permissions and cannot already exist:

```bash
backup_file="backups/ufc-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen).sqlite"
python -m ufc_odds_model.backup create --db data/ufc.sqlite --output "$backup_file"
python -m ufc_odds_model.backup verify --backup "$backup_file"
```

Keep the printed JSON report with the backup, or record its SHA-256 and row counts in the operator log. Keep backups outside Git and restrict access: the database may contain a manual bet ledger. The SQLite file alone does **not** preserve retained raw source payloads, model artifacts, or evaluation reports. Use the [portable evidence bundle](EVIDENCE_BUNDLE.md) when you need a recoverable copy of those files with the database. Never include provider API keys in an archive.

## Upgrade an existing database

Stop imports, alert checks, settlement commands, and the dashboard before upgrading the project code or schema. Ordinary CLI commands refuse an older schema; `init-db` only initializes a new or empty database. To apply pending migrations, supply a **new** backup path:

```bash
backup_file="backups/ufc-before-migration-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen).sqlite"
ufc-model --db data/ufc.sqlite migrate --backup "$backup_file"
```

`migrate` creates a standalone SQLite backup, drills a restore, verifies the published backup, and compares its hash, schema, and table counts before it applies SQL. If creation or verification fails, no migration is applied. The command prints the backup path before applying the migrations so you can recover from a later migration error. Preserve that path and the pre-upgrade code version in the operator log. After success, run the source-integrity check and `ufc-model audit` before event work. A database already at the current schema does not need another backup or migration.

## Recover to a new path

Stop imports, CLI jobs, and the dashboard before switching databases. Preserve the failed database and its WAL/SHM sidecars together for investigation. If a migration failed, use the verified pre-migration backup with the matching older code version. Choose the reviewed backup and restore to a **new** path:

```bash
python -m ufc_odds_model.backup verify --backup backups/SELECTED.sqlite
python -m ufc_odds_model.backup restore --backup backups/SELECTED.sqlite --output data/ufc-recovered.sqlite
python -m ufc_odds_model.integrity --db data/ufc-recovered.sqlite
ufc-model --db data/ufc-recovered.sqlite audit
```

If source payload files were archived separately, restore them to the paths recorded in `ingestion_runs` before running the evidence check. A [portable evidence bundle](EVIDENCE_BUNDLE.md) instead recovers payloads into a new directory and rebinds the restored database's receipt paths. Review any integrity or audit issue before operating. Point the CLI to the recovered database with `--db data/ufc-recovered.sqlite` and set `UFC_MODEL_DB=data/ufc-recovered.sqlite` for the dashboard. The restore command will fail if that output path already exists; use another new path. Do not copy a live SQLite main file without its WAL or replace a database while a writer is running.

## What the drill proves

It proves that the saved SQLite file can be opened and restored with matching schema and row counts, and that SQLite reports no structural or foreign key problems at verification time. It does not validate whether the original data was accurate, rights-cleared, current, or complete. Run the separate source receipt and domain audits before model decisions.
