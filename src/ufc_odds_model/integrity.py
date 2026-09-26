"""Read-only verification of SQLite structure and retained source payloads.

Run with ``python -m ufc_odds_model.integrity --db data/ufc.sqlite`` before
using a database for an event-day decision. This checks evidence, not model
quality or the right to use a provider's data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from .db import DEFAULT_DB


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def database_file_state(database: Path) -> dict[str, int | None]:
    """Capture both SQLite's main file and its write-ahead log.

    A WAL transaction can change the logical database while leaving the main
    file's modification time untouched until the next checkpoint.
    """
    database = database.resolve()
    main = database.stat()
    wal_path = Path(str(database) + "-wal")
    try:
        wal = wal_path.stat()
    except FileNotFoundError:
        wal = None
    # SQLite can create or touch an empty WAL while a read-only verifier opens
    # the database. An empty WAL contains no committed frames to invalidate.
    if wal is not None and wal.st_size == 0:
        wal = None
    return {
        "main_mtime_ns": main.st_mtime_ns,
        "main_size": main.st_size,
        "wal_mtime_ns": wal.st_mtime_ns if wal else None,
        "wal_size": wal.st_size if wal else None,
    }


def _read_only_connection(database: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(database.resolve()), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_evidence(
    database_path: str | Path = DEFAULT_DB,
    *,
    project_root: str | Path | None = None,
    max_issues: int = 100,
) -> dict:
    """Check the database and every ingestion payload without changing either.

    Relative receipt paths are resolved from ``project_root`` (the current
    working directory by default). ``issues`` is capped for readable output,
    while all receipts are still checked and counted.
    """
    if isinstance(max_issues, bool) or not isinstance(max_issues, int) or max_issues < 1:
        raise ValueError("max_issues must be a positive integer")
    database = Path(database_path).expanduser().resolve()
    root = Path(project_root).expanduser().resolve() if project_root else Path.cwd().resolve()
    report: dict = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "database_path": str(database),
        "project_root": str(root),
        "database_mtime_ns": None,
        "database_file_state": None,
        "status": "unavailable",
        "ok": False,
        "database_integrity": None,
        "foreign_key_violations": None,
        "missing_migrations": [],
        "receipt_count": 0,
        "verified_receipts": 0,
        "by_source": {},
        "issue_count": 0,
        "issues": [],
    }

    def issue(code: str, *, run_id: int | None = None, detail: str = "") -> None:
        report["issue_count"] += 1
        if len(report["issues"]) < max_issues:
            entry = {"code": code, "detail": detail}
            if run_id is not None:
                entry["run_id"] = run_id
            report["issues"].append(entry)

    if not database.is_file():
        issue("missing_database", detail="Database file does not exist")
        return report
    try:
        before_state = database_file_state(database)
    except OSError as exc:
        issue("database_read_failed", detail=str(exc))
        return report
    try:
        with closing(_read_only_connection(database)) as connection:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            report["database_integrity"] = integrity
            if integrity != ["ok"]:
                issue("sqlite_integrity_failed", detail="SQLite integrity_check did not return ok")
            foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
            report["foreign_key_violations"] = len(foreign_keys)
            if foreign_keys:
                issue("foreign_key_violations", detail=f"{len(foreign_keys)} broken references")

            applied = {
                str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
            }
            expected = {
                entry.name for entry in files("ufc_odds_model").joinpath("sql/migrations").iterdir()
                if entry.name.endswith(".sql")
            }
            report["missing_migrations"] = sorted(expected - applied)
            if report["missing_migrations"]:
                issue("missing_migrations", detail=", ".join(report["missing_migrations"]))

            counts: Counter[str] = Counter()
            for receipt in connection.execute(
                "SELECT run_id, source, payload_path, sha256 FROM ingestion_runs ORDER BY run_id"
            ):
                run_id = int(receipt["run_id"])
                counts[str(receipt["source"])] += 1
                report["receipt_count"] += 1
                stated = str(receipt["sha256"] or "")
                if not _SHA256.fullmatch(stated):
                    issue("invalid_receipt_hash", run_id=run_id, detail="Stored SHA-256 is invalid")
                    continue
                recorded = str(receipt["payload_path"] or "")
                if not recorded:
                    issue("missing_payload_path", run_id=run_id, detail="Receipt has no payload path")
                    continue
                try:
                    payload = Path(recorded).expanduser()
                    if not payload.is_absolute():
                        payload = root / payload
                    if payload.is_symlink():
                        issue("symlink_payload", run_id=run_id, detail=str(payload))
                        continue
                    if not payload.is_file():
                        issue("missing_payload", run_id=run_id, detail=str(payload))
                        continue
                    actual = _digest_file(payload)
                except (OSError, ValueError) as exc:
                    issue("unreadable_payload", run_id=run_id, detail=f"{recorded}: {exc}")
                    continue
                if actual != stated:
                    issue("payload_hash_mismatch", run_id=run_id, detail=str(payload))
                    continue
                report["verified_receipts"] += 1
            report["by_source"] = dict(sorted(counts.items()))
    except sqlite3.Error as exc:
        issue("database_read_failed", detail=str(exc))
        return report

    if report["receipt_count"] == 0:
        issue("no_source_receipts", detail="No saved ingestion payload can be verified")
    try:
        after_state = database_file_state(database)
    except OSError as exc:
        issue("database_changed_during_check", detail=f"Database became inaccessible: {exc}")
        report["status"] = "issues"
        return report
    report["database_mtime_ns"] = after_state["main_mtime_ns"]
    report["database_file_state"] = after_state
    if after_state != before_state:
        issue("database_changed_during_check", detail="Repeat verification after database writes stop")
    report["ok"] = report["issue_count"] == 0
    report["status"] = "verified" if report["ok"] else "issues"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify SQLite and retained UFC source payloads")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output", help="Write a timestamped JSON result for later inspection")
    arguments = parser.parse_args(argv)
    report = verify_evidence(arguments.db, project_root=arguments.project_root)
    if arguments.output:
        target = Path(arguments.output).expanduser()
        if target.resolve() == Path(arguments.db).expanduser().resolve():
            parser.error("--output must not replace the database")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
