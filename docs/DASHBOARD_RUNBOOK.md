# Read-only MySQL dashboard

Install `.[dashboard,reports]`, configure MySQL, run `ufc-mysql init`, and publish or restore the retained datasets as described in [MySQL runtime](MYSQL_RUNTIME.md). Then run:

```sh
streamlit run app.py --server.address 127.0.0.1
```

The default database is `mysql:operating`. Reads use MySQL sessions; loading a page never initializes tables, trains a model, captures a source, records a bet or updates a ledger. A short read cache bounds repeated archive scans. A failed database read shows unavailable rather than selecting a local report or SQLite source.

| Section | Evidence shown |
|---|---|
| Upcoming card | Published frozen forecast, official portraits, earlier-round chart, source-bound PDF/CSV downloads, identity-held matchups and past-fight/style context |
| Historical data | All retained cards/bouts, opponent, method, division, reach and quality filters, original source links |
| Model evidence | Saved V6/V7 fixed and rolling metrics, feature coverage, limitations and unpromoted candidate results |
| Data quality | MySQL health, per-card coverage, missing reach/control time, source hashes and unresolved identity/date/round holds |
| Ledgers | Actual stored paper and manual wager entries, with separate totals and truthful empty states |
| Forward test | Hash-verified frozen forecasts, recovered-vs-prestart provenance, settled metrics and pending outcomes |
| Rankings | Official Meta/media champion and 15 contenders for every active division, update/capture dates and tie notes |

The historical chart uses up to five eligible earlier fights. The separate context panel shows the latest five source rows, including visible held rows excluded from aggregates. Rates retain observed denominators and sample counts. Opponent Elo reflects usable archive results before the bout, not today's ranking or a complete career record.

Current rankings are descriptive and never inserted into historical features. V7 is a research comparison; V6 remains the delivered model after V7's worse fixed-benchmark result. Historical performance does not establish live profitability.

Publish sources and forecasts through CLI jobs. The dashboard resolves its forecast from MySQL and verifies matching local release artifacts for downloads. Keep server credentials outside Git. Portraits load from official UFC URLs and require internet; licensed fonts and report exports are local. Bind locally for private use; public hosting requires the deployment controls described in the container guide.

For verification, open all seven tabs, change a historical filter and matchup, inspect both rankings systems and divisions, and download a report. Database failure must remain an unavailable state. Explicit legacy `UFC_MODEL_DB=/path/to/file.sqlite` is retained only for migration/fixture comparison.
