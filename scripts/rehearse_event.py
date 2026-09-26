"""One isolated, offline, synthetic pre-fight to settlement rehearsal."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.alerts import record_prefight_checks
from ufc_odds_model.audit import audit_database
from ufc_odds_model.dashboard_data import load_dashboard
from ufc_odds_model.importers import import_bouts_csv
from ufc_odds_model.ingest import import_odds_payload
from ufc_odds_model.integrity import verify_evidence
from ufc_odds_model.paper import record_paper_candidates, settle_paper_bets
from ufc_odds_model.pipeline import score_event, utc_now, utc_string


EVENT_ID = "demo-rehearsal-event"
EVENT_NAME = "SYNTHETIC DEMO — one event rehearsal"
FIGHTERS = {
    "demo-ada": "Demo Ada Alpha",
    "demo-blake": "Demo Blake Beta",
    "demo-cora": "Demo Cora Gamma",
    "demo-deni": "Demo Deni Delta",
}
CSV_COLUMNS = (
    "event_id", "event_name", "event_date", "event_status", "start_time_utc",
    "event_provider_status", "bout_id", "fighter_a_id", "fighter_a_name",
    "fighter_b_id", "fighter_b_name", "bout_status", "bout_provider_status",
    "outcome", "winner_fighter_id", "method", "source_observed_at_utc",
    "source_url", "source_revision_id", "license_name", "license_url",
    "reviewed_by", "weight_class",
)


def _card_row(
    event_id: str, event_name: str, start: datetime, bout_id: str,
    fighter_a: str, fighter_b: str, *, completed: bool,
    observed: datetime, winner: str = "",
) -> dict[str, str]:
    status = "completed" if completed else "scheduled"
    return {
        "event_id": event_id,
        "event_name": event_name,
        "event_date": start.date().isoformat(),
        "event_status": status,
        "start_time_utc": utc_string(start),
        "event_provider_status": status,
        "bout_id": bout_id,
        "fighter_a_id": fighter_a,
        "fighter_a_name": FIGHTERS[fighter_a],
        "fighter_b_id": fighter_b,
        "fighter_b_name": FIGHTERS[fighter_b],
        "bout_status": status,
        "bout_provider_status": status,
        "outcome": "win" if completed else "",
        "winner_fighter_id": winner,
        "method": "synthetic decision" if completed else "",
        "source_observed_at_utc": utc_string(observed),
        "source_url": "demo://synthetic-rehearsal/card",
        "source_revision_id": "synthetic-demo-v1",
        "license_name": "synthetic fixture",
        "license_url": "demo://synthetic-rehearsal/fixture",
        "reviewed_by": "synthetic rehearsal script",
        "weight_class": "synthetic",
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _demo_origin(connection) -> None:
    # The manual CSV importer correctly records its source as manual. In this
    # disposable fixture only, mark those same saved rows as demo so every
    # dashboard view carries the existing "Demo data only" notice.
    connection.execute("UPDATE fighters SET source = 'demo', source_fighter_id = fighter_id")
    connection.execute("UPDATE events SET source = 'demo', source_event_id = event_id")
    connection.execute("UPDATE bouts SET source = 'demo', source_bout_id = bout_id")
    connection.commit()


def _mock_odds(event_start: datetime, updated: datetime) -> list[dict]:
    pairings = (
        ("demo-rehearsal-1", "demo-ada", "demo-cora"),
        ("demo-rehearsal-2", "demo-blake", "demo-deni"),
    )
    return [{
        "id": f"synthetic-odds-{bout_id}",
        "home_team": FIGHTERS[a],
        "away_team": FIGHTERS[b],
        "commence_time": utc_string(event_start),
        "bookmakers": [{
            "key": "synthetic-demo-book",
            "markets": [{
                "key": "h2h",
                "last_update": utc_string(updated),
                "outcomes": [
                    {"name": FIGHTERS[a], "price": 2.4},
                    {"name": FIGHTERS[b], "price": 1.55},
                ],
            }],
        }],
    } for bout_id, a, b in pairings]


def run_rehearsal(output_dir: Path) -> dict:
    """Run the actual import, audit, score, gate, paper, settlement, and read paths.

    ``output_dir`` must not exist. All files stay below it and no feed is fetched.
    The only simulated clock jump is the fictional post-event result import.
    """
    root = output_dir.expanduser().resolve()
    if root.exists():
        raise ValueError(f"Output directory already exists: {root}")
    root.mkdir(parents=True)
    database = root / "synthetic-demo.sqlite"
    now = utc_now()
    event_start = now + timedelta(days=7)
    history = [
        (35, "demo-ada", "demo-blake", "demo-ada"),
        (28, "demo-cora", "demo-deni", "demo-cora"),
        (21, "demo-ada", "demo-deni", "demo-ada"),
        (14, "demo-cora", "demo-blake", "demo-cora"),
    ]
    history_rows = [
        _card_row(
            f"demo-history-{index}", f"SYNTHETIC DEMO — prior card {index}",
            now - timedelta(days=days_ago), f"demo-history-bout-{index}",
            a, b, completed=True,
            observed=now - timedelta(days=days_ago) + timedelta(hours=3), winner=winner,
        )
        for index, (days_ago, a, b, winner) in enumerate(history, start=1)
    ]
    scheduled_rows = [
        _card_row(EVENT_ID, EVENT_NAME, event_start, "demo-rehearsal-1",
                  "demo-ada", "demo-cora", completed=False,
                  observed=now - timedelta(seconds=10)),
        _card_row(EVENT_ID, EVENT_NAME, event_start, "demo-rehearsal-2",
                  "demo-blake", "demo-deni", completed=False,
                  observed=now - timedelta(seconds=10)),
    ]
    history_csv = root / "synthetic_demo_history.csv"
    scheduled_csv = root / "synthetic_demo_scheduled_card.csv"
    _write_csv(history_csv, history_rows)
    _write_csv(scheduled_csv, scheduled_rows)

    connection = db.connect(database)
    try:
        db.init_db(connection)
        imported_history = import_bouts_csv(connection, history_csv, root / "raw" / "manual")
        imported_card = import_bouts_csv(connection, scheduled_csv, root / "raw" / "manual")
        _demo_origin(connection)
        audit = audit_database(connection, as_of=utc_now())
        if not audit["ok"]:
            raise RuntimeError("Synthetic preflight audit reported errors")

        captured = utc_now()
        odds = import_odds_payload(
            connection, _mock_odds(event_start, captured), utc_string(captured),
            root / "raw" / "odds",
        )
        if odds["matched_quotes"] != 4:
            raise RuntimeError("Synthetic odds failed to match both card bouts")
        score_path, scored = score_event(
            connection, EVENT_ID, utc_now(), root / "reports",
            required_snapshot_at_utc=odds["snapshot_at_utc"],
        )
        checked = record_prefight_checks(
            connection, scored, ingestion_run_id=int(odds["ingestion_run_id"]),
        )
        if len(checked) != 2 or not all(row["alert_eligible"] for row in checked):
            raise RuntimeError("Synthetic card did not clear the unchanged pre-fight gate")
        paper = record_paper_candidates(
            connection, checked, bankroll_units=1000,
            max_fraction_per_bet=0.01, max_fraction_per_event=0.015,
        )
        prefight_database = root / "synthetic-demo-prefight.sqlite"
        snapshot_connection = sqlite3.connect(prefight_database)
        try:
            connection.backup(snapshot_connection)
        finally:
            snapshot_connection.close()
        prefight_database.chmod(0o444)
        prefight_evidence = verify_evidence(prefight_database, project_root=root)
        prefight_integrity_path = root / "reports" / "synthetic_demo_prefight_integrity.json"
        prefight_integrity_path.write_text(
            json.dumps(prefight_evidence, indent=2) + "\n", encoding="utf-8",
        )
        before = load_dashboard(
            prefight_database, integrity_report=prefight_integrity_path,
        )
        if (before["data_origin"] != "demo_only" or len(before["upcoming_events"]) != 1
                or before["paper_ledger"]["summary"]["bets"] != 2
                or not prefight_evidence["ok"]
                or before["integrity"]["status"] != "verified_recently"):
            raise RuntimeError("Read-only demo dashboard did not show the pre-fight decision")
        prefight_snapshot_path = root / "reports" / "synthetic_demo_prefight_dashboard.json"
        prefight_snapshot_path.write_text(
            json.dumps({"synthetic_demo": True, "dashboard": before}, indent=2) + "\n",
            encoding="utf-8",
        )

        # Fictional result receipt is explicitly dated after the fictional card.
        # This time shift is isolated to this synthetic, disposable output DB.
        result_observed = event_start + timedelta(hours=3)
        result_imported = result_observed + timedelta(minutes=1)
        result_rows = [
            _card_row(EVENT_ID, EVENT_NAME, event_start, "demo-rehearsal-1",
                      "demo-ada", "demo-cora", completed=True,
                      observed=result_observed, winner="demo-ada"),
            _card_row(EVENT_ID, EVENT_NAME, event_start, "demo-rehearsal-2",
                      "demo-blake", "demo-deni", completed=True,
                      observed=result_observed, winner="demo-deni"),
        ]
        results_csv = root / "synthetic_demo_results.csv"
        _write_csv(results_csv, result_rows)
        with patch("ufc_odds_model.importers.utc_now", return_value=result_imported):
            imported_results = import_bouts_csv(
                connection, results_csv, root / "raw" / "manual",
            )
        with patch("ufc_odds_model.paper.utc_now", return_value=result_imported):
            settlement = settle_paper_bets(connection, EVENT_ID)
        evidence = verify_evidence(database, project_root=root)
        final_integrity_path = root / "reports" / "synthetic_demo_final_integrity.json"
        final_integrity_path.write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8",
        )
        after = load_dashboard(database, integrity_report=final_integrity_path)
        if (after["data_origin"] != "demo_only" or not after["quality"]["ok"] or not evidence["ok"]
                or after["integrity"]["status"] != "verified_recently"
                or settlement != {"won": 1, "lost": 1, "pending_review": 0, "open": 0}):
            raise RuntimeError("Synthetic settlement or saved evidence failed verification")
        ledger = after["paper_ledger"]["summary"]
        if ledger["stake_units"] > 15 or ledger["by_status"] != {"won": 1, "lost": 1}:
            raise RuntimeError("Synthetic paper ledger exceeded its cap or lost settlement rows")
        report = {
            "synthetic_demo": True,
            "operating_evidence": False,
            "offline": True,
            "event_id": EVENT_ID,
            "event_name": EVENT_NAME,
            "database": str(database),
            "prefight_database": str(prefight_database),
            "score_report": str(score_path),
            "prefight_dashboard_snapshot": str(prefight_snapshot_path),
            "prefight_integrity_report": str(prefight_integrity_path),
            "final_integrity_report": str(final_integrity_path),
            "imported_prior_results": imported_history,
            "imported_scheduled_bouts": imported_card,
            "imported_completed_bouts": imported_results,
            "preflight_audit_ok": audit["ok"],
            "preflight_audit_errors": sum(i["severity"] == "error" for i in audit["issues"]),
            "preflight_audit_warning_codes": sorted({
                i["code"] for i in audit["issues"] if i["severity"] == "warning"
            }),
            "mock_odds_matched_quotes": odds["matched_quotes"],
            "scored_bouts": len(scored),
            "accepted_gate_checks": sum(row["alert_eligible"] for row in checked),
            "paper_stakes": [row["stake_units"] for row in paper],
            "settlement": settlement,
            "dashboard_origin": after["data_origin"],
            "dashboard_integrity_status": after["integrity"]["status"],
            "dashboard_postsettlement_audit_ok": after["quality"]["ok"],
            "dashboard_prefight_event_seen": len(before["upcoming_events"]) == 1,
            "prefight_verified_receipts": prefight_evidence["verified_receipts"],
            "dashboard_paper_summary": ledger,
            "receipt_count": evidence["receipt_count"],
            "verified_receipts": evidence["verified_receipts"],
        }
        (root / "synthetic_demo_report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8",
        )
        return report
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        help="New directory for a retained synthetic demo DB and reports; omitted means a temporary rehearsal",
    )
    args = parser.parse_args()
    if args.output_dir is None:
        with tempfile.TemporaryDirectory(prefix="ufc-synthetic-rehearsal-") as temporary:
            report = run_rehearsal(Path(temporary) / "run")
            report["temporary_files_removed_after_exit"] = True
            report["database"] = "temporary synthetic demo database (deleted after exit)"
            report["prefight_database"] = "temporary read-only synthetic demo snapshot (deleted after exit)"
            report["score_report"] = "temporary synthetic demo score report (deleted after exit)"
            report["prefight_dashboard_snapshot"] = "temporary synthetic demo dashboard snapshot (deleted after exit)"
            report["prefight_integrity_report"] = "temporary synthetic demo integrity report (deleted after exit)"
            report["final_integrity_report"] = "temporary synthetic demo integrity report (deleted after exit)"
            print(json.dumps(report, indent=2))
    else:
        print(json.dumps(run_rehearsal(args.output_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
