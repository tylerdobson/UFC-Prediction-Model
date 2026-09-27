"""Read-only, JSON-serializable views for the local dashboard.

This module never initializes a database, imports a feed, trains a model, or
creates a recommendation. It displays stored evidence and labels missing or
stale evidence explicitly. A fresh-looking row still needs the separate
pre-fight alert gate before it can be treated as an alert candidate.
"""

from __future__ import annotations

import hashlib
import csv
import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import unquote, urlsplit

from .audit import audit_database
from .evaluation import MIN_PRIOR_RESULT_EVIDENCE_COVERAGE
from .features import COVERAGE_NAMES
from .integrity import database_file_state, verify_evidence
from .pipeline import utc_string
from .research_evaluation import RESEARCH_EVALUATION_VERSION, SOURCE as RESEARCH_SOURCE, _source_results
from .wikipedia_recount import reviewed_same_revision_recount


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
    source_evidence: dict[str, str | None] | None
    is_demo: bool
    bouts: list[BoutView]


class HistoricalResearchView(TypedDict):
    source: str
    events: int
    bouts: int
    results: int
    fighters: int
    event_page_receipts: int
    source_bout_rows: int | None
    held_identity_rows: int | None
    accepted_bout_coverage: float | None
    receipt_events_verified: int
    receipt_status: str
    receipt_reason: str | None
    first_date: str | None
    last_date: str | None
    by_year: list[dict[str, Any]]
    recent_events: list[dict[str, Any]]


class DashboardView(TypedDict):
    status: str
    reason: str | None
    data_origin: str
    as_of_utc: str
    upcoming_events: list[EventView]
    historical_research: HistoricalResearchView
    quality: dict[str, Any]
    ingestion_runs: list[dict[str, Any]]
    job_runs: list[dict[str, Any]]
    paper_ledger: dict[str, Any]
    manual_ledger: dict[str, Any]
    evaluation: dict[str, Any]
    research_evaluation: dict[str, Any]
    integrity: dict[str, Any]


_REQUIRED_TABLES = {
    "fighters", "events", "bouts", "results", "odds_quotes", "predictions",
    "bets", "paper_bets", "ingestion_runs",
}
_PREFIGHT_STATUSES = {None, "scheduled", "not_started"}
_DEFAULT_MAX_AGE_SECONDS = 60
_UFC332_EVENT_ID = "wikipedia_pilot:83826247"
_UFC332_EVENT_DATE = "2026-10-03"
_UFC332_CAPTURE = "2026-09-26T22:47:55Z"
_FORWARD_EVIDENCE_FILES = {
    "historical_manifest": "docs/HISTORICAL_2026_PILOT_CARDS.json",
    "card_manifest": "data/raw/ufc332-intake/manifest.json",
    "card_csv": "data/raw/ufc332-intake/reviewed-card-template.csv",
    "odds_manifest": "data/raw/ufc332-odds-intake/intake_manifest.json",
    "research_db": "data/ufc_research_2011_2025.sqlite",
    "holdout_report": "reports/ufc_research_1993_2026_holdout.json",
}


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


def _empty_historical_research() -> HistoricalResearchView:
    return {
        "source": "wikipedia_research", "events": 0, "bouts": 0,
        "results": 0, "fighters": 0, "event_page_receipts": 0,
        "source_bout_rows": None, "held_identity_rows": None,
        "accepted_bout_coverage": None, "receipt_events_verified": 0,
        "receipt_status": "unavailable", "receipt_reason": "No completed research events.",
        "first_date": None, "last_date": None, "by_year": [],
        "recent_events": [],
    }


def _empty_view(as_of: datetime, status: str, reason: str) -> DashboardView:
    return {
        "status": status,
        "reason": reason,
        "data_origin": "empty",
        "as_of_utc": utc_string(as_of),
        "upcoming_events": [],
        "historical_research": _empty_historical_research(),
        "quality": {"status": "unavailable", "ok": None, "summary": {}, "issues": [], "reason": reason},
        "ingestion_runs": [],
        "job_runs": [],
        "paper_ledger": _empty_ledger(),
        "manual_ledger": _empty_ledger(),
        "evaluation": {"status": "unavailable", "reason": "No saved evaluation report was supplied."},
        "research_evaluation": {"status": "unavailable", "reason": "Research database is unavailable."},
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


def _historical_research(
    connection: sqlite3.Connection, as_of: datetime, *, has_receipts: bool,
) -> HistoricalResearchView:
    """Summarize imported research bouts without mixing in demo or live records.

    Annual rows and recent events are bounded for dashboard display. The four
    totals are computed over the full eligible population, so they reconcile
    independently of those display limits.
    """
    result = _empty_historical_research()
    cutoff_date = as_of.date().isoformat()
    counts = connection.execute(
        """
        SELECT COUNT(DISTINCT e.event_id) AS events,
               COUNT(b.bout_id) AS bouts,
               COUNT(r.bout_id) AS results,
               MIN(e.event_date) AS first_date,
               MAX(e.event_date) AS last_date
        FROM events AS e
        LEFT JOIN bouts AS b ON b.event_id = e.event_id
            AND b.source = 'wikipedia_research' AND b.status = 'completed'
        LEFT JOIN results AS r ON r.bout_id = b.bout_id
        WHERE e.source = 'wikipedia_research' AND e.status = 'completed'
          AND e.event_date <= ?
        """, (cutoff_date,),
    ).fetchone()
    result.update(
        events=int(counts["events"]), bouts=int(counts["bouts"]),
        results=int(counts["results"]),
        first_date=counts["first_date"], last_date=counts["last_date"],
    )
    result["fighters"] = int(connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT b.fighter_a_id AS fighter_id FROM bouts AS b
            JOIN events AS e ON e.event_id = b.event_id
            WHERE e.source = 'wikipedia_research' AND e.status = 'completed'
              AND e.event_date <= ? AND b.source = 'wikipedia_research'
              AND b.status = 'completed'
            UNION
            SELECT b.fighter_b_id AS fighter_id FROM bouts AS b
            JOIN events AS e ON e.event_id = b.event_id
            WHERE e.source = 'wikipedia_research' AND e.status = 'completed'
              AND e.event_date <= ? AND b.source = 'wikipedia_research'
              AND b.status = 'completed'
        )
        """, (cutoff_date, cutoff_date),
    ).fetchone()[0])
    if has_receipts:
        result["event_page_receipts"] = int(connection.execute(
            """
            SELECT COUNT(DISTINCT wr.event_page_id)
            FROM wikipedia_source_receipts AS wr
            JOIN events AS e ON e.source = 'wikipedia_research'
                AND CAST(SUBSTR(
                    e.source_event_id, 1,
                    INSTR(e.source_event_id || ':', ':') - 1
                ) AS INTEGER) = wr.event_page_id
            WHERE e.status = 'completed' AND e.event_date <= ?
            """, (cutoff_date,),
        ).fetchone()[0])
    result["by_year"] = [dict(row) for row in connection.execute(
        """
        SELECT SUBSTR(e.event_date, 1, 4) AS year,
               COUNT(DISTINCT e.event_id) AS events,
               COUNT(b.bout_id) AS bouts,
               COUNT(r.bout_id) AS results
        FROM events AS e
        LEFT JOIN bouts AS b ON b.event_id = e.event_id
            AND b.source = 'wikipedia_research' AND b.status = 'completed'
        LEFT JOIN results AS r ON r.bout_id = b.bout_id
        WHERE e.source = 'wikipedia_research' AND e.status = 'completed'
          AND e.event_date <= ?
        GROUP BY SUBSTR(e.event_date, 1, 4)
        ORDER BY year DESC LIMIT 100
        """, (cutoff_date,),
    )][::-1]
    result["recent_events"] = [dict(row) for row in connection.execute(
        """
        SELECT e.event_id, e.name, e.event_date,
               COUNT(b.bout_id) AS bouts, COUNT(r.bout_id) AS results
        FROM events AS e
        LEFT JOIN bouts AS b ON b.event_id = e.event_id
            AND b.source = 'wikipedia_research' AND b.status = 'completed'
        LEFT JOIN results AS r ON r.bout_id = b.bout_id
        WHERE e.source = 'wikipedia_research' AND e.status = 'completed'
          AND e.event_date <= ?
        GROUP BY e.event_id
        ORDER BY e.event_date DESC, e.event_id DESC LIMIT 12
        """, (cutoff_date,),
    )]
    _research_receipt_coverage(connection, result, cutoff_date, has_receipts=has_receipts)
    return result


def _research_event_receipt_key(source_event_id: object) -> tuple[int, str] | None:
    """Match a stored research event to its exact source page or section."""
    if not isinstance(source_event_id, str):
        return None
    page, separator, section = source_event_id.partition(":")
    if not page.isdecimal() or (separator and not section):
        return None
    return int(page), unquote(section).replace("_", " ").casefold() if separator else ""


def _research_page_receipt_key(page_id: object, page_url: object) -> tuple[int, str] | None:
    if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id <= 0:
        return None
    if not isinstance(page_url, str):
        return None
    try:
        parsed = urlsplit(page_url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "en.wikipedia.org":
        return None
    return page_id, unquote(parsed.fragment).replace("_", " ").casefold()


def _receipt_payload_matches(path_text: object, stated_hash: object) -> bool:
    if (not isinstance(path_text, str) or not path_text
            or not isinstance(stated_hash, str) or len(stated_hash) != 64
            or any(char not in "0123456789abcdef" for char in stated_hash)):
        return False
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        if path.is_symlink() or not path.is_file():
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == stated_hash
    except (OSError, ValueError):
        return False


def _research_receipt_coverage(
    connection: sqlite3.Connection, result: HistoricalResearchView,
    cutoff_date: str, *, has_receipts: bool,
) -> None:
    """Show source-row coverage only when each displayed event has checked evidence.

    Repeated imports produce multiple receipts. The latest receipt for each
    exact page/section is used. Changed counts for the same revision need a
    checked, scoped identity crosswalk receipt; other conflicts fail closed.
    Embedded-card sections retain separate receipt keys.
    """
    events = connection.execute(
        """
        SELECT e.event_id, e.source_event_id, e.event_date,
               COUNT(b.bout_id) AS imported_bouts,
               COUNT(r.bout_id) AS result_bouts
        FROM events AS e
        LEFT JOIN bouts AS b ON b.event_id = e.event_id
            AND b.source = 'wikipedia_research' AND b.status = 'completed'
        LEFT JOIN results AS r ON r.bout_id = b.bout_id
        WHERE e.source = 'wikipedia_research' AND e.status = 'completed'
          AND e.event_date <= ?
        GROUP BY e.event_id
        """, (cutoff_date,),
    ).fetchall()
    if not events or not has_receipts:
        result["receipt_reason"] = (
            "No completed research events." if not events
            else "Research source receipt table is unavailable."
        )
        return

    latest: dict[tuple[int, str], sqlite3.Row] = {}
    revision_rows: dict[tuple[tuple[int, str], int], list[sqlite3.Row]] = {}
    for row in connection.execute(
        """
        SELECT w.run_id, w.event_page_id, w.page_url, w.revision_id,
               w.imported_bouts, w.skipped_unresolved_bouts, w.crosswalk_run_id,
               i.payload_path, i.sha256
        FROM wikipedia_source_receipts AS w
        JOIN ingestion_runs AS i ON i.run_id = w.run_id
        WHERE i.source = 'wikipedia_research'
        ORDER BY w.run_id
        """
    ):
        key = _research_page_receipt_key(row["event_page_id"], row["page_url"])
        if key is None:
            continue
        revision = int(row["revision_id"])
        revision_rows.setdefault((key, revision), []).append(row)
        latest[key] = row

    bad = 0
    held = 0
    held_by_year: Counter[str] = Counter()
    checked_hashes: dict[tuple[str, str], bool] = {}
    for event in events:
        key = _research_event_receipt_key(event["source_event_id"])
        row = latest.get(key) if key is not None else None
        if (row is None or int(row["imported_bouts"]) != int(event["imported_bouts"])
                or int(event["result_bouts"]) != int(event["imported_bouts"])
                or reviewed_same_revision_recount(
                    connection, revision_rows[(key, int(row["revision_id"]))],
                    str(event["event_id"]), checked_hashes, _receipt_payload_matches,
                ) is None):
            bad += 1
            continue
        payload = (str(row["payload_path"]), str(row["sha256"]))
        if payload not in checked_hashes:
            checked_hashes[payload] = _receipt_payload_matches(*payload)
        if not checked_hashes[payload]:
            bad += 1
            continue
        result["receipt_events_verified"] += 1
        skipped = int(row["skipped_unresolved_bouts"])
        held += skipped
        held_by_year[str(event["event_date"])[:4]] += skipped
    if bad:
        result["receipt_reason"] = (
            f"{bad} of {len(events)} completed research events have missing, "
            "conflicting, mismatched, or unreadable source receipts."
        )
        return

    imported = int(result["bouts"])
    source_rows = imported + held
    result.update(
        source_bout_rows=source_rows,
        held_identity_rows=held,
        accepted_bout_coverage=imported / source_rows if source_rows else None,
        receipt_status="verified",
        receipt_reason=None,
    )
    for year in result["by_year"]:
        skipped = held_by_year[str(year["year"])]
        parsed = int(year["bouts"]) + skipped
        year["held_identity_rows"] = skipped
        year["source_bout_rows"] = parsed
        year["accepted_bout_coverage"] = int(year["bouts"]) / parsed if parsed else None


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
        source_snapshot = connection.execute(
            """SELECT source_url, source_revision_id, license_name, license_url,
                      source_observed_at_utc, reviewed_by
               FROM card_event_snapshots WHERE event_id = ?
               ORDER BY source_observed_at_utc DESC, event_snapshot_id DESC LIMIT 1""",
            (event["event_id"],),
        ).fetchone()
        source_evidence = (
            {key: source_snapshot[key] for key in source_snapshot.keys()}
            if source_snapshot is not None else None
        )
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
            "source_evidence": source_evidence,
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


def _valid_prior_result_split(value: object, expected_bouts: int, threshold: float) -> bool:
    if not isinstance(value, dict) or value.get("bouts") != expected_bouts or not _nonnegative_int(value.get("bouts")):
        return False
    keys = ("available_prior_result_instances", "observed_prior_result_instances",
            "unverified_feature_rows")
    if not all(_nonnegative_int(value.get(key)) for key in keys):
        return False
    available = value["available_prior_result_instances"]
    observed = value["observed_prior_result_instances"]
    unknown = value["unverified_feature_rows"]
    if observed > available or unknown > expected_bouts:
        return False
    expected_coverage = observed / available if available else None
    coverage = value.get("coverage")
    if expected_coverage is None:
        if coverage is not None:
            return False
    elif (_report_number(coverage) is None or not 0 <= coverage <= 1
          or not math.isclose(coverage, expected_coverage, rel_tol=0, abs_tol=1e-12)):
        return False
    meets = bool(available and unknown == 0 and expected_coverage is not None
                 and expected_coverage >= threshold)
    return type(value.get("meets_threshold")) is bool and value["meets_threshold"] == meets


def _valid_prior_result_evidence(
    value: object, split_bouts: dict[str, int] | None, status: str,
    *, available_bouts: int | None = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    threshold = _report_number(value.get("threshold"))
    if threshold is None or not math.isclose(
        threshold, MIN_PRIOR_RESULT_EVIDENCE_COVERAGE, rel_tol=0, abs_tol=1e-12
    ):
        return False
    if type(value.get("promotion_eligible")) is not bool:
        return False
    if split_bouts is None:
        return (status == "insufficient_history" and value["promotion_eligible"] is False
                and available_bouts is not None
                and _valid_prior_result_split(value.get("available"), available_bouts, threshold))
    if not all(_valid_prior_result_split(value.get(name), bouts, threshold)
               for name, bouts in split_bouts.items()):
        return False
    promoted = all(value[name]["meets_threshold"] for name in split_bouts)
    if value["promotion_eligible"] != promoted:
        return False
    if status != ("ok" if promoted else "insufficient_result_evidence"):
        return False
    warning = value.get("warning")
    return warning is None if promoted else isinstance(warning, str) and bool(warning.strip())


def _valid_evaluation_result(result: dict[str, Any]) -> bool:
    """Check the saved metric schema before exposing a report as evidence."""
    if result.get("status") == "insufficient_history":
        available = result.get("available")
        return (result.get("split") is None and result.get("test") is None
                and isinstance(available, dict)
                and _nonnegative_int(available.get("bouts"))
                and _valid_prior_result_evidence(
                    result.get("prior_result_evidence"), None, "insufficient_history",
                    available_bouts=available["bouts"],
                ))
    if result.get("status") not in {"ok", "insufficient_result_evidence"}:
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
    if not _valid_prior_result_evidence(
        result.get("prior_result_evidence"),
        {name: summaries[name]["bouts"] for name in summaries}, result["status"],
    ):
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


def _research_source_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    """Recompute the exact accepted-result fingerprint used by the saved report."""
    digest = hashlib.sha256()
    events: set[str] = set()
    counts = {"win": 0, "draw": 0, "no_contest": 0}
    source = _source_results(connection)
    fields = (
        "event_id", "event_date", "bout_id", "fighter_a_id",
        "fighter_b_id", "outcome", "winner_fighter_id",
    )
    for row in source:
        events.add(str(row["event_id"]))
        outcome = str(row["outcome"])
        if outcome not in counts:
            raise ValueError("Unexpected research outcome")
        counts[outcome] += 1
        digest.update(json.dumps(
            [row[key] for key in fields], separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8") + b"\n")
    return {
        "accepted_events_with_results": len(events),
        "accepted_bouts_with_results": len(source),
        "outcomes": counts,
        "accepted_source_rows_sha256": digest.hexdigest(),
    }


def _valid_research_report(saved: object) -> bool:
    """Accept only the narrow retrospective schema; never promote its scores."""
    if not isinstance(saved, dict):
        return False
    if (saved.get("schema_version") != 1
            or saved.get("evaluation_version") != RESEARCH_EVALUATION_VERSION
            or saved.get("status") != "research_only"
            or saved.get("research_only") is not True
            or saved.get("promotion_eligible") is not False
            or saved.get("source") != RESEARCH_SOURCE):
        return False
    source = saved.get("source_summary")
    if not isinstance(source, dict):
        return False
    if not all(_nonnegative_int(source.get(key)) for key in (
        "accepted_events_with_results", "accepted_bouts_with_results",
    )):
        return False
    digest = source.get("accepted_source_rows_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return False
    counts = source.get("outcomes")
    if (not isinstance(counts, dict)
            or not all(_nonnegative_int(counts.get(key)) for key in ("win", "draw", "no_contest"))
            or sum(counts[key] for key in ("win", "draw", "no_contest"))
            != source["accepted_bouts_with_results"]):
        return False
    splits = saved.get("split")
    if not isinstance(splits, dict):
        return False
    summaries: dict[str, dict[str, Any]] = {}
    for name in ("train", "validation", "test"):
        summary = splits.get(name)
        if (not isinstance(summary, dict)
                or not all(_nonnegative_int(summary.get(key)) for key in (
                    "binary_bouts", "events", "event_dates",
                ))
                or not 0 < summary["event_dates"] <= summary["events"] <= summary["binary_bouts"]):
            return False
        try:
            first = date.fromisoformat(summary["first_date"])
            last = date.fromisoformat(summary["last_date"])
        except (KeyError, TypeError, ValueError):
            return False
        if first > last:
            return False
        summaries[name] = summary
    if (summaries["train"]["last_date"] >= summaries["validation"]["first_date"]
            or summaries["validation"]["last_date"] >= summaries["test"]["first_date"]
            or sum(summaries[name]["binary_bouts"] for name in summaries) != counts["win"]):
        return False
    test_bouts = summaries["test"]["binary_bouts"]
    test = saved.get("test")
    if not isinstance(test, dict):
        return False
    for name in ("elo", "logistic_calibrated"):
        model = test.get(name)
        if (not isinstance(model, dict)
                or not _valid_metrics(model.get("metrics"), test_bouts)
                or not _valid_calibration_bins(model.get("calibration_bins"), test_bouts)):
            return False
    calibration = saved.get("calibration")
    if (not isinstance(calibration, dict)
            or calibration.get("method") != "symmetric_temperature_on_validation_only"
            or calibration.get("validation_binary_bouts") != summaries["validation"]["binary_bouts"]
            or type(calibration.get("applied")) is not bool
            or (_report_number(calibration.get("scale")) or 0) <= 0):
        return False
    uncertainty = saved.get("test_uncertainty")
    if (not isinstance(uncertainty, dict)
            or uncertainty.get("method") != "paired_event_date_block_bootstrap"
            or not _nonnegative_int(uncertainty.get("test_event_date_blocks"))
            or uncertainty["test_event_date_blocks"] != summaries["test"]["event_dates"]):
        return False
    for key in (
        "logistic_minus_elo_brier_score_95pct_interval",
        "logistic_minus_elo_log_loss_95pct_interval",
    ):
        interval = uncertainty.get(key)
        if (not isinstance(interval, list) or len(interval) != 2
                or any(_report_number(value) is None for value in interval)
                or interval[0] > interval[1]):
            return False
    book = saved.get("bookmaker")
    if (not isinstance(book, dict)
            or book.get("status") != "not_evaluated_without_historical_point_in_time_prices"
            or book.get("roi") is not None):
        return False
    identity = saved.get("identity_coverage")
    if not isinstance(identity, dict):
        return False
    held = identity.get("held_source_bout_rows")
    if held is not None and not _nonnegative_int(held):
        return False
    by_period = identity.get("by_split_dates")
    if held is None:
        return by_period is None
    if not isinstance(by_period, dict):
        return False
    for name in ("train", "validation", "test"):
        period = by_period.get(name)
        if (not isinstance(period, dict)
                or not all(_nonnegative_int(period.get(key)) for key in (
                    "completed_events", "accepted_bouts_all_outcomes", "held_source_bout_rows",
                ))):
            return False
        imported = period["accepted_bouts_all_outcomes"]
        omitted = period["held_source_bout_rows"]
        share = _report_number(period.get("accepted_share_of_accepted_plus_held"))
        if imported + omitted == 0 or share is None or abs(share - imported / (imported + omitted)) > 1e-9:
            return False
    return True


def _research_period_coverage_from_receipts(
    connection: sqlite3.Connection, splits: dict[str, Any], cutoff_date: str,
) -> dict[str, dict[str, int]]:
    """Independently count each split's accepted and held rows from checked receipts."""
    latest: dict[tuple[int, str], int] = {}
    for row in connection.execute(
        """SELECT w.event_page_id, w.page_url, w.skipped_unresolved_bouts
           FROM wikipedia_source_receipts w
           JOIN ingestion_runs i ON i.run_id = w.run_id
           WHERE i.source = 'wikipedia_research'
           ORDER BY w.run_id"""
    ):
        key = _research_page_receipt_key(row["event_page_id"], row["page_url"])
        if key is not None:
            latest[key] = int(row["skipped_unresolved_bouts"])
    periods = {
        name: {"completed_events": 0, "accepted_bouts_all_outcomes": 0,
               "held_source_bout_rows": 0}
        for name in ("train", "validation", "test")
    }
    for event in connection.execute(
        """SELECT e.source_event_id, e.event_date, COUNT(r.bout_id) AS accepted_bouts
           FROM events e
           LEFT JOIN bouts b ON b.event_id = e.event_id
               AND b.source = 'wikipedia_research' AND b.status = 'completed'
           LEFT JOIN results r ON r.bout_id = b.bout_id
           WHERE e.source = 'wikipedia_research' AND e.status = 'completed'
               AND e.event_date <= ?
           GROUP BY e.event_id""", (cutoff_date,),
    ):
        key = _research_event_receipt_key(event["source_event_id"])
        if key is None or key not in latest:
            raise ValueError("Research event receipt disappeared during report check")
        for name, split in splits.items():
            if split["first_date"] <= event["event_date"] <= split["last_date"]:
                period = periods[name]
                period["completed_events"] += 1
                period["accepted_bouts_all_outcomes"] += int(event["accepted_bouts"])
                period["held_source_bout_rows"] += latest[key]
                break
    return periods


def _research_evaluation_report(
    path: str | Path | None, connection: sqlite3.Connection,
    history: HistoricalResearchView, as_of: datetime,
) -> dict[str, Any]:
    if path is None:
        return {"status": "unavailable", "reason": "No research holdout report was supplied."}
    report_path = Path(path)
    if not report_path.is_file():
        return {"status": "missing_report", "reason": f"Research holdout report is missing: {report_path}"}
    try:
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        json.dumps(saved, allow_nan=False)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"status": "invalid_report", "reason": "Research holdout report is not valid JSON."}
    if not _valid_research_report(saved):
        return {"status": "invalid_report", "reason": "Research holdout report has invalid metrics, splits, or provenance."}
    if history["receipt_status"] != "verified":
        return {"status": "unverified_source", "reason": "Research source receipts are unavailable or no longer reconcile."}
    if saved["split"]["test"]["last_date"] > as_of.date().isoformat():
        return {"status": "future_data", "reason": "Research holdout report includes outcomes after the dashboard date."}
    future_events = connection.execute(
        "SELECT 1 FROM events WHERE source = ? AND status = 'completed' AND event_date > ? LIMIT 1",
        (RESEARCH_SOURCE, as_of.date().isoformat()),
    ).fetchone()
    if future_events is not None:
        return {"status": "future_data", "reason": "Research database includes completed events after the dashboard date."}
    current = _research_source_summary(connection)
    reported = saved["source_summary"]
    if any(reported.get(key) != current[key] for key in current):
        return {"status": "stale_report", "reason": "Research results changed after this holdout report; rerun it."}
    held = saved["identity_coverage"].get("held_source_bout_rows")
    if held is not None and held != history["held_identity_rows"]:
        return {"status": "stale_report", "reason": "Research identity holds differ from checked source receipts; rerun the holdout."}
    if held is not None:
        current_periods = _research_period_coverage_from_receipts(
            connection, saved["split"], as_of.date().isoformat(),
        )
        saved_periods = saved["identity_coverage"]["by_split_dates"]
        if any(any(saved_periods[name][key] != value for key, value in current_periods[name].items())
               for name in current_periods):
            return {"status": "stale_report", "reason": "Research date-period coverage differs from checked source receipts; rerun the holdout."}
    return {
        "status": "available", "research_only": True, "promotion_eligible": False,
        "source_path": str(report_path),
        "split": saved["split"], "test": saved["test"],
        "test_uncertainty": saved["test_uncertainty"],
        "identity_coverage": saved["identity_coverage"],
        "accepted_source_rows_sha256": current["accepted_source_rows_sha256"],
    }


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
            or result.get("status") not in {"ok", "insufficient_history", "insufficient_result_evidence"}
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
            "available" if result["status"] == "ok" else result["status"]
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


class _ForwardEvidenceError(ValueError):
    def __init__(self, status: str, reason: str):
        self.status = status
        super().__init__(reason)


def _forward_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _forward_sha(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _forward_file(root: Path, location: str | Path, expected_sha: object) -> Path:
    """Check one local proof without allowing a report to read outside the project."""
    if not _forward_sha(expected_sha):
        raise _ForwardEvidenceError("invalid_report", "A forward report has an invalid source digest.")
    try:
        path = Path(location).expanduser()
        if not path.is_absolute():
            path = root / path
        if path.is_symlink() or not path.is_file():
            raise OSError("missing or linked proof")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise OSError("proof is outside project root")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_sha:
            raise OSError("source digest changed")
        return resolved
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise _ForwardEvidenceError(
            "stale_evidence", "A local UFC 332 source or receipt no longer matches the saved report."
        ) from exc


def _forward_json(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("missing or linked JSON")
        saved = json.loads(path.read_text(encoding="utf-8"))
        json.dumps(saved, allow_nan=False)
        if not isinstance(saved, dict):
            raise ValueError("JSON object required")
        return saved
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _ForwardEvidenceError("invalid_report", "A saved UFC 332 report is unreadable or invalid JSON.") from exc


def _forward_referenced_file(root: Path, manifest_path: Path,
                             location: object, expected_sha: object) -> Path:
    """Resolve a saved path beside its manifest, or the older repo-relative path."""
    if not isinstance(location, str) or not location.strip():
        raise _ForwardEvidenceError("invalid_report", "A forward source path is missing.")
    candidate = Path(location).expanduser()
    if not candidate.is_absolute():
        beside = manifest_path.parent / candidate
        candidate = beside if beside.exists() or beside.is_symlink() else root / candidate
    return _forward_file(root, candidate, expected_sha)


def _forward_probability(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value < 1)


def _forward_canonical_utc(value: object) -> datetime | None:
    parsed = _timestamp(value)
    return parsed if (parsed is not None and isinstance(value, str)
                      and parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value) else None


def _forward_capture_receipts(root: Path, card: dict, odds: dict,
                              odds_manifest_path: Path, cutoff: str) -> bool:
    """Check captured network times and exact hashes without trusting report flags."""
    source = card.get("source") or {}
    lookup = card.get("fighter_lookup") or {}
    sections = (source, lookup, odds)
    has_any = any(any(key in item for key in (
        "receipt_path", "receipt_sha256", "fetched_at_utc")) for item in sections)
    if not has_any and cutoff == _UFC332_CAPTURE:
        return False  # Preserve the older sealed research replay, visibly unproved.
    if (not all(isinstance(item, dict) and item.get("receipt_path")
                and _forward_sha(item.get("receipt_sha256")) for item in sections)
            or not isinstance(source.get("api_url"), str)
            or not isinstance(lookup.get("api_url"), str)):
        raise _ForwardEvidenceError("invalid_report", "Fresh card or odds capture receipt is missing.")
    for url, path in ((source["api_url"], "/w/rest.php/v1/page/"),
                      (lookup["api_url"], "/w/api.php")):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "en.wikipedia.org"
                or parsed.username or parsed.password or parsed.port is not None
                or not parsed.path.startswith(path)):
            raise _ForwardEvidenceError("invalid_report", "Fresh MediaWiki receipt URL is invalid.")
    source_receipt = _forward_json(_forward_file(
        root, source["receipt_path"], source["receipt_sha256"]))
    lookup_receipt = _forward_json(_forward_file(
        root, lookup["receipt_path"], lookup["receipt_sha256"]))
    odds_receipt = _forward_json(_forward_referenced_file(
        root, odds_manifest_path, odds["receipt_path"], odds["receipt_sha256"]))
    for info, receipt in ((source, source_receipt), (lookup, lookup_receipt)):
        if (receipt.get("schema_version") != 1
                or receipt.get("request_url") != info["api_url"]
                or receipt.get("response_sha256") != info.get("raw_sha256")
                or receipt.get("fetched_at_utc") != info.get("fetched_at_utc")):
            raise _ForwardEvidenceError("invalid_report", "MediaWiki capture receipt differs from its response.")
    if (odds_receipt.get("schema_version") != 1
            or odds_receipt.get("provider") != "the-odds-api"
            or odds_receipt.get("sport_key") != "mma_mixed_martial_arts"
            or odds_receipt.get("market") != "h2h"
            or odds_receipt.get("region") != odds.get("region")
            or odds_receipt.get("response_sha256") != odds.get("sha256")
            or odds_receipt.get("fetched_at_utc") != odds.get("captured_at_utc")
            or "request_url" in odds_receipt or "apiKey" in odds_receipt):
        raise _ForwardEvidenceError("invalid_report", "Odds capture receipt differs from its response.")
    source_time = _forward_canonical_utc(source.get("fetched_at_utc"))
    lookup_time = _forward_canonical_utc(lookup.get("fetched_at_utc"))
    cutoff_time = _forward_canonical_utc(cutoff)
    start_time = _forward_canonical_utc((card.get("official_start_review") or {}).get(
        "event_start_utc_for_conservative_cutoff"))
    if (None in (source_time, lookup_time, cutoff_time, start_time)
            or source.get("fetched_at_utc") != card.get("captured_at_utc")
            or not source_time <= lookup_time <= cutoff_time < start_time):
        raise _ForwardEvidenceError("invalid_report", "Fresh capture times are out of order.")
    return True


def _forward_source_inputs(root: Path, strict: dict, full: dict,
                           files: dict[str, str], cutoff: str
                           ) -> tuple[dict, dict, set[int], bool]:
    """Bind both reports to the same saved card, selected CSV, and odds bytes."""
    inputs = strict["checked_inputs"]
    pairs = (
        ("card_manifest_sha256", "card_manifest"),
        ("selected_card_csv_sha256", "card_csv"),
        ("odds_manifest_sha256", "odds_manifest"),
        ("research_database_sha256", "research_db"),
    )
    for digest_key, file_key in pairs:
        full_key = "database_sha256" if file_key == "research_db" else digest_key
        if inputs.get(digest_key) != full.get(full_key):
            raise _ForwardEvidenceError("mismatched_reports", "Forward reports use different source evidence.")
        _forward_file(root, files[file_key], inputs.get(digest_key))
    card_path = root / files["card_manifest"]
    odds_path = root / files["odds_manifest"]
    card = _forward_json(card_path)
    odds = _forward_json(odds_path)
    for key, value in (
        ("card_source_sha256", card.get("source", {}).get("raw_sha256")),
        ("card_fighter_lookup_sha256", card.get("fighter_lookup", {}).get("raw_sha256")),
    ):
        if inputs.get(key) != value or full.get(key) != value:
            raise _ForwardEvidenceError("mismatched_reports", "Forward card source hashes do not agree.")
    if (inputs.get("odds_manifest_sha256") != card.get("odds_coverage_comparison", {}).get(
            "source_manifest_sha256")
            or inputs.get("odds_response_sha256") != odds.get("sha256")
            or full.get("odds_response_sha256") != odds.get("sha256")):
        raise _ForwardEvidenceError("mismatched_reports", "Forward odds source hashes do not agree.")
    _forward_referenced_file(root, card_path, card["source"]["raw_path"],
                             inputs["card_source_sha256"])
    _forward_referenced_file(root, card_path, card["fighter_lookup"]["raw_path"],
                             inputs["card_fighter_lookup_sha256"])
    _forward_referenced_file(root, odds_path, odds["raw_path"],
                             inputs["odds_response_sha256"])
    event = card.get("event") or {}
    start = (card.get("official_start_review") or {}).get(
        "event_start_utc_for_conservative_cutoff")
    if (event.get("source_page_id") != 83826247 or event.get("event_date") != _UFC332_EVENT_DATE
            or event.get("name") != strict.get("event_name")
            or event.get("name") != full.get("event_name")
            or event.get("source_revision_url") != strict.get("card_source_revision_url")
            or event.get("source_revision_url") != full.get("card_source_revision_url")
            or start != strict.get("event_start_at_utc")
            or start != full.get("event_start_at_utc")
            or card.get("captured_at_utc") != strict.get("card_captured_at_utc")
            or card.get("captured_at_utc") != full.get("card_captured_at_utc")
            or odds.get("captured_at_utc") != cutoff):
        raise _ForwardEvidenceError("mismatched_reports", "Forward card identity or capture time differs from source.")
    receipts_verified = _forward_capture_receipts(root, card, odds, odds_path, cutoff)
    rows = card.get("fight_card_rows")
    if (not isinstance(rows, list) or len(rows) != 13
            or [row.get("position") for row in rows if isinstance(row, dict)] != list(range(1, 14))):
        raise _ForwardEvidenceError("invalid_report", "Saved UFC 332 card positions are incomplete.")
    try:
        with (root / files["card_csv"]).open(
                encoding="utf-8-sig", newline="") as stream:
            selected_csv = list(csv.DictReader(stream))
        selected_positions = {int(row["source_position"]) for row in selected_csv}
        if len(selected_positions) != len(selected_csv) or len(selected_positions) != 8:
            raise ValueError("selected positions are not eight unique rows")
        for row in selected_csv:
            source = rows[int(row["source_position"]) - 1]
            for side in ("fighter_a", "fighter_b"):
                if (row.get(f"{side}_id") != source[side].get("stable_id")
                        or row.get(f"{side}_name") != source[side].get("name")):
                    raise ValueError("selected fighter differs from source")
    except (OSError, TypeError, KeyError, ValueError, IndexError) as exc:
        raise _ForwardEvidenceError("invalid_report", "Selected UFC 332 card rows do not match the source.") from exc
    return card, odds, selected_positions, receipts_verified


def _forward_strict_proofs(root: Path, strict: dict, manifest: dict,
                           files: dict[str, str], cutoff: str) -> None:
    inputs = strict["checked_inputs"]
    if inputs.get("historical_manifest_sha256") is None:
        raise _ForwardEvidenceError("invalid_report", "Strict report lacks its historical manifest hash.")
    _forward_file(root, files["historical_manifest"],
                  inputs["historical_manifest_sha256"])
    expected_events = manifest.get("events")
    proofs = inputs.get("historical_result_proofs")
    lookups = inputs.get("historical_identity_lookup_receipts")
    if (not isinstance(expected_events, list) or len(expected_events) != 23
            or not isinstance(proofs, list) or len(proofs) != len(expected_events)
            or not isinstance(lookups, list)
            or inputs.get("exact_cutoff_proof_failures") != []):
        raise _ForwardEvidenceError("invalid_report", "Strict report has incomplete result proof coverage.")
    marker = cutoff.replace(":", "").replace("-", "")
    for event, proof in zip(expected_events, proofs):
        if (not isinstance(event, dict) or not isinstance(proof, dict)
                or proof.get("slug") != event.get("slug")
                or proof.get("page_id") != event.get("page_id")
                or proof.get("cutoff_at_utc") != cutoff
                or (_timestamp(proof.get("revision_timestamp_utc")) is None)
                or _timestamp(proof["revision_timestamp_utc"]) > _timestamp(cutoff)):
            raise _ForwardEvidenceError("invalid_report", "Strict result revision differs from the exact cutoff.")
        for kind in ("selection", "content"):
            prefix = (f"data/raw/historical-revisions/{event['slug']}/"
                      f"result-{marker}.{kind}")
            sidecar = _forward_file(root, prefix + ".receipt.json",
                                    proof.get(f"{kind}_sidecar_sha256"))
            response = _forward_file(root, prefix + ".response.json",
                                     proof.get(f"{kind}_sha256"))
            saved_sidecar = _forward_json(sidecar)
            if (saved_sidecar.get("page_id") != event["page_id"]
                    or saved_sidecar.get("cutoff_utc") != cutoff
                    or saved_sidecar.get("role") != "result"
                    or saved_sidecar.get("response_kind") != kind
                    or saved_sidecar.get("response_sha256") != proof.get(f"{kind}_sha256")
                    or Path(str(saved_sidecar.get("response_path"))).resolve() != response
                    or saved_sidecar.get("fetched_at_utc") != proof.get(f"{kind}_fetched_at_utc")):
                raise _ForwardEvidenceError("invalid_report", "Strict result proof metadata differs from its saved files.")
    seen_runs: set[int] = set()
    for item in lookups:
        if (not isinstance(item, dict) or type(item.get("run_id")) is not int
                or item["run_id"] <= 0 or item["run_id"] in seen_runs
                or item.get("source") != "wikipedia_action_api"
                or _timestamp(item.get("fetched_at_utc")) is None
                or _timestamp(item["fetched_at_utc"]) > _timestamp(cutoff)):
            raise _ForwardEvidenceError("invalid_report", "Strict identity lookup evidence is invalid.")
        seen_runs.add(item["run_id"])
        _forward_file(root, item.get("payload_path"), item.get("payload_sha256"))


def _forward_full_history(root: Path, full: dict, files: dict[str, str],
                          cutoff: str) -> None:
    database = _forward_file(root, full.get("database_path"), full.get("database_sha256"))
    holdout = _forward_file(root, full.get("holdout_report_path"),
                            full.get("holdout_report_sha256"))
    if (database != (root / files["research_db"]).resolve()
            or holdout != (root / files["holdout_report"]).resolve()):
        raise _ForwardEvidenceError("mismatched_reports", "Full-history report belongs to another local source.")
    saved_holdout = _forward_json(holdout)
    if (saved_holdout.get("research_only") is not True
            or saved_holdout.get("promotion_eligible") is not False
            or saved_holdout.get("source_summary") != full.get("source_summary")):
        raise _ForwardEvidenceError("invalid_report", "Saved full-history holdout differs from its report.")
    catalog = full.get("receipt_catalog") or {}
    integrity = verify_evidence(database)
    if (not integrity.get("ok") or integrity.get("receipt_count") != catalog.get("ingestion_runs")
            or integrity.get("verified_receipts") != full.get("integrity_verified_receipts")):
        raise _ForwardEvidenceError("stale_evidence", "Full-history source receipts no longer verify.")
    digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    latest = ""
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            for run_id, source, fetched, stated_sha in connection.execute(
                    "SELECT run_id, source, fetched_at_utc, sha256 FROM ingestion_runs ORDER BY run_id"):
                if (_timestamp(fetched) is None
                        or _timestamp(fetched) >= _timestamp(cutoff)
                        or not _forward_sha(stated_sha)):
                    raise ValueError("research receipt is after cutoff or lacks a hash")
                counts[source] += 1
                latest = max(latest, fetched)
                digest.update(json.dumps([run_id, source, fetched, stated_sha],
                                         separators=(",", ":")).encode("utf-8") + b"\n")
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise _ForwardEvidenceError("stale_evidence", "Full-history receipt catalog could not be checked.") from exc
    if (digest.hexdigest() != catalog.get("catalog_sha256")
            or dict(sorted(counts.items())) != catalog.get("by_source")
            or latest != catalog.get("latest_fetch_at_utc")):
        raise _ForwardEvidenceError("stale_evidence", "Full-history receipt catalog changed.")
    _forward_file(root, full["database_path"], full["database_sha256"])


def load_ufc332_forward_research(
    strict_report: str | Path | None,
    full_report: str | Path | None,
    *,
    evidence_root: str | Path,
    evidence_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verify two saved UFC 332 research scenarios without fitting on page load."""
    if strict_report is None or full_report is None:
        return {"status": "unavailable", "reason": "Both saved UFC 332 research reports are required."}
    strict_path, full_path = Path(strict_report).expanduser(), Path(full_report).expanduser()
    if not strict_path.is_file() or not full_path.is_file():
        return {"status": "missing_report", "reason": "A saved UFC 332 forward report is missing."}
    try:
        root = Path(evidence_root).expanduser().resolve(strict=True)
        strict, full = _forward_json(strict_path), _forward_json(full_path)
        files = dict(_FORWARD_EVIDENCE_FILES)
        if evidence_files is not None:
            if not isinstance(evidence_files, dict) or any(
                    key not in {"card_manifest", "card_csv", "odds_manifest"}
                    or not isinstance(value, str) or not value
                    for key, value in evidence_files.items()):
                raise _ForwardEvidenceError("invalid_report", "Forward evidence paths are invalid.")
            files.update(evidence_files)
        cutoff = strict.get("history_cutoff_at_utc")
        if _forward_canonical_utc(cutoff) is None:
            raise _ForwardEvidenceError("invalid_report", "Forward odds cutoff is invalid.")
        expected = (_UFC332_EVENT_ID, _UFC332_EVENT_DATE, cutoff)
        if (strict.get("schema_version") != 1
                or strict.get("replay_version") != "archived-forward-research-v1"
                or strict.get("status") != "research_only"
                or strict.get("research_only") is not True
                or strict.get("model_eligible") is not False
                or strict.get("promotion_eligible") is not False
                or strict.get("alert_eligible") is not False
                or strict.get("paper_decisions_created") != 0
                or strict.get("bets_placed") != 0
                or strict.get("database_access") != "read_only"
                or (strict.get("event_id"), strict.get("event_date"),
                    strict.get("history_cutoff_at_utc")) != expected
                or strict.get("odds_captured_at_utc") != cutoff
                or strict.get("historical_result_selection_rule") !=
                    "latest_publisher_result_revision_at_exact_odds_capture_for_every_prior_event"):
            raise _ForwardEvidenceError("invalid_report", "Strict UFC 332 report is not a research-only exact-cutoff replay.")
        if (full.get("schema_version") != 1
                or full.get("version") != "captured-forward-research-v1"
                or full.get("scenario") != "captured_full_result_history"
                or full.get("status") != "research_only"
                or full.get("research_only") is not True
                or full.get("promotion_eligible") is not False
                or full.get("alert_eligible") is not False
                or (full.get("event_id"), full.get("target_event_date"),
                    full.get("cutoff_at_utc")) != expected
                or full.get("odds_captured_at_utc") != cutoff):
            raise _ForwardEvidenceError("invalid_report", "Full-history UFC 332 report is not research-only.")
        inputs = strict.get("checked_inputs")
        if (not isinstance(inputs, dict) or strict.get("checked_input_sha256") != _forward_digest(inputs)
                or strict.get("holds") != []):
            raise _ForwardEvidenceError("invalid_report", "Strict report input digest or hold status is invalid.")
        if strict.get("forecast_rows_sha256") != _forward_digest(strict.get("bouts")):
            raise _ForwardEvidenceError("invalid_report", "Strict forecast rows changed after the saved replay.")
        forecasts = full.get("forecasts")
        if not isinstance(forecasts, list) or len(forecasts) != 8:
            raise _ForwardEvidenceError("invalid_report", "Full-history report lacks eight selected forecasts.")
        if full.get("forecast_rows_sha256") != _forward_digest(forecasts):
            raise _ForwardEvidenceError("invalid_report", "Full-history forecast rows changed after the saved replay.")
        pairs = [{"position": row.get("position"), "fighter_a_id": row.get("fighter_a_id"),
                  "fighter_b_id": row.get("fighter_b_id")} for row in forecasts]
        catalog = full.get("receipt_catalog") or {}
        history_digest = _forward_digest({
            "cutoff_at_utc": full["cutoff_at_utc"],
            "target_event_date": full["target_event_date"],
            "pairs": pairs, "database_sha256": full.get("database_sha256"),
            "holdout_report_sha256": full.get("holdout_report_sha256"),
            "receipt_catalog_sha256": catalog.get("catalog_sha256"),
            "source_summary": full.get("source_summary"),
        })
        final_digest = _forward_digest({
            "history_checked_input_sha256": history_digest,
            **{key: full.get(key) for key in (
                "card_manifest_sha256", "card_source_sha256",
                "card_fighter_lookup_sha256", "selected_card_csv_sha256",
                "odds_manifest_sha256", "odds_response_sha256")},
        })
        if full.get("checked_input_sha256") != final_digest:
            raise _ForwardEvidenceError("invalid_report", "Full-history report input digest is invalid.")
        card, odds, selected, receipts_verified = _forward_source_inputs(
            root, strict, full, files, cutoff)
        manifest = _forward_json(root / files["historical_manifest"])
        _forward_strict_proofs(root, strict, manifest, files, cutoff)
        _forward_full_history(root, full, files, cutoff)
        strict_rows = strict.get("bouts")
        if (not isinstance(strict_rows, list) or len(strict_rows) != 13
                or [row.get("source_position") for row in strict_rows] != list(range(1, 14))
                or {row.get("source_position") for row in strict_rows
                    if row.get("selected_for_replay") is True} != selected
                or [row.get("position") for row in forecasts] != sorted(selected)
                or full.get("source_card_bouts") != 13
                or full.get("selected_card_bouts") != 8):
            raise _ForwardEvidenceError("invalid_report", "Forward reports do not cover the same eight card rows.")
        source_rows = card["fight_card_rows"]
        rows: list[dict[str, Any]] = []
        for forecast in forecasts:
            position = forecast["position"]
            source, strict_row = source_rows[position - 1], strict_rows[position - 1]
            if (forecast.get("alert_eligible") is not False
                    or strict_row.get("alert_eligible") is not False
                    or strict_row.get("forecast_status") != "exploratory_elo_only"
                    or strict_row.get("source_position") != position
                    or not _forward_probability(strict_row.get("elo_fighter_a_probability"))
                    or any(not _forward_probability(forecast.get(key)) for key in (
                        "elo_probability_fighter_a",
                        "logistic_raw_probability_fighter_a",
                        "logistic_calibrated_probability_fighter_a"))):
                raise _ForwardEvidenceError("invalid_report", "Forward probabilities or alert flags are invalid.")
            for side in ("fighter_a", "fighter_b"):
                if (strict_row.get(side) != source[side].get("name")
                        or forecast.get(side + "_name") != source[side].get("name")
                        or strict_row.get(side + "_id") != source[side].get("stable_id")
                        or forecast.get(side + "_id") != source[side].get("stable_id")):
                    raise _ForwardEvidenceError("mismatched_reports", "Forward fighter identities differ from the card.")
            books = strict_row.get("saved_two_sided_books")
            if (not isinstance(books, list) or books != forecast.get("saved_two_sided_books")
                    or any(not isinstance(book, dict) or book.get("executable_price_verified") is not False
                           or book.get("captured_at_utc") != cutoff
                           or not _forward_probability(book.get("fighter_a_no_vig_implied_probability"))
                           for book in books)):
                raise _ForwardEvidenceError("mismatched_reports", "Forward saved bookmaker observations differ.")
            rows.append({
                "source_position": position,
                "bout": f"{strict_row['fighter_a']} vs {strict_row['fighter_b']}",
                "strict_elo_probability_fighter_a": strict_row["elo_fighter_a_probability"],
                "captured_elo_probability_fighter_a": forecast["elo_probability_fighter_a"],
                "captured_logistic_probability_fighter_a": forecast[
                    "logistic_calibrated_probability_fighter_a"],
                "strict_prior_bouts": (
                    strict_row.get("fighter_a_prior_result_bouts_in_cohort"),
                    strict_row.get("fighter_b_prior_result_bouts_in_cohort")),
                "captured_prior_bouts": (
                    forecast.get("prior_binary_or_draw_bouts_fighter_a"),
                    forecast.get("prior_binary_or_draw_bouts_fighter_b")),
                "saved_two_sided_book_count": len(books),
                "alert_eligible": False,
            })
        identity_holds = [row["position"] for row in source_rows
                          if not row["fighter_a"].get("stable_id")
                          or not row["fighter_b"].get("stable_id")]
        strict_coverage = strict.get("coverage") or {}
        priced = sum(row["saved_two_sided_book_count"] > 0 for row in rows)
        if (full.get("source_identity_hold_positions") != identity_holds
                or strict_coverage.get("source_card_bouts") != 13
                or strict_coverage.get("stable_id_eligible_card_bouts") != 8
                or strict_coverage.get("selected_card_bouts") != 8
                or strict_coverage.get("historical_events_required") != 23
                or strict_coverage.get("historical_events_verified") != 23
                or strict_coverage.get("selected_bouts_with_exploratory_elo") != 8
                or strict_coverage.get("selected_bouts_with_two_sided_saved_books") != priced
                or full.get("selected_bouts_with_two_sided_saved_books") != priced):
            raise _ForwardEvidenceError("invalid_report", "Forward card coverage does not reconcile.")
        return {
            "status": "available", "research_only": True, "promotion_eligible": False,
            "alert_eligible": False, "event_id": _UFC332_EVENT_ID,
            "event_name": strict["event_name"], "event_date": _UFC332_EVENT_DATE,
            "cutoff_at_utc": cutoff,
            "capture_receipts_verified": receipts_verified,
            "card_captured_at_utc": card["captured_at_utc"],
            "fighter_lookup_fetched_at_utc": card["fighter_lookup"].get("fetched_at_utc"),
            "event_start_at_utc": strict["event_start_at_utc"],
            "strict_history_scope": {
                "events": 23, "source_result_bouts": strict_coverage["historical_source_result_bouts"],
                "stable_id_result_bouts": strict_coverage["historical_stable_id_result_bouts"],
            },
            "captured_history_scope": {
                "events": full["source_summary"]["accepted_events_with_results"],
                "accepted_result_bouts": full["source_summary"]["accepted_bouts_with_results"],
                "training_bouts": full["model"]["logistic_training_bouts"],
                "validation_bouts": full["model"]["calibration_validation_bouts"],
                "test_bouts": full["model"]["historical_test_bouts"],
            },
            "source_card_bouts": 13, "selected_card_bouts": 8,
            "identity_hold_positions": identity_holds,
            "selected_bouts_with_two_sided_saved_books": priced,
            "rows": rows,
            "source_revision_url": strict["card_source_revision_url"],
            "license_url": strict["card_license_url"],
            "strict_report_path": str(strict_path),
            "full_report_path": str(full_path),
            "strict_checked_input_sha256": strict["checked_input_sha256"],
            "full_checked_input_sha256": full["checked_input_sha256"],
        }
    except _ForwardEvidenceError as exc:
        return {"status": exc.status, "reason": str(exc)}
    except (OSError, TypeError, ValueError, KeyError, IndexError, sqlite3.Error) as exc:
        return {"status": "invalid_report", "reason": "Saved UFC 332 research evidence could not be verified."}


def load_dashboard(
    db_path: str | Path,
    *,
    as_of: datetime | None = None,
    event_limit: int = 8,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    evaluation_report: str | Path | None = None,
    research_report: str | Path | None = None,
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
            history = _historical_research(
                connection, as_of, has_receipts="wikipedia_source_receipts" in tables,
            )
            view: DashboardView = {
                "status": "available" if event_sources else "empty",
                "reason": None if event_sources else "Database has no events yet.",
                "data_origin": origin,
                "as_of_utc": utc_string(as_of),
                "upcoming_events": _upcoming_events(connection, as_of, event_limit, max_age_seconds),
                "historical_research": history,
                "quality": quality,
                "ingestion_runs": ingestion,
                "job_runs": _job_runs(connection, as_of),
                "paper_ledger": _paper_ledger(connection),
                "manual_ledger": _manual_ledger(connection),
                "evaluation": _evaluation_report(evaluation_report, path, as_of),
                "research_evaluation": _research_evaluation_report(
                    research_report, connection, history, as_of,
                ),
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
