"""Create and prove a restorable, single-file SQLite backup.

SQLite's backup API reads a consistent snapshot, including committed WAL pages.
The source is opened read-only. A backup or restore is first written to a
private temporary file, checked, and published with an exclusive hard link so
an existing file can never be replaced by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .db import DEFAULT_DB


class BackupError(RuntimeError):
    """The database could not be copied or verified safely."""


def _existing_database(path: str | Path) -> Path:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise BackupError(f"SQLite database does not exist: {database}")
    return database


def _new_destination(path: str | Path, source: Path) -> Path:
    destination = Path(path).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Destination already exists: {destination}")
    if destination.resolve() == source:
        raise BackupError("Source and destination must be different files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _standalone_backup(path: str | Path) -> Path:
    backup = _existing_database(path)
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{backup}{suffix}").exists():
            raise BackupError(
                f"Backup has a {suffix} sidecar; supply a standalone SQLite backup file"
            )
    return backup


def _read_only(database: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _copy_database(source: Path, destination: Path, *, timeout_seconds: float) -> None:
    """Copy committed SQLite pages, including WAL content, to a new file."""
    deadline = time.monotonic() + timeout_seconds

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise BackupError("SQLite backup exceeded its time limit")

    with closing(_read_only(source)) as origin:
        with closing(sqlite3.connect(destination, timeout=5)) as target:
            origin.backup(target, pages=256, progress=progress, sleep=0.25)
            # Publish one standalone file even when the source uses WAL.
            mode = target.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise BackupError(f"Could not normalize backup journal mode: {mode}")
            target.commit()
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{destination}{suffix}").exists():
            raise BackupError(f"Temporary backup has a SQLite sidecar: {suffix}")


def _snapshot(database: Path) -> dict:
    """Read the exact schema and every application's table count."""
    with closing(_read_only(database)) as connection:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise BackupError(f"SQLite integrity_check failed: {integrity[:3]}")
        foreign_key_violations = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_violations:
            raise BackupError(
                f"SQLite foreign_key_check found {len(foreign_key_violations)} violation(s)"
            )
        schema = [
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
        tables = [str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        if "schema_migrations" not in tables:
            raise BackupError("Database has no application migration table")
        table_counts = {}
        for table in tables:
            identifier = '"' + table.replace('"', '""') + '"'
            table_counts[table] = int(connection.execute(
                f"SELECT COUNT(*) FROM {identifier}"
            ).fetchone()[0])
        encoded_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
        return {
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "schema_sha256": hashlib.sha256(encoded_schema).hexdigest(),
            "table_counts": table_counts,
        }


def _verify_restore(database: Path, *, timeout_seconds: float) -> dict:
    original = _snapshot(database)
    with tempfile.TemporaryDirectory(prefix="ufc-restore-drill-") as folder:
        restored = Path(folder) / "restored.sqlite"
        _copy_database(database, restored, timeout_seconds=timeout_seconds)
        replay = _snapshot(restored)
    if replay["schema_sha256"] != original["schema_sha256"]:
        raise BackupError("Restored database schema differs from the backup")
    if replay["table_counts"] != original["table_counts"]:
        raise BackupError("Restored database table counts differ from the backup")
    return original


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _publish(source: Path, destination: Path) -> None:
    # The temporary file is in destination's directory. link() is atomic and
    # fails if destination exists, including an existing symlink.
    _sync_file(source)
    os.link(source, destination)
    try:
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Some filesystems cannot fsync directories; the SQLite file itself
        # was synced, and a successful link is still exclusive.
        pass


def _temporary_destination(parent: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".ufc-sqlite-", suffix=".sqlite", dir=parent)
    os.close(descriptor)
    return Path(name)


def _cleanup_temporary(path: Path) -> None:
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))):
        candidate.unlink(missing_ok=True)


def _validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")


def create_backup(
    source_database: str | Path = DEFAULT_DB,
    backup_path: str | Path = "backups/ufc.sqlite",
    *,
    timeout_seconds: float = 60,
) -> dict:
    """Make a unique SQLite backup, drill a restore, then publish it.

    A previously existing destination is never changed. Use a unique dated
    path for every invocation. The source remains read-only throughout.
    """
    _validate_timeout(timeout_seconds)
    source = _existing_database(source_database)
    destination = _new_destination(backup_path, source)
    temporary = _temporary_destination(destination.parent)
    try:
        _copy_database(source, temporary, timeout_seconds=timeout_seconds)
        checked = _verify_restore(temporary, timeout_seconds=timeout_seconds)
        _publish(temporary, destination)
        return {
            "ok": True,
            "operation": "create_backup",
            "source_database": str(source),
            "backup_path": str(destination),
            "backup_sha256": _digest_file(destination),
            "backup_size_bytes": destination.stat().st_size,
            "verified_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **checked,
            "temporary_restore_matches": True,
        }
    finally:
        _cleanup_temporary(temporary)


def verify_backup(backup_path: str | Path, *, timeout_seconds: float = 60) -> dict:
    """Prove that an existing backup restores without changing the backup."""
    _validate_timeout(timeout_seconds)
    backup = _standalone_backup(backup_path)
    checked = _verify_restore(backup, timeout_seconds=timeout_seconds)
    return {
        "ok": True,
        "operation": "verify_backup",
        "backup_path": str(backup),
        "backup_sha256": _digest_file(backup),
        "backup_size_bytes": backup.stat().st_size,
        "verified_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **checked,
        "temporary_restore_matches": True,
    }


def restore_backup(
    backup_path: str | Path,
    restore_path: str | Path,
    *,
    timeout_seconds: float = 60,
) -> dict:
    """Restore to a new path, validate it, and refuse to replace any file."""
    _validate_timeout(timeout_seconds)
    backup = _standalone_backup(backup_path)
    destination = _new_destination(restore_path, backup)
    original = _snapshot(backup)
    temporary = _temporary_destination(destination.parent)
    try:
        _copy_database(backup, temporary, timeout_seconds=timeout_seconds)
        checked = _snapshot(temporary)
        if checked["schema_sha256"] != original["schema_sha256"]:
            raise BackupError("Restored database schema differs from the backup")
        if checked["table_counts"] != original["table_counts"]:
            raise BackupError("Restored database table counts differ from the backup")
        _publish(temporary, destination)
        return {
            "ok": True,
            "operation": "restore_backup",
            "backup_path": str(backup),
            "restored_database": str(destination),
            "restored_sha256": _digest_file(destination),
            "verified_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **checked,
            "backup_counts_match": True,
        }
    finally:
        _cleanup_temporary(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and drill SQLite backups safely")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create and verify a new backup")
    create.add_argument("--db", default=str(DEFAULT_DB))
    create.add_argument("--output", required=True)
    verify = commands.add_parser("verify", help="Drill a restore of an existing backup")
    verify.add_argument("--backup", required=True)
    restore = commands.add_parser("restore", help="Restore to a new path")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            report = create_backup(args.db, args.output)
        elif args.command == "verify":
            report = verify_backup(args.backup)
        else:
            report = restore_backup(args.backup, args.output)
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"ok": False, "operation": args.command, "error": str(exc)}))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
