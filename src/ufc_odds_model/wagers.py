"""Manual wager ledger. A quote is never treated as an executed bet."""

from __future__ import annotations

import sqlite3

from .pipeline import parse_utc, utc_now, utc_string


def record_bet(
    connection: sqlite3.Connection,
    prediction_id: int,
    quote_id: int,
    stake: float,
    actual_decimal_odds: float,
    placed_at_utc: str | None = None,
) -> int:
    if stake <= 0 or actual_decimal_odds <= 1:
        raise ValueError("Stake must be positive and decimal odds must exceed 1")
    placed_at_utc = placed_at_utc or utc_string(utc_now())
    placed_at = parse_utc(placed_at_utc)
    match = connection.execute(
        """
        SELECT p.prediction_id, p.feature_cutoff_at_utc, q.quote_id, q.bout_id,
               q.selection_fighter_id, q.captured_at_utc, q.bookmaker_updated_at_utc,
               e.start_time_utc, b.status AS bout_status
        FROM predictions p
        JOIN odds_quotes q ON q.bout_id = p.bout_id
        JOIN bouts b ON b.bout_id = p.bout_id
        JOIN events e ON e.event_id = b.event_id
        WHERE p.prediction_id = ? AND q.quote_id = ?
        """,
        (prediction_id, quote_id),
    ).fetchone()
    if match is None:
        raise ValueError("Prediction and quote do not refer to the same bout")
    if match["bout_status"] != "scheduled":
        raise ValueError("The bout is not scheduled")
    if parse_utc(match["feature_cutoff_at_utc"]) > placed_at:
        raise ValueError("Prediction was generated after the placement time")
    if parse_utc(match["captured_at_utc"]) > placed_at:
        raise ValueError("Quote was captured after the placement time")
    if match["bookmaker_updated_at_utc"] and parse_utc(match["bookmaker_updated_at_utc"]) > placed_at:
        raise ValueError("Bookmaker quote was updated after the placement time")
    if match["start_time_utc"] and placed_at >= parse_utc(match["start_time_utc"]):
        raise ValueError("Placement time must be before the event starts")
    cursor = connection.execute(
        """
        INSERT INTO bets(
            prediction_id, quote_id, selection_fighter_id, placed_at_utc,
            actual_decimal_odds, stake
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id, quote_id, match["selection_fighter_id"], placed_at_utc,
            actual_decimal_odds, stake,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def settle_bet(
    connection: sqlite3.Connection,
    bet_id: int,
    status: str,
    payout: float | None = None,
    settled_at_utc: str | None = None,
) -> None:
    if status not in {"won", "lost", "push", "void"}:
        raise ValueError("Status must be won, lost, push, or void")
    bet = connection.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
    if bet is None:
        raise ValueError(f"Unknown bet: {bet_id}")
    if bet["settlement_status"] != "open":
        raise ValueError("This bet is already settled")
    if payout is None:
        payout = (
            bet["stake"] * bet["actual_decimal_odds"] if status == "won"
            else bet["stake"] if status in {"push", "void"}
            else 0.0
        )
    if payout < 0:
        raise ValueError("Payout cannot be negative")
    settled_at_utc = settled_at_utc or utc_string(utc_now())
    parse_utc(settled_at_utc)
    connection.execute(
        """
        UPDATE bets
        SET settlement_status = ?, payout = ?, settled_at_utc = ?
        WHERE bet_id = ?
        """,
        (status, payout, settled_at_utc, bet_id),
    )
    connection.commit()
