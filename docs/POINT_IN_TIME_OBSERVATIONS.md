# Dated fighter observations

The model can use age, reach, recent form, and a paired fight-stat feature only when their inputs are reconstructable at the historical decision time. The import path accepts **reviewed, lawfully usable CSV snapshots** with stable IDs already present in the operating database. Neither the repository nor the importer supplies a rights-cleared live feed. Source timestamps and evidence URIs require independent human verification; writing an old timestamp in a new CSV does not prove that the values were public then.

## Import format

Start with [`profile_observations_template.csv`](../examples/profile_observations_template.csv) and [`fight_stat_observations_template.csv`](../examples/fight_stat_observations_template.csv). Every row needs `observed_at_utc`, a full timezone-aware source publication/snapshot time to whole-second precision, plus `source_evidence_uri` pointing to the dated source record or preserved archive. The `--license-uri` argument records where permitted use is documented. Keep the source permission record with the private project. These are provenance fields, not automatic rights checks.

```bash
ufc-model --db data/ufc.sqlite import-profile-observations \
  path/to/reviewed_profiles.csv --source YOUR_REVIEWED_SOURCE \
  --license-uri https://provider.example/permission

ufc-model --db data/ufc.sqlite import-fight-stat-observations \
  path/to/reviewed_fight_stats.csv --source YOUR_REVIEWED_SOURCE \
  --license-uri https://provider.example/permission
```

The profile CSV has `fighter_id,observed_at_utc,source_evidence_uri,birth_date,reach_cm`. At least one of birth date or reach is required; blanks never erase older known fields. Reach is centimeters. A later source correction requires a **later observation timestamp**. The fight-stat CSV has `bout_id,fighter_id,observed_at_utc,source_evidence_uri,sig_strikes_landed,sig_strikes_attempted`; include one row for each participant. The importer checks that each ID belongs to a completed result and that a stat source timestamp follows the known card start, or falls on a later UTC date if start is unknown. It rejects negative counts, landed counts above attempts, unknown IDs, conflicting rows for the same source/time, and future observation timestamps. It does not infer identities by name.

Each imported CSV is retained byte-for-byte at a content-addressed path under ignored `data/raw/`, with SHA-256, source, import time, and immutable observation rows in SQLite. A repeat import of identical rows adds a receipt but no duplicate observations. The default raw paths can be changed with `--raw-dir`. Keep these files with database backups; the `source_evidence_uri` may point to material you must retain separately under its license.

## Feature and cutoff policy

For a historical evaluation bout, the decision time is the card's known UTC start minus the configured lead time. For an upcoming card, it is the actual scoring timestamp. Profile and stat observations with `observed_at_utc >= decision_time` are excluded. Completed results and stats from events on the decision's UTC date are excluded even if a result appears earlier that day, because individual bout starts and publish delays are usually unknown. This conservative same-day rule also applies to `training_rows`. Current profile values and current career totals never fill historical gaps.

The logistic feature vector retains Elo, prior bout count, smoothed overall win rate, and rest; adds the last five completed win/draw/loss points as recent form; computes age from a source-dated birth date and reach from a source-dated measurement; and adds an opponent-adjusted significant-strike feature. For each paired observed fight, a fighter's significant-strike accuracy is compared with that **opponent's defensive strike rate from earlier event dates**, using one landed and one attempted pseudo-count to stabilize a short record. The fighter's residuals are averaged with two neutral pseudo-bouts. A stat fight needs both participants' attempts above zero. Same-day fights share the pre-date defensive baseline, independent of their arbitrary row order. Age and reach difference values are zero unless both sides are known; separate availability differences and the coverage record expose missing inputs. Stat residuals are centered at zero and shrink to zero when absent.

The saved `predictions.feature_coverage_json` records six booleans: `fighter_a_age`, `fighter_b_age`, `fighter_a_reach`, `fighter_b_reach`, `fighter_a_adjusted_stats`, and `fighter_b_adjusted_stats`. Elo predictions have null observation coverage because Elo does not use these features. `evaluate` reports fighter-instance coverage and bouts with both sides known for train, validation, and test periods. Logistic model versions hash the exact feature names, labelled rows, coverage, target-card feature rows, fitted weights, and calibration scale; a newly available historical or target observation changes the version when it changes the evidence.

These features do not establish predictive value on their own. Compare the v2 logistic model on later untouched events with Elo and the bookmaker baseline, including the same priced subset, before considering a model promotion. Low coverage or source uncertainty should be reported as uncertainty, not treated as a model edge.
