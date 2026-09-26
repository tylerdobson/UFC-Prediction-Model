"""Import reviewed, dated fighter observations with immutable raw receipts.

The CSV's ``observed_at_utc`` must be supported by its evidence URI. It is the
time the source snapshot existed, not the time this local import was run. A
later import cannot make an observation eligible for an earlier decision unless
that earlier source snapshot can be independently verified.
"""

from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
from datetime import date
from pathlib import Path

from .pipeline import parse_utc, utc_now, utc_string
from .raw_snapshots import retain_snapshot


_COMMON = {"observed_at_utc", "source_evidence_uri"}
_PROFILE_COLUMNS = _COMMON | {"fighter_id", "birth_date", "reach_cm"}
_STAT_COLUMNS = _COMMON | {
    "bout_id", "fighter_id", "sig_strikes_landed", "sig_strikes_attempted",
}


def _rows(path: str | Path, required: set[str]) -> tuple[bytes, list[dict[str, str]]]:
    payload = Path(path).read_bytes()
    with io.StringIO(payload.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Observation CSV is missing columns: {', '.join(sorted(missing))}")
        rows = [
            {key: value.strip() if isinstance(value, str) else "" for key, value in row.items()}
            for row in reader
        ]
    if not rows:
        raise ValueError("Observation CSV contains no rows")
    return payload, rows


def _timestamp(row: dict[str, str], line: int, now) -> str:
    value = row["observed_at_utc"]
    if not value or "T" not in value:
        raise ValueError(f"Line {line}: observed_at_utc needs a full UTC timestamp")
    try:
        observed = parse_utc(value)
    except ValueError as exc:
        raise ValueError(f"Line {line}: observed_at_utc needs a valid timezone") from exc
    if observed.microsecond:
        raise ValueError(f"Line {line}: observed_at_utc must have whole-second precision")
    if observed > now:
        raise ValueError(f"Line {line}: source observation cannot be in the future")
    return utc_string(observed)


def _evidence(row: dict[str, str], line: int) -> str:
    evidence = row["source_evidence_uri"]
    if not evidence:
        raise ValueError(f"Line {line}: source_evidence_uri is required")
    return evidence


def _source(source: str) -> str:
    source = source.strip()
    if not source:
        raise ValueError("A nonempty reviewed source name is required")
    return source


def _license_uri(license_uri: str) -> str:
    license_uri = license_uri.strip()
    if not license_uri:
        raise ValueError("A source license URI is required")
    return license_uri


def import_profile_observations_csv(
    connection: sqlite3.Connection,
    path: str | Path,
    source: str,
    license_uri: str,
    snapshot_dir: str | Path = "data/raw/profile-observations",
) -> int:
    """Import optional birth/reach fields; blank cells do not erase older values."""
    source = _source(source)
    license_uri = _license_uri(license_uri)
    payload, raw_rows = _rows(path, _PROFILE_COLUMNS)
    now = utc_now()
    validated: list[tuple[str, str, str, str | None, float | None]] = []
    seen: dict[tuple[str, str], tuple[str, str | None, float | None]] = {}
    for line, row in enumerate(raw_rows, start=2):
        fighter_id = row["fighter_id"]
        if not fighter_id:
            raise ValueError(f"Line {line}: fighter_id is required")
        observed = _timestamp(row, line, now)
        evidence = _evidence(row, line)
        birth = row["birth_date"] or None
        if birth is not None:
            try:
                born = date.fromisoformat(birth)
            except ValueError as exc:
                raise ValueError(f"Line {line}: birth_date must be YYYY-MM-DD") from exc
            if born >= parse_utc(observed).date():
                raise ValueError(f"Line {line}: birth_date must precede the observation")
        reach = None
        if row["reach_cm"]:
            try:
                reach = float(row["reach_cm"])
            except ValueError as exc:
                raise ValueError(f"Line {line}: reach_cm must be numeric") from exc
            if not math.isfinite(reach) or not 100 <= reach <= 250:
                raise ValueError(f"Line {line}: reach_cm is outside the accepted 100–250 cm range")
        if birth is None and reach is None:
            raise ValueError(f"Line {line}: at least one profile value is required")
        key = (fighter_id, observed)
        value = (evidence, birth, reach)
        if key in seen and seen[key] != value:
            raise ValueError(f"Line {line}: conflicting duplicate profile observation")
        seen[key] = value
        validated.append((fighter_id, observed, evidence, birth, reach))

    inserted = 0
    with connection:
        for fighter_id, observed, evidence, birth, reach in validated:
            if connection.execute(
                "SELECT 1 FROM fighters WHERE fighter_id = ?", (fighter_id,)
            ).fetchone() is None:
                raise ValueError(f"Unknown stable fighter ID: {fighter_id}")
            old = connection.execute(
                """SELECT birth_date, reach_cm, source_evidence_uri, source_license_uri
                   FROM fighter_profile_observations
                   WHERE fighter_id = ? AND source = ? AND observed_at_utc = ?""",
                (fighter_id, source, observed),
            ).fetchone()
            if old is not None and (
                old["birth_date"], old["reach_cm"], old["source_evidence_uri"], old["source_license_uri"]
            ) != (
                birth, reach, evidence, license_uri
            ):
                raise ValueError(f"Conflicting immutable profile observation: {fighter_id} at {observed}")
        snapshot_path, digest = retain_snapshot(payload, snapshot_dir, suffix=".csv", kind="profile")
        receipt = connection.execute(
            """INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256)
               VALUES (?, ?, ?, ?)""",
            (f"reviewed-profile-csv:{source}", utc_string(now), str(snapshot_path), digest),
        )
        for fighter_id, observed, evidence, birth, reach in validated:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO fighter_profile_observations(
                       fighter_id, source, source_evidence_uri, observed_at_utc,
                       source_license_uri, birth_date, reach_cm, ingestion_run_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (fighter_id, source, evidence, observed, license_uri, birth, reach, receipt.lastrowid),
            )
            inserted += cursor.rowcount
    return inserted


def _nonnegative_int(value: str, line: int, field: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError(f"Line {line}: {field} must be a nonnegative integer")
    return int(value)


def import_fight_stat_observations_csv(
    connection: sqlite3.Connection,
    path: str | Path,
    source: str,
    license_uri: str,
    snapshot_dir: str | Path = "data/raw/fight-stat-observations",
) -> int:
    """Import per-fighter striking counts only for verified completed bouts."""
    source = _source(source)
    license_uri = _license_uri(license_uri)
    payload, raw_rows = _rows(path, _STAT_COLUMNS)
    now = utc_now()
    validated: list[tuple[str, str, str, str, int, int]] = []
    seen: dict[tuple[str, str, str], tuple[str, int, int]] = {}
    for line, row in enumerate(raw_rows, start=2):
        bout_id, fighter_id = row["bout_id"], row["fighter_id"]
        if not bout_id or not fighter_id:
            raise ValueError(f"Line {line}: bout_id and fighter_id are required")
        observed = _timestamp(row, line, now)
        evidence = _evidence(row, line)
        landed = _nonnegative_int(row["sig_strikes_landed"], line, "sig_strikes_landed")
        attempted = _nonnegative_int(row["sig_strikes_attempted"], line, "sig_strikes_attempted")
        if landed > attempted:
            raise ValueError(f"Line {line}: landed strikes cannot exceed attempts")
        key = (bout_id, fighter_id, observed)
        value = (evidence, landed, attempted)
        if key in seen and seen[key] != value:
            raise ValueError(f"Line {line}: conflicting duplicate fight-stat observation")
        seen[key] = value
        validated.append((bout_id, fighter_id, observed, evidence, landed, attempted))

    inserted = 0
    with connection:
        for bout_id, fighter_id, observed, evidence, landed, attempted in validated:
            bout = connection.execute(
                """SELECT b.fighter_a_id, b.fighter_b_id, b.status, e.event_date,
                          e.start_time_utc, r.bout_id AS has_result
                   FROM bouts b JOIN events e ON e.event_id = b.event_id
                   LEFT JOIN results r ON r.bout_id = b.bout_id
                   WHERE b.bout_id = ?""",
                (bout_id,),
            ).fetchone()
            if (bout is None or bout["status"] != "completed" or not bout["has_result"]
                    or fighter_id not in {bout["fighter_a_id"], bout["fighter_b_id"]}):
                raise ValueError(f"Stat fighter must participate in a completed result: {bout_id}/{fighter_id}")
            observed_time = parse_utc(observed)
            if bout["start_time_utc"]:
                if observed_time <= parse_utc(str(bout["start_time_utc"])):
                    raise ValueError(f"Stat observation must follow the known event start: {bout_id}")
            elif observed_time.date() <= date.fromisoformat(str(bout["event_date"])):
                raise ValueError(f"Stat observation without a known start needs a later UTC date: {bout_id}")
            old = connection.execute(
                """SELECT sig_strikes_landed, sig_strikes_attempted, source_evidence_uri,
                          source_license_uri
                   FROM fight_stat_observations
                   WHERE bout_id = ? AND fighter_id = ? AND source = ? AND observed_at_utc = ?""",
                (bout_id, fighter_id, source, observed),
            ).fetchone()
            if old is not None and (
                old["sig_strikes_landed"], old["sig_strikes_attempted"], old["source_evidence_uri"],
                old["source_license_uri"],
            ) != (landed, attempted, evidence, license_uri):
                raise ValueError(f"Conflicting immutable stat observation: {bout_id}/{fighter_id} at {observed}")
        snapshot_path, digest = retain_snapshot(payload, snapshot_dir, suffix=".csv", kind="fight-stat")
        receipt = connection.execute(
            """INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256)
               VALUES (?, ?, ?, ?)""",
            (f"reviewed-fight-stats-csv:{source}", utc_string(now), str(snapshot_path), digest),
        )
        for bout_id, fighter_id, observed, evidence, landed, attempted in validated:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO fight_stat_observations(
                       bout_id, fighter_id, source, source_evidence_uri,
                       source_license_uri, observed_at_utc, sig_strikes_landed, sig_strikes_attempted,
                       ingestion_run_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (bout_id, fighter_id, source, evidence, license_uri,
                 observed, landed, attempted, receipt.lastrowid),
            )
            inserted += cursor.rowcount
    return inserted
