"""Logical MySQL recovery is offline-verifiable and never activates live data."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import uuid

from ufc_odds_model import backup, evidence_bundle, integrity, mysql_backup as subject, mysql_runtime


SCHEMA = "CREATE TABLE ingestion_runs(run_id INTEGER PRIMARY KEY,source TEXT NOT NULL,payload_path TEXT NOT NULL,sha256 TEXT NOT NULL);"


def dataset(payload_path="data/raw/capture.txt", payload=b"retained public evidence"):
    return subject._snapshot_document(SCHEMA, {"ingestion_runs": [[1, "public-capture", payload_path, hashlib.sha256(payload).hexdigest()]]}, dataset="source", metadata={})


def delivery(*, forecast_path=None, forecast_payload=b"frozen report"):
    raw = subject._json({"divisions": [{"name": "Heavyweight", "champion": "Real source name"}]})
    digest = hashlib.sha256(raw.encode()).hexdigest(); captured = "2026-10-19T10:00:00Z"
    identity = hashlib.sha256(subject._json(["rankings", "current", captured, None, digest]).encode()).hexdigest()
    rows = {"ufc_app_document": [[identity, "rankings", "current", raw, digest, None, captured, captured]],
            "ufc_app_document_active": [["rankings", "current", identity]],
            "ufc_app_forecast_chain": [], "ufc_app_forecast_entry": []}
    if forecast_path:
        payload = {"schema_version": 1, "kind": "freeze", "forecast_id": "real-edition", "previous_sha256": "0" * 64,
                   "source_path": forecast_path, "source_sha256": hashlib.sha256(forecast_payload).hexdigest()}
        payload["entry_sha256"] = hashlib.sha256(subject._json(payload).encode()).hexdigest()
        rows["ufc_app_forecast_chain"] = [["forecast", payload["entry_sha256"], 1]]
        rows["ufc_app_forecast_entry"] = [["forecast", 1, payload["entry_sha256"], "0" * 64, "freeze", "real-edition", subject._json(payload)]]
    native = {name: "CREATE TABLE " + name for name in rows}
    return subject._snapshot_document(subject._DELIVERY_SCHEMA, rows, dataset="delivery-recovery-archive",
        metadata={"native_schema_sql": native, "native_schema_sha256": hashlib.sha256(subject._json(native).encode()).hexdigest()})


def rehash(snapshot):
    digest = hashlib.sha256()
    for name, table in sorted(snapshot["tables"].items()):
        digest.update(subject._json([name, table["columns"]]).encode() + b"\n")
        for row in table["rows"]: digest.update(subject._json(row).encode() + b"\n")
    snapshot["content_sha256"] = digest.hexdigest()
    return snapshot


def envelope(snapshot=None, documents=None):
    result = {"format_version": 1, "storage_backend": "mysql-logical-backup", "snapshot": snapshot or dataset(),
              "delivery_snapshot": documents or delivery(), "native_schema_check": {"ok": True, "native_schema_sha256": "a" * 64}}
    result["envelope_sha256"] = hashlib.sha256(subject._json(result).encode()).hexdigest()
    return result


class FakeNative:
    """Supplies native metadata rows; execute('PRAGMA') cannot satisfy this fake."""
    def __init__(self):
        self.dataset = "qa-native"; self.closed = False
        self.tables, _, _ = mysql_runtime.describe_schema("CREATE TABLE parent(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE); CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id));")
        self.native = {key: [] for key in ("TABLES", "COLUMNS", "KEY_COLUMN_USAGE", "STATISTICS", "TRIGGERS")}
        for name, table in self.tables.items():
            table["physical"] = "qa_" + name; physical = table["physical"]
            self.native["TABLES"].append((physical, "InnoDB"))
            for column in table["columns"]:
                required = column["notnull"] or column["name"] in table["primary"]
                self.native["COLUMNS"].append((physical, column["name"], column["mysql_type"], "NO" if required else "YES", "auto_increment" if column["name"] == table["ordinal"] else ""))
            for i, column in enumerate(table["primary"], 1):
                self.native["KEY_COLUMN_USAGE"].append((physical, "PRIMARY", column, None, None, i))
            for number, fields in enumerate([table["primary"], *table["unique"]]):
                for i, column in enumerate(fields, 1): self.native["STATISTICS"].append((physical, str(number), 0, column, i))
            for operation in ("INSERT", "UPDATE", "DELETE"):
                trigger = "guard_" + hashlib.sha256(("qa_" + name + operation).encode()).hexdigest()[:32]
                body = "BEGIN IF EXISTS(SELECT 1 FROM ufc_runtime_datasets WHERE dataset='qa-native' AND immutable=1 AND status='ready') THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='published imported datasets are immutable'; END IF; END"
                self.native["TRIGGERS"].append((physical, trigger, "BEFORE", operation, body))
        self.native["KEY_COLUMN_USAGE"].append(("qa_child", "fk1", "parent_id", "qa_parent", "id", 1))
        self.raw = self; self.violations = []
    @contextmanager
    def cursor(self): yield self
    def execute(self, sql, params=()):
        if "information_schema." not in sql: raise AssertionError("Native checks must query actual MySQL metadata")
        self.results = self.native[sql.split("information_schema.")[1].split()[0]]
    def fetchall(self): return self.results
    def foreign_key_violations(self): return self.violations
    def close(self): self.closed = True


class MySQLMaintenanceTests(unittest.TestCase):
    def test_independent_logical_schema_content_and_fk_validation(self):
        saved = dataset(); self.assertTrue(subject.validate_snapshot(saved)["ok"])
        altered = copy.deepcopy(saved); altered["tables"]["ingestion_runs"]["rows"][0][-1] = "0" * 64
        with self.assertRaises(Exception): subject.validate_snapshot(altered)
        altered = copy.deepcopy(saved); altered["schema_sql"] += " "
        with self.assertRaises(Exception): subject.validate_snapshot(altered)
        altered = copy.deepcopy(saved); altered["tables"]["ingestion_runs"]["rows"].append(altered["tables"]["ingestion_runs"]["rows"][0])
        with self.assertRaises(Exception): subject.validate_snapshot(rehash(altered))

    def test_global_documents_active_pointers_chain_and_tail_are_verified(self):
        saved = delivery(forecast_path="output/report.json")
        checked = subject.validate_delivery_snapshot(saved)
        self.assertEqual((checked["document_count"], checked["entry_count"]), (1, 1))
        for table, index, value in (("ufc_app_document", 4, "changed payload"),
                                    ("ufc_app_document_active", 1, "evaluation"),
                                    ("ufc_app_forecast_chain", 2, "0" * 64),
                                    ("ufc_app_forecast_entry", 4, "f" * 64)):
            altered = copy.deepcopy(saved); altered["tables"][table]["rows"][0][index] = value
            with self.subTest(table=table), self.assertRaises(Exception): subject.validate_delivery_snapshot(rehash(altered))

    def test_native_checks_require_real_keys_types_guards_and_anti_join(self):
        native = FakeNative(); self.assertTrue(subject.verify_native_schema(native)["ok"])
        native.native["KEY_COLUMN_USAGE"].pop()
        self.assertIn("native_foreign_keys_differ", [i["code"] for i in subject.verify_native_schema(native)["issues"]])
        native = FakeNative(); item = list(native.native["COLUMNS"][1]); item[2] = "varchar(255)"; native.native["COLUMNS"][1] = tuple(item)
        self.assertIn("native_columns_differ", [i["code"] for i in subject.verify_native_schema(native)["issues"]])
        native = FakeNative(); trigger = list(native.native["TRIGGERS"][0]); trigger[4] = trigger[4].replace("immutable=1", "immutable=0"); native.native["TRIGGERS"][0] = tuple(trigger)
        self.assertFalse(subject.verify_native_schema(native)["ok"])
        native = FakeNative(); native.violations = [("child", 1)]; self.assertEqual(subject.verify_native_schema(native)["foreign_key_violations"], 1)

    def test_private_json_checksum_symlink_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private.json"; subject._publish_json(envelope(), path)
            with patch("sqlite3.connect", side_effect=AssertionError("No SQLite conversion")):
                self.assertTrue(backup.verify_backup(path)["ok"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): subject._publish_json({}, path)
            path.chmod(0o644)
            with self.assertRaises(subject.MySQLBackupError): subject.verify_backup(path)
            path.chmod(0o600); linked = path.with_name("linked.json"); linked.symlink_to(path)
            with self.assertRaises(backup.BackupError): backup.verify_backup(linked)
            changed = json.loads(path.read_text()); changed["snapshot"]["content_sha256"] = "a" * 64; path.write_text(json.dumps(changed))
            with self.assertRaises(subject.MySQLBackupError): subject.verify_backup(path)

    def test_restore_refuses_file_or_used_namespace_before_import(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private.json"; subject._publish_json(envelope(), path)
            with patch.object(mysql_runtime, "import_dataset_snapshot") as importer:
                with self.assertRaises(subject.MySQLBackupError): backup.restore_backup(path, Path(folder) / "no.sqlite")
                with patch.object(mysql_runtime, "dataset_exists", return_value=True):
                    with self.assertRaises(subject.MySQLBackupError): backup.restore_backup(path, "mysql:existing")
                importer.assert_not_called()

    def test_native_backup_restore_rechecks_exact_content_and_separate_delivery(self):
        saved = dataset(); documents = delivery(); imports = []; versions = {}
        def publish(snapshot, target):
            imports.append(target); stored = copy.deepcopy(snapshot); stored["dataset"] = mysql_runtime.dataset_name(target); versions[target] = stored
            return {"content_sha256": stored["content_sha256"]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private.json"
            with patch.object(mysql_runtime, "connect_dataset", side_effect=lambda *a, **k: FakeNative()), \
                 patch.object(mysql_runtime, "export_dataset_snapshot", return_value=saved), \
                 patch.object(subject, "export_delivery_snapshot", return_value=documents):
                self.assertTrue(backup.create_backup("mysql:operating", path)["ok"])
            with patch.object(mysql_runtime, "dataset_exists", return_value=False), \
                 patch.object(mysql_runtime, "import_dataset_snapshot", side_effect=publish), \
                 patch.object(mysql_runtime, "export_dataset_snapshot", side_effect=lambda target: versions[target]), \
                 patch.object(mysql_runtime, "connect_dataset", side_effect=lambda *a, **k: FakeNative()), \
                 patch.object(mysql_runtime, "activate_alias", side_effect=AssertionError("No activation")):
                result = backup.restore_backup(path, "mysql:new-recovery")
            self.assertEqual(imports, ["mysql:new-recovery", subject._delivery_target("mysql:new-recovery")])
            self.assertTrue(result["native_restore_drill_performed"]); self.assertFalse(result["live_delivery_activated"])

    def test_actual_payload_hashes_missing_files_and_links_are_not_faked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); payload = root / "data/raw/capture.txt"; payload.parent.mkdir(parents=True); payload.write_bytes(b"retained public evidence")
            self.assertTrue(subject.resolve_receipts(dataset(), root)[0]["verified"])
            payload.write_bytes(b"changed")
            self.assertEqual(subject.resolve_receipts(dataset(), root)[0]["issue"], "payload_hash_mismatch")
            payload.unlink(); self.assertEqual(subject.resolve_receipts(dataset(), root)[0]["issue"], "missing_payload")
            source = root / "real"; source.write_bytes(b"retained public evidence"); payload.symlink_to(source)
            self.assertEqual(subject.resolve_receipts(dataset(), root)[0]["issue"], "symlink_payload")

    def test_statistics_manifest_and_all_source_file_bytes_are_verified(self):
        script = "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE source_files(file TEXT PRIMARY KEY,sha256 TEXT NOT NULL,source_url TEXT NOT NULL,bytes INTEGER NOT NULL);"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); capture = root / "data/raw/archive"; capture.mkdir(parents=True)
            manifest = capture / "manifest.json"; manifest.write_text('{"pinned":"archive"}')
            payload = capture / "bouts.csv"; payload.write_text("real rows")
            saved = subject._snapshot_document(script, {"metadata": [["source_manifest_sha256", json.dumps(subject._sha(manifest))]],
                "source_files": [["bouts.csv", subject._sha(payload), "https://example.com/retained", payload.stat().st_size]]}, dataset="statistics", metadata={})
            self.assertEqual(len(subject.resolve_receipts(saved, root)), 2)
            self.assertTrue(all(r["verified"] for r in subject.resolve_receipts(saved, root)))
            payload.write_text("tampered")
            self.assertIn("payload_hash_mismatch", [r.get("issue") for r in subject.resolve_receipts(saved, root)])

    def test_mysql_integrity_dispatch_checks_receipts_and_hides_driver_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); payload = root / "data/raw/capture.txt"; payload.parent.mkdir(parents=True); payload.write_bytes(b"retained public evidence")
            state = {"backend": "mysql", "dataset": "source", "content_sha256": dataset()["content_sha256"]}
            with patch.object(subject, "dataset_state", return_value=state), \
                 patch.object(mysql_runtime, "export_dataset_snapshot", return_value=dataset()), \
                 patch.object(mysql_runtime, "connect_dataset", side_effect=lambda *a, **k: FakeNative()):
                report = integrity.verify_evidence("mysql:source", project_root=root)
                self.assertTrue(report["ok"]); self.assertEqual(report["verified_receipts"], 1)
                payload.write_bytes(b"tamper")
                self.assertEqual(integrity.verify_evidence("mysql:source", project_root=root)["issues"][0]["code"], "payload_hash_mismatch")
            with patch.object(subject, "dataset_state", side_effect=Exception("password=super-secret")):
                report = integrity.verify_evidence("mysql:source", project_root=root)
            self.assertNotIn("super-secret", json.dumps(report)); self.assertFalse(report["ok"])

    def test_mysql_bundle_contains_delivery_frozen_report_and_unchanged_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); payload = root / "data/raw/capture.txt"; payload.parent.mkdir(parents=True); payload.write_bytes(b"retained public evidence")
            report = root / "output/report.json"; report.parent.mkdir(); report.write_bytes(b"frozen report")
            saved = envelope(documents=delivery(forecast_path="output/report.json")); path = root / "all-evidence.zip"
            def create(source, destination, **kwargs): subject._publish_json(saved, destination); return {"ok": True}
            with patch.object(subject, "create_backup", side_effect=create), patch("sqlite3.connect", side_effect=AssertionError("No SQLite")):
                result = evidence_bundle.create_bundle("mysql:operating", path, project_root=root, models_dir=None, reports_dir=None)
                checked = evidence_bundle.verify_bundle(path)
            self.assertEqual(result["receipt_count"], 2); self.assertEqual(checked["delivery"]["entry_count"], 1)
            with zipfile.ZipFile(path) as archive:
                self.assertIn("database.mysql.json", archive.namelist()); self.assertNotIn("database.sqlite", archive.namelist())
            with self.assertRaises(subject.MySQLBackupError): evidence_bundle.restore_bundle(path, root / "recovery")

    def test_bundle_rejects_missing_changed_or_unrecorded_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); payload = root / "data/raw/capture.txt"; payload.parent.mkdir(parents=True); payload.write_bytes(b"changed")
            def create(source, destination, **kwargs): subject._publish_json(envelope(), destination); return {"ok": True}
            with patch.object(subject, "create_backup", side_effect=create):
                with self.assertRaises(subject.MySQLBackupError): subject.create_bundle("mysql:source", root / "bad.zip", project_root=root, models_dir=None, reports_dir=None)
            self.assertFalse((root / "bad.zip").exists())
            payload.write_bytes(b"retained public evidence")
            with patch.object(subject, "create_backup", side_effect=create): subject.create_bundle("mysql:source", root / "good.zip", project_root=root, models_dir=None, reports_dir=None)
            with zipfile.ZipFile(root / "good.zip") as source, zipfile.ZipFile(root / "tampered.zip", "w") as target:
                for name in source.namelist(): target.writestr(name, b"changed" if name.startswith("payloads/") else source.read(name))
            with self.assertRaises(subject.MySQLBackupError): subject.verify_bundle(root / "tampered.zip")

    def test_bundle_recovers_files_using_index_without_mutating_database_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); payload = root / "data/raw/capture.txt"; payload.parent.mkdir(parents=True); payload.write_bytes(b"retained public evidence")
            original = dataset(); saved = envelope(snapshot=original); archive = root / "all.zip"
            def create(source, destination, **kwargs): subject._publish_json(saved, destination); return {"ok": True}
            with patch.object(subject, "create_backup", side_effect=create):
                subject.create_bundle("mysql:source", archive, project_root=root, models_dir=None, reports_dir=None)
            restored = copy.deepcopy(original); restored["dataset"] = "recovery"; output = root / "recovered"
            with patch.object(subject, "restore_backup", return_value={"delivery_recovery_target": "mysql:recovery.delivery"}) as restore, \
                 patch.object(mysql_runtime, "export_dataset_snapshot", return_value=restored):
                report = evidence_bundle.restore_bundle(archive, output, mysql_target="mysql:recovery")
            self.assertFalse(report["receipt_paths_rebound_in_database"]); restore.assert_called_once()
            self.assertEqual(restored["tables"]["ingestion_runs"]["rows"], original["tables"]["ingestion_runs"]["rows"])
            payload.unlink()
            self.assertTrue(subject.resolve_receipts(restored, output)[0]["verified"])
            index = json.loads((output / "receipt_paths.json").read_text())
            restored_payload = output / index["receipts"][0]["archive_path"]
            restored_payload.write_bytes(b"changed after recovery")
            self.assertEqual(subject.resolve_receipts(restored, output)[0]["issue"], "payload_hash_mismatch")
            self.assertEqual(restored_payload.stat().st_mode & 0o777, 0o600)
            other = copy.deepcopy(restored); other["dataset"] = "another-recovery"
            with self.assertRaises(subject.MySQLBackupError): subject.resolve_receipts(other, output)

    def test_restore_detects_replay_mismatch_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private.json"; subject._publish_json(envelope(), path)
            changed = dataset(payload=b"wrong restored bytes")
            with patch.object(mysql_runtime, "dataset_exists", return_value=False), \
                 patch.object(mysql_runtime, "import_dataset_snapshot", return_value={}) as importer, \
                 patch.object(mysql_runtime, "export_dataset_snapshot", return_value=changed), \
                 patch.object(mysql_runtime, "connect_dataset", side_effect=lambda *a, **k: FakeNative()):
                with self.assertRaises(subject.MySQLBackupError): subject.restore_backup(path, "mysql:new")
            self.assertEqual(importer.call_count, 1)

    def test_archive_traversal_and_duplicate_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unsafe.zip"
            manifest = {"format_version": 2, "storage_backend": "mysql", "files": [{"archive_path": "../escape", "role": "reports", "sha256": "0" * 64, "size_bytes": 1}]}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest)); archive.writestr("../escape", "x")
            with self.assertRaises(subject.MySQLBackupError): subject.verify_bundle(path)
            self.assertFalse((Path(folder).parent / "escape").exists())


@unittest.skipUnless(os.environ.get("UFC_MYSQL_BACKUP_INTEGRATION") == "1", "requires initialized native MySQL operating + delivery data")
class NativeMySQLBackupTests(unittest.TestCase):
    def test_real_backup_and_isolated_recovery_preserve_rows_and_delivery(self):
        target = "mysql:backupqa-" + uuid.uuid4().hex[:20]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private.mysql.json"
            created = backup.create_backup("mysql:operating", path)
            restored = backup.restore_backup(path, target)
            self.assertEqual(created["content_sha256"], mysql_runtime.export_dataset_snapshot(target)["content_sha256"])
            self.assertEqual(created["delivery"]["content_sha256"], restored["delivery"]["content_sha256"])
            self.assertTrue(restored["native_schema_check"]["ok"])
            self.assertTrue(restored["delivery_native_schema_check"]["ok"])
            self.assertFalse(restored["alias_activated"]); self.assertFalse(restored["live_delivery_activated"])


if __name__ == "__main__": unittest.main()
