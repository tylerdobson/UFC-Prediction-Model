"""The operator commands share one live snapshot and persisted gate path."""

from __future__ import annotations

import io
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.card_history import record_card_snapshots
from ufc_odds_model.alerts import record_prefight_checks
from ufc_odds_model.cli import main
from ufc_odds_model.ingest import import_odds_payload
from ufc_odds_model.pipeline import score_event, utc_now, utc_string
from tests.test_evaluation import add_card_snapshot


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
            db.upsert_fighter(connection, "fa", "Fighter Alpha", "demo", "fa")
            db.upsert_fighter(connection, "fb", "Fighter Bravo", "demo", "fb")
            db.upsert_event(
                connection, "event", "UFC test card",
                (now + timedelta(days=4)).date().isoformat(), "scheduled",
                source="demo", source_event_id="event",
                start_time_utc=self.start,
            )
            db.upsert_bout(connection, "bout", "event", "fa", "fb", "scheduled",
                           source="demo", source_bout_id="bout")
            roster_observed = utc_string(now - timedelta(seconds=10))
            roster_file = self.root / "roster.csv"
            roster_file.write_text("reviewed fixture card", encoding="utf-8")
            roster_receipt_id = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("reviewed_csv", roster_observed, str(roster_file),
                 hashlib.sha256(roster_file.read_bytes()).hexdigest()),
            ).lastrowid
            record_card_snapshots(connection, int(roster_receipt_id), [{
                "event_id": "event", "event_name": "UFC test card",
                "event_date": (now + timedelta(days=4)).date().isoformat(),
                "event_status": "scheduled", "event_provider_status": None,
                "start_time_utc": self.start,
                "source_observed_at_utc": roster_observed,
                "observation_basis": "reviewed_csv", "source_url": "fixture://roster",
                "source_revision_id": "fixture-1", "license_name": "fixture",
                "license_url": "fixture://license", "reviewed_by": "test",
                "bouts": [{
                    "event_id": "event", "bout_id": "bout", "fighter_a_id": "fa",
                    "fighter_a_name": "Fighter Alpha", "fighter_b_id": "fb",
                    "fighter_b_name": "Fighter Bravo", "bout_status": "scheduled",
                    "bout_provider_status": None, "weight_class": None,
                    "outcome": None, "winner_fighter_id": None, "method": None,
                }],
            }])
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

    def _add_qualified_observed_history(self) -> None:
        """Fifteen dated cards with retained before/after observations."""
        with db.connect(self.database) as connection:
            connection.execute("UPDATE events SET source = 'manual' WHERE event_id = 'event'")
            for index in range(15):
                start = utc_now() - timedelta(days=2 * (15 - index))
                event_id = f"observed-{index:02d}"
                db.upsert_event(
                    connection, event_id, event_id, start.date().isoformat(),
                    "completed", source="licensed-fixture-provider",
                    source_event_id=event_id, start_time_utc=utc_string(start),
                )
                for number in range(15):
                    fighter_a = "fa" if number == 0 else f"history-a-{index}-{number}"
                    fighter_b = f"history-b-{index}-{number}"
                    if number != 0:
                        db.upsert_fighter(connection, fighter_a, fighter_a)
                    db.upsert_fighter(connection, fighter_b, fighter_b)
                    bout_id = f"history-bout-{index}-{number}"
                    db.upsert_bout(connection, bout_id, event_id, fighter_a,
                                   fighter_b, "completed")
                    db.upsert_result(connection, bout_id, "win", fighter_a,
                                     utc_string(start + timedelta(hours=3)))
                add_card_snapshot(connection, self.root, event_id,
                                  utc_string(start - timedelta(hours=25)), "scheduled")
                add_card_snapshot(connection, self.root, event_id,
                                  utc_string(start + timedelta(hours=4)), "completed")
            connection.commit()

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

    def test_real_card_without_observed_history_rejects_paper_decision(self) -> None:
        # The other CLI fixtures are explicitly demo-only. A real manual card
        # with the same fresh quote and zero prior results must fail closed.
        with db.connect(self.database) as connection:
            connection.execute("UPDATE events SET source = 'manual' WHERE event_id = 'event'")
            connection.commit()
        result = self._command("paper-trade", "--bankroll-units", "1000")
        self.assertEqual(result["paper_bets_created"], 0)
        self.assertEqual(result["checks"][0]["alert_reason"], "model_not_validated")
        with db.connect(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT gate_decision, gate_reason FROM prefight_gate_checks"
            ).fetchone()[:], ("reject", "model_not_validated"))
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM paper_bets"
            ).fetchone()[0], 0)

    def test_real_card_records_model_gap_even_below_nominal_edge_floor(self) -> None:
        with db.connect(self.database) as connection:
            connection.execute("UPDATE events SET source = 'manual' WHERE event_id = 'event'")
            connection.commit()
        result = self._command("alert-event", "--min-ev", "1.0")
        self.assertEqual(result["alert_candidates"], 0)
        self.assertEqual(result["checks"][0]["decision"], "pass")
        self.assertEqual(result["checks"][0]["alert_reason"], "model_not_validated")

    def test_qualified_observed_history_can_clear_readiness_floor(self) -> None:
        self._add_qualified_observed_history()
        result = self._command("paper-trade", "--bankroll-units", "1000")
        self.assertEqual(result["checks"][0]["alert_reason"], "ok")
        self.assertEqual(result["paper_bets_created"], 1)
        self.assertEqual(result["paper_bets"][0]["stake_units"], 10)

    def test_changed_result_outside_saved_snapshot_blocks_real_candidate(self) -> None:
        self._add_qualified_observed_history()
        with db.connect(self.database) as connection:
            db.upsert_result(connection, "history-bout-0-1", "win",
                             "history-b-0-1", utc_string(utc_now()))
            connection.commit()
        result = self._command("paper-trade", "--bankroll-units", "1000")
        self.assertEqual(result["checks"][0]["alert_reason"], "model_not_validated")
        self.assertEqual(result["paper_bets_created"], 0)

    def test_direct_gate_rejects_probability_not_reproduced_by_elo(self) -> None:
        self._add_qualified_observed_history()
        with db.connect(self.database) as connection:
            odds = import_odds_payload(
                connection, self._payload(), utc_string(utc_now()),
                self.root / "raw",
            )
            _, rows = score_event(
                connection, "event", utc_now(), self.root / "reports",
                required_snapshot_at_utc=odds["snapshot_at_utc"],
            )
            self.assertEqual(rows[0]["decision"], "candidate")
            connection.execute(
                "UPDATE predictions SET p_fighter_a = 0.99 WHERE prediction_id = ?",
                (rows[0]["prediction_id"],),
            )
            forged = dict(rows[0], model_selection_probability=0.99)
            checked = record_prefight_checks(
                connection, [forged], ingestion_run_id=int(odds["ingestion_run_id"]),
            )
            self.assertEqual(checked[0]["alert_reason"], "model_not_validated")
            self.assertFalse(checked[0]["alert_eligible"])

    def test_tampered_retained_source_blocks_fetch_and_alert(self) -> None:
        (self.root / "roster.csv").write_text("source changed after import", encoding="utf-8")
        error_output = io.StringIO()
        with patch.dict(os.environ, {"ODDS_API_KEY": "fixture-key"}), patch(
            "ufc_odds_model.odds_api.fetch_mma_h2h"
        ) as fetch, redirect_stderr(error_output):
            result = main(["--db", str(self.database), "alert-event", "event"])
        self.assertEqual(result, 1)
        self.assertIn("payload_hash_mismatch", error_output.getvalue())
        fetch.assert_not_called()
        with db.connect(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM prefight_gate_checks"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
