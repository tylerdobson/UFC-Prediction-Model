# UFC Prediction Model

A Python and SQLite project for importing UFC cards and timestamped bookmaker prices, evaluating fight-winner probabilities, paper-trading under stake caps, and recording bets you place manually. It includes Elo and a point-in-time logistic model. **Its demo data and demo betting candidates are fictional and have no betting value.**

## Where everything lives

```text
UFC-Prediction-Model/
├── app.py                               # read-only Streamlit dashboard
├── dashboard.css                        # dashboard layout and theme
├── src/ufc_odds_model/
│   ├── sql/migrations/                # versioned database schema
│   ├── db.py                            # SQLite reads and writes
│   ├── ufcstats.py                      # UFCStats HTML parser
│   ├── wikipedia_catalog.py             # 15-year event discovery with review rows
│   ├── wikipedia_embedded.py            # checked cards inside year/season pages
│   ├── wikipedia_history.py             # research-only completed-result importer
│   ├── odds_api.py                      # The Odds API client
│   ├── sportradar.py                    # UFC card/results API adapter
│   ├── importers.py                     # CSV and UFCStats imports
│   ├── raw_snapshots.py                 # checked content-addressed source copies
│   ├── card_history.py                  # dated event and matchup observations
│   ├── observations.py                  # dated fighter profiles and fight statistics
│   ├── integrity.py                     # read-only database and payload verification
│   ├── backup.py                        # SQLite backup, verify, and restore drill
│   ├── evidence_bundle.py               # portable database, payload, and artifact recovery
│   ├── jobs.py                          # durable CLI success/failure status
│   ├── ingest.py                        # match MMA prices to verified UFC bouts
│   ├── features.py                      # earlier-fight-only feature replay
│   ├── elo.py, logistic.py              # probability models
│   ├── evaluation.py                    # chronological holdout comparison
│   ├── research_evaluation.py           # retrospective, research-only model comparison
│   ├── live_logistic.py                 # calibrated upcoming-card model
│   ├── audit.py                         # identities, results, and quote coverage
│   ├── alerts.py                        # local pre-fight freshness gate
│   ├── dashboard_data.py                # read-only evidence view over SQLite
│   ├── pipeline.py                      # event reports and walk-forward backtest
│   ├── paper.py, wagers.py              # separate paper/actual ledgers
│   └── cli.py                           # commands below
├── examples/bouts_template.csv          # CSV exchange format
├── docs/WEB_APP_PLAN.md                  # dashboard build plan
├── docs/DASHBOARD_RUNBOOK.md             # dashboard launch and screen guide
├── docs/DATA_SOURCE_DECISION.md          # provider rights, cost, and quota review
├── docs/WIKIPEDIA_HISTORY_SOURCE.md      # research import limits and identity review
├── docs/RESEARCH_DATASET_CARD.md          # 1993–2026 import counts, exclusions, and limits
├── docs/RESEARCH_EVALUATION.md           # accepted-result holdout method and uncertainty
├── docs/RESULTS_SPOTCHECK.md             # bounded checks against UFC-published results
├── docs/LOCAL_RUNBOOK.md                 # operator setup and event-day commands
├── docs/ONE_EVENT_PILOT.md                # real UFC 332 source/odds intake and holds
├── docs/EVIDENCE_BUNDLE.md               # portable evidence archive and recovery drill
├── docs/BUILD_ROADMAP.md                 # release milestones and acceptance gates
├── docs/LATENCY_POLICY.md                # pre-fight alert safeguards
├── docs/UFC_SYSTEM_ARCHITECTURE.drawio    # editable end-to-end workflow
├── docs/UFC_SYSTEM_ARCHITECTURE.png       # full-size workflow preview
├── tests/
├── data/ufc.sqlite                      # created locally; ignored by Git
├── data/raw/                            # source snapshots; ignored by Git
└── reports/                             # generated event CSVs; ignored by Git
```

SQLite is appropriate for one person's local project, including a serious first version. The SQL schema is committed; the live database, raw data, API key, and reports stay on your machine. If the project later needs several users or concurrent jobs, the repository can move to PostgreSQL without changing the basic table design.

The [system architecture diagram](docs/UFC_SYSTEM_ARCHITECTURE.drawio) opens in diagrams.net; a [full-size PNG preview](docs/UFC_SYSTEM_ARCHITECTURE.png) is also available. The [build roadmap](docs/BUILD_ROADMAP.md) defines design constraints and milestone gates. The [source decision record](docs/DATA_SOURCE_DECISION.md) covers free account limits and historical-price availability. The [research dataset card](docs/RESEARCH_DATASET_CARD.md) records the checked **1993–2026** import: **790 completed events, 7,258 bouts/results, and 1,878 fighters**. The [retrospective comparison](docs/RESEARCH_EVALUATION.md) reports a separate, research-only Elo and logistic holdout, and the [result spot check](docs/RESULTS_SPOTCHECK.md) compares a bounded sample with UFC-published outcomes. The [historical revision pilot](docs/HISTORICAL_REVISION_PILOT.md) checks 23 source-dated 2026 cards against that research history while preserving retrospective fetch times. The [public dataset survey](docs/PUBLIC_DATASET_SURVEY.md) compares Kaggle and GitHub leads; the [Wikidata review worksheet](docs/WIKIDATA_IDENTITY_REVIEW.md) lists candidates for currently unresolved names. The [local runbook](docs/LOCAL_RUNBOOK.md) has the operating sequence; the [one-event rehearsal](docs/EVENT_REHEARSAL.md) exercises it offline with fictional data. The [UFC 332 intake pilot](docs/ONE_EVENT_PILOT.md) documents independently timestamped source, identity, and live odds captures with review holds and research-only forecasts. The [container deployment runbook](docs/CONTAINER_DEPLOYMENT.md) packages a verified, read-only dashboard snapshot; the [authenticated hosting plan](docs/HOSTED_DEPLOYMENT.md) prepares a private `app.tylerjamesdobson.com` deployment without changing DNS yet.

An existing database never upgrades as a side effect of another CLI command. Use `ufc-model --db data/ufc.sqlite migrate --backup backups/UNIQUE.sqlite` for a schema upgrade; it verifies the pre-migration backup first. For recovery across machines or paths, use the [portable evidence bundle](docs/EVIDENCE_BUNDLE.md), which includes receipt payloads, model files, and reports alongside SQLite.

## Run the starter

Use Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ufc-model init-db
ufc-model seed-demo
ufc-model list-events
ufc-model backtest
ufc-model score-event demo-upcoming
```

CI installs from the committed `uv.lock` so runtime dependencies and artifact
hashes are fixed. To use the same resolved environment locally, install
`uv==0.12.9` and run `uv sync --frozen --extra dashboard`; prefix commands with
`uv run --frozen --extra dashboard`. The plain `pip` setup above remains a
simple way to start a student project.

`seed-demo` creates fictional historical bouts, a fictional upcoming card, and fictional odds. `score-event` defaults to Elo and saves a CSV in `reports/` with a win probability for each fighter, the available quote, its break-even probability, estimated profit per dollar, and a `candidate`/`pass`/`no_quote` decision. A candidate is a model output, **not an instruction to place a bet**.

To view saved evidence in the local web app, install its optional dependency and launch it from the repository root:

```bash
python -m pip install -e '.[dashboard]'
python -m streamlit run app.py --server.address 127.0.0.1
```

The dashboard reads SQLite and optional saved evaluation/integrity reports. Its Historical data tab shows imported research counts by year. It labels fictional, research-only, missing, stale, rejected, and superseded evidence and shows recent CLI job status. It cannot import data, retrain a model, send an alert, or place a wager. See the [dashboard runbook](docs/DASHBOARD_RUNBOOK.md) for the five views and optional paths.

You can inspect the real SQL database directly:

```bash
sqlite3 data/ufc.sqlite ".tables"
sqlite3 -header -column data/ufc.sqlite \
  "SELECT event_date, name, status FROM events ORDER BY event_date DESC LIMIT 10;"
```

## Load real data

The reliable local path is the CSV importer. Copy [the template](examples/bouts_template.csv), replace the fictional rows, keep stable IDs for each event, fighter, and bout, then run:

```bash
ufc-model import-csv path/to/your_bouts.csv
```

One CSV row represents one bout. Historical completed bouts need `outcome` and, for a win, `winner_fighter_id`. Upcoming bouts leave those fields blank and use `scheduled` status. Store the event's confirmed UTC start time when available. The importer separates those rows into `fighters`, `events`, `bouts`, and `results` in SQLite. It also keeps a content-addressed copy under ignored `data/raw/manual/`, a SHA-256 ingestion receipt, and an immutable card snapshot; use `--raw-dir` to change the local snapshot directory. Include a reviewed `source_observed_at_utc` and attribution for prospective cards. A missing observation time remains unknown and cannot authorize an alert. See [card roster history](docs/CARD_ROSTER_HISTORY.md).

Before relying on an imported source, check that SQLite and every retained payload still match their receipts. This read-only command exits with status 1 for missing or changed evidence:

```bash
python -m ufc_odds_model.integrity --db data/ufc.sqlite --output reports/integrity.json
```

There is also a UFCStats parser and an optional importer:

```bash
ufc-model import-ufcstats completed --limit 5
ufc-model import-ufcstats upcoming --limit 2
```

UFCStats currently serves a JavaScript browser check to direct Python requests on the development machine, so unattended fetching may fail. The parser has offline fixture tests; the CLI reports a source error instead of treating a blocked response as an empty card. Do not treat UFC or UFCStats pages as permission to build the operational database; [UFC's site terms](https://www.ufc.com/news/terms-use) restrict scraping and database construction. Use a source whose rights have been checked for this project.

For a **research-only** historical experiment, the [Wikipedia results importer](docs/WIKIPEDIA_HISTORY_SOURCE.md) can read event catalogs and completed results from 1993 onward into a separate database with page revision, license, and payload-hash receipts. This example shows the original 2011–2025 pass; the [dataset card](docs/RESEARCH_DATASET_CARD.md) gives the full 1993–2026 rebuild:

```bash
ufc-model --db data/ufc_research_2011_2025.sqlite init-db
ufc-model --db data/ufc_research_2011_2025.sqlite import-wikipedia-years \
  --first-year 2011 --last-year 2025 \
  --review-out reports/wikipedia_2011_2025_review.json
ufc-model --db data/ufc_research_2011_2025.sqlite import-wikipedia-embedded \
  --first-year 2011 --last-year 2025 \
  --review-out reports/wikipedia_2011_2025_embedded_review.json
```

Review the catalog exceptions, skipped event pages, unresolved fighter identities, and source results before interpreting the sample. These retrospective pages lack exact pre-fight times, dated roster changes, and historical bookmaker prices. The importer cannot write to the default operating database, and a database containing its research events cannot run `alert-event` or `paper-trade`. The separate [research-only comparison](docs/RESEARCH_EVALUATION.md) calibrates a retrospective model on later accepted results; it does not unlock the operating `evaluate` command or a betting decision workflow.

After upcoming UFC bouts are in the database, get a key from [The Odds API](https://the-odds-api.com/sports/mma-ufc-odds.html). Put `ODDS_API_KEY=...` in a private `.env` file in the project root (`chmod 600 .env`), or export it in the shell, then run:

```bash
ufc-model import-odds
ufc-model score-event YOUR_EVENT_ID
```

The odds feed includes MMA outside the UFC. The importer only stores a quote when both fighter names and the date match exactly one known UFC bout in your database. Unmatched quotes stay in the saved raw response for review. The CLI reads the private `.env` file automatically from its current working directory; an exported variable takes precedence. See the [local runbook](docs/LOCAL_RUNBOOK.md#configure-real-sources).

For a **permitted internal evaluation** of an authenticated UFC card/results source, the optional [Sportradar MMA Daily Summaries](https://developer.sportradar.com/mma/reference/mma-daily-summaries) adapter can import a UTC day:

```bash
ufc-model import-sportradar 2026-10-10
ufc-model list-events
ufc-model audit
```

The adapter stores the raw JSON, stable source IDs, results, and provider status. It accepts only rows explicitly tagged as UFC. Live, delayed, postponed, and incomplete fights cannot be scored. A single daily response may contain only part of a card. Access requires a Sportradar MMA key; no real live call has been made in this repository without one. [Sportradar's current terms](https://developer.sportradar.com/sportradar-updates/page/terms-and-conditions) limit its free trial to internal evaluation and require express written approval for betting-related use. Do not use trial output as the operating data source for this decision app without that approval and appropriate rights. The [source decision record](docs/DATA_SOURCE_DECISION.md) explains the current source path.

If an existing UFCStats or CSV fighter is the same person as a Sportradar fighter, link the IDs **before importing the second provider**. Names alone never merge records:

```bash
ufc-model link-fighter --source sportradar \
  --source-id sr:competitor:123456 --fighter-id ufcstats:YOUR_FIGHTER_ID
```

To backtest prices at a fixed time, The Odds API's [historical endpoint](https://the-odds-api.com/liveapi/guides/v4/) requires historical-plan access. Import a snapshot after its completed UFC bouts are in SQLite:

```bash
ufc-model import-historical-odds --as-of 2025-10-04T23:00:00Z
ufc-model audit --decision-hours-before-event 24
ufc-model evaluate --decision-hours-before-event 24 --output reports/evaluation.json
```

The importer records the provider's snapshot timestamp, which may be earlier than the requested time. It rejects odds updated after that snapshot, and it leaves ambiguous fighter/date matches unmatched. The audit reports how many bouts actually have timely prices on **both sides from one bookmaker**.

## How the modeling and betting pieces work

The model forecasts `P(fighter A wins)` from earlier event results. The other fighter's probability is `1 - P(A wins)`. Every report records a UTC cutoff and model version. The historical evaluation requires a source-dated pre-decision card snapshot for each target bout, and only replays prior results supported by source-dated completed snapshots available before the cutoff. It reports how many targets were excluded for missing or changed roster evidence. An event date or local import timestamp alone cannot prove what was known before a past decision.

For a two-outcome wager at decimal odds `d`, the simple break-even probability is `1 / d`, and estimated profit per dollar is `p × d - 1`. Quotes older than the configured freshness limit are ignored. The report uses the latest available quote for each bookmaker and fighter, then selects the highest estimated edge for each bout. This calculation assumes the wager either wins or loses; draws, no contests, substitutions, and voids must follow the actual bookmaker's rules.

`ufc-model backtest` is a **research-only legacy replay** using current roster and result tables. It reports exploratory Elo accuracy, Brier score, log loss, and the count of quote-based candidates, including unresolved draws and no contests. It always returns `null` for betting profit and ROI because this path cannot prove the historical roster and quote receipt at each decision. Use `evaluate` for the source-dated model comparison and the prospective paper ledger for auditable decisions. Historical MMA odds are a paid feature of [The Odds API](https://the-odds-api.com/historical-odds-data/).

`ufc-model evaluate` builds pre-fight features, trains regularized logistic regression on early event dates, calibrates on later validation dates when there are at least 30 validation bouts, and reports Elo/logistic metrics on untouched later dates. It requires intact source-dated card and result snapshots; absent or changed historical rosters are excluded and counted. When historical two-sided prices are traced to the same hash-checked API response, bookmaker, capture time, and market update time at the decision cutoff, it adds a no-vig bookmaker baseline with coverage and compares all models on that **same subset**. Legacy quotes without a matching source receipt are excluded. `--output` saves a timestamped JSON snapshot for the dashboard; the command still prints the evaluation to the terminal. Small validation/test samples are labeled. `ufc-model audit` checks identities, cancellations and likely substitutions, results, event times, invalid/stale quotes, and historical/live price coverage. Resolve errors and review warnings before interpreting model returns.

The evaluation reports source-observed prior-result coverage for each chronological split. It returns `insufficient_result_evidence` when any split lacks complete source-observed prior history, leaving its metrics exploratory. The live logistic path requires complete training and calibration history plus its sample-size gates. A manually supplied `score-event --as-of` more than five minutes old is marked an unverified historical replay and cannot produce a `candidate` decision.

For an upcoming card, `ufc-model score-event YOUR_EVENT_ID --model logistic` uses a separately calibrated logistic model once the database has at least 100 earlier binary bouts across 10 event dates, including a 30-bout later calibration period. It fails explicitly when history is too small. Its version hashes the training inputs and parameters, and the fitted weights/calibration are saved under ignored `models/`. The fictional demo intentionally fails this history gate. `paper-trade` also accepts `--model logistic`.

Current model features replay Elo, prior bouts, smoothed win rate, rest, and recent form from earlier bouts. The logistic path also accepts dated age, reach, and opponent-adjusted fight statistics when reviewed observations were available before each decision cutoff. Missing observations remain neutral and their coverage is saved with each prediction; no real profile or stat source is connected yet. See [dated fighter observations](docs/POINT_IN_TIME_OBSERVATIONS.md) for the CSV formats, import commands, and cutoff rules.

If you place a bet yourself, use the IDs in the report to record the **actual** stake and accepted price:

```bash
ufc-model record-bet --prediction-id 1 --quote-id 1 --stake 5 --actual-decimal-odds 1.80
ufc-model settle-bet --bet-id 1 --status won --payout 9.00
```

The ledger records the actual return and keeps it separate from hypothetical backtest bets. There is no automatic bet placement.

For prospective paper trading, configure `ODDS_API_KEY` in `.env` or the shell, then refresh, score, check the pre-fight gate, and record eligible decisions in one command:

```bash
ufc-model paper-trade YOUR_EVENT_ID --bankroll-units 1000 \
  --max-age-seconds 60 --decimal-odds-drift 0.05 --min-ev 0.03
ufc-model settle-paper YOUR_EVENT_ID
```

Paper stakes default to at most 1% of the declared bankroll per accepted candidate and 5% across the event. The command requires a fresh live odds import, a known future card start, and a matching card observation seen in the preceding 24 hours. It verifies both the roster payload and the exact API response containing the scored quote. A new roster import or changed payload invalidates that check before paper recording; an unlinked quote is rejected with `quote_source_unverified`. The gate also records the prediction, quote, timestamps, threshold settings, and rejection reason; only matching accepted checks can create a new paper bet. The bankroll and caps stay fixed for that event. The ledger freezes the decision and settlement separately from actual bets. Binary wins/losses can be settled from results; draws, no contests, cancellations, and missing results remain pending review because bookmaker rules differ. `bankroll-units` is a simulation input, not an account connection.

For a **local pre-fight alert check**, refresh prices and re-score in one step:

```bash
ufc-model alert-event YOUR_EVENT_ID --model elo --max-age-seconds 60 \
  --decimal-odds-drift 0.05 --min-ev 0.03
```

An alert candidate must use the just-imported odds snapshot, a recent bookmaker update, a future known card start, and enough estimated profit after the adverse price change you specify. The command prints local eligibility results; it does not notify anyone or place a wager. A larger modeled edge cannot repair delayed in-play fight metrics. See the [latency policy](docs/LATENCY_POLICY.md).

## Next steps for a trustworthy model

1. Complete the [UFC 332 intake pilot](docs/ONE_EVENT_PILOT.md): recheck the current card, resolve the five held fighter-ID rows and three odds name/alias holds, and document a reviewer for any promoted roster. The September 27 provisional local database contains eight linked bouts and 40 saved quote rows across four of them, but stays `review_pending` with no operating predictions or decisions. Two offline forward replays now use the same receipt-verified `2026-09-27T16:01:50Z` cutoff, one from an exact-cutoff 23-event archive and one from the larger research history captured before the saved quote; neither can authorize alerts.
2. Expand the [historical revision pilot](docs/HISTORICAL_REVISION_PILOT.md) into a sufficiently large, rights-checked **source-dated history** of completed results and pre-fight rosters. Its 23 cards contain 112 paired binary bouts with source-linked fighter page IDs; 175 pre-fight source rows remain held. A research-only replay checks the latest completed revision of every earlier card at each later decision cutoff before constructing features. This work does not enter the operating model gate. The gate requires at least 100 earlier binary bouts across 10 event dates, complete prior-result source evidence, a calibrated chronological evaluation, and minimum 50/30/30 train/validation/test bouts before a real alert can pass. The current UFC 332 one-card operating pilot has zero completed results and remains blocked.
3. On event day, capture a new card observation and fresh two-sided quotes for the reviewed card, then run the alert gate and capped paper ledger if the model and source evidence qualify. Audit quote age, roster changes, rejected decisions, and bookmaker-specific settlement after the event. The September 26 odds snapshot is historical intake evidence, not an event-day price.
4. Grow the operating history and historical price record one event at a time. Re-evaluate the chronological Elo, logistic, and bookmaker baselines, calibration, coverage, and uncertainty before promoting a model. The 1993–2026 Wikipedia dataset, including 1,647 held bout rows, remains research-only until its identity and point-in-time limits are addressed.
5. Add rights-cleared, dated fighter profiles and per-fight statistics when they can be observed before each decision. Review source timestamps and coverage before using age, reach, recent form, or opponent-adjusted features. Keep API keys and ingestion jobs server-side; the [web dashboard](docs/WEB_APP_PLAN.md) remains read-only.

Run offline tests with `python -m unittest discover -s tests -v`.
