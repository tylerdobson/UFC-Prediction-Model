# UFC Prediction Model

A local Python/SQLite research project that predicts UFC bout winners, compares probabilities with source-verified bookmaker prices, and records frozen forecasts for later evaluation. Complete Wikipedia cards and CC0 Wikidata birth dates improve the model; the read-only Streamlit dashboard shows results, coverage, uncertainty and the market gap. The core uses only Python’s standard library. This is research, not betting advice; no component places wagers.

## Current performance

Lower log loss and Brier are better. These are saved retrospective measurements, not a claim of betting profit.

| Evaluation | Model | Bouts | Accuracy | Log loss | Brier |
|---|---|---:|---:|---:|---:|
| Fixed test · complete cards | v3 · age + decline | 1,447 | 57.43% | 0.67123 | 0.23946 |
| Same fixed test | v2 · result only | 1,447 | 53.63% | 0.68779 | 0.24734 |
| Same fixed test | Elo K=64 | 1,447 | 55.98% | 0.68180 | 0.24439 |
| Same fixed test | Coin flip | 1,447 | 50.45% | 0.69315 | 0.25000 |
| Fixed test · originally kept | v3 | 800 | 59.13% | 0.66002 | 0.23420 |
| Rolling origin · 2016–2026 | v3 | 5,278 | 59.21% | 0.66591 | 0.23676 |
| Same rolling folds | v2 | 5,278 | 54.15% | 0.68616 | 0.24655 |
| UFC 332 · saved v2 forward | Model · all | 8 | 62.50% | 0.64123 | 0.22464 |
| UFC 332 · same priced subset | Model | 4 | 25.00% | 0.71285 | 0.25985 |
| UFC 332 · same priced subset | No-vig market | 4 | 75.00% | 0.54111 | 0.18343 |

Rolling v3 loss has a 95% event-date bootstrap interval **[0.65969, 0.67221]**. The paired v3-minus-v2 interval **[−0.02535, −0.01487]** excludes zero. The 2026 fold is weaker: 54.02% accuracy, loss 0.68924. The younger-fighter classifier scores 61.99% on the 763 fixed-test bouts with both birth dates; it is not a calibrated probability model.

No historical prices exist for the holdout. The review’s typical ~65%+ market accuracy is a reference, not a measured benchmark here. UFC 332’s priced sample favors the market but is only four bouts and uses the older v2 model. No closing snapshot or live wagering edge is established. Exact 0.5 ties use deterministic hash orientation.

## Quickstart

Use Python 3.11+ from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dashboard]'
python -m unittest discover -s tests
python -m streamlit run app.py --server.address 127.0.0.1
```

The dashboard reads local retained databases and reports. Missing operating cards or evidence are shown as unavailable. It never fetches a source, fits a model, modifies a database or places a wager. Selected model reports and the forecast ledger are committed; databases, raw receipts and credentials stay local.

Inspect the settled forecast ledger without an operating database:

```sh
ufc-model forecast summary
```

For an explicitly labelled forecast on a reviewed future operating card:

```sh
ufc-model --db PATH_TO_OPERATING_DB score-event EVENT_ID --model logistic \
  --training-history data/ufc_research_complete_profiles.sqlite
```

This opt-in uses `logistic-prior-v3:research_retrospective`. Defaults still use source-dated operating history. Research training does not pass operating model readiness; alert and paper gates remain fail-closed. Event preparation, source capture, the T-7d/T-24h/T-2h schedule and settlement commands are in [EVENT_WORKFLOW.md](docs/EVENT_WORKFLOW.md).

## How it works

The research history contains 790 events and 8,905 bouts from 1993–2026, including 1,647 restored held rows. Unresolved names receive provisional IDs without automatic merges. Earlier-date results supply Elo, experience, win rate, rest and recent form. Birth dates supply age and an age-above-32 decline term. V3 excludes age/reach/stat coverage flags; missing optional inputs are neutral.

Training, calibration and test dates are fixed. K=64 and fitting start 2010 were chosen on validation and pre-test rolling folds. Yearly folds train/calibrate before each test year. Every saved forecast records its cutoff, parameters, probabilities, contributions, coverage and hashes. Alerts reject a verified model–market gap above 10 percentage points by default, along with stale prices, stale rosters, unresolved identities and inadequate model evidence.

The legacy filename `data/ufc_research_2011_2025.sqlite` actually contains **1993–2026**. It remains byte-identical; restored history and profiles live in new database copies. No data, backup, raw receipt or old report was deleted, renamed or overwritten.

## Documentation

- [One-page model card](docs/MODEL_CARD.md): coverage, metrics, reliability and limitations.
- [Upgrade methods and exact experiment commands](docs/MODEL_UPGRADE.md).
- [Final upgrade report](reports/model_upgrade_summary.md): per-phase targets, skips, decisions and reproduction.
- [Event workflow](docs/EVENT_WORKFLOW.md) and [dashboard runbook](docs/DASHBOARD_RUNBOOK.md).
- [Data-source decision](docs/DATA_SOURCE_DECISION.md), [dataset card](docs/RESEARCH_DATASET_CARD.md) and [dated observations](docs/POINT_IN_TIME_OBSERVATIONS.md).
- [Provenance and earlier workflow reference](docs/LEGACY_WORKFLOW_REFERENCE.md), [backup runbook](docs/BACKUP_RUNBOOK.md) and [evidence recovery](docs/EVIDENCE_BUNDLE.md).
