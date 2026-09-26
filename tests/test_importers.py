"""Reviewed CSV imports retain exact source evidence and commit atomically."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.importers import import_bouts_csv, import_ufcstats_events


HEADER = (
    "event_id,event_name,event_date,event_status,start_time_utc,bout_id,"
    "fighter_a_id,fighter_a_name,fighter_b_id,fighter_b_name,bout_status,"
    "weight_class,outcome,winner_fighter_id,method\n"
)
COMPLETED = (
    "past,Past card,2026-01-03,completed,2026-01-03T20:00:00Z,past-bout,"
    "a,Alice Example,b,Bob Example,completed,Lightweight,win,a,Decision\n"
)
SCHEDULED = (
    "future,Future card,2026-12-05,scheduled,2026-12-05T20:00:00Z,future-bout,"
    "a,Alice Example,c,Cara Example,scheduled,Lightweight,,,\n"
)


class ReviewedCsvImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "reviewed.csv"
        self.raw = self.root / "raw"
        self.connection = db.connect(self.root / "data.sqlite")
        self.addCleanup(self.connection.close)
        db.init_db(self.connection)

    def test_retains_exact_bytes_hash_and_receipt(self) -> None:
        payload = ("\ufeff" + HEADER + COMPLETED + SCHEDULED).encode("utf-8")
        self.source.write_bytes(payload)
        count = import_bouts_csv(self.connection, self.source, self.raw)
        self.assertEqual(count, 2)
        receipt = self.connection.execute(
            "SELECT source, fetched_at_utc, payload_path, sha256 FROM ingestion_runs"
        ).fetchone()
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(receipt["source"], "manual-csv")
        self.assertEqual(receipt["sha256"], digest)
        snapshot = Path(receipt["payload_path"])
        self.assertEqual(snapshot, self.raw.resolve() / f"{digest}.csv")
        self.assertEqual(snapshot.read_bytes(), payload)
        self.assertTrue(receipt["fetched_at_utc"].endswith("Z"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM results").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT outcome, winner_fighter_id FROM results WHERE bout_id = 'past-bout'"
        ).fetchone()["winner_fighter_id"], "a")

        self.source.write_text("changed source", encoding="utf-8")
        self.assertEqual(snapshot.read_bytes(), payload)

    def test_replay_reuses_snapshot_preserves_result_time_and_logs_new_receipt(self) -> None:
        payload = (HEADER + COMPLETED).encode("utf-8")
        self.source.write_bytes(payload)
        first_time = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        with patch("ufc_odds_model.importers.utc_now", return_value=first_time):
            self.assertEqual(import_bouts_csv(self.connection, self.source, self.raw), 1)
        recorded_at = self.connection.execute(
            "SELECT recorded_at_utc FROM results WHERE bout_id = 'past-bout'"
        ).fetchone()[0]
        with patch("ufc_odds_model.importers.utc_now", return_value=first_time + timedelta(days=1)):
            self.assertEqual(import_bouts_csv(self.connection, self.source, self.raw), 1)
        receipts = self.connection.execute(
            "SELECT payload_path, sha256 FROM ingestion_runs ORDER BY run_id"
        ).fetchall()
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]["payload_path"], receipts[1]["payload_path"])
        self.assertEqual(receipts[0]["sha256"], receipts[1]["sha256"])
        self.assertEqual(len(list(self.raw.glob("*.csv"))), 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM results").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT recorded_at_utc FROM results WHERE bout_id = 'past-bout'"
        ).fetchone()[0], recorded_at)

    def test_invalid_csv_creates_no_data_snapshot_or_receipt(self) -> None:
        for invalid, expected in (
            ("event_id,event_name\nx,Card\n", "missing columns"),
            (HEADER + COMPLETED.replace(",win,a,Decision", ",draw,a,Decision"),
             "draw/no contest cannot have a winner"),
            (HEADER + COMPLETED.replace("2026-01-03", "not-a-date", 1),
             "event_date must be YYYY-MM-DD"),
        ):
            with self.subTest(expected=expected):
                self.source.write_text(invalid, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    import_bouts_csv(self.connection, self.source, self.raw)
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 0)
                self.assertEqual(self.connection.execute(
                    "SELECT COUNT(*) FROM ingestion_runs"
                ).fetchone()[0], 0)
                self.assertFalse(self.raw.exists())

    def test_conflict_rolls_back_rows_and_receipt(self) -> None:
        conflicting = (
            HEADER + COMPLETED + COMPLETED.replace(
                "past-bout,a,Alice Example,b,Bob Example",
                "past-bout,a,Alice Example,c,Cara Example",
            )
        )
        self.source.write_text(conflicting, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed matchup"):
            import_bouts_csv(self.connection, self.source, self.raw)
        for table in ("fighters", "events", "bouts", "results", "ingestion_runs"):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertFalse(self.raw.exists())

    def test_existing_hash_path_with_different_payload_is_never_overwritten(self) -> None:
        payload = (HEADER + COMPLETED).encode("utf-8")
        self.source.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        self.raw.mkdir()
        snapshot = self.raw / f"{digest}.csv"
        snapshot.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "Existing CSV snapshot differs"):
            import_bouts_csv(self.connection, self.source, self.raw)
        self.assertEqual(snapshot.read_bytes(), b"tampered")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0], 0)


class UFCStatsSnapshotTests(unittest.TestCase):
    def test_replay_keeps_content_address_and_tampering_blocks_receipt(self) -> None:
        event = {
            "source_event_id": "source-event", "source_event_url": "https://ufcstats.test/event",
            "name": "Fixture card", "date": "2026-01-03",
        }
        bout = {
            "source_bout_id": "source-bout", "source_event_id": "source-event",
            "fighter_a_source_id": "fighter-a", "fighter_a_name": "Fighter A",
            "fighter_b_source_id": "fighter-b", "fighter_b_name": "Fighter B",
            "winner_source_id": "fighter-a", "weight_class": "Lightweight",
            "method": "Decision", "status": "completed", "outcome": "win",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = db.connect(root / "test.sqlite")
            self.addCleanup(connection.close)
            db.init_db(connection)
            raw_dir = root / "raw"
            with patch("ufc_odds_model.importers.ufcstats.fetch_completed_events", return_value=[event]), \
                    patch("ufc_odds_model.importers.ufcstats.fetch_event_bouts", return_value=[bout]):
                self.assertEqual(import_ufcstats_events(connection, "completed", 1, raw_dir),
                                 {"events": 1, "bouts": 1})
                first = connection.execute(
                    "SELECT payload_path, sha256 FROM ingestion_runs"
                ).fetchone()
                raw_path = Path(first["payload_path"])
                self.assertEqual(raw_path, raw_dir.resolve() / f"{first['sha256']}.json")
                original = raw_path.read_bytes()
                self.assertEqual(hashlib.sha256(original).hexdigest(), first["sha256"])
                self.assertEqual(import_ufcstats_events(connection, "completed", 1, raw_dir),
                                 {"events": 1, "bouts": 1})
                receipts = connection.execute(
                    "SELECT payload_path FROM ingestion_runs ORDER BY run_id"
                ).fetchall()
                self.assertEqual([row["payload_path"] for row in receipts], [str(raw_path)] * 2)
                self.assertEqual(raw_path.read_bytes(), original)
                raw_path.write_bytes(b"tampered evidence")
                with self.assertRaisesRegex(ValueError, "differs from its content address"):
                    import_ufcstats_events(connection, "completed", 1, raw_dir)
                self.assertEqual(raw_path.read_bytes(), b"tampered evidence")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM ingestion_runs"
                ).fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
