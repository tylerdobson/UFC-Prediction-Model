"""Checks for the stdlib logistic model and chronological validation tools."""

from __future__ import annotations

import unittest

from ufc_odds_model.features import FEATURE_NAMES, FeatureRow
from ufc_odds_model.logistic import (
    binary_metrics,
    chronological_event_split,
    fit_logistic,
    fit_temperature,
)


def row(day: int, feature: float, target: int) -> FeatureRow:
    return FeatureRow(
        bout_id=f"{day}-{feature}-{target}",
        event_id=f"event-{day}",
        event_date=f"2024-01-{day:02d}",
        fighter_a_id="a",
        fighter_b_id="b",
        features=(feature,) + (0.0,) * (len(FEATURE_NAMES) - 1),
        target=target,
    )


class LogisticTests(unittest.TestCase):
    def test_fit_learns_signal_and_preserves_pair_symmetry(self):
        examples = [row(day, 1.0, 1) for day in range(1, 11)]
        examples += [row(day, -1.0, 0) for day in range(11, 21)]
        model = fit_logistic(examples)
        positive = model.predict_probability((1.0,) + (0.0,) * (len(FEATURE_NAMES) - 1))
        negative = model.predict_probability((-1.0,) + (0.0,) * (len(FEATURE_NAMES) - 1))
        self.assertGreater(positive, 0.5)
        self.assertAlmostEqual(positive + negative, 1.0)
        self.assertEqual(model.predict_probability((0.0,) * len(FEATURE_NAMES)), 0.5)

    def test_split_keeps_whole_dates_in_order(self):
        examples = [row(day, 1.0, 1) for day in range(1, 11)]
        examples += [row(5, -1.0, 0)]
        train, validation, test = chronological_event_split(reversed(examples))
        self.assertTrue(train and validation and test)
        train_dates = {item.event_date for item in train}
        validation_dates = {item.event_date for item in validation}
        test_dates = {item.event_date for item in test}
        self.assertTrue(train_dates.isdisjoint(validation_dates | test_dates))
        self.assertTrue(validation_dates.isdisjoint(test_dates))
        self.assertLess(max(train_dates), min(validation_dates))
        self.assertLess(max(validation_dates), min(test_dates))

    def test_temperature_softens_overconfident_validation_predictions(self):
        probabilities = [0.9] * 40
        outcomes = [1, 0] * 20
        calibrator = fit_temperature(probabilities, outcomes)
        self.assertLess(calibrator.scale, 1.0)
        self.assertAlmostEqual(
            calibrator.predict_probability(0.9) + calibrator.predict_probability(0.1), 1.0
        )
        self.assertEqual(fit_temperature(probabilities[:5], outcomes[:5]).scale, 1.0)

    def test_metrics(self):
        metrics = binary_metrics([0.9, 0.1], [1, 0])
        self.assertEqual(metrics["bouts"], 2)
        self.assertEqual(metrics["accuracy"], 1.0)
        self.assertAlmostEqual(metrics["brier_score"], 0.01)


if __name__ == "__main__":
    unittest.main()
