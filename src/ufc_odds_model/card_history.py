"""Immutable, receipt-linked observations of card status and matchups.

The import clock and a source-observed clock have different meanings. A
reviewed CSV without an explicit source observation time is useful for current
identity/results, but cannot establish that a historical roster was known
before a fight. The original CSV bytes remain in the ingestion receipt.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

_PREFIGHT_PROVIDER_STATUSES = {None, "scheduled", "not_started"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _cell(row: Mapping[str, str | None], name: str) -> str:
    value = row.get(name)
    return value.strip() if isinstance(value, str) else ""


def _utc_timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def prepare_csv_card_snapshots(
    rows: Sequence[Mapping[str, str | None]], imported_at_utc: str,
) -> list[dict]:
    """Validate one observation per event in a CSV and return immutable rows.

    A CSV can contain many cards, but it cannot silently combine different
    observation times or conflicting event metadata for one card. A missing
    ``source_observed_at_utc`` remains NULL; the import clock never fills it.
    """
    imported = _utc_timestamp(imported_at_utc, "imported_at_utc")
    cards: dict[str, dict] = {}
    seen_bouts: set[str] = set()
    for line, row in enumerate(rows, start=2):
        event_id = _cell(row, "event_id")
        bout_id = _cell(row, "bout_id")
        source_text = _cell(row, "source_observed_at_utc")
        observed = (
            _utc_timestamp(source_text, f"Line {line}: source_observed_at_utc")
            if source_text else None
        )
        if observed is not None and datetime.fromisoformat(
            observed.replace("Z", "+00:00")
        ) > datetime.fromisoformat(imported.replace("Z", "+00:00")):
            raise ValueError(f"Line {line}: source_observed_at_utc is after import time")
        event_fields = {
            "event_id": event_id,
            "event_name": _cell(row, "event_name"),
            "event_date": _cell(row, "event_date"),
            "event_status": _cell(row, "event_status"),
            "event_provider_status": _cell(row, "event_provider_status") or None,
            "start_time_utc": _cell(row, "start_time_utc") or None,
            "source_observed_at_utc": observed,
            "observation_basis": "reviewed_csv" if observed else "unknown",
            "source_url": _cell(row, "source_url") or None,
            "source_revision_id": _cell(row, "source_revision_id") or None,
            "license_name": _cell(row, "license_name") or None,
            "license_url": _cell(row, "license_url") or None,
            "reviewed_by": _cell(row, "reviewed_by") or None,
        }
        card = cards.get(event_id)
        if card is None:
            card = {**event_fields, "bouts": []}
            cards[event_id] = card
        elif any(card[field] != value for field, value in event_fields.items()):
            raise ValueError(
                f"Line {line}: conflicting event metadata or source observation time for {event_id}"
            )
        if bout_id in seen_bouts:
            prior = next(
                bout for candidate in cards.values() for bout in candidate["bouts"]
                if bout["bout_id"] == bout_id
            )
            if (
                prior["event_id"] != event_id
                or prior["fighter_a_id"] != _cell(row, "fighter_a_id")
                or prior["fighter_b_id"] != _cell(row, "fighter_b_id")
            ):
                raise ValueError("A changed matchup needs a new bout_id; cancel the old bout")
            raise ValueError(f"Line {line}: duplicate bout_id {bout_id} in one CSV")
        seen_bouts.add(bout_id)
        card["bouts"].append({
            "event_id": event_id,
            "bout_id": bout_id,
            "fighter_a_id": _cell(row, "fighter_a_id"),
            "fighter_a_name": _cell(row, "fighter_a_name"),
            "fighter_b_id": _cell(row, "fighter_b_id"),
            "fighter_b_name": _cell(row, "fighter_b_name"),
            "bout_status": _cell(row, "bout_status"),
            "bout_provider_status": _cell(row, "bout_provider_status") or None,
            "weight_class": _cell(row, "weight_class") or None,
            "outcome": _cell(row, "outcome") or None,
            "winner_fighter_id": _cell(row, "winner_fighter_id") or None,
            "method": _cell(row, "method") or None,
        })
    return list(cards.values())


def record_card_snapshots(
    connection: sqlite3.Connection, ingestion_run_id: int, cards: Sequence[Mapping],
) -> None:
    """Record validated cards inside the caller's ingestion transaction."""
    for card in cards:
        cursor = connection.execute(
            """INSERT INTO card_event_snapshots(
                ingestion_run_id, event_id, source_observed_at_utc,
                observation_basis, source_url, source_revision_id,
                license_name, license_url, reviewed_by, event_name,
                event_date, start_time_utc, event_status,
                event_provider_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ingestion_run_id, card["event_id"], card["source_observed_at_utc"],
                card["observation_basis"], card["source_url"],
                card["source_revision_id"], card["license_name"],
                card["license_url"], card["reviewed_by"],
                card["event_name"], card["event_date"],
                card["start_time_utc"], card["event_status"],
                card["event_provider_status"],
            ),
        )
        event_snapshot_id = int(cursor.lastrowid)
        for bout in card["bouts"]:
            connection.execute(
                """INSERT INTO card_bout_snapshots(
                    event_snapshot_id, bout_id, fighter_a_id, fighter_a_name,
                    fighter_b_id, fighter_b_name, bout_status,
                    bout_provider_status, weight_class, outcome,
                    winner_fighter_id, method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_snapshot_id, bout["bout_id"], bout["fighter_a_id"],
                    bout["fighter_a_name"], bout["fighter_b_id"],
                    bout["fighter_b_name"], bout["bout_status"],
                    bout["bout_provider_status"], bout["weight_class"], bout["outcome"],
                    bout["winner_fighter_id"], bout["method"],
                ),
            )


def _receipt_payload_problem(path_text: str, expected_sha256: str) -> str | None:
    """Hash the selected receipt's regular file without following its leaf link."""
    if not _SHA256.fullmatch(str(expected_sha256 or "")):
        return "roster_payload_invalid_receipt"
    if not path_text:
        return "roster_payload_missing"
    path = Path(path_text).expanduser()
    # macOS commonly places temporary files below /var, which is itself a
    # link to /private/var. Reject a linked payload, but permit a trusted
    # directory alias while O_NOFOLLOW protects the final file component.
    if path.is_symlink():
        return "roster_payload_symlink"
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return "roster_payload_unreadable"
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return "roster_payload_missing"
    except OSError as exc:
        return "roster_payload_symlink" if exc.errno == errno.ELOOP else "roster_payload_unreadable"
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return None if digest.hexdigest() == expected_sha256 else "roster_payload_changed"


def latest_prefight_roster(
    connection: sqlite3.Connection, event_id: str, bout_id: str, as_of: datetime,
    max_age_seconds: float = 86_400.0,
) -> dict:
    """Check the latest imported card observation for one pre-fight decision.

    This checks timing and matchup evidence, not completeness of an entire
    provider response. Callers must separately enforce event/bout eligibility,
    quote freshness and identity review. The roster observation itself must
    be no more than ``max_age_seconds`` old (24 hours by default).
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be finite and positive")
    now = as_of.astimezone(timezone.utc)
    snapshot = connection.execute(
        """SELECT s.*, r.fetched_at_utc, r.payload_path, r.sha256
           FROM card_event_snapshots s
           JOIN ingestion_runs r ON r.run_id = s.ingestion_run_id
           WHERE s.event_id = ?
           ORDER BY r.fetched_at_utc DESC, s.event_snapshot_id DESC LIMIT 1""",
        (event_id,),
    ).fetchone()
    if snapshot is None:
        return {"accepted": False, "reason": "no_card_snapshot"}
    detail = {
        "event_snapshot_id": snapshot["event_snapshot_id"],
        "ingestion_run_id": snapshot["ingestion_run_id"],
        "source_observed_at_utc": snapshot["source_observed_at_utc"],
        "observation_basis": snapshot["observation_basis"],
        "source_url": snapshot["source_url"],
        "source_revision_id": snapshot["source_revision_id"],
        "license_name": snapshot["license_name"],
        "license_url": snapshot["license_url"],
        "reviewed_by": snapshot["reviewed_by"],
    }
    payload_problem = _receipt_payload_problem(snapshot["payload_path"], snapshot["sha256"])
    if payload_problem is not None:
        return {**detail, "accepted": False, "reason": payload_problem}
    observed_text = snapshot["source_observed_at_utc"]
    if observed_text is None:
        return {**detail, "accepted": False, "reason": "missing_roster_observation_time"}
    try:
        observed = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
        fetched = datetime.fromisoformat(snapshot["fetched_at_utc"].replace("Z", "+00:00"))
    except ValueError:
        return {**detail, "accepted": False, "reason": "invalid_roster_observation_time"}
    if observed.tzinfo is None or fetched.tzinfo is None or observed > fetched:
        return {**detail, "accepted": False, "reason": "invalid_roster_observation_time"}
    if observed > now or fetched > now:
        return {**detail, "accepted": False, "reason": "future_roster_observation"}
    if now - observed > timedelta(seconds=max_age_seconds):
        return {**detail, "accepted": False, "reason": "stale_roster_observation"}
    event = connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    bout = connection.execute("SELECT * FROM bouts WHERE bout_id = ?", (bout_id,)).fetchone()
    if event is None or bout is None or bout["event_id"] != event_id:
        return {**detail, "accepted": False, "reason": "roster_matchup_mismatch"}
    start_text = event["start_time_utc"]
    if not start_text or not snapshot["start_time_utc"]:
        return {**detail, "accepted": False, "reason": "missing_event_start"}
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        snapshot_start = datetime.fromisoformat(snapshot["start_time_utc"].replace("Z", "+00:00"))
    except ValueError:
        return {**detail, "accepted": False, "reason": "invalid_event_start"}
    if start.tzinfo is None or snapshot_start.tzinfo is None:
        return {**detail, "accepted": False, "reason": "invalid_event_start"}
    if observed >= start or now >= start:
        return {**detail, "accepted": False, "reason": "roster_observed_at_or_after_start"}
    if start != snapshot_start or event["status"] != "scheduled" or snapshot["event_status"] != "scheduled":
        return {**detail, "accepted": False, "reason": "roster_event_changed"}
    if (event["provider_status"] not in _PREFIGHT_PROVIDER_STATUSES
            or snapshot["event_provider_status"] not in _PREFIGHT_PROVIDER_STATUSES):
        return {**detail, "accepted": False, "reason": "roster_provider_status_not_prefight"}
    observed_bout = connection.execute(
        "SELECT * FROM card_bout_snapshots WHERE event_snapshot_id = ? AND bout_id = ?",
        (snapshot["event_snapshot_id"], bout_id),
    ).fetchone()
    if observed_bout is None:
        return {**detail, "accepted": False, "reason": "bout_missing_from_latest_roster"}
    if (
        bout["status"] != "scheduled" or observed_bout["bout_status"] != "scheduled"
        or bout["fighter_a_id"] != observed_bout["fighter_a_id"]
        or bout["fighter_b_id"] != observed_bout["fighter_b_id"]
    ):
        return {**detail, "accepted": False, "reason": "roster_matchup_mismatch"}
    if (bout["provider_status"] not in _PREFIGHT_PROVIDER_STATUSES
            or observed_bout["bout_provider_status"] not in _PREFIGHT_PROVIDER_STATUSES):
        return {**detail, "accepted": False, "reason": "roster_provider_status_not_prefight"}
    return {**detail, "accepted": True, "reason": "ok"}
