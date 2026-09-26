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

`init-db` applies the versioned SQL migrations to `data/ufc.sqlite`. Keep `data/`, `models/`, and `reports/` out of Git. For a completely separate fictional walkthrough, use a new database path and mark every resulting screen as demo:

```bash
ufc-model --db data/demo.sqlite init-db
ufc-model --db data/demo.sqlite seed-demo
ufc-model --db data/demo.sqlite score-event demo-upcoming
UFC_MODEL_DB=data/demo.sqlite python -m streamlit run app.py --server.address 127.0.0.1
```

The demo contains no real market signal. It has too little history for the calibrated logistic gate and cannot exercise a real live odds check.

## Configure real sources

The operator approved free API accounts. Obtain keys through the providers' own account pages and set them in the shell that runs the CLI. The application does not read `.env` automatically. Do not paste keys into the dashboard, commit them, or include them in bug reports. Check that each account's terms permit this project's intended use.

```bash
export ODDS_API_KEY="your-key"
```

The Odds API free tier can collect prospective snapshots, while historical odds generally need a paid plan. A reviewed rights-cleared CSV import is supported for fight history. The optional Sportradar adapter is for permitted internal source evaluation; its current free-trial terms restrict publication/display and require express written approval for betting-related use. Do not run a decision workflow on trial data without the appropriate rights. Read [DATA_SOURCE_DECISION.md](DATA_SOURCE_DECISION.md) for the checked limits and terms.

## Before an event

1. Import a dated UFC card and historical results with `ufc-model import-csv path/to/reviewed_bouts.csv` from a source you have rights to use. A licensed production feed can be added after its scope is checked; the Sportradar trial is not the default source for this betting decision workflow. Link known cross-provider fighter IDs with `link-fighter` **before** mixing provider histories. Review replacements, cancellations, event status, and UTC start time against the source.
2. Run `ufc-model audit` and resolve identity, status, timing, and result errors. Keep the raw source payloads and ingestion receipts. If the current roster cannot be verified, leave the event unavailable.
3. Run `ufc-model evaluate --decision-hours-before-event 24 --output reports/evaluation.json` after adding enough history. Inspect train/validation/test sizes, calibration, bookmaker coverage, and missing metrics. `null` ROI means there were no qualifying historical prices; it is not zero return. Re-run the evaluation when the reviewed history or cutoff policy changes.
4. With `ODDS_API_KEY` set, run `ufc-model alert-event EVENT_ID --model elo --max-age-seconds 60 --decimal-odds-drift 0.05 --min-ev 0.03`. This fetches a new odds snapshot and saves explicit accepted/rejected gate checks. Run `ufc-model paper-trade EVENT_ID --bankroll-units 1000` when ready to create capped paper decisions. It independently refreshes odds and repeats the gate. A previously accepted alert is not authority to use an older line.
5. Launch the local dashboard with `python -m streamlit run app.py --server.address 127.0.0.1`. See [DASHBOARD_RUNBOOK.md](DASHBOARD_RUNBOOK.md) for screen details and optional path settings. Review the saved gate reason, quote capture and bookmaker update times, model cutoff/version, and open exposure. Recheck any live market directly if deciding whether to place a real wager; the dashboard price is observational.

No API key or confirmed future card means no live alert or new paper decision. An unavailable state is expected in that case.

## After the event

Import verified results, rerun `ufc-model audit`, then run `ufc-model settle-paper EVENT_ID`. Draws, no contests, cancellations, substitutions, and unresolved results need human review under the relevant bookmaker's rules. If an actual wager was independently placed, record its accepted price and stake with `record-bet`, then enter the bookmaker's real settlement with `settle-bet`. The manual ledger and paper ledger remain separate.

Keep a dated log of missing bouts, unmatched quotes, bookmaker update delay, gate rejections, source failures, and discrepancies. Do not promote a model on a small positive return alone.

## Backup and recovery

Back up SQLite before a source import or schema migration. From the repository root with the dashboard and ingest commands stopped:

```bash
mkdir -p backups
python -c "import sqlite3; src=sqlite3.connect('data/ufc.sqlite'); dst=sqlite3.connect('backups/ufc-before-event.sqlite'); src.backup(dst); dst.close(); src.close()"
```

Keep raw snapshots, model artifacts, and evaluation reports with the database backup if you need to reproduce a decision. To restore, stop the dashboard and all CLI jobs, preserve the failed database separately, copy the selected backup to `data/ufc.sqlite`, and run `ufc-model init-db` plus `ufc-model audit` before resuming. A backup command and a restore drill on a separate path should be verified before relying on this operationally.

When a source fetch fails, leave prior saved records intact and show their age. Do not silently present the last available quote as current. Check the CLI error, source quota, snapshot receipt, and the provider status before retrying.
