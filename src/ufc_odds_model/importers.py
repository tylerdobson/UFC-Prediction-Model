"""Import human-reviewed CSV data or UFCStats event metadata."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date
from pathlib import Path

from . import db, ufcstats
from .pipeline import utc_now, utc_string
from .raw_snapshots import retain_snapshot


REQUIRED_CSV_COLUMNS = {
    "event_id", "event_name", "event_date", "event_status", "bout_id",
    "fighter_a_id", "fighter_a_name", "fighter_b_id", "fighter_b_name",
    "bout_status", "outcome", "winner_fighter_id",
}


def _csv_cell(row: dict[str, str | None], field: str) -> str:
    value = row.get(field)
    return value.strip() if isinstance(value, str) else ""


def _retain_csv_snapshot(payload: bytes, snapshot_dir: str | Path) -> tuple[Path, str]:
    """Keep one exact, content-addressed copy without replacing prior evidence."""
    return retain_snapshot(payload, snapshot_dir, suffix=".csv", kind="CSV")


def import_bouts_csv(
    connection: sqlite3.Connection,
    path: str | Path,
    snapshot_dir: str | Path = "data/raw/manual",
) -> int:
    """Import reviewed bouts and retain the exact CSV with an atomic DB receipt."""
    source_path = Path(path)
    raw_bytes = source_path.read_bytes()
    with io.StringIO(raw_bytes.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream)
        missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)
    if not rows:
        raise ValueError("CSV contains no bouts")
    for line, row in enumerate(rows, start=2):
        try:
            date.fromisoformat(_csv_cell(row, "event_date"))
        except ValueError as exc:
            raise ValueError(f"Line {line}: event_date must be YYYY-MM-DD") from exc
        for field in REQUIRED_CSV_COLUMNS - {"outcome", "winner_fighter_id"}:
            if not _csv_cell(row, field):
                raise ValueError(f"Line {line}: {field} is required")
        if row["fighter_a_id"] == row["fighter_b_id"]:
            raise ValueError(f"Line {line}: fighter IDs must differ")
        if row["event_status"] not in {"scheduled", "completed", "cancelled"}:
            raise ValueError(f"Line {line}: invalid event_status")
        if row["bout_status"] not in {"scheduled", "completed", "cancelled"}:
            raise ValueError(f"Line {line}: invalid bout_status")
        outcome = _csv_cell(row, "outcome")
        winner = _csv_cell(row, "winner_fighter_id")
        if row["bout_status"] == "completed":
            if outcome not in {"win", "draw", "no_contest"}:
                raise ValueError(f"Line {line}: completed bout needs a valid outcome")
            if outcome == "win" and winner not in {row["fighter_a_id"], row["fighter_b_id"]}:
                raise ValueError(f"Line {line}: winner must be one of this bout's fighters")
            if outcome != "win" and winner:
                raise ValueError(f"Line {line}: draw/no contest cannot have a winner")
        elif outcome or winner:
            raise ValueError(f"Line {line}: scheduled/cancelled bouts cannot have results")

    imported_at = utc_string(utc_now())
    with connection:
        for row in rows:
            db.upsert_fighter(connection, row["fighter_a_id"], row["fighter_a_name"].strip())
            db.upsert_fighter(connection, row["fighter_b_id"], row["fighter_b_name"].strip())
            db.upsert_event(
                connection, row["event_id"], row["event_name"], row["event_date"],
                row["event_status"], source="manual", source_event_id=row["event_id"],
                start_time_utc=_csv_cell(row, "start_time_utc") or None,
            )
            db.upsert_bout(
                connection, row["bout_id"], row["event_id"],
                row["fighter_a_id"], row["fighter_b_id"], row["bout_status"],
                weight_class=_csv_cell(row, "weight_class") or None,
                source="manual", source_bout_id=row["bout_id"],
            )
            if row["bout_status"] == "completed":
                outcome = _csv_cell(row, "outcome")
                winner = _csv_cell(row, "winner_fighter_id") or None
                method = _csv_cell(row, "method") or None
                existing = connection.execute(
                    "SELECT outcome, winner_fighter_id, method FROM results WHERE bout_id = ?",
                    (row["bout_id"],),
                ).fetchone()
                if existing is None or (existing["outcome"], existing["winner_fighter_id"], existing["method"]) != (
                    outcome, winner, method
                ):
                    db.upsert_result(
                        connection, row["bout_id"], outcome, winner, imported_at, method,
                    )
        snapshot_path, digest = _retain_csv_snapshot(raw_bytes, snapshot_dir)
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES (?, ?, ?, ?)",
            ("manual-csv", imported_at, str(snapshot_path), digest),
        )
    return len(rows)


def import_ufcstats_events(
    connection: sqlite3.Connection,
    kind: str,
    limit: int,
    raw_dir: str | Path = "data/raw/ufcstats",
) -> dict[str, int]:
    """Import a bounded list of completed or upcoming UFCStats events."""
    if kind not in {"completed", "upcoming"}:
        raise ValueError("kind must be completed or upcoming")
    if limit <= 0:
        raise ValueError("limit must be positive")
    listing = (
        ufcstats.fetch_completed_events() if kind == "completed"
        else ufcstats.fetch_upcoming_events()
    )
    imported_bouts = 0
    imported_events = 0
    for event in listing[:limit]:
        bouts = ufcstats.fetch_event_bouts(event["source_event_url"])
        if not bouts:
            continue
        snapshot = {"event": event, "bouts": bouts}
        raw_bytes = json.dumps(snapshot, indent=2, ensure_ascii=False).encode("utf-8")
        captured = utc_string(utc_now())
        path, digest = retain_snapshot(raw_bytes, raw_dir, kind="UFCStats")
        event_id = f"ufcstats:{event['source_event_id']}"
        with connection:
            db.upsert_event(
                connection, event_id, event["name"], event["date"], kind.replace("upcoming", "scheduled"),
                source="ufcstats", source_event_id=event["source_event_id"],
            )
            current_bout_ids: set[str] = set()
            for bout in bouts:
                fighter_a_id = db.resolve_fighter_id(connection, "ufcstats", bout["fighter_a_source_id"])
                fighter_b_id = db.resolve_fighter_id(connection, "ufcstats", bout["fighter_b_source_id"])
                bout_id = f"ufcstats:{bout['source_bout_id']}"
                current_bout_ids.add(bout_id)
                db.upsert_fighter(connection, fighter_a_id, bout["fighter_a_name"], "ufcstats", bout["fighter_a_source_id"])
                db.upsert_fighter(connection, fighter_b_id, bout["fighter_b_name"], "ufcstats", bout["fighter_b_source_id"])
                db.upsert_bout(
                    connection, bout_id, event_id, fighter_a_id, fighter_b_id,
                    bout["status"], weight_class=bout["weight_class"],
                    source="ufcstats", source_bout_id=bout["source_bout_id"],
                )
                if bout["outcome"]:
                    winner_id = (
                        db.resolve_fighter_id(connection, "ufcstats", bout["winner_source_id"])
                        if bout["winner_source_id"] else None
                    )
                    db.upsert_result(
                        connection, bout_id, bout["outcome"], winner_id,
                        captured, bout["method"],
                    )
            prior_scheduled = connection.execute(
                "SELECT bout_id FROM bouts WHERE event_id = ? AND status = 'scheduled'",
                (event_id,),
            ).fetchall()
            for previous in prior_scheduled:
                if previous["bout_id"] not in current_bout_ids:
                    connection.execute(
                        "UPDATE bouts SET status = 'cancelled' WHERE bout_id = ?",
                        (previous["bout_id"],),
                    )
            connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) VALUES (?, ?, ?, ?)",
                ("ufcstats-parsed", captured, str(path), digest),
            )
        imported_events += 1
        imported_bouts += len(bouts)
    return {"events": imported_events, "bouts": imported_bouts}
