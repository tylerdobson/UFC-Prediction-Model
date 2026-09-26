"""Checks that the audit finds data faults and respects historical price time."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.audit import audit_database


NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = db.connect(Path(self.temp.name) / "audit.sqlite")
        db.init_db(self.connection)
        for fighter_id, name in (("a", "Fighter A"), ("b", "Fighter B"), ("c", "Fighter C")):
            db.upsert_fighter(self.connection, fighter_id, name, "test", fighter_id)
        db.upsert_event(
            self.connection, "past", "Past card", "2026-09-20", "completed",
            start_time_utc="2026-09-20T20:00:00Z",
        )
        db.upsert_bout(self.connection, "past-bout", "past", "a", "b", "completed")
        db.upsert_result(
            self.connection, "past-bout", "win", "a", "2026-09-20T22:00:00Z"
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_quote_coverage_uses_only_fresh_prices_known_at_decision_time(self):
        # The decision is 24 hours before the event. The first pair was
        # captured after it; the second pair was known six hours before it.
        for fighter_id in ("a", "b"):
            db.add_quote(
                self.connection, "past-bout", "book", fighter_id, 2.0,
                "2026-09-19T21:00:00Z", "test",
                "2026-09-19T20:59:00Z",
            )
        report = audit_database(self.connection, as_of=NOW)
        coverage = report["summary"]["quote_coverage"]
        self.assertEqual(coverage["completed_win_bouts_with_any_quote"], 1)
        self.assertEqual(coverage["completed_win_bouts_with_fresh_decision_quote"], 0)
        self.assertIn("historical_quote_coverage_gap", {issue["code"] for issue in report["issues"]})

        for fighter_id in ("a", "b"):
            db.add_quote(
                self.connection, "past-bout", "book", fighter_id, 2.1,
                "2026-09-19T14:00:00Z", "test",
                "2026-09-19T13:59:00Z",
            )
        report = audit_database(self.connection, as_of=NOW)
        coverage = report["summary"]["quote_coverage"]
        self.assertEqual(coverage["completed_win_bouts_with_fresh_decision_quote"], 1)
        self.assertEqual(coverage["completed_win_bouts_with_two_sided_decision_quotes"], 1)

    def test_old_bookmaker_update_disqualifies_recent_capture(self):
        db.add_quote(
            self.connection, "past-bout", "book", "a", 2.0,
            "2026-09-19T14:00:00Z", "test", "2026-09-18T10:00:00Z",
        )
        db.add_quote(
            self.connection, "past-bout", "book", "b", 2.0,
            "2026-09-19T14:00:00Z", "test", "2026-09-19T13:59:00Z",
        )
        coverage = audit_database(self.connection, as_of=NOW)["summary"]["quote_coverage"]
        self.assertEqual(coverage["completed_win_bouts_with_fresh_decision_quote"], 1)
        self.assertEqual(coverage["completed_win_bouts_with_two_sided_decision_quotes"], 0)

    def test_referential_and_identity_faults_are_reported_without_writes(self):
        db.upsert_fighter(self.connection, "same-name", "Fighter A", "other", "other-a")
        self.connection.execute(
            "UPDATE results SET winner_fighter_id = 'c' WHERE bout_id = 'past-bout'"
        )
        self.connection.execute(
            "UPDATE bouts SET status = 'scheduled' WHERE bout_id = 'past-bout'"
        )
        self.connection.execute(
            "INSERT INTO odds_quotes(bout_id, bookmaker, market, selection_fighter_id, "
            "decimal_odds, captured_at_utc, source) "
            "VALUES ('past-bout', 'book', 'h2h', 'c', 2.0, '2026-09-19T14:00:00Z', 'test')"
        )
        self.connection.commit()
        changes_before = self.connection.total_changes
        report = audit_database(self.connection, as_of=NOW)
        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertTrue({
            "ambiguous_fighter_name", "winner_not_in_bout", "result_on_noncompleted_bout",
            "quote_selection_not_in_bout", "scheduled_bout_on_completed_event",
        } <= codes)
        self.assertEqual(self.connection.total_changes, changes_before)

    def test_missing_start_substitutions_and_stale_live_quote(self):
        db.upsert_event(self.connection, "future", "Future card", "2026-09-30", "scheduled")
        db.upsert_bout(self.connection, "old", "future", "a", "b", "cancelled")
        db.upsert_bout(self.connection, "replacement", "future", "a", "c", "scheduled")
        db.add_quote(
            self.connection, "replacement", "book", "a", 1.8,
            "2026-09-24T10:00:00Z", "test",
        )
        report = audit_database(self.connection, as_of=NOW)
        codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("missing_event_start", codes)
        self.assertIn("possible_opponent_substitution", codes)
        self.assertIn("stale_or_future_live_quote", codes)
        self.assertEqual(report["summary"]["possible_opponent_substitutions"], 1)
        self.assertEqual(report["summary"]["quote_coverage"]["scheduled_bouts_with_fresh_quote"], 0)

    def test_non_prefight_provider_status_is_flagged_and_excluded_from_live_coverage(self):
        db.upsert_event(
            self.connection, "future", "Future card", "2026-09-30", "scheduled",
            start_time_utc="2026-09-30T20:00:00Z", provider_status="live",
        )
        db.upsert_bout(
            self.connection, "future-bout", "future", "a", "c", "scheduled",
            provider_status="not_started",
        )
        db.add_quote(
            self.connection, "future-bout", "book", "a", 1.8,
            "2026-09-26T11:00:00Z", "test",
        )
        report = audit_database(self.connection, as_of=NOW)
        self.assertIn("event_provider_status_needs_review", {i["code"] for i in report["issues"]})
        self.assertEqual(report["summary"]["quote_coverage"]["scheduled_bouts"], 0)
        self.assertEqual(
            report["summary"]["quote_coverage"]["scheduled_bouts_excluded_by_provider_status"], 1
        )

        self.connection.execute(
            "UPDATE events SET provider_status = 'not_started' WHERE event_id = 'future'"
        )
        self.connection.execute(
            "UPDATE bouts SET provider_status = 'postponed' WHERE bout_id = 'future-bout'"
        )
        report = audit_database(self.connection, as_of=NOW)
        self.assertIn("bout_provider_status_needs_review", {i["code"] for i in report["issues"]})
        self.assertEqual(report["summary"]["quote_coverage"]["scheduled_bouts"], 0)

        self.connection.execute(
            "UPDATE bouts SET provider_status = 'scheduled' WHERE bout_id = 'future-bout'"
        )
        report = audit_database(self.connection, as_of=NOW)
        self.assertEqual(report["summary"]["quote_coverage"]["scheduled_bouts"], 1)
        self.assertEqual(report["summary"]["quote_coverage"]["scheduled_bouts_with_fresh_quote"], 1)

    def test_bad_time_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            audit_database(self.connection, as_of=datetime(2026, 9, 26))
        with self.assertRaises(ValueError):
            audit_database(self.connection, as_of=NOW, max_quote_age_hours=0)


if __name__ == "__main__":
    unittest.main()
