"""Identity leads must preserve source evidence and leave database IDs unchanged."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.identity_review import build_identity_review


class IdentityReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        db.init_db(self.connection)
        db.upsert_fighter(self.connection, "wikipedia:42", "Alex Smith", "wikipedia", "42")
        self.connection.commit()
        source_url = "https://en.wikipedia.org/wiki/UFC_Example"
        source = {
            "id": 123,
            "title": "UFC Example",
            "latest": {"id": 77, "timestamp": "2026-01-01T00:00:00Z"},
            "source": "== Results ==\n[[Alex Smith]] fought Known Fighter.\n",
            "license": {"url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
        }
        payload = json.dumps(source).encode()
        self.source_path = self.directory / "event.json"
        self.source_path.write_bytes(payload)
        receipt = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES (?, ?, ?, ?)",
            ("wikipedia_research", "2026-01-02T00:00:00Z", str(self.source_path),
             hashlib.sha256(payload).hexdigest()),
        )
        self.connection.execute(
            "INSERT INTO wikipedia_source_receipts("
            "run_id, event_page_id, event_title, page_url, revision_id, "
            "revision_timestamp_utc, license_title, license_url, imported_bouts, "
            "skipped_unresolved_bouts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt.lastrowid, 123, "UFC Example", source_url, 77,
             "2026-01-01T00:00:00Z", "CC BY-SA 4.0",
             "https://creativecommons.org/licenses/by-sa/4.0/deed.en", 0, 1),
        )
        self.connection.commit()
        self.review_path = self.directory / "review.json"
        self.review_path.write_text(json.dumps({
            "schema_version": 1,
            "source": "wikipedia_research",
            "unresolved_fighters": [{
                "event_id": "wikipedia_research:123", "event_title": "UFC Example",
                "bout_position": 1, "fighter_name": "Alex Smith", "linked_title": None,
                "reason": "unlinked_name", "source_url": source_url,
            }],
            "skipped_bouts": [{
                "event_id": "wikipedia_research:123", "bout_position": 1,
                "fighter_a": "Alex Smith", "fighter_b": "Known Fighter",
                "outcome": "win", "reason": "unresolved_fighter_identity",
            }],
        }))

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_offline_report_retains_revision_and_never_merges_by_name(self):
        report = build_identity_review(self.connection, [self.review_path])
        entry = report["fighters"][0]
        self.assertEqual(report["summary"]["held_bouts"], 1)
        self.assertEqual(report["summary"]["automatically_resolved_bouts"], 0)
        self.assertEqual(entry["exact_existing_fighters"][0]["fighter_id"], "wikipedia:42")
        self.assertEqual(entry["same_revision_link_titles"], ["Alex Smith"])
        self.assertEqual(entry["occurrences"][0]["source_revision_url"],
                         "https://en.wikipedia.org/w/index.php?oldid=77")
        self.assertEqual(entry["review_status"], "pending_human_identity_review")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM fighters").fetchone()[0], 1)

    def test_online_lookup_and_page_check_keep_the_exact_responses(self):
        urls = []

        def fake_fetch(url: str) -> dict:
            urls.append(url)
            if "/w/api.php?" in url:
                return {"query": {"pages": [{
                    "title": "Alex Smith", "pageid": 42, "ns": 0,
                }]}}
            return {
                "id": 42, "title": "Alex Smith",
                "latest": {"id": 88, "timestamp": "2026-01-03T00:00:00Z"},
                "source": "Alex Smith is a UFC mixed martial artist who fought Known Fighter.",
                "license": {"url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            }

        report = build_identity_review(
            self.connection, [self.review_path], lookup_wikipedia=True,
            inspect_candidate_pages=True,
            lookup_raw_dir=self.directory / "lookups", fetch_json=fake_fetch,
        )
        candidate = report["fighters"][0]["current_wikipedia_title_candidate"]
        self.assertEqual(len(urls), 2)
        self.assertEqual(candidate["fighter_id"], "wikipedia:42")
        self.assertEqual(candidate["current_page_evidence"]["mentioned_opponents_from_held_bouts"],
                         ["Known Fighter"])
        self.assertFalse(candidate["current_page_evidence"]["automated_text_checks_are_identity_proof"])
        for kind in ("lookup_receipt", "source_receipt"):
            info = candidate if kind == "lookup_receipt" else candidate["current_page_evidence"]
            path = Path(info[kind + "_path"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             info[kind + "_sha256"])
            self.assertIn("request_url", json.loads(path.read_text()))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 0)

    def test_changed_receipt_fails_closed(self):
        self.source_path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "Changed source receipt"):
            build_identity_review(self.connection, [self.review_path])

    def test_later_page_revision_cannot_be_assigned_to_older_review(self):
        source_url = "https://en.wikipedia.org/wiki/UFC_Example"
        later_payload = json.dumps({
            "id": 123, "title": "UFC Example",
            "latest": {"id": 78, "timestamp": "2026-01-04T00:00:00Z"},
            "source": "== Results ==\nKnown Fighter fought a different opponent.\n",
            "license": {"url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
        }).encode()
        later_path = self.directory / "later-event.json"
        later_path.write_bytes(later_payload)
        receipt = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES (?, ?, ?, ?)",
            ("wikipedia_research", "2026-01-04T00:00:00Z", str(later_path),
             hashlib.sha256(later_payload).hexdigest()),
        )
        self.connection.execute(
            "INSERT INTO wikipedia_source_receipts("
            "run_id, event_page_id, event_title, page_url, revision_id, "
            "revision_timestamp_utc, license_title, license_url, imported_bouts, "
            "skipped_unresolved_bouts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt.lastrowid, 123, "UFC Example", source_url, 78,
             "2026-01-04T00:00:00Z", "CC BY-SA 4.0",
             "https://creativecommons.org/licenses/by-sa/4.0/deed.en", 0, 0),
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "Multiple source revisions"):
            build_identity_review(self.connection, [self.review_path])

    def test_repeat_import_of_same_revision_is_unambiguous(self):
        source_url = "https://en.wikipedia.org/wiki/UFC_Example"
        receipt = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES (?, ?, ?, ?)",
            ("wikipedia_research", "2026-01-03T00:00:00Z", str(self.source_path),
             hashlib.sha256(self.source_path.read_bytes()).hexdigest()),
        )
        self.connection.execute(
            "INSERT INTO wikipedia_source_receipts("
            "run_id, event_page_id, event_title, page_url, revision_id, "
            "revision_timestamp_utc, license_title, license_url, imported_bouts, "
            "skipped_unresolved_bouts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt.lastrowid, 123, "UFC Example", source_url, 77,
             "2026-01-01T00:00:00Z", "CC BY-SA 4.0",
             "https://creativecommons.org/licenses/by-sa/4.0/deed.en", 0, 1),
        )
        self.connection.commit()
        report = build_identity_review(self.connection, [self.review_path])
        self.assertEqual(report["fighters"][0]["occurrences"][0]["source_revision_id"], 77)

    def test_current_title_namesake_stays_a_pending_lead(self):
        def fake_fetch(url: str) -> dict:
            if "/w/api.php?" in url:
                return {"query": {"pages": [{
                    "title": "Alex Smith", "pageid": 99, "ns": 0,
                }]}}
            return {
                "id": 99, "title": "Alex Smith",
                "latest": {"id": 89, "timestamp": "2026-01-03T00:00:00Z"},
                "source": "Alex Smith is a singer who toured with Known Fighter.",
                "license": {"url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            }

        report = build_identity_review(
            self.connection, [self.review_path], lookup_wikipedia=True,
            inspect_candidate_pages=True,
            lookup_raw_dir=self.directory / "namesake", fetch_json=fake_fetch,
        )
        entry = report["fighters"][0]
        self.assertEqual(entry["exact_existing_fighters"][0]["fighter_id"], "wikipedia:42")
        self.assertEqual(entry["current_wikipedia_title_candidate"]["fighter_id"], "wikipedia:99")
        self.assertEqual(report["summary"]["held_bouts_with_candidate_page_opponent_text_for_all_unresolved_sides"], 0)
        self.assertEqual(report["summary"]["automatically_resolved_bouts"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM fighters").fetchone()[0], 1)

    def test_page_inspection_requires_lookup(self):
        with self.assertRaisesRegex(ValueError, "requires a Wikipedia title lookup"):
            build_identity_review(self.connection, [self.review_path], inspect_candidate_pages=True)

    def test_repeated_review_rows_are_counted_once_and_conflicts_fail(self):
        report = build_identity_review(self.connection, [self.review_path, self.review_path])
        self.assertEqual(report["summary"]["held_bouts"], 1)
        self.assertEqual(report["summary"]["unresolved_fighter_rows"], 1)
        self.assertEqual(len(report["fighters"][0]["occurrences"]), 1)
        changed = json.loads(self.review_path.read_text(encoding="utf-8"))
        changed["skipped_bouts"][0]["fighter_b"] = "Someone else"
        other = self.directory / "conflict.json"
        other.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Conflicting repeated held bout"):
            build_identity_review(self.connection, [self.review_path, other])


if __name__ == "__main__":
    unittest.main()
