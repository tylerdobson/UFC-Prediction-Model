# MySQL runtime and migration contract

MySQL 8.4 is the default and required runtime database. Normal CLI commands, scheduled refreshes, model research, dashboard, rankings and forward tracking use native MySQL tables. PyMySQL is a core dependency. No database outage triggers a SQLite fallback.

## Initialize and migrate

```sh
uv sync --frozen --all-extras
. .venv/bin/activate
.venv/bin/python scripts/local_mysql.py status
ufc-mysql init
# Existing local archive only; original source files remain unchanged.
ufc-mysql bootstrap --project-root . --receipt output/NEW-MIGRATION/receipt.json
ufc-mysql inspect mysql:statistics
ufc-pipeline refresh
ufc-mysql rankings
streamlit run app.py --server.address 127.0.0.1
```

For a clean installation, [configure MySQL](LOCAL_MYSQL.md), initialize it, run `./scripts/demo.sh`, and refresh sources. Bootstrap requires the ignored retained local research/statistics evidence; a clone does not contain those private inputs. An explicit `ufc-mysql import-legacy SOURCE --dataset NEW-NAME --role research` imports another retained source once. Import verifies original byte SHA-256, logical row hashes, row counts, primary keys and foreign keys. Originals are opened read-only and preserved. Repeated imports verify and reuse the same immutable version.

## Data boundaries

| Handle | Purpose |
|---|---|
| `mysql:operating` | Mutable operating schema, quotes, source receipts, predictions and separate paper/actual ledgers |
| `mysql:research-complete` | Immutable imported research labels and dated/static profile evidence |
| `mysql:statistics` | Alias for the latest quality-approved canonical archive |
| `mysql:statistics-v6` | Pinned archive used by the frozen V6 and first V7 experiment |
| `mysql:forecast` | Append-only frozen forecast/settlement chain |
| `mysql:intake-ufc332` | Retained older intake evidence, isolated from current operating records |
| `mysql:synthetic-demo-…` | Explicit synthetic demo namespace; never activated as the real archive |

Physical `rt_<namespace-hash>_<table>` tables preserve each dataset's schema, original identifiers and logical rows. The runtime catalog records role, schema hash, content hash, source hash and build/ready state. A narrow query facade translates existing parameterized repository queries; all normal execution and relational constraints are native MySQL. Published immutable datasets have database guards against inserts, updates and deletes. Scratch forecast input tables are connection-local MySQL temporary tables.

`ufc_app_document` retains immutable versioned JSON payloads for rankings, official portrait metadata, model evaluations, forecast editions and descriptive context. `ufc_app_document_active` selects a current edition. Document hashes bind kind, key, capture time, payload and source. Published forecast exports must match this payload and their release hashes before the UI offers downloads.

The forecast ledger stores hash-linked entries and a locked tail in `ufc_app_forecast_entry`/`ufc_app_forecast_chain`. Per-ledger writer locks serialize initial creation and appends. Imported older records preserve their recorded times; a new freeze uses its actual registration time. A saved report recovered after an event is labeled accordingly. Forecasts are not wagers.

## Refresh publication

Raw CSVs normalize directly into a new MySQL version. Missing optional fields remain null and ambiguous identities/incomplete paired rounds remain held. Nonempty and retained-coverage checks run before publication. The statistics alias and its publication receipt commit together. Warehouse analytical views use a separate validated transaction; replay reconciles an interrupted refresh between these steps. DDL runs before publication transactions. A failed build or quality gate leaves the previous statistics alias available.

Current official rankings refresh alongside the archive. A separately verified portrait manifest covers each exact ranked athlete URL; the worker reuses it for up to seven days while source/roster bindings match, then captures official images again. Portraits are display evidence and are never historical model inputs. Source publication dates and capture time remain separate. Meta and media rankings are distinct; tied published ranks are preserved. Current ranking snapshots are not historical model inputs.

## Model experiment reproducibility

The first V7 experiment ran entirely against imported native MySQL sources:

```sh
python -m ufc_odds_model.research_v7 \
  --database mysql:research-complete --statistics mysql:statistics-v6 \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --output-dir reports/NEW-V7-EDITION
```

Use a new edition directory. Protocol → pretest selection → evaluation is exclusive and hash verified. Existing files are never overwritten. The retained 43-feature candidate did not beat V6 on the reused fixed benchmark; there is no automatic promotion. Historical source capture, incomplete reach and uncertain identity remain material limitations.

## Recovery and privileges

[Backups](BACKUP_RUNBOOK.md) include the selected dataset plus global delivery documents and forecast chains; [portable evidence bundles](EVIDENCE_BUNDLE.md) also retain referenced source artifacts. Restore requires a new namespace and does not replace live aliases or global delivery pointers automatically.

The app user has database-scoped privileges. MySQL binary-log trigger restrictions may require a separate schema installer; the local helper permits only narrowly validated project trigger DDL using the owner-private project admin socket. It does not grant SUPER or change global server settings. For another server, use a DBA/schema account through the supported installer configuration.

Explicit legacy SQLite commands and tests remain for migration compatibility and frozen experiment reproduction. They are not normal defaults. Raw source files, JSON run receipts, report exports and private configuration remain filesystem artifacts.
