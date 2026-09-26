"""Event scoring, honest quote selection, and walk-forward evaluation."""

from __future__ import annotations

import csv
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
) -> dict | None:
    """Choose from the latest available price for each book and fighter."""
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for quote in db.quotes_as_of(connection, bout["bout_id"], utc_string(as_of)):
        key = (quote["bookmaker"], quote["selection_fighter_id"])
        if key in seen:
            continue
        seen.add(key)
        captured = parse_utc(quote["captured_at_utc"])
        if as_of - captured > timedelta(hours=max_quote_age_hours):
            continue
        updated_at = quote["bookmaker_updated_at_utc"]
        if updated_at:
            updated = parse_utc(updated_at)
            if updated > as_of or as_of - updated > timedelta(hours=max_quote_age_hours):
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
            }
        )
    return max(candidates, key=lambda item: item["expected_profit_per_dollar"], default=None)


REPORT_FIELDS = [
    "event_id", "event_name", "bout_id", "fighter_a", "fighter_b",
    "model_version", "as_of_utc", "p_fighter_a", "p_fighter_b",
    "prediction_id", "quote_id", "bookmaker", "selection", "decimal_odds",
    "break_even_probability", "model_selection_probability",
    "expected_profit_per_dollar", "quote_captured_at_utc", "decision",
]


def score_event(
    connection: sqlite3.Connection,
    event_id: str,
    as_of: datetime,
    report_dir: str | Path = "reports",
    min_expected_profit: float = 0.03,
    max_quote_age_hours: float = 24.0,
) -> tuple[Path, list[dict]]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Prediction cutoff needs a timezone")
    as_of = as_of.astimezone(timezone.utc)
    event = connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if event is None:
        raise ValueError(f"Unknown event: {event_id}")
    if event["status"] != "scheduled":
        raise ValueError("Only scheduled events can receive live predictions")
    if event["start_time_utc"] and as_of >= parse_utc(event["start_time_utc"]):
        raise ValueError("The prediction cutoff must precede the event start time")
    bouts = db.event_bouts(connection, event_id)
    if not bouts:
        raise ValueError("This event has no scheduled bouts")
    history_before = min(event["event_date"], as_of.date().isoformat())
    model = model_from_results(db.prior_results(connection, history_before))
    report_rows: list[dict] = []
    now_str = utc_string(utc_now())
    as_of_str = utc_string(as_of)
    for bout in bouts:
        probability_a = model.probability(bout["fighter_a_id"], bout["fighter_b_id"])
        prediction_id = db.save_prediction(
            connection, bout["bout_id"], MODEL_VERSION, as_of_str, now_str, probability_a
        )
        quote = best_quote(connection, bout, probability_a, as_of, max_quote_age_hours)
        row = {
            "event_id": event_id,
            "event_name": event["name"],
            "bout_id": bout["bout_id"],
            "fighter_a": bout["fighter_a_name"],
            "fighter_b": bout["fighter_b_name"],
            "model_version": MODEL_VERSION,
            "as_of_utc": as_of_str,
            "p_fighter_a": round(probability_a, 4),
            "p_fighter_b": round(1 - probability_a, 4),
            "prediction_id": prediction_id,
            "quote_id": quote["quote_id"] if quote else "",
            "bookmaker": quote["bookmaker"] if quote else "",
            "selection": quote["selection_name"] if quote else "",
            "decimal_odds": quote["decimal_odds"] if quote else "",
            "break_even_probability": round(quote["break_even_probability"], 4) if quote else "",
            "model_selection_probability": round(quote["model_probability"], 4) if quote else "",
            "expected_profit_per_dollar": round(quote["expected_profit_per_dollar"], 4) if quote else "",
            "quote_captured_at_utc": quote["captured_at_utc"] if quote else "",
            "decision": (
                "candidate" if quote and quote["expected_profit_per_dollar"] >= min_expected_profit
                else "pass" if quote else "no_quote"
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
    """Predict later events; simulate $1 bets only where historical quotes exist."""
    events = connection.execute(
        "SELECT event_id, event_date, start_time_utc FROM events WHERE status = 'completed' "
        "ORDER BY event_date, event_id"
    ).fetchall()
    probabilities: list[float] = []
    outcomes: list[int] = []
    skipped_non_binary = 0
    paper_bets = 0
    paper_profit = 0.0
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
            if bout["outcome"] != "win":
                skipped_non_binary += 1
                continue
            probability_a = model.probability(bout["fighter_a_id"], bout["fighter_b_id"])
            probabilities.append(probability_a)
            outcomes.append(int(bout["winner_fighter_id"] == bout["fighter_a_id"]))
            if decision_time:
                quote = best_quote(connection, bout, probability_a, decision_time, max_quote_age_hours)
                if quote and quote["expected_profit_per_dollar"] >= min_expected_profit:
                    paper_bets += 1
                    paper_profit += (
                        quote["decimal_odds"] - 1.0
                        if quote["selection_fighter_id"] == bout["winner_fighter_id"]
                        else -1.0
                    )
    count = len(outcomes)
    if not count:
        return {"bouts": 0, "skipped_draw_or_no_contest": skipped_non_binary,
                "accuracy": None, "brier_score": None, "log_loss": None,
                "paper_bets": 0, "paper_profit_units": None, "paper_roi": None}
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / count
    log_loss = -sum(
        y * math.log(max(min(p, 1 - 1e-15), 1e-15))
        + (1 - y) * math.log(max(min(1 - p, 1 - 1e-15), 1e-15))
        for p, y in zip(probabilities, outcomes)
    ) / count
    accuracy = sum(int((p >= 0.5) == bool(y)) for p, y in zip(probabilities, outcomes)) / count
    return {
        "bouts": count,
        "skipped_draw_or_no_contest": skipped_non_binary,
        "accuracy": round(accuracy, 4),
        "brier_score": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "paper_bets": paper_bets,
        "paper_profit_units": round(paper_profit, 4) if paper_bets else None,
        "paper_roi": round(paper_profit / paper_bets, 4) if paper_bets else None,
    }
