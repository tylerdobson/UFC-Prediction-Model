"""Point-in-time and outcome-orientation checks for fight features."""

from __future__ import annotations

import sqlite3
import unittest

from ufc_odds_model import db
from ufc_odds_model.features import FEATURE_NAMES, event_feature_rows, training_rows


class FeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        db.init_db(self.connection)
        for fighter in "abc":
            db.upsert_fighter(self.connection, fighter, fighter.upper())
        db.upsert_event(self.connection, "e1", "First", "2024-01-01", "completed")
        db.upsert_bout(self.connection, "first", "e1", "a", "b", "completed")
        db.upsert_result(self.connection, "first", "win", "b", "2024-01-02T00:00:00Z")
        db.upsert_bout(self.connection, "same-day", "e1", "c", "a", "completed")
        db.upsert_result(self.connection, "same-day", "win", "a", "2024-01-02T00:00:00Z")
        db.upsert_event(self.connection, "e2", "Second", "2024-01-10", "completed")
        db.upsert_bout(self.connection, "second", "e2", "b", "c", "completed")
        db.upsert_result(self.connection, "second", "win", "b", "2024-01-11T00:00:00Z")
        db.upsert_event(self.connection, "e3", "Upcoming", "2024-01-20", "scheduled")
        db.upsert_bout(self.connection, "future", "e3", "c", "b", "scheduled")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def test_training_order_does_not_reveal_winner(self):
        rows = {row.bout_id: row for row in training_rows(self.connection)}
        self.assertEqual(rows["first"].fighter_a_id, "a")
        self.assertEqual(rows["first"].target, 0)
        self.assertEqual(rows["same-day"].fighter_a_id, "a")
        self.assertEqual(rows["same-day"].target, 1)
        self.assertEqual(rows["first"].features, (0.0,) * len(FEATURE_NAMES))
        self.assertEqual(rows["same-day"].features, (0.0,) * len(FEATURE_NAMES))
        self.assertNotEqual(rows["second"].features, (0.0,) * len(FEATURE_NAMES))

    def test_cutoff_excludes_results_that_were_not_yet_available(self):
        early = event_feature_rows(self.connection, "e3", "2024-01-05")[0]
        late = event_feature_rows(self.connection, "e3", "2024-01-15")[0]
        self.assertNotEqual(early.features, late.features)
        self.assertEqual(len(training_rows(self.connection, "2024-01-10")), 2)
        before = early.features
        db.upsert_event(self.connection, "e4", "Later", "2024-02-01", "completed")
        db.upsert_bout(self.connection, "later", "e4", "b", "c", "completed")
        db.upsert_result(self.connection, "later", "win", "c", "2024-02-02T00:00:00Z")
        self.assertEqual(event_feature_rows(self.connection, "e3", "2024-01-05")[0].features, before)

    def test_draw_updates_history_but_no_contest_does_not(self):
        db.upsert_event(self.connection, "e4", "Draw", "2024-01-12", "completed")
        db.upsert_bout(self.connection, "draw", "e4", "a", "c", "completed")
        db.upsert_result(self.connection, "draw", "draw", None, "2024-01-13T00:00:00Z")
        with_draw = event_feature_rows(self.connection, "e3")[0].features
        self.assertEqual(len(training_rows(self.connection)), 3)
        db.upsert_event(self.connection, "e5", "NC", "2024-01-15", "completed")
        db.upsert_bout(self.connection, "nc", "e5", "b", "c", "completed")
        db.upsert_result(self.connection, "nc", "no_contest", None, "2024-01-16T00:00:00Z")
        self.assertEqual(event_feature_rows(self.connection, "e3")[0].features, with_draw)

    def test_swapping_pair_negates_every_feature(self):
        before = event_feature_rows(self.connection, "e3")[0]
        db.upsert_event(self.connection, "e4", "Other", "2024-01-20", "scheduled")
        db.upsert_bout(self.connection, "reverse", "e4", "b", "c", "scheduled")
        reverse = event_feature_rows(self.connection, "e4")[0]
        for first, second in zip(before.features, reverse.features):
            self.assertAlmostEqual(first, -second)


if __name__ == "__main__":
    unittest.main()
