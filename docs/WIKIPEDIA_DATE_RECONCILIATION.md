# Reviewed calendar-date conflicts

Checked **2026-09-26**. Four completed UFC cards were held because their Wikipedia year catalog and event article gave adjacent but different calendar dates. The separate research importer accepts only these four exact event/page IDs, catalog and event dates, and source revisions. It stores the UFC event-listing date as the research event date and preserves both original dates in `reports/wikipedia_date_reconciliation.json` with every source receipt. There is no general one-day tolerance.

| Card | Catalog date | Event-page date | Stored date | UFC evidence |
| --- | --- | --- | --- | --- |
| UFC 193 | 2015-11-15 | 2015-11-14 | **2015-11-14** | [Event listing](https://www.ufc.com/event/ufc-193); [UFC explains November 14 US / November 15 Australia](https://www.ufc.com/news/rousey-vs-holm-now-headline-ufc-193) |
| Blaydes vs. Ngannou 2 | 2018-11-25 | 2018-11-24 | **2018-11-24** | [Beijing event listing](https://www.ufc.com/event/ufc-china-2018); [UFC's November 24 announcement](https://www.ufc.com/news/ufc-returns-china-first-ever-event-beijing-nov-24) |
| Hermansson vs. Vettori | 2020-12-06 | 2020-12-05 | **2020-12-05** | [December 5 event listing](https://www.ufc.com/event/ufc-fight-night-december-05-2020); [UFC card update](https://www.ufc.com/news/ufc-fight-night-update-december-5) |
| Whittaker vs. Till | 2020-07-25 | 2020-07-26 | **2020-07-25** | [July 25 event listing](https://www.ufc.com/event/ufc-fight-night-july-25-2020); [US/Japan date explanation](https://www.ufc.com/news/ufc-fight-pass-launches-japan) |

The [explicit registry and revision check](../src/ufc_odds_model/wikipedia_date_reconciliation.py) records the event number, title, Wikipedia page ID, both dates, catalog and event revisions, accepted date, and UFC evidence URL. It imports only if every reviewed field still matches; a later Wikipedia edit needs another review. At this pass, the four result tables had **48 source bout rows**, of which **45** entered the research database and **3** remain held for fighter identity review. Event pages and catalog payloads are retained with SHA-256 receipts; the UFC URLs are cited for the editorial date decision, not copied into the database.

Run on a separate, initialized research database:

```bash
python -m ufc_odds_model.wikipedia_date_reconciliation \
  --db data/ufc_research_rebuild.sqlite \
  --review-out reports/rebuild_date_review.json
```

These dates are a calendar convention for retrospective result ordering. They do **not** prove an exact UTC start time or when a roster was known before the fight, and they cannot qualify the cards for strict historical pre-fight evaluation or betting alerts.
