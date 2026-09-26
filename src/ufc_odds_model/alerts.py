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

from . import db
from .card_history import latest_prefight_roster
from .quote_evidence import linked_quote_rows, quote_receipt_matches


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


def _isolated_demo_database(connection: sqlite3.Connection, event_id: str) -> bool:
    """Keep the fictional rehearsal usable without relaxing an operating DB."""
    event = connection.execute(
        "SELECT source FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if event is None or event["source"] != "demo":
        return False
    return all(connection.execute(
        f"SELECT 1 FROM {table} WHERE COALESCE(source, '') != 'demo' LIMIT 1"
    ).fetchone() is None for table in ("events", "bouts", "fighters"))


def _ready_model_probabilities(
    connection: sqlite3.Connection, event_id: str, model_version: str,
    cutoff_text: str,
) -> tuple[bool, dict[str, float] | None]:
    """Check operating history and reproduce the model behind a live gate.

    ``None`` probabilities on success is reserved for an isolated, visibly
    fictional demo database. Operating rows must have enough point-in-time
    history for a chronological holdout, and *every* result used by live Elo
    must match a retained completed-card observation available at its cutoff.
    A passed readiness check is a sample/provenance floor, not a betting edge.
    """
    if _isolated_demo_database(connection, event_id):
        return True, None
    if connection.execute(
        "SELECT 1 FROM events WHERE source = 'wikipedia_research' LIMIT 1"
    ).fetchone() is not None:
        return False, None
    cutoff = _utc(cutoff_text)
    event = connection.execute(
        "SELECT event_date FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if cutoff is None or event is None:
        return False, None
    history_before = min(str(event["event_date"]), cutoff.date().isoformat())
    # The fixed event-date rule excludes same-day results. An implausible
    # completed event on/after that date must not enter the holdout instead.
    if connection.execute(
        "SELECT 1 FROM events WHERE status = 'completed' AND event_date >= ? LIMIT 1",
        (history_before,),
    ).fetchone() is not None:
        return False, None

    history = db.prior_results(connection, history_before)
    binary = [row for row in history if row["outcome"] == "win"]
    if len(binary) < 100 or len({str(row["event_date"]) for row in binary}) < 10:
        return False, None
    # This set covers precisely the rows that Elo updates from (wins/draws).
    # Missing or changed completed-card evidence cannot be hidden by a large
    # valid holdout elsewhere in the same database.
    from .features import _observed_result_ids

    used_ids = {str(row["bout_id"]) for row in history if row["outcome"] != "no_contest"}
    observed_ids = _observed_result_ids(
        connection, history_before, cutoff.isoformat(), {}
    )
    if not used_ids or not used_ids.issubset(observed_ids):
        return False, None

    from .evaluation import evaluate_models

    try:
        holdout = evaluate_models(connection)
    except (TypeError, ValueError):
        return False, None
    if holdout.get("status") != "ok" or not holdout.get("calibration", {}).get("applied"):
        return False, None
    if not holdout.get("prior_result_evidence", {}).get("promotion_eligible"):
        return False, None
    splits = holdout.get("split") or {}
    if any((splits.get(name) or {}).get("bouts", 0) < minimum
           for name, minimum in (("train", 50), ("validation", 30), ("test", 30))):
        return False, None
    if any((splits.get(name) or {}).get("events", 0) < minimum
           for name, minimum in (("train", 5), ("validation", 2), ("test", 2))):
        return False, None

    from .elo import MODEL_VERSION as ELO_MODEL_VERSION, model_from_results
    from .logistic import MODEL_VERSION as LOGISTIC_MODEL_VERSION

    if model_version == ELO_MODEL_VERSION:
        model = model_from_results(history)
        bouts = db.event_bouts(connection, event_id)
        return True, {
            str(bout["bout_id"]): model.probability(
                str(bout["fighter_a_id"]), str(bout["fighter_b_id"])
            ) for bout in bouts
        }
    if model_version.startswith(f"{LOGISTIC_MODEL_VERSION}:"):
        from .live_logistic import prepare_live_logistic

        try:
            run = prepare_live_logistic(connection, event_id, cutoff)
        except (TypeError, ValueError):
            return False, None
        return (run.model_version == model_version,
                run.predictions if run.model_version == model_version else None)
    return False, None


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

    quote_receipt_cache: dict = {}
    verified_quotes = []
    for row in source_rows:
        verified_quotes.append(_verify_saved_score(
            connection, row, edge_floor, ingestion_run_id, now,
            quote_receipt_cache,
        ))
    checked = gate_prefight_alerts(
        source_rows,
        snapshot_at_utc=receipt["snapshot_at_utc"],
        event_start_time_utc=event["start_time_utc"],
        now_utc=now,
        max_age_seconds=max_age_seconds,
        decimal_odds_drift=decimal_odds_drift,
        min_edge=min_edge,
    )
    readiness_cache: dict[tuple[str, str], tuple[bool, dict[str, float] | None]] = {}
    for row, verified_quote in zip(checked, verified_quotes):
        if row["alert_eligible"] and not verified_quote:
            row["alert_eligible"] = False
            row["alert_decision"] = "reject"
            row["alert_reason"] = "quote_source_unverified"
        roster = latest_prefight_roster(connection, event_id, str(row["bout_id"]), now)
        row["roster_snapshot_id"] = roster.get("event_snapshot_id") if roster["accepted"] else None
        if not roster["accepted"]:
            row["alert_eligible"] = False
            row["alert_decision"] = "reject"
            row["alert_reason"] = str(roster["reason"])
        # A current, source-verified quote still needs a validated model even
        # when its nominal EV is below the candidate threshold. Preserve
        # stale/mismatched quote and roster reasons ahead of model readiness.
        market_probe = gate_prefight_alerts(
            [{**row, "decision": "candidate"}],
            snapshot_at_utc=receipt["snapshot_at_utc"],
            event_start_time_utc=event["start_time_utc"],
            now_utc=now,
            max_age_seconds=max_age_seconds,
            decimal_odds_drift=decimal_odds_drift,
            min_edge=min_edge,
        )[0]
        if (verified_quote and roster["accepted"]
                and row.get("quote_id") and row["decision"] in ("candidate", "pass")
                and market_probe["alert_reason"] in ("ok", "edge_below_floor")):
            key = (str(row["model_version"]), str(row["as_of_utc"]))
            if key not in readiness_cache:
                readiness_cache[key] = _ready_model_probabilities(
                    connection, event_id, key[0], key[1]
                )
            ready, expected = readiness_cache[key]
            if ready and expected is not None:
                prediction = connection.execute(
                    "SELECT p_fighter_a FROM predictions WHERE prediction_id = ?",
                    (int(row["prediction_id"]),),
                ).fetchone()
                probability = expected.get(str(row["bout_id"]))
                ready = bool(
                    prediction is not None and probability is not None
                    and math.isclose(float(prediction["p_fighter_a"]), probability,
                                     rel_tol=1e-12, abs_tol=1e-12)
                )
            if not ready:
                row["alert_eligible"] = False
                row["alert_decision"] = "reject"
                row["alert_reason"] = "model_not_validated"
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
    connection: sqlite3.Connection, row: Mapping[str, object], min_edge: float,
    ingestion_run_id: int, cutoff_at_utc: datetime, receipt_cache: dict,
) -> bool:
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
        return False
    try:
        quote_id = int(quote_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("The scored quote ID is invalid") from exc
    quote = connection.execute("SELECT * FROM odds_quotes WHERE quote_id = ?", (quote_id,)).fetchone()
    if quote is None or quote["bout_id"] != bout_id or quote["market"] != "h2h":
        raise ValueError("The scored quote does not match the saved bout")
    evidence = linked_quote_rows(connection, bout_id, ingestion_run_id)
    verified_quote = any(
        item["quote_id"] == quote_id and quote_receipt_matches(
            item, cutoff_at_utc, receipt_cache, live_only=True,
        )
        for item in evidence
    )
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
    return verified_quote
