"""Event-ordered evaluation against Elo and observed bookmaker prices.

The model is fit on early events, temperature is fit on later validation
events, and the final event dates are held out for metrics. Historical odds
are eligible only when both sides of one bookmaker's market were observable
at the specified pre-event decision time.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

from .features import (
    COVERAGE_NAMES, FeatureRow, event_feature_rows, source_snapshot_available,
)
from .logistic import (
    binary_metrics,
    chronological_event_split,
    fit_logistic,
    fit_temperature,
)
from .pipeline import parse_utc, utc_string
from .quote_evidence import linked_quote_rows, quote_receipt_matches


MIN_PRIOR_RESULT_EVIDENCE_COVERAGE = 1.0


def _split_summary(rows: list[FeatureRow]) -> dict[str, int | str | None]:
    dates = sorted({row.event_date for row in rows})
    return {
        "bouts": len(rows),
        "events": len({row.event_id for row in rows}),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def _observation_coverage(rows: list[FeatureRow]) -> dict[str, object]:
    """Report individual fighter and paired availability on each split."""
    denominator = 2 * len(rows)
    age = sum(row.coverage[0] + row.coverage[1] for row in rows)
    reach = sum(row.coverage[2] + row.coverage[3] for row in rows)
    adjusted_stats = sum(row.coverage[4] + row.coverage[5] for row in rows)
    return {
        "bouts": len(rows),
        "fighter_instances": denominator,
        "fighter_instances_with_age": age,
        "fighter_instances_with_reach": reach,
        "fighter_instances_with_adjusted_stats": adjusted_stats,
        "age_fighter_coverage": age / denominator if denominator else None,
        "reach_fighter_coverage": reach / denominator if denominator else None,
        "adjusted_stats_fighter_coverage": adjusted_stats / denominator if denominator else None,
        "bouts_with_both_ages": sum(row.coverage[0] and row.coverage[1] for row in rows),
        "bouts_with_both_reaches": sum(row.coverage[2] and row.coverage[3] for row in rows),
        "bouts_with_both_adjusted_stats": sum(row.coverage[4] and row.coverage[5] for row in rows),
    }


def _prior_result_evidence(rows: list[FeatureRow]) -> dict[str, int | float | bool | None]:
    """Count source-proven prior-result instances used by these examples.

    A completed prior bout contributes once for each target bout that could
    have used it. This sample-weighted measure matches the model's exposure
    to history rather than counting each source bout only once.
    """
    available = sum(row.prior_result_rows_available for row in rows)
    observed = sum(row.prior_result_rows_observed or 0 for row in rows)
    unknown = sum(row.prior_result_rows_observed is None for row in rows)
    coverage = observed / available if available else None
    return {
        "bouts": len(rows),
        "available_prior_result_instances": available,
        "observed_prior_result_instances": observed,
        "unverified_feature_rows": unknown,
        "coverage": coverage,
        "meets_threshold": bool(
            available and not unknown and coverage is not None
            and coverage >= MIN_PRIOR_RESULT_EVIDENCE_COVERAGE
        ),
    }


def _point_in_time_rows(
    connection: sqlite3.Connection,
    decision_hours_before_event: float,
) -> tuple[list[FeatureRow], dict[str, datetime], dict[str, dict[str, int]]]:
    """Build labelled rows with one conservative UTC cutoff per event.

    A bout on the decision date may have ended *after* the fixed decision
    time. Without reliable bout start timestamps, history is restricted to
    event dates strictly before the decision date. Events without a start
    timestamp cannot serve as labelled targets, but their source-observed
    completed results can still inform later events. A labelled target also
    needs a retained, pre-decision scheduled-card observation.
    """
    events = connection.execute(
        """
        SELECT event_id, event_date, start_time_utc FROM events
        WHERE status = 'completed' ORDER BY event_date, event_id
        """
    ).fetchall()
    rows: list[FeatureRow] = []
    decision_times: dict[str, datetime] = {}
    excluded = {
        "missing_start_time": {"events": 0, "binary_bouts": 0},
        "missing_prefight_snapshot": {"events": 0, "binary_bouts": 0},
        "roster_mismatch": {"events": 0, "binary_bouts": 0},
    }
    receipt_cache: dict[int, bool] = {}
    for event in events:
        event_id = str(event["event_id"])
        results = connection.execute(
            """
            SELECT b.bout_id, b.fighter_a_id, b.fighter_b_id, r.winner_fighter_id
            FROM bouts AS b JOIN results AS r ON r.bout_id = b.bout_id
            WHERE b.event_id = ? AND b.status = 'completed' AND r.outcome = 'win'
            ORDER BY b.bout_id
            """,
            (event_id,),
        ).fetchall()
        if not event["start_time_utc"]:
            excluded["missing_start_time"]["events"] += 1
            excluded["missing_start_time"]["binary_bouts"] += len(results)
            continue
        try:
            start = parse_utc(str(event["start_time_utc"]))
        except ValueError:
            excluded["missing_start_time"]["events"] += 1
            excluded["missing_start_time"]["binary_bouts"] += len(results)
            continue
        decision_time = start - timedelta(hours=decision_hours_before_event)
        # A current completed card cannot prove who was scheduled at the old
        # decision time. Choose the latest independently timed, intact source
        # observation available then, and require its schedule to match.
        snapshots = connection.execute(
            """SELECT s.*, r.fetched_at_utc, r.payload_path, r.sha256
               FROM card_event_snapshots s
               JOIN ingestion_runs r ON r.run_id = s.ingestion_run_id
               WHERE s.event_id = ? AND s.source_observed_at_utc < ?
               ORDER BY s.source_observed_at_utc DESC, s.event_snapshot_id DESC""",
            (event_id, utc_string(decision_time)),
        ).fetchall()
        snapshot = next((
            candidate for candidate in snapshots
            if source_snapshot_available(candidate, utc_string(decision_time), receipt_cache)
        ), None)
        if snapshot is None:
            excluded["missing_prefight_snapshot"]["events"] += 1
            excluded["missing_prefight_snapshot"]["binary_bouts"] += len(results)
            continue
        try:
            snapshot_start = (
                parse_utc(str(snapshot["start_time_utc"]))
                if snapshot["start_time_utc"] else None
            )
        except ValueError:
            snapshot_start = None
        if (
            snapshot["event_status"] != "scheduled"
            or snapshot["event_date"] != event["event_date"]
            or snapshot["event_provider_status"] not in (None, "scheduled", "not_started")
            or snapshot_start != start
        ):
            excluded["roster_mismatch"]["events"] += 1
            excluded["roster_mismatch"]["binary_bouts"] += len(results)
            continue
        known_bouts = {
            str(bout["bout_id"]): bout for bout in connection.execute(
                "SELECT * FROM card_bout_snapshots WHERE event_snapshot_id = ?",
                (snapshot["event_snapshot_id"],),
            )
        }
        decision_times[event_id] = decision_time
        features_by_bout = {
            row.bout_id: row
            for row in event_feature_rows(
                connection, event_id, cutoff_at_utc=decision_time,
                require_result_observations=True, _receipt_cache=receipt_cache,
            )
        }
        mismatched_bouts = 0
        for result in results:
            scheduled = known_bouts.get(str(result["bout_id"]))
            if (
                scheduled is None
                or scheduled["bout_status"] != "scheduled"
                or scheduled["bout_provider_status"] not in (None, "scheduled", "not_started")
                or scheduled["outcome"] is not None
                or scheduled["fighter_a_id"] != result["fighter_a_id"]
                or scheduled["fighter_b_id"] != result["fighter_b_id"]
            ):
                excluded["roster_mismatch"]["binary_bouts"] += 1
                mismatched_bouts += 1
                continue
            base = features_by_bout.get(result["bout_id"])
            if base is None:
                raise ValueError(f"Completed result has no scoreable bout: {result['bout_id']}")
            first_id, second_id = sorted((base.fighter_a_id, base.fighter_b_id))
            winner = str(result["winner_fighter_id"])
            if winner not in {first_id, second_id}:
                raise ValueError(f"Winner is not a participant: {result['bout_id']}")
            features = base.features if base.fighter_a_id == first_id else tuple(-value for value in base.features)
            coverage = base.coverage if base.fighter_a_id == first_id else (
                base.coverage[1], base.coverage[0], base.coverage[3], base.coverage[2],
                base.coverage[5], base.coverage[4],
            )
            rows.append(
                FeatureRow(
                    bout_id=base.bout_id,
                    event_id=base.event_id,
                    event_date=base.event_date,
                    fighter_a_id=first_id,
                    fighter_b_id=second_id,
                    features=features,
                    target=int(winner == first_id),
                    coverage=coverage,
                    prior_result_rows_available=base.prior_result_rows_available,
                    prior_result_rows_observed=base.prior_result_rows_observed,
                )
            )
        if mismatched_bouts:
            excluded["roster_mismatch"]["events"] += 1
    return rows, decision_times, excluded


def _calibration_bins(probabilities: list[float], outcomes: list[int]) -> list[dict[str, float | int | None]]:
    """Five fixed probability bins, including an explicit empty-bin count."""
    result: list[dict[str, float | int | None]] = []
    for index in range(5):
        pairs = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes)
            if min(int(probability * 5), 4) == index
        ]
        count = len(pairs)
        result.append(
            {
                "lower": index / 5,
                "upper": (index + 1) / 5,
                "bouts": count,
                "mean_probability": sum(pair[0] for pair in pairs) / count if count else None,
                "observed_win_rate": sum(pair[1] for pair in pairs) / count if count else None,
            }
        )
    return result


def _bookmaker_probability(
    connection: sqlite3.Connection,
    row: FeatureRow,
    decision_time: datetime,
    max_quote_age_hours: float,
    receipt_cache: dict,
) -> float | None:
    """Return a no-vig probability for the first, canonically ordered fighter.

    A book is usable only when both sides share one source, capture time, and
    bookmaker market update time. Among usable two-sided markets, use the
    freshest capture that satisfies the point-in-time and freshness rules.
    """
    oldest_allowed = decision_time - timedelta(hours=max_quote_age_hours)
    fighters = {row.fighter_a_id, row.fighter_b_id}
    markets: dict[tuple[str, int, datetime, datetime], dict[str, set[float]]] = {}
    quotes = linked_quote_rows(connection, row.bout_id)
    for quote in quotes:
        if not quote_receipt_matches(quote, decision_time, receipt_cache):
            continue
        fighter_id = quote["selection_fighter_id"]
        if fighter_id not in fighters:
            continue
        captured = parse_utc(quote["captured_at_utc"])
        if not oldest_allowed <= captured <= decision_time:
            continue
        if not quote["bookmaker_updated_at_utc"]:
            continue
        updated = parse_utc(quote["bookmaker_updated_at_utc"])
        if not oldest_allowed <= updated <= captured:
            continue
        odds = float(quote["decimal_odds"])
        if not math.isfinite(odds) or odds <= 1.0:
            continue
        # Both sides must come from the same captured market and update, not
        # two individually recent but non-contemporaneous prices.
        key = (str(quote["bookmaker"]), int(quote["ingestion_run_id"]), captured, updated)
        markets.setdefault(key, {}).setdefault(str(fighter_id), set()).add(odds)

    paired: list[tuple[datetime, datetime, str, float]] = []
    for (bookmaker, _receipt_id, captured, updated), sides in markets.items():
        side_a = sides.get(row.fighter_a_id, set())
        side_b = sides.get(row.fighter_b_id, set())
        if len(side_a) != 1 or len(side_b) != 1:
            continue
        implied_a = 1.0 / next(iter(side_a))
        implied_b = 1.0 / next(iter(side_b))
        probability_a = implied_a / (implied_a + implied_b)
        paired.append((captured, updated, bookmaker, probability_a))
    if not paired:
        return None
    # Tie-breaking by book name is deterministic and is unrelated to outcome.
    paired.sort(key=lambda item: (-item[0].timestamp(), -item[1].timestamp(), item[2]))
    return paired[0][3]


def evaluate_models(
    connection: sqlite3.Connection,
    decision_hours_before_event: float = 24,
    max_quote_age_hours: float = 24,
) -> dict:
    """Compare Elo, a calibrated logistic model, and available book prices.

    This is an observational holdout evaluation, not a betting backtest.
    Odds absent from the database produce missing coverage rather than an
    estimated bookmaker benchmark. Dates without a known start time cannot
    have a fixed pre-event quote cutoff.
    """
    if not math.isfinite(decision_hours_before_event) or decision_hours_before_event <= 0:
        raise ValueError("decision_hours_before_event must be positive and finite")
    if not math.isfinite(max_quote_age_hours) or max_quote_age_hours <= 0:
        raise ValueError("max_quote_age_hours must be positive and finite")

    rows, decision_times, exclusions = _point_in_time_rows(
        connection, decision_hours_before_event
    )
    try:
        train, validation, test = chronological_event_split(rows)
    except ValueError as exc:
        available_evidence = _prior_result_evidence(rows)
        return {
            "status": "insufficient_history",
            "reason": str(exc),
            "available": _split_summary(rows),
            "observation_coverage": {"available": _observation_coverage(rows)},
            "prior_result_evidence": {
                "threshold": MIN_PRIOR_RESULT_EVIDENCE_COVERAGE,
                "available": available_evidence,
                "promotion_eligible": False,
            },
            "excluded_missing_start_time": exclusions["missing_start_time"],
            "excluded_missing_prefight_snapshot": exclusions["missing_prefight_snapshot"],
            "excluded_roster_mismatch": exclusions["roster_mismatch"],
            "sample_size_flags": {
                "fewer_than_three_event_dates": len({row.event_date for row in rows}) < 3,
                "warning": "There is not enough event history to create separate train, validation, and test periods.",
            },
            "split": None,
            "calibration": None,
            "test": None,
        }

    model = fit_logistic(train)
    validation_probabilities = [model.predict_probability(row.features) for row in validation]
    calibrator = fit_temperature(validation_probabilities, [int(row.target) for row in validation])
    outcomes = [int(row.target) for row in test]
    elo_probabilities = [1.0 / (1.0 + 10.0 ** (-row.features[0])) for row in test]
    logistic_probabilities = [calibrator.predict_probability(model.predict_probability(row.features)) for row in test]

    book_probabilities: list[float] = []
    book_outcomes: list[int] = []
    book_indices: list[int] = []
    quote_receipt_cache: dict = {}
    for index, row in enumerate(test):
        decision_time = decision_times[row.event_id]
        book_probability = _bookmaker_probability(
            connection, row, decision_time, max_quote_age_hours,
            quote_receipt_cache,
        )
        if book_probability is not None:
            book_probabilities.append(book_probability)
            book_outcomes.append(int(row.target))
            book_indices.append(index)

    result_evidence = {
        "threshold": MIN_PRIOR_RESULT_EVIDENCE_COVERAGE,
        "train": _prior_result_evidence(train),
        "validation": _prior_result_evidence(validation),
        "test": _prior_result_evidence(test),
    }
    result_evidence["promotion_eligible"] = all(
        result_evidence[split]["meets_threshold"]
        for split in ("train", "validation", "test")
    )
    result_evidence["warning"] = (
        None if result_evidence["promotion_eligible"] else
        "At least one split lacks complete source-observed prior-result history; "
        "the reported metrics are exploratory and must not promote the model."
    )

    return {
        "status": "ok" if result_evidence["promotion_eligible"] else "insufficient_result_evidence",
        "decision_hours_before_event": decision_hours_before_event,
        "max_quote_age_hours": max_quote_age_hours,
        "feature_history_rule": "results_from_earlier_utc_dates;_dated_observations_strictly_before_exact_decision_utc",
        "observation_coverage_names": COVERAGE_NAMES,
        "observation_coverage": {
            "train": _observation_coverage(train),
            "validation": _observation_coverage(validation),
            "test": _observation_coverage(test),
        },
        "prior_result_evidence": result_evidence,
        "excluded_missing_start_time": exclusions["missing_start_time"],
        "excluded_missing_prefight_snapshot": exclusions["missing_prefight_snapshot"],
        "excluded_roster_mismatch": exclusions["roster_mismatch"],
        "split": {
            "train": _split_summary(train),
            "validation": _split_summary(validation),
            "test": _split_summary(test),
        },
        "calibration": {
            "method": "symmetric_temperature",
            "validation_bouts": len(validation),
            "applied": len(validation) >= 30,
            "scale": calibrator.scale,
        },
        "sample_size_flags": {
            "train_under_30_bouts": len(train) < 30,
            "validation_under_30_bouts": len(validation) < 30,
            "test_under_30_bouts": len(test) < 30,
            "bookmaker_subset_under_30_bouts": len(book_probabilities) < 30,
            "warning": "Small samples make these metrics unstable; passing these counts does not establish a betting edge.",
        },
        "test": {
            "elo": {
                "metrics": binary_metrics(elo_probabilities, outcomes),
                "calibration_bins": _calibration_bins(elo_probabilities, outcomes),
            },
            "logistic": {
                "metrics": binary_metrics(logistic_probabilities, outcomes),
                "calibration_bins": _calibration_bins(logistic_probabilities, outcomes),
            },
            "bookmaker": {
                "available_bouts": len(book_probabilities),
                "coverage": len(book_probabilities) / len(test),
                "metrics": binary_metrics(book_probabilities, book_outcomes) if book_probabilities else None,
                "elo_on_available_bouts": binary_metrics(
                    [elo_probabilities[index] for index in book_indices], book_outcomes
                ) if book_probabilities else None,
                "logistic_on_available_bouts": binary_metrics(
                    [logistic_probabilities[index] for index in book_indices], book_outcomes
                ) if book_probabilities else None,
                "calibration_bins": _calibration_bins(book_probabilities, book_outcomes) if book_probabilities else None,
            },
        },
    }
