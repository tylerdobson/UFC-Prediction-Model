"""A forward research forecast cannot use source receipts from after cutoff."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.archived_pit_evaluation import _utc
from ufc_odds_model.captured_forward_history import _receipt_catalog, replay_captured_card


CUTOFF = "2026-09-26T22:47:55Z"


class CapturedForwardReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE ingestion_runs (
                run_id INTEGER PRIMARY KEY, source TEXT, fetched_at_utc TEXT,
                sha256 TEXT);
            CREATE TABLE wikipedia_source_receipts (
                run_id INTEGER PRIMARY KEY, event_page_id INTEGER, page_url TEXT,
                revision_timestamp_utc TEXT, imported_bouts INTEGER,
                skipped_unresolved_bouts INTEGER, crosswalk_run_id INTEGER);
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY, source_event_id TEXT,
                event_date TEXT, source TEXT, status TEXT);
            CREATE TABLE bouts (
                bout_id TEXT PRIMARY KEY, event_id TEXT, source TEXT, status TEXT);
            CREATE TABLE results (bout_id TEXT PRIMARY KEY);
            INSERT INTO ingestion_runs VALUES
                (1, 'wikipedia_research', '2026-09-26T22:30:00Z',
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
            INSERT INTO wikipedia_source_receipts VALUES
                (1, 11, 'https://en.wikipedia.org/wiki/Example',
                 '2026-09-20T11:00:00Z', 1, 2, NULL);
            INSERT INTO events VALUES
                ('wikipedia_research:11', '11', '2026-09-19',
                 'wikipedia_research', 'completed');
            INSERT INTO bouts VALUES
                ('bout-1', 'wikipedia_research:11', 'wikipedia_research', 'completed');
            INSERT INTO results VALUES ('bout-1');
        """)

    def test_exact_event_receipt_before_cutoff_is_admitted(self) -> None:
        report = _receipt_catalog(self.connection, _utc(CUTOFF, "cutoff"))
        self.assertEqual(report["completed_events_with_exact_receipts"], 1)
        self.assertEqual(report["held_source_bout_rows"], 2)
        self.assertEqual(report["ingestion_runs"], 1)

    def test_later_download_cannot_backdate_a_result(self) -> None:
        self.connection.execute(
            "UPDATE ingestion_runs SET fetched_at_utc = ? WHERE run_id = 1",
            ("2026-09-26T22:47:56Z",),
        )
        with self.assertRaisesRegex(ValueError, "at or after cutoff"):
            _receipt_catalog(self.connection, _utc(CUTOFF, "cutoff"))

    def test_revision_cannot_be_published_after_its_recorded_download(self) -> None:
        self.connection.execute(
            "UPDATE wikipedia_source_receipts SET revision_timestamp_utc = ?",
            ("2026-09-26T22:40:00Z",),
        )
        with self.assertRaisesRegex(ValueError, "published after its recorded fetch"):
            _receipt_catalog(self.connection, _utc(CUTOFF, "cutoff"))

    def test_identity_crosswalk_must_precede_result_receipt(self) -> None:
        self.connection.execute(
            """INSERT INTO ingestion_runs VALUES
               (0, 'wikipedia_identity_crosswalk', '2026-09-26T22:40:00Z', ?)""",
            ("b" * 64,),
        )
        self.connection.execute(
            "UPDATE wikipedia_source_receipts SET crosswalk_run_id = 0",
        )
        with self.assertRaisesRegex(ValueError, "crosswalk source or timing is invalid"):
            _receipt_catalog(self.connection, _utc(CUTOFF, "cutoff"))

    def test_identity_crosswalk_must_reference_crosswalk_source(self) -> None:
        self.connection.execute(
            """INSERT INTO ingestion_runs VALUES
               (0, 'wikipedia_catalog', '2026-09-26T22:20:00Z', ?)""",
            ("b" * 64,),
        )
        self.connection.execute(
            "UPDATE wikipedia_source_receipts SET crosswalk_run_id = 0",
        )
        with self.assertRaisesRegex(ValueError, "crosswalk source or timing is invalid"):
            _receipt_catalog(self.connection, _utc(CUTOFF, "cutoff"))

    def test_db_result_count_must_match_saved_source_receipt(self) -> None:
        self.connection.execute(
            "UPDATE wikipedia_source_receipts SET imported_bouts = 0",
        )
        with self.assertRaisesRegex(ValueError, "Result count differs"):
            _receipt_catalog(self.connection, _utc(CUTOFF, "cutoff"))

    def test_full_replay_rejects_one_market_for_two_selected_bouts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / name for name in ("card.json", "card.csv", "odds.json")]
            for path in paths:
                path.write_text("{}")
            verified = {
                "manifest": {"captured_at_utc": "2026-09-26T22:47:45Z",
                             "event": {"event_date": "2026-10-03"}},
                "event_start_utc": "2026-10-03T20:00:00Z",
            }
            selected = [{"source_position": "1"}, {"source_position": "2"}]
            odds = {"captured_at_utc": CUTOFF,
                    "comparison": {
                        1: {"status": "exact", "odds_event_id": "same"},
                        2: {"status": "exact", "odds_event_id": "same"},
                    }}
            with patch("ufc_odds_model.captured_forward_history.verify_manifest",
                       return_value=verified), patch(
                           "ufc_odds_model.captured_forward_history._selected_rows",
                           return_value=selected), patch(
                           "ufc_odds_model.captured_forward_history._odds_input",
                           return_value=odds), patch(
                           "ufc_odds_model.captured_forward_history.captured_research_probabilities"
                       ) as probabilities:
                with self.assertRaisesRegex(ValueError, "multiple selected card bouts"):
                    replay_captured_card(*paths, Path(temp) / "database.sqlite",
                                         Path(temp) / "holdout.json")
                probabilities.assert_not_called()


if __name__ == "__main__":
    unittest.main()
