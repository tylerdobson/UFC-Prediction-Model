"""A recovery archive must carry usable source receipts and artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model import evidence_bundle
from ufc_odds_model.evidence_bundle import (
    BundleError, create_bundle, restore_bundle, verify_bundle,
)
from ufc_odds_model.integrity import verify_evidence


class EvidenceBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "operating.sqlite"
        self.payload = self.root / "raw" / "source.json"
        self.payload.parent.mkdir()
        self.payload.write_text('{"card":"reviewed fixture"}', encoding="utf-8")
        self.digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        with db.connect(self.database) as connection:
            db.init_db(connection)
            connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("fixture", "2026-01-01T00:00:00Z", str(self.payload), self.digest),
            )
            connection.commit()
        self.models = self.root / "models"
        self.models.mkdir()
        (self.models / "version.json").write_text('{"version":"fixture"}', encoding="utf-8")
        self.reports = self.root / "reports"
        self.reports.mkdir()
        (self.reports / "evaluation.json").write_text('{"status":"fixture"}', encoding="utf-8")

    def _bundle(self) -> Path:
        archive = self.root / "backups" / "evidence.zip"
        result = create_bundle(self.database, archive, models_dir=self.models,
                               reports_dir=self.reports)
        self.assertTrue(result["ok"])
        return archive

    def test_verified_restore_rebases_receipts_without_altering_original(self) -> None:
        archive = self._bundle()
        original = self.database.read_bytes()
        report = verify_bundle(archive)
        self.assertEqual(report["receipt_count"], 1)
        self.assertEqual(report["artifact_count"], 2)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        destination = self.root / "recovered"
        recovered = restore_bundle(archive, destination)
        self.assertEqual(recovered["receipt_count"], 1)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual((destination / "payloads" / "run-00000001").read_bytes(),
                         self.payload.read_bytes())
        self.assertEqual((destination / "models" / "version.json").read_text(),
                         '{"version":"fixture"}')
        self.assertEqual((destination / "reports" / "evaluation.json").read_text(),
                         '{"status":"fixture"}')
        self.assertTrue(verify_evidence(destination / "database.sqlite")["ok"])
        with sqlite3.connect(destination / "database.sqlite") as connection:
            path = connection.execute(
                "SELECT payload_path FROM ingestion_runs WHERE run_id = 1"
            ).fetchone()[0]
            self.assertEqual(path, str((destination / "payloads" / "run-00000001").resolve()))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE ingestion_runs SET payload_path = ? WHERE run_id = 1", ("bad",)
                )
        self.assertEqual(recovered["rebindings"][0]["original_path"], str(self.payload))
        saved_report = json.loads((destination / "restore_report.json").read_text())
        self.assertEqual(saved_report["bundle_sha256"], report["bundle_sha256"])

    def test_missing_or_changed_payload_prevents_bundle_publish(self) -> None:
        self.payload.write_text("changed", encoding="utf-8")
        output = self.root / "bad.zip"
        with self.assertRaisesRegex(BundleError, "changed"):
            create_bundle(self.database, output, models_dir=self.models,
                          reports_dir=self.reports)
        self.assertFalse(output.exists())

    def test_archive_tampering_is_rejected_before_recovery(self) -> None:
        original = self._bundle()
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(tampered, "w") as target:
            for name in source.namelist():
                data = source.read(name)
                target.writestr(name, b"changed" if name.startswith("payloads/") else data)
        with self.assertRaisesRegex(BundleError, "Archive file changed"):
            verify_bundle(tampered)
        destination = self.root / "not-created"
        with self.assertRaises(BundleError):
            restore_bundle(tampered, destination)
        self.assertFalse(destination.exists())

    def test_existing_bundle_and_recovery_are_never_replaced(self) -> None:
        archive = self._bundle()
        before = archive.read_bytes()
        with self.assertRaises(FileExistsError):
            create_bundle(self.database, archive, models_dir=self.models,
                          reports_dir=self.reports)
        self.assertEqual(archive.read_bytes(), before)
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            restore_bundle(archive, destination)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_relative_artifact_directories_use_project_root(self) -> None:
        other_cwd = self.root / "other-working-directory"
        other_cwd.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(other_cwd)
            archive = self.root / "from-elsewhere.zip"
            report = create_bundle(
                self.database, archive, project_root=self.root,
                models_dir="models", reports_dir="reports",
            )
        finally:
            os.chdir(previous)
        self.assertEqual(report["artifact_count"], 2)
        with zipfile.ZipFile(archive) as contents:
            self.assertIn("models/version.json", contents.namelist())
            self.assertIn("reports/evaluation.json", contents.namelist())

    def test_recovery_uses_verified_copy_if_source_archive_is_replaced(self) -> None:
        archive = self._bundle()
        original_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        tampered = self.root / "replacement.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
            for name in source.namelist():
                data = source.read(name)
                target.writestr(name, b"evil" if name == "models/version.json" else data)

        actual_verify = evidence_bundle.verify_bundle

        def verify_then_replace(stable_path: Path) -> dict:
            result = actual_verify(stable_path)
            os.replace(tampered, archive)
            return result

        recovered = self.root / "recovered-after-swap"
        with patch.object(evidence_bundle, "verify_bundle", side_effect=verify_then_replace):
            report = restore_bundle(archive, recovered)
        self.assertEqual(report["bundle_sha256"], original_sha)
        self.assertEqual((recovered / "models" / "version.json").read_text(),
                         '{"version":"fixture"}')

    def test_older_schema_bundle_restores_before_explicit_migration(self) -> None:
        old_database = self.root / "old.sqlite"
        migration_dir = files("ufc_odds_model").joinpath("sql/migrations")
        with sqlite3.connect(old_database) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
            for name in ("001_initial.sql", "002_provider_status.sql",
                         "003_fighter_external_ids.sql", "004_paper_bets.sql"):
                connection.executescript(migration_dir.joinpath(name).read_text())
                connection.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')",
                                   (name,))
            connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("fixture", "2026-01-01T00:00:00Z", str(self.payload), self.digest),
            )
            connection.commit()
        archive = self.root / "old.zip"
        create_bundle(old_database, archive, models_dir=self.models, reports_dir=self.reports)
        recovered = self.root / "old-recovered"
        result = restore_bundle(archive, recovered)
        self.assertEqual(result["receipt_count"], 1)
        evidence = verify_evidence(recovered / "database.sqlite")
        self.assertEqual({issue["code"] for issue in evidence["issues"]}, {"missing_migrations"})
        self.assertEqual(evidence["verified_receipts"], 1)


if __name__ == "__main__":
    unittest.main()
