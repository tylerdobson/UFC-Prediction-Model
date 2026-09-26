"""The live logistic path must remain reproducible and free of future results."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from ufc_odds_model import db
from ufc_odds_model.live_logistic import prepare_live_logistic


AS_OF = datetime(2024, 1, 12, 12, tzinfo=timezone.utc)


class LiveLogisticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.connection)
        db.upsert_fighter(self.connection, "anchor", "Anchor Fighter")
        db.upsert_fighter(self.connection, "novice", "Novice Fighter")
        db.upsert_event(
            self.connection, "target", "Target Card", "2024-01-20", "scheduled",
            start_time_utc="2024-01-20T20:00:00Z",
        )
        db.upsert_bout(self.connection, "target-bout", "target", "anchor", "novice", "scheduled")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def add_card(self, day: int, count: int = 10) -> None:
        date = f"2024-01-{day:02d}"
        event_id = f"history-{day:02d}"
        db.upsert_event(
            self.connection, event_id, event_id, date, "completed",
            start_time_utc=f"{date}T20:00:00Z",
        )
        for number in range(count):
            a_id = "anchor" if number == 0 else f"a-{day:02d}-{number:02d}"
            b_id = f"b-{day:02d}-{number:02d}"
            if number != 0:
                db.upsert_fighter(self.connection, a_id, a_id)
            db.upsert_fighter(self.connection, b_id, b_id)
            bout_id = f"bout-{day:02d}-{number:02d}"
            db.upsert_bout(self.connection, bout_id, event_id, a_id, b_id, "completed")
            winner = a_id if (day + number) % 3 else b_id
            db.upsert_result(self.connection, bout_id, "win", winner, f"{date}T23:00:00Z")
        self.connection.commit()

    def add_eligible_history(self) -> None:
        for day in range(1, 12):
            self.add_card(day)

    def test_live_probabilities_have_auditable_chronological_calibration(self):
        self.add_eligible_history()
        prepared = prepare_live_logistic(self.connection, "target", AS_OF)
        self.assertEqual(prepared.training_bouts, 80)
        self.assertEqual(prepared.calibration_bouts, 30)
        self.assertEqual(prepared.training_last_date, "2024-01-08")
        self.assertEqual(prepared.calibration_first_date, "2024-01-09")
        self.assertEqual(prepared.calibration_last_date, "2024-01-11")
        self.assertEqual(prepared.history_before_date, "2024-01-12")
        self.assertTrue(prepared.model_version.startswith("logistic-prior-v1:"))
        self.assertEqual(len(prepared.model_version.split(":")[1]), 16)
        self.assertEqual(set(prepared.predictions), {"target-bout"})
        self.assertGreater(prepared.predictions["target-bout"], 0)
        self.assertLess(prepared.predictions["target-bout"], 1)

    def test_future_and_same_day_results_cannot_change_model_or_target_features(self):
        self.add_eligible_history()
        original = prepare_live_logistic(self.connection, "target", AS_OF)
        self.assertEqual(original, prepare_live_logistic(self.connection, "target", AS_OF))
        self.add_card(12)
        self.add_card(13)
        after_future_results = prepare_live_logistic(self.connection, "target", AS_OF)
        self.assertEqual(original.model_version, after_future_results.model_version)
        self.assertEqual(original.predictions, after_future_results.predictions)
        self.assertEqual(original.model.weights, after_future_results.model.weights)

    def test_change_to_earlier_result_changes_version(self):
        self.add_eligible_history()
        original = prepare_live_logistic(self.connection, "target", AS_OF)
        db.upsert_result(
            self.connection, "bout-02-00", "win", "b-02-00", "2024-01-02T23:00:00Z"
        )
        self.connection.commit()
        amended = prepare_live_logistic(self.connection, "target", AS_OF)
        self.assertNotEqual(original.model_version, amended.model_version)

    def test_small_sample_never_falls_back_to_uncalibrated_live_model(self):
        for day in range(1, 10):
            self.add_card(day)
        with self.assertRaisesRegex(ValueError, "at least 100 earlier binary bouts"):
            prepare_live_logistic(self.connection, "target", AS_OF)

    def test_completed_event_with_future_known_start_is_rejected(self):
        self.add_eligible_history()
        self.connection.execute(
            "UPDATE events SET start_time_utc = '2024-01-13T20:00:00Z' "
            "WHERE event_id = 'history-05'"
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "event start on or after"):
            prepare_live_logistic(self.connection, "target", AS_OF)

    def test_target_must_be_upcoming_and_as_of_aware(self):
        self.add_eligible_history()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            prepare_live_logistic(self.connection, "target", datetime(2024, 1, 12, 12))
        with self.assertRaisesRegex(ValueError, "already started"):
            prepare_live_logistic(
                self.connection, "target", datetime(2024, 1, 20, 20, tzinfo=timezone.utc)
            )

    def test_provider_live_status_blocks_prefight_predictions(self):
        self.add_eligible_history()
        self.connection.execute(
            "UPDATE bouts SET provider_status = 'live' WHERE bout_id = 'target-bout'"
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "Provider bout status"):
            prepare_live_logistic(self.connection, "target", AS_OF)


if __name__ == "__main__":
    unittest.main()
