# UFC Prediction Model

A Python and SQLite starter for estimating UFC fight-winner probabilities, comparing them with timestamped bookmaker prices, and recording bets you place manually. The first model is a simple Elo baseline. **Its demo data and demo betting candidates are fictional and have no betting value.**

## Where everything lives

```text
UFC-Prediction-Model/
├── src/ufc_odds_model/
│   ├── sql/migrations/001_initial.sql  # versioned database schema
│   ├── db.py                            # SQLite reads and writes
│   ├── ufcstats.py                      # UFCStats HTML parser
│   ├── odds_api.py                      # The Odds API client
│   ├── importers.py                     # CSV and UFCStats imports
│   ├── ingest.py                        # match MMA prices to known UFC bouts
│   ├── elo.py                           # probability model
│   ├── pipeline.py                      # event reports and walk-forward backtest
│   ├── wagers.py                        # actual-bet ledger
│   └── cli.py                           # commands below
├── examples/bouts_template.csv          # CSV exchange format
├── tests/
├── data/ufc.sqlite                      # created locally; ignored by Git
├── data/raw/                            # source snapshots; ignored by Git
└── reports/                             # generated event CSVs; ignored by Git
```

SQLite is appropriate for one person's local project, including a serious first version. The SQL schema is committed; the live database, raw data, API key, and reports stay on your machine. If the project later needs several users or concurrent jobs, the repository can move to PostgreSQL without changing the basic table design.

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

`seed-demo` creates fictional historical bouts, a fictional upcoming card, and fictional odds. `score-event` saves a CSV in `reports/` with a win probability for each fighter, the available quote, its break-even probability, estimated profit per dollar, and a `candidate`/`pass`/`no_quote` decision. A candidate is a model output, **not an instruction to place a bet**.

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

One CSV row represents one bout. Historical completed bouts need `outcome` and, for a win, `winner_fighter_id`. Upcoming bouts leave those fields blank and use `scheduled` status. Store the event's confirmed UTC start time when available. The importer separates those rows into `fighters`, `events`, `bouts`, and `results` in SQLite.

There is also a UFCStats parser and an optional importer:

```bash
ufc-model import-ufcstats completed --limit 5
ufc-model import-ufcstats upcoming --limit 2
```

UFCStats currently serves a JavaScript browser check to direct Python requests on the development machine, so unattended fetching may fail. The parser has offline fixture tests; the CLI reports a source error instead of treating a blocked response as an empty card. The [UFCStats event pages](http://ufcstats.com/statistics/events/completed?page=all) and [UFC events page](https://www.ufc.com/events) are useful sources to review while preparing the CSV.

After upcoming UFC bouts are in the database, get a key from [The Odds API](https://the-odds-api.com/sports/mma-ufc-odds.html) and run:

```bash
export ODDS_API_KEY="your-key"
ufc-model import-odds
ufc-model score-event YOUR_EVENT_ID
```

The odds feed includes MMA outside the UFC. The importer only stores a quote when both fighter names and the date match exactly one scheduled bout in your UFC database. Unmatched quotes stay in the saved raw response for review. The key comes from `ODDS_API_KEY`; `.env.example` shows its name but the application does not read `.env` automatically.

## How the modeling and betting pieces work

The model forecasts `P(fighter A wins)` from earlier event results. The other fighter's probability is `1 - P(A wins)`. Every report records a UTC cutoff and model version. The event scoring code excludes results from the cutoff date and later, so a historical prediction cannot learn a future fight result.

For a two-outcome wager at decimal odds `d`, the simple break-even probability is `1 / d`, and estimated profit per dollar is `p × d - 1`. Quotes older than the configured freshness limit are ignored. The report uses the latest available quote for each bookmaker and fighter, then selects the highest estimated edge for each bout. This calculation assumes the wager either wins or loses; draws, no contests, substitutions, and voids must follow the actual bookmaker's rules.

`ufc-model backtest` predicts completed events from earlier events. It reports accuracy, Brier score, and log loss. It also simulates flat $1 bets **only** where an event has a known start time and a historical quote captured before the chosen decision cutoff (default: 24 hours before the event). If there are no qualifying historical quotes, betting ROI is `null`; it is never fabricated from current prices. Historical MMA odds are a paid feature of [The Odds API](https://the-odds-api.com/historical-odds-data/).

If you place a bet yourself, use the IDs in the report to record the **actual** stake and accepted price:

```bash
ufc-model record-bet --prediction-id 1 --quote-id 1 --stake 5 --actual-decimal-odds 1.80
ufc-model settle-bet --bet-id 1 --status won --payout 9.00
```

The ledger records the actual return and keeps it separate from hypothetical backtest bets. There is no automatic bet placement.

## Next steps for a trustworthy model

1. Import a sufficiently large, checked history of UFC bouts and stable fighter IDs. Review cancellations, opponent substitutions, draws, and no contests.
2. Obtain historical bookmaker quotes at a consistent time before each fight. Without historical prices, collect live quotes and paper-trade while the dataset grows.
3. Add pre-fight features such as age, reach, rest days, recent performance, and opponent-adjusted fight stats. Rebuild every historical feature using only earlier fights; current career averages can leak future results.
4. Compare Elo with a logistic regression model and a bookmaker-implied-probability baseline. Calibrate probabilities on past validation events, then evaluate on later untouched events.
5. Review calibration, price availability, realistic settlement, return, and uncertainty before treating any apparent edge as actionable. Keep stake limits and an auditable bet ledger.

Run offline tests with `python -m unittest discover -s tests -v`.
