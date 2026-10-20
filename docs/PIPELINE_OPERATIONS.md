# Scheduled native MySQL refresh

The normal pipeline captures an immutable upstream archive, normalizes CSVs
directly into a new MySQL statistics version, checks coverage, refreshes official
rankings and publishes committed evidence. It creates no SQLite staging store
and does not retrain a model. Every attempt retains a sanitized JSON run receipt
with source hashes, stage timing, coverage, outcome and alert status.

## Run or replay

From the repository root:

```sh
.venv/bin/ufc-pipeline refresh
.venv/bin/ufc-pipeline status
# Equivalent module entry point:
.venv/bin/python -m ufc_odds_model.pipeline_ops refresh
```

The refresh resolves the current upstream Git commit and downloads files pinned
to that commit. `--commit FULL_40_CHARACTER_GIT_SHA` selects an explicit immutable
source version. A retained archive can be replayed without external source
network calls by disabling the otherwise-default rankings refresh:

```sh
.venv/bin/ufc-pipeline refresh \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --no-refresh-rankings
```

This replay still requires the configured MySQL server. Without
`--no-refresh-rankings`, an archive replay also fetches the current official
rankings. It verifies their source-bound fighter portrait map, reusing a matching MySQL publication for at most seven days; a new roster or expired capture triggers bounded official athlete-page/image capture. Portrait coverage and explicit failures remain visible. A clean installation needs no private research database: the default
build uses canonical archive identities without a research crosswalk. The local
installed configuration supplies `mysql:research-complete`. Use `--history
mysql:research-complete` to select that read-only input explicitly, or
`--no-history` to override a configured crosswalk. Its exact source identity
contributes to the build fingerprint. Native refresh rejects SQLite history
paths; import retained legacy evidence first. Ambiguous research joins remain
explicit gaps.

The verified native refresh `20261020T030415Z-f1bcb649f5` completed in 28.1 seconds
with 793 cards, 8,950 canonical bouts and 41,514 fighter-round fact rows. These are
that run's observed counts, not a promise that every field or research join is
complete. Inspect the current publication and coverage for later runs.

## Runtime configuration

The default private configuration is
`~/Library/Application Support/UFCForecast/pipeline-env.json`; `--config PATH`
selects another file. It is parsed as JSON, never sourced as shell code, and must
be a current-user-owned regular file with no group/other permissions (`0600`).

```json
{
  "environment": {
    "UFC_MYSQL_HOST": "127.0.0.1",
    "UFC_MYSQL_PORT": "3307",
    "UFC_MYSQL_USER": "ufc_pipeline",
    "UFC_MYSQL_PASSWORD": "YOUR_LOCAL_PASSWORD",
    "UFC_MYSQL_DATABASE": "ufc_warehouse"
  },
  "options": {
    "mysql": true,
    "history": "mysql:research-complete",
    "refresh_rankings": true,
    "min_retained_fraction": 0.9
  }
}
```

Supported options include `ops_root`, `archive`, `history`, `commit`, `repository`,
`mysql`, `refresh_rankings`, `local_alert`, `webhook_alert` and
`min_retained_fraction`. Explicit CLI values override configured options. A flat
object of supported environment variables is accepted by the pipeline's config
reader; the wrapped form above also works across native runtime commands. Keep
credentials outside the repository and never embed them in a plist.

MySQL is the normal CLI default. `--mysql` can make that choice explicit.
`--no-mysql` selects the retained legacy fixture/replay implementation; a failed
MySQL connection never enters that branch.

## Quality and publication boundaries

Before publication, a dataset must contain a card, fighter, bout and usable
paired statistics bout. When a prior publication exists, card/bout/usable-paired
counts must retain at least 90% of the previous values by default. The `quality`
stage records observed/prior counts, thresholds and each result. Individual held
bouts and optional missing fields stay documented rather than becoming zeros.

For an intentional smaller nonempty source, review the quality report and choose
an explicit finite `--min-retained-fraction` from 0 to 1. Zero relaxes the shrink
threshold, not the nonempty requirement:

```sh
.venv/bin/ufc-pipeline refresh --archive PATH/manifest.json \
  --no-refresh-rankings --min-retained-fraction 0
```

The native version name derives from source commit/manifest, research input and
builder hashes. Repeated inputs reverify sources and the existing immutable
MySQL content rather than create another full version. Warehouse publication
uses a stable content-derived key as well.

The statistics alias and its `pipeline/statistics` delivery document commit in
one transaction after verification. Warehouse analytical views publish in a
separate transaction; rankings publish separately too. A failure before alias
activation retains the previous statistics alias. A later failure can leave a
complete new warehouse or ranking publication while the archive alias is older;
replay reconciles these stages. A lost commit acknowledgement has an uncertain
outcome, so inspect status and replay the same inputs rather than assuming a
rollback. This is not a distributed transaction across database and file logs.

Native refresh failures return `1`, configuration failures `2`, and concurrent
lock conflicts `3`. An OS-held advisory lock permits one refresh per operations
directory and releases after process exit. The legacy branch uses its historical
lock-conflict code `75`. Receipts expose error categories, not raw credentials or
arbitrary connection exception strings. A failed quality build is retained for
inspection without alias activation.

## Retained files and MySQL records

```text
data/raw/case-study-ops/
  sources/                         pinned upstream/offline source receipts
  rankings/                        actual official HTML and capture receipts
  native-exports/INPUT_HASH/
    coverage.json
    coverage.csv
  runs/RUN_ID/run.json              timings, hashes, checks, outcome, alerts
  .refresh.lock                    OS-held ownership; file can remain idle
  scheduler.stdout.log
  scheduler.stderr.log
```

Normalized rows and ready identities live in MySQL runtime datasets/catalogs.
`mysql:statistics` resolves the current approved version. The active delivery
document retains coverage, inputs, quality results and warehouse receipt. Native
`status` verifies MySQL content and reads this publication; it does not rely on a
local `latest-ready.json`. Older `versions/.../statistics.sqlite` and local ready
pointers belong to the explicit legacy implementation.

Use the [backup](BACKUP_RUNBOOK.md) and [evidence bundle](EVIDENCE_BUNDLE.md)
procedures for recovery. Current rankings remain descriptive current context,
separate from historical model inputs and separate from source stated update
labels.

## Daily schedule and failure notifications

The existing project LaunchAgent runs at **09:00 local time**, using the
repository's `.venv/bin/python` and working directory. Installation does not run
an immediate refresh. The installer manages only
`com.ufcforecast.statistics-refresh` in the current user's LaunchAgents:

```sh
.venv/bin/python scripts/install_refresh_schedule.py status
.venv/bin/python scripts/install_refresh_schedule.py install --mysql \
  --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json" \
  --dry-run
.venv/bin/python scripts/install_refresh_schedule.py install --mysql \
  --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json"
.venv/bin/python scripts/install_refresh_schedule.py uninstall
```

The plist contains paths and arguments, never passwords. Configuration is reread
at each run. `--hour`/`--minute` change the local schedule. The separate project
MySQL service is managed by [local_mysql.py](LOCAL_MYSQL.md); this scheduler does
not manage another service or change system MySQL settings.

Failed refreshes request a local Mac notification with the run ID. Read the run
receipt's stage array for the failed stage and use `ufc-pipeline status` to verify
the active MySQL publication. The receipt records notification-command success;
macOS settings determine whether a banner appears. `--no-local-alert` disables
it for a manual run. Successful runs send no notification. The Mac must be
running and the user logged in; sleep/connectivity can delay work.

Scheduled runs disable webhooks. Optional manual HTTPS webhook alerts require
both `--webhook-alert` and `UFC_PIPELINE_ALERT_WEBHOOK`; they use a five-second
timeout and a generic failure payload. The selected destination is local Mac
notifications, with no outbound webhook configured by default.

## Verification

```sh
.venv/bin/python -B -m unittest tests.test_pipeline_ops tests.test_mysql_runtime \
  tests.test_mysql_delivery -v
```

Pure tests exercise fixtures, failure gates and secret-safe behavior without
installing a schedule or sending notifications. Native tests are opt-in with
`UFC_MYSQL_INTEGRATION=1` against an initialized test instance; they exercise real
publication, rollback and concurrent ledger writers. Review fixtures and target
configuration before enabling database-writing tests.
