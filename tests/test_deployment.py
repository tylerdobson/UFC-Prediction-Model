"""A dashboard container starts only with a self-contained, verified snapshot."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.deployment import DeploymentError, check_dashboard_deployment


class DashboardDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.root / "reports").mkdir()
        raw = self.data / "raw" / "source.json"
        raw.parent.mkdir()
        raw.write_bytes(b'{"event":"fixture"}')
        self.raw = raw
        self.database = self.data / "dashboard.sqlite"
        with db.connect(self.database) as connection:
            db.init_db(connection)
            connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("fixture", "2026-01-01T00:00:00Z", str(raw),
                 hashlib.sha256(raw.read_bytes()).hexdigest()),
            )
            connection.commit()

    def test_checked_snapshot_can_be_served_without_writes(self) -> None:
        before = self.database.read_bytes()
        report = check_dashboard_deployment(self.root, self.database)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["verified_receipts"], 1)
        self.assertFalse(report["source_rights_checked"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_changed_payload_and_external_receipt_fail_closed(self) -> None:
        self.raw.write_bytes(b"changed")
        with self.assertRaisesRegex(DeploymentError, "integrity check failed"):
            check_dashboard_deployment(self.root, self.database)

        outside = self.root / "outside.json"
        outside.write_bytes(b"outside")
        outside_database = self.data / "outside-receipt.sqlite"
        with db.connect(outside_database) as connection:
            db.init_db(connection)
            connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("fixture", "2026-01-01T00:00:00Z", str(outside),
                 hashlib.sha256(outside.read_bytes()).hexdigest()),
            )
            connection.commit()
        with self.assertRaisesRegex(DeploymentError, "outside deployment data"):
            check_dashboard_deployment(self.root, outside_database)

    def test_live_sidecar_is_rejected_before_opening_database(self) -> None:
        Path(f"{self.database}-wal").write_bytes(b"live")
        with self.assertRaisesRegex(DeploymentError, "standalone backup snapshot"):
            check_dashboard_deployment(self.root, self.database)

    def test_unknown_schema_version_is_rejected(self) -> None:
        with db.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_utc) VALUES (?, ?)",
                ("999_future.sql", "2026-01-01T00:00:00Z"),
            )
            connection.commit()
        with self.assertRaisesRegex(DeploymentError, "incompatible"):
            check_dashboard_deployment(self.root, self.database)

    def test_missing_snapshot_and_relative_paths_are_rejected(self) -> None:
        with self.assertRaisesRegex(DeploymentError, "absolute path"):
            check_dashboard_deployment(self.root, "data/dashboard.sqlite")
        with self.assertRaisesRegex(DeploymentError, "regular file under"):
            check_dashboard_deployment(self.root, self.data / "missing.sqlite")


if __name__ == "__main__":
    unittest.main()
