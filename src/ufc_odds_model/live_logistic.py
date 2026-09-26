"""Point-in-time logistic probabilities for a scheduled UFC event.

This path has stricter sample gates than the exploratory holdout report. A
live forecast is unavailable until historical completed cards provide enough
training dates and a separate, later calibration period.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .evaluation import (
    MIN_PRIOR_RESULT_EVIDENCE_COVERAGE, _observation_coverage,
    _point_in_time_rows, _prior_result_evidence,
)
from .features import COVERAGE_NAMES, FEATURE_NAMES, FeatureRow, event_feature_rows
from .logistic import (
    MODEL_VERSION,
    LogisticModel,
    TemperatureCalibrator,
    fit_logistic,
    fit_temperature,
)
from .pipeline import parse_utc


MIN_BINARY_BOUTS = 100
MIN_EVENT_DATES = 10
MIN_TRAIN_BOUTS = 50
MIN_TRAIN_DATES = 5
MIN_VALIDATION_BOUTS = 30
MIN_VALIDATION_DATES = 2
DECISION_HOURS_BEFORE_EVENT = 24


@dataclass(frozen=True)
class LiveLogisticRun:
    model: LogisticModel
    calibrator: TemperatureCalibrator
    model_version: str
    predictions: dict[str, float]
    as_of_utc: str
    history_before_date: str
    training_bouts: int
    training_event_dates: int
    training_last_date: str
    calibration_bouts: int
    calibration_event_dates: int
    calibration_first_date: str
    calibration_last_date: str
    observation_coverage: dict[str, dict[str, object]]
    prior_result_evidence: dict[str, object]
    coverage_by_bout: dict[str, dict[str, bool]]


def _split_for_live(rows: list[FeatureRow]) -> tuple[list[FeatureRow], list[FeatureRow]]:
    """Keep whole event dates in time order and reserve a sizable tail."""
    ordered = sorted(rows, key=lambda row: (row.event_date, row.event_id, row.bout_id))
    dates = sorted({row.event_date for row in ordered})
    if len(ordered) < MIN_BINARY_BOUTS or len(dates) < MIN_EVENT_DATES:
        raise ValueError(
            "Live logistic unavailable: need at least "
            f"{MIN_BINARY_BOUTS} earlier binary bouts across {MIN_EVENT_DATES} event dates "
            f"(have {len(ordered)} bouts, {len(dates)} dates)"
        )
    target_validation = max(MIN_VALIDATION_BOUTS, math.ceil(len(ordered) * 0.20))
    validation_dates: set[str] = set()
    validation_count = 0
    for event_date in reversed(dates):
        if validation_count >= target_validation and len(validation_dates) >= MIN_VALIDATION_DATES:
            break
        validation_dates.add(event_date)
        validation_count += sum(row.event_date == event_date for row in ordered)
    training = [row for row in ordered if row.event_date not in validation_dates]
    validation = [row for row in ordered if row.event_date in validation_dates]
    training_dates = {row.event_date for row in training}
    if (
        len(training) < MIN_TRAIN_BOUTS
        or len(training_dates) < MIN_TRAIN_DATES
        or len(validation) < MIN_VALIDATION_BOUTS
        or len(validation_dates) < MIN_VALIDATION_DATES
    ):
        raise ValueError(
            "Live logistic unavailable: chronological split needs at least "
            f"{MIN_TRAIN_BOUTS} training bouts on {MIN_TRAIN_DATES} dates and "
            f"{MIN_VALIDATION_BOUTS} calibration bouts on {MIN_VALIDATION_DATES} "
            f"later dates (have {len(training)} / {len(training_dates)} and "
            f"{len(validation)} / {len(validation_dates)})"
        )
    return training, validation


def _model_version(
    training: list[FeatureRow],
    validation: list[FeatureRow],
    model: LogisticModel,
    calibrator: TemperatureCalibrator,
    history_before_date: str,
    target_features: list[FeatureRow],
) -> str:
    """Hash the exact labelled inputs, fitted parameters and cutoff rule."""
    payload = {
        "algorithm": MODEL_VERSION,
        "decision_hours_before_event": DECISION_HOURS_BEFORE_EVENT,
        "feature_names": FEATURE_NAMES,
        "history_before_date": history_before_date,
        "l2": 0.02,
        "min_calibration_samples": MIN_VALIDATION_BOUTS,
        "training": [
            (row.bout_id, row.event_id, row.event_date, row.fighter_a_id,
             row.fighter_b_id, row.features, row.target, row.coverage,
             row.prior_result_rows_available, row.prior_result_rows_observed)
            for row in training
        ],
        "calibration": [
            (row.bout_id, row.event_id, row.event_date, row.fighter_a_id,
             row.fighter_b_id, row.features, row.target, row.coverage,
             row.prior_result_rows_available, row.prior_result_rows_observed)
            for row in validation
        ],
        "target_features": [
            (row.bout_id, row.fighter_a_id, row.fighter_b_id, row.features, row.coverage)
            for row in target_features
        ],
        "weights": model.weights,
        "calibration_scale": calibrator.scale,
        "training_last_date": training[-1].event_date,
        "calibration_first_date": validation[0].event_date,
        "calibration_last_date": validation[-1].event_date,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return f"{MODEL_VERSION}:{digest[:16]}"


def prepare_live_logistic(
    connection: sqlite3.Connection,
    event_id: str,
    as_of_datetime: datetime,
) -> LiveLogisticRun:
    """Fit and calibrate using only cards that predate the UTC forecast day.

    Historical targets also require a known event start *before* ``as_of``.
    Their features use results from dates before their own fixed 24-hour
    decision time. The upcoming event's features use results from dates
    strictly before ``as_of``. Any insufficiency raises ``ValueError`` rather
    than silently emitting an uncalibrated production probability.
    """
    if as_of_datetime.tzinfo is None or as_of_datetime.utcoffset() is None:
        raise ValueError("as_of_datetime must be timezone-aware")
    as_of = as_of_datetime.astimezone(timezone.utc)
    cutoff_date = as_of.date().isoformat()
    event = connection.execute(
        "SELECT event_date, start_time_utc, status, provider_status FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None:
        raise ValueError(f"Unknown event: {event_id}")
    if event["status"] != "scheduled":
        raise ValueError("Live logistic requires a scheduled event")
    if event["provider_status"] not in (None, "scheduled", "not_started"):
        raise ValueError("Provider event status is not eligible for pre-fight scoring")
    if str(event["event_date"]) < cutoff_date:
        raise ValueError("Target event date is earlier than the forecast date")
    if event["start_time_utc"] and parse_utc(str(event["start_time_utc"])) <= as_of:
        raise ValueError("Target event has already started")
    target_bouts = connection.execute(
        "SELECT status, provider_status FROM bouts WHERE event_id = ? AND status != 'cancelled'",
        (event_id,),
    ).fetchall()
    if not target_bouts:
        raise ValueError("Target event has no scheduled bouts")
    if any(row["status"] != "scheduled" for row in target_bouts):
        raise ValueError("Live logistic requires all scoreable target bouts to be scheduled")
    if any(row["provider_status"] not in (None, "scheduled", "not_started") for row in target_bouts):
        raise ValueError("Provider bout status is not eligible for pre-fight scoring")

    # A completed card labelled with an earlier date but a future known start
    # would enter the date-based feature replay for other historical rows.
    # Refuse the forecast until that inconsistent record is reviewed.
    for past_event in connection.execute(
        """
        SELECT event_id, start_time_utc FROM events
        WHERE status = 'completed' AND event_date < ? AND start_time_utc IS NOT NULL
        """,
        (cutoff_date,),
    ):
        if parse_utc(str(past_event["start_time_utc"])) >= as_of:
            raise ValueError(
                "Completed history has an event start on or after the forecast time: "
                f"{past_event['event_id']}"
            )

    historical, decision_times, _ = _point_in_time_rows(
        connection, DECISION_HOURS_BEFORE_EVENT
    )
    historical_starts = {
        row["event_id"]: parse_utc(str(row["start_time_utc"]))
        for row in connection.execute(
            """
            SELECT event_id, start_time_utc FROM events
            WHERE status = 'completed' AND event_date < ? AND start_time_utc IS NOT NULL
            """,
            (cutoff_date,),
        )
    }
    eligible = [
        row for row in historical
        if row.event_date < cutoff_date
        and decision_times[row.event_id] < as_of
        and historical_starts[row.event_id] < as_of
    ]
    training, validation = _split_for_live(eligible)
    result_evidence = {
        "threshold": MIN_PRIOR_RESULT_EVIDENCE_COVERAGE,
        "training": _prior_result_evidence(training),
        "calibration": _prior_result_evidence(validation),
    }
    if not all(result_evidence[split]["meets_threshold"] for split in ("training", "calibration")):
        raise ValueError(
            "Live logistic unavailable: training and calibration need complete "
            "source-observed prior-result history at their decision cutoffs"
        )
    model = fit_logistic(training)
    validation_probabilities = [model.predict_probability(row.features) for row in validation]
    calibrator = fit_temperature(
        validation_probabilities,
        [int(row.target) for row in validation],
        min_samples=MIN_VALIDATION_BOUTS,
    )
    target_features = event_feature_rows(
        connection, event_id, cutoff_at_utc=as_of,
        require_result_observations=True,
    )
    target_result_evidence = _prior_result_evidence(target_features)
    if not target_result_evidence["meets_threshold"]:
        raise ValueError(
            "Live logistic unavailable: target features need complete "
            "source-observed prior-result history at the forecast cutoff"
        )
    result_evidence["target"] = target_result_evidence
    version = _model_version(training, validation, model, calibrator, cutoff_date, target_features)
    predictions = {
        row.bout_id: calibrator.predict_probability(model.predict_probability(row.features))
        for row in target_features
    }
    return LiveLogisticRun(
        model=model,
        calibrator=calibrator,
        model_version=version,
        predictions=predictions,
        as_of_utc=as_of.isoformat().replace("+00:00", "Z"),
        history_before_date=cutoff_date,
        training_bouts=len(training),
        training_event_dates=len({row.event_date for row in training}),
        training_last_date=training[-1].event_date,
        calibration_bouts=len(validation),
        calibration_event_dates=len({row.event_date for row in validation}),
        calibration_first_date=validation[0].event_date,
        calibration_last_date=validation[-1].event_date,
        observation_coverage={
            "training": _observation_coverage(training),
            "calibration": _observation_coverage(validation),
            "target": _observation_coverage(target_features),
        },
        prior_result_evidence=result_evidence,
        coverage_by_bout={
            row.bout_id: dict(zip(COVERAGE_NAMES, row.coverage)) for row in target_features
        },
    )
