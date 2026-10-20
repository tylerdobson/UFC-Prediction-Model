# Dashboard history

`dashboard-fight-history.json` is a public, descriptive export for the saved V6
dashboard edition. It contains 47 fighter-fight records and 136 observed rounds
across 12 fighters. A bout involving two roster fighters can appear in each
fighter's history; these counts are not 47 distinct bouts.

Each fighter has up to five available earlier fights, displayed oldest to newest.
The export requires the original card's explicit canonical fighter IDs and the
normalized archive's existing crosswalk. Names alone do not establish identity.
The asset records source hashes, archive commit, capture time, original counts,
validated exposure, opponents, dates and public UFCStats links.

Dates must be strictly before the forecast cutoff date and the event's two-day
embargo. Missing fights are not padded; unknown durations and counts produce
null rates. The two chart series have separate opponents and dates. Their bar
positions are not a shared fight timeline. This export does not reproduce model
features, train a model or change the saved probabilities. Archive capture dates
do not establish source availability before every historical fight.

The reader binds the asset to the saved report's SHA-256 and cutoff, and validates
ordering, round totals, counts and rates before display. The checked-in asset
works without a local database. For a different report, regenerate history from
its matching card and canonical archive; a mismatched asset is unavailable.

With the original retained local inputs, reproduce the export from the project
root without changing a database or the published asset:

```sh
PYTHONPATH=src .venv/bin/python -m ufc_odds_model.dashboard_history \
  --report output/ufc333-20261019-v6-final/prediction-report.json \
  --card reports/ufc333_card_20261019.json \
  --statistics-db data/ufc_fight_statistics.sqlite \
  > /tmp/ufc-dashboard-history.json
```

The local source card and SQLite archive are not required to view the checked-in
dashboard. The export's SHA-256 is
`29d81a83fe67ecdd443b2777527a6cb03a76a2f9b84dc643f1c608d2a46ad67f`.
