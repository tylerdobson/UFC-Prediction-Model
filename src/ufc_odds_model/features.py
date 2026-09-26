"""Pre-fight features from completed bouts, with conservative date cutoffs.

The database has event dates but usually no historical bout start timestamps.
Every fight on a date therefore sees only results from *earlier dates*. This
also prevents one fight on a card from learning another result on that card.
Historical UFCStats pages list winners first, so training pairs are reordered
by stable fighter ID independently of the outcome.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from typing import Mapping

from .elo import EloModel


FEATURE_NAMES = (
    "elo_difference_400",
    "log_prior_bouts_difference",
    "smoothed_win_rate_difference",
    "rest_years_difference",
    "known_rest_difference",
)


@dataclass(frozen=True)
class FeatureRow:
    bout_id: str
    event_id: str
    event_date: str
    fighter_a_id: str
    fighter_b_id: str
    features: tuple[float, ...]
    target: int | None = None


@dataclass
class _FighterHistory:
    scored_bouts: int = 0
    win_points: float = 0.0
    last_event_date: date | None = None


class FeatureBuilder:
    """Replay past results; call ``values`` before updating a target date."""

    def __init__(self) -> None:
        self.elo = EloModel()
        self.history: dict[str, _FighterHistory] = {}

    def values(self, fighter_a_id: str, fighter_b_id: str, event_date: str) -> tuple[float, ...]:
        target_date = date.fromisoformat(event_date)
        a = self.history.get(fighter_a_id, _FighterHistory())
        b = self.history.get(fighter_b_id, _FighterHistory())

        def rest_years(history: _FighterHistory) -> float:
            if history.last_event_date is None:
                return 0.0
            days = (target_date - history.last_event_date).days
            if days <= 0:
                raise ValueError("Feature history must be strictly before the target date")
            return min(days, 730) / 365.0

        return (
            (self.elo.ratings[fighter_a_id] - self.elo.ratings[fighter_b_id]) / 400.0,
            math.log1p(a.scored_bouts) - math.log1p(b.scored_bouts),
            (a.win_points + 1.0) / (a.scored_bouts + 2.0)
            - (b.win_points + 1.0) / (b.scored_bouts + 2.0),
            rest_years(a) - rest_years(b),
            float(a.last_event_date is not None) - float(b.last_event_date is not None),
        )

    def update(self, result: Mapping[str, object]) -> None:
        """Update from a completed, validated bout result."""
        outcome = str(result["outcome"])
        if outcome == "no_contest":
            return
        if outcome not in {"win", "draw"}:
            raise ValueError(f"Unknown result outcome: {outcome}")
        a_id = str(result["fighter_a_id"])
        b_id = str(result["fighter_b_id"])
        winner = result["winner_fighter_id"]
        if outcome == "win" and winner not in {a_id, b_id}:
            raise ValueError("Result winner is not a participant")
        fight_date = date.fromisoformat(str(result["event_date"]))
        for fighter_id in (a_id, b_id):
            history = self.history.get(fighter_id)
            if history and history.last_event_date is not None and fight_date < history.last_event_date:
                raise ValueError("Results must be replayed in date order")
        self.elo.update(result)
        for fighter_id in (a_id, b_id):
            history = self.history.setdefault(fighter_id, _FighterHistory())
            history.scored_bouts += 1
            history.win_points += 0.5 if outcome == "draw" else float(winner == fighter_id)
            history.last_event_date = fight_date


_COMPLETED_RESULTS = """
    SELECT b.bout_id, b.event_id, b.fighter_a_id, b.fighter_b_id,
           e.event_date, r.outcome, r.winner_fighter_id
    FROM bouts AS b
    JOIN events AS e ON e.event_id = b.event_id
    JOIN results AS r ON r.bout_id = b.bout_id
    WHERE b.status = 'completed' AND e.status = 'completed'
"""


def training_rows(connection: sqlite3.Connection, before_date: str | None = None) -> list[FeatureRow]:
    """Return binary training examples; draws inform history, NCs do not.

    ``before_date`` is exclusive. It allows a model trained for a future event
    to exclude events on its date and all later results.
    """
    if before_date is not None:
        date.fromisoformat(before_date)
    query = _COMPLETED_RESULTS
    params: tuple[str, ...] = ()
    if before_date is not None:
        query += " AND e.event_date < ?"
        params = (before_date,)
    query += " ORDER BY e.event_date, e.event_id, b.bout_id"
    results = connection.execute(query, params).fetchall()
    builder = FeatureBuilder()
    examples: list[FeatureRow] = []
    for event_date, date_results in groupby(results, key=lambda row: row["event_date"]):
        same_date = list(date_results)
        for row in same_date:
            if row["outcome"] != "win":
                continue
            # UFCStats completed cards present the winner first. A stable ID
            # ordering removes that target leakage and is reversible at scoring.
            fighter_a_id, fighter_b_id = sorted((row["fighter_a_id"], row["fighter_b_id"]))
            examples.append(
                FeatureRow(
                    bout_id=row["bout_id"],
                    event_id=row["event_id"],
                    event_date=event_date,
                    fighter_a_id=fighter_a_id,
                    fighter_b_id=fighter_b_id,
                    features=builder.values(fighter_a_id, fighter_b_id, event_date),
                    target=int(row["winner_fighter_id"] == fighter_a_id),
                )
            )
        for row in same_date:
            builder.update(row)
    return examples


def event_feature_rows(
    connection: sqlite3.Connection,
    event_id: str,
    cutoff_date: str | None = None,
) -> list[FeatureRow]:
    """Score non-cancelled bouts using results before the event and cutoff.

    ``cutoff_date`` is the UTC date of the prediction time. Only earlier event
    dates count; this is conservative when bouts occurred earlier on that day.
    """
    event = connection.execute(
        "SELECT event_date FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if event is None:
        raise ValueError(f"Unknown event: {event_id}")
    event_date = str(event["event_date"])
    date.fromisoformat(event_date)
    if cutoff_date is not None:
        date.fromisoformat(cutoff_date)
    history_before = min(event_date, cutoff_date) if cutoff_date else event_date
    prior = connection.execute(
        _COMPLETED_RESULTS + " AND e.event_date < ? ORDER BY e.event_date, e.event_id, b.bout_id",
        (history_before,),
    ).fetchall()
    builder = FeatureBuilder()
    for row in prior:
        builder.update(row)
    bouts = connection.execute(
        """
        SELECT bout_id, fighter_a_id, fighter_b_id FROM bouts
        WHERE event_id = ? AND status != 'cancelled' ORDER BY bout_id
        """,
        (event_id,),
    ).fetchall()
    return [
        FeatureRow(
            bout_id=row["bout_id"],
            event_id=event_id,
            event_date=event_date,
            fighter_a_id=row["fighter_a_id"],
            fighter_b_id=row["fighter_b_id"],
            features=builder.values(row["fighter_a_id"], row["fighter_b_id"], event_date),
        )
        for row in bouts
    ]
