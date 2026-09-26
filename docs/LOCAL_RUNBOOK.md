# Local operator runbook

This is a **pre-fight, read-only dashboard plus CLI workflow** for one operator. The CLI imports sources, evaluates models, checks fresh prices, and records paper decisions. The dashboard reads saved evidence. Neither component places a wager or delivers a notification. Use the [source decision record](DATA_SOURCE_DECISION.md) before connecting an account.

## First local setup

Use Python 3.11 or newer from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dashboard]'
ufc-model init-db
python -m unittest discover -s tests -v
```

`init-db` creates the current schema in a new or empty `data/ufc.sqlite`. Ordinary CLI commands require an initialized, current schema and do not create or upgrade a database. If you install a newer project version over an existing database, stop other jobs and run the explicit [backup-gated migration](BACKUP_RUNBOOK.md#upgrade-an-existing-database) before importing or scoring. `init-db` refuses to upgrade an existing database. Keep `data/`, `models/`, and `reports/` out of Git. For a completely separate fictional walkthrough, use a new database path and mark every resulting screen as demo:

```bash
ufc-model --db data/demo.sqlite init-db
ufc-model --db data/demo.sqlite seed-demo
ufc-model --db data/demo.sqlite score-event demo-upcoming
UFC_MODEL_DB=data/demo.sqlite python -m streamlit run app.py --server.address 127.0.0.1
```

The demo contains no real market signal. It has too little history for the calibrated logistic gate and cannot exercise a real live odds check.

## Configure real sources

The operator approved free API accounts. Obtain keys through the providers' own account pages and set them in the shell that runs the CLI. Check that each account's terms permit this project's intended use. For a local `.env` file, copy the ignored template, edit `ODDS_API_KEY=` with your key, and load it into the current terminal before running a CLI command:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env locally; keep the key out of chat and Git.
set -a; source .env; set +a
```

The application reads the exported shell variable; it does not load `.env` automatically. Run the `source` command again in each new terminal. Do not put keys into the dashboard, commit them, or include them in bug reports.

The Odds API free tier can collect prospective snapshots, while historical odds generally need a paid plan. A reviewed rights-cleared CSV import is supported for fight history. The optional Sportradar adapter is for permitted internal source evaluation; its current free-trial terms restrict publication/display and require express written approval for betting-related use. Do not run a decision workflow on trial data without the appropriate rights. Read [DATA_SOURCE_DECISION.md](DATA_SOURCE_DECISION.md) for the checked limits and terms.

For a broad outcome-history research dataset, run the [Wikipedia year-range and embedded-card importers](WIKIPEDIA_HISTORY_SOURCE.md) in their **own** database. The year, event, and summary pages are retained with hashes and revision metadata. Open the generated review JSON files before using the results, then run integrity, audit, and the research-only Elo backtest. The [dataset card](RESEARCH_DATASET_CARD.md) records the checked 1993–2026 coverage and rebuild sequence. To inspect that database in the dashboard, set `UFC_MODEL_DB=data/ufc_research_2011_2025.sqlite` when launching Streamlit; the legacy filename contains the expanded history and remains labeled as research data. It cannot pass live alert or paper-bet gates. A separate, source-dated operating database is still required for pre-fight decisions.

A Wikipedia batch can leave earlier event commits in place if a later event fails; its review JSON is published only after full success. Follow the [partial-import recovery procedure](WIKIPEDIA_HISTORY_SOURCE.md) before relying on a report from that run.

## Before an event

1. Import a dated UFC card and historical results with `ufc-model import-csv path/to/reviewed_bouts.csv` from a source you have rights to use. For the upcoming card, fill `source_observed_at_utc`, source attribution, and reviewer fields in the [CSV template](../examples/bouts_template.csv). A missing or older-than-24-hour roster observation cannot pass the alert gate. A licensed production feed can be added after its scope is checked; the Sportradar trial is not the default source for this betting decision workflow. Link known cross-provider fighter IDs with `link-fighter` **before** mixing provider histories. Review replacements, cancellations, event status, and UTC start time against the source. Import [dated profile and stat observations](POINT_IN_TIME_OBSERVATIONS.md) only when their publication times and usage rights are verified.
2. Run `python -m ufc_odds_model.integrity --db data/ufc.sqlite --output reports/integrity.json` from the repository root. It checks SQLite integrity, applied migrations, foreign keys, and the SHA-256 of every saved source payload without writing to the database. A missing, changed, or unreadable payload must be investigated before relying on its rows. The ignored JSON report records the last check for the dashboard. Then run `ufc-model audit` and resolve identity, status, timing, and result errors. If the current roster cannot be verified, leave the event unavailable.
3. Run `ufc-model evaluate --decision-hours-before-event 24 --output reports/evaluation.json` after adding enough history. Historical targets require a scheduled roster snapshot captured before the decision, and prior results require completed snapshots captured before that decision. Backdated timestamps and revision labels on CSV files imported afterward cannot qualify. Inspect excluded target counts, train/validation/test sizes, calibration, bookmaker coverage, and missing metrics. `null` ROI means there were no qualifying historical prices; it is not zero return. Re-run the evaluation when the reviewed history or cutoff policy changes.
4. With `ODDS_API_KEY` set, run `ufc-model alert-event EVENT_ID --model elo --max-age-seconds 60 --decimal-odds-drift 0.05 --min-ev 0.03`. This fetches a new odds snapshot and saves explicit accepted/rejected gate checks linked to the roster snapshot. Run `ufc-model paper-trade EVENT_ID --bankroll-units 1000` when ready to create capped paper decisions. It independently refreshes odds and repeats the gate. A previously accepted alert is not authority to use an older line or superseded roster.
5. Rerun `python -m ufc_odds_model.integrity --db data/ufc.sqlite --output reports/integrity.json` after the latest import and gate writes so the dashboard's saved integrity status reflects the current database. Launch the local dashboard with `python -m streamlit run app.py --server.address 127.0.0.1`. See [DASHBOARD_RUNBOOK.md](DASHBOARD_RUNBOOK.md) for screen details and optional path settings. Review the saved gate reason, quote capture and bookmaker update times, model cutoff/version, recent source job status, and open exposure. Recheck any live market directly if deciding whether to place a real wager; the dashboard price is observational.

No API key or confirmed future card means no live alert or new paper decision. An unavailable state is expected in that case.

## After the event

Import verified results, rerun `ufc-model audit`, then run `ufc-model settle-paper EVENT_ID`. Draws, no contests, cancellations, substitutions, and unresolved results need human review under the relevant bookmaker's rules. If an actual wager was independently placed, record its accepted price and stake with `record-bet`, then enter the bookmaker's real settlement with `settle-bet`. The manual ledger and paper ledger remain separate.

Keep a dated log of missing bouts, unmatched quotes, bookmaker update delay, gate rejections, source failures, and discrepancies. Do not promote a model on a small positive return alone.

## Backup and recovery

Back up SQLite before a source import. The backup command uses SQLite's online backup API and checks a temporary restore. Choose a new output path each time:

```bash
python -m ufc_odds_model.backup create --db data/ufc.sqlite \
  --output backups/ufc-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen).sqlite
```

For portable recovery, create an [evidence bundle](EVIDENCE_BUNDLE.md) containing the database, all receipt payloads, model artifacts, and reports. Restore to a **new** directory, then verify receipts and run the audit before switching over. The [SQLite backup drill](BACKUP_RUNBOOK.md) covers the smaller single-file backup used by the migration command.

For a schema upgrade, use `ufc-model --db data/ufc.sqlite migrate --backup backups/UNIQUE.sqlite`. This command creates and verifies the pre-migration backup before applying any pending SQL. It refuses an existing backup path. Preserve the printed backup path and verify the upgraded database with the source-integrity command and `audit` before resuming event jobs. Do not run `init-db` as an upgrade shortcut.

For a packaged, private dashboard with a read-only database snapshot, follow the [container deployment runbook](CONTAINER_DEPLOYMENT.md). Its startup preflight verifies the snapshot and every retained payload before Streamlit serves it. The CLI ingestion and decision jobs remain in the operator environment.

When a source fetch fails, leave prior saved records intact and show their age. The failed import or alert run is retained in `operator_job_runs` with command, time, and a credential-free error category, and appears in Data Quality. Do not silently present the last available quote as current. Check the CLI error, source quota, snapshot receipt, and provider status before retrying.
