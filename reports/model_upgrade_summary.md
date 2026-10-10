# Model upgrade summary

Required phases 0–6 are implemented on `model-accuracy-upgrade`, with a commit for each phase and no push. The prior completed-event capture, card review queue and licensing changes were preserved in commit `34b4ee1` before the upgrade. The code review in `docs/codex/MODEL_UPGRADE_PROMPT.md` guided the work.

Complete-card v3 improves fixed-test log loss from complete-card v2’s 0.68779 to **0.67123**, and accuracy from 53.63% to **57.43%**. Pooled rolling loss is **0.66591**, with a paired improvement interval that excludes zero. This is retrospective research evidence. The saved UFC 332 priced sample favors the market; the upgrade establishes no live wagering edge.

## Per-phase results and targets

All primary accuracy/calibration tables use `sha256(bout_id)` orientation, including exact 0.5 ties. Log loss and Brier are orientation-invariant. Fixed dates: train through 2021-03-13; validation 2021-03-20–2023-12-02; test from 2023-12-09.

| Phase / calibrated model | Test scope | Bouts | Accuracy | Log loss | Brier | Target outcome |
|---|---|---:|---:|---:|---:|---|
| 0 · v2 baseline | All | 800 | 55.625% | 0.68556 | 0.24625 | Met: exact legacy reproduction |
| 0 · v2 baseline | Originally kept | 800 | 55.625% | 0.68556 | 0.24625 | Same fitted model |
| 1 · Complete v2 | All | 1,447 | 53.628% | 0.68779 | 0.24734 | Met: count and both loss targets |
| 1 · Complete v2 | Originally kept | 800 | 52.125% | 0.69330 | 0.25002 | Same fitted model |
| 2 · v3 age only | All | 1,447 | 56.323% | 0.68026 | 0.24364 | Met: 0.6805 ± 0.003 |
| 2 · v3 age only | Originally kept | 800 | 57.875% | 0.66947 | 0.23850 | Same fitted model |
| 2 · v3 age + hinge | All | 1,447 | 56.116% | 0.67922 | 0.24324 | Met: 0.6794 ± 0.003 |
| 2 · v3 age + hinge | Originally kept | 800 | 58.125% | 0.66798 | 0.23793 | Same fitted model |
| 3 · Selected v3 | All | 1,447 | 57.429% | 0.67123 | 0.23946 | Met: loss ≤ 0.676; accuracy ≥ 56% |
| 3 · Selected v3 | Originally kept | 800 | 59.125% | 0.66002 | 0.23420 | Same fitted model |
| 4 · v3 carried from 3 | All | 1,447 | 57.429% | 0.67123 | 0.23946 | Met: ledger, guard, schedule, CLV |
| 4 · v3 carried from 3 | Originally kept | 800 | 59.125% | 0.66002 | 0.23420 | Same fitted model |
| 5 · v3 carried from 3 | All | 1,447 | 57.429% | 0.67123 | 0.23946 | Met: opt-in; defaults/gates preserved |
| 5 · v3 carried from 3 | Originally kept | 800 | 59.125% | 0.66002 | 0.23420 | Same fitted model |
| 6 · Frozen v3 replay | All | 1,447 | 57.429% | 0.67123 | 0.23946 | Met: deliverable and audit |
| 6 · Frozen v3 replay | Originally kept | 800 | 59.125% | 0.66002 | 0.23420 | Same fitted model |

Phase 0 separately reproduces the published sorted-ID holdout exactly: 800 bouts, Elo 52.25% / 0.69189627 and calibrated v2 55.75% / 0.68555882, temperature scale 1.39556755. Hash orientation changes tie accuracy to 52.125% / 55.625%; it does not change losses. V2 has four exact-probability ties. The legacy table is retained only for this acceptance check.

Phase 1 restores all 1,647 held bouts after reconciling 30 of the 1,677 worksheet positions. All restored methods come from verified retained pages; parser fallbacks and remaining held rows are both zero. Complete history has 790 events and 8,905 bouts, including 65 draws and 92 no contests. There are 911 provisional identities and 297 identity collision flags, with no automatic merges. Elo K=32 loss is 0.68433 and complete v2 loss is 0.68779, within both specified ±0.004 ranges.

Wikidata matches 1,898/1,899 real identities. Birth dates cover 1,809/1,878 original fighters and 1,828/1,899 complete-history real fighters; 615 have usable height. There are 22 original-cohort DOB conflicts, 23 across the expanded cohort, and 36 total attribute-conflict records. Conflicts remain reported. The expected birth-date coverage is met; height is slightly below the approximate 620 reference. Both dates are known on 763 test bouts: selected v3 accuracy 58.98%, loss 0.65932, Brier 0.23389; younger-fighter accuracy 61.99%. The younger classifier has no claimed probability loss/Brier. V3 excludes all three profile coverage flags.

Validation chooses K=64 from {32,48,64,96}; pre-test yearly folds choose fitting start 2010 from {1993,2001,2005,2010}. No labels after 2023-12-02 enter selection. Phase 6 replays these frozen choices once to add originally-kept rolling baselines. Weights, temperature, fixed-test metrics and all-bout rolling metrics equal Phase 3 exactly. Phases 4 and 5 carry those benchmark metrics without further fitting.

## Baselines, rolling folds and reliability

| Fixed test scope | Model | Bouts | Accuracy | Log loss | Brier |
|---|---|---:|---:|---:|---:|
| All | v3 | 1,447 | 57.43% | 0.67123 | 0.23946 |
| All | v2 | 1,447 | 53.63% | 0.68779 | 0.24734 |
| All | Elo K=64 | 1,447 | 55.98% | 0.68180 | 0.24439 |
| All | Coin flip | 1,447 | 50.45% | 0.69315 | 0.25000 |
| Originally kept | v3 | 800 | 59.13% | 0.66002 | 0.23420 |
| Originally kept | v2 | 800 | 52.12% | 0.69330 | 0.25002 |
| Originally kept | Elo K=64 | 800 | 52.88% | 0.69214 | 0.24950 |
| Originally kept | Coin flip | 800 | 49.62% | 0.69315 | 0.25000 |

| Rolling scope | Model | Bouts | Accuracy | Log loss | Brier | 95% date-block loss interval |
|---|---|---:|---:|---:|---:|---|
| All | v3 | 5,278 | 59.21% | 0.66591 | 0.23676 | [0.65969, 0.67221] |
| All | v2 | 5,278 | 54.15% | 0.68616 | 0.24655 | [0.68277, 0.68968] |
| All | Elo K=64 | 5,278 | 55.68% | 0.68297 | 0.24497 | [0.67886, 0.68736] |
| All | Coin flip | 5,278 | 51.17% | 0.69315 | 0.25000 | [0.69315, 0.69315] |
| Originally kept | v3 | 4,128 | 58.84% | 0.66705 | 0.23736 | [0.65967, 0.67435] |
| Originally kept | v2 | 4,128 | 52.45% | 0.69293 | 0.24985 | [0.68938, 0.69653] |
| Originally kept | Elo K=64 | 4,128 | 54.05% | 0.68848 | 0.24768 | [0.68348, 0.69346] |
| Originally kept | Coin flip | 4,128 | 51.31% | 0.69315 | 0.25000 | [0.69315, 0.69315] |

Pooled v3 accuracy 95% interval: [57.80%,60.55%]. Paired v3-minus-v2 loss interval: **[−0.02535,−0.01487]**, excluding zero; originally-kept paired interval: [−0.03231,−0.01901], also excluding zero. The fixed-test paired interval is [−0.02687,−0.00645]. The preliminary Phase 2 hinge interval [−0.01809,+0.00054] includes zero. Intervals resample whole event dates, 2,000 repeats; they do not measure identity/provenance bias.

Younger-fighter rolling accuracy is 59.86% on 3,911 bouts (95% interval [58.27%,61.47%]). Coin classifier accuracy reflects deterministic ties; its expected accuracy is 50%. No captured historical market prices exist for these fixed/rolling splits, so the market baseline is explicitly unavailable. The review’s typical ~65%+ market accuracy is a reference, not a measured holdout benchmark.

Each yearly fold trains on earlier dates and calibrates on the last 15% of eligible pre-year dates. Every yearly and pooled test comparison includes both all and originally-kept subsets in the JSON report.

| Test year | Bouts | v3 accuracy | v3 loss | v3 Brier | 95% loss interval | v2 loss | Elo loss |
|---|---:|---:|---:|---:|---|---:|---:|
| 2016 | 482 | 63.49% | 0.65514 | 0.23157 | [0.63466, 0.67540] | 0.68155 | 0.67287 |
| 2017 | 446 | 57.40% | 0.67126 | 0.23917 | [0.65064, 0.69219] | 0.70176 | 0.68391 |
| 2018 | 469 | 57.78% | 0.66678 | 0.23711 | [0.65028, 0.68331] | 0.68897 | 0.68296 |
| 2019 | 506 | 59.09% | 0.67874 | 0.24281 | [0.65762, 0.69783] | 0.68892 | 0.70177 |
| 2020 | 444 | 61.26% | 0.66549 | 0.23633 | [0.64576, 0.68567] | 0.68186 | 0.67785 |
| 2021 | 497 | 60.36% | 0.66676 | 0.23716 | [0.65034, 0.68372] | 0.68097 | 0.68186 |
| 2022 | 506 | 62.65% | 0.65516 | 0.23140 | [0.63747, 0.67353] | 0.67794 | 0.67805 |
| 2023 | 504 | 56.55% | 0.65833 | 0.23347 | [0.64063, 0.67690] | 0.68914 | 0.68547 |
| 2024 | 513 | 57.89% | 0.66648 | 0.23722 | [0.64418, 0.69071] | 0.68098 | 0.68141 |
| 2025 | 513 | 59.84% | 0.65685 | 0.23258 | [0.63735, 0.67721] | 0.68506 | 0.68012 |
| 2026 | 398 | 54.02% | 0.68924 | 0.24806 | [0.66542, 0.71276] | 0.69352 | 0.68621 |

The 2026 fold is weaker and partial through September 19. Fixed-test favorite reliability is shown below and in the Model tab.

| Favorite probability | Bouts | Mean predicted | Observed favorite wins |
|---|---:|---:|---:|
| 50%–60% | 856 | 54.28% | 52.34% |
| 60%–70% | 423 | 64.35% | 62.17% |
| 70%–80% | 135 | 73.37% | 67.41% |
| 80%–90% | 33 | 82.23% | 87.88% |
| 90%–100% | 0 | — | — |

## Forward-test ledger and live behavior

The append-only, hash-chained ledger preserves the original September 27 16:43:11Z v2 forecasts for UFC 332 (October 3 20:00Z). Its recovery entry was recorded after the event, on October 10 UTC / October 9 local time. Probabilities and input hashes were preserved; this is not a new prospective v3 forecast.

| Same-event comparison | Bouts | Accuracy | Log loss | Brier |
|---|---:|---:|---:|---:|
| Saved model · all | 8 | 62.50% | 0.64123 | 0.22464 |
| Saved model · priced subset | 4 | 25.00% | 0.71285 | 0.25985 |
| No-vig market · priced subset | 4 | 75.00% | 0.54111 | 0.18343 |

One event is settled; cumulative results equal its results. The winners are Silva, Talbott, Ribovics, Soldić, Kopylov, Walker, Hernandez and Naurdiev. Completed revision 1379225905 is retained and verified. Participants match by stable IDs, independent of winner-first order or card position. Green’s weight-class hold remains visible and was accepted only for research settlement. Nothing was promoted into operating results or wager permission.

The default market gap limit is 0.10. Figueiredo-like 0.52 versus 0.17 and Green-like 0.52 versus 0.30 rows become `pass_market_disagreement`, regardless of positive EV. A partly retained selected book cannot hide another verified two-sided market. Overrides require the explicit `--max-market-disagreement` flag and do not relax other gates.

T-7d, T-24h and T-2h capture commands support real slot windows and idempotent receipts, with a quota floor of 50. All three commands were exercised offline with mocked responses; repeated captures make no new provider request. Paper and actual CLV use verified entry prices and a later observed same-book two-sided pre-start snapshot. UFC 332 lacks a closing proxy, so no CLV is claimed. No actual wagers were placed by this upgrade.

`--training-history` on score/alert/paper fits labelled research-retrospective v3 from a separate read-only DB. The recorded full-history smoke uses 5,985 training and 1,504 calibration bouts through September 19 against an explicitly synthetic card with zero operating results. Defaults refuse insufficient operating history; opt-in forecasts retain `research_retrospective`, and operating model readiness remains false. Same-day/future labels and future-fetched target profiles are excluded. Live calibration keeps the existing whole-date 20% bout tail; rolling evaluation uses 15% of pre-year dates.

## Deliverable, verification and limitations

The one-page model card, 70-line README, exact event workflow, and six-tab read-only dashboard are delivered. Upcoming cards show model/verified no-vig probabilities, market-gap passes, the three largest saved feature contributions (`weight × value × calibration scale`, in logit units), and per-fighter coverage. Saved contributions must reproduce the probability. The Model tab shows yearly baseline losses and favorite reliability; Forward test shows matched per-event/cumulative losses and Brier.

All **428 offline tests pass in the actual UFC repository**. Streamlit runtime checks render all six tabs and the retained model/forward data with no exceptions. Hash checks show all 5,935 existing protected files unchanged during this runtime check. Receipt verification also succeeds while former development-copy paths are simulated missing. The local health endpoint returned `ok`; screenshot review was unavailable because no browser was enabled.

The wheel builds and installs; installed CLI, migrations 014/015 and completed-capture verification work outside the repository with site packages disabled. Core dependencies remain `[]`. Existing data and old reports are preserved; phase metrics append one entry each. The original DB SHA-256 remains:

`e9a24dc1e69599deedb083c60fab47b3fd11bbb044d4e89b46313ca53bda07a4`

No fight-statistics signal, historical odds, established market edge or v3 prospective validation is claimed. Missing ages, provisional identities, current identity mappings, static facts fetched retrospectively, temporal drift and the small forward sample remain limitations. The project is for research, not betting advice.

## Skipped work and open decisions

Optional experience-scaled/MOV Elo K was not evaluated or adopted. Constant K and the training window were selected through the required validation/fold comparison; extra modifiers remain a later controlled experiment. Optional Phase 7 pre-UFC article-record parsing was skipped to finish the required data, evaluation and forward workflow first. No pre-UFC features or coverage gains are claimed.

Tyler’s remaining decisions:

1. Whether to license/permit a fight-statistics source; UFCStats-derived datasets remain research-only with unclear rights.
2. Whether to buy historical odds for matched market comparisons and richer features.
3. Whether to make research training the default, and separately whether its evidence should qualify for operating readiness. Current defaults and gates remain unchanged.
4. Whether to rename the legacy research DB and clean up old copies after checking report/receipt references.

Cleanup candidates, report only: `data/csv-check.sqlite`, `data/dashboard-research-20260926T2217Z.sqlite`, `data/dashboard-research-20260926T2233Z.sqlite`, `data/dashboard_demo.sqlite`, `data/demo.sqlite`, `data/identity-clone-20260926T2230Z.sqlite`, `data/packaging.sqlite`, `data/sample.sqlite`; review the older `data/ufc_research.sqlite` and `data/ufc_research_2026_candidate.sqlite` staging copies too. Some copies may anchor saved evidence, so check references before any later cleanup. Keep the original, complete and enriched research DBs and the UFC 332 operating intake as evidence. No cleanup/rename was performed. `ufc_research_2011_2025.sqlite` actually spans 1993–2026.

## Exact reproduction commands

Run from this repository after installation. Choose a fresh `REPRO_TAG` for each run; outputs refuse existing paths. These commands omit `--phase` so recorded phase entries cannot be duplicated. Frozen inputs and retained receipts reproduce the saved measurements; source refreshes would be a new experiment.

```sh
source .venv/bin/activate
REPRO_TAG=$(date -u +%Y%m%dT%H%M%SZ)

# Phase 0, including the exact published legacy table.
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite   --identity-review reports/wikipedia_1993_2026_identity_review.json   --out "reports/reproduce-phase0-${REPRO_TAG}.json"

# Phase 1: restoration counts, methods, identities and original hash.
ufc-model research-complete-history --db data/ufc_research_2011_2025.sqlite   --out-db "data/reproduce-complete-${REPRO_TAG}.sqlite"   --identity-report "reports/reproduce-identities-${REPRO_TAG}.json"   --raw-dir "data/raw/reproduce-complete-${REPRO_TAG}"
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite   --complete-db data/ufc_research_complete.sqlite   --identity-review reports/wikipedia_1993_2026_identity_review.json   --out "reports/reproduce-phase1-${REPRO_TAG}.json"

# Phase 2: cached profile checkpoint replay; no network or source refresh.
ufc-model import-wikidata-profiles --db data/ufc_research_complete_profiles.sqlite   --out-db "data/reproduce-profiles-${REPRO_TAG}.sqlite" --offline   --report-out "reports/reproduce-profiles-${REPRO_TAG}.json"
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite   --complete-db data/ufc_research_complete_profiles.sqlite   --identity-review reports/wikipedia_1993_2026_identity_review.json   --features age --out "reports/reproduce-age-${REPRO_TAG}.json"
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite   --complete-db data/ufc_research_complete_profiles.sqlite   --identity-review reports/wikipedia_1993_2026_identity_review.json   --features age-hinge --out "reports/reproduce-hinge-${REPRO_TAG}.json"

# Phases 3 and 6: pre-test selection, then fixed/rolling scores and all CIs.
ufc-model research-select --db data/ufc_research_complete_profiles.sqlite   --out "reports/reproduce-selection-${REPRO_TAG}.json"
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite   --complete-db data/ufc_research_complete_profiles.sqlite   --identity-review reports/wikipedia_1993_2026_identity_review.json   --selection "reports/reproduce-selection-${REPRO_TAG}.json" --rolling-origin   --out "reports/reproduce-final-${REPRO_TAG}.json"

# Phase 4: recover the exact saved probabilities into a NEW ledger and settle.
ufc-model forecast freeze wikipedia_pilot:83826247   --source reports/ufc332-forward-research-full-20260927T1643Z.json   --ledger "reports/reproduce-ledger-${REPRO_TAG}.jsonl"
ufc-model forecast settle wikipedia_pilot:83826247   --completed-manifest data/raw/ufc332-completed-20261009/manifest.json   --ledger "reports/reproduce-ledger-${REPRO_TAG}.jsonl"
ufc-model forecast summary --ledger "reports/reproduce-ledger-${REPRO_TAG}.jsonl"

# Phase 5: fixed-cutoff synthetic behavior check; no real card, odds or wager.
python scripts/verify_research_training.py   --history data/ufc_research_complete_profiles.sqlite   --cutoff 2026-10-10T01:00:00Z   --output "reports/reproduce-training-${REPRO_TAG}.json"

# Final verification and local dashboard.
python -m unittest discover -s tests
python -m pip wheel --no-deps --wheel-dir "/tmp/ufc-wheel-${REPRO_TAG}" .
python -m streamlit run app.py --server.address 127.0.0.1
```

The profile replay reports zero **new** attribute rows because it starts from the frozen enriched checkpoint; its coverage and conflicts match the retained queries. The initial import command is documented in `docs/MODEL_UPGRADE.md`. Cohort-specific coverage and all yearly/baseline/reliability values come from the retained DB and comparison JSONs. Exact prepare, scheduled capture, freeze and completed-page capture commands are in `docs/EVENT_WORKFLOW.md`; they require reviewed future event details and actual due times.

To print all recorded phase test values, rolling numbers and forward results without refitting:

```sh
python - <<'READ_REPORT'
import json
from pathlib import Path
for entry in json.loads(Path('reports/model_upgrade_metrics.json').read_text())['phases']:
    print('phase', entry['phase'], json.dumps(entry['report']['test'], indent=2))
r = json.loads(Path('reports/model_lab_phase6.json').read_text())
print(json.dumps(r['rolling_origin'], indent=2))
print(json.dumps(r['test']['all']['logistic_calibrated']['favorite_reliability'], indent=2))
from ufc_odds_model.forecast_ledger import summary
print(json.dumps(summary(), indent=2))
READ_REPORT
```

## Files changed

The following lists upgrade changes after the preserved pre-existing-work commit, plus the final report/audit. The Codex brief itself is retained as the specification.

```
README.md
app.py
docs/DASHBOARD_RUNBOOK.md
docs/EVENT_WORKFLOW.md
docs/LEGACY_WORKFLOW_REFERENCE.md
docs/MODEL_CARD.md
docs/MODEL_UPGRADE.md
docs/codex/MODEL_UPGRADE_PROMPT.md
pyproject.toml
reports/forecast_ledger.jsonl
reports/forward_ledger_phase4.json
reports/model_lab_phase0.json
reports/model_lab_phase1.json
reports/model_lab_phase2.json
reports/model_lab_phase2_age_only.json
reports/model_lab_phase3.json
reports/model_lab_phase3_selection.json
reports/model_lab_phase6.json
reports/model_upgrade_audit.json
reports/model_upgrade_metrics.json
reports/model_upgrade_summary.md
reports/research_complete_identity_report.json
reports/research_training_phase5.json
reports/wikidata_profiles_report.json
scripts/capture_completed_event.py
scripts/rehearse_event.py
scripts/verify_research_training.py
src/ufc_odds_model/alerts.py
src/ufc_odds_model/cli.py
src/ufc_odds_model/dashboard_data.py
src/ufc_odds_model/elo.py
src/ufc_odds_model/evaluation.py
src/ufc_odds_model/features.py
src/ufc_odds_model/forecast_ledger.py
src/ufc_odds_model/logistic.py
src/ufc_odds_model/market_guard.py
src/ufc_odds_model/model_lab.py
src/ufc_odds_model/odds_api.py
src/ufc_odds_model/odds_schedule.py
src/ufc_odds_model/pipeline.py
src/ufc_odds_model/research_complete.py
src/ufc_odds_model/research_evaluation.py
src/ufc_odds_model/research_training.py
src/ufc_odds_model/retained_paths.py
src/ufc_odds_model/sql/migrations/014_static_fighter_attributes.sql
src/ufc_odds_model/sql/migrations/015_forecast_metadata_and_odds_schedule.sql
src/ufc_odds_model/wikidata_profiles.py
tests/test_capture_completed_event.py
tests/test_dashboard_app.py
tests/test_forecast_ledger.py
tests/test_market_guard.py
tests/test_model_lab.py
tests/test_odds_schedule.py
tests/test_prefight_cli.py
tests/test_research_complete.py
tests/test_research_training.py
tests/test_retained_paths.py
tests/test_upgrade_dashboard.py
tests/test_wikidata_profiles.py
```
