"""Evidence verification catches changed source files without changing the DB."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.importers import import_bouts_csv
from ufc_odds_model.integrity import main, verify_evidence


CSV = (
    "event_id,event_name,event_date,event_status,bout_id,fighter_a_id,"
    "fighter_a_name,fighter_b_id,fighter_b_name,bout_status,outcome,winner_fighter_id\n"
    "card-1,Card One,2025-01-01,completed,bout-1,one,Fighter One,two,"
    "Fighter Two,completed,win,one\n"
)


class EvidenceIntegrityTests(unittest.TestCase):
    def test_missing_database_does_not_create_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.sqlite"
            report = verify_evidence(path)
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["issues"][0]["code"], "missing_database")
            self.assertFalse(path.exists())

    def test_saved_csv_is_verified_then_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "ufc.sqlite"
            source = root / "bouts.csv"
            source.write_text(CSV, encoding="utf-8")
            with db.connect(database) as connection:
                db.init_db(connection)
                import_bouts_csv(connection, source, root / "raw")
                snapshot = Path(connection.execute(
                    "SELECT payload_path FROM ingestion_runs"
                ).fetchone()[0])
            before = database.stat().st_mtime_ns
            clean = verify_evidence(database, project_root=root)
            self.assertTrue(clean["ok"])
            self.assertEqual(clean["status"], "verified")
            self.assertEqual(clean["receipt_count"], 1)
            self.assertEqual(clean["verified_receipts"], 1)
            self.assertEqual(clean["by_source"], {"manual-csv": 1})
            self.assertEqual(database.stat().st_mtime_ns, before)
            saved_report = root / "integrity.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--db", str(database), "--output", str(saved_report)]), 0)
            self.assertEqual(json.loads(saved_report.read_text(encoding="utf-8"))["status"], "verified")

            snapshot.write_text("changed after import", encoding="utf-8")
            changed = verify_evidence(database, project_root=root)
            self.assertFalse(changed["ok"])
            self.assertEqual(changed["verified_receipts"], 0)
            self.assertEqual(changed["issues"][0]["code"], "payload_hash_mismatch")

            snapshot.unlink()
            missing = verify_evidence(database, project_root=root)
            self.assertEqual(missing["issues"][0]["code"], "missing_payload")

            snapshot.symlink_to(source)
            linked = verify_evidence(database, project_root=root)
            self.assertEqual(linked["issues"][0]["code"], "symlink_payload")

    def test_uninitialized_database_reports_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "empty.sqlite"
            with sqlite3.connect(database):
                pass
            report = verify_evidence(database)
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["issues"][0]["code"], "database_read_failed")


if __name__ == "__main__":
    unittest.main()
