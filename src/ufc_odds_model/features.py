"""Pre-fight features replayed at a strict UTC decision cutoff.

Fight results are replayed by event date, with all same-date results withheld
until the next UTC date because individual bout start times are unavailable.
Optional profile and per-fight stat values require source-dated observations
strictly before the decision timestamp. Missing values stay neutral and are
counted in ``FeatureRow.coverage`` rather than filled from current profiles.
"""

from __future__ import annotations

import math
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from itertools import groupby
from typing import Mapping

from .elo import EloModel
from .pipeline import parse_utc, utc_string


FEATURE_NAMES = (
    "elo_difference_400",
    "log_prior_bouts_difference",
    "smoothed_win_rate_difference",
    "rest_years_difference",
    "known_rest_difference",
    "recent_form_5_difference",
    "age_years_difference_10",
    "known_age_difference",
    "reach_cm_difference_20",
    "known_reach_difference",
    "opponent_adjusted_sig_strike_accuracy_difference",
    "known_sig_strike_stat_difference",
    "log_stat_bouts_difference",
)

# A-age, B-age, A-reach, B-reach, A-adjusted-stats, B-adjusted-stats.
COVERAGE_NAMES = (
    "fighter_a_age", "fighter_b_age", "fighter_a_reach", "fighter_b_reach",
    "fighter_a_adjusted_stats", "fighter_b_adjusted_stats",
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
    coverage: tuple[bool, bool, bool, bool, bool, bool] = (False,) * 6


@dataclass
class _FighterHistory:
    scored_bouts: int = 0
    win_points: float = 0.0
    last_event_date: date | None = None
    recent_results: deque[float] = field(default_factory=lambda: deque(maxlen=5))
    defense_landed: int = 0
    defense_attempted: int = 0
    adjusted_stat_total: float = 0.0
    stat_bouts: int = 0


@dataclass(frozen=True)
class _Profile:
    birth_date: date | None = None
    reach_cm: float | None = None


class FeatureBuilder:
    """Replay past results and paired stats in whole UTC-date batches."""

    def __init__(self) -> None:
        self.elo = EloModel()
        self.history: dict[str, _FighterHistory] = {}

    def values_with_coverage(
        self, fighter_a_id: str, fighter_b_id: str, event_date: str,
        profiles: Mapping[str, _Profile] | None = None,
    ) -> tuple[tuple[float, ...], tuple[bool, bool, bool, bool, bool, bool]]:
        target_date = date.fromisoformat(event_date)
        a = self.history.get(fighter_a_id, _FighterHistory())
        b = self.history.get(fighter_b_id, _FighterHistory())
        profiles = profiles or {}
        a_profile = profiles.get(fighter_a_id, _Profile())
        b_profile = profiles.get(fighter_b_id, _Profile())

        def rest_years(history: _FighterHistory) -> float:
            if history.last_event_date is None:
                return 0.0
            days = (target_date - history.last_event_date).days
            if days <= 0:
                raise ValueError("Feature history must be strictly before the target date")
            return min(days, 730) / 365.0

        def age_years(profile: _Profile) -> float:
            if profile.birth_date is None:
                return 0.0
            days = (target_date - profile.birth_date).days
            if days <= 0:
                raise ValueError("Fighter birth date must precede the target event")
            return days / 365.2425

        def recent_form(history: _FighterHistory) -> float:
            return (sum(history.recent_results) + 1.0) / (len(history.recent_results) + 2.0)

        def adjusted_stat(history: _FighterHistory) -> float:
            # Two neutral pseudo-bouts shrink sparse adjusted performance.
            return history.adjusted_stat_total / (history.stat_bouts + 2.0)

        coverage = (
            a_profile.birth_date is not None, b_profile.birth_date is not None,
            a_profile.reach_cm is not None, b_profile.reach_cm is not None,
            a.stat_bouts > 0, b.stat_bouts > 0,
        )
        values = (
            (self.elo.ratings[fighter_a_id] - self.elo.ratings[fighter_b_id]) / 400.0,
            math.log1p(a.scored_bouts) - math.log1p(b.scored_bouts),
            (a.win_points + 1.0) / (a.scored_bouts + 2.0)
            - (b.win_points + 1.0) / (b.scored_bouts + 2.0),
            rest_years(a) - rest_years(b),
            float(a.last_event_date is not None) - float(b.last_event_date is not None),
            recent_form(a) - recent_form(b),
            (age_years(a_profile) - age_years(b_profile)) / 10.0
            if coverage[0] and coverage[1] else 0.0,
            float(coverage[0]) - float(coverage[1]),
            (float(a_profile.reach_cm) - float(b_profile.reach_cm)) / 20.0
            if coverage[2] and coverage[3] else 0.0,
            float(coverage[2]) - float(coverage[3]),
            adjusted_stat(a) - adjusted_stat(b),
            float(coverage[4]) - float(coverage[5]),
            math.log1p(a.stat_bouts) - math.log1p(b.stat_bouts),
        )
        if len(values) != len(FEATURE_NAMES) or not all(math.isfinite(value) for value in values):
            raise ValueError("Feature replay produced invalid values")
        return values, coverage

    def values(self, fighter_a_id: str, fighter_b_id: str, event_date: str) -> tuple[float, ...]:
        """Return the neutral-profile feature vector for direct callers."""
        return self.values_with_coverage(fighter_a_id, fighter_b_id, event_date)[0]

    def update_group(
        self,
        results: list[Mapping[str, object]],
        stats: Mapping[tuple[str, str], tuple[int, int]] | None = None,
    ) -> None:
        """Apply one complete date without ordering its unknown bout start times."""
        stats = stats or {}
        pending: list[tuple[str, float, int, int]] = []
        for result in results:
            if str(result["outcome"]) == "no_contest":
                continue
            bout_id = str(result["bout_id"])
            a_id = str(result["fighter_a_id"])
            b_id = str(result["fighter_b_id"])
            a_stat = stats.get((bout_id, a_id))
            b_stat = stats.get((bout_id, b_id))
            if a_stat is None or b_stat is None or a_stat[1] == 0 or b_stat[1] == 0:
                continue
            a_prior = self.history.get(a_id, _FighterHistory())
            b_prior = self.history.get(b_id, _FighterHistory())
            # An opponent's *earlier-date* conceded strike rate is the
            # difficulty baseline for each observed fight. This date's fights
            # are applied only after every residual has been calculated.
            a_vs_defense = (b_prior.defense_landed + 1.0) / (b_prior.defense_attempted + 2.0)
            b_vs_defense = (a_prior.defense_landed + 1.0) / (a_prior.defense_attempted + 2.0)
            pending.append((a_id, a_stat[0] / a_stat[1] - a_vs_defense, b_stat[0], b_stat[1]))
            pending.append((b_id, b_stat[0] / b_stat[1] - b_vs_defense, a_stat[0], a_stat[1]))
        for result in results:
            self.update(result)
        for fighter_id, residual, conceded_landed, conceded_attempted in pending:
            history = self.history.setdefault(fighter_id, _FighterHistory())
            history.adjusted_stat_total += residual
            history.stat_bouts += 1
            history.defense_landed += conceded_landed
            history.defense_attempted += conceded_attempted

    def update(self, result: Mapping[str, object]) -> None:
        """Update result-only history; no-contests do not contribute."""
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
            points = 0.5 if outcome == "draw" else float(winner == fighter_id)
            history.scored_bouts += 1
            history.win_points += points
            history.recent_results.append(points)
            history.last_event_date = fight_date


_COMPLETED_RESULTS = """
    SELECT b.bout_id, b.event_id, b.fighter_a_id, b.fighter_b_id,
           e.event_date, r.outcome, r.winner_fighter_id
    FROM bouts AS b
    JOIN events AS e ON e.event_id = b.event_id
    JOIN results AS r ON r.bout_id = b.bout_id
    WHERE b.status = 'completed' AND e.status = 'completed'
"""


def _profiles_as_of(connection: sqlite3.Connection, cutoff_at_utc: str) -> dict[str, _Profile]:
    snapshots: dict[str, dict[str, object]] = {}
    times: dict[tuple[str, str], tuple[str, object]] = {}
    for row in connection.execute(
        """SELECT fighter_id, observed_at_utc, birth_date, reach_cm
           FROM fighter_profile_observations WHERE observed_at_utc < ?
           ORDER BY observed_at_utc, source, observation_id""",
        (cutoff_at_utc,),
    ):
        fighter_id, observed = str(row["fighter_id"]), str(row["observed_at_utc"])
        for field_name in ("birth_date", "reach_cm"):
            value = row[field_name]
            if value is None:
                continue
            key = (fighter_id, field_name)
            if key in times and times[key][0] == observed and times[key][1] != value:
                raise ValueError(f"Conflicting profile sources for {fighter_id} at {observed}")
            times[key] = (observed, value)
            snapshots.setdefault(fighter_id, {})[field_name] = value
    return {
        fighter_id: _Profile(
            date.fromisoformat(str(values["birth_date"])) if "birth_date" in values else None,
            float(values["reach_cm"]) if "reach_cm" in values else None,
        )
        for fighter_id, values in snapshots.items()
    }


def _stats_as_of(
    connection: sqlite3.Connection, history_before_date: str, cutoff_at_utc: str,
) -> dict[tuple[str, str], tuple[int, int]]:
    stats: dict[tuple[str, str], tuple[int, int]] = {}
    times: dict[tuple[str, str], str] = {}
    for row in connection.execute(
        """SELECT s.bout_id, s.fighter_id, s.observed_at_utc,
                  s.sig_strikes_landed, s.sig_strikes_attempted
           FROM fight_stat_observations s
           JOIN bouts b ON b.bout_id = s.bout_id
           JOIN events e ON e.event_id = b.event_id
           WHERE e.event_date < ? AND s.observed_at_utc < ?
             AND b.status = 'completed' AND e.status = 'completed'
           ORDER BY s.observed_at_utc, s.source, s.observation_id""",
        (history_before_date, cutoff_at_utc),
    ):
        key = (str(row["bout_id"]), str(row["fighter_id"]))
        observed = str(row["observed_at_utc"])
        value = (int(row["sig_strikes_landed"]), int(row["sig_strikes_attempted"]))
        if key in times and times[key] == observed and stats[key] != value:
            raise ValueError(f"Conflicting fight stat sources for {key[0]}/{key[1]} at {observed}")
        times[key] = observed
        stats[key] = value
    return stats


def _builder_as_of(
    connection: sqlite3.Connection, history_before_date: str, cutoff_at_utc: str,
) -> tuple[FeatureBuilder, dict[str, _Profile]]:
    prior = connection.execute(
        _COMPLETED_RESULTS + " AND e.event_date < ? ORDER BY e.event_date, e.event_id, b.bout_id",
        (history_before_date,),
    ).fetchall()
    stats = _stats_as_of(connection, history_before_date, cutoff_at_utc)
    builder = FeatureBuilder()
    for _, date_results in groupby(prior, key=lambda row: row["event_date"]):
        builder.update_group(list(date_results), stats)
    return builder, _profiles_as_of(connection, cutoff_at_utc)


def _row(
    builder: FeatureBuilder, profiles: Mapping[str, _Profile], bout_id: str,
    event_id: str, event_date: str, a_id: str, b_id: str, target: int | None = None,
) -> FeatureRow:
    values, coverage = builder.values_with_coverage(a_id, b_id, event_date, profiles)
    return FeatureRow(bout_id, event_id, event_date, a_id, b_id, values, target, coverage)


def training_rows(connection: sqlite3.Connection, before_date: str | None = None) -> list[FeatureRow]:
    """Return binary examples; rebuild observation eligibility for each date."""
    if before_date is not None:
        date.fromisoformat(before_date)
    query = _COMPLETED_RESULTS
    params: tuple[str, ...] = ()
    if before_date is not None:
        query += " AND e.event_date < ?"
        params = (before_date,)
    query += " ORDER BY e.event_date, e.event_id, b.bout_id"
    results = connection.execute(query, params).fetchall()
    examples: list[FeatureRow] = []
    for event_date, date_results in groupby(results, key=lambda row: row["event_date"]):
        # Midnight is conservative when this helper has no event start time.
        cutoff_at_utc = f"{event_date}T00:00:00Z"
        builder, profiles = _builder_as_of(connection, event_date, cutoff_at_utc)
        for result in date_results:
            if result["outcome"] != "win":
                continue
            a_id, b_id = sorted((result["fighter_a_id"], result["fighter_b_id"]))
            examples.append(_row(
                builder, profiles, str(result["bout_id"]), str(result["event_id"]),
                event_date, a_id, b_id, int(result["winner_fighter_id"] == a_id),
            ))
    return examples


def event_feature_rows(
    connection: sqlite3.Connection,
    event_id: str,
    cutoff_date: str | None = None,
    *,
    cutoff_at_utc: datetime | str | None = None,
) -> list[FeatureRow]:
    """Score a card from strictly earlier dates and source observations.

    ``cutoff_at_utc`` is the exact decision time. The legacy date-only cutoff
    uses UTC midnight and therefore cannot admit same-day observations.
    """
    event = connection.execute(
        "SELECT event_date FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if event is None:
        raise ValueError(f"Unknown event: {event_id}")
    event_date = str(event["event_date"])
    date.fromisoformat(event_date)
    if cutoff_at_utc is not None:
        if cutoff_date is not None:
            raise ValueError("Choose cutoff_date or cutoff_at_utc, not both")
        cutoff_time = parse_utc(cutoff_at_utc) if isinstance(cutoff_at_utc, str) else cutoff_at_utc
        if cutoff_time.tzinfo is None or cutoff_time.utcoffset() is None:
            raise ValueError("cutoff_at_utc must be timezone-aware")
        cutoff_time = cutoff_time.astimezone(timezone.utc)
        history_before = min(event_date, cutoff_time.date().isoformat())
        cutoff_str = utc_string(cutoff_time)
    else:
        if cutoff_date is not None:
            date.fromisoformat(cutoff_date)
        history_before = min(event_date, cutoff_date) if cutoff_date else event_date
        cutoff_str = f"{history_before}T00:00:00Z"
    builder, profiles = _builder_as_of(connection, history_before, cutoff_str)
    bouts = connection.execute(
        """SELECT bout_id, fighter_a_id, fighter_b_id FROM bouts
           WHERE event_id = ? AND status != 'cancelled' ORDER BY bout_id""",
        (event_id,),
    ).fetchall()
    return [
        _row(builder, profiles, str(bout["bout_id"]), event_id, event_date,
             str(bout["fighter_a_id"]), str(bout["fighter_b_id"]))
        for bout in bouts
    ]
