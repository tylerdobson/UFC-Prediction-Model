"""Command-line entry points for the UFC modeling workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing

from . import db
from .demo import seed_demo
from .ingest import import_live_odds
from .importers import import_bouts_csv, import_ufcstats_events
from .pipeline import parse_utc, score_event, utc_now
from .pipeline import walk_forward_backtest
from .wagers import record_bet, settle_bet


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UFC moneyline modeling starter")
    parser.add_argument("--db", default=str(db.DEFAULT_DB), help="SQLite database path")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="Create the SQLite schema from versioned SQL")
    subcommands.add_parser("seed-demo", help="Load fictional fights and example odds")
    subcommands.add_parser("list-events", help="Show stored events")
    csv_import = subcommands.add_parser("import-csv", help="Import events, bouts, and results from CSV")
    csv_import.add_argument("path")
    stats_import = subcommands.add_parser("import-ufcstats", help="Try the UFCStats HTML adapter")
    stats_import.add_argument("kind", choices=["completed", "upcoming"])
    stats_import.add_argument("--limit", type=int, default=5)
    stats_import.add_argument("--raw-dir", default="data/raw/ufcstats")

    scoring = subcommands.add_parser("score-event", help="Predict one scheduled event")
    scoring.add_argument("event_id")
    scoring.add_argument("--as-of", help="UTC ISO timestamp; defaults to now")
    scoring.add_argument("--min-ev", type=float, default=0.03,
                         help="Minimum estimated profit per $1 for candidate status")
    scoring.add_argument("--max-quote-age-hours", type=float, default=24.0)
    scoring.add_argument("--report-dir", default="reports")

    backtest = subcommands.add_parser("backtest", help="Walk forward through past events")
    backtest.add_argument("--min-prior-results", type=int, default=0)
    backtest.add_argument("--decision-hours-before-event", type=float, default=24.0)
    backtest.add_argument("--min-ev", type=float, default=0.03)
    backtest.add_argument("--max-quote-age-hours", type=float, default=24.0)

    odds = subcommands.add_parser("import-odds", help="Fetch live MMA prices and match known UFC bouts")
    odds.add_argument("--regions", default="us")
    odds.add_argument("--raw-dir", default="data/raw/odds")

    bet = subcommands.add_parser("record-bet", help="Record an actual manually placed wager")
    bet.add_argument("--prediction-id", type=int, required=True)
    bet.add_argument("--quote-id", type=int, required=True)
    bet.add_argument("--stake", type=float, required=True)
    bet.add_argument("--actual-decimal-odds", type=float, required=True)

    settlement = subcommands.add_parser("settle-bet", help="Record bookmaker settlement")
    settlement.add_argument("--bet-id", type=int, required=True)
    settlement.add_argument("--status", choices=["won", "lost", "push", "void"], required=True)
    settlement.add_argument("--payout", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        with closing(db.connect(args.db)) as connection:
            db.init_db(connection)
            if args.command == "init-db":
                print(f"Initialized {args.db}")
            elif args.command == "seed-demo":
                event_id = seed_demo(connection)
                print(f"Loaded fictional demo data. Next: ufc-model score-event {event_id}")
            elif args.command == "list-events":
                rows = connection.execute(
                    "SELECT event_id, event_date, status, name FROM events ORDER BY event_date"
                ).fetchall()
                for row in rows:
                    print(f"{row['event_date']}  {row['status']:<10}  {row['event_id']:<24}  {row['name']}")
            elif args.command == "import-csv":
                count = import_bouts_csv(connection, args.path)
                print(f"Imported {count} bouts from {args.path}")
            elif args.command == "import-ufcstats":
                result = import_ufcstats_events(connection, args.kind, args.limit, args.raw_dir)
                print(json.dumps(result, indent=2))
            elif args.command == "score-event":
                as_of = parse_utc(args.as_of) if args.as_of else utc_now()
                report, rows = score_event(
                    connection, args.event_id, as_of, args.report_dir,
                    args.min_ev, args.max_quote_age_hours,
                )
                print(f"Wrote {len(rows)} bout predictions to {report}")
                for row in rows:
                    print(
                        f"{row['fighter_a']} vs {row['fighter_b']}: "
                        f"P(A)={row['p_fighter_a']:.1%}; {row['decision']} "
                        f"{row['selection']} {row['decimal_odds']}"
                    )
            elif args.command == "backtest":
                result = walk_forward_backtest(
                    connection, args.min_prior_results,
                    args.decision_hours_before_event, args.min_ev,
                    args.max_quote_age_hours,
                )
                print(json.dumps(result, indent=2))
            elif args.command == "import-odds":
                api_key = os.environ.get("ODDS_API_KEY", "")
                result = import_live_odds(connection, api_key, args.raw_dir, args.regions)
                print(json.dumps(result, indent=2))
            elif args.command == "record-bet":
                bet_id = record_bet(
                    connection, args.prediction_id, args.quote_id,
                    args.stake, args.actual_decimal_odds,
                )
                print(f"Recorded bet {bet_id}")
            elif args.command == "settle-bet":
                settle_bet(connection, args.bet_id, args.status, args.payout)
                print(f"Settled bet {args.bet_id} as {args.status}")
    except (ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
