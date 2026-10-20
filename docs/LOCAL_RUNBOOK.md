# Local operator runbook

Normal CLI, model research, dashboard, rankings and forecast tracking use native
MySQL. The dashboard reads committed evidence; CLI commands perform explicit
imports, model evaluations and ledger writes. Scheduled source failures use local
Mac notifications. No component places a wager.

## Set up the local runtime

Use Python 3.11 or newer from the repository root:

```sh
uv sync --frozen --all-extras
.venv/bin/python scripts/local_mysql.py setup
.venv/bin/python scripts/local_mysql.py install-service
.venv/bin/python scripts/local_mysql.py status
.venv/bin/ufc-mysql init
./scripts/demo.sh
```

The [project MySQL guide](LOCAL_MYSQL.md) describes the separate loopback instance
on port 3307, its owner-private configuration and recovery credentials. Existing
server users can configure a dedicated database instead. MySQL outages fail
clearly; local SQLite files are never selected as fallback.

The demo uses a separate, explicitly synthetic MySQL namespace. It neither
activates the real archive nor establishes a market signal. For existing retained
local research/report evidence, follow [MySQL bootstrap](MYSQL_RUNTIME.md):

```sh
.venv/bin/ufc-mysql bootstrap --project-root . \
  --receipt output/NEW-MIGRATION/receipt.json
.venv/bin/ufc-pipeline refresh
.venv/bin/ufc-pipeline status
.venv/bin/streamlit run app.py --server.address 127.0.0.1
```

Bootstrap requires the ignored retained inputs and fails clearly when they are
absent from a clean clone. It verifies/imports originals read-only; it is not a
source download command. A fresh installation can use the native synthetic demo
and then configure/capture real sources. Keep raw evidence, backups and private
configuration outside Git; preserve versioned release/report artifacts according
to the repository's existing policy.

## Know the data boundaries

| Handle | Purpose |
|---|---|
| `mysql:operating` | Current source receipts, cards, quotes, predictions and separate paper/actual ledgers |
| `mysql:research-complete` | Immutable research labels and retained profile evidence |
| `mysql:statistics` | Latest quality-approved archive alias |
| `mysql:statistics-v6` | Pinned archive for frozen model reproduction |
| `mysql:forecast` | Hash-chained frozen forecasts and later settlements |

Research history is retrospective evidence. A current official athlete fact or
ranking is descriptive context unless its historical publication timing has been
verified. Refreshing sources does not retrain a model or promote a new candidate.
Inspect [model/runtime evidence](MYSQL_RUNTIME.md) and the saved evaluations before
using an edition. Forecast records, paper decisions and actual wagers are
separate records; an empty actual-wager ledger stays empty.

## Configure real sources

Keep source/API credentials separate from database connection settings. Source
adapters read their documented environment variables and the CLI's private
`.env` file; keep that file outside Git with mode `0600`. Never paste keys into
reports, screenshots or chat. Review the [source decision record](DATA_SOURCE_DECISION.md)
and each account's permitted use before collection or publication. Source limits
and rights are separate from database setup and model performance.

Retained [Wikipedia history](WIKIPEDIA_HISTORY_SOURCE.md) and static profile data
remain research evidence with their own receipts. Use explicit MySQL research
namespaces and preserve originals when reproducing those imports. A later fetch
or imported timestamp does not establish what was known before a past fight.

## Refresh and inspect evidence

```sh
.venv/bin/ufc-pipeline refresh
.venv/bin/ufc-pipeline status
.venv/bin/ufc-mysql inspect mysql:statistics
.venv/bin/ufc-mysql rankings
.venv/bin/python -m ufc_odds_model.integrity \
  --db mysql:operating --project-root . --output reports/integrity.json
.venv/bin/ufc-model --db mysql:operating audit
```

The pipeline pins the upstream source commit, normalizes CSVs directly into a
new MySQL version, checks coverage and publishes its receipt. The statistics alias
and receipt commit together; analytical warehouse views publish in a separate
transaction. Run logs identify interrupted stages. Replaying the same inputs
verifies/reuses the version. Optional missing statistics stay null and held
identity/round records remain visible as gaps.

Official rankings keep Meta and media sets separate, preserve ties and expose
source update labels separately from capture time. Current ranks are not inputs
to historical model evaluation. The [pipeline runbook](PIPELINE_OPERATIONS.md)
covers the existing daily 09:00 local schedule and failure notifications.

Source-integrity checks inspect actual MySQL schema/FKs and hash retained files.
An unavailable file, changed hash or unresolved identity needs review before its
rows are relied on. The check does not turn retrospective data into verified
pre-fight evidence.

## Before and after a card

1. Import a reviewed, dated roster/results source with `ufc-model --db
   mysql:operating import-csv PATH`. Check replacements, cancellations, event
   identity and UTC start time. Use the [CSV template](../examples/bouts_template.csv)
   and [dated observation guide](POINT_IN_TIME_OBSERVATIONS.md) for attribution
   and publication timing. Resolve provider IDs explicitly; do not guess aliases.
2. Run integrity and `audit` after imports. Review source age, missing facts,
   chronological evaluation coverage and saved model version/cutoff. A research
   probability alone does not satisfy operating decision gates.
3. If prospective odds collection is configured, use `import-odds` or the explicit
   scheduled odds commands. Credentials belong in private configuration, never
   report exports. Read the [source decision record](DATA_SOURCE_DECISION.md)
   before connecting accounts or using licensed feeds.
4. `alert-event` and `paper-trade` independently refresh prices and apply roster,
   quote and model-readiness checks. Missing verified evidence produces a hold.
   Review exact command options with `--help`. No API key or verified future
   roster means no new live gate approval. Saved prices are observational.
5. After verified results, run `audit`, settle the applicable paper/forecast
   records, and review exceptional outcomes. Record an actual wager only if it
   was independently placed, using its accepted price and bookmaker settlement.

The operating gate needs source-observed historical evidence, adequate
chronological train/validation/test periods and reproducible probabilities.
Historical imports and current rankings cannot replace those observations. The
dashboard labels holds and retrospective research separately. Keep a dated log
of missing bouts, source failures and rejected gates; inspect prospective results
before considering any model promotion.

## Backup, recover and maintain

```sh
.venv/bin/python -m ufc_odds_model.backup create \
  --db mysql:operating \
  --output "backups/ufc-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen).mysql.json"
```

The [backup runbook](BACKUP_RUNBOOK.md) covers private logical JSON, actual native
restore drills and scope. The [evidence bundle guide](EVIDENCE_BUNDLE.md) adds
recorded payloads and artifacts. Restore into new namespaces/directories, verify
receipts and run audits before any separate activation. Delivery recovery
preserves documents and forecast chains in recovery-only tables; it does not
replace live global pointers. Back up required research/statistics datasets
separately and preserve private server recovery credentials outside bundles.

Keep original frozen SQLite sources unchanged for explicit historical
reproduction. They remain migration inputs, not normal runtime defaults. For
hosted/container operation, follow the [deployment guide](CONTAINER_DEPLOYMENT.md).
Local services depend on the Mac, login session, sleep and connectivity.

When a source or database command fails, preserve existing evidence and inspect
the sanitized run receipt. Source age must remain visible; a previous quote or
ranking is not relabeled as freshly captured. Retry after reviewing the failed
stage and source/server status.
