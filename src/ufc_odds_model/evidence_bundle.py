"""Portable, verified archive of a SQLite snapshot and its decision evidence.

The original database remains byte-for-byte inside the archive. Recovery makes
a new database copy and rebinds receipt paths to the extracted payloads. That
exceptional rebinding is recorded in the recovery report, never applied to an
operating database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .backup import BackupError, create_backup, verify_backup
from .db import DEFAULT_DB
from .integrity import verify_evidence


class BundleError(RuntimeError):
    """An evidence archive is incomplete, changed, or unsafe to recover."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: str) -> bool:
    return bool(value) and "\\" not in value and all(
        part not in {"", ".", ".."} for part in value.split("/")
    )


def _write_file(archive: zipfile.ZipFile, source: Path, name: str, role: str,
                *, run_id: int | None = None, expected_sha256: str | None = None) -> dict:
    if source.is_symlink() or not source.is_file():
        raise BundleError(f"Evidence file is missing or a symlink: {source}")
    if not _safe_name(name):
        raise BundleError(f"Unsafe archive name: {name}")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_stream, archive.open(name, "w") as output_stream:
        for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            output_stream.write(chunk)
    actual = digest.hexdigest()
    if expected_sha256 and actual != expected_sha256:
        raise BundleError(f"Receipt payload changed: run {run_id}")
    entry = {"archive_path": name, "role": role, "sha256": actual, "size_bytes": size}
    if run_id is not None:
        entry["run_id"] = run_id
    return entry


def _optional_files(path: str | Path | None, label: str) -> list[tuple[Path, str]]:
    if path is None:
        return []
    directory = Path(path).expanduser()
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise BundleError(f"{label} must be a real directory: {directory}")
    files = []
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            raise BundleError(f"Symlink in {label}: {item}")
        if item.is_file():
            if item.name in {".env", "secrets.toml"}:
                raise BundleError(f"Credential-like file in {label}: {item}")
            relative = item.relative_to(directory).as_posix()
            files.append((item, f"{label}/{relative}"))
    return files


def create_bundle(
    database_path: str | Path = DEFAULT_DB,
    output_path: str | Path = "backups/ufc-evidence.zip",
    *,
    project_root: str | Path | None = None,
    models_dir: str | Path | None = "models",
    reports_dir: str | Path | None = "reports",
) -> dict:
    """Archive a consistent database snapshot, every receipt payload, and artifacts."""
    source = Path(database_path).expanduser().resolve()
    root = Path(project_root).expanduser().resolve() if project_root else Path.cwd().resolve()
    destination = Path(output_path).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Bundle already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ufc-bundle-", dir=destination.parent) as workspace:
        temporary = Path(workspace)
        database_copy = temporary / "database.sqlite"
        create_backup(source, database_copy)
        archive_path = temporary / "bundle.zip"
        entries: list[dict] = []
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            entries.append(_write_file(archive, database_copy, "database.sqlite", "database"))
            with closing(sqlite3.connect(database_copy)) as connection:
                receipts = connection.execute(
                    "SELECT run_id, payload_path, sha256 FROM ingestion_runs ORDER BY run_id"
                ).fetchall()
            for run_id, recorded_path, expected_hash in receipts:
                payload = Path(recorded_path).expanduser()
                if not payload.is_absolute():
                    payload = root / payload
                name = f"payloads/run-{int(run_id):08d}"
                entries.append(_write_file(archive, payload, name, "payload",
                                           run_id=int(run_id), expected_sha256=str(expected_hash)))
            for label, directory in (("models", models_dir), ("reports", reports_dir)):
                resolved_directory = None
                if directory is not None:
                    resolved_directory = Path(directory).expanduser()
                    if not resolved_directory.is_absolute():
                        resolved_directory = root / resolved_directory
                for path, name in _optional_files(resolved_directory, label):
                    entries.append(_write_file(archive, path, name, label))
            manifest = {"format_version": 1, "created_at_utc": _utc_now(),
                        "files": entries}
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        checked = verify_bundle(archive_path)
        # The source and destination are on the same filesystem; this publish
        # refuses an existing file even if another process created it meanwhile.
        archive_path.chmod(0o600)
        os.link(archive_path, destination)
        return {**checked, "operation": "create_bundle", "bundle_path": str(destination),
                "bundle_sha256": _sha256(destination)}


def _manifest(archive: zipfile.ZipFile) -> dict:
    members = archive.namelist()
    if len(members) != len(set(members)) or "manifest.json" not in members:
        raise BundleError("Duplicate archive entries or missing manifest")
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise BundleError("Invalid bundle manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise BundleError("Unsupported bundle format")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise BundleError("Bundle manifest has no file list")
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BundleError("Invalid bundle file entry")
        name, role = entry.get("archive_path"), entry.get("role")
        if not isinstance(name, str) or not _safe_name(name):
            raise BundleError("Unsafe archive path")
        if role not in {"database", "payload", "models", "reports"}:
            raise BundleError("Unknown bundle file role")
        if role == "database" and name != "database.sqlite":
            raise BundleError("Invalid database entry")
        if role == "payload" and not name.startswith("payloads/run-"):
            raise BundleError("Invalid payload entry")
        if role in {"models", "reports"} and not name.startswith(f"{role}/"):
            raise BundleError("Invalid artifact entry")
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise BundleError("Invalid file digest")
        if (isinstance(entry.get("size_bytes"), bool)
                or not isinstance(entry.get("size_bytes"), int)
                or entry["size_bytes"] < 0):
            raise BundleError("Invalid file size")
        names.append(name)
    if len(names) != len(set(names)) or sorted(members) != sorted([*names, "manifest.json"]):
        raise BundleError("Manifest does not match archive contents")
    if names.count("database.sqlite") != 1:
        raise BundleError("Bundle must have exactly one database")
    return manifest


def _copy_entry(archive: zipfile.ZipFile, name: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(name) as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def verify_bundle(bundle_path: str | Path) -> dict:
    """Check archive structure, bytes, SQLite restore, and receipt completeness."""
    requested = Path(bundle_path).expanduser()
    bundle = requested.resolve()
    if requested.is_symlink() or not bundle.is_file():
        raise BundleError(f"Bundle is missing or a symlink: {bundle}")
    with zipfile.ZipFile(bundle) as archive, tempfile.TemporaryDirectory(
        prefix="ufc-bundle-verify-"
    ) as workspace:
        manifest = _manifest(archive)
        for entry in manifest["files"]:
            digest = hashlib.sha256()
            size = 0
            with archive.open(entry["archive_path"]) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != entry["sha256"] or size != entry["size_bytes"]:
                raise BundleError(f"Archive file changed: {entry['archive_path']}")
        database = Path(workspace) / "database.sqlite"
        _copy_entry(archive, "database.sqlite", database)
        db_report = verify_backup(database)
        with closing(sqlite3.connect(database)) as connection:
            receipts = {
                int(row[0]): str(row[1]) for row in connection.execute(
                    "SELECT run_id, sha256 FROM ingestion_runs"
                )
            }
        archived = {int(entry["run_id"]): entry["sha256"] for entry in manifest["files"]
                    if entry["role"] == "payload" and isinstance(entry.get("run_id"), int)}
        if archived != receipts or len(archived) != sum(
            entry["role"] == "payload" for entry in manifest["files"]
        ):
            raise BundleError("Receipt payloads do not match the archived database")
        return {"ok": True, "operation": "verify_bundle", "bundle_path": str(bundle),
                "bundle_sha256": _sha256(bundle), "verified_at_utc": _utc_now(),
                "receipt_count": len(receipts), "artifact_count": sum(
                    entry["role"] in {"models", "reports"} for entry in manifest["files"]
                ), "table_counts": db_report["table_counts"]}


def restore_bundle(bundle_path: str | Path, output_directory: str | Path) -> dict:
    """Recover into a new directory and rebase receipt paths in its DB copy."""
    bundle = Path(bundle_path).expanduser().resolve()
    if not bundle.is_file() or Path(bundle_path).expanduser().is_symlink():
        raise BundleError(f"Bundle is missing or a symlink: {bundle}")
    destination = Path(output_directory).expanduser().absolute()
    # Work from one private copy throughout verification and extraction. A
    # source archive replaced between those phases cannot change recovered
    # bytes after its checksum has been reported.
    with tempfile.TemporaryDirectory(prefix="ufc-bundle-restore-") as workspace:
        stable = Path(workspace) / "evidence.zip"
        shutil.copyfile(bundle, stable)
        verified = verify_bundle(stable)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700, exist_ok=False)
        try:
            with zipfile.ZipFile(stable) as archive:
                manifest = _manifest(archive)
                for entry in manifest["files"]:
                    _copy_entry(archive, entry["archive_path"], destination / entry["archive_path"])
            database = destination / "database.sqlite"
            payloads = [entry for entry in manifest["files"] if entry["role"] == "payload"]
            rebindings = []
            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                trigger_row = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
                    "AND name = 'ingestion_runs_no_update'"
                ).fetchone()
                has_immutable_migration = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = '005_prefight_gate.sql'"
                ).fetchone() is not None
                if has_immutable_migration and (trigger_row is None or not trigger_row["sql"]):
                    raise BundleError("Receipt immutability trigger is missing")
                trigger_sql = trigger_row["sql"] if trigger_row else None
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if trigger_sql:
                        connection.execute("DROP TRIGGER ingestion_runs_no_update")
                    for entry in payloads:
                        row = connection.execute(
                            "SELECT payload_path FROM ingestion_runs WHERE run_id = ?",
                            (entry["run_id"],),
                        ).fetchone()
                        if row is None:
                            raise BundleError("Archived receipt disappeared")
                        new_path = str((destination / entry["archive_path"]).resolve())
                        connection.execute("UPDATE ingestion_runs SET payload_path = ? WHERE run_id = ?",
                                           (new_path, entry["run_id"]))
                        rebindings.append({"run_id": entry["run_id"],
                                           "original_path": row["payload_path"],
                                           "restored_path": new_path})
                    if trigger_sql:
                        connection.execute(trigger_sql)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            structural = verify_backup(database)
            if structural["table_counts"] != verified["table_counts"]:
                raise BundleError("Recovery changed table counts")
            if payloads:
                evidence = verify_evidence(database, project_root=destination)
                receipt_issues = [issue for issue in evidence["issues"]
                                  if issue["code"] != "missing_migrations"]
                if receipt_issues:
                    raise BundleError(f"Restored receipt verification failed: {receipt_issues[:3]}")
            report = {"ok": True, "operation": "restore_bundle", "bundle_path": str(bundle),
                      "bundle_sha256": verified["bundle_sha256"],
                      "restored_database": str(database), "restored_at_utc": _utc_now(),
                      "receipt_count": len(payloads), "rebindings": rebindings,
                      "table_counts": structural["table_counts"]}
            (destination / "restore_report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return report
        except Exception:
            shutil.rmtree(destination)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive and recover portable UFC decision evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--db", default=str(DEFAULT_DB))
    create.add_argument("--output", required=True)
    create.add_argument("--project-root", default=".")
    create.add_argument("--models", default="models")
    create.add_argument("--reports", default="reports")
    verify = commands.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--bundle", required=True)
    restore.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_bundle(args.db, args.output, project_root=args.project_root,
                                   models_dir=args.models, reports_dir=args.reports)
        elif args.command == "verify":
            result = verify_bundle(args.bundle)
        else:
            result = restore_bundle(args.bundle, args.output)
    except (BackupError, BundleError, FileExistsError, OSError, sqlite3.Error,
            zipfile.BadZipFile, ValueError) as exc:
        print(json.dumps({"ok": False, "operation": args.command, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
