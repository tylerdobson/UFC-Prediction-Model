"""Fail-closed eligibility gate for local pre-fight alert candidates.

This module does not place orders or send notifications. A larger model edge
cannot repair lagged in-play metrics or a stale sportsbook line. The gate is
for pre-fight prices only; in-play use needs a separately verified live feed
and execution design.
"""

from __future__ import annotations

import math
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .card_history import latest_prefight_roster


def _utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_number(value: object) -> float | None:
    # bool is technically numeric in Python, but is never a valid price or
    # probability in a score report.
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def gate_prefight_alerts(
    rows: Iterable[Mapping[str, object]],
    *,
    snapshot_at_utc: str | datetime,
    event_start_time_utc: str | datetime,
    now_utc: str | datetime,
    max_age_seconds: float = 60,
    decimal_odds_drift: float = 0.05,
    min_edge: float = 0.03,
) -> list[dict[str, object]]:
    """Check every scored row against one freshly imported odds snapshot.

    Each returned copy includes ``alert_eligible`` (bool), ``alert_decision``
    (``alert_candidate`` or ``reject``), and a machine-readable ``alert_reason``.
    Eligible rows include conservative odds and expected profit per $1 after
    subtracting ``decimal_odds_drift`` from the displayed decimal price.

    All three reference times must include a timezone. Invalid or missing
    event, snapshot, quote, model, or bookmaker timestamps reject the row.
    ``bookmaker_updated_at_utc`` must be present in each score report row.
    The caller must pass the snapshot time returned by the immediately
    preceding successful odds import, not a time chosen from an old report.
    """
    age_limit = _finite_number(max_age_seconds)
    drift = _finite_number(decimal_odds_drift)
    edge_floor = _finite_number(min_edge)
    if age_limit is None or age_limit <= 0:
        raise ValueError("max_age_seconds must be a positive finite number")
    if drift is None or drift < 0:
        raise ValueError("decimal_odds_drift must be a nonnegative finite number")
    if edge_floor is None or edge_floor < 0:
        raise ValueError("min_edge must be a nonnegative finite number")

    snapshot = _utc(snapshot_at_utc)
    event_start = _utc(event_start_time_utc)
    now = _utc(now_utc)
    limit = timedelta(seconds=age_limit)
    checked: list[dict[str, object]] = []

    for row in rows:
        result = dict(row)
        result.update(
            alert_eligible=False,
            alert_decision="reject",
            alert_reason="",
            conservative_decimal_odds=None,
            conservative_expected_profit_per_dollar=None,
        )

        if snapshot is None or now is None or event_start is None:
            reason = "invalid_reference_time"
        elif now >= event_start:
            reason = "event_started"
        elif row.get("decision") != "candidate":
            reason = "not_model_candidate"
        elif not row.get("quote_id") or not row.get("bookmaker"):
            reason = "missing_quote"
        else:
            as_of = _utc(row.get("as_of_utc"))
            captured = _utc(row.get("quote_captured_at_utc"))
            book_updated = _utc(row.get("bookmaker_updated_at_utc"))
            if as_of is None or captured is None or book_updated is None:
                reason = "invalid_row_time"
            elif captured != snapshot:
                reason = "different_snapshot"
            elif not (book_updated <= captured <= as_of <= now < event_start):
                reason = "invalid_time_order"
            elif any(now - point > limit for point in (as_of, captured, book_updated)):
                reason = "stale_data"
            else:
                probability = _finite_number(row.get("model_selection_probability"))
                displayed_odds = _finite_number(row.get("decimal_odds"))
                if probability is None or not 0 < probability < 1:
                    reason = "invalid_probability"
                elif displayed_odds is None or displayed_odds <= 1:
                    reason = "invalid_odds"
                else:
                    conservative_odds = displayed_odds - drift
                    if conservative_odds <= 1:
                        reason = "price_drift_exhausts_odds"
                    else:
                        conservative_profit = probability * conservative_odds - 1.0
                        result["conservative_decimal_odds"] = round(conservative_odds, 4)
                        result["conservative_expected_profit_per_dollar"] = round(
                            conservative_profit, 4
                        )
                        reason = "ok" if conservative_profit >= edge_floor else "edge_below_floor"

        if reason == "ok":
            result["alert_eligible"] = True
            result["alert_decision"] = "alert_candidate"
        result["alert_reason"] = reason
        checked.append(result)
    return checked


def record_prefight_checks(
    connection: sqlite3.Connection,
    rows: Iterable[Mapping[str, object]],
    *,
    ingestion_run_id: int,
    max_age_seconds: float = 60,
    decimal_odds_drift: float = 0.05,
    min_edge: float = 0.03,
) -> list[dict[str, object]]:
    """Evaluate and store immutable checks against a real live-odds receipt.

    Scored rows are checked against saved predictions and quotes before they
    enter the gate. The receipt identifies the exact source file and snapshot;
    a caller cannot mark a raw score row eligible by adding a boolean field.
    """
    edge_floor = _finite_number(min_edge)
    if edge_floor is None or edge_floor < 0:
        raise ValueError("min_edge must be a nonnegative finite number")
    receipt = connection.execute(
        "SELECT * FROM ingestion_runs WHERE run_id = ?", (ingestion_run_id,)
    ).fetchone()
    if receipt is None or receipt["source"] != "the-odds-api" or not receipt["snapshot_at_utc"]:
        raise ValueError("A live odds ingestion receipt with a snapshot time is required")
    latest = connection.execute(
        "SELECT MAX(run_id) FROM ingestion_runs WHERE source = 'the-odds-api'"
    ).fetchone()[0]
    if latest != ingestion_run_id:
        raise ValueError("A newer live odds import superseded this snapshot")
    path = Path(receipt["payload_path"])
    try:
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("The live odds source snapshot is missing or unreadable") from exc
    if actual_hash != receipt["sha256"]:
        raise ValueError("The live odds source snapshot is missing or changed")

    snapshot = _utc(receipt["snapshot_at_utc"])
    fetched = _utc(receipt["fetched_at_utc"])
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if snapshot is None or fetched is None or not snapshot <= fetched <= now:
        raise ValueError("The live odds receipt has invalid timestamp order")

    source_rows = list(rows)
    if not source_rows:
        return []
    event_ids = {str(row.get("event_id") or "") for row in source_rows}
    if len(event_ids) != 1 or "" in event_ids:
        raise ValueError("One known event is required per gate run")
    event_id = next(iter(event_ids))
    event = connection.execute(
        "SELECT start_time_utc, status, provider_status FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None or event["status"] != "scheduled" or event["provider_status"] not in (
        None, "scheduled", "not_started"
    ):
        raise ValueError("A scheduled event is required for a pre-fight gate run")
    if not event["start_time_utc"]:
        raise ValueError("A known event start is required for a pre-fight gate run")

    for row in source_rows:
        _verify_saved_score(connection, row, edge_floor)
    checked = gate_prefight_alerts(
        source_rows,
        snapshot_at_utc=receipt["snapshot_at_utc"],
        event_start_time_utc=event["start_time_utc"],
        now_utc=now,
        max_age_seconds=max_age_seconds,
        decimal_odds_drift=decimal_odds_drift,
        min_edge=min_edge,
    )
    for row in checked:
        roster = latest_prefight_roster(connection, event_id, str(row["bout_id"]), now)
        row["roster_snapshot_id"] = roster.get("event_snapshot_id") if roster["accepted"] else None
        if not roster["accepted"]:
            row["alert_eligible"] = False
            row["alert_decision"] = "reject"
            row["alert_reason"] = str(roster["reason"])
    checked_at = now.isoformat().replace("+00:00", "Z")
    connection.execute("SAVEPOINT record_prefight_checks")
    try:
        for row in checked:
            def optional_number(key: str) -> float | None:
                return _finite_number(row.get(key))

            quote_id = int(row["quote_id"]) if row.get("quote_id") else None
            cursor = connection.execute(
                """
                INSERT INTO prefight_gate_checks(
                    ingestion_run_id, event_id, bout_id, prediction_id, quote_id,
                    snapshot_at_utc, checked_at_utc, event_start_time_utc,
                    model_version, model_cutoff_at_utc, bookmaker,
                    bookmaker_updated_at_utc, quote_captured_at_utc,
                    quoted_decimal_odds, model_probability, model_decision,
                    gate_decision, gate_reason, max_age_seconds,
                    decimal_odds_drift, min_edge, conservative_decimal_odds,
                    conservative_expected_profit_per_dollar, roster_snapshot_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingestion_run_id, event_id, row["bout_id"], int(row["prediction_id"]),
                    quote_id, receipt["snapshot_at_utc"], checked_at,
                    event["start_time_utc"], row["model_version"], row["as_of_utc"],
                    row.get("bookmaker") or None,
                    row.get("bookmaker_updated_at_utc") or None,
                    row.get("quote_captured_at_utc") or None,
                    optional_number("decimal_odds"), optional_number("model_selection_probability"),
                    row["decision"], row["alert_decision"], row["alert_reason"],
                    max_age_seconds, decimal_odds_drift, min_edge,
                    row["conservative_decimal_odds"],
                    row["conservative_expected_profit_per_dollar"],
                    row["roster_snapshot_id"],
                ),
            )
            row["gate_check_id"] = int(cursor.lastrowid)
            row["alert_checked_at_utc"] = checked_at
            row["snapshot_at_utc"] = receipt["snapshot_at_utc"]
        connection.execute("RELEASE SAVEPOINT record_prefight_checks")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT record_prefight_checks")
        connection.execute("RELEASE SAVEPOINT record_prefight_checks")
        raise
    connection.commit()
    return checked


def _verify_saved_score(
    connection: sqlite3.Connection, row: Mapping[str, object], min_edge: float
) -> None:
    try:
        prediction_id = int(row["prediction_id"])
        bout_id = str(row["bout_id"])
        event_id = str(row["event_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("A scored row needs saved event, bout, and prediction IDs") from exc
    saved = connection.execute(
        """
        SELECT p.bout_id, p.feature_cutoff_at_utc, p.model_version, p.p_fighter_a,
               b.event_id, b.fighter_a_id, b.fighter_b_id, b.status AS bout_status,
               b.provider_status AS bout_provider_status
        FROM predictions p JOIN bouts b ON b.bout_id = p.bout_id
        WHERE p.prediction_id = ?
        """,
        (prediction_id,),
    ).fetchone()
    if (saved is None or saved["bout_id"] != bout_id or saved["event_id"] != event_id
            or saved["bout_status"] != "scheduled" or saved["bout_provider_status"] not in (
                None, "scheduled", "not_started"
            )):
        raise ValueError("The scored prediction does not match a scheduled bout")
    if row.get("as_of_utc") != saved["feature_cutoff_at_utc"] or row.get("model_version") != saved["model_version"]:
        raise ValueError("The scored model version or cutoff differs from the saved prediction")
    quote_id = row.get("quote_id")
    if not quote_id:
        if row.get("decision") != "no_quote":
            raise ValueError("A scored candidate needs a saved quote")
        return
    try:
        quote_id = int(quote_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("The scored quote ID is invalid") from exc
    quote = connection.execute("SELECT * FROM odds_quotes WHERE quote_id = ?", (quote_id,)).fetchone()
    if quote is None or quote["bout_id"] != bout_id or quote["market"] != "h2h":
        raise ValueError("The scored quote does not match the saved bout")
    if quote["selection_fighter_id"] == saved["fighter_a_id"]:
        probability = float(saved["p_fighter_a"])
    elif quote["selection_fighter_id"] == saved["fighter_b_id"]:
        probability = 1.0 - float(saved["p_fighter_a"])
    else:
        raise ValueError("The scored quote selection is not in the bout")
    price = _finite_number(row.get("decimal_odds"))
    scored_probability = _finite_number(row.get("model_selection_probability"))
    if (row.get("bookmaker") != quote["bookmaker"]
            or row.get("quote_captured_at_utc") != quote["captured_at_utc"]
            or (row.get("bookmaker_updated_at_utc") or None) != quote["bookmaker_updated_at_utc"]
            or price is None or not math.isclose(price, quote["decimal_odds"], rel_tol=1e-12)
            or scored_probability is None
            or not math.isclose(scored_probability, probability, rel_tol=1e-12, abs_tol=1e-12)):
        raise ValueError("The scored probability, price, or quote times differ from saved evidence")
    expected_decision = "candidate" if probability * price - 1 >= min_edge else "pass"
    if row.get("decision") != expected_decision:
        raise ValueError("The scored model decision differs from saved evidence")
