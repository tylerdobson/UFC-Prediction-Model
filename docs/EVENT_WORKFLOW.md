# Event preparation and settlement

Run from the repository with the editable package installed. Use a separate
operating database and new output directories. The research history is never
an operating card. `EVENT` below must be the exact imported event ID.

## Prepare

Create a reviewed event spec using the schema in
`data/raw/ufc332-first-capture-20260927T163956Z/event-spec.json`: exact page ID,
title, event date, conservative UTC start, reviewer and review time. Do not
copy UFC 332's dates to a new event. Capture the source and identity responses:

```sh
python scripts/capture_prefight_card.py --event-spec path/to/reviewed-event-spec.json \
  --output-dir data/raw/EVENT-first-capture-UNIQUE
```

Review every identity, pairing and hold in the generated manifest. Import
only after human review with the existing importer:

```sh
python scripts/import_reviewed_prefight_card.py review-template \
  --manifest data/raw/EVENT-first-capture-UNIQUE/manifest.json \
  --output reports/EVENT-review-UNIQUE.json
# Human review: complete the review JSON, including explicit approvals.
python scripts/import_reviewed_prefight_card.py import-approved \
  --manifest data/raw/EVENT-first-capture-UNIQUE/manifest.json \
  --review reports/EVENT-review-UNIQUE.json --output-dir data/EVENT-operating-UNIQUE
```

The verified manifest preserves source hashes, license, fetch time and start
evidence. Refresh the card before a scoring run, and retain each new capture.
Plan the odds schedule and run each capture at its due UTC time:

```sh
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite odds-schedule EVENT
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite odds-schedule EVENT --capture --slot t7d
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite odds-schedule EVENT --capture --slot t24h
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite odds-schedule EVENT --capture --slot t2h
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite score-event EVENT --model logistic
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite forecast freeze EVENT
```

Captures must be within 60 minutes of their due time (explicit tolerance at
most 120 minutes). The event must not have started, and API quota must be at
least 50 before the request. Missing/low quota fails closed. The closing proxy
is the last verified pre-start snapshot, not necessarily the true closing line.
The default logistic needs sufficient source-dated operating training history.
For labelled forecast research when that history is missing, explicitly use:

```sh
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite score-event EVENT \
  --model logistic --training-history data/ufc_research_complete_profiles.sqlite
```

The same flag is accepted by `alert-event` and `paper-trade`; research-only
training still fails the existing operating model-readiness gate. The default
path and all roster, identity, odds freshness and market-gap checks remain.
An attractive calculated EV alone does not authorize a wager. Local alerts
and paper entries still require fresh odds, a fresh reviewed roster, verified
identities, model readiness and the default 0.10 market-gap limit.

## Settle

After the verified event start, capture the completed page against the exact
pre-fight manifest used for preparation:

```sh
python scripts/capture_completed_event.py \
  --prefight-manifest data/raw/EVENT-first-capture-UNIQUE/manifest.json \
  --output-dir data/raw/EVENT-completed-UNIQUE
ufc-model forecast settle EVENT --completed-manifest data/raw/EVENT-completed-UNIQUE/manifest.json
ufc-model forecast summary
ufc-model --db data/EVENT-operating-UNIQUE/operating-card.sqlite clv
```

The capture is review-pending. Operating result imports require the existing
review workflow; draws, no contests, substitutions and unresolved pairs stay
on hold. Ledger settlement never promotes research data to operating evidence.
CLV remains unavailable until an entry and a later verified two-sided snapshot
exist. Actual wager settlement remains a separate manual bookmaker record.

## Recovered UFC 332 research forecast

The shipped ledger already contains these entries. Reproduce in a new ledger:

```sh
ufc-model forecast freeze wikipedia_pilot:83826247 \
  --source reports/ufc332-forward-research-full-20260927T1643Z.json \
  --ledger reports/ufc332-reproduction-UNIQUE.jsonl
ufc-model forecast settle wikipedia_pilot:83826247 \
  --completed-manifest data/raw/ufc332-completed-20261009/manifest.json \
  --ledger reports/ufc332-reproduction-UNIQUE.jsonl
ufc-model forecast summary --ledger reports/ufc332-reproduction-UNIQUE.jsonl
```

This preserves the originally saved v2 probabilities; recovery happened after
the event. Eight forecasts score 5/8, loss 0.64123. The four priced bouts score
model loss 0.71285 versus market 0.54111. No closing snapshot or wagering edge
is claimed from those early prices.
