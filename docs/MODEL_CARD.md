# UFC winner model · v3

**Purpose:** estimate a fighter’s chance of winning a binary UFC bout. Research
use, not betting advice. Draws/no contests are excluded from binary scores;
draws update later history. The app does not place wagers.

**Data:** 790 Wikipedia events, 1993-11-12–2026-09-19; 8,905 bouts versus 7,258
originally kept. Restored 1,647 held bouts with verified methods, zero parser
fallbacks and no remaining held exclusions. Unlinked fighters retain 911
provisional IDs; collision flags require review. Wikidata CC0 supplies valid
birth dates for 1,809/1,878 original real fighters and 1,828/1,899 real fighters
in complete history. Both dates are known for 763/1,447 test bouts (52.7%).
Provisional fighters usually lack ages, so coverage still limits performance.

**Method:** earlier-date Elo/history, age difference and an age-above-32 hinge;
regularized symmetric logistic, with later temperature calibration. V3 drops
the three profile/stat coverage flags. Missing values are neutral. K=64 and
fitting start 2010 were selected on validation/pre-test folds. Train ends
2021-03-13; validation is 2021-03-20–2023-12-02; test starts 2023-12-09.
Calibration uses hash orientation and favorite reliability, not sorted IDs.

| Evaluation | Model | N | Accuracy | Log loss | Brier |
|---|---|---:|---:|---:|---:|
| Fixed complete test | v3 | 1,447 | 57.43% | 0.67123 | 0.23946 |
| Same test | v2 | 1,447 | 53.63% | 0.68779 | 0.24734 |
| Same test | Elo K=64 | 1,447 | 55.98% | 0.68180 | 0.24439 |
| Same test | Coin flip | 1,447 | 50.45% | 0.69315 | 0.25000 |
| Originally kept test | v3 | 800 | 59.13% | 0.66002 | 0.23420 |
| Rolling 2016–2026 | v3 | 5,278 | 59.21% | 0.66591 | 0.23676 |
| Same rolling folds | v2 | 5,278 | 54.15% | 0.68616 | 0.24655 |

Rolling v3 loss 95% date-block interval: **[0.65969, 0.67221]**; accuracy
**[57.80%, 60.55%]**. Paired v3-minus-v2 loss interval **[−0.02535, −0.01487]**
excludes zero. Each fold calibrates on the last 15% of earlier dates. The 2026
fold is weaker (54.02%, loss 0.68924). With both birth dates, v3 scores 58.98%,
loss 0.65932; the younger-fighter classifier scores 61.99% on those 763 bouts,
without claiming probability calibration. Coin accuracy reflects hash ties.

| Favorite confidence | N | Mean predicted | Observed wins |
|---|---:|---:|---:|
| 50–60% | 856 | 54.28% | 52.34% |
| 60–70% | 423 | 64.35% | 62.17% |
| 70–80% | 135 | 73.37% | 67.41% |
| 80–90% | 33 | 82.23% | 87.88% |
| 90–100% | 0 | — | — |

**Forward ledger:** recovered September 27 v2 forecasts for UFC 332 score
5/8, loss 0.64123, Brier 0.22464. On four priced bouts: model 25%, loss 0.71285,
Brier 0.25985; market 75%, loss 0.54111, Brier 0.18343. These are the same bouts.
Recovery was after the event; it does not validate v3. No closing proxy or
actual wager was captured/placed by this upgrade.

**Limits:** no real fight statistics, historical odds or established market
edge. The review’s typical ~65%+ market accuracy is not measured on this
holdout. Static profiles are retrospective research facts; live target
attributes require pre-decision fetch receipts. Name collisions, current
identity mappings, missing ages and temporal drift remain. Bootstrap CIs
measure date-resampling variation, not provenance bias. The opt-in training
path remains `research_retrospective`; it cannot promote operating decisions.
Defaults and all freshness, roster, identity and model-readiness gates remain.
The default market-gap guard passes on gaps above 0.10.

[Methods and reproductions](MODEL_UPGRADE.md) · [Event workflow](EVENT_WORKFLOW.md)
