# Read-only dashboard runbook

The first web release is a **local, private Streamlit dashboard**. It reads already saved SQLite rows and an optional evaluation report. Opening a page does not fetch an API, fit a model, run an alert, settle a ledger, or place a wager.

## Install and start

From the repository root, with Python 3.11 or newer:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dashboard]'
python -m streamlit run app.py --server.address 127.0.0.1
```

Open the local URL Streamlit prints (normally `http://127.0.0.1:8501`). The UI defaults to `data/ufc.sqlite` and `reports/evaluation.json`. A missing database or report is shown as unavailable. It does not create the database. To initialize an empty database for a clean installation, use `ufc-model --db data/ufc.sqlite init-db` before launching; see the README for the full event workflow.

Optional server-side paths can be set before launch:

```bash
export UFC_MODEL_DB=/absolute/path/to/ufc.sqlite
export UFC_MODEL_EVALUATION_REPORT=/absolute/path/to/evaluation.json
python -m streamlit run app.py --server.address 127.0.0.1
```

`UFC_MODEL_DB` and `UFC_MODEL_EVALUATION_REPORT` are read on the server only. Keep the SQLite database, saved reports, raw provider payloads, and API credentials outside Git. The app has no fields for credentials and should stay bound to localhost until authentication and a reviewed hosting plan exist.

## What each view means

- **Upcoming card:** displays stored UFC bouts, fighter IDs, latest stored predictions, observed quotes, quote age, and the adapter's availability reason. A recent saved alert check is historical evidence; it is not proof that the market is still executable. Use the CLI pre-fight gate at the decision time.
- **Model evidence:** displays a saved chronological evaluation from `reports/evaluation.json` when present. Missing, invalid, insufficient-history, and possibly stale reports are labelled. The dashboard does not evaluate models on page load.
- **Data quality:** displays ingestion receipts, current database audit issues, and price coverage. A saved receipt proves a payload was captured; it is not by itself a verified import or row-count receipt.
- **Ledgers:** displays capped simulated paper decisions and separately recorded actual wagers. The dashboard cannot write either ledger. Binary results may settle automatically in the CLI; nonbinary and unresolved cases need review.

The checked-in demo data is fictional. If it is the only source in the selected database, the UI labels it prominently. An empty or stale view should be treated as an expected protective state, not an application error.
The page is a snapshot, not a live market feed. Refresh it to see later stored records and read the footer's snapshot time. Always rerun the CLI alert gate before acting; even a prior accepted check can expire or be superseded while a browser tab sits idle.

## Local smoke check

After installation, run the app and verify all four tabs. With an empty database, each view should show unavailable or no-entry states. With `data/demo.sqlite`, the dashboard must label demo data, and it must not show an executable wager. In a second terminal, the Streamlit health endpoint should return `ok`:

```bash
curl -fsS http://127.0.0.1:8501/_stcore/health
```

For the offline core pipeline tests run `python -m unittest discover -s tests -v`. The dashboard dependency is optional so core CLI tests and CI do not need to install Streamlit.
