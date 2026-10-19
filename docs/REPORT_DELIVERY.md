# Earlier V4/V5 UFC 333 delivery

The current V6 release and reusable all-card workflow are documented in [All-card statistics](ALL_CARD_STATISTICS.md). This record preserves the earlier V4/V5 editions and their measurements.

## Delivered edition

The primary deliverable is `output/pdf/UFC-333-Prediction-Report-with-Stats.pdf`. Companion HTML, CSV, JSON and forecast receipt live under `output/ufc333-20261019-stats/`. The frozen cutoff is October 19, 2026, 19:56:30 UTC; model version is `logistic-prior-v5:research_retrospective:1b8479e2b299ed4a`. Earlier V4 artifacts remain preserved.

The official October 24 card observed on October 19 lists 14 bouts. Six have two established research identities; eight remain unscored. Nine participants have only provisional candidate IDs and one lacks matching history. No provisional identity was merged, and no human roster or identity approval is claimed. The earliest card start is 14:00 UTC, main card 18:00 UTC. Scheduled rounds are inferred from championship format and do not enter the model.

All 12 scored fighters have verified dated official reach and positive-attempt earlier-fight paired statistics. The report displays actual count coverage: Volkanovski 9, Evloev 8, Yan 15, Dvalishvili 15, Volkov 15, Kuniev 3, Pico 1, Keita 2, Murzakanov 6, Reyes 12, Dawson 10 and Aliev 3. Coverage describes matched earlier history, not complete careers.

## Sources and feature chronology

Retained official card, guide, identity evidence, athlete HTML, headers and receipts live under `data/raw/ufc333-report-20261019/` and `data/raw/ufc333-fighter-stats-20261019/`. Public reports exclude internal paths and raw payloads.

The per-fight archive is pinned to [Greco1899/scrape_ufc_stats commit 3e2f3db5751c14b5d4ea52e41921982ca23b9b23](https://github.com/Greco1899/scrape_ufc_stats/tree/3e2f3db5751c14b5d4ea52e41921982ca23b9b23), retained with its GPL-3.0 license, README, capture headers and hashes under `data/raw/ufcstats-archive-20261019/`. Publisher licensing does not establish rights in upstream UFC material. The raw archive is local research evidence, excluded from Git and public deliverables.

The loader supplies 6,595 exactly matched paired-stat bouts and 1,379 reach profiles. Names receive Unicode, case and whitespace normalization only. Date, participant pair and winner must agree. Ambiguous aliases, duplicate pair/date matches, blank placeholders and incomplete rounds are excluded. Each target date's features are constructed before that date's results or strike counts update history. Opponent-adjusted accuracy compares a fighter's earlier landed/attempted rate with the opponent's still-earlier defense, with shrinkage. The count feature records available earlier striking history.

Archive reach is applied retrospectively as an approximate static adult attribute. Historical measurement times are unknown. New target forecasts use official reach captured before their cutoff. Five official measurements differ from archived rounded values by 0.5 inch (1.27 cm); both versions remain retained. Numeric official reach is interpreted under the inch convention, converted using 2.54, and cross-checked with the archive; the official HTML itself omits a unit label.

Official current career rates have explicit per-minute, per-15-minute and percentage units. They are descriptive only and never replace earlier-fight model inputs. Normalized takedown accuracy is omitted for all 12 profiles because several publisher panels contain inconsistent or blank landed counts. Original published values remain in the athlete manifest and raw HTML; no replacement is inferred. Other genuinely absent granular fields remain unavailable.

The source capture is retrospective research evidence, not proof that these measurements or stat feeds were available before every historical fight. The research result database ends September 19; later results are absent even though the archive itself is newer.

## Model protocol and measured results

V4 retains the existing 11 features and Elo K=64. Its grid crosses L2 {0.002, 0.02, 0.1} with fitting windows {since 2010, five years, eight years}. Whole-date yearly folds from 2016 through December 2, 2023 choose minimum pooled log loss. The selected policy is L2 0.002 and an eight-year fit. V4 fixed-benchmark loss improves from V3 0.67122750 to 0.66271769, accuracy 57.43% to 59.23%; its paired loss interval is [-0.01169986, -0.00545926].

V5 freezes the V4 policy and tests one reach/statistics feature candidate. Selection is written before fixed-test evaluation. Its rule requires lower all-bout development and fixed log loss, at least 100 complete bouts and at least 25% complete coverage in both. An initial 50% feasibility target was relaxed before loss inspection after ingestion found only 525/1,447 complete fixed-benchmark bouts (36.28%); this is recorded in the selection provenance. Development coverage is 1,998/3,831 (52.15%). Full-cohort and complete/incomplete-subset results are all retained.

V5 development loss is 0.66023865 versus V4 0.66132576. On the same 1,447 fixed bouts, V5 loss is 0.66144121 and Brier score 0.23469798; accuracy is 59.16%, one fewer correct pick than V4. The paired event-date bootstrap loss interval [-0.00538745, +0.00261842] includes zero. The complete 525-bout subset loss changes 0.64986374 to 0.64849659, also with an interval including zero. V5 meets the declared research-selection rule; its incremental gain is modest and does not establish a reliable improvement.

Rolling V5 loss is 0.65951695 versus V4 0.66079269 across 5,278 bouts, accuracy 59.98%. The latest partial year has 398 bouts, loss 0.67365536 and accuracy 57.29%. The rolling pool includes 3,831 development observations and is not independent confirming evidence. The fixed benchmark is reused, not a fresh holdout. Conditional bootstrap intervals exclude data, identity, timing and selection uncertainty. Independent formulas recomputed fixed predictions and metrics.

Only fitting examples are windowed; all earlier history builds features. Fixed fitting/calibration boundaries remain prescribed by V3. Yearly folds reserve the final 15% of earlier dates for calibration. Report forecasts preserve the existing live whole-date 20%-of-bouts calibration tail and minimum samples. No target result enters fit, calibration or features.

## Verification and reproduction

From the repository root after installing `.[dashboard,reports]`:

```sh
python -m unittest discover -s tests
python -m ufc_odds_model.research_upgrade verify \
  --db data/ufc_research_complete_profiles.sqlite \
  --selection reports/research_v4_selection.json \
  --report reports/research_v4_evaluation.json
python -c "from ufc_odds_model.research_stats import load_verified_stats_report; r=load_verified_stats_report('reports/research_v5_evaluation.json','reports/research_v5_selection.json','data/ufc_research_complete_profiles.sqlite','data/raw/ufcstats-archive-20261019/manifest.json'); print(r['research_champion'])"
```

Repeat the V5 experiment with new output paths; selection must complete before evaluation. Editing the pinned implementation requires a new experiment.

```sh
python -m ufc_odds_model.research_stats select \
  --db data/ufc_research_complete_profiles.sqlite \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --out reports/NEW-stats-selection.json
python -m ufc_odds_model.research_stats evaluate \
  --db data/ufc_research_complete_profiles.sqlite \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --selection reports/NEW-stats-selection.json --out reports/NEW-stats-evaluation.json
```

Generate a new future-event report after refreshing sources:

```sh
ufc-model-report --card reports/ufc333_card_20261019.json \
  --selection reports/research_v5_selection.json \
  --evaluation reports/research_v5_evaluation.json \
  --stats-archive data/raw/ufcstats-archive-20261019/manifest.json \
  --athlete-facts reports/ufc333_fighter_stats_20261019.json \
  --output-dir output/NEW-FORECAST-EDITION
python scripts/render_prediction_pdf.py output/NEW-FORECAST-EDITION/prediction-report.json \
  --output output/pdf/NEW-FORECAST-EDITION.pdf
```

Generation verifies retained raw hashes, capture order, database identity, model selection and established IDs. It fits a transient target card, requires verified core athlete facts and real reach/earlier-stat coverage for every scored V5 participant, then saves model parameters, training/calibration dates, contribution metadata and output hashes. The preserved research database is opened read-only and never migrated. Frozen output directories are exclusive.

The original legacy database SHA-256 remains `e9a24dc1e69599deedb083c60fab47b3fd11bbb044d4e89b46313ca53bda07a4`; the enriched database remains `d0019aaf2d7358d4601660ef61da0a5171f337ec881a07813afb69a03262616f`.

## Sharing and operating boundaries

PDF and HTML carry probabilities, comparisons, sources and limitations together. CSV includes all 14 matchups; the eight held rows have empty probabilities and explicit reasons. JSON contains the public presentation payload. The release manifest hashes the public formats, PDF and receipt.

No verified bookmaker price snapshot is available. Event-page odds lack bookmaker capture/update times and are excluded; the report makes no EV, staking or operating decision. Injuries, camps, non-UFC records and later results are not modeled. The read-only dashboard retains its V3 operating defaults and existing fail-closed gates. The obsolete main-README next-steps passage is absent; archival provenance remains in legacy workflow documentation.
