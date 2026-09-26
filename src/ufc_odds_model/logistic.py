"""Small, reproducible fight-winner logistic model using Python's stdlib.

Features are differences between fighters. The model has no intercept, so
swapping fighters yields complementary probabilities. Fit only on rows from
earlier event dates, then reserve later dates for calibration and evaluation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .features import FEATURE_NAMES, FeatureRow


MODEL_VERSION = "logistic-prior-v1"


def _sigmoid(score: float) -> float:
    if score >= 0:
        return 1.0 / (1.0 + math.exp(-score))
    exp_score = math.exp(score)
    return exp_score / (1.0 + exp_score)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small positive-definite Newton system with pivoting."""
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("Singular logistic Hessian; increase L2 regularization")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return [augmented[row][size] for row in range(size)]


@dataclass(frozen=True)
class LogisticModel:
    weights: tuple[float, ...]

    def predict_probability(self, features: Sequence[float]) -> float:
        if len(features) != len(self.weights):
            raise ValueError(f"Expected {len(self.weights)} features, got {len(features)}")
        if not all(math.isfinite(value) for value in features):
            raise ValueError("Features must be finite")
        return _sigmoid(sum(weight * value for weight, value in zip(self.weights, features)))


def fit_logistic(
    rows: Iterable[FeatureRow],
    l2: float = 0.02,
    max_iterations: int = 100,
    tolerance: float = 1e-9,
) -> LogisticModel:
    """Minimize mean log loss plus L2 penalty with damped Newton steps."""
    if not math.isfinite(l2) or l2 <= 0:
        raise ValueError("l2 must be finite and positive")
    if max_iterations <= 0 or tolerance <= 0:
        raise ValueError("Iterations and tolerance must be positive")
    examples = list(rows)
    if not examples:
        raise ValueError("At least one completed binary bout is required")
    dimension = len(FEATURE_NAMES)
    for row in examples:
        if row.target not in (0, 1):
            raise ValueError("Every training row needs a binary target")
        if len(row.features) != dimension or not all(math.isfinite(value) for value in row.features):
            raise ValueError(f"Every row needs {dimension} finite features")
    weights = [0.0] * dimension
    count = len(examples)

    def objective(candidate: Sequence[float]) -> float:
        loss = 0.0
        for row in examples:
            score = sum(weight * value for weight, value in zip(candidate, row.features))
            # log(1 + exp(score)) - y*score, evaluated without overflow.
            loss += max(score, 0.0) + math.log1p(math.exp(-abs(score))) - row.target * score
        return loss / count + 0.5 * l2 * sum(weight * weight for weight in candidate)

    for _ in range(max_iterations):
        gradient = [l2 * weight for weight in weights]
        hessian = [[l2 if i == j else 0.0 for j in range(dimension)] for i in range(dimension)]
        for row in examples:
            score = sum(weight * value for weight, value in zip(weights, row.features))
            probability = _sigmoid(score)
            variance = probability * (1.0 - probability) / count
            for i, x_i in enumerate(row.features):
                gradient[i] += (probability - row.target) * x_i / count
                for j, x_j in enumerate(row.features):
                    hessian[i][j] += variance * x_i * x_j
        if max(abs(value) for value in gradient) < tolerance:
            break
        delta = _solve(hessian, gradient)
        old_loss = objective(weights)
        step = 1.0
        while step > 1e-8:
            proposal = [weight - step * change for weight, change in zip(weights, delta)]
            if objective(proposal) <= old_loss:
                weights = proposal
                break
            step *= 0.5
        if step <= 1e-8 or max(abs(step * change) for change in delta) < tolerance:
            break
    return LogisticModel(tuple(weights))


def chronological_event_split(
    rows: Iterable[FeatureRow],
    train_fraction: float = 0.7,
    validation_fraction: float = 0.15,
) -> tuple[list[FeatureRow], list[FeatureRow], list[FeatureRow]]:
    """Split whole event dates into train, validation, and untouched test sets."""
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 - train_fraction:
        raise ValueError("Fractions must leave room for all three sets")
    ordered = sorted(rows, key=lambda row: (row.event_date, row.event_id, row.bout_id))
    dates = sorted({row.event_date for row in ordered})
    if len(dates) < 3:
        raise ValueError("At least three distinct event dates are required")
    train_days = min(max(1, int(len(dates) * train_fraction)), len(dates) - 2)
    validation_days = min(max(1, int(len(dates) * validation_fraction)), len(dates) - train_days - 1)
    train_end = dates[train_days - 1]
    validation_end = dates[train_days + validation_days - 1]
    train = [row for row in ordered if row.event_date <= train_end]
    validation = [row for row in ordered if train_end < row.event_date <= validation_end]
    test = [row for row in ordered if row.event_date > validation_end]
    return train, validation, test


@dataclass(frozen=True)
class TemperatureCalibrator:
    scale: float = 1.0

    def predict_probability(self, probability: float) -> float:
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Probability must be finite and within [0, 1]")
        clipped = min(max(probability, 1e-12), 1.0 - 1e-12)
        logit = math.log(clipped / (1.0 - clipped))
        return _sigmoid(self.scale * logit)


def fit_temperature(
    probabilities: Sequence[float],
    outcomes: Sequence[int],
    min_samples: int = 30,
) -> TemperatureCalibrator:
    """Fit a symmetric temperature on later validation events only.

    With fewer than ``min_samples`` observations, retain identity calibration.
    The scale is bounded to avoid an extreme fit to one validation period.
    """
    if len(probabilities) != len(outcomes):
        raise ValueError("Probabilities and outcomes must have equal lengths")
    if min_samples <= 0:
        raise ValueError("min_samples must be positive")
    for probability, outcome in zip(probabilities, outcomes):
        if not math.isfinite(probability) or not 0 <= probability <= 1 or outcome not in (0, 1):
            raise ValueError("Validation data must contain valid probabilities and binary outcomes")
    if len(probabilities) < min_samples:
        return TemperatureCalibrator()
    logits = [math.log(p / (1.0 - p)) for probability in probabilities
              for p in [min(max(probability, 1e-12), 1.0 - 1e-12)]]

    def loss(scale: float) -> float:
        total = 0.0
        for logit, outcome in zip(logits, outcomes):
            score = scale * logit
            total += max(score, 0.0) + math.log1p(math.exp(-abs(score))) - outcome * score
        # A light pull toward identity helps short validation periods.
        return total / len(outcomes) + 0.002 * (scale - 1.0) ** 2

    left, right = 0.25, 4.0
    for _ in range(80):
        point_a = left + (right - left) / 3.0
        point_b = right - (right - left) / 3.0
        if loss(point_a) < loss(point_b):
            right = point_b
        else:
            left = point_a
    return TemperatureCalibrator((left + right) / 2.0)


def binary_metrics(probabilities: Sequence[float], outcomes: Sequence[int]) -> dict[str, float | int]:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("Metrics need equally sized non-empty probability and outcome sequences")
    for probability, outcome in zip(probabilities, outcomes):
        if not math.isfinite(probability) or not 0 <= probability <= 1 or outcome not in (0, 1):
            raise ValueError("Metrics need valid probabilities and binary outcomes")
    count = len(outcomes)
    return {
        "bouts": count,
        "accuracy": sum(int((p >= 0.5) == bool(y)) for p, y in zip(probabilities, outcomes)) / count,
        "brier_score": sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / count,
        "log_loss": -sum(
            y * math.log(min(max(p, 1e-12), 1.0 - 1e-12))
            + (1 - y) * math.log(min(max(1.0 - p, 1e-12), 1.0 - 1e-12))
            for p, y in zip(probabilities, outcomes)
        ) / count,
    }
