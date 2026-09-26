"""A transparent fight-winner baseline using pre-event Elo ratings."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

MODEL_VERSION = "elo-k32-v1"


class EloModel:
    def __init__(self, k_factor: float = 32.0, initial_rating: float = 1500.0):
        self.k_factor = k_factor
        self.initial_rating = initial_rating
        self.ratings: defaultdict[str, float] = defaultdict(lambda: initial_rating)

    def probability(self, fighter_a_id: str, fighter_b_id: str) -> float:
        a_rating = self.ratings[fighter_a_id]
        b_rating = self.ratings[fighter_b_id]
        return 1.0 / (1.0 + 10.0 ** ((b_rating - a_rating) / 400.0))

    def update(self, row: Mapping[str, object]) -> None:
        outcome = row["outcome"]
        if outcome == "no_contest":
            return
        fighter_a_id = str(row["fighter_a_id"])
        fighter_b_id = str(row["fighter_b_id"])
        predicted_a = self.probability(fighter_a_id, fighter_b_id)
        if outcome == "draw":
            actual_a = 0.5
        elif outcome == "win":
            actual_a = 1.0 if row["winner_fighter_id"] == fighter_a_id else 0.0
        else:
            raise ValueError(f"Unknown result outcome: {outcome}")
        change = self.k_factor * (actual_a - predicted_a)
        self.ratings[fighter_a_id] += change
        self.ratings[fighter_b_id] -= change


def model_from_results(rows: list[Mapping[str, object]]) -> EloModel:
    model = EloModel()
    for row in rows:
        model.update(row)
    return model
