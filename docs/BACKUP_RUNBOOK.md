# MySQL backup and recovery drill

Run these commands from the repository root with the project environment active
and [MySQL configured](LOCAL_MYSQL.md). Normal storage is native MySQL; a server
failure does not select a SQLite file.

## Create and verify a private backup

Use a unique filename before imports or maintenance:

```sh
backup_file="backups/ufc-operating-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen).mysql.json"
python -m ufc_odds_model.backup create \
  --db mysql:operating --output "$backup_file"
python -m ufc_odds_model.backup verify --backup "$backup_file"
```

The owner-private JSON file contains the selected logical dataset's retained
schema, metadata, ordered rows and checksums. It also contains all four global
delivery tables: immutable documents, active document pointers, forecast chains
and forecast entries. Document payload/identity hashes, active references and
forecast chain/index/tail consistency are verified. No connection passwords or
private configuration files are added by the backup module.

Creation checks actual MySQL table engines, ordered column types/nullability,
primary and foreign keys, required unique constraints and runtime guards. It
also runs real foreign-key anti-joins. The report names the checked scope;
column-default semantics, arbitrary source-trigger bodies and CHECK-expression
equivalence are not certified by that physical inspection.

`verify` works offline. It checks the envelope, retained schema, every logical
row, keys and global delivery evidence. It reports
`native_restore_drill_performed: false`: a real native restore is the separate
step below. The dataset and delivery archive each use a consistent transaction;
they are captured in separate transactions. Pause writers for a coordinated
recovery point across components.

Keep the report/checksum and code revision with the backup. The file must remain
owned by the current user with mode `0600`; verification refuses symlinks and
group/world-readable files. Existing destinations are never replaced. Store
backups outside Git and on a separate protected volume when required.

## Back up each required dataset

A backup covers **one logical dataset plus global delivery evidence**, not the
entire MySQL server. Back up `mysql:research-complete`, the current
`mysql:statistics` and pinned `mysql:statistics-v6` separately when they are
required for reproduction. Use distinct filenames and retain each resolved
dataset identity and content hash. Unrelated namespaces, server users, secrets,
runtime aliases and warehouse analytical views are not a server-wide backup.

Raw files, model files and report exports are not included in the smaller JSON
backup. Use an [evidence bundle](EVIDENCE_BUNDLE.md) to preserve recorded payloads
and report artifacts as well. Keep additional source captures and server
configuration in their own reviewed recovery plan.

## Restore into new namespaces

Stop writers while reviewing a recovery. Preserve the failed instance and its
diagnostics. Choose a new, unused namespace; the command refuses existing
datasets and aliases:

```sh
python -m ufc_odds_model.backup verify --backup backups/SELECTED.mysql.json
python -m ufc_odds_model.backup restore \
  --backup backups/SELECTED.mysql.json --output mysql:recovery-operating-20261020
ufc-mysql inspect mysql:recovery-operating-20261020
python -m ufc_odds_model.integrity \
  --db mysql:recovery-operating-20261020 --project-root .
ufc-model --db mysql:recovery-operating-20261020 audit
```

Restore checks the new native schema and re-exports its rows to compare content,
schema and table counts. It also restores global delivery evidence into a
separate recovery-only namespace. Read the exact `delivery_recovery_target` in
the result rather than assuming a physical table name. Documents, active-pointer
rows and forecast chains can be inspected there without replacing live global
tables.

Neither the statistics alias nor live document/forecast pointers are activated.
Moving reviewed recovery data into service is a separate maintenance operation;
this command is not a live failover tool. A failed post-restore check may leave
new recovery namespaces for diagnosis. It never drops READY data as cleanup.
Receipt verification still needs the original files or the recovered payloads
from an evidence bundle.

## Schema maintenance and verification

Create and verify the backup before changing schema or code. `ufc-mysql init`
initializes native operating/delivery schemas. `ufc-model --db mysql:operating
migrate --backup NEW.mysql.json` takes a new backup before the native schema
check; do not treat it as a general in-place MySQL schema-upgrade engine. Follow
the reviewed migration for that release and verify source receipts and domain
audits before resuming writes.

The focused tests are:

```sh
python -B -m unittest tests.test_mysql_backup tests.test_backup \
  tests.test_integrity tests.test_evidence_bundle -v
# Opt-in: requires initialized native operating and global delivery tables.
UFC_MYSQL_BACKUP_INTEGRATION=1 python -B -m unittest \
  tests.test_mysql_backup.NativeMySQLBackupTests -v
```

The native test creates new recovery namespaces. Run it against the intended
test instance and retain its receipts. A successful drill proves recoverability
and byte/relational integrity at that point, not factual accuracy, completeness
or model quality.

## Explicit legacy SQLite reproduction

Retained historical SQLite inputs remain supported when supplied explicitly:

```sh
python -m ufc_odds_model.backup create \
  --db path/to/retained.sqlite --output backups/UNIQUE.sqlite
python -m ufc_odds_model.backup restore \
  --backup backups/UNIQUE.sqlite --output recovered/NEW.sqlite
```

That compatibility path uses SQLite's online backup API, includes committed WAL
content and drills structural/FK/schema/count checks. It does not include MySQL
delivery tables. Keep frozen inputs unchanged; use the MySQL workflow above for
normal operations.
