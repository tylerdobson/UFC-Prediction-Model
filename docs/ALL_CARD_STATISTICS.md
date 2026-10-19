# All-card historical fighter statistics

This workflow maintains a separate, source-verified statistics store for every card in the retained UFCStats archive. It supports historical card comparisons, new-card comparisons and evaluated model training. It does not modify the original research databases or earlier frozen forecasts.

## What is stored

The normalized store retains canonical source IDs, event dates, fight results, round-level and fight-level facts, elapsed time where the round format can be interpreted, fighter profiles and source hashes. Statistics include significant and total strikes, head/body/leg and distance/clinch/ground breakdowns, takedowns, knockdowns, submission attempts, reversals and available control time. Each missing field remains distinct from a measured zero.

Duplicate event labels referring to the same official fight URL are deduplicated. Ambiguous rematches, inconsistent participant/outcome matches, incomplete rounds and invalid counts are held with explicit reasons. A separate conservative crosswalk connects established research identities to canonical statistics identities. Unresolved identities remain visible in coverage reports.

Per-minute rates require validated elapsed time. Accuracy requires attempts; takedown defense uses opponents' attempts and successes. Each displayed metric reports its contributing history rather than pretending every field covers the same set of bouts. Career averages published today are never used to reconstruct an earlier fight.

## Inspect the existing store

After installing the project, use `ufc-statistics`. The equivalent command without installation is `PYTHONPATH=src python -m ufc_odds_model.statistics_workflow`.

```sh
ufc-statistics events --stats-db data/ufc_fight_statistics.sqlite --query 'UFC 300'
ufc-statistics fighters --stats-db data/ufc_fight_statistics.sqlite --query 'McGregor'
ufc-statistics card --stats-db data/ufc_fight_statistics.sqlite \
  --event-id ufcstats:event:6750e338922a099d --out-dir output/UFC-300-statistics-NEW
```

The card command accepts a canonical archive event ID or a research event ID with `--history data/ufc_research_complete_profiles.sqlite`. It exports self-contained HTML, CSV and JSON. Historical cards use only earlier fights. Missing or ambiguous identities produce explicit gaps, not guessed joins. Reach is labeled as an approximate static attribute from a retrospective capture.

For a future card, provide its source-listed manifest instead of an event ID:

```sh
ufc-statistics card --stats-db data/ufc_fight_statistics.sqlite \
  --card reports/YOUR-CARD.json --out-dir output/YOUR-CARD-statistics-NEW
```

Use established research IDs or canonical UFCStats IDs for participants. Each target's statistics are cut off before the earlier of its event date and the requested `--as-of` date. Target and same-date statistics are excluded. A two-day timing embargo also excludes records immediately before the target event date, including other bouts from the same tournament, so a source date disagreement cannot leak target-event statistics into history.

## Refresh and backfill

A refresh resolves a Git commit first, then downloads only the required CSV and text artifacts from that immutable revision. It retains response metadata and content hashes and never executes downloaded repository code. A partial download is not published as a usable capture.

```sh
ufc-statistics refresh --raw-root data/raw/statistics-refresh
```

The command prints the new manifest path. Pass it to the builder with a new database path when changing source revisions:

```sh
ufc-statistics build \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --history data/ufc_research_complete_profiles.sqlite \
  --db data/ufc_fight_statistics.sqlite \
  --coverage-json reports/statistics_coverage_all_cards.json \
  --coverage-csv reports/statistics_coverage_all_cards.csv
```

Repeating an identical build verifies and reuses the existing store. A changed archive or research input needs a new output database; saved evidence is preserved. Coverage outputs distinguish all canonical archive cards from the subset that can be safely joined to the existing research result history.

The retained archive contains 793 event records and 8,950 distinct official fight URLs. Normalized accepted counts, rejected records, field availability and research join coverage are in the generated coverage outputs. Source capture is retrospective and does not prove historical operating availability.

## Delivered coverage and reports

The verified store contains 793 cards, 8,950 distinct bouts, 4,629 profiles and all 42,162 source round rows. There are 8,818 usable paired-stat bouts (98.53%), with validated durations. The 132 held bouts comprise 107 identity ambiguities, 23 incomplete/conflicting paired-round cases and two unresolved events. A further 358 fighter-bout control-time totals remain unknown; other supported statistics are retained. Reach exists for 2,490 profiles. Missing reach and control time are never replaced with zeros or guesses.

The conservative crosswalk links 1,667 established research fighters; 1,658 have 13,830 usable earlier fighter-bout records. Unresolved research identities remain gaps. A separate SQL audit reproduced all 626 near-target record exclusions across the historical feature replay.

Ready-to-open examples are [UFC 300](../output/all-card-statistics/UFC-300-20240413/card-statistics.html), [UFC 229](../output/all-card-statistics/UFC-229-20181006/card-statistics.html), and [the upcoming UFC 333 roster](../output/all-card-statistics/UFC-333-20261024/card-statistics.html). Each has HTML, CSV and JSON companions. Real debutants correctly have no prior UFC statistics. These descriptive comparisons are separate from the model's shrunk features and winner probabilities.

## Model selection and accuracy

V6 keeps the frozen V5 policy and compares three declared feature families: striking, balanced striking/grappling and balanced statistics over the last five available fights. Shrinkage stabilizes small samples. Every feature is derived from earlier fight records; same-date results and statistics are withheld as a group. A two-day timing embargo guards against inconsistent event dates across sources, including same-event tournaments.

The protocol is saved before inspecting development losses. Development years through December 2, 2023 select one candidate by calibrated log loss. That candidate is evaluated once on the existing 1,447-bout benchmark. V5 remains the fallback unless the candidate improves development and fixed-benchmark log loss, does not reduce fixed-benchmark winner accuracy, and meets the coverage rule. These are reused historical comparisons, not a fresh holdout or a guarantee of future accuracy.

The selected family is `balanced_recent5`. The fixed 1,447-bout benchmark improves from V5 accuracy 59.16% to V6 61.30% (31 more correct), log loss 0.66144121 to 0.65216965, and Brier score 0.23469798 to 0.23048288. The paired event-date loss interval is [-0.01715353, -0.00135635]. Full reach and rich-stat coverage is 532/1,447 (36.77%). Independent formulas exactly reproduced every baseline probability and the saved metrics.

Rolling pooled accuracy is 61.63% versus 59.98%, with loss 0.65359549 versus 0.65951695 over 5,278 bouts. This includes development years and is not independent confirming evidence. In the 398-bout 2026 slice, accuracy remains 57.29% and log loss worsens from 0.67365536 to 0.67830013. The full-period improvement does not establish a gain in the latest year or on future cards.

To repeat the experiment, choose new output paths and run selection before evaluation:

```sh
ufc-statistics protocol --db data/ufc_research_complete_profiles.sqlite \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --statistics-db data/ufc_fight_statistics.sqlite --out reports/NEW-v6-protocol.json
ufc-statistics select --db data/ufc_research_complete_profiles.sqlite \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --statistics-db data/ufc_fight_statistics.sqlite \
  --protocol reports/NEW-v6-protocol.json --out reports/NEW-v6-selection.json
ufc-statistics evaluate --db data/ufc_research_complete_profiles.sqlite \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --statistics-db data/ufc_fight_statistics.sqlite \
  --selection reports/NEW-v6-selection.json --out reports/NEW-v6-evaluation.json
ufc-statistics verify --db data/ufc_research_complete_profiles.sqlite \
  --archive data/raw/ufcstats-archive-20261019/manifest.json \
  --statistics-db data/ufc_fight_statistics.sqlite \
  --selection reports/NEW-v6-selection.json --report reports/NEW-v6-evaluation.json
```

The saved experiment records log loss, accuracy, Brier score, paired uncertainty, date splits, feature definitions, coverage and source hashes. A gain whose interval includes zero remains uncertain, even when the model selection rule accepts it.

## Score another scheduled card

The standard scoring pipeline can opt into the evaluated statistics policy for any scheduled card in an operating database:

```sh
ufc-model --db data/YOUR-OPERATING-CARD.sqlite score-event YOUR-EVENT-ID \
  --model logistic --training-history data/ufc_research_complete_profiles.sqlite \
  --statistics-db data/ufc_fight_statistics.sqlite \
  --statistics-archive data/raw/ufcstats-archive-20261019/manifest.json \
  --statistics-selection reports/research_v6_selection.json \
  --statistics-evaluation reports/research_v6_evaluation.json
```

The shareable prediction report accepts the same store with `ufc-model-report --statistics-db data/ufc_fight_statistics.sqlite --card reports/YOUR-CARD.json --output-dir output/NEW-EDITION`. Optional `--athlete-facts` adds verified current official profile comparisons for that card. Historical model statistics come from the store automatically. `--require-complete-statistics` rejects cards with missing reach/earlier-strike inputs; otherwise missing optional history is disclosed and contributes neutrally.

The existing workflow defaults and operating gates remain explicit. Statistics experiments and forecasts remain research-only. Neither a fuller archive nor retrospective accuracy establishes live wagering performance.
