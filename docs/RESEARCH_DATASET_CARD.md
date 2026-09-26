# UFC result-history research dataset

Checked **2026-09-26**. The local research database at `data/ufc_research_2011_2025.sqlite` now covers **1993-11-12 through 2026-09-19**. Its filename predates the expansion and is retained so the running dashboard and earlier review artifacts continue to point to the same database. The source is English Wikipedia's official MediaWiki page-source API, with narrowly documented calendar-date reconciliations against [UFC event listings](WIKIPEDIA_DATE_RECONCILIATION.md). This is retrospective result history for research, not an operating betting feed.

| Accepted event period | Completed events | Imported bouts/results | Source bout rows held for identity review |
| --- | ---: | ---: | ---: |
| 1993–2010 | 166 | 1,248 | 279 |
| 2011–2025 | 592 | 5,808 | 1,167 |
| 2026 through September 19 | 32 | 172 | 231 |
| **Total** | **790** | **7,228** | **1,677** |

The accepted bouts involve **1,872 stable fighter IDs** and include **7,097 wins, 55 draws, and 76 no contests**. All 790 completed catalog events were represented after reviewing older embedded finale sections and four one-day date conflicts. Across those cards, the source result tables contain **8,905 bout rows**; **81.2%** have both fighter identities resolved and entered the database. The held 18.8% is uneven across years, including 231 of 403 source rows in 2026. It can bias a model toward better-linked fighters and eras. Do not equate an event count with complete fight coverage.

The 2011–2025 starting point had 588 events, 5,763 bouts/results, and 1,542 fighters. The expansion added **202 events, 1,465 bouts/results, and 330 represented fighters**. The four additional 2011–2025 cards use a pinned, per-event date review; the 2026 year import excludes future scheduled cards by date. The [public data survey](PUBLIC_DATASET_SURVEY.md) records Kaggle and GitHub alternatives used as gap-review leads rather than unreviewed operating imports.

## Source evidence and quality checks

Every accepted event or embedded section has a retained JSON source payload, SHA-256 ingestion receipt, page ID, revision ID/time, source URL, and CC BY-SA license metadata. Year catalog pages and Action API identity/redirect lookups have separate receipts. The local `reports/ufc_research_1993_2026_integrity.json` verified **2,795 of 2,795 receipts**, SQLite integrity `ok`, zero foreign-key violations, and zero missing migrations or changed payloads. The local `reports/ufc_research_1993_2026_audit.json` has zero errors and 1,580 warnings, mostly because this retrospective source cannot provide verified UTC starts or source-dated card observations. The count of receipts includes repeated reviewed passes, not unique pages.

The [identity review worksheet](IDENTITY_REVIEW.md) provides candidate leads for the original 2011–2025 held rows; a separate 2026 worksheet is in `reports/`. Neither automatically merges names or changes results. The year, embedded, and date-review JSON reports list every unresolved fighter and source exception. Four reconciled cards preserve both original Wikipedia dates, the accepted date, the exact source revisions, and a cited UFC event listing in [the date review](WIKIPEDIA_DATE_RECONCILIATION.md). Early-era tournament fights, cancellations, draws, and no contests still need independent sample checking before model promotion.

## Rebuild and inspect

Use a **new** database path for a rebuild. The commands below show the source passes; keep their review JSON files and use distinct output paths:

```bash
ufc-model --db data/ufc_research_rebuild.sqlite init-db
ufc-model --db data/ufc_research_rebuild.sqlite import-wikipedia-years \
  --first-year 1993 --last-year 2010 --review-out reports/rebuild_pre2011_years.json
ufc-model --db data/ufc_research_rebuild.sqlite import-wikipedia-embedded \
  --first-year 1993 --last-year 2010 --review-out reports/rebuild_pre2011_embedded.json
ufc-model --db data/ufc_research_rebuild.sqlite import-wikipedia-years \
  --first-year 2011 --last-year 2025 --review-out reports/rebuild_2011_2025_years.json
ufc-model --db data/ufc_research_rebuild.sqlite import-wikipedia-embedded \
  --first-year 2011 --last-year 2025 --review-out reports/rebuild_2011_2025_embedded.json
ufc-model --db data/ufc_research_rebuild.sqlite import-wikipedia-years \
  --first-year 2026 --last-year 2026 --review-out reports/rebuild_2026_years.json
python -m ufc_odds_model.wikipedia_date_reconciliation \
  --db data/ufc_research_rebuild.sqlite --review-out reports/rebuild_date_review.json
python -m ufc_odds_model.integrity --db data/ufc_research_rebuild.sqlite
ufc-model --db data/ufc_research_rebuild.sqlite audit
ufc-model --db data/ufc_research_rebuild.sqlite backtest
```

MediaWiki pages can change. The date reconciliation is tied to specific revisions and fails if either reviewed source changes; compare revisions and re-review instead of loosening the check. The local dashboard's **Historical data** tab reads the imported counts from this research database and labels the data research-only. The database, raw snapshots, and reports are private local artifacts, ignored by Git; use the verified evidence bundle for recovery.

The local `backups/ufc-research-1993-2026-20260926.zip` contains a consistent database copy, all 2,795 ingestion payloads, models, and reports. It passed bundle verification and a separate restore drill whose database again verified all 2,795 receipts. The local `backups/ufc-identity-review-evidence-20260926.zip` preserves 124 supplementary identity-review files; its 214 candidate lookup/page receipts and archive manifest hashes were checked. These archives are ignored by Git and are not part of the public repository history.

## Modeling limits

The retrospective, date-ordered Elo replay evaluated **7,097 binary outcomes**: accuracy **55.54%**, Brier score **0.2463**, and log loss **0.6856**. These are exploratory metrics on the accepted subset, not a calibrated prospective model or a wagering return. The database contains **zero historical bookmaker quotes**; paper profit and ROI remain `null`. Wikipedia revisions do not establish when a pre-fight roster, correction, or statistic first became available, and the dataset lacks verified UTC event starts. Strict point-in-time evaluation, bookmaker comparison, and live alerts therefore remain unavailable on this research database. Obtain a permitted source-dated operating history and timestamped prices before claiming a betting edge.

English Wikipedia text is [CC BY-SA 4.0](https://foundation.wikimedia.org/wiki/Terms_of_Use). Attribute source pages and revisions and review share-alike obligations before redistribution. Follow [Wikimedia API usage guidance](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines) when rebuilding.
