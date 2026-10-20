# UFC Forecast: data engineering case study

## Problem and deliverable

Fight outcomes, round statistics, profile reach, scheduled cards and current rankings have different identities, capture dates and missing-data rules. A useful prediction report needs repeatable joins and inspectable evidence before it needs more model complexity.

The deliverable is a native MySQL pipeline plus a read-only sports dashboard, official division rankings, verified PDF/CSV exports and a forward-test ledger. [Run locally](../README.md#run-locally) and inspect the [architecture](diagrams/statistics-pipeline.svg).

## One-command demonstration

With the project environment and MySQL configured, run `./scripts/demo.sh`. It creates `output/pipeline-demo-mysql/index.html`, source receipts, coverage/quality reports and an isolated synthetic MySQL namespace. Every fixture is invented. Missing reach, unknown duration and incomplete paired rounds exercise real parser and quality behavior. Replay verifies the saved edition and dataset instead of silently rebuilding it. Choose a new output directory after changing inputs or transformation code.

The demo does not activate the real statistics alias or analytical warehouse views. It does not train a winner model. An explicit legacy fixture mode remains for regression comparison; the normal demonstration requires MySQL and uses no SQLite staging.

## Source and storage design

Pinned upstream CSVs retain URL, Git commit, capture time, byte count and SHA-256. The normalized MySQL datasets preserve canonical events, fighters, bouts, original source rows, paired fighter-bout/round facts, research crosswalks, held identities and field-level quality gaps. Dataset roles separate mutable operating data, immutable research/statistics and synthetic fixtures.

The retained real edition contains 793 cards, 8,950 bouts and 4,629 fighter profiles, with 8,818 usable paired-stat bouts and 132 held bouts. Its normalized facts contain 17,636 fighter-bout rows and 41,514 fighter-round rows. Reach exists for 2,490 profiles; absent measurements remain null.

Native SQL analytical views expose active coverage, history and quality. Versioned document tables retain rankings, evaluation reports and forecast payloads. An append-only ledger links forecasts and settlements by hash. Raw captures and shareable report exports remain files; their hashes bind them to the database evidence. [Full runtime contract](MYSQL_RUNTIME.md).

## Publication and operations

A refresh builds a new native MySQL namespace, validates paired rounds and identities, then applies nonempty and retained-coverage gates. Published immutable data has database write guards. The statistics alias and publication receipt update in one transaction. The analytical warehouse has its own validated publication transaction; replay reconciles failures between the two publication steps. Schema setup occurs separately from data transactions.

Every attempt retains stage timings, source/build fingerprints, counts and outcome. A local launchd schedule invokes refresh at 09:00; failures trigger Mac notifications, with optional explicit webhook configuration. Failed normalization or quality gates preserve the last approved statistics alias. [Scheduling and recovery](PIPELINE_OPERATIONS.md).

Backups retain the selected dataset and global document/forecast evidence. Portable bundles include referenced source artifacts. Restoration targets a new namespace and does not silently replace live pointers. Original SQLite archives remain immutable one-time migration evidence, not runtime databases.

## Analytical product

The dashboard's seven sections expose the upcoming card, searchable historical archive, measured model evidence, quality holds, separate actual/paper ledgers, prospective forecast tracking and current rankings. Official Meta and media rankings are separate, including champions, all 15 published contender rows, ties and source dates for each active division.

Past-fight context includes the opponent, observed result, method, detail, round/time and pre-bout archive Elo/record. Measured style aggregates expose numerator, denominator and sample count. Held rows are visible but excluded from feature calculations. Current rankings and career averages are not substituted for past-only model evidence.

## Model experiment and outcome

V7 predeclares style and finish/opponent feature families, selects on bounded 2016–2023 development folds, and compares against frozen V6. Chronology, whole-date withholding, the target embargo, missingness and source identity remain explicit.

The selected 43-feature candidate produced 61.16% accuracy and 0.65386 log loss on the reused 1,447-bout benchmark. V6 retained 61.30% and 0.65217. The candidate was not promoted. This is a useful negative result: richer data was tested, and the delivered model remains the better measured option. Neither reused benchmark establishes live accuracy or profit.

## Verification and limits

Native integration verifies actual MySQL constraints, dataset immutability, exact legacy-import row fingerprints, snapshot restore, concurrent forecast appends and rollback. A native forecast reproduction matches all six frozen V6 probabilities exactly. The normal demo traps SQLite connection attempts and verifies real aliases are unchanged. Browser checks cover all dashboard sections and source-bound downloads. See the dated [verification record](PIPELINE_VERIFICATION.md).

This remains a single-machine batch system. A local schedule needs an awake, logged-in Mac; source access can fail. Retrospective source capture, historical reach uncertainty, unresolved identities and sparse forward outcomes remain visible limitations. Database architecture improves traceability and operation; model accuracy must be demonstrated separately.
