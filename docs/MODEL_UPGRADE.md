# Model upgrade evidence

The task and measured review are in [the Codex brief](codex/MODEL_UPGRADE_PROMPT.md).
This work preserves the operating gates and the original research database.
The filename `data/ufc_research_2011_2025.sqlite` describes an old import scope;
its current contents span 1993–2026. Do not rename or clean up data as part of
this upgrade.

## Phase 0: fixed comparison protocol

```sh
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite \
  --identity-review reports/wikipedia_1993_2026_identity_review.json \
  --out reports/model_lab_phase0.json --phase 0
```

Outputs are exclusive-create: choose a new `--out` filename for subsequent
runs. `--phase` appends once to the explicitly appendable
`reports/model_upgrade_metrics.json`; omit it when reproducing a measurement.
The original DB is opened in SQLite read-only/query-only mode and its hash is
checked before and after. Frozen older schemas can be evaluated without
migrating the original DB.

Training ends on 2021-03-13, validation is 2021-03-20 through 2023-12-02,
and test begins on 2023-12-09. Added fights cannot move these dates. Same-date
results remain withheld until subsequent dates. Temperature uses validation
only. Comparisons report all test fights and the original retained subset,
paired event-date bootstrap intervals, hash-oriented calibration, and
favorite-oriented reliability. Captured market prices must pass the existing
two-sided, source-receipt and pre-event checks. Missing baselines are reported
as unavailable, with no fabricated market or birth-date values.

The published holdout is reproduced exactly in
`published_holdout_reproduction`: 800 test fights; Elo accuracy 52.25%, log
loss 0.6918962689; calibrated v2 accuracy 55.75%, log loss 0.6855588168;
temperature scale 1.3955675460. The main tables instead resolve exact 0.5 ties
using hash orientation: 52.125% and 55.625% respectively. Loss and Brier are
unchanged. The coin-flip classifier's deterministic ties score 49.625%; its
expected accuracy is 50%. The old sorted-ID tables remain solely to verify
the published result, not to claim better accuracy.

The checked worksheet/receipt reconciliation confirms 1,647 held fights:
659 in training, 332 in validation, and 656 in test (all outcomes). These
omissions make the original holdout conditional on accepted identities.
