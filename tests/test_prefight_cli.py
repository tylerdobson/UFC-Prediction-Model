"""The operator commands share one live snapshot and persisted gate path."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.cli import main
from ufc_odds_model.pipeline import utc_now, utc_string


class PrefightCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "test.sqlite"
        now = utc_now()
        self.start = utc_string(now + timedelta(days=4))
        self.updated = utc_string(now - timedelta(seconds=2))
        with db.connect(self.database) as connection:
            db.init_db(connection)
            db.upsert_fighter(connection, "fa", "Fighter Alpha")
            db.upsert_fighter(connection, "fb", "Fighter Bravo")
            db.upsert_event(
                connection, "event", "UFC test card",
                (now + timedelta(days=4)).date().isoformat(), "scheduled",
                start_time_utc=self.start,
            )
            db.upsert_bout(connection, "bout", "event", "fa", "fb", "scheduled")
            connection.commit()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _payload(self) -> list[dict]:
        return [{
            "id": "odds-bout", "commence_time": self.start,
            "home_team": "Fighter Alpha", "away_team": "Fighter Bravo",
            "bookmakers": [{"key": "test-book", "markets": [{
                "key": "h2h", "last_update": self.updated,
                "outcomes": [
                    {"name": "Fighter Alpha", "price": 2.3},
                    {"name": "Fighter Bravo", "price": 1.6},
                ],
            }]}],
        }]

    def _command(self, command: str, *options: str, payload: list[dict] | None = None) -> dict:
        output = io.StringIO()
        with patch.dict(os.environ, {"ODDS_API_KEY": "fixture-key"}), patch(
            "ufc_odds_model.odds_api.fetch_mma_h2h",
            return_value=self._payload() if payload is None else payload,
        ), redirect_stdout(output):
            result = main([
                "--db", str(self.database), command, "event",
                "--raw-dir", str(self.root / "raw"),
                "--report-dir", str(self.root / "reports"),
                *options,
            ])
        self.assertEqual(result, 0)
        return json.loads(output.getvalue())

    def test_paper_trade_persists_accepted_gate_and_capped_entry(self) -> None:
        # A tempting older line must not displace a lower-edge price in the
        # fresh import used for the decision.
        with db.connect(self.database) as connection:
            db.add_quote(
                connection, "bout", "test-book", "fa", 3.5,
                utc_string(utc_now() - timedelta(seconds=20)), "old-fixture",
                utc_string(utc_now() - timedelta(seconds=21)),
            )
            connection.commit()
        result = self._command("paper-trade", "--bankroll-units", "1000")
        self.assertEqual(result["paper_bets_created"], 1)
        self.assertEqual(result["checks"][0]["alert_reason"], "ok")
        self.assertEqual(result["paper_bets"][0]["stake_units"], 10)
        self.assertEqual(result["paper_bets"][0]["quoted_decimal_odds"], 2.3)
        with db.connect(self.database) as connection:
            saved = connection.execute(
                """SELECT p.gate_check_id, g.gate_decision, g.ingestion_run_id,
                          r.snapshot_at_utc, r.sha256
                   FROM paper_bets p JOIN prefight_gate_checks g USING (gate_check_id)
                   JOIN ingestion_runs r ON r.run_id = g.ingestion_run_id"""
            ).fetchone()
            self.assertEqual(saved["gate_decision"], "alert_candidate")
            self.assertEqual(saved["snapshot_at_utc"], result["snapshot_at_utc"])
            self.assertEqual(len(saved["sha256"]), 64)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 0)

    def test_large_adverse_price_move_blocks_paper_entry(self) -> None:
        result = self._command(
            "paper-trade", "--bankroll-units", "1000", "--decimal-odds-drift", "0.5"
        )
        self.assertEqual(result["paper_bets_created"], 0)
        self.assertEqual(result["checks"][0]["alert_reason"], "edge_below_floor")
        with db.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT gate_reason FROM prefight_gate_checks"
            ).fetchone()[0], "edge_below_floor")

    def test_empty_fresh_snapshot_does_not_reuse_old_price(self) -> None:
        with db.connect(self.database) as connection:
            db.add_quote(
                connection, "bout", "test-book", "fa", 3.5,
                utc_string(utc_now() - timedelta(seconds=20)), "old-fixture",
                utc_string(utc_now() - timedelta(seconds=21)),
            )
            connection.commit()
        result = self._command("paper-trade", "--bankroll-units", "1000", payload=[])
        self.assertEqual(result["paper_bets_created"], 0)
        self.assertEqual(result["checks"][0]["decision"], "no_quote")
        self.assertEqual(result["checks"][0]["alert_reason"], "not_model_candidate")

    def test_alert_event_also_persists_gate_result(self) -> None:
        result = self._command("alert-event")
        self.assertEqual(result["alert_candidates"], 1)
        with db.connect(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM prefight_gate_checks WHERE gate_decision = 'alert_candidate'"
            ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
