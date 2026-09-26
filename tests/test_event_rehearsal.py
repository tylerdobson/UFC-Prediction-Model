"""One full synthetic operator path, including persisted receipts and dashboard reads."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.rehearse_event import run_rehearsal
from ufc_odds_model.dashboard_data import load_dashboard
from ufc_odds_model.pipeline import parse_utc


class EventRehearsalTests(unittest.TestCase):
    def test_one_event_from_reviewed_card_to_verified_paper_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "synthetic-demo"
            with patch("ufc_odds_model.odds_api.fetch_mma_h2h",
                       side_effect=AssertionError("live odds network call")):
                report = run_rehearsal(root)
            self.assertEqual(json.loads((root / "synthetic_demo_report.json").read_text()), report)
            self.assertTrue(report["synthetic_demo"])
            self.assertFalse(report["operating_evidence"])
            self.assertIn("roster_observed_at_or_after_start",
                          report["preflight_audit_warning_codes"])
            self.assertEqual(report["accepted_gate_checks"], 2)
            self.assertEqual(report["paper_stakes"], [10.0, 5.0])
            self.assertEqual(report["settlement"], {
                "won": 1, "lost": 1, "pending_review": 0, "open": 0,
            })
            self.assertEqual(report["verified_receipts"], report["receipt_count"])
            self.assertEqual(report["receipt_count"], 4)
            self.assertEqual(report["prefight_verified_receipts"], 3)
            self.assertEqual(report["dashboard_origin"], "demo_only")
            self.assertEqual(report["dashboard_integrity_status"], "verified_recently")
            self.assertTrue(report["dashboard_prefight_event_seen"])

            prefight_database = root / "synthetic-demo-prefight.sqlite"
            self.assertEqual(prefight_database.stat().st_mode & 0o777, 0o444)
            prefight = load_dashboard(
                prefight_database,
                integrity_report=root / "reports" / "synthetic_demo_prefight_integrity.json",
            )
            self.assertEqual(prefight["data_origin"], "demo_only")
            self.assertEqual(prefight["integrity"]["status"], "verified_recently")
            self.assertEqual(len(prefight["upcoming_events"]), 1)
            self.assertEqual(prefight["paper_ledger"]["summary"]["by_status"], {"open": 2})
            self.assertEqual(prefight["paper_ledger"]["summary"]["open_exposure_units"], 15.0)

            database = root / "synthetic-demo.sqlite"
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM results").fetchone()[0], 6)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM card_event_snapshots"
                ).fetchone()[0], 6)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM prefight_gate_checks "
                    "WHERE gate_decision = 'alert_candidate' AND roster_snapshot_id IS NOT NULL"
                ).fetchone()[0], 2)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM events WHERE source = 'demo' AND name LIKE 'SYNTHETIC DEMO%'"
                ).fetchone()[0], 5)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM odds_quote_receipts qr "
                    "JOIN ingestion_runs r ON r.run_id = qr.ingestion_run_id "
                    "WHERE r.source = 'the-odds-api'"
                ).fetchone()[0], 4)
                event = connection.execute(
                    "SELECT start_time_utc FROM events WHERE event_id = 'demo-rehearsal-event'"
                ).fetchone()
                ledger = connection.execute(
                    "SELECT settlement_status, decision_at_utc, settled_at_utc, "
                    "stake_units, payout_units FROM paper_bets ORDER BY paper_bet_id"
                ).fetchall()
                self.assertEqual({row["settlement_status"] for row in ledger}, {"won", "lost"})
                self.assertAlmostEqual(sum(row["stake_units"] for row in ledger), 15.0)
                self.assertTrue(all(
                    parse_utc(row["decision_at_utc"]) < parse_utc(event["start_time_utc"])
                    < parse_utc(row["settled_at_utc"])
                    for row in ledger
                ))
                as_of = parse_utc(max(row["settled_at_utc"] for row in ledger)) + timedelta(seconds=1)
            finally:
                connection.close()

            before_mtime = database.stat().st_mtime_ns
            dashboard = load_dashboard(database, as_of=as_of)
            self.assertEqual(database.stat().st_mtime_ns, before_mtime)
            self.assertEqual(dashboard["data_origin"], "demo_only")
            self.assertEqual(dashboard["paper_ledger"]["summary"]["by_status"], {
                "won": 1, "lost": 1,
            })
            self.assertEqual(dashboard["paper_ledger"]["summary"]["realized_profit_units"], 9.0)
            with self.assertRaisesRegex(ValueError, "already exists"):
                run_rehearsal(root)


if __name__ == "__main__":
    unittest.main()
