# Read-only dashboard runbook

The first web release is a **local, private Streamlit dashboard**. It reads already saved SQLite rows and optional evaluation and source-integrity reports. The Historical data view verifies the retained research-source payload hashes before showing source-row coverage. Opening a page does not fetch an API, fit a model, run an alert, settle a ledger, or place a wager.

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
export UFC_MODEL_RESEARCH_REPORT=/absolute/path/to/ufc_research_1993_2026_holdout.json
export UFC_MODEL_INTEGRITY_REPORT=/absolute/path/to/integrity.json
python -m streamlit run app.py --server.address 127.0.0.1
```

These paths are read on the server only. Keep the SQLite database, saved reports, raw provider payloads, and API credentials outside Git. The app has no fields for credentials and should stay bound to localhost until authentication and a reviewed hosting plan exist.

## What each view means

- **Upcoming card:** displays stored UFC bouts, fighter IDs, latest stored predictions, observed quotes, quote age, and the adapter's availability reason. The source and model evidence panel shows the six optional dated inputs for each logistic prediction. A saved alert check is historical evidence; the dashboard compares its roster snapshot with the latest stored matchup and labels a changed roster. This does not prove that the market is still executable. Use the CLI pre-fight gate at the decision time.
- **Historical data:** displays the completed Wikipedia research database's imported events, bouts, fighters, results, annual totals, recent cards, and distinct source-page receipts. It labels this history research-only. For each completed card, the dashboard finds its exact page or embedded-section receipt, checks its payload hash and imported-bout count, and displays parsed source rows, held identity rows, and annual accepted shares only when every card reconciles. Conflicting or unreadable receipts suppress those coverage figures. The [dataset card](RESEARCH_DATASET_CARD.md) explains the four reconciled event dates and the limits of this accepted subset.
- **Model evidence:** displays a saved operating chronological evaluation from `reports/evaluation.json` when present, plus optional dated-input coverage for upcoming predictions. A separate **retrospective research holdout** panel reads `reports/ufc_research_1993_2026_holdout.json` when present. It appears only when the report's schema and accepted-result fingerprint match the selected database and every completed research card has a verified source receipt. A changed result, missing payload, or mismatched identity hold suppresses its scores. The panel labels its Elo and calibrated logistic metrics research-only; they cannot promote a model or support an alert or ROI claim. Missing, invalid, insufficient-history, and stale operating reports remain labelled. The dashboard does not fit models on page load.
- **Data quality:** displays ingestion receipts, the ten most recent operator jobs including failed or still-started jobs, current database audit issues, price coverage, and the last saved source-integrity check. Job rows contain only an error category, never a provider response. Run `python -m ufc_odds_model.integrity --db data/ufc.sqlite --output reports/integrity.json` before an event; the dashboard labels an absent, expired, failed, different-database, or database-changed check. The checker records both the main SQLite file and any nonempty write-ahead log so later WAL-only writes make its result stale. A saved receipt alone does not prove the payload remains intact.
- **Ledgers:** displays capped simulated paper decisions and separately recorded actual wagers. Paper rows say whether a roster snapshot was recorded at the gate; older rows without one are labelled legacy/unverified. The dashboard cannot write either ledger. Binary results may settle automatically in the CLI; nonbinary and unresolved cases need review.

The checked-in demo data is fictional. If it is the only source in the selected database, the UI labels it prominently. An empty or stale view should be treated as an expected protective state, not an application error.
The page is a snapshot, not a live market feed. Refresh it to see later stored records and read the footer's snapshot time. Always rerun the CLI alert gate before acting; even a prior accepted check can expire or be superseded while a browser tab sits idle.

## Local smoke check

After installation, run the app and verify all five tabs. With an empty database, each view should show unavailable or no-entry states. With `data/demo.sqlite`, the dashboard must label demo data, and it must not show an executable wager. Set `UFC_MODEL_DB=data/ufc_research_2011_2025.sqlite` to inspect the real research history; the Historical data tab should show its counts while Upcoming card remains unavailable. In a second terminal, the Streamlit health endpoint should return `ok`:

```bash
curl -fsS http://127.0.0.1:8501/_stcore/health
```

For the offline core pipeline tests run `python -m unittest discover -s tests -v`. The dashboard dependency is optional so core CLI tests and CI do not need to install Streamlit.
