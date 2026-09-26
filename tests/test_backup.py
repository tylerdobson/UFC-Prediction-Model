"""A backup is usable only after a real restore and SQLite checks pass."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.backup import BackupError, create_backup, restore_backup, verify_backup


class BackupDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "ufc.sqlite"
        with db.connect(self.database) as connection:
            db.init_db(connection)
            db.upsert_fighter(connection, "f1", "First Fighter")
            db.upsert_fighter(connection, "f2", "Second Fighter")
            db.upsert_event(connection, "event-1", "Fixture Card", "2026-01-01", "completed")
            db.upsert_bout(connection, "bout-1", "event-1", "f1", "f2", "completed")
            connection.commit()

    def test_create_verify_restore_and_count_all_tables(self) -> None:
        backup = self.root / "backups" / "dated.sqlite"
        before = self.database.read_bytes()
        report = create_backup(self.database, backup)
        self.assertTrue(report["ok"])
        self.assertTrue(report["temporary_restore_matches"])
        self.assertEqual(report["integrity_check"], "ok")
        self.assertEqual(report["foreign_key_violations"], 0)
        self.assertEqual(report["table_counts"]["fighters"], 2)
        self.assertEqual(report["table_counts"]["events"], 1)
        self.assertEqual(report["table_counts"]["bouts"], 1)
        self.assertIn("schema_migrations", report["table_counts"])
        self.assertEqual(self.database.read_bytes(), before)
        self.assertTrue(backup.is_file())
        self.assertFalse(Path(f"{backup}-wal").exists())
        self.assertFalse(Path(f"{backup}-shm").exists())

        backup_bytes = backup.read_bytes()
        checked = verify_backup(backup)
        self.assertEqual(checked["table_counts"], report["table_counts"])
        self.assertEqual(backup.read_bytes(), backup_bytes)
        restored = self.root / "recovered" / "ufc.sqlite"
        recovered = restore_backup(backup, restored)
        self.assertTrue(recovered["backup_counts_match"])
        self.assertEqual(recovered["table_counts"], report["table_counts"])
        with sqlite3.connect(restored) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 1)

    def test_existing_destination_is_never_overwritten(self) -> None:
        backup = self.root / "dated.sqlite"
        backup.write_bytes(b"existing backup sentinel")
        with self.assertRaises(FileExistsError):
            create_backup(self.database, backup)
        self.assertEqual(backup.read_bytes(), b"existing backup sentinel")
        self.assertFalse(list(self.root.glob(".ufc-sqlite-*")))

    def test_restore_refuses_existing_path(self) -> None:
        backup = self.root / "dated.sqlite"
        create_backup(self.database, backup)
        destination = self.root / "existing.sqlite"
        destination.write_bytes(b"preserve this")
        with self.assertRaises(FileExistsError):
            restore_backup(backup, destination)
        self.assertEqual(destination.read_bytes(), b"preserve this")

    def test_missing_source_does_not_create_backup(self) -> None:
        missing = self.root / "missing.sqlite"
        backup = self.root / "dated.sqlite"
        with self.assertRaises(BackupError):
            create_backup(missing, backup)
        self.assertFalse(missing.exists())
        self.assertFalse(backup.exists())

    def test_foreign_key_violation_rejects_backup_before_publish(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("INSERT INTO bouts(bout_id, event_id, fighter_a_id, "
                               "fighter_b_id, status) VALUES "
                               "('orphan', 'missing-event', 'f1', 'f2', 'completed')")
            connection.commit()
        backup = self.root / "dated.sqlite"
        with self.assertRaisesRegex(BackupError, "foreign_key_check"):
            create_backup(self.database, backup)
        self.assertFalse(backup.exists())
        self.assertFalse(list(self.root.glob(".ufc-sqlite-*")))

    def test_wal_pages_are_captured_without_manual_checkpoint(self) -> None:
        writer = sqlite3.connect(self.database)
        try:
            self.assertEqual(writer.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
            writer.execute("INSERT INTO fighters(fighter_id, canonical_name) "
                           "VALUES ('f3', 'Recent Fighter')")
            writer.commit()
            self.assertTrue(Path(f"{self.database}-wal").exists())
            backup = self.root / "dated.sqlite"
            report = create_backup(self.database, backup)
            self.assertEqual(report["table_counts"]["fighters"], 3)
            with sqlite3.connect(backup) as copied:
                self.assertEqual(copied.execute("SELECT COUNT(*) FROM fighters").fetchone()[0], 3)
            self.assertFalse(Path(f"{backup}-wal").exists())
        finally:
            writer.close()

    def test_corrupt_existing_backup_fails_restore_drill(self) -> None:
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"this is not a SQLite database")
        with self.assertRaises(sqlite3.DatabaseError):
            verify_backup(broken)

    def test_sidecar_backup_is_rejected_as_non_standalone(self) -> None:
        backup = self.root / "dated.sqlite"
        create_backup(self.database, backup)
        wal = Path(f"{backup}-wal")
        wal.write_bytes(b"sidecar")
        with self.assertRaisesRegex(BackupError, "sidecar"):
            verify_backup(backup)
        with self.assertRaisesRegex(BackupError, "sidecar"):
            restore_backup(backup, self.root / "restored.sqlite")
        self.assertFalse((self.root / "restored.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
