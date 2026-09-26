"""Pre-fight alert gate: time, quote provenance, and price sensitivity."""

from __future__ import annotations

import unittest

from ufc_odds_model.alerts import gate_prefight_alerts


SNAPSHOT = "2026-09-26T12:00:00Z"
NOW = "2026-09-26T12:00:20Z"
START = "2026-09-26T16:00:00Z"


def row(**updates: object) -> dict[str, object]:
    scored = {
        "decision": "candidate",
        "quote_id": 77,
        "bookmaker": "test-book",
        "as_of_utc": "2026-09-26T12:00:05Z",
        "quote_captured_at_utc": SNAPSHOT,
        "bookmaker_updated_at_utc": "2026-09-26T11:59:50Z",
        "model_selection_probability": 0.55,
        "decimal_odds": 2.0,
    }
    scored.update(updates)
    return scored


def gate(scored: dict[str, object], **options: object) -> dict[str, object]:
    kwargs = {
        "snapshot_at_utc": SNAPSHOT,
        "event_start_time_utc": START,
        "now_utc": NOW,
    }
    kwargs.update(options)
    return gate_prefight_alerts([scored], **kwargs)[0]


class AlertGateTests(unittest.TestCase):
    def test_accepts_fresh_prefight_quote_only_after_price_drift(self) -> None:
        original = row()
        result = gate(original)
        self.assertEqual(result["alert_decision"], "alert_candidate")
        self.assertTrue(result["alert_eligible"])
        self.assertEqual(result["alert_reason"], "ok")
        self.assertEqual(result["conservative_decimal_odds"], 1.95)
        self.assertAlmostEqual(result["conservative_expected_profit_per_dollar"], 0.0725)
        self.assertNotIn("alert_decision", original)

    def test_rejects_stale_quote_even_if_report_is_fresh(self) -> None:
        result = gate(
            row(
                quote_captured_at_utc="2026-09-26T11:57:00Z",
                bookmaker_updated_at_utc="2026-09-26T11:56:50Z",
            ),
            snapshot_at_utc="2026-09-26T11:57:00Z",
        )
        self.assertEqual(result["alert_reason"], "stale_data")

    def test_rejects_stale_report_or_bookmaker_update(self) -> None:
        old_report = gate(row(as_of_utc="2026-09-26T11:58:00Z"))
        self.assertEqual(old_report["alert_reason"], "invalid_time_order")
        old_book = gate(row(bookmaker_updated_at_utc="2026-09-26T11:58:00Z"))
        self.assertEqual(old_book["alert_reason"], "stale_data")

    def test_rejects_after_event_start(self) -> None:
        result = gate(row(), event_start_time_utc="2026-09-26T12:00:20Z")
        self.assertEqual(result["alert_reason"], "event_started")

    def test_requires_exact_imported_snapshot(self) -> None:
        result = gate(row(), snapshot_at_utc="2026-09-26T12:00:01Z")
        self.assertEqual(result["alert_reason"], "different_snapshot")
        same_in_offset = gate(row(), snapshot_at_utc="2026-09-26T08:00:00-04:00")
        self.assertTrue(same_in_offset["alert_eligible"])

    def test_price_drift_can_remove_a_nominal_edge(self) -> None:
        nominal_edge = row(model_selection_probability=0.52, decimal_odds=2.0)
        self.assertGreater(0.52 * 2.0 - 1.0, 0.03)
        result = gate(nominal_edge, decimal_odds_drift=0.05)
        self.assertEqual(result["alert_reason"], "edge_below_floor")
        self.assertFalse(result["alert_eligible"])

    def test_invalid_and_missing_times_fail_closed(self) -> None:
        self.assertEqual(gate(row(bookmaker_updated_at_utc=""))["alert_reason"], "invalid_row_time")
        self.assertEqual(gate(row(as_of_utc="2026-09-26T12:00:05"))["alert_reason"], "invalid_row_time")
        self.assertEqual(gate(row(), event_start_time_utc="")["alert_reason"], "invalid_reference_time")
        self.assertEqual(
            gate(row(bookmaker_updated_at_utc="2026-09-26T12:00:01Z"))["alert_reason"],
            "invalid_time_order",
        )

    def test_rejects_pass_rows_and_invalid_config(self) -> None:
        self.assertEqual(gate(row(decision="pass"))["alert_reason"], "not_model_candidate")
        with self.assertRaises(ValueError):
            gate(row(), max_age_seconds=0)


if __name__ == "__main__":
    unittest.main()
