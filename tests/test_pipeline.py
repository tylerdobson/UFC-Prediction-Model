"""Integration checks for time cutoffs and realistic quote use."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.demo import seed_demo
from ufc_odds_model.pipeline import score_event, utc_string, walk_forward_backtest


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = db.connect(self.root / "test.sqlite")
        db.init_db(self.connection)
        self.now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        seed_demo(self.connection, self.now)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_future_result_does_not_change_an_earlier_prediction(self):
        _, before = score_event(self.connection, "demo-upcoming", self.now, self.root / "reports")
        probability = before[0]["p_fighter_a"]
        db.upsert_event(
            self.connection, "future-event", "Future event", "2026-09-27", "completed"
        )
        db.upsert_bout(
            self.connection, "future-bout", "future-event", "demo-a", "demo-c", "completed"
        )
        db.upsert_result(
            self.connection, "future-bout", "win", "demo-c", "2026-09-27T23:00:00Z"
        )
        self.connection.commit()
        _, after = score_event(self.connection, "demo-upcoming", self.now, self.root / "reports")
        self.assertEqual(after[0]["p_fighter_a"], probability)

    def test_backtest_rejects_quotes_after_decision_time(self):
        event = self.connection.execute(
            "SELECT start_time_utc FROM events WHERE event_id = 'demo-past-6'"
        ).fetchone()
        start = datetime.fromisoformat(event["start_time_utc"].replace("Z", "+00:00"))
        db.add_quote(
            self.connection, "demo-past-6-bout", "test-book", "demo-d", 4.0,
            utc_string(start + timedelta(minutes=1)), "test",
        )
        self.connection.commit()
        self.assertEqual(walk_forward_backtest(self.connection)["paper_bets"], 0)
        db.add_quote(
            self.connection, "demo-past-6-bout", "test-book", "demo-d", 3.0,
            utc_string(start - timedelta(hours=25)), "test",
        )
        self.connection.commit()
        result = walk_forward_backtest(self.connection)
        self.assertEqual(result["paper_bets"], 1)
        self.assertEqual(result["paper_profit_units"], 2.0)


if __name__ == "__main__":
    unittest.main()
