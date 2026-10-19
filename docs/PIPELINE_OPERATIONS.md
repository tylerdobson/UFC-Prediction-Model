# Scheduled statistics refresh

The operations pipeline captures an immutable upstream archive, normalizes it into a new SQLite statistics store, checks its quality, optionally publishes a transactional MySQL snapshot, and activates a local ready pointer. Every attempt retains a JSON run receipt with stage timing, source/build hashes, coverage, outcome, and alert status. It does not retrain a prediction model or change the original research databases.

## Run a refresh

Run commands from the project directory using its existing virtual environment:

```sh
.venv/bin/python -m ufc_odds_model.pipeline_ops refresh
.venv/bin/python -m ufc_odds_model.pipeline_ops status
```

The first command resolves the current upstream Git commit, downloads files using that exact commit, and verifies source hashes. To retain a specific source version, pass `--commit FULL_40_CHARACTER_GIT_SHA`. To rebuild a previously captured archive without network access:

```sh
.venv/bin/python -m ufc_odds_model.pipeline_ops refresh \
  --archive data/raw/ufcstats-archive-20261019/manifest.json
```

Canonical UFCStats identities work without a research database. An optional read-only research crosswalk can be supplied with `--history data/ufc_research_complete_profiles.sqlite`. It provides exact research identity links while retaining explicit ambiguous joins and coverage gaps. Neither source path is modified.

The default operations directory is `data/raw/case-study-ops`, separate from original databases and model artifacts. Use `--ops-root PATH` to change it. It is already covered by the repository's ignored `data/raw/` directory.

## Runtime configuration and MySQL

Configuration is read at runtime from `~/Library/Application Support/UFCForecast/pipeline-env.json` when present, or from an explicit `--config PATH`. It must be a regular file owned by the current user with no group or other permissions; use `chmod 600 PATH`. JSON is parsed as data and is never sourced as a shell script.

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
    "min_retained_fraction": 0.9
  }
}
```

A flat object containing only the supported environment variables is also accepted. Supported options are `ops_root`, `archive`, `history`, `commit`, `repository`, `mysql`, `local_alert`, `webhook_alert`, and `min_retained_fraction`. Explicit CLI values override options. Keep private configuration outside the repository.

```sh
.venv/bin/python -m ufc_odds_model.pipeline_ops refresh --mysql \
  --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json"
```

MySQL publication calls `mysql_warehouse.publish_statistics` with a stable version key derived from the built database hash. Retrying the same data reuses that warehouse version rather than creating a complete duplicate snapshot for every operations run. The warehouse publisher validates and commits its active snapshot in one transaction. Credentials and raw exception messages are excluded from operations receipts and scheduler output.

## Quality gates and failure behavior

Before MySQL publication or local activation, a dataset must have at least one card, fighter, bout, and usable paired statistics bout. When a prior ready snapshot exists, the next dataset must retain at least 90% of its card, bout, and usable paired-bout counts. This catches empty or substantially truncated captures even when their CSV headers and hashes are valid. Every gate logs observed counts, prior counts, thresholds, and pass/fail results in the `quality` stage.

Individual quarantined bouts remain documented source gaps. Missing optional statistics do not make an otherwise valid archive fail the dataset gate. To deliberately replace a larger dataset with a smaller nonempty one, choose an explicit threshold after reviewing the quality receipt:

```sh
.venv/bin/python -m ufc_odds_model.pipeline_ops refresh \
  --archive PATH/manifest.json --min-retained-fraction 0
```

The threshold accepts a finite number from 0 to 1; setting it to zero never permits an empty dataset. It can also be configured using `options.min_retained_fraction`.

A failed run returns nonzero and preserves the last verified local ready pointer. Exit codes are `1` for refresh failures, `2` for configuration failures, and `75` when another refresh already owns this operations directory. An OS-held advisory lock permits one writer per operations directory and is released after a process exits, including after a crash.

MySQL publication occurs before local activation. A failure before the warehouse transaction commits retains the previous committed warehouse snapshot. If the connection loses the commit acknowledgment, the outcome is uncertain: MySQL may already contain the complete new version. Replaying the same stable version resolves that uncertainty without duplicate rows. The local pointer remains at its prior verified version on a failed run. If the warehouse succeeds but local receipt storage or activation fails, a retry reconciles the same warehouse version with the local pointer; this is not a distributed transaction. A built version that fails the dataset gate remains available for inspection and is not activated.

## Receipts and immutable versions

```text
data/raw/case-study-ops/
  sources/                         pinned upstream or retained offline receipts
  versions/COMMIT/INPUT_HASH/
    statistics.sqlite
    coverage.json
    coverage.csv
    ready.json                     hashes, provenance, counts, logical digest
  runs/RUN_ID/run.json              stage timing, checks, outcome, alert status
  latest-ready.json                relocatable pointer to verified version
  .refresh.lock                    persistent file; lock ownership is OS-held
  scheduler.stdout.log
  scheduler.stderr.log
```

The version identity includes the source manifest hash, immutable source commit, optional research database hash, and builder implementation hash. A repeated source/build combination rechecks all retained source hashes, version artifact hashes, and the SQLite logical digest before reuse. Each attempt still creates a distinct run receipt. The `status` command verifies the pointed-to artifacts rather than only checking that a pointer file exists.

## Daily schedule and local failure notifications

The LaunchAgent runs at **09:00 local time**, without running immediately on installation. It uses the project's `.venv/bin/python` and project working directory. The installer manages only `com.ufcforecast.statistics-refresh` in the current user's `~/Library/LaunchAgents`; it does not manage MySQL or other services.

```sh
.venv/bin/python scripts/install_refresh_schedule.py install --mysql \
  --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json" --dry-run
.venv/bin/python scripts/install_refresh_schedule.py install --mysql \
  --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json"
.venv/bin/python scripts/install_refresh_schedule.py status
.venv/bin/python scripts/install_refresh_schedule.py uninstall
```

The installer automatically uses the default private configuration when it exists and enables MySQL when that configuration requests it or supplies MySQL environment values. `--mysql` or `--no-mysql` overrides that choice. The plist contains the configuration path, interpreter, arguments, and log paths; it contains no credentials. The configuration file is reread on every scheduled run. `--hour` and `--minute` allow a different local schedule.

Failed refreshes request a local Mac notification containing only the run ID, failed stage, and preservation status. The receipt records whether the notification command succeeded; the displayed banner also depends on macOS notification settings. `--no-local-alert` disables local notifications for a manual run. Successful runs do not send notifications.

Scheduled refreshes explicitly disable webhooks. Optional manual webhook alerts require both `--webhook-alert` and `UFC_PIPELINE_ALERT_WEBHOOK` containing an HTTPS URL. They use a five-second timeout and send only a generic failure payload. There is no webhook call unless explicitly enabled. The implemented and selected alert destination for this project is local Mac notifications.

## Verify without network or service changes

```sh
.venv/bin/python -B -m unittest tests.test_pipeline_ops -v
```

The tests build synthetic retained archives in temporary directories and exercise repeat refreshes, stable publication keys, a separate-process lock conflict, tampered artifacts, empty and truncated feeds, publication failures, post-activation receipt rollback, private configuration, and LaunchAgent install/rollback using an injected command runner. They do not install a real schedule, send a real notification, contact a webhook, or require MySQL.
