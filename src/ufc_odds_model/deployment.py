"""Read-only native MySQL startup check with explicit legacy snapshot support.

Normal containers require a current operating MySQL dataset and retained
receipt bytes under mounted data/. Explicit SQLite paths are legacy migration
or evidence inputs and retain their standalone snapshot checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from .db import pending_migrations
from .integrity import verify_evidence


class DeploymentError(RuntimeError):
    """The selected dashboard snapshot cannot be served safely."""


def _absolute_path(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DeploymentError(f"{label} must be an absolute path")
    return path


def _within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(directory.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _mysql_deployment(root: Path, target: str) -> dict:
    from .mysql_runtime import RuntimeDatabaseError, connect_dataset, inspect_dataset
    connection = None
    try:
        receipt = inspect_dataset(target, verify=True)
        if receipt.get('role') != 'operating' or receipt.get('immutable'):
            raise DeploymentError('Dashboard startup requires an operating MySQL dataset, not synthetic or historical statistics')
        connection = connect_dataset(target, readonly=True)
        try:
            pending = pending_migrations(connection)
        except ValueError as exc:
            raise DeploymentError('MySQL migration history is incompatible with this image') from exc
        if pending:
            raise DeploymentError('MySQL schema is older than this image; migrate it explicitly before serving')
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise DeploymentError('MySQL dataset has broken foreign-key references')
        verified = 0
        for row in connection.execute('SELECT payload_path,sha256 FROM ingestion_runs'):
            recorded = Path(str(row['payload_path'])).expanduser()
            payload = recorded if recorded.is_absolute() else root / recorded
            if not payload.exists() and not payload.is_symlink() and payload.is_absolute():
                parts = payload.parts
                offsets = [i for i in range(len(parts) - 1) if parts[i:i + 2] == ('data', 'raw')]
                if offsets:
                    payload = root.joinpath(*parts[offsets[-1]:])
            if payload.is_symlink() or not _within(payload, root / 'data'):
                raise DeploymentError('A MySQL source receipt points outside deployment data/')
            digest = row['sha256']
            if not payload.is_file() or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise DeploymentError('A retained MySQL source payload or SHA256 receipt is missing')
            with payload.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != digest:
                raise DeploymentError('Retained MySQL source payload hash does not match its immutable receipt')
            verified += 1
        return {'status': 'ready', 'storage_backend': 'mysql', 'database_path': target,
                'resolved_dataset': receipt['dataset'], 'receipt_count': verified, 'verified_receipts': verified,
                'source_rights_checked': False, 'model_evaluation_checked': False, 'sqlite_fallback_used': False}
    except RuntimeDatabaseError as exc:
        raise DeploymentError('Native MySQL startup check failed; verify the project connection and dataset. SQLite fallback is disabled.') from exc
    except OSError as exc:
        raise DeploymentError('Could not read a retained source payload') from exc
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def check_dashboard_deployment(root_path: str | Path, database_path: str | Path = 'mysql:operating') -> dict:
    """Verify mount layout, standalone SQLite, and all retained payloads.

    This verifies source *bytes*, not source rights, result accuracy, model
    quality, or the freshness of any quote or evaluation report.
    """
    root = _absolute_path(root_path, "Deployment root")
    if root.is_symlink() or not root.is_dir():
        raise DeploymentError("Deployment root must be an existing directory, not a symlink")
    data = root / "data"
    reports = root / "reports"
    if data.is_symlink() or not data.is_dir():
        raise DeploymentError("Deployment data directory is missing or is a symlink")
    if reports.is_symlink() or not reports.is_dir():
        raise DeploymentError("Deployment reports directory is missing or is a symlink")
    from .mysql_runtime import is_mysql_target
    if is_mysql_target(database_path):
        return _mysql_deployment(root, str(database_path))
    database = _absolute_path(database_path, "Dashboard database")
    if database.is_symlink() or not database.is_file() or not _within(database, data):
        raise DeploymentError("Dashboard database must be a regular file under deployment data/")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{database}{suffix}").exists():
            raise DeploymentError("Dashboard database has a SQLite sidecar; create a standalone backup snapshot")

    integrity = verify_evidence(database, project_root=root)
    if not integrity["ok"]:
        codes = sorted({item["code"] for item in integrity["issues"]})
        raise DeploymentError("Source integrity check failed: " + ", ".join(codes))

    uri = "file:" + quote(str(database.resolve()), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            try:
                pending = pending_migrations(connection)
            except ValueError as exc:
                raise DeploymentError("Database migration history is incompatible with this image") from exc
            if pending:
                raise DeploymentError("Database schema is older than this image; migrate a copy first")
            for (recorded,) in connection.execute("SELECT payload_path FROM ingestion_runs"):
                payload = Path(str(recorded)).expanduser()
                if not payload.is_absolute():
                    payload = root / payload
                if not _within(payload, data):
                    raise DeploymentError(
                        "A source receipt points outside deployment data/; "
                        "restore an evidence bundle under data/ before serving"
                    )
    except sqlite3.Error as exc:
        raise DeploymentError("Could not inspect source receipt paths") from exc

    return {
        "status": "ready",
        "database_path": str(database),
        "receipt_count": integrity["receipt_count"],
        "verified_receipts": integrity["verified_receipts"],
        "source_rights_checked": False,
        "model_evaluation_checked": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check native MySQL and retained dashboard evidence read-only")
    parser.add_argument("--root", required=True, help="Absolute repository/deployment directory")
    parser.add_argument("--db", default='mysql:operating', help="Native MySQL operating handle; explicit legacy SQLite snapshot paths are accepted")
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        report = check_dashboard_deployment(arguments.root, arguments.db)
    except DeploymentError as exc:
        parser.exit(1, f"Dashboard deployment unavailable: {exc}\n")
    if not arguments.quiet:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
