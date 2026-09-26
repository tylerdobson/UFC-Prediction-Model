"""Existing paper ledgers survive the gate migration as identifiable legacy rows."""

from __future__ import annotations

import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from ufc_odds_model import db


class GateMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_legacy_paper_row_and_gates_new_inserts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = db.connect(Path(directory) / "old.sqlite")
            try:
                connection.execute(
                    "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
                )
                folder = files("ufc_odds_model").joinpath("sql/migrations")
                for name in (
                    "001_initial.sql", "002_provider_status.sql",
                    "003_fighter_external_ids.sql", "004_paper_bets.sql",
                ):
                    connection.executescript(folder.joinpath(name).read_text(encoding="utf-8"))
                    connection.execute(
                        "INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')",
                        (name,),
                    )
                db.upsert_fighter(connection, "a", "Fighter A")
                db.upsert_fighter(connection, "b", "Fighter B")
                db.upsert_event(
                    connection, "e", "Old card", "2026-10-01", "scheduled",
                    start_time_utc="2026-10-01T20:00:00Z",
                )
                db.upsert_bout(connection, "b1", "e", "a", "b", "scheduled")
                db.add_quote(
                    connection, "b1", "book", "a", 2.1,
                    "2026-09-26T12:00:00Z", "fixture", "2026-09-26T11:59:59Z",
                )
                prediction_id = db.save_prediction(
                    connection, "b1", "elo", "2026-09-26T12:00:01Z",
                    "2026-09-26T12:00:01Z", 0.6,
                )
                quote_id = connection.execute("SELECT quote_id FROM odds_quotes").fetchone()[0]
                paper_values = (
                    prediction_id, quote_id, "e", "b1", "a", "book",
                    "2026-09-26T12:00:01Z", "2026-09-26T12:00:02Z",
                    "2026-09-26T12:00:00Z", 2.1, 0.6, 0.26,
                    1000, 10, 50, 10,
                )
                insert = """INSERT INTO paper_bets(
                    prediction_id, quote_id, event_id, bout_id, selection_fighter_id,
                    bookmaker, decision_at_utc, recorded_at_utc, quote_captured_at_utc,
                    quoted_decimal_odds, model_probability, expected_profit_per_unit,
                    bankroll_units, per_bet_cap_units, event_cap_units, stake_units)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
                connection.execute(insert, paper_values)
                connection.commit()

                db.init_db(connection)
                legacy = connection.execute(
                    "SELECT gate_check_id, stake_units FROM paper_bets"
                ).fetchone()
                self.assertIsNone(legacy["gate_check_id"])
                self.assertEqual(legacy["stake_units"], 10)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = '005_prefight_gate.sql'"
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = '007_immutable_decision_receipts.sql'"
                ).fetchone()[0], 1)
                with self.assertRaisesRegex(Exception, "immutable"):
                    connection.execute("UPDATE paper_bets SET stake_units = 500")
                connection.execute(
                    "UPDATE paper_bets SET settlement_status = 'lost', "
                    "payout_units = 0, settled_at_utc = '2026-10-02T00:00:00Z'"
                )
                self.assertEqual(connection.execute(
                    "SELECT settlement_status, payout_units FROM paper_bets"
                ).fetchone()["settlement_status"], "lost")
                with self.assertRaisesRegex(Exception, "immutable"):
                    connection.execute("DELETE FROM paper_bets")
                with self.assertRaisesRegex(Exception, "matching accepted pre-fight gate"):
                    connection.execute(insert, paper_values)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
