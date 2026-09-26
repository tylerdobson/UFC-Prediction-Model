"""Read-only startup check for the private dashboard container.

The container mounts ``data/`` at its *host* absolute path so saved source
receipts continue to point at the same files. A single-file SQLite snapshot is
required: a live database with WAL sidecars is not a stable deployment input.
"""

from __future__ import annotations

import argparse
import json
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


def check_dashboard_deployment(root_path: str | Path, database_path: str | Path) -> dict:
    """Verify mount layout, standalone SQLite, and all retained payloads.

    This verifies source *bytes*, not source rights, result accuracy, model
    quality, or the freshness of any quote or evaluation report.
    """
    root = _absolute_path(root_path, "Deployment root")
    database = _absolute_path(database_path, "Dashboard database")
    if root.is_symlink() or not root.is_dir():
        raise DeploymentError("Deployment root must be an existing directory, not a symlink")
    data = root / "data"
    reports = root / "reports"
    if data.is_symlink() or not data.is_dir():
        raise DeploymentError("Deployment data directory is missing or is a symlink")
    if reports.is_symlink() or not reports.is_dir():
        raise DeploymentError("Deployment reports directory is missing or is a symlink")
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
    parser = argparse.ArgumentParser(description="Check a private, read-only dashboard snapshot")
    parser.add_argument("--root", required=True, help="Absolute repository/deployment directory")
    parser.add_argument("--db", required=True, help="Absolute standalone SQLite snapshot under root/data")
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
