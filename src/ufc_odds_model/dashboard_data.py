"""Read-only, JSON-serializable views for the local dashboard.

This module never initializes a database, imports a feed, trains a model, or
creates a recommendation. It displays stored evidence and labels missing or
stale evidence explicitly. A fresh-looking row still needs the separate
pre-fight alert gate before it can be treated as an alert candidate.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

from .audit import audit_database
from .features import COVERAGE_NAMES
from .integrity import database_file_state
from .pipeline import utc_string


class PredictionView(TypedDict):
    prediction_id: int
    model_version: str
    feature_cutoff_at_utc: str
    generated_at_utc: str
    p_fighter_a: float
    p_fighter_b: float
    age_seconds: int
    feature_coverage: dict[str, bool] | None
    feature_coverage_status: str


class QuoteView(TypedDict):
    quote_id: int
    bookmaker: str
    selection_fighter_id: str
    selection_name: str
    decimal_odds: float
    break_even_probability: float
    captured_at_utc: str
    bookmaker_updated_at_utc: str | None
    source: str
    age_seconds: int
    freshness: str


class BoutView(TypedDict):
    bout_id: str
    fighter_a_id: str
    fighter_a_name: str
    fighter_b_id: str
    fighter_b_name: str
    weight_class: str | None
    scheduled_rounds: int | None
    status: str
    provider_status: str | None
    prediction: PredictionView | None
    quotes: list[QuoteView]
    gate_check: dict[str, Any] | None
    availability: str


class EventView(TypedDict):
    event_id: str
    name: str
    event_date: str
    start_time_utc: str | None
    status: str
    provider_status: str | None
    source: str | None
    is_demo: bool
    bouts: list[BoutView]


class DashboardView(TypedDict):
    status: str
    reason: str | None
    data_origin: str
    as_of_utc: str
    upcoming_events: list[EventView]
    quality: dict[str, Any]
    ingestion_runs: list[dict[str, Any]]
    job_runs: list[dict[str, Any]]
    paper_ledger: dict[str, Any]
    manual_ledger: dict[str, Any]
    evaluation: dict[str, Any]
    integrity: dict[str, Any]


_REQUIRED_TABLES = {
    "fighters", "events", "bouts", "results", "odds_quotes", "predictions",
    "bets", "paper_bets", "ingestion_runs",
}
_PREFIGHT_STATUSES = {None, "scheduled", "not_started"}
_DEFAULT_MAX_AGE_SECONDS = 60


def _timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _required_finite(value: object, name: str) -> float:
    number = _finite_float(value)
    if number is None:
        raise ValueError(f"Invalid stored {name}")
    return number


def _empty_ledger() -> dict[str, Any]:
    return {
        "summary": {
            "bets": 0, "by_status": {}, "stake_units": 0.0,
            "open_exposure_units": 0.0, "pending_review_stake_units": 0.0,
            "settled_stake_units": 0.0, "realized_profit_units": 0.0,
            "realized_roi": None, "unresolved_settlements": 0,
        },
        "rows": [],
    }


def _empty_view(as_of: datetime, status: str, reason: str) -> DashboardView:
    return {
        "status": status,
        "reason": reason,
        "data_origin": "empty",
        "as_of_utc": utc_string(as_of),
        "upcoming_events": [],
        "quality": {"status": "unavailable", "ok": None, "summary": {}, "issues": [], "reason": reason},
        "ingestion_runs": [],
        "job_runs": [],
        "paper_ledger": _empty_ledger(),
        "manual_ledger": _empty_ledger(),
        "evaluation": {"status": "unavailable", "reason": "No saved evaluation report was supplied."},
        "integrity": {"status": "unavailable", "reason": "No saved integrity check was supplied."},
    }


def _latest_prediction(
    connection: sqlite3.Connection, bout_id: str, as_of: datetime,
) -> PredictionView | None:
    eligible: list[tuple[datetime, int, sqlite3.Row]] = []
    for row in connection.execute("SELECT * FROM predictions WHERE bout_id = ?", (bout_id,)):
        cutoff = _timestamp(row["feature_cutoff_at_utc"])
        generated = _timestamp(row["generated_at_utc"])
        if cutoff is None or generated is None or not cutoff <= generated <= as_of:
            continue
        eligible.append((generated, int(row["prediction_id"]), row))
    if not eligible:
        return None
    generated, _, row = max(eligible, key=lambda item: item[:2])
    probability = _finite_float(row["p_fighter_a"])
    if probability is None or not 0 <= probability <= 1:
        return None
    model_version = str(row["model_version"])
    feature_coverage: dict[str, bool] | None = None
    feature_coverage_status = "not_applicable"
    if model_version.startswith("logistic-"):
        feature_coverage_status = "missing"
        raw_coverage = row["feature_coverage_json"] if "feature_coverage_json" in row.keys() else None
        if raw_coverage is not None:
            try:
                parsed_coverage = json.loads(raw_coverage)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_coverage = None
            if (isinstance(parsed_coverage, dict)
                    and set(parsed_coverage) == set(COVERAGE_NAMES)
                    and all(type(parsed_coverage[name]) is bool for name in COVERAGE_NAMES)):
                feature_coverage = {name: parsed_coverage[name] for name in COVERAGE_NAMES}
                feature_coverage_status = "recorded"
            else:
                feature_coverage_status = "invalid"
    return {
        "prediction_id": int(row["prediction_id"]),
        "model_version": model_version,
        "feature_cutoff_at_utc": str(row["feature_cutoff_at_utc"]),
        "generated_at_utc": str(row["generated_at_utc"]),
        "p_fighter_a": probability,
        "p_fighter_b": 1.0 - probability,
        "age_seconds": max(0, int((as_of - generated).total_seconds())),
        "feature_coverage": feature_coverage,
        "feature_coverage_status": feature_coverage_status,
    }


def _latest_quotes(
    connection: sqlite3.Connection, bout_id: str, as_of: datetime,
    max_age_seconds: int,
) -> list[QuoteView]:
    latest: dict[tuple[str, str], tuple[datetime, int, sqlite3.Row]] = {}
    rows = connection.execute(
        """
        SELECT q.*, f.canonical_name AS selection_name
        FROM odds_quotes AS q
        JOIN fighters AS f ON f.fighter_id = q.selection_fighter_id
        WHERE q.bout_id = ? AND q.market = 'h2h'
        """, (bout_id,),
    )
    for row in rows:
        captured = _timestamp(row["captured_at_utc"])
        if captured is None or captured > as_of:
            continue
        key = (str(row["bookmaker"]), str(row["selection_fighter_id"]))
        candidate = (captured, int(row["quote_id"]), row)
        if key not in latest or candidate[:2] > latest[key][:2]:
            latest[key] = candidate

    views: list[QuoteView] = []
    for captured, _, row in latest.values():
        odds = _finite_float(row["decimal_odds"])
        if odds is None or odds <= 1:
            continue
        age = max(0, int((as_of - captured).total_seconds()))
        updated_text = row["bookmaker_updated_at_utc"]
        updated = _timestamp(updated_text)
        if updated_text is None:
            freshness = "unverified_bookmaker_time"
        elif updated is None or updated > captured:
            freshness = "invalid_bookmaker_time"
        elif age > max_age_seconds or (as_of - updated).total_seconds() > max_age_seconds:
            freshness = "stale"
        else:
            freshness = "fresh"
        views.append({
            "quote_id": int(row["quote_id"]),
            "bookmaker": str(row["bookmaker"]),
            "selection_fighter_id": str(row["selection_fighter_id"]),
            "selection_name": str(row["selection_name"]),
            "decimal_odds": odds,
            "break_even_probability": 1.0 / odds,
            "captured_at_utc": str(row["captured_at_utc"]),
            "bookmaker_updated_at_utc": str(updated_text) if updated_text is not None else None,
            "source": str(row["source"]),
            "age_seconds": age,
            "freshness": freshness,
        })
    views.sort(key=lambda item: (item["selection_name"], item["bookmaker"]))
    return views


def _availability(
    event: sqlite3.Row, bout: sqlite3.Row, prediction: PredictionView | None,
    quotes: list[QuoteView], gate_check: dict[str, Any] | None,
    as_of: datetime, max_age_seconds: int,
) -> str:
    if event["provider_status"] not in _PREFIGHT_STATUSES or bout["provider_status"] not in _PREFIGHT_STATUSES:
        return "provider_status_blocked"
    if bout["status"] != "scheduled":
        return "bout_not_scheduled"
    start = _timestamp(event["start_time_utc"])
    if start is None:
        return "event_start_unavailable"
    if as_of >= start:
        return "event_started"
    if prediction is None:
        return "no_valid_prediction"
    if prediction["age_seconds"] > max_age_seconds:
        return "stale_prediction"
    if not quotes:
        return "no_quote"
    if not any(quote["freshness"] == "fresh" for quote in quotes):
        return "no_verified_fresh_quote"
    if gate_check is not None:
        return "gate_accepted_recently" if gate_check["display_status"] == "accepted_recently" else "gate_" + str(gate_check["display_status"])
    return "requires_alert_gate"


def _stored_roster_status(
    connection: sqlite3.Connection, gate: sqlite3.Row, checked: datetime, as_of: datetime,
) -> str | None:
    """Check saved roster identity using SQLite only; page loads never hash files."""
    if "roster_snapshot_id" not in gate.keys() or gate["roster_snapshot_id"] is None:
        return "roster_unverified"
    tables = {row["name"] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('card_event_snapshots', 'card_bout_snapshots')"
    )}
    if tables != {"card_event_snapshots", "card_bout_snapshots"}:
        return "roster_unverified"
    latest = connection.execute(
        """
        SELECT s.*, r.fetched_at_utc
        FROM card_event_snapshots AS s
        JOIN ingestion_runs AS r ON r.run_id = s.ingestion_run_id
        WHERE s.event_id = ?
        ORDER BY r.fetched_at_utc DESC, s.event_snapshot_id DESC LIMIT 1
        """, (gate["event_id"],),
    ).fetchone()
    if latest is None or latest["event_snapshot_id"] != gate["roster_snapshot_id"]:
        return "roster_superseded"
    observed = _timestamp(latest["source_observed_at_utc"])
    fetched = _timestamp(latest["fetched_at_utc"])
    start = _timestamp(latest["start_time_utc"])
    if (observed is None or fetched is None or start is None
            or observed > fetched or fetched > checked or observed > checked
            or fetched > as_of or observed > as_of
            or as_of - observed > timedelta(hours=24) or observed >= start
            or as_of >= start or latest["event_status"] != "scheduled"
            or latest["event_provider_status"] not in _PREFIGHT_STATUSES):
        return "roster_superseded"
    event = connection.execute(
        "SELECT * FROM events WHERE event_id = ?", (gate["event_id"],)
    ).fetchone()
    bout = connection.execute(
        "SELECT * FROM bouts WHERE bout_id = ?", (gate["bout_id"],)
    ).fetchone()
    observed_bout = connection.execute(
        "SELECT * FROM card_bout_snapshots WHERE event_snapshot_id = ? AND bout_id = ?",
        (latest["event_snapshot_id"], gate["bout_id"]),
    ).fetchone()
    if (event is None or bout is None or observed_bout is None
            or event["status"] != "scheduled" or event["provider_status"] not in _PREFIGHT_STATUSES
            or _timestamp(event["start_time_utc"]) != start
            or bout["event_id"] != event["event_id"]
            or bout["status"] != "scheduled" or observed_bout["bout_status"] != "scheduled"
            or bout["provider_status"] not in _PREFIGHT_STATUSES
            or observed_bout["bout_provider_status"] not in _PREFIGHT_STATUSES
            or bout["fighter_a_id"] != observed_bout["fighter_a_id"]
            or bout["fighter_b_id"] != observed_bout["fighter_b_id"]):
        return "roster_superseded"
    return None


def _latest_gate_check(
    connection: sqlite3.Connection, bout_id: str,
    prediction: PredictionView | None, quotes: list[QuoteView],
    as_of: datetime, max_age_seconds: int,
) -> dict[str, Any] | None:
    tables = {row["name"] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'prefight_gate_checks'"
    )}
    if "prefight_gate_checks" not in tables:
        return None
    candidates: list[tuple[datetime, int, sqlite3.Row]] = []
    for row in connection.execute(
        "SELECT * FROM prefight_gate_checks WHERE bout_id = ?", (bout_id,)
    ):
        checked = _timestamp(row["checked_at_utc"])
        if checked is not None and checked <= as_of:
            candidates.append((checked, int(row["gate_check_id"]), row))
    if not candidates:
        return None
    checked, _, row = max(candidates, key=lambda item: item[:2])
    latest_receipt = connection.execute(
        "SELECT run_id, snapshot_at_utc FROM ingestion_runs "
        "WHERE source = 'the-odds-api' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    quote = next((item for item in quotes if item["quote_id"] == row["quote_id"]), None)
    snapshot = _timestamp(row["snapshot_at_utc"])
    stored_limit = _finite_float(row["max_age_seconds"])
    limit = min(stored_limit, float(max_age_seconds)) if stored_limit is not None else None
    saved_odds = _finite_float(row["quoted_decimal_odds"])
    saved_probability = _finite_float(row["model_probability"])
    if row["gate_decision"] != "alert_candidate":
        status = "rejected"
    elif (snapshot is None or snapshot > checked or limit is None or limit <= 0):
        status = "invalid_time"
    elif ((as_of - checked).total_seconds() > limit
          or (as_of - snapshot).total_seconds() > limit):
        status = "expired"
    elif (latest_receipt is None or row["ingestion_run_id"] != latest_receipt["run_id"]
          or row["snapshot_at_utc"] != latest_receipt["snapshot_at_utc"]):
        status = "superseded"
    elif (prediction is None or row["prediction_id"] != prediction["prediction_id"]
          or row["model_version"] != prediction["model_version"]
          or row["model_cutoff_at_utc"] != prediction["feature_cutoff_at_utc"]):
        status = "prediction_superseded"
    elif (quote is None or quote["freshness"] != "fresh"
          or row["bookmaker"] != quote["bookmaker"]
          or row["quote_captured_at_utc"] != quote["captured_at_utc"]
          or row["bookmaker_updated_at_utc"] != quote["bookmaker_updated_at_utc"]
          or saved_odds is None
          or not math.isclose(saved_odds, quote["decimal_odds"], rel_tol=1e-12)):
        status = "quote_superseded_or_stale"
    else:
        roster_status = _stored_roster_status(connection, row, checked, as_of)
        if roster_status is not None:
            status = roster_status
        else:
            selection_probability = (prediction["p_fighter_a"]
                                     if quote["selection_fighter_id"] == connection.execute(
                                         "SELECT fighter_a_id FROM bouts WHERE bout_id = ?", (bout_id,)
                                     ).fetchone()["fighter_a_id"] else prediction["p_fighter_b"])
            status = ("accepted_recently" if saved_probability is not None
                      and math.isclose(saved_probability, selection_probability,
                                       rel_tol=1e-12, abs_tol=1e-12)
                      else "prediction_changed")
    return {
        "gate_check_id": int(row["gate_check_id"]),
        "ingestion_run_id": int(row["ingestion_run_id"]),
        "prediction_id": int(row["prediction_id"]),
        "quote_id": int(row["quote_id"]) if row["quote_id"] is not None else None,
        "checked_at_utc": str(row["checked_at_utc"]),
        "snapshot_at_utc": str(row["snapshot_at_utc"]),
        "age_seconds": int((as_of - checked).total_seconds()),
        "gate_decision": str(row["gate_decision"]),
        "gate_reason": str(row["gate_reason"]),
        "model_decision": str(row["model_decision"]),
        "max_age_seconds": stored_limit,
        "decimal_odds_drift": _finite_float(row["decimal_odds_drift"]),
        "min_edge": _finite_float(row["min_edge"]),
        "conservative_decimal_odds": _finite_float(row["conservative_decimal_odds"]),
        "conservative_expected_profit_per_dollar": _finite_float(
            row["conservative_expected_profit_per_dollar"]
        ),
        "roster_snapshot_id": row["roster_snapshot_id"] if "roster_snapshot_id" in row.keys() else None,
        "display_status": status,
    }


def _upcoming_events(
    connection: sqlite3.Connection, as_of: datetime, event_limit: int,
    max_age_seconds: int,
) -> list[EventView]:
    candidates = connection.execute(
        """
        SELECT * FROM events
        WHERE status = 'scheduled' AND event_date >= ?
        ORDER BY event_date, COALESCE(start_time_utc, ''), event_id
        """, (as_of.date().isoformat(),),
    ).fetchall()
    result: list[EventView] = []
    for event in candidates:
        start = _timestamp(event["start_time_utc"])
        if start is not None and start <= as_of:
            continue
        bouts: list[BoutView] = []
        for bout in connection.execute(
            """
            SELECT b.*, fa.canonical_name AS fighter_a_name,
                   fb.canonical_name AS fighter_b_name
            FROM bouts AS b
            JOIN fighters AS fa ON fa.fighter_id = b.fighter_a_id
            JOIN fighters AS fb ON fb.fighter_id = b.fighter_b_id
            WHERE b.event_id = ? ORDER BY b.bout_id
            """, (event["event_id"],),
        ):
            prediction = _latest_prediction(connection, bout["bout_id"], as_of)
            quotes = _latest_quotes(connection, bout["bout_id"], as_of, max_age_seconds)
            gate_check = _latest_gate_check(connection, bout["bout_id"], prediction,
                                            quotes, as_of, max_age_seconds)
            bouts.append({
                "bout_id": str(bout["bout_id"]),
                "fighter_a_id": str(bout["fighter_a_id"]),
                "fighter_a_name": str(bout["fighter_a_name"]),
                "fighter_b_id": str(bout["fighter_b_id"]),
                "fighter_b_name": str(bout["fighter_b_name"]),
                "weight_class": bout["weight_class"],
                "scheduled_rounds": bout["scheduled_rounds"],
                "status": str(bout["status"]),
                "provider_status": bout["provider_status"],
                "prediction": prediction,
                "quotes": quotes,
                "gate_check": gate_check,
                "availability": _availability(event, bout, prediction, quotes,
                                               gate_check, as_of, max_age_seconds),
            })
        result.append({
            "event_id": str(event["event_id"]),
            "name": str(event["name"]),
            "event_date": str(event["event_date"]),
            "start_time_utc": event["start_time_utc"],
            "status": str(event["status"]),
            "provider_status": event["provider_status"],
            "source": event["source"],
            "is_demo": event["source"] == "demo",
            "bouts": bouts,
        })
        if len(result) >= event_limit:
            break
    return result


def _summarize_ledger(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = Counter(str(row["settlement_status"]) for row in rows)
    settled = [row for row in rows if row["settlement_status"] in {"won", "lost", "push", "void"}
               and row["payout_units"] is not None]
    settled_stake = sum(float(row["stake_units"]) for row in settled)
    realized_profit = sum(float(row["profit_units"]) for row in settled)
    return {
        "bets": len(rows),
        "by_status": dict(by_status),
        "stake_units": round(sum(float(row["stake_units"]) for row in rows), 4),
        "open_exposure_units": round(sum(float(row["stake_units"]) for row in rows
                                          if row["settlement_status"] == "open"), 4),
        "pending_review_stake_units": round(sum(float(row["stake_units"]) for row in rows
                                                if row["settlement_status"] == "pending_review"), 4),
        "settled_stake_units": round(settled_stake, 4),
        "realized_profit_units": round(realized_profit, 4),
        "realized_roi": round(realized_profit / settled_stake, 4) if settled_stake else None,
        "unresolved_settlements": sum(1 for row in rows
                                      if row["settlement_status"] in {"won", "lost", "push", "void"}
                                      and row["payout_units"] is None),
    }


def _paper_ledger(connection: sqlite3.Connection) -> dict[str, Any]:
    gate_columns = {item["name"] for item in connection.execute(
        "PRAGMA table_info(prefight_gate_checks)"
    )}
    recorded_rosters = {}
    if "roster_snapshot_id" in gate_columns:
        recorded_rosters = {
            int(row["gate_check_id"]): row["roster_snapshot_id"]
            for row in connection.execute(
                "SELECT gate_check_id, roster_snapshot_id FROM prefight_gate_checks"
            )
        }
    records = connection.execute(
        """
        SELECT pb.*, e.name AS event_name, e.event_date,
               fa.canonical_name AS fighter_a_name, fb.canonical_name AS fighter_b_name,
               fs.canonical_name AS selection_name, p.model_version
        FROM paper_bets AS pb
        JOIN events AS e ON e.event_id = pb.event_id
        JOIN bouts AS b ON b.bout_id = pb.bout_id
        JOIN fighters AS fa ON fa.fighter_id = b.fighter_a_id
        JOIN fighters AS fb ON fb.fighter_id = b.fighter_b_id
        JOIN fighters AS fs ON fs.fighter_id = pb.selection_fighter_id
        JOIN predictions AS p ON p.prediction_id = pb.prediction_id
        ORDER BY pb.decision_at_utc DESC, pb.paper_bet_id DESC
        """
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        row["roster_evidence_status"] = (
            "snapshot_recorded" if recorded_rosters.get(row.get("gate_check_id")) is not None
            else "legacy_unverified"
        )
        row["stake_units"] = _required_finite(row["stake_units"], "paper stake")
        row["payout_units"] = (_required_finite(row["payout_units"], "paper payout")
                               if row["payout_units"] is not None else None)
        row["profit_units"] = (
            row["payout_units"] - row["stake_units"]
            if row["settlement_status"] in {"won", "lost"} and row["payout_units"] is not None
            else None
        )
        rows.append(row)
    return {"summary": _summarize_ledger(rows), "rows": rows}


def _job_runs(connection: sqlite3.Connection, as_of: datetime) -> list[dict[str, Any]]:
    tables = {item["name"] for item in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'operator_job_runs'"
    )}
    if "operator_job_runs" not in tables:
        return []
    return [dict(row) for row in connection.execute(
        """
        SELECT job_run_id, command, event_id, started_at_utc, finished_at_utc,
               status, error_category
        FROM operator_job_runs
        WHERE started_at_utc <= ?
        ORDER BY started_at_utc DESC, job_run_id DESC LIMIT 10
        """, (utc_string(as_of),),
    )]


def _manual_ledger(connection: sqlite3.Connection) -> dict[str, Any]:
    records = connection.execute(
        """
        SELECT bet.*, p.bout_id, p.model_version, p.feature_cutoff_at_utc,
               q.bookmaker, q.captured_at_utc AS quote_captured_at_utc,
               e.event_id, e.name AS event_name, e.event_date,
               fa.canonical_name AS fighter_a_name, fb.canonical_name AS fighter_b_name,
               fs.canonical_name AS selection_name
        FROM bets AS bet
        JOIN predictions AS p ON p.prediction_id = bet.prediction_id
        JOIN odds_quotes AS q ON q.quote_id = bet.quote_id
        JOIN bouts AS b ON b.bout_id = p.bout_id
        JOIN events AS e ON e.event_id = b.event_id
        JOIN fighters AS fa ON fa.fighter_id = b.fighter_a_id
        JOIN fighters AS fb ON fb.fighter_id = b.fighter_b_id
        JOIN fighters AS fs ON fs.fighter_id = bet.selection_fighter_id
        ORDER BY bet.placed_at_utc DESC, bet.bet_id DESC
        """
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        row["stake_units"] = _required_finite(row["stake"], "manual stake")
        row["payout_units"] = (_required_finite(row["payout"], "manual payout")
                               if row["payout"] is not None else None)
        row["profit_units"] = (
            row["payout_units"] - row["stake_units"]
            if row["settlement_status"] in {"won", "lost", "push", "void"}
            and row["payout_units"] is not None else None
        )
        rows.append(row)
    return {"summary": _summarize_ledger(rows), "rows": rows}


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _report_number(value: object) -> float | None:
    return _finite_float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _valid_metrics(value: object, expected_bouts: int) -> bool:
    if (not isinstance(value, dict) or not _nonnegative_int(value.get("bouts"))
            or value["bouts"] != expected_bouts or expected_bouts <= 0):
        return False
    for name in ("accuracy", "brier_score", "log_loss"):
        number = _report_number(value.get(name))
        if number is None or number < 0 or (name != "log_loss" and number > 1):
            return False
    return True


def _valid_calibration_bins(value: object, expected_bouts: int) -> bool:
    if not isinstance(value, list) or len(value) != 5:
        return False
    total = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not _nonnegative_int(item.get("bouts")):
            return False
        count = item["bouts"]
        lower = _report_number(item.get("lower"))
        upper = _report_number(item.get("upper"))
        if lower is None or upper is None or abs(lower - index / 5) > 1e-9 or abs(upper - (index + 1) / 5) > 1e-9:
            return False
        for name in ("mean_probability", "observed_win_rate"):
            number = _report_number(item.get(name))
            if (count == 0 and item.get(name) is not None) or (count > 0 and (number is None or not 0 <= number <= 1)):
                return False
        total += count
    return total == expected_bouts


def _valid_evaluation_result(result: dict[str, Any]) -> bool:
    """Check the saved metric schema before exposing a report as evidence."""
    if result.get("status") == "insufficient_history":
        return result.get("split") is None and result.get("test") is None
    if result.get("status") != "ok":
        return False
    splits = result.get("split")
    calibration = result.get("calibration")
    test = result.get("test")
    if not all(isinstance(item, dict) for item in (splits, calibration, test)):
        return False
    summaries: dict[str, dict[str, Any]] = {}
    for name in ("train", "validation", "test"):
        summary = splits.get(name)
        if not isinstance(summary, dict) or not all(_nonnegative_int(summary.get(key)) for key in ("bouts", "events")):
            return False
        if not 0 < summary["events"] <= summary["bouts"]:
            return False
        try:
            first = date.fromisoformat(summary["first_date"])
            last = date.fromisoformat(summary["last_date"])
        except (KeyError, TypeError, ValueError):
            return False
        if first > last:
            return False
        summaries[name] = summary
    if summaries["train"]["last_date"] >= summaries["validation"]["first_date"]:
        return False
    if summaries["validation"]["last_date"] >= summaries["test"]["first_date"]:
        return False
    if (calibration.get("method") != "symmetric_temperature"
            or calibration.get("validation_bouts") != summaries["validation"]["bouts"]
            or not isinstance(calibration.get("applied"), bool)
            or (_report_number(calibration.get("scale")) or 0) <= 0):
        return False
    test_bouts = summaries["test"]["bouts"]
    for name in ("elo", "logistic"):
        model = test.get(name)
        if (not isinstance(model, dict)
                or not _valid_metrics(model.get("metrics"), test_bouts)
                or not _valid_calibration_bins(model.get("calibration_bins"), test_bouts)):
            return False
    book = test.get("bookmaker")
    if not isinstance(book, dict) or not _nonnegative_int(book.get("available_bouts")):
        return False
    priced = book["available_bouts"]
    coverage = _report_number(book.get("coverage"))
    if priced > test_bouts or coverage is None or abs(coverage - priced / test_bouts) > 1e-9:
        return False
    subset_keys = ("metrics", "elo_on_available_bouts", "logistic_on_available_bouts")
    if priced == 0:
        return all(book.get(key) is None for key in (*subset_keys, "calibration_bins"))
    return (all(_valid_metrics(book.get(key), priced) for key in subset_keys)
            and _valid_calibration_bins(book.get("calibration_bins"), priced))


def _evaluation_report(
    path: str | Path | None, db_path: Path, as_of: datetime,
) -> dict[str, Any]:
    if path is None:
        return {"status": "unavailable", "reason": "No saved evaluation report was supplied."}
    report_path = Path(path)
    if not report_path.is_file():
        return {"status": "missing_report", "reason": f"Evaluation report is missing: {report_path}"}
    try:
        saved = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "invalid_report", "reason": "Evaluation report is not valid JSON."}
    if not isinstance(saved, dict):
        return {"status": "invalid_report", "reason": "Evaluation report must be a JSON object."}
    try:
        json.dumps(saved, allow_nan=False)
    except (TypeError, ValueError):
        return {"status": "invalid_report", "reason": "Evaluation report contains invalid numeric values."}
    generated_at = _timestamp(saved.get("generated_at_utc"))
    result = saved.get("evaluation")
    source_mtime = saved.get("source_db_mtime_ns")
    if (saved.get("schema_version") != 1 or generated_at is None
            or not isinstance(result, dict)
            or result.get("status") not in {"ok", "insufficient_history"}
            or not isinstance(saved.get("parameters"), dict)
            or (source_mtime is not None and (not isinstance(source_mtime, int)
                                               or isinstance(source_mtime, bool)
                                               or source_mtime < 0))):
        return {"status": "invalid_report", "reason": "Evaluation report lacks valid provenance or model results."}
    if not _valid_evaluation_result(result):
        return {"status": "invalid_report", "reason": "Saved evaluation has incomplete metrics or split evidence; rerun evaluation."}
    if generated_at > as_of:
        return {"status": "invalid_report", "reason": "Evaluation report generation time is in the future."}
    source_db_path = saved.get("source_db_path")
    if source_db_path is None:
        return {
            "status": "unverified_provenance",
            "reason": "Saved evaluation has no source database path; rerun evaluation for this database.",
            "generated_at_utc": saved["generated_at_utc"],
            "source_path": str(report_path),
        }
    if not isinstance(source_db_path, str) or not Path(source_db_path).is_absolute():
        return {"status": "invalid_report", "reason": "Evaluation source database path must be absolute."}
    reported_database = Path(source_db_path).resolve()
    selected_database = db_path.resolve()
    if reported_database != selected_database:
        return {
            "status": "wrong_database",
            "reason": "Saved evaluation belongs to a different database; rerun evaluation for this database.",
            "generated_at_utc": saved["generated_at_utc"],
            "source_path": str(report_path),
            "source_db_path": str(reported_database),
            "selected_db_path": str(selected_database),
        }
    if source_mtime is None:
        return {
            "status": "unverified_provenance",
            "reason": "Saved evaluation has no source database modification time; rerun evaluation.",
            "generated_at_utc": saved["generated_at_utc"],
            "source_path": str(report_path),
            "source_db_path": str(reported_database),
        }
    try:
        possibly_stale = db_path.stat().st_mtime_ns > source_mtime
    except OSError:
        return {"status": "unavailable", "reason": "Database file changed or became inaccessible during report check."}
    return {
        "status": "possibly_stale" if possibly_stale else (
            "available" if result["status"] == "ok" else "insufficient_history"
        ),
        "reason": "Database changed after this report; rerun evaluation to confirm it." if possibly_stale else None,
        "generated_at_utc": saved["generated_at_utc"],
        "age_seconds": int((as_of - generated_at).total_seconds()),
        "source_path": str(report_path),
        "source_db_path": str(reported_database),
        "source_db_mtime_ns": source_mtime,
        "result_status": result["status"],
        "parameters": saved["parameters"],
        "result": result,
    }


def _integrity_report(path: str | Path | None, db_path: Path, as_of: datetime) -> dict[str, Any]:
    """Display a saved verifier result without reading source files on page load."""
    if path is None:
        return {"status": "unavailable", "reason": "No saved integrity check was supplied."}
    try:
        report_path = Path(path)
        if not report_path.is_file():
            return {"status": "missing_report", "reason": "Run the local evidence integrity check."}
    except (OSError, TypeError, ValueError):
        return {"status": "invalid_report", "reason": "Saved integrity check path is invalid."}
    try:
        saved = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "invalid_report", "reason": "Saved integrity check is not valid JSON."}
    if not isinstance(saved, dict):
        return {"status": "invalid_report", "reason": "Saved integrity check must be a JSON object."}
    checked = _timestamp(saved.get("checked_at_utc"))
    source_path = saved.get("database_path")
    project_root = saved.get("project_root")
    mtime = saved.get("database_mtime_ns")
    count = saved.get("receipt_count")
    verified = saved.get("verified_receipts")
    issue_count = saved.get("issue_count")
    status = saved.get("status")
    issues = saved.get("issues")
    by_source = saved.get("by_source")
    file_state = saved.get("database_file_state")
    valid_file_state = (
        isinstance(file_state, dict)
        and all(_nonnegative_int(file_state.get(key)) for key in ("main_mtime_ns", "main_size"))
        and ((file_state.get("wal_mtime_ns") is None and file_state.get("wal_size") is None)
             or all(_nonnegative_int(file_state.get(key)) for key in ("wal_mtime_ns", "wal_size")))
        and file_state.get("main_mtime_ns") == mtime
    )
    valid_issues = (isinstance(issues, list) and _nonnegative_int(issue_count)
                    and len(issues) <= issue_count
                    and (issue_count == 0 or bool(issues))
                    and all(isinstance(item, dict)
                            and isinstance(item.get("code"), str)
                            and bool(item["code"])
                            and isinstance(item.get("detail"), str)
                            and ("run_id" not in item or _nonnegative_int(item["run_id"]))
                            for item in issues))
    valid_sources = False
    if isinstance(by_source, dict) and _nonnegative_int(count):
        valid_sources = (
            all(isinstance(key, str) and bool(key) and _nonnegative_int(value)
                for key, value in by_source.items())
            and sum(by_source.values()) == count
        )
    if (checked is None or checked > as_of
            or not isinstance(source_path, str) or not source_path.strip()
            or not isinstance(project_root, str) or not project_root.strip()
            or status not in {"verified", "issues", "unavailable"}
            or not isinstance(saved.get("ok"), bool)
            or not all(_nonnegative_int(item) for item in (count, verified, issue_count))
            or verified > count or not valid_issues or not valid_sources
            or (mtime is not None and not _nonnegative_int(mtime))
            or (status == "verified" and (
                not saved["ok"] or count == 0 or issue_count != 0 or verified != count
                or not valid_file_state or saved.get("database_integrity") != ["ok"]
                or saved.get("foreign_key_violations") != 0
                or saved.get("missing_migrations") != []
            ))
            or (status != "verified" and (saved["ok"] or issue_count == 0))):
        return {"status": "invalid_report", "reason": "Saved integrity check has incomplete provenance or counts."}
    try:
        reported_database = Path(source_path)
        reported_root = Path(project_root)
        if not reported_database.is_absolute() or not reported_root.is_absolute():
            raise ValueError("relative source provenance path")
        reported_root.resolve()
        different_database = reported_database.resolve() != db_path.resolve()
    except (OSError, ValueError, RuntimeError):
        return {"status": "invalid_report", "reason": "Saved integrity check has an invalid database path."}
    if different_database:
        return {"status": "wrong_database", "reason": "Saved integrity check belongs to another database."}
    if status != "verified":
        return {
            "status": "issues", "reason": f"Last evidence check found {issue_count} issue(s).",
            "checked_at_utc": saved["checked_at_utc"], "issue_count": issue_count,
            "receipt_count": count, "verified_receipts": verified,
        }
    try:
        current_file_state = database_file_state(db_path)
    except OSError:
        return {"status": "unavailable", "reason": "Database became inaccessible after the saved evidence check."}
    if current_file_state != file_state:
        display_status, reason = "possibly_stale", "Database or write-ahead log changed after the saved evidence check."
    elif as_of - checked > timedelta(minutes=15):
        display_status, reason = "expired", "Evidence check is over 15 minutes old; rerun it before an event."
    else:
        display_status, reason = "verified_recently", "Payload hashes matched at the saved check time."
    return {
        "status": display_status, "reason": reason, "checked_at_utc": saved["checked_at_utc"],
        "receipt_count": count, "verified_receipts": verified,
    }


def load_dashboard(
    db_path: str | Path,
    *,
    as_of: datetime | None = None,
    event_limit: int = 8,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    evaluation_report: str | Path | None = None,
    integrity_report: str | Path | None = None,
) -> DashboardView:
    """Load a dashboard snapshot from SQLite opened in read-only mode.

    The displayed quote freshness matches the default pre-fight alert age (60
    seconds), but ``requires_alert_gate`` is never an actionable signal. The
    stored evaluation JSON is optional because page loads must not fit models.
    """
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    as_of = as_of.astimezone(timezone.utc)
    if not isinstance(event_limit, int) or isinstance(event_limit, bool) or event_limit <= 0:
        raise ValueError("event_limit must be a positive integer")
    if not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be a positive integer")

    path = Path(db_path).expanduser()
    if not path.is_file():
        return _empty_view(as_of, "missing_database", f"Database does not exist: {path}")
    try:
        # `mode=ro` prevents writes at the SQLite file layer. `query_only`
        # provides a second guard if a helper later receives this connection.
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            missing = sorted(_REQUIRED_TABLES - tables)
            if missing:
                return _empty_view(as_of, "schema_missing", "Missing tables: " + ", ".join(missing))

            event_sources = [row["source"] for row in connection.execute("SELECT source FROM events")]
            if not event_sources:
                origin = "empty"
            elif all(source == "demo" for source in event_sources):
                origin = "demo_only"
            elif all(source == "wikipedia_research" for source in event_sources):
                origin = "research_only"
            elif any(source == "wikipedia_research" for source in event_sources):
                origin = "research_mixed"
            elif any(source == "demo" for source in event_sources):
                origin = "mixed"
            else:
                origin = "non_demo"

            quality = audit_database(connection, as_of=as_of)
            quality["status"] = "ready"
            quality["as_of_utc"] = utc_string(as_of)
            ingestion = [dict(row) for row in connection.execute(
                """
                SELECT run_id, source, fetched_at_utc, payload_path, sha256
                FROM ingestion_runs ORDER BY run_id DESC LIMIT 20
                """
            )]
            view: DashboardView = {
                "status": "available" if event_sources else "empty",
                "reason": None if event_sources else "Database has no events yet.",
                "data_origin": origin,
                "as_of_utc": utc_string(as_of),
                "upcoming_events": _upcoming_events(connection, as_of, event_limit, max_age_seconds),
                "quality": quality,
                "ingestion_runs": ingestion,
                "job_runs": _job_runs(connection, as_of),
                "paper_ledger": _paper_ledger(connection),
                "manual_ledger": _manual_ledger(connection),
                "evaluation": _evaluation_report(evaluation_report, path, as_of),
                "integrity": _integrity_report(integrity_report, path, as_of),
            }
            # Standard JSON excludes NaN/Infinity; never send such values to
            # the UI even if someone bypassed SQLite's normal constraints.
            json.dumps(view, allow_nan=False)
            return view
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return _empty_view(as_of, "unreadable_database", "SQLite could not read the database or its schema.")
    except (ValueError, TypeError, OverflowError):
        return _empty_view(as_of, "invalid_data", "Stored records contain invalid values; inspect and repair the source rows.")
