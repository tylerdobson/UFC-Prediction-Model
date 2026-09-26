"""Command-line entry points for the UFC modeling workflow."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from . import db
from .alerts import record_prefight_checks
from .integrity import verify_evidence
from .jobs import TRACKED_COMMANDS, finish_job, start_job
from .audit import audit_database
from .demo import seed_demo
from .evaluation import evaluate_models
from .ingest import import_historical_odds, import_live_odds
from .importers import import_bouts_csv, import_ufcstats_events
from .observations import import_fight_stat_observations_csv, import_profile_observations_csv
from .pipeline import parse_utc, score_event, utc_now
from .pipeline import walk_forward_backtest
from .paper import record_paper_candidates, settle_paper_bets
from .wagers import record_bet, settle_bet
from .wikipedia_history import import_wikipedia_history


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UFC moneyline modeling starter")
    parser.add_argument("--db", default=str(db.DEFAULT_DB), help="SQLite database path")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="Create the SQLite schema from versioned SQL")
    subcommands.add_parser("seed-demo", help="Load fictional fights and example odds")
    subcommands.add_parser("list-events", help="Show stored events")
    csv_import = subcommands.add_parser("import-csv", help="Import events, bouts, and results from CSV")
    csv_import.add_argument("path")
    csv_import.add_argument("--raw-dir", default="data/raw/manual",
                            help="Directory for content-addressed copies of reviewed CSV input")
    profiles = subcommands.add_parser(
        "import-profile-observations", help="Import source-dated fighter age/reach observations"
    )
    profiles.add_argument("path")
    profiles.add_argument("--source", required=True, help="Reviewed source name")
    profiles.add_argument("--license-uri", required=True, help="Source permission/license evidence URI")
    profiles.add_argument("--raw-dir", default="data/raw/profile-observations")
    fight_stats = subcommands.add_parser(
        "import-fight-stat-observations", help="Import source-dated per-fight strike observations"
    )
    fight_stats.add_argument("path")
    fight_stats.add_argument("--source", required=True, help="Reviewed source name")
    fight_stats.add_argument("--license-uri", required=True, help="Source permission/license evidence URI")
    fight_stats.add_argument("--raw-dir", default="data/raw/fight-stat-observations")
    stats_import = subcommands.add_parser("import-ufcstats", help="Try the UFCStats HTML adapter")
    stats_import.add_argument("kind", choices=["completed", "upcoming"])
    stats_import.add_argument("--limit", type=int, default=5)
    stats_import.add_argument("--raw-dir", default="data/raw/ufcstats")

    wiki_import = subcommands.add_parser(
        "import-wikipedia-history",
        help="Import reviewed historical UFC 295–304 results into a separate research database",
    )
    wiki_import.add_argument("--first-event", type=int, default=295)
    wiki_import.add_argument("--last-event", type=int, default=304)
    wiki_import.add_argument("--crosswalk", help="Reviewed JSON identities for unlinked fighters")
    wiki_import.add_argument("--review-out", default="reports/wikipedia_identity_review.json")
    wiki_import.add_argument("--raw-dir", default="data/raw/wikipedia")

    scoring = subcommands.add_parser("score-event", help="Predict one scheduled event")
    scoring.add_argument("event_id")
    scoring.add_argument("--as-of", help="UTC ISO timestamp; defaults to now")
    scoring.add_argument("--min-ev", type=float, default=0.03,
                         help="Minimum estimated profit per $1 for candidate status")
    scoring.add_argument("--max-quote-age-hours", type=float, default=24.0)
    scoring.add_argument("--report-dir", default="reports")
    scoring.add_argument("--model", choices=["elo", "logistic"], default="elo")
    scoring.add_argument("--model-dir", default="models")

    backtest = subcommands.add_parser("backtest", help="Walk forward through past events")
    backtest.add_argument("--min-prior-results", type=int, default=0)
    backtest.add_argument("--decision-hours-before-event", type=float, default=24.0)
    backtest.add_argument("--min-ev", type=float, default=0.03)
    backtest.add_argument("--max-quote-age-hours", type=float, default=24.0)

    odds = subcommands.add_parser("import-odds", help="Fetch live MMA prices and match known UFC bouts")
    odds.add_argument("--regions", default="us")
    odds.add_argument("--raw-dir", default="data/raw/odds")

    historical_odds = subcommands.add_parser(
        "import-historical-odds", help="Fetch one paid historical MMA price snapshot"
    )
    historical_odds.add_argument("--as-of", required=True, help="Requested UTC ISO snapshot time")
    historical_odds.add_argument("--regions", default="us")
    historical_odds.add_argument("--raw-dir", default="data/raw/odds")

    daily = subcommands.add_parser("import-sportradar", help="Import one UFC daily summary")
    daily.add_argument("date", help="UTC event date, YYYY-MM-DD")
    daily.add_argument("--access-level", choices=["trial", "production"], default="trial")
    daily.add_argument("--raw-dir", default="data/raw/sportradar")

    fighter_link = subcommands.add_parser(
        "link-fighter", help="Link a reviewed provider fighter ID to an existing canonical ID"
    )
    fighter_link.add_argument("--source", required=True)
    fighter_link.add_argument("--source-id", required=True)
    fighter_link.add_argument("--fighter-id", required=True)

    audit = subcommands.add_parser("audit", help="Check identities, results, quote times, and coverage")
    audit.add_argument("--as-of", help="UTC ISO timestamp; defaults to now")
    audit.add_argument("--decision-hours-before-event", type=float, default=24.0)
    audit.add_argument("--max-quote-age-hours", type=float, default=24.0)

    comparison = subcommands.add_parser(
        "evaluate", help="Compare Elo, logistic, and available historical bookmaker prices"
    )
    comparison.add_argument("--decision-hours-before-event", type=float, default=24.0)
    comparison.add_argument("--max-quote-age-hours", type=float, default=24.0)
    comparison.add_argument(
        "--output", help="Save a timestamped JSON evaluation snapshot for the dashboard"
    )

    paper = subcommands.add_parser("paper-trade", help="Refresh odds, check the pre-fight gate, and log capped paper bets")
    paper.add_argument("event_id")
    paper.add_argument("--bankroll-units", type=float, required=True)
    paper.add_argument("--max-fraction-per-bet", type=float, default=0.01)
    paper.add_argument("--max-fraction-per-event", type=float, default=0.05)
    paper.add_argument("--min-ev", type=float, default=0.03)
    paper.add_argument("--max-age-seconds", type=float, default=60.0)
    paper.add_argument("--decimal-odds-drift", type=float, default=0.05)
    paper.add_argument("--regions", default="us")
    paper.add_argument("--raw-dir", default="data/raw/odds")
    paper.add_argument("--report-dir", default="reports")
    paper.add_argument("--model", choices=["elo", "logistic"], default="elo")
    paper.add_argument("--model-dir", default="models")

    paper_settlement = subcommands.add_parser("settle-paper", help="Settle binary paper bets for an event")
    paper_settlement.add_argument("event_id")

    alerts = subcommands.add_parser(
        "alert-event", help="Refresh live odds and print local pre-fight alert candidates"
    )
    alerts.add_argument("event_id")
    alerts.add_argument("--model", choices=["elo", "logistic"], default="elo")
    alerts.add_argument("--regions", default="us")
    alerts.add_argument("--raw-dir", default="data/raw/odds")
    alerts.add_argument("--report-dir", default="reports")
    alerts.add_argument("--model-dir", default="models")
    alerts.add_argument("--max-age-seconds", type=float, default=60.0)
    alerts.add_argument("--decimal-odds-drift", type=float, default=0.05)
    alerts.add_argument("--min-ev", type=float, default=0.03)

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


def _check_prefight_event(connection, args) -> tuple[object, dict, list[dict]]:
    wikipedia_history = connection.execute(
        "SELECT 1 FROM events WHERE source = 'wikipedia_research' LIMIT 1"
    ).fetchone()
    if wikipedia_history:
        raise ValueError("Research-only Wikipedia history cannot be used for pre-fight alerts or paper bets")
    evidence = verify_evidence(args.db)
    if not evidence["ok"]:
        codes = ", ".join(sorted({issue["code"] for issue in evidence["issues"]}))
        raise ValueError(f"Source integrity check failed before alert scoring: {codes}")
    if (not all(math.isfinite(value) for value in (
        args.max_age_seconds, args.decimal_odds_drift, args.min_ev
    )) or args.max_age_seconds <= 0 or args.decimal_odds_drift < 0 or args.min_ev < 0):
        raise ValueError("Alert freshness and edge settings must be nonnegative, with positive age")
    event = connection.execute(
        "SELECT start_time_utc, status, provider_status FROM events WHERE event_id = ?",
        (args.event_id,),
    ).fetchone()
    if event is None:
        raise ValueError(f"Unknown event: {args.event_id}")
    if event["status"] != "scheduled" or event["provider_status"] not in (
        None, "scheduled", "not_started"
    ):
        raise ValueError("Event is not available for pre-fight alerts")
    if not event["start_time_utc"] or parse_utc(event["start_time_utc"]) <= utc_now():
        raise ValueError("A future known event start is required for pre-fight alerts")
    odds_result = import_live_odds(
        connection, os.environ.get("ODDS_API_KEY", ""),
        args.raw_dir, args.regions,
    )
    report, rows = score_event(
        connection, args.event_id, utc_now(), args.report_dir,
        args.min_ev, args.max_age_seconds / 3600.0,
        model_kind=args.model, model_dir=args.model_dir,
        required_snapshot_at_utc=odds_result["snapshot_at_utc"],
    )
    checked = record_prefight_checks(
        connection, rows, ingestion_run_id=int(odds_result["ingestion_run_id"]),
        max_age_seconds=args.max_age_seconds,
        decimal_odds_drift=args.decimal_odds_drift,
        min_edge=args.min_ev,
    )
    return report, odds_result, checked


def _safe_error_text(error: BaseException) -> str:
    message = str(error)
    for name in ("ODDS_API_KEY", "SPORTRADAR_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    job_run_id: int | None = None
    try:
        if args.command == "import-wikipedia-history" and Path(args.db).resolve() == db.DEFAULT_DB.resolve():
            raise ValueError("Use an explicit separate --db path for Wikipedia research history")
        with closing(db.connect(args.db)) as connection:
            db.init_db(connection)
            if args.command in TRACKED_COMMANDS:
                job_run_id = start_job(
                    connection, args.command, getattr(args, "event_id", None)
                )
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
                count = import_bouts_csv(connection, args.path, args.raw_dir)
                print(f"Imported {count} bouts from {args.path}")
            elif args.command == "import-profile-observations":
                count = import_profile_observations_csv(
                    connection, args.path, args.source, args.license_uri, args.raw_dir,
                )
                print(f"Imported {count} new fighter profile observations from {args.path}")
            elif args.command == "import-fight-stat-observations":
                count = import_fight_stat_observations_csv(
                    connection, args.path, args.source, args.license_uri, args.raw_dir,
                )
                print(f"Imported {count} new per-fight stat observations from {args.path}")
            elif args.command == "import-ufcstats":
                result = import_ufcstats_events(connection, args.kind, args.limit, args.raw_dir)
                print(json.dumps(result, indent=2))
            elif args.command == "import-wikipedia-history":
                result = import_wikipedia_history(
                    connection,
                    args.first_event,
                    args.last_event,
                    raw_dir=args.raw_dir,
                    review_out=args.review_out,
                    crosswalk_path=args.crosswalk,
                )
                print(json.dumps(result, indent=2))
            elif args.command == "score-event":
                as_of = parse_utc(args.as_of) if args.as_of else utc_now()
                report, rows = score_event(
                    connection, args.event_id, as_of, args.report_dir,
                    args.min_ev, args.max_quote_age_hours,
                    model_kind=args.model, model_dir=args.model_dir,
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
            elif args.command == "import-historical-odds":
                api_key = os.environ.get("ODDS_API_KEY", "")
                result = import_historical_odds(connection, api_key, args.as_of, args.raw_dir, args.regions)
                print(json.dumps(result, indent=2))
            elif args.command == "import-sportradar":
                from .sportradar import import_daily_summaries
                api_key = os.environ.get("SPORTRADAR_API_KEY", "")
                result = import_daily_summaries(
                    connection, api_key, args.date, args.raw_dir, args.access_level
                )
                print(json.dumps(result, indent=2))
            elif args.command == "link-fighter":
                db.link_external_fighter(connection, args.source, args.source_id, args.fighter_id)
                connection.commit()
                print(f"Linked {args.source}:{args.source_id} to {args.fighter_id}")
            elif args.command == "audit":
                result = audit_database(
                    connection,
                    as_of=parse_utc(args.as_of) if args.as_of else utc_now(),
                    decision_hours_before_event=args.decision_hours_before_event,
                    max_quote_age_hours=args.max_quote_age_hours,
                )
                print(json.dumps(result, indent=2))
            elif args.command == "evaluate":
                result = evaluate_models(
                    connection, args.decision_hours_before_event,
                    args.max_quote_age_hours,
                )
                if args.output:
                    target = Path(args.output)
                    source_db = Path(args.db)
                    if target.resolve() == source_db.resolve():
                        raise ValueError("Evaluation output must not replace the database")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    payload = {
                        "schema_version": 1,
                        "generated_at_utc": utc_now().isoformat(),
                        "source_db_path": str(source_db.resolve()),
                        "source_db_mtime_ns": source_db.stat().st_mtime_ns if source_db.exists() else None,
                        "parameters": {
                            "decision_hours_before_event": args.decision_hours_before_event,
                            "max_quote_age_hours": args.max_quote_age_hours,
                        },
                        "evaluation": result,
                    }
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=f".{target.name}.", dir=target.parent
                    )
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                            json.dump(payload, output, indent=2)
                            output.write("\n")
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(temporary, target)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                print(json.dumps(result, indent=2))
            elif args.command == "paper-trade":
                report, odds_result, checked = _check_prefight_event(connection, args)
                created = record_paper_candidates(
                    connection, checked, args.bankroll_units,
                    args.max_fraction_per_bet, args.max_fraction_per_event,
                    max_quote_age_hours=args.max_age_seconds / 3600.0,
                )
                print(json.dumps({"report": str(report), "paper_bets_created": len(created),
                                  "snapshot_at_utc": odds_result["snapshot_at_utc"],
                                  "checks": checked, "paper_bets": created}, indent=2))
            elif args.command == "settle-paper":
                result = settle_paper_bets(connection, args.event_id)
                print(json.dumps(result, indent=2))
            elif args.command == "alert-event":
                _, odds_result, checked = _check_prefight_event(connection, args)
                candidates = [row for row in checked if row["alert_eligible"]]
                print(json.dumps({
                    "snapshot_at_utc": odds_result["snapshot_at_utc"],
                    "matched_quotes": odds_result["matched_quotes"],
                    "alert_candidates": len(candidates),
                    "checks": checked,
                }, indent=2))
            elif args.command == "record-bet":
                bet_id = record_bet(
                    connection, args.prediction_id, args.quote_id,
                    args.stake, args.actual_decimal_odds,
                )
                print(f"Recorded bet {bet_id}")
            elif args.command == "settle-bet":
                settle_bet(connection, args.bet_id, args.status, args.payout)
                print(f"Settled bet {args.bet_id} as {args.status}")
            if job_run_id is not None:
                finish_job(connection, job_run_id)
                job_run_id = None
    except Exception as error:
        if job_run_id is not None:
            # The command connection has closed and rolled back any partial
            # writes. A separate write preserves its failure status.
            try:
                with closing(db.connect(args.db)) as status_connection:
                    finish_job(status_connection, job_run_id, error=error)
            except (OSError, sqlite3.Error, ValueError):
                print("Warning: could not save the failed job status", file=sys.stderr)
        if isinstance(error, (ValueError, RuntimeError, OSError, sqlite3.Error)):
            print(f"Error: {_safe_error_text(error)}", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
