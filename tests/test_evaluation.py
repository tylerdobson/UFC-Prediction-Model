"""Holdout evaluation and historical bookmaker coverage checks."""

from __future__ import annotations

import json
import math
import sqlite3
import unittest

from ufc_odds_model import db
from ufc_odds_model.evaluation import _point_in_time_rows, evaluate_models


def make_history(connection: sqlite3.Connection, reverse_bout_order: bool = False) -> None:
    for number in range(1, 11):
        event_id = f"event-{number:02d}"
        bout_id = f"bout-{number:02d}"
        first_id, second_id = f"a-{number:02d}", f"b-{number:02d}"
        db.upsert_fighter(connection, first_id, first_id)
        db.upsert_fighter(connection, second_id, second_id)
        db.upsert_event(
            connection,
            event_id,
            event_id,
            f"2024-01-{number:02d}",
            "completed",
            start_time_utc=f"2024-01-{number:02d}T12:00:00Z",
        )
        a_id, b_id = (second_id, first_id) if reverse_bout_order else (first_id, second_id)
        db.upsert_bout(connection, bout_id, event_id, a_id, b_id, "completed")
        winner = first_id if number % 2 else second_id
        db.upsert_result(connection, bout_id, "win", winner, f"2024-01-{number:02d}T14:00:00Z")
    connection.commit()


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_insufficient_history_is_explicit(self):
        result = evaluate_models(self.connection)
        self.assertEqual(result["status"], "insufficient_history")
        self.assertEqual(result["available"]["bouts"], 0)
        self.assertTrue(result["sample_size_flags"]["fewer_than_three_event_dates"])
        self.assertIsNone(result["test"])
        json.dumps(result)

    def test_split_metrics_and_unavailable_bookmaker_are_serializable(self):
        make_history(self.connection)
        result = evaluate_models(self.connection)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([result["split"][part]["bouts"] for part in ("train", "validation", "test")], [7, 1, 2])
        self.assertEqual(result["split"]["train"]["last_date"], "2024-01-07")
        self.assertEqual(result["split"]["validation"]["first_date"], "2024-01-08")
        self.assertEqual(result["split"]["test"]["first_date"], "2024-01-09")
        self.assertFalse(result["calibration"]["applied"])
        self.assertTrue(result["sample_size_flags"]["train_under_30_bouts"])
        self.assertTrue(result["sample_size_flags"]["validation_under_30_bouts"])
        self.assertTrue(result["sample_size_flags"]["test_under_30_bouts"])
        for name in ("elo", "logistic"):
            self.assertEqual(result["test"][name]["metrics"]["bouts"], 2)
            self.assertEqual(len(result["test"][name]["calibration_bins"]), 5)
        self.assertEqual(result["test"]["bookmaker"]["available_bouts"], 0)
        self.assertEqual(result["test"]["bookmaker"]["coverage"], 0)
        self.assertIsNone(result["test"]["bookmaker"]["metrics"])
        json.dumps(result, allow_nan=False)

    def test_observation_coverage_is_split_specific_and_time_gated(self):
        make_history(self.connection)
        receipt = self.connection.execute(
            """INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256)
               VALUES ('fixture', '2024-01-10T00:00:00Z', 'fixture', 'fixture')"""
        )
        for fighter_id, observed, birth, reach in (
            ("a-09", "2024-01-08T11:00:00Z", "1990-01-01", 180),
            ("b-09", "2024-01-08T11:00:00Z", "1992-01-01", None),
            ("b-10", "2024-01-09T13:00:00Z", "1993-01-01", 175),
        ):
            self.connection.execute(
                """INSERT INTO fighter_profile_observations(
                       fighter_id, source, source_evidence_uri, source_license_uri,
                       observed_at_utc, birth_date, reach_cm, ingestion_run_id
                   ) VALUES (?, 'fixture', 'fixture://snapshot', 'fixture://license', ?, ?, ?, ?)""",
                (fighter_id, observed, birth, reach, receipt.lastrowid),
            )
        self.connection.commit()
        result = evaluate_models(self.connection)
        self.assertEqual(result["observation_coverage"]["train"]["fighter_instances_with_age"], 0)
        test = result["observation_coverage"]["test"]
        self.assertEqual(test["fighter_instances"], 4)
        self.assertEqual(test["fighter_instances_with_age"], 2)
        self.assertEqual(test["age_fighter_coverage"], 0.5)
        self.assertEqual(test["bouts_with_both_ages"], 1)
        self.assertEqual(test["fighter_instances_with_reach"], 1)
        self.assertEqual(test["bouts_with_both_reaches"], 0)

    def test_bookmaker_requires_both_sides_of_one_book_before_cutoff(self):
        make_history(self.connection)
        # Event 9 starts Jan 9 at 12:00, so the default decision is Jan 8 at 12:00.
        for fighter_id, odds in (("a-09", 2.0), ("b-09", 4.0)):
            db.add_quote(self.connection, "bout-09", "valid", fighter_id, odds,
                         "2024-01-08T11:00:00Z", "test", "2024-01-08T10:55:00Z")
        for fighter_id, odds in (("a-09", 9.0), ("b-09", 1.2)):
            db.add_quote(self.connection, "bout-09", "future", fighter_id, odds,
                         "2024-01-08T13:00:00Z", "test")
            db.add_quote(self.connection, "bout-09", "stale", fighter_id, odds,
                         "2024-01-07T10:00:00Z", "test")
            db.add_quote(self.connection, "bout-09", "future-update", fighter_id, odds,
                         "2024-01-08T11:00:00Z", "test", "2024-01-08T12:30:00Z")
        db.add_quote(self.connection, "bout-09", "split-a", "a-09", 2.0,
                     "2024-01-08T11:00:00Z", "test")
        db.add_quote(self.connection, "bout-09", "split-b", "b-09", 2.0,
                     "2024-01-08T11:00:00Z", "test")
        db.add_quote(self.connection, "bout-10", "incomplete", "a-10", 1.2,
                     "2024-01-09T11:00:00Z", "test")
        self.connection.commit()

        result = evaluate_models(self.connection)
        bookmaker = result["test"]["bookmaker"]
        self.assertEqual(bookmaker["available_bouts"], 1)
        self.assertEqual(bookmaker["coverage"], 0.5)
        self.assertEqual(bookmaker["metrics"]["bouts"], 1)
        self.assertAlmostEqual(bookmaker["metrics"]["brier_score"], 1 / 9)
        self.assertAlmostEqual(bookmaker["metrics"]["log_loss"], -math.log(2 / 3))
        self.assertEqual(bookmaker["elo_on_available_bouts"]["bouts"], 1)
        self.assertEqual(bookmaker["logistic_on_available_bouts"]["bouts"], 1)
        self.assertEqual(bookmaker["calibration_bins"][3]["bouts"], 1)

    def test_imported_winner_first_orientation_does_not_change_model_metrics(self):
        make_history(self.connection)
        first = evaluate_models(self.connection)
        other = sqlite3.connect(":memory:")
        other.row_factory = sqlite3.Row
        other.execute("PRAGMA foreign_keys = ON")
        try:
            db.init_db(other)
            make_history(other, reverse_bout_order=True)
            second = evaluate_models(other)
        finally:
            other.close()
        self.assertEqual(first["test"]["elo"], second["test"]["elo"])
        self.assertEqual(first["test"]["logistic"], second["test"]["logistic"])

    def test_missing_event_start_excludes_target_from_all_model_comparisons(self):
        make_history(self.connection)
        self.connection.execute("UPDATE events SET start_time_utc = NULL WHERE event_id = 'event-09'")
        for fighter_id in ("a-09", "b-09"):
            db.add_quote(self.connection, "bout-09", "book", fighter_id, 2.0,
                         "2024-01-08T11:00:00Z", "test")
        self.connection.commit()
        result = evaluate_models(self.connection)
        bookmaker = result["test"]["bookmaker"]
        self.assertEqual(result["excluded_missing_start_time"], {"events": 1, "binary_bouts": 1})
        self.assertEqual(sum(result["split"][part]["bouts"] for part in ("train", "validation", "test")), 9)
        self.assertEqual(result["split"]["test"]["first_date"], "2024-01-08")
        self.assertEqual(bookmaker["available_bouts"], 0)

    def test_prior_day_result_after_decision_is_excluded_from_features(self):
        make_history(self.connection)
        db.upsert_fighter(self.connection, "c-07", "c-07")
        db.upsert_fighter(self.connection, "c-08", "c-08")
        # Jan 7 is safely before event 9's Jan 8 noon decision point.
        db.upsert_bout(self.connection, "extra-07", "event-07", "a-09", "c-07", "completed")
        db.upsert_result(self.connection, "extra-07", "win", "a-09", "2024-01-07T14:00:00Z")
        # Jan 8's result was learned after that point and must not cancel it.
        self.connection.execute(
            "UPDATE events SET start_time_utc = '2024-01-08T20:00:00Z' WHERE event_id = 'event-08'"
        )
        db.upsert_bout(self.connection, "extra-08", "event-08", "a-09", "c-08", "completed")
        db.upsert_result(self.connection, "extra-08", "win", "c-08", "2024-01-08T22:00:00Z")
        self.connection.commit()

        rows, _, _ = _point_in_time_rows(self.connection, 24)
        event_nine = next(row for row in rows if row.bout_id == "bout-09")
        self.assertAlmostEqual(event_nine.features[0], 16.0 / 400.0)

    def test_missing_start_history_can_still_inform_later_event(self):
        make_history(self.connection)
        self.connection.execute("UPDATE events SET start_time_utc = NULL WHERE event_id = 'event-07'")
        db.upsert_fighter(self.connection, "c-07", "c-07")
        db.upsert_bout(self.connection, "extra-07", "event-07", "a-09", "c-07", "completed")
        db.upsert_result(self.connection, "extra-07", "win", "a-09", "2024-01-07T14:00:00Z")
        self.connection.commit()

        rows, _, excluded = _point_in_time_rows(self.connection, 24)
        self.assertEqual(excluded, {"events": 1, "binary_bouts": 2})
        self.assertFalse(any(row.event_id == "event-07" for row in rows))
        event_nine = next(row for row in rows if row.bout_id == "bout-09")
        self.assertAlmostEqual(event_nine.features[0], 16.0 / 400.0)

    def test_invalid_cutoffs_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_models(self.connection, decision_hours_before_event=0)
        with self.assertRaises(ValueError):
            evaluate_models(self.connection, max_quote_age_hours=float("nan"))


if __name__ == "__main__":
    unittest.main()
