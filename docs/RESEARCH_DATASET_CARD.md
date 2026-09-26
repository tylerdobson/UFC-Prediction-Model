# UFC result-history research dataset

Checked 2026-09-26. The local research database at `data/ufc_research_2011_2025.sqlite` contains 15 complete calendar years of retrospective UFC event results, 2011–2025. It was built from English Wikipedia's official MediaWiki page-source API. The database and raw JSON snapshots are ignored by Git; the private repository contains the reproducible importer and this audit, not a hidden production feed.

| Checked measure | 2011–2025 import |
| --- | ---: |
| Completed numbered catalog rows | 592 |
| Events imported | 588 |
| Distinct event/summary source pages | 583 |
| Parsed source bout rows on accepted pages | 6,927 |
| Bouts/results with two stable fighter IDs | 5,763 |
| Bouts held for fighter identity review | 1,164 |
| Stable fighter IDs represented in imported bouts | 1,542 |
| Imported wins / draws / no contests | 5,648 / 45 / 70 |

The import retained 558 standalone event pages and 30 event sections embedded in year or *The Ultimate Fighter* season pages. Four catalog entries are excluded because their event-page date disagrees with the year catalog: UFC 193; UFC Fight Night: Blaydes vs. Ngannou 2; UFC on ESPN: Hermansson vs. Vettori; and UFC on ESPN: Whittaker vs. Till. These one-day conflicts may reflect local versus broadcast dates, but the importer does not guess. Nine canceled or unnumbered catalog rows are also excluded. The 1,164 skipped bout rows involve unlinked or unresolved fighters. **Imported-bout coverage is 83.2% of the parsed source bout rows**, and the omissions are uneven across years; this can bias any trained model. Review files list each excluded row.

Every accepted source page has a retained JSON payload with a SHA-256 receipt, page ID, revision ID and timestamp, license URL, and source URL. Each year catalog page is also retained. The database contains 79 Action API request/response receipts used to resolve canonical event and fighter page IDs; the latest broad and embedded passes save each lookup used for their page-ID decisions. The [source integrity command](../src/ufc_odds_model/integrity.py) verified the research database and all 2,484 ingestion receipts at the checked point: SQLite integrity `ok`, zero foreign-key violations, zero missing or changed payloads. The total includes multiple reviewed import passes; it is not a count of unique pages. See [the importer guide](WIKIPEDIA_HISTORY_SOURCE.md) for the identity crosswalk and attribution rules.

## Rebuild and inspect

```bash
ufc-model --db data/ufc_research_2011_2025.sqlite init-db
ufc-model --db data/ufc_research_2011_2025.sqlite import-wikipedia-years \
  --first-year 2011 --last-year 2025 \
  --review-out reports/wikipedia_2011_2025_review.json
ufc-model --db data/ufc_research_2011_2025.sqlite import-wikipedia-embedded \
  --first-year 2011 --last-year 2025 \
  --review-out reports/wikipedia_2011_2025_embedded_review.json
python -m ufc_odds_model.integrity \
  --db data/ufc_research_2011_2025.sqlite \
  --output reports/wikipedia_2011_2025_integrity.json
ufc-model --db data/ufc_research_2011_2025.sqlite audit
ufc-model --db data/ufc_research_2011_2025.sqlite backtest
```

Use a new database path for a rebuild; `init-db` does not overwrite an existing one. Open `reports/wikipedia_2011_2025_review.json` and `reports/wikipedia_2011_2025_embedded_review.json` before interpreting coverage. Rebuilds can differ because Wikipedia pages are edited; compare revision receipts and review rows before combining versions. The local web app's **Historical data** tab shows event, bout, fighter, year, and recent-event counts from this research database.

## Modeling limits

The retrospective, date-ordered Elo replay evaluated 5,648 binary outcomes, with accuracy **54.89%**, Brier score **0.2465**, and log loss **0.6860**. This is an exploratory check on the imported subset, not a calibrated prospective result or a wagering return. It has **zero historical bookmaker quotes**. Current Wikipedia revisions do not establish when a pre-fight roster, result correction, or stat became available; the dataset also lacks verified UTC event starts. The strict chronological model evaluation and live betting alerts therefore remain unavailable on this research database. Add a source-dated operating history and timestamped prices before comparing to a bookmaker baseline or reporting ROI.

English Wikipedia text is under [CC BY-SA 4.0](https://foundation.wikimedia.org/wiki/Terms_of_Use). Attribute each source page and revision and review share-alike obligations before redistribution. Follow Wikimedia's [API usage guidelines](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines) when rebuilding.
