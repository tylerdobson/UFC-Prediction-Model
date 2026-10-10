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
