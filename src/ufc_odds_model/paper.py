"""Capped, auditable paper decisions; this module never places real wagers."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from .card_history import latest_prefight_roster
from .pipeline import parse_utc, utc_now, utc_string
from .quote_evidence import linked_quote_rows, quote_receipt_matches


def _positive_finite(value: float, name: str, *, at_most_one: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0 or (at_most_one and number > 1):
        raise ValueError(f"{name} must be finite and in (0, 1]" if at_most_one
                         else f"{name} must be a finite positive number")
    return number


def _candidate_details(
    connection: sqlite3.Connection,
    row: Mapping,
    recorded_time: datetime,
    max_report_age_minutes: float,
    max_quote_age_hours: float,
) -> dict:
    try:
        prediction_id = int(row["prediction_id"])
        quote_id = int(row["quote_id"])
        gate_check_id = int(row["gate_check_id"])
        event_id = str(row["event_id"])
        bout_id = str(row["bout_id"])
        as_of_utc = str(row["as_of_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Candidate needs an accepted gate, event, bout, prediction, quote, and cutoff IDs") from exc
    if prediction_id <= 0 or quote_id <= 0 or gate_check_id <= 0 or not event_id or not bout_id:
        raise ValueError("Candidate IDs must be populated and positive")
    gate = connection.execute(
        "SELECT * FROM prefight_gate_checks WHERE gate_check_id = ?", (gate_check_id,)
    ).fetchone()
    if (gate is None or gate["gate_decision"] != "alert_candidate" or gate["gate_reason"] != "ok"
            or gate["prediction_id"] != prediction_id or gate["quote_id"] != quote_id
            or gate["event_id"] != event_id or gate["bout_id"] != bout_id):
        raise ValueError("Paper candidate needs a matching accepted pre-fight gate check")
    roster = latest_prefight_roster(connection, event_id, bout_id, recorded_time)
    if (not roster["accepted"]
            or roster["event_snapshot_id"] != gate["roster_snapshot_id"]):
        raise ValueError("The dated card roster changed or is no longer verifiable")
    checked_at = parse_utc(gate["checked_at_utc"])
    if checked_at > recorded_time or (recorded_time - checked_at).total_seconds() > gate["max_age_seconds"]:
        raise ValueError("The accepted pre-fight gate check is too old for paper recording")
    latest_import = connection.execute(
        "SELECT MAX(run_id) FROM ingestion_runs WHERE source = 'the-odds-api'"
    ).fetchone()[0]
    if latest_import != gate["ingestion_run_id"]:
        raise ValueError("A newer live odds import superseded this gate check")
    receipt_cache: dict = {}
    if not any(
        evidence["quote_id"] == quote_id and quote_receipt_matches(
            evidence, recorded_time, receipt_cache, live_only=True,
        )
        for evidence in linked_quote_rows(connection, bout_id, int(gate["ingestion_run_id"]))
    ):
        raise ValueError("The checked paper quote source is missing or changed")
    decision_time = parse_utc(as_of_utc)
    match = connection.execute(
        """
        SELECT p.prediction_id, p.bout_id, p.feature_cutoff_at_utc, p.p_fighter_a,
               q.quote_id, q.selection_fighter_id, q.bookmaker, q.decimal_odds,
               q.captured_at_utc, q.bookmaker_updated_at_utc,
               b.event_id, b.fighter_a_id, b.fighter_b_id,
               b.status AS bout_status, b.provider_status AS bout_provider_status,
               e.status AS event_status, e.provider_status AS event_provider_status,
               e.start_time_utc
        FROM predictions p
        JOIN bouts b ON b.bout_id = p.bout_id
        JOIN events e ON e.event_id = b.event_id
        JOIN odds_quotes q ON q.bout_id = b.bout_id
        WHERE p.prediction_id = ? AND q.quote_id = ?
        """,
        (prediction_id, quote_id),
    ).fetchone()
    if match is None or match["bout_id"] != bout_id or match["event_id"] != event_id:
        raise ValueError("Candidate prediction and quote must match the event and bout")
    if (gate["model_cutoff_at_utc"] != match["feature_cutoff_at_utc"]
            or gate["event_start_time_utc"] != match["start_time_utc"]
            or gate["bookmaker"] != match["bookmaker"]
            or gate["quote_captured_at_utc"] != match["captured_at_utc"]
            or gate["bookmaker_updated_at_utc"] != match["bookmaker_updated_at_utc"]
            or not math.isclose(gate["quoted_decimal_odds"], match["decimal_odds"], rel_tol=1e-12)):
        raise ValueError("The gate check no longer matches the saved prediction and quote")
    newer_quote = connection.execute(
        """SELECT 1 FROM odds_quotes WHERE bout_id = ? AND bookmaker = ?
           AND selection_fighter_id = ? AND captured_at_utc > ? LIMIT 1""",
        (bout_id, match["bookmaker"], match["selection_fighter_id"],
         match["captured_at_utc"]),
    ).fetchone()
    if newer_quote is not None:
        raise ValueError("A newer quote superseded the checked paper price")
    if match["event_status"] != "scheduled" or match["bout_status"] != "scheduled":
        raise ValueError("New paper decisions require a scheduled event and bout")
    if match["event_provider_status"] not in (None, "scheduled", "not_started"):
        raise ValueError("The provider reports the event is not scheduled")
    if match["bout_provider_status"] not in (None, "scheduled", "not_started"):
        raise ValueError("The provider reports the bout is not scheduled")
    if parse_utc(match["feature_cutoff_at_utc"]) != decision_time:
        raise ValueError("Candidate cutoff does not match the saved prediction")
    if decision_time > recorded_time:
        raise ValueError("A live paper decision cannot use a future prediction cutoff")
    if recorded_time - decision_time > timedelta(minutes=max_report_age_minutes):
        raise ValueError("The prediction report is too old for live paper trading")
    captured = parse_utc(match["captured_at_utc"])
    if captured > decision_time:
        raise ValueError("The quote was captured after the decision cutoff")
    if decision_time - captured > timedelta(hours=max_quote_age_hours):
        raise ValueError("The quote is too old for live paper trading")
    updated = match["bookmaker_updated_at_utc"]
    if updated:
        updated_time = parse_utc(updated)
        if updated_time > captured or updated_time > decision_time:
            raise ValueError("The bookmaker updated the quote after the decision cutoff")
        if decision_time - updated_time > timedelta(hours=max_quote_age_hours):
            raise ValueError("The bookmaker quote update is too old for live paper trading")
    if not match["start_time_utc"]:
        raise ValueError("A live paper decision requires a known event start time")
    if recorded_time >= parse_utc(match["start_time_utc"]):
        raise ValueError("A live paper decision must be recorded before the event starts")
    if row.get("quote_captured_at_utc") and str(row["quote_captured_at_utc"]) != match["captured_at_utc"]:
        raise ValueError("The report quote timestamp differs from the saved quote")
    if row.get("decimal_odds") not in (None, ""):
        try:
            report_odds = float(row["decimal_odds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Report odds must be numeric") from exc
        if not math.isfinite(report_odds) or not math.isclose(report_odds, match["decimal_odds"], rel_tol=1e-12):
            raise ValueError("The report price differs from the saved quote")
    if match["selection_fighter_id"] == match["fighter_a_id"]:
        probability = float(match["p_fighter_a"])
    elif match["selection_fighter_id"] == match["fighter_b_id"]:
        probability = 1.0 - float(match["p_fighter_a"])
    else:
        raise ValueError("Quote selection is not in the bout")
    edge = probability * float(match["decimal_odds"]) - 1.0
    if not math.isfinite(edge) or edge <= 0:
        raise ValueError("A paper candidate needs positive expected value at the saved price")
    stressed_edge = probability * (float(match["decimal_odds"]) - gate["decimal_odds_drift"]) - 1.0
    if (not math.isclose(probability, gate["model_probability"], rel_tol=1e-12, abs_tol=1e-12)
            or gate["conservative_expected_profit_per_dollar"] is None
            or stressed_edge + 1e-12 < gate["min_edge"]
            or not math.isclose(stressed_edge, gate["conservative_expected_profit_per_dollar"],
                                rel_tol=0, abs_tol=0.00005001)):
        raise ValueError("The saved gate probability or stressed edge is invalid")
    return {
        "gate_check_id": gate_check_id,
        "prediction_id": prediction_id,
        "quote_id": quote_id,
        "event_id": event_id,
        "bout_id": bout_id,
        "selection_fighter_id": match["selection_fighter_id"],
        "bookmaker": match["bookmaker"],
        "decision_at_utc": utc_string(decision_time),
        "quote_captured_at_utc": match["captured_at_utc"],
        "quoted_decimal_odds": float(match["decimal_odds"]),
        "model_probability": probability,
        "expected_profit_per_unit": edge,
        "conservative_expected_profit_per_unit": gate["conservative_expected_profit_per_dollar"],
    }


def record_paper_candidates(
    connection: sqlite3.Connection,
    report_rows: Sequence[Mapping],
    bankroll_units: float,
    max_fraction_per_bet: float = 0.01,
    max_fraction_per_event: float = 0.05,
    max_report_age_minutes: float = 15.0,
    max_quote_age_hours: float = 24.0,
) -> list[dict]:
    """Record accepted pre-fight gate candidates with per-bet and event caps.

    Existing decisions count toward the event cap. Repeating an identical report
    creates no new rows. Candidates with the largest saved edge receive the
    available event stake first; a final candidate may receive a partial stake.
    Only simulated stakes are recorded. There is no bookmaker integration here.
    """
    bankroll = _positive_finite(bankroll_units, "bankroll_units")
    per_bet = _positive_finite(max_fraction_per_bet, "max_fraction_per_bet", at_most_one=True)
    per_event = _positive_finite(max_fraction_per_event, "max_fraction_per_event", at_most_one=True)
    report_age = _positive_finite(max_report_age_minutes, "max_report_age_minutes")
    quote_age = _positive_finite(max_quote_age_hours, "max_quote_age_hours")
    bet_cap = bankroll * per_bet
    event_cap = bankroll * per_event
    if not math.isfinite(bet_cap) or not math.isfinite(event_cap):
        raise ValueError("Stake caps must be finite")

    if any(row.get("decision") == "candidate" and "alert_eligible" not in row
           for row in report_rows):
        raise ValueError("Paper candidates require a stored pre-fight gate check")
    candidates = [row for row in report_rows
                  if row.get("decision") == "candidate" and row.get("alert_eligible")]
    if not candidates:
        return []
    event_ids = {str(row.get("event_id")) for row in candidates}
    for event_id in event_ids:
        prior_policies = connection.execute(
            "SELECT DISTINCT bankroll_units, per_bet_cap_units, event_cap_units "
            "FROM paper_bets WHERE event_id = ?", (event_id,)
        ).fetchall()
        if any(
            not math.isclose(float(prior["bankroll_units"]), bankroll, rel_tol=1e-12, abs_tol=1e-9)
            or not math.isclose(float(prior["per_bet_cap_units"]), bet_cap, rel_tol=1e-12, abs_tol=1e-9)
            or not math.isclose(float(prior["event_cap_units"]), event_cap, rel_tol=1e-12, abs_tol=1e-9)
            for prior in prior_policies
        ):
            raise ValueError("The event already has paper decisions using a different bankroll or stake caps")
    recorded_time = utc_now()
    details: list[dict] = []
    seen_predictions: dict[int, int] = {}
    for row in candidates:
        try:
            prediction_id = int(row["prediction_id"])
            quote_id = int(row["quote_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Candidate needs valid prediction and quote IDs") from exc
        previous = seen_predictions.get(prediction_id)
        if previous is not None:
            if previous != quote_id:
                raise ValueError("The same prediction has conflicting candidate quotes")
            continue
        seen_predictions[prediction_id] = quote_id
        existing = connection.execute(
            "SELECT quote_id, event_id, bout_id FROM paper_bets WHERE prediction_id = ?",
            (prediction_id,),
        ).fetchone()
        if existing is not None:
            if (existing["quote_id"] != quote_id or existing["event_id"] != row.get("event_id")
                    or existing["bout_id"] != row.get("bout_id")):
                raise ValueError("A paper decision already exists for this prediction with different details")
            continue
        details.append(_candidate_details(
            connection, row, recorded_time, report_age, quote_age
        ))
    details.sort(key=lambda item: (-item["conservative_expected_profit_per_unit"], item["event_id"], item["bout_id"]))
    if not details:
        return []

    events = {item["event_id"] for item in details}
    used = {
        event_id: float(connection.execute(
            "SELECT COALESCE(SUM(stake_units), 0) AS total FROM paper_bets WHERE event_id = ?",
            (event_id,),
        ).fetchone()["total"])
        for event_id in events
    }
    recorded_at = utc_string(recorded_time)
    created: list[dict] = []
    connection.execute("SAVEPOINT record_paper_candidates")
    try:
        for item in details:
            remaining = max(0.0, event_cap - used[item["event_id"]])
            stake = min(bet_cap, remaining)
            if stake <= 1e-10:
                continue
            cursor = connection.execute(
                """
                INSERT INTO paper_bets(
                    prediction_id, quote_id, event_id, bout_id, selection_fighter_id,
                    bookmaker, decision_at_utc, recorded_at_utc, quote_captured_at_utc,
                    quoted_decimal_odds, model_probability, expected_profit_per_unit,
                    bankroll_units, per_bet_cap_units, event_cap_units, stake_units,
                    gate_check_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["prediction_id"], item["quote_id"], item["event_id"], item["bout_id"],
                    item["selection_fighter_id"], item["bookmaker"], item["decision_at_utc"],
                    recorded_at, item["quote_captured_at_utc"], item["quoted_decimal_odds"],
                    item["model_probability"], item["expected_profit_per_unit"],
                    bankroll, bet_cap, event_cap, stake, item["gate_check_id"],
                ),
            )
            used[item["event_id"]] += stake
            created.append({**item, "paper_bet_id": int(cursor.lastrowid),
                            "bankroll_units": bankroll, "per_bet_cap_units": bet_cap,
                            "event_cap_units": event_cap, "stake_units": stake,
                            "recorded_at_utc": recorded_at})
        connection.execute("RELEASE SAVEPOINT record_paper_candidates")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT record_paper_candidates")
        connection.execute("RELEASE SAVEPOINT record_paper_candidates")
        raise
    connection.commit()
    return created


def settle_paper_bets(connection: sqlite3.Connection, event_id: str) -> dict[str, int]:
    """Settle binary results; flag draws, no contests, and cancellations for review."""
    event = connection.execute(
        "SELECT event_id, status FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if event is None:
        raise ValueError(f"Unknown event: {event_id}")
    rows = connection.execute(
        """
        SELECT pb.paper_bet_id, pb.stake_units, pb.quoted_decimal_odds,
               pb.selection_fighter_id, b.status AS bout_status,
               b.provider_status AS bout_provider_status,
               r.outcome, r.winner_fighter_id
        FROM paper_bets pb
        JOIN bouts b ON b.bout_id = pb.bout_id
        LEFT JOIN results r ON r.bout_id = pb.bout_id
        WHERE pb.event_id = ? AND pb.settlement_status = 'open'
        ORDER BY pb.paper_bet_id
        """,
        (event_id,),
    ).fetchall()
    counts = {"won": 0, "lost": 0, "pending_review": 0, "open": 0}
    changed_at = utc_string(utc_now())
    connection.execute("SAVEPOINT settle_paper_bets")
    try:
        for row in rows:
            outcome = row["outcome"]
            if outcome == "win":
                status = "won" if row["winner_fighter_id"] == row["selection_fighter_id"] else "lost"
                payout = row["stake_units"] * row["quoted_decimal_odds"] if status == "won" else 0.0
                note = None
                settled_at = changed_at
            elif outcome in {"draw", "no_contest"}:
                status, payout, note, settled_at = "pending_review", None, outcome, None
            elif (event["status"] == "cancelled" or row["bout_status"] == "cancelled"
                  or row["bout_provider_status"] == "cancelled"):
                status, payout, note, settled_at = "pending_review", None, "cancelled", None
            elif event["status"] == "completed" or row["bout_status"] == "completed":
                status, payout, note, settled_at = "pending_review", None, "missing_result", None
            else:
                counts["open"] += 1
                continue
            connection.execute(
                """
                UPDATE paper_bets
                SET settlement_status = ?, payout_units = ?, status_updated_at_utc = ?,
                    settled_at_utc = ?, settlement_note = ?
                WHERE paper_bet_id = ? AND settlement_status = 'open'
                """,
                (status, payout, changed_at, settled_at, note, row["paper_bet_id"]),
            )
            counts[status] += 1
        connection.execute("RELEASE SAVEPOINT settle_paper_bets")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT settle_paper_bets")
        connection.execute("RELEASE SAVEPOINT settle_paper_bets")
        raise
    connection.commit()
    return counts
