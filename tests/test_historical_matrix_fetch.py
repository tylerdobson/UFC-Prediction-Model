"""Matrix acquisition chooses every earlier card at each later decision."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.fetch_historical_matrix import fetch_matrix, required_matrix


def _events() -> list[dict]:
    dates = ["2026-06-06", "2026-06-13", "2026-06-20"]
    events = []
    for index, day in enumerate(dates):
        pre = f"{int(day[:4]):04d}-{int(day[5:7]):02d}-{int(day[8:]):02d}T00:00:00Z"
        # A midnight event's 24-hour cutoff is the preceding UTC date.
        from datetime import datetime, timedelta, timezone
        start = datetime.fromisoformat(pre.replace("Z", "+00:00"))
        events.append({
            "slug": f"event{index}", "page_id": index + 1,
            "event_date": day,
            "earliest_start_at_utc": pre,
            "prefight_cutoff_at_utc": (start - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "result_cutoff_at_utc": (
                (start + timedelta(days=6)) if index == 2
                else datetime.fromisoformat(dates[index + 1] + "T00:00:00+00:00") - timedelta(days=1)
            ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "official_start_url": f"https://www.ufc.com/news/event{index}",
        })
    return events


class HistoricalMatrixFetchTests(unittest.TestCase):
    def test_every_earlier_event_is_selected_at_each_later_cutoff(self):
        events = _events()
        self.assertEqual(required_matrix(events), [
            ("event0", 1, events[1]["prefight_cutoff_at_utc"], "event1"),
            ("event0", 1, events[2]["prefight_cutoff_at_utc"], "event2"),
            ("event1", 2, events[2]["prefight_cutoff_at_utc"], "event2"),
        ])

    def test_bounded_fetch_stops_before_second_new_proof(self):
        events = _events()
        calls = []

        def fake_save(path, role, page_id, cutoff):
            calls.append((path, role, page_id, cutoff))
            return {"cutoff_at_utc": cutoff, "expected_page_id": page_id,
                    "revision_id": 10, "revision_timestamp_utc": "2026-06-07T00:00:00Z"}

        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "events": events}))
            result = fetch_matrix(manifest, Path(temp) / "raw", max_new_proofs=1,
                                  save=fake_save, sleep=lambda _: None)
        self.assertEqual(result, {"required_pairs": 3, "new_proofs": 1,
                                  "reused_proofs": 0, "unvisited_pairs": 2})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], ("result", 1, events[1]["prefight_cutoff_at_utc"]))

    def test_mismatched_saved_proof_is_rejected(self):
        events = _events()
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "events": events}))
            with self.assertRaisesRegex(ValueError, "differs from manifest"):
                fetch_matrix(manifest, Path(temp) / "raw", max_new_proofs=1,
                             save=lambda *args: {"cutoff_at_utc": "2026-01-01T00:00:00Z",
                                                 "expected_page_id": 1},
                             sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
