# Portable evidence recovery

The MySQL evidence bundle combines a private logical backup with recorded source
payloads and exported artifacts. It preserves the selected dataset and all
global delivery documents, active-pointer records and forecast chains without
creating a SQLite copy.

## Create and verify

Pause imports, publications and ledger writes for a coordinated operating point.
Use a new output name:

```sh
bundle="backups/ufc-evidence-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen).zip"
python -m ufc_odds_model.evidence_bundle create \
  --db mysql:operating --project-root . --models models --reports reports \
  --output "$bundle"
python -m ufc_odds_model.evidence_bundle verify --bundle "$bundle"
```

Format 2 contains `database.mysql.json`, payloads and optional `models/` and
`reports/` files. The recorded payload set includes ingestion receipts; for a
statistics dataset it includes its source files and pinned source manifest. It
also includes exact saved forecast files referenced by the global forecast
ledger. Creation refuses recorded payloads that are missing, linked or changed.
The archive is not an indiscriminate copy of all `data/raw/` or `output/` files;
retain additional captures and report directories separately when needed.

Verification hashes every archived file, rejects unsafe/duplicate paths, checks
that payload identities and hashes match the embedded receipts, and verifies
the logical dataset, document identities and forecast chains offline. It does
not claim a native restore drill until recovery is actually run against MySQL.
The selected dataset and global delivery snapshot are each transactionally
consistent, captured in separate transactions.

The archive is owner-private and may contain manual ledger data and licensed
source material. Keep it outside Git. Do not add credential directories to the
artifact paths; `.env` and `secrets.toml` files there are rejected. Project
connection configuration remains outside the bundle.

## Recover without overwriting

Prepare the configured MySQL server and stop writers while reviewing recovery.
Choose both a new directory and a new unused namespace:

```sh
python -m ufc_odds_model.evidence_bundle restore \
  --bundle backups/SELECTED.zip \
  --output recovered/ufc-20261020 \
  --mysql-target mysql:recovery-operating-20261020
python -m ufc_odds_model.integrity \
  --db mysql:recovery-operating-20261020 \
  --project-root recovered/ufc-20261020
ufc-model --db mysql:recovery-operating-20261020 audit
```

The directory contains the original logical backup, extracted payloads, optional
artifacts, `receipt_paths.json` and `restore_report.json`. Extracted files are
private. The recovery index binds receipt identities and original hashes to the
new payload paths and the restored dataset's content/schema hashes. **Database
receipts are not rewritten** and immutability triggers are not removed.

The restore creates the selected dataset plus a separate delivery recovery
namespace. The result reports its exact `delivery_recovery_target`; the native
schema, content and forecast chains are verified again. Live global delivery
tables, aliases and the dashboard's active publications remain unchanged.
Activation requires separate reviewed maintenance. A failed recovery can leave
new namespaces and extracted evidence for diagnosis rather than deleting READY
data.

Back up other required research/statistics datasets separately. This is not a
server-wide restore of users, credentials, aliases or analytical views. Preserve
additional raw captures and server recovery configuration independently. Archived
reports describe their original run; regenerate current integrity/audit results
before resuming writes.

## Legacy archives

Explicit SQLite format-1 bundles remain readable for historical reproduction.
They restore `database.sqlite` into a new directory and record receipt-path
rebinding in that copy. They contain no native MySQL delivery archive. Omit
`--mysql-target` for that legacy path. Normal commands use MySQL format 2.

See the [backup runbook](BACKUP_RUNBOOK.md) for scope, physical-check limits and
the opt-in native restore test. Recovery proves retained bytes and relationships;
it does not establish factual accuracy, completeness or predictive performance.
