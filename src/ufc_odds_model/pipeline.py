"""Event scoring, honest quote selection, and walk-forward evaluation."""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .elo import MODEL_VERSION, model_from_results


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp needs a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def best_quote(
    connection: sqlite3.Connection,
    bout: sqlite3.Row,
    p_fighter_a: float,
    as_of: datetime,
    max_quote_age_hours: float,
    required_snapshot_at_utc: str | None = None,
) -> dict | None:
    """Choose from the latest available price for each book and fighter."""
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for quote in db.quotes_as_of(connection, bout["bout_id"], utc_string(as_of)):
        if required_snapshot_at_utc is not None and quote["captured_at_utc"] != required_snapshot_at_utc:
            continue
        key = (quote["bookmaker"], quote["selection_fighter_id"])
        if key in seen:
            continue
        seen.add(key)
        captured = parse_utc(quote["captured_at_utc"])
        if as_of - captured > timedelta(hours=max_quote_age_hours):
            continue
        updated_at = quote["bookmaker_updated_at_utc"]
        if required_snapshot_at_utc is not None and not updated_at:
            continue
        if updated_at:
            updated = parse_utc(updated_at)
            if updated > captured or updated > as_of or as_of - updated > timedelta(hours=max_quote_age_hours):
                continue
        if quote["selection_fighter_id"] == bout["fighter_a_id"]:
            probability = p_fighter_a
        elif quote["selection_fighter_id"] == bout["fighter_b_id"]:
            probability = 1.0 - p_fighter_a
        else:
            continue
        decimal_odds = float(quote["decimal_odds"])
        candidates.append(
            {
                "quote_id": quote["quote_id"],
                "bookmaker": quote["bookmaker"],
                "selection_fighter_id": quote["selection_fighter_id"],
                "selection_name": quote["selection_name"],
                "decimal_odds": decimal_odds,
                "break_even_probability": 1.0 / decimal_odds,
                "model_probability": probability,
                "expected_profit_per_dollar": probability * decimal_odds - 1.0,
                "captured_at_utc": quote["captured_at_utc"],
                "bookmaker_updated_at_utc": updated_at,
            }
        )
    return max(candidates, key=lambda item: item["expected_profit_per_dollar"], default=None)


REPORT_FIELDS = [
    "event_id", "event_name", "bout_id", "fighter_a", "fighter_b",
    "model_version", "as_of_utc", "p_fighter_a", "p_fighter_b",
    "prediction_id", "quote_id", "bookmaker", "selection", "decimal_odds",
    "break_even_probability", "model_selection_probability",
    "expected_profit_per_dollar", "quote_captured_at_utc", "decision",
    "bookmaker_updated_at_utc", "feature_coverage_json", "history_evidence_status",
]


def score_event(
    connection: sqlite3.Connection,
    event_id: str,
    as_of: datetime,
    report_dir: str | Path = "reports",
    min_expected_profit: float = 0.03,
    max_quote_age_hours: float = 24.0,
    model_kind: str = "elo",
    model_dir: str | Path = "models",
    required_snapshot_at_utc: str | None = None,
) -> tuple[Path, list[dict]]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Prediction cutoff needs a timezone")
    as_of = as_of.astimezone(timezone.utc)
    # An old, manually supplied cutoff is a retrospective replay. Its current
    # result table may contain later corrections, so it cannot create a bet
    # candidate even if a saved quote makes the arithmetic look attractive.
    historical_replay = as_of < utc_now() - timedelta(minutes=5)
    event = connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if event is None:
        raise ValueError(f"Unknown event: {event_id}")
    if event["status"] != "scheduled":
        raise ValueError("Only scheduled events can receive live predictions")
    if event["provider_status"] not in (None, "scheduled", "not_started"):
        raise ValueError("The provider reports this event is not available for pre-fight scoring")
    if event["start_time_utc"] and as_of >= parse_utc(event["start_time_utc"]):
        raise ValueError("The prediction cutoff must precede the event start time")
    bouts = db.event_bouts(connection, event_id)
    if not bouts:
        raise ValueError("This event has no scheduled bouts")
    history_before = min(event["event_date"], as_of.date().isoformat())
    coverage_by_bout: dict[str, dict[str, bool]] = {}
    if model_kind == "elo":
        model = model_from_results(db.prior_results(connection, history_before))
        model_version = MODEL_VERSION
        probabilities = {
            bout["bout_id"]: model.probability(bout["fighter_a_id"], bout["fighter_b_id"])
            for bout in bouts
        }
    elif model_kind == "logistic":
        from .features import FEATURE_NAMES
        from .live_logistic import prepare_live_logistic
        run = prepare_live_logistic(connection, event_id, as_of)
        model_version = run.model_version
        probabilities = run.predictions
        coverage_by_bout = run.coverage_by_bout
        artifact = {
            "model_version": model_version,
            "feature_cutoff_date": run.history_before_date,
            "feature_names": FEATURE_NAMES,
            "weights": run.model.weights,
            "calibration_scale": run.calibrator.scale,
            "training_bouts": run.training_bouts,
            "training_event_dates": run.training_event_dates,
            "training_last_date": run.training_last_date,
            "calibration_bouts": run.calibration_bouts,
            "calibration_event_dates": run.calibration_event_dates,
            "calibration_first_date": run.calibration_first_date,
            "calibration_last_date": run.calibration_last_date,
            "observation_coverage": run.observation_coverage,
            "prior_result_evidence": run.prior_result_evidence,
            "coverage_by_bout": run.coverage_by_bout,
        }
        directory = Path(model_dir)
        directory.mkdir(parents=True, exist_ok=True)
        artifact_path = directory / f"{model_version.replace(':', '_')}.json"
        artifact_text = json.dumps(artifact, indent=2) + "\n"
        if artifact_path.exists() and artifact_path.read_text(encoding="utf-8") != artifact_text:
            raise ValueError("Saved logistic model version conflicts with existing artifact")
        artifact_path.write_text(artifact_text, encoding="utf-8")
    else:
        raise ValueError("model_kind must be elo or logistic")
    report_rows: list[dict] = []
    now_str = utc_string(utc_now())
    as_of_str = utc_string(as_of)
    for bout in bouts:
        probability_a = probabilities[bout["bout_id"]]
        prediction_id = db.save_prediction(
            connection, bout["bout_id"], model_version, as_of_str, now_str, probability_a
        )
        coverage_json = (
            json.dumps(coverage_by_bout[bout["bout_id"]], sort_keys=True)
            if bout["bout_id"] in coverage_by_bout else None
        )
        connection.execute(
            "UPDATE predictions SET feature_coverage_json = ? WHERE prediction_id = ?",
            (coverage_json, prediction_id),
        )
        quote = best_quote(
            connection, bout, probability_a, as_of, max_quote_age_hours,
            required_snapshot_at_utc=required_snapshot_at_utc,
        )
        row = {
            "event_id": event_id,
            "event_name": event["name"],
            "bout_id": bout["bout_id"],
            "fighter_a": bout["fighter_a_name"],
            "fighter_b": bout["fighter_b_name"],
            "model_version": model_version,
            "as_of_utc": as_of_str,
            "p_fighter_a": round(probability_a, 4),
            "p_fighter_b": round(1 - probability_a, 4),
            "prediction_id": prediction_id,
            "quote_id": quote["quote_id"] if quote else "",
            "bookmaker": quote["bookmaker"] if quote else "",
            "selection": quote["selection_name"] if quote else "",
            "decimal_odds": quote["decimal_odds"] if quote else "",
            "break_even_probability": round(quote["break_even_probability"], 4) if quote else "",
            "model_selection_probability": quote["model_probability"] if quote else "",
            "expected_profit_per_dollar": quote["expected_profit_per_dollar"] if quote else "",
            "quote_captured_at_utc": quote["captured_at_utc"] if quote else "",
            "bookmaker_updated_at_utc": quote["bookmaker_updated_at_utc"] if quote else "",
            "decision": (
                "research_only" if historical_replay and quote
                else "candidate" if quote and quote["expected_profit_per_dollar"] >= min_expected_profit
                else "pass" if quote else "no_quote"
            ),
            "feature_coverage_json": coverage_json or "",
            "history_evidence_status": (
                "unverified_historical_replay" if historical_replay else "current_cutoff"
            ),
        }
        report_rows.append(row)
    connection.commit()
    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_event_id = re.sub(r"[^A-Za-z0-9_-]", "_", event_id)
    file_path = output_dir / f"{safe_event_id}_{as_of.strftime('%Y%m%dT%H%M%SZ')}.csv"
    with file_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report_rows)
    return file_path, report_rows


def walk_forward_backtest(
    connection: sqlite3.Connection,
    min_prior_results: int = 0,
    decision_hours_before_event: float = 24.0,
    min_expected_profit: float = 0.03,
    max_quote_age_hours: float = 24.0,
) -> dict[str, float | int | None]:
    """Explore date-ordered Elo; returns are unavailable without decision receipts.

    This legacy replay reads today's roster/results and quote rows without a
    direct quote-to-source-receipt link. Its candidate count is diagnostic,
    never a verified betting record. Use chronological ``evaluate`` and the
    prospective paper ledger for decision evidence and returns.
    """
    events = connection.execute(
        "SELECT event_id, event_date, start_time_utc FROM events WHERE status = 'completed' "
        "ORDER BY event_date, event_id"
    ).fetchall()
    probabilities: list[float] = []
    outcomes: list[int] = []
    skipped_non_binary = 0
    paper_bets = 0
    unresolved_paper_bets = 0
    for event in events:
        decision_time = (
            parse_utc(event["start_time_utc"]) - timedelta(hours=decision_hours_before_event)
            if event["start_time_utc"] else None
        )
        history_before = min(event["event_date"], decision_time.date().isoformat()) if decision_time else event["event_date"]
        prior = db.prior_results(connection, history_before)
        if len(prior) < min_prior_results:
            continue
        model = model_from_results(prior)
        bouts = connection.execute(
            """
            SELECT b.*, r.outcome, r.winner_fighter_id
            FROM bouts b JOIN results r ON r.bout_id = b.bout_id
            WHERE b.event_id = ?
            """,
            (event["event_id"],),
        ).fetchall()
        for bout in bouts:
            probability_a = model.probability(bout["fighter_a_id"], bout["fighter_b_id"])
            # The wager decision must be made before inspecting its result.
            # Draws and no contests need bookmaker-specific manual settlement.
            if decision_time:
                quote = best_quote(connection, bout, probability_a, decision_time, max_quote_age_hours)
                if quote and quote["expected_profit_per_dollar"] >= min_expected_profit:
                    paper_bets += 1
                    if bout["outcome"] != "win":
                        unresolved_paper_bets += 1
            if bout["outcome"] != "win":
                skipped_non_binary += 1
                continue
            # Some sources list the winner first after a fight. Evaluate against
            # a stable ID order so row orientation cannot reveal the label.
            first_id, second_id = sorted((bout["fighter_a_id"], bout["fighter_b_id"]))
            probabilities.append(model.probability(first_id, second_id))
            outcomes.append(int(bout["winner_fighter_id"] == first_id))
    count = len(outcomes)
    if not count:
        return {"status": "research_only", "bouts": 0,
                "paper_return_unavailable_reason": "unverified_historical_decision_evidence",
                "skipped_draw_or_no_contest": skipped_non_binary,
                "accuracy": None, "brier_score": None, "log_loss": None,
                "paper_bets": paper_bets,
                "paper_unresolved_bets": unresolved_paper_bets,
                "paper_profit_units": None, "paper_roi": None}
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / count
    log_loss = -sum(
        y * math.log(max(min(p, 1 - 1e-15), 1e-15))
        + (1 - y) * math.log(max(min(1 - p, 1 - 1e-15), 1e-15))
        for p, y in zip(probabilities, outcomes)
    ) / count
    accuracy = sum(int((p >= 0.5) == bool(y)) for p, y in zip(probabilities, outcomes)) / count
    return {
        "status": "research_only",
        "paper_return_unavailable_reason": "unverified_historical_decision_evidence",
        "bouts": count,
        "skipped_draw_or_no_contest": skipped_non_binary,
        "accuracy": round(accuracy, 4),
        "brier_score": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "paper_bets": paper_bets,
        "paper_unresolved_bets": unresolved_paper_bets,
        "paper_profit_units": None,
        "paper_roi": None,
    }
