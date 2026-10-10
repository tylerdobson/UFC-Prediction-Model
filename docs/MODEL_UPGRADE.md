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

## Phase 1: complete-card research history

```sh
ufc-model research-complete-history --db data/ufc_research_2011_2025.sqlite \
  --out-db data/ufc_research_complete.sqlite
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite \
  --complete-db data/ufc_research_complete.sqlite \
  --identity-review reports/wikipedia_1993_2026_identity_review.json \
  --out reports/model_lab_phase1.json --phase 1
```

The original 1,677 worksheet positions reconcile to 30 reviewed identity
recoveries and 1,647 genuinely held fights. All 1,647 were reparsed from intact
retained wikitext, including embedded cards; no review-JSON fallback was needed.
The SQLite backup API creates a separate DB. Both DB outputs and reports refuse
existing paths. The original DB hash remains
`e9a24dc1e69599deedb083c60fab47b3fd11bbb044d4e89b46313ca53bda07a4`.

Restored fights keep method/outcome and carry source
`wikipedia_research_restored`. A content-addressed restoration manifest records
the source receipt for every restored fight. Title redirects and normalization
come only from hash-verified saved Action API responses. Plain/unresolved names
use NFKC/casefold/whitespace-normalized provisional IDs and are never merged
automatically, even when a canonical real name matches. The identity report
flags same-date appearances, nearby weight-class changes, and name collisions;
early tournaments and genuine weight changes can produce valid flags.

All binary test fights: 1,447; Elo K=32 accuracy 55.56%, log loss 0.6843,
Brier 0.2456; calibrated v2 accuracy 53.63%, log loss 0.6878, Brier 0.2473.
The count and both loss targets are met. No held rows remain. These scores
measure the complete result history and provisional identities; restored data
alone does not make the model strong. The originally kept subset is evaluated
using this same fitted model, with restored earlier fights in its history.

## Phase 2: CC0 static attributes and coverage-safe v3

```sh
ufc-model import-wikidata-profiles --db data/ufc_research_complete.sqlite \
  --out-db data/ufc_research_complete_profiles.sqlite
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite \
  --complete-db data/ufc_research_complete_profiles.sqlite \
  --identity-review reports/wikipedia_1993_2026_identity_review.json \
  --features age --out reports/model_lab_phase2_age_only.json
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite \
  --complete-db data/ufc_research_complete_profiles.sqlite \
  --identity-review reports/wikipedia_1993_2026_identity_review.json \
  --features age-hinge --out reports/model_lab_phase2.json --phase 2
```

Migration 014 appends immutable static values and query receipts. The importer
uses saved page-title lookups, falls back to pageprops batches of 50, queries
at most 250 titles/items per SPARQL POST, waits at least 1.5 seconds between
requests, and honors numeric or HTTP-date Retry-After. Requests and the exact
response bytes are retained for cached/offline reproduction. Day precision is
required for birth dates; distinct day-precision dates cause the fighter to be
skipped and reported. Normalized SI heights become cm; height is not reach and
does not fill the reach feature. UFC athlete IDs remain external review IDs.
Provisional names are not mapped to Wikidata automatically.

Wikidata structured data uses [CC0](https://www.wikidata.org/wiki/Wikidata:Data_access).
The API contracts are described by [Wikidata's SPARQL service](https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service)
and [MediaWiki pageprops](https://www.mediawiki.org/wiki/API:Pageprops).
Usable birth dates cover 1,809 of the original 1,878 fighters. The complete DB
adds 21 linked fighters: 1,828 of 1,899 real identities now have birth dates,
1,898 match an item, and 615 have unambiguous usable height. The report lists
birth, height and external-ID conflicts rather than selecting convenient values.

V2 remains reproducible with result-only features. V3 excludes all three
profile-coverage difference flags from its vector. Coverage is metadata only;
missing paired ages produce neutral age/hinge values. Static research facts
can apply retrospectively. Operating scoring requires an intact receipt
fetched strictly before the cutoff; dated observations retain both of their
existing timestamp checks. Conflicting static and dated birth dates fail closed.

Age-only: 1,447 test fights, 56.32% accuracy, log loss 0.6803. Age plus the
decline-above-32 hinge: 56.12%, log loss 0.6792, Brier 0.2432. Both loss targets
are met. On 763 test fights with both birth dates, hinge v3 scores 58.06%,
log loss 0.6672; the younger-fighter classifier is 61.99% accurate. The paired
v3-minus-v2 log-loss 95% interval is [-0.01809, +0.00054], which includes zero.

## Phase 3: validation selection and rolling-origin evaluation

```sh
ufc-model research-select --db data/ufc_research_complete_profiles.sqlite \
  --out reports/model_lab_phase3_selection.json
ufc-model research-compare --db data/ufc_research_2011_2025.sqlite \
  --complete-db data/ufc_research_complete_profiles.sqlite \
  --identity-review reports/wikipedia_1993_2026_identity_review.json \
  --selection reports/model_lab_phase3_selection.json --rolling-origin \
  --out reports/model_lab_phase3.json --phase 3
```

Selection never loads labels later than 2023-12-02. K=64 minimizes validation
Elo loss (0.68132 versus K=32's 0.68422). Training start 2010 minimizes pooled
2016–2023 pre-test fold loss (0.66453 versus 2005's 0.66616). The 2023 selection
fold ends on 2023-12-02; it excludes the fixed-test dates. The selection
manifest is frozen, bound to the database hash, then consumed by evaluation.
K, training start, features, weights and temperature are recorded; the model
version hash includes K and the training window. Experience/method K modifiers
are optional and were not adopted.

Each yearly fold fits on earlier dates, holding out the last 15% of eligible
pre-year dates for temperature. Earlier results still inform Elo/history;
the training-start window selects fitting rows, not an artificial rating reset.
Year Y is never used for its weights or temperature. Both all-test and retained
test subsets are shown. The full 2016–2026 evaluation is run only after selection.

Fixed test: 1,447 fights, 57.43% accuracy, log loss 0.67123, Brier 0.23946.
The accuracy >=56% and loss <=0.676 targets are met. The original 800 retained
test fights score 59.13%, log loss 0.66002, Brier 0.23420.
Rolling pooled: 5,278 fights, 59.21% accuracy, log loss 0.66591 (95% date-block
interval [0.65969, 0.67221]); v2 loss 0.68616. The paired v3-minus-v2 interval
[-0.02535, -0.01487] excludes zero. Yearly metrics and intervals are in the
report. The 2026 fold is weaker: 54.02%, loss 0.68924 through September 19.

The review uses typically ~65%+ accuracy as a no-vig market reference. This
repository has no captured historical prices to verify that benchmark on the
holdout. The model does not establish market parity; richer fight statistics
or market inputs would be needed to close that gap. Retrospective improvement
also does not prove prospective wagering value.

## Phase 4: frozen forward forecasts and the market guardrail

The append-only `reports/forecast_ledger.jsonl` preserves the September 27 v2
forecasts, their original cutoff and input hashes. Recovery on October 9 is
explicitly marked after-event recovery, not a newly prospective freeze. The
completed Wikipedia capture retains revision 1379225905, response bytes,
fetch receipt and the verified pre-fight anchor. Results match by participant
IDs, so winner-first ordering and card-position changes cannot change labels.
Eight saved forecasts settle: 5/8 correct, log loss 0.64123, Brier 0.22464.
On the same four bouts with saved two-sided prices, model accuracy is 25%,
loss 0.71285, Brier 0.25985; market accuracy is 75%, loss 0.54111, Brier 0.18343.
The saved market rule averages each book's no-vig probability. Weight-class
holds remain visible and cannot approve an operating result or wager.

`forecast freeze`, `forecast settle`, and `forecast summary` verify the ledger
hash chain. New freezes require a future verified start and a full scored card.
Repeated scoring creates a new CSV; reports are never replaced. Missing
absolute receipt paths may be rebased under the copied repo's `data/raw`,
with the original hashes still required; an existing changed file is refused.

Scoring and pre-fight alerts now return `pass_market_disagreement` whenever
the absolute model/verified two-sided market gap exceeds 0.10. The explicit
`--max-market-disagreement` override is persisted; every other gate remains.
Figueiredo-like 0.52/0.17 and Green-like 0.52/0.30 predictions are rejected even
with positive calculated EV. This guardrail does not establish profitability.

`odds-schedule` supports T-7d, T-24h and T-2h captures within the actual slot
window; no backdating. It checks quota on the zero-cost sports endpoint and
refuses below 50 remaining requests. Responses also enforce the header floor.
See [The Odds API documentation](https://the-odds-api.com/liveapi/guides/v4/).
`clv` compares paper and actual entry prices with the last later verified
same-book, two-sided, pre-start snapshot. Positive price CLV is
`entry_decimal / closing_decimal - 1`. One early capture cannot establish CLV;
UFC 332 has no observed closing proxy. No actual wagers were placed.
Exact event commands are in [EVENT_WORKFLOW.md](EVENT_WORKFLOW.md).
