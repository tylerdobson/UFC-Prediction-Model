"""Paper decisions must stay within exposure caps and settle conservatively."""

from __future__ import annotations

import math
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.paper import record_paper_candidates, settle_paper_bets
from ufc_odds_model.pipeline import utc_now, utc_string


class PaperLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = utc_now()
        self.cutoff = utc_string(self.now - timedelta(minutes=5))
        self.quote_time = utc_string(self.now - timedelta(hours=1))
        self.event_start = utc_string(self.now + timedelta(days=7))
        self.event_date = (self.now + timedelta(days=7)).date().isoformat()
        self.result_time = utc_string(self.now + timedelta(days=7, hours=3))
        self.paper_clock = patch(
            "ufc_odds_model.paper.utc_now",
            return_value=self.now,
        )
        self.paper_clock.start()
        self.connection = db.connect(Path(self.temp.name) / "paper.sqlite")
        db.init_db(self.connection)
        self.event_id = "event-1"
        db.upsert_event(
            self.connection, self.event_id, "Fixture card", self.event_date, "scheduled",
            start_time_utc=self.event_start,
        )
        self.report_rows = []
        for index in range(6):
            fighter_a = f"fighter-{index}-a"
            fighter_b = f"fighter-{index}-b"
            bout_id = f"bout-{index}"
            db.upsert_fighter(self.connection, fighter_a, fighter_a)
            db.upsert_fighter(self.connection, fighter_b, fighter_b)
            db.upsert_bout(self.connection, bout_id, self.event_id, fighter_a, fighter_b, "scheduled")
            price = 2.30 - index * 0.05
            db.add_quote(
                self.connection, bout_id, "fixture-book", fighter_a, price,
                self.quote_time, "fixture",
            )
            quote_id = self.connection.execute(
                "SELECT quote_id FROM odds_quotes WHERE bout_id = ?", (bout_id,)
            ).fetchone()["quote_id"]
            prediction_id = db.save_prediction(
                self.connection, bout_id, "fixture-v1", self.cutoff,
                self.cutoff, 0.6,
            )
            self.report_rows.append({
                "decision": "candidate", "event_id": self.event_id, "bout_id": bout_id,
                "prediction_id": prediction_id, "quote_id": quote_id,
                "as_of_utc": self.cutoff,
                "quote_captured_at_utc": self.quote_time,
                "decimal_odds": price,
            })
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.paper_clock.stop()
        self.temp.cleanup()

    def test_caps_across_calls_and_idempotent_report(self):
        first = record_paper_candidates(
            self.connection, self.report_rows[:2], 1000, 0.01, 0.035
        )
        self.assertEqual([row["stake_units"] for row in first], [10.0, 10.0])
        second = record_paper_candidates(
            self.connection, self.report_rows, 1000, 0.01, 0.035
        )
        self.assertEqual([row["stake_units"] for row in second], [10.0, 5.0])
        self.assertEqual([row["bout_id"] for row in second], ["bout-2", "bout-3"])
        self.assertEqual(record_paper_candidates(
            self.connection, self.report_rows, 1000, 0.01, 0.035
        ), [])
        total = self.connection.execute(
            "SELECT COUNT(*) AS n, SUM(stake_units) AS total FROM paper_bets"
        ).fetchone()
        self.assertEqual(total["n"], 4)
        self.assertAlmostEqual(total["total"], 35.0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 0)

    def test_binary_settlement_and_ambiguous_results(self):
        created = record_paper_candidates(
            self.connection, self.report_rows[:4], 1000, 0.01, 0.05
        )
        db.upsert_result(self.connection, "bout-0", "win", "fighter-0-a", self.result_time)
        db.upsert_result(self.connection, "bout-1", "win", "fighter-1-b", self.result_time)
        db.upsert_result(self.connection, "bout-2", "draw", None, self.result_time)
        db.upsert_result(self.connection, "bout-3", "no_contest", None, self.result_time)
        db.upsert_event(
            self.connection, self.event_id, "Fixture card", self.event_date, "completed",
            start_time_utc=self.event_start,
        )
        self.connection.commit()
        self.assertEqual(settle_paper_bets(self.connection, self.event_id), {
            "won": 1, "lost": 1, "pending_review": 2, "open": 0,
        })
        ledger = self.connection.execute(
            "SELECT bout_id, settlement_status, payout_units, settled_at_utc, settlement_note "
            "FROM paper_bets ORDER BY bout_id"
        ).fetchall()
        self.assertEqual(ledger[0]["settlement_status"], "won")
        self.assertAlmostEqual(ledger[0]["payout_units"], created[0]["stake_units"] * 2.30)
        self.assertIsNotNone(ledger[0]["settled_at_utc"])
        self.assertEqual(ledger[1]["settlement_status"], "lost")
        self.assertEqual(ledger[1]["payout_units"], 0.0)
        self.assertEqual(ledger[2]["settlement_note"], "draw")
        self.assertEqual(ledger[3]["settlement_note"], "no_contest")
        self.assertTrue(all(row["payout_units"] is None for row in ledger[2:]))
        self.assertTrue(all(row["settled_at_utc"] is None for row in ledger[2:]))
        self.assertEqual(settle_paper_bets(self.connection, self.event_id), {
            "won": 0, "lost": 0, "pending_review": 0, "open": 0,
        })

    def test_cancelled_bout_is_flagged_for_review(self):
        record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        self.connection.execute("UPDATE bouts SET status = 'cancelled' WHERE bout_id = 'bout-0'")
        self.connection.commit()
        self.assertEqual(settle_paper_bets(self.connection, self.event_id)["pending_review"], 1)
        row = self.connection.execute(
            "SELECT settlement_status, settlement_note FROM paper_bets"
        ).fetchone()
        self.assertEqual((row["settlement_status"], row["settlement_note"]),
                         ("pending_review", "cancelled"))

    def test_event_bankroll_cannot_change_between_recording_calls(self):
        record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        with self.assertRaisesRegex(ValueError, "different bankroll"):
            record_paper_candidates(self.connection, self.report_rows[1:2], 2000)
        with self.assertRaisesRegex(ValueError, "stake caps"):
            record_paper_candidates(self.connection, self.report_rows[1:2], 1000,
                                    max_fraction_per_event=0.10)
        self.assertEqual(len(record_paper_candidates(
            self.connection, self.report_rows[1:2], 1000
        )), 1)

    def test_stale_report_and_quote_cannot_be_logged_as_live_paper(self):
        old_cutoff = utc_string(self.now - timedelta(hours=1))
        old_prediction = db.save_prediction(
            self.connection, "bout-0", "fixture-v1", old_cutoff, old_cutoff, 0.6,
        )
        stale_report = dict(self.report_rows[0], prediction_id=old_prediction,
                            as_of_utc=old_cutoff)
        with self.assertRaisesRegex(ValueError, "report is too old"):
            record_paper_candidates(self.connection, [stale_report], 1000)

        stale_quote_time = utc_string(self.now - timedelta(hours=25))
        db.add_quote(
            self.connection, "bout-0", "fixture-book", "fighter-0-a", 2.30,
            stale_quote_time, "fixture",
        )
        stale_quote_id = self.connection.execute(
            "SELECT quote_id FROM odds_quotes WHERE bout_id = ? AND captured_at_utc = ?",
            ("bout-0", stale_quote_time),
        ).fetchone()["quote_id"]
        stale_quote = dict(self.report_rows[0], quote_id=stale_quote_id,
                           quote_captured_at_utc=stale_quote_time)
        with self.assertRaisesRegex(ValueError, "quote is too old"):
            record_paper_candidates(self.connection, [stale_quote], 1000)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)

    def test_invalid_inputs_and_mismatched_quote_are_rejected(self):
        for value in (0, -1, math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(ValueError):
                record_paper_candidates(self.connection, self.report_rows[:1], value)
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000, 1.01)
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000,
                                    max_report_age_minutes=math.nan)
        wrong = dict(self.report_rows[0], decimal_odds=99.0)
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, [wrong], 1000)
        future = dict(self.report_rows[0], as_of_utc=utc_string(self.now - timedelta(hours=2)))
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, [future], 1000)
        with patch(
            "ufc_odds_model.paper.utc_now",
            return_value=self.now - timedelta(minutes=6),
        ), self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        with patch(
            "ufc_odds_model.paper.utc_now",
            return_value=self.now + timedelta(days=7, minutes=1),
        ), self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
