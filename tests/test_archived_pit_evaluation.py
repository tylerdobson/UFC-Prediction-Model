"""The archived replay needs every prior publisher revision at each cutoff."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from ufc_odds_model import db
from ufc_odds_model.archived_pit_evaluation import (
    _sidecar, evaluate_archived_pit,
)
from ufc_odds_model.wikipedia_history import ACTION_API


class ArchivedPitEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "research.sqlite"
        connection = db.connect(self.database)
        db.init_db(connection)
        lookup_url = ACTION_API + "?" + urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "redirects": "1", "titles": "Fighter A|Fighter B", "maxlag": "5",
            "prop": "pageprops", "ppprop": "disambiguation",
        })
        self.lookup_path = self.root / "lookup.json"
        self.lookup_path.write_text(json.dumps({
            "request_url": lookup_url, "response": {"query": {"pages": []}},
        }))
        self.lookup_sha256 = hashlib.sha256(self.lookup_path.read_bytes()).hexdigest()
        cursor = connection.execute(
            """INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256)
               VALUES ('wikipedia_action_api', '2026-09-26T00:00:00Z', ?, ?)""",
            (str(self.lookup_path), self.lookup_sha256),
        )
        self.lookup_run_id = int(cursor.lastrowid)
        connection.commit()
        connection.close()
        self.events = []
        for index, day in enumerate((1, 8, 15), 1):
            event_date = date(2026, 6, day)
            start = f"{event_date.isoformat()}T21:00:00Z"
            pre = f"{(event_date - timedelta(days=1)).isoformat()}T21:00:00Z"
            next_date = event_date + timedelta(days=7)
            self.events.append({
                "slug": f"event{index}", "page_id": index,
                "event_date": event_date.isoformat(),
                "earliest_start_at_utc": start,
                "prefight_cutoff_at_utc": pre,
                "result_cutoff_at_utc": f"{(next_date - timedelta(days=1)).isoformat()}T21:00:00Z",
                "official_start_url": f"https://www.ufc.com/news/event-{index}",
            })
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"schema_version": 1, "events": self.events}))
        self.raw_root = self.root / "raw"

    def reviewed(self, item, role, cutoff, raw_root, *, changed_later=False,
                 missing_later=False, mismatched=False):
        self.assertEqual(raw_root, self.raw_root)
        if (missing_later and item["slug"] == "event1" and role == "result"
                and cutoff == self.events[2]["prefight_cutoff_at_utc"]):
            raise FileNotFoundError("missing selected revision")
        winner = "wikipedia:101" if item["page_id"] != 2 else "wikipedia:102"
        if (changed_later and item["slug"] == "event1" and role == "result"
                and cutoff == self.events[2]["prefight_cutoff_at_utc"]):
            winner = "wikipedia:102"
        match = {
            "bout_id": f"bout-{item['page_id']}",
            "fighter_a_id": "wikipedia:101", "fighter_b_id": "wikipedia:102",
            "identity_verified": True,
            "identity_lookup_run_ids": [self.lookup_run_id],
            "outcome": "win" if role == "result" else None,
            "winner_fighter_id": winner if role == "result" else None,
        }
        return {
            "slug": item["slug"], "role": role,
            "kind": "completed" if role == "result" else "scheduled",
            "event_id": f"wikipedia_research:{item['page_id']}",
            "event_date": item["event_date"],
            "cutoff_at_utc": cutoff,
            "decision_cutoff_at_utc": (
                "2026-06-01T00:00:00Z" if mismatched else cutoff),
            "source_bouts": 1, "matches": [match],
            "revision_id": 1000 + item["page_id"],
            "revision_timestamp_utc": item["event_date"] + "T23:00:00Z",
            "selection_sidecar_sha256": "1" * 64,
            "selection_sha256": "2" * 64,
            "content_sidecar_sha256": "3" * 64,
            "content_sha256": "4" * 64,
            "wikitext_sha256": "5" * 64,
            "selection_fetched_at_utc": "2026-09-26T00:00:00Z",
            "content_fetched_at_utc": "2026-09-26T00:00:01Z",
        }

    def evaluate(self, **kwargs):
        def fake(connection, item, role, cutoff, raw_root):
            return self.reviewed(item, role, cutoff, raw_root, **kwargs)

        with patch("ufc_odds_model.archived_pit_evaluation.review_archived_card",
                   side_effect=fake) as reviewer:
            before = hashlib.sha256(self.database.read_bytes()).hexdigest()
            report = evaluate_archived_pit(self.manifest, self.database, self.raw_root)
            after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(report["research_database_sha256"], before)
        return report, reviewer.call_args_list

    def test_requires_every_earlier_result_at_each_target_cutoff(self):
        report, calls = self.evaluate(missing_later=True)
        self.assertEqual(report["status"], "research_only")
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["evaluation_status"], "insufficient_verified_history")
        self.assertEqual(report["coverage"]["scored_binary_bouts"], 2)
        self.assertEqual(report["event_coverage"][-1]["scored_binary_bouts"], 0)
        self.assertEqual(report["holds"][-1]["reason"], "incomplete_prior_revision_matrix")
        self.assertEqual(report["holds"][-1]["missing_prior_events"], ["event1"])
        self.assertIn(
            ("event1", "result", self.events[2]["prefight_cutoff_at_utc"]),
            {(call.args[1]["slug"], call.args[2], call.args[3]) for call in calls},
        )
        self.assertEqual(report["bookmaker"]["roi"], None)

    def test_full_matrix_uses_latest_result_selected_for_each_target(self):
        unchanged, _ = self.evaluate()
        corrected, calls = self.evaluate(changed_later=True)
        self.assertEqual(corrected["status"], "research_only")
        self.assertEqual(corrected["evaluation_status"], "exploratory_chronological_holdout")
        self.assertEqual(corrected["coverage"]["scored_binary_bouts"], 3)
        self.assertEqual(corrected["split"]["train"]["event_dates"], 1)
        self.assertEqual(corrected["split"]["validation"]["event_dates"], 1)
        self.assertEqual(corrected["split"]["test"]["event_dates"], 1)
        self.assertFalse(corrected["calibration"]["applied"])
        self.assertNotEqual(unchanged["feature_rows_sha256"], corrected["feature_rows_sha256"])
        self.assertNotEqual(unchanged["checked_input_sha256"], corrected["checked_input_sha256"])
        self.assertEqual(corrected["identity_lookup_receipts"], [{
            "run_id": self.lookup_run_id, "source": "wikipedia_action_api",
            "fetched_at_utc": "2026-09-26T00:00:00Z",
            "payload_path": str(self.lookup_path.resolve()),
            "payload_sha256": self.lookup_sha256,
            "request_url": json.loads(self.lookup_path.read_text())["request_url"],
        }])
        self.assertTrue(all(
            proof["selection_sidecar_sha256"] == "1" * 64
            and proof["selection_sha256"] == "2" * 64
            and proof["content_sidecar_sha256"] == "3" * 64
            and proof["content_sha256"] == "4" * 64
            for proof in corrected["proof_evidence"]
        ))
        included = [proof for proof in corrected["proof_evidence"]
                    if proof["included_matches"]]
        self.assertTrue(included)
        self.assertTrue(all(
            binding["identity_lookup_run_ids"] == [self.lookup_run_id]
            for proof in included for binding in proof["included_matches"]
        ))
        selected_event1_results = {
            call.args[3] for call in calls
            if call.args[1]["slug"] == "event1" and call.args[2] == "result"
        }
        self.assertEqual(selected_event1_results, {
            self.events[0]["result_cutoff_at_utc"],
            self.events[2]["prefight_cutoff_at_utc"],
        })

    def test_changed_cutoff_is_held_without_scoring(self):
        report, _ = self.evaluate(mismatched=True)
        self.assertEqual(report["coverage"]["scored_binary_bouts"], 0)
        self.assertEqual(report["evaluation_status"], "insufficient_verified_history")
        self.assertEqual({hold["reason"] for hold in report["holds"]},
                         {"invalid_target_proof"})

    def test_sidecar_cannot_redirect_to_another_response(self):
        sidecar = self.root / "result-20260607T210000Z.selection.receipt.json"
        sidecar.write_text(json.dumps({
            "page_id": 1, "cutoff_utc": "2026-06-07T21:00:00Z",
            "role": "result", "response_kind": "selection",
            "response_path": str(self.root / "other.response.json"),
            "response_sha256": "0" * 64,
            "request_url": "https://en.wikipedia.org/w/api.php",
            "fetched_at_utc": "2026-09-26T00:00:00Z",
        }))
        with self.assertRaisesRegex(ValueError, "fixed sibling"):
            _sidecar(sidecar, page_id=1, cutoff="2026-06-07T21:00:00Z",
                     role="result", kind="selection")

    def test_sidecar_metadata_is_hashed_with_its_response_binding(self):
        sidecar = self.root / "result-20260607T210000Z.selection.receipt.json"
        response = self.root / "result-20260607T210000Z.selection.response.json"
        payload = {
            "page_id": 1, "cutoff_utc": "2026-06-07T21:00:00Z",
            "role": "result", "response_kind": "selection",
            "response_path": str(response), "response_sha256": "a" * 64,
            "request_url": "https://en.wikipedia.org/w/api.php",
            "fetched_at_utc": "2026-09-26T00:00:00Z",
        }
        sidecar.write_text(json.dumps(payload))
        receipt, sidecar_hash = _sidecar(
            sidecar, page_id=1, cutoff="2026-06-07T21:00:00Z",
            role="result", kind="selection",
        )
        self.assertEqual(sidecar_hash, hashlib.sha256(sidecar.read_bytes()).hexdigest())
        self.assertEqual(receipt.sha256, "a" * 64)
        self.assertEqual(Path(receipt.path), response)

    def test_changed_identity_payload_and_uncheckpointed_wal_fail_closed(self):
        self.lookup_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "lookup receipt.*invalid"):
            self.evaluate()
        wal = Path(str(self.database) + "-wal")
        wal.write_bytes(b"uncheckpointed")
        with self.assertRaisesRegex(ValueError, "nonempty -wal"):
            evaluate_archived_pit(self.manifest, self.database, self.raw_root)


if __name__ == "__main__":
    unittest.main()
