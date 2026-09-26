"""The retrospective path is date-ordered, source-isolated, and non-operating."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.research_evaluation import (
    _held_bouts_by_event,
    evaluate_research_history,
    main,
    research_feature_rows,
)


class ResearchEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def add_result(
        self, event_id: str, event_date: str, bout_id: str,
        a_id: str, b_id: str, winner: str | None,
        *, source: str = "wikipedia_research", outcome: str = "win",
    ) -> None:
        for fighter_id in (a_id, b_id):
            db.upsert_fighter(self.connection, fighter_id, fighter_id)
        db.upsert_event(
            self.connection, event_id, event_id, event_date, "completed",
            source=source, source_event_id=(
                event_id.removeprefix("wikipedia_research:")
                if source == "wikipedia_research" and event_id.startswith("wikipedia_research:")
                else event_id
            ),
        )
        db.upsert_bout(
            self.connection, bout_id, event_id, a_id, b_id, "completed",
            source=source, source_bout_id=bout_id,
        )
        db.upsert_result(
            self.connection, bout_id, outcome, winner,
            f"{event_date}T23:00:00Z",
        )

    def attach_receipts(self, held_by_event: dict[str, int] | None = None) -> None:
        held_by_event = held_by_event or {}
        for event in self.connection.execute(
            """SELECT e.event_id, e.source_event_id, COUNT(b.bout_id) AS bouts
               FROM events e LEFT JOIN bouts b ON b.event_id = e.event_id
                 AND b.status = 'completed'
               WHERE e.source = 'wikipedia_research'
               GROUP BY e.event_id"""
        ).fetchall():
            page_id = int(str(event["source_event_id"]).split(":")[0])
            payload = self.root / f"source-{page_id}.json"
            payload.write_text(json.dumps({"event_id": event["event_id"]}))
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            run = self.connection.execute(
                """INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256)
                   VALUES (?, ?, ?, ?)""",
                ("wikipedia_research", "2026-09-26T00:00:00Z", str(payload), digest),
            )
            self.connection.execute(
                """INSERT INTO wikipedia_source_receipts(
                   run_id, event_page_id, event_title, page_url, revision_id,
                   revision_timestamp_utc, license_title, license_url,
                   imported_bouts, skipped_unresolved_bouts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.lastrowid, page_id, str(event["event_id"]),
                 f"https://en.wikipedia.org/wiki/Event_{page_id}", 1,
                 "2026-09-26T00:00:00Z", "CC BY-SA 4.0",
                 "https://creativecommons.org/licenses/by-sa/4.0/",
                 int(event["bouts"]), held_by_event.get(str(event["event_id"]), 0)),
            )

    def test_same_date_results_are_withheld_and_other_sources_are_ignored(self):
        self.add_result("event-1", "2024-01-01", "bout-1", "a", "b", "a")
        self.add_result("event-1", "2024-01-01", "bout-2", "a", "c", "a")
        self.add_result("event-2", "2024-01-02", "bout-3", "a", "b", "a")
        self.add_result("event-3", "2024-01-03", "bout-4", "a", "c", "c")
        self.add_result("event-other", "2024-01-01", "bout-other", "a", "d", "d", source="other")
        self.add_result(
            "event-4", "2024-01-04", "bout-draw", "a", "b", None, outcome="draw",
        )
        self.add_result(
            "event-4", "2024-01-04", "bout-nc", "a", "c", None, outcome="no_contest",
        )
        rows, summary = research_feature_rows(self.connection)
        by_bout = {row.bout_id: row for row in rows}
        self.assertEqual(set(by_bout), {"bout-1", "bout-2", "bout-3", "bout-4"})
        self.assertEqual(by_bout["bout-1"].features[0], 0.0)
        self.assertEqual(by_bout["bout-2"].features[0], 0.0)
        self.assertGreater(by_bout["bout-3"].features[0], 0.0)
        self.assertEqual(summary["outcomes"], {"win": 4, "draw": 1, "no_contest": 1})
        self.assertTrue(all(not any(row.coverage) for row in rows))
        self.assertTrue(all(all(value == 0 for value in row.features[6:]) for row in rows))

    def test_calibration_and_test_use_disjoint_later_dates_with_identity_coverage(self):
        start = date(2024, 1, 1)
        for day in range(30):
            event_date = (start + timedelta(days=day)).isoformat()
            event_id = f"wikipedia_research:{day + 1}"
            for bout in range(8):
                a_id, b_id = f"a-{bout}", f"b-{bout}"
                winner = a_id if (day + bout) % 3 else b_id
                self.add_result(
                    event_id, event_date, f"bout-{day:02d}-{bout}",
                    a_id, b_id, winner,
                )
        self.attach_receipts({"wikipedia_research:30": 1})
        self.connection.commit()
        review = {
            "schema_version": 1,
            "source": "wikipedia_research",
            "review_required": True,
            "summary": {"held_bouts": 1},
            "fighters": [
                {"occurrences": [{"event_id": "wikipedia_research:30", "bout_position": 9}]},
                {"occurrences": [{"event_id": "wikipedia_research:30", "bout_position": 9}]},
            ],
        }
        report = evaluate_research_history(
            self.connection, identity_review=review, identity_review_sha256="checked-worksheet",
        )
        self.assertEqual(report["status"], "research_only")
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["source_summary"]["accepted_bouts_with_results"], 240)
        self.assertEqual(report["split"]["train"]["binary_bouts"], 168)
        self.assertEqual(report["split"]["validation"]["binary_bouts"], 32)
        self.assertEqual(report["split"]["test"]["binary_bouts"], 40)
        self.assertLess(report["split"]["train"]["last_date"], report["split"]["validation"]["first_date"])
        self.assertLess(report["split"]["validation"]["last_date"], report["split"]["test"]["first_date"])
        self.assertTrue(report["calibration"]["applied"])
        self.assertEqual(report["test"]["elo"]["metrics"]["bouts"], 40)
        self.assertEqual(report["test"]["logistic_calibrated"]["metrics"]["bouts"], 40)
        self.assertEqual(report["identity_coverage"]["held_source_bout_rows"], 1)
        self.assertEqual(report["identity_coverage"]["by_split_dates"]["test"]["held_source_bout_rows"], 1)
        self.assertAlmostEqual(
            report["identity_coverage"]["by_split_dates"]["test"]["accepted_share_of_accepted_plus_held"],
            40 / 41,
        )
        self.assertIsNone(report["bookmaker"]["roi"])
        self.assertEqual(report["source_summary"]["accepted_source_rows_sha256"],
                         evaluate_research_history(self.connection)["source_summary"]["accepted_source_rows_sha256"])

    def test_rejects_identity_worksheet_that_does_not_match_database(self):
        self.add_result("event-1", "2024-01-01", "bout-1", "a", "b", "a")
        review = {
            "schema_version": 1, "source": "wikipedia_research", "review_required": True,
            "summary": {"held_bouts": 1},
            "fighters": [{"occurrences": [{"event_id": "missing", "bout_position": 1}]}],
        }
        # Three dates are needed before coverage is computed.
        self.add_result("event-2", "2024-01-02", "bout-2", "a", "b", "b")
        self.add_result("event-3", "2024-01-03", "bout-3", "a", "b", "a")
        with self.assertRaisesRegex(ValueError, "absent from this database"):
            evaluate_research_history(self.connection, identity_review=review)

    def test_rejects_truncated_identity_worksheet_against_saved_receipts(self):
        for day in range(1, 4):
            self.add_result(
                f"wikipedia_research:{day}", f"2024-01-{day:02d}",
                f"bout-{day}", "a", "b", "a",
            )
        self.attach_receipts({"wikipedia_research:3": 1})
        stale = {
            "schema_version": 1, "source": "wikipedia_research",
            "review_required": True, "summary": {"held_bouts": 0}, "fighters": [],
        }
        with self.assertRaisesRegex(ValueError, "worksheet disagrees with source receipt"):
            evaluate_research_history(self.connection, identity_review=stale)

    def test_reviewed_replay_reconciles_old_held_worksheet_and_detects_tampering(self):
        event_id = "wikipedia_research:31"
        self.add_result(event_id, "2024-01-31", "bout-31", "wikipedia:74765470", "b", "b")
        self.connection.execute(
            "UPDATE fighters SET canonical_name = 'Denise Gomes' WHERE fighter_id = 'wikipedia:74765470'"
        )
        self.connection.execute(
            "UPDATE fighters SET canonical_name = 'Loma Lookboonmee' WHERE fighter_id = 'b'"
        )
        self.connection.execute(
            "UPDATE bouts SET weight_class = ?, source_bout_id = ? "
            "WHERE bout_id = 'bout-31'",
            ("Women's Strawweight", "31:b:wikipedia:74765470"),
        )
        self.connection.execute(
            "UPDATE results SET winner_fighter_id = 'wikipedia:74765470', "
            "method = 'Decision (unanimous)' WHERE bout_id = 'bout-31'"
        )
        source = self.root / "source.json"
        source.write_text(json.dumps({
            "id": 31, "title": "UFC 31",
            "latest": {"id": 1, "timestamp": "2024-01-31T00:00:00Z"},
            "license": {"title": "CC BY-SA 4.0",
                        "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            "source": "{{Infobox MMA event\n|name=UFC 31\n|date={{start date|2024|1|31}}\n}}\n"
                      "==Results==\n{{MMAevent}}\n"
                      "{{MMAevent bout|Women's Strawweight|Denise Gomes|def.|"
                      "[[Loma Lookboonmee]]|Decision (unanimous)|3|5:00|}}\n"
                      "==References==",
        }))
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()

        def source_receipt(imported: int, held: int, crosswalk_run_id: int | None = None) -> None:
            run = self.connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES ('wikipedia_research', '2026-09-26T00:00:00Z', ?, ?)",
                (str(source), source_sha),
            ).lastrowid
            self.connection.execute(
                """INSERT INTO wikipedia_source_receipts(
                   run_id, event_page_id, event_title, page_url, revision_id,
                   revision_timestamp_utc, license_title, license_url,
                   imported_bouts, skipped_unresolved_bouts, crosswalk_run_id)
                   VALUES (?, 31, 'UFC 31', 'https://en.wikipedia.org/wiki/UFC_31',
                           1, '2026-09-26T00:00:00Z', 'CC BY-SA 4.0',
                           'https://creativecommons.org/licenses/by-sa/4.0/', ?, ?, ?)""",
                (run, imported, held, crosswalk_run_id),
            )

        source_receipt(0, 1)
        review_path = self.root / "crosswalk.json"
        review_path.write_text(json.dumps({"schema_version": 2, "decisions": [{
            "event_id": event_id, "bout_position": 1, "fighter_name": "Denise Gomes",
            "source_revision_id": 1, "source_receipt_sha256": source_sha,
            "fighter_id": "wikipedia:74765470", "canonical_name": "Denise Gomes",
            "page_title": "Denise Gomes", "reviewed_by": "reviewer",
            "evidence_urls": ["https://www.ufc.com/event/ufc-31"],
        }]}))
        crosswalk_run = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES ('wikipedia_identity_crosswalk', '2026-09-26T00:00:00Z', ?, ?)",
            (str(review_path), hashlib.sha256(review_path.read_bytes()).hexdigest()),
        ).lastrowid
        source_receipt(1, 0, crosswalk_run)
        worksheet = {
            "schema_version": 1, "source": "wikipedia_research", "review_required": True,
            "summary": {"held_bouts": 1},
            "fighters": [{"occurrences": [{"event_id": event_id, "bout_position": 1}]}],
        }
        self.assertEqual(_held_bouts_by_event(self.connection, worksheet), {})
        empty_worksheet = dict(worksheet, summary={"held_bouts": 0}, fighters=[])
        with self.assertRaisesRegex(ValueError, "absent from the identity worksheet"):
            _held_bouts_by_event(self.connection, empty_worksheet)
        self.connection.execute("DELETE FROM results WHERE bout_id = 'bout-31'")
        with self.assertRaisesRegex(ValueError, "mismatched source receipt"):
            _held_bouts_by_event(self.connection, worksheet)
        db.upsert_result(self.connection, "bout-31", "win", "wikipedia:74765470",
                         "2024-01-31T23:00:00Z", "Decision (unanimous)")
        review_path.write_text('{"tampered":true}')
        with self.assertRaisesRegex(ValueError, "conflicting.*source receipt"):
            _held_bouts_by_event(self.connection, worksheet)

    def test_cli_opens_database_read_only_and_writes_separate_report(self):
        path = self.root / "research.sqlite"
        with db.connect(path) as disk:
            db.init_db(disk)
            for day in range(1, 4):
                for fighter_id in ("a", "b"):
                    db.upsert_fighter(disk, fighter_id, fighter_id)
                event_id = f"wikipedia_research:{day}"
                bout_id = f"bout-{day}"
                db.upsert_event(
                    disk, event_id, event_id, f"2024-01-{day:02d}", "completed",
                    source="wikipedia_research", source_event_id=str(day),
                )
                db.upsert_bout(
                    disk, bout_id, event_id, "a", "b", "completed",
                    source="wikipedia_research", source_bout_id=bout_id,
                )
                db.upsert_result(disk, bout_id, "win", "a", f"2024-01-{day:02d}T23:00:00Z")
            disk.commit()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        output = self.root / "report.json"
        main(["--db", str(path), "--out", str(output)])
        report = json.loads(output.read_text())
        self.assertEqual(report["source_summary"]["outcomes"]["win"], 3)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
