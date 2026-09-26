"""Fail-closed checks for the archived card cohort review."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.review_historical_pilot import review_cohort, validate_manifest


EVENT = {
    "slug": "example",
    "page_id": 123,
    "event_date": "2026-06-06",
    "earliest_start_at_utc": "2026-06-06T21:00:00Z",
    "prefight_cutoff_at_utc": "2026-06-05T21:00:00Z",
    "result_cutoff_at_utc": "2026-06-13T21:00:00Z",
    "official_start_url": "https://www.ufc.com/news/example",
}


class HistoricalPilotTests(unittest.TestCase):
    def test_manifest_rejects_wrong_cutoffs_and_spoofed_official_url(self):
        self.assertEqual(validate_manifest({"schema_version": 1, "events": [EVENT]}),
                         [EVENT])
        for changed in (
            {"prefight_cutoff_at_utc": "2026-06-05T21:00:01Z"},
            {"official_start_url": "https://www.ufc.com.evil.test/news/example"},
            {"official_start_url": "https://www.ufc.com:443/news/example"},
            {"result_cutoff_at_utc": "2026-06-06T20:00:00Z"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_manifest({"schema_version": 1, "events": [{**EVENT, **changed}]})

    def test_later_result_cutoff_must_equal_next_prefight_cutoff(self):
        next_event = {**EVENT, "slug": "next", "page_id": 124,
                      "event_date": "2026-06-14",
                      "earliest_start_at_utc": "2026-06-14T21:00:00Z",
                      "prefight_cutoff_at_utc": "2026-06-13T21:00:00Z",
                      "result_cutoff_at_utc": "2026-06-21T21:00:00Z"}
        validate_manifest({"schema_version": 1, "events": [EVENT, next_event]})
        with self.assertRaisesRegex(ValueError, "next event"):
            validate_manifest({"schema_version": 1, "events": [
                {**EVENT, "result_cutoff_at_utc": "2026-06-13T20:00:00Z"},
                next_event,
            ]})

    def test_only_stable_id_paired_binary_bouts_are_counted(self):
        def fake_review(selection, content, research_db, kind):
            common = {"event_id": "wikipedia_research:123", "event_date": "2026-06-06",
                      "source_bouts": 5, "held_bouts": 2, "revision_id": 100,
                      "revision_timestamp_utc": "2026-06-05T12:00:00Z"}
            if kind == "scheduled":
                return {**common, "decision_cutoff_at_utc": EVENT["prefight_cutoff_at_utc"],
                        "matches": [
                    {"bout_id": 1, "identity_verified": True},
                    {"bout_id": 2, "identity_verified": True},
                    {"bout_id": 3, "identity_verified": False},
                    {"bout_id": 4, "identity_verified": True},
                ]}
            return {**common, "decision_cutoff_at_utc": EVENT["result_cutoff_at_utc"],
                    "matches": [
                {"bout_id": 1, "identity_verified": True, "outcome": "win"},
                {"bout_id": 2, "identity_verified": True, "outcome": "draw"},
                {"bout_id": 3, "identity_verified": True, "outcome": "win"},
            ]}

        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "cohort.json"
            manifest.write_text(json.dumps({"schema_version": 1, "events": [EVENT]}))
            with patch("scripts.review_historical_pilot.review", side_effect=fake_review):
                report = review_cohort(manifest, Path(temp) / "research.sqlite",
                                       Path(temp) / "raw")
        self.assertFalse(report["model_eligible"])
        self.assertEqual(report["totals"]["paired_bouts"], 2)
        self.assertEqual(report["totals"]["paired_binary_bouts"], 1)
        self.assertEqual(report["totals"]["prefight_held_bouts"], 2)

    def test_misnamed_receipt_cannot_change_decision_cutoff(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "cohort.json"
            manifest.write_text(json.dumps({"schema_version": 1, "events": [EVENT]}))
            fake = {"event_id": "wikipedia_research:123", "event_date": "2026-06-06",
                    "decision_cutoff_at_utc": "2026-06-05T22:00:00Z"}
            with patch("scripts.review_historical_pilot.review", return_value=fake):
                with self.assertRaisesRegex(ValueError, "cutoff changed"):
                    review_cohort(manifest, Path(temp) / "research.sqlite",
                                  Path(temp) / "raw")


if __name__ == "__main__":
    unittest.main()
