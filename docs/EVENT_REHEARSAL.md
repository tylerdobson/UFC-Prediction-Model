# One-event synthetic operator rehearsal

This is a **disposable, offline demo** of the pre-fight decision path. It never reads an API key, contacts a provider, places a wager, or touches the operating or research database. All fighters, bouts, results, card observations, and prices are fictional. A passed gate and the reported paper return establish that the software path works on this fixture; they do **not** establish model quality or a betting edge.

From the repository root after `pip install -e '.[dashboard]'`:

```bash
python scripts/rehearse_event.py
python -m unittest discover -s tests -p test_event_rehearsal.py -v
```

The first command creates a new temporary directory, prints a summary, and removes the files on exit. To retain a copy for inspection, choose a **new** directory inside the already ignored `data/raw/` tree:

```bash
python scripts/rehearse_event.py --output-dir data/raw/rehearsal-20260926
UFC_MODEL_INTEGRITY_REPORT=data/raw/rehearsal-20260926/reports/synthetic_demo_final_integrity.json \
UFC_MODEL_DB=data/raw/rehearsal-20260926/synthetic-demo.sqlite \
  python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502
```

The command refuses an output directory that already exists. The separate dashboard shows a **Demo data only** notice. After the rehearsal has finished, the event is completed, so its final outcome is on **Ledgers** and **Data quality**. To inspect the pre-fight Upcoming card instead, point Streamlit at the consistent, read-only SQLite snapshot made before the fictional results:

```bash
UFC_MODEL_INTEGRITY_REPORT=data/raw/rehearsal-20260926/reports/synthetic_demo_prefight_integrity.json \
UFC_MODEL_DB=data/raw/rehearsal-20260926/synthetic-demo-prefight.sqlite \
  python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502
```

The saved `reports/synthetic_demo_prefight_dashboard.json` also captures that read-only view. The two database files share the retained synthetic source payloads under the same output directory.

The pre-fight snapshot freezes the rows, not the wall clock. After 60 seconds the live dashboard correctly marks its fictional quote and gate as stale; the saved JSON shows their status at rehearsal time. The saved integrity status expires after 15 minutes unless its hash check is rerun. An expired display must not be treated as a current alert.

## What the run exercises

1. Initialize a new SQLite database from the current migrations.
2. Import a reviewed-format CSV with four earlier completed bouts and a separate, source-dated scheduled two-bout card. Retain both exact CSV payloads and immutable card snapshots. The script tags rows as demo in its isolated database so the existing dashboard visibly labels them. The receipts still identify the original manual CSV imports.
3. Audit the staged card and results. An error stops the run.
4. Import one in-memory, fictional odds payload through the existing odds normalizer/receipt path. It deliberately uses source-compatible JSON so the unchanged quote provenance and pre-fight alert gate can be exercised; `synthetic-demo-book` is **not** a sportsbook quote.
5. Score the event with Elo using only earlier event dates. Save the score CSV, then run the existing 60-second freshness, roster, receipt, and price-drift gate. Record capped paper decisions with a 1% per-bet and 1.5% per-event limit on a 1,000-unit fictional bankroll.
6. Make a consistent SQLite backup of the pre-fight state, mark it read-only, and read its dashboard. Import fictional results with a simulated timestamp after the fictional card, then settle the paper entries through the existing settlement function. Read the final dashboard and verify SQLite, foreign keys, migrations, and source payload hashes in both databases.

The simulated post-event timestamp is applied **only inside this fixture** so the fictional decision precedes the fictional event and its settlement follows it. A retained fixture has future-dated synthetic result receipts; it must stay separate from any operating database. No real feed, historical price, bookmaker settlement, or source-use right is tested.

The first audit has no errors but does retain warnings: historical cards have no bookmaker prices, the upcoming card has no quote until the following import step, and completed-result snapshots were observed after their fictional fights. Those warnings remain visible in the report and are expected for this staged fixture.

The output directory contains `synthetic-demo.sqlite`, `synthetic-demo-prefight.sqlite`, three labeled CSV inputs, retained raw payloads, `reports/` with a labeled score CSV, pre-fight dashboard snapshot, and separate integrity reports for the two database states, plus `synthetic_demo_report.json`. The report includes explicit `synthetic_demo: true` and `operating_evidence: false` fields. A successful run currently produces two accepted gate checks, stakes of 10 and 5 units, one fictional win and one fictional loss, three verified pre-fight receipts, and four verified final receipts. The 60% return is fixture arithmetic from deliberately favorable mock prices and has no predictive meaning.

The next operating milestone is to repeat the same path for **one actual upcoming event** with a rights-cleared, timestamped roster/results source and a configured odds API account. Keep a real paper ledger and verify the provider's result and bookmaker settlement after the event. This synthetic rehearsal cannot replace that event-day drill.
