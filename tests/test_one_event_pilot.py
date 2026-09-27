"""Offline verification and isolated import of one source-dated fight card."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.import_reviewed_prefight_card import run_import, verify_manifest, write_template
from ufc_odds_model.card_history import latest_prefight_roster


def _save_json(path: Path, payload: object) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


class OneEventPilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path, self.odds_path = self._fixture()

    def _fixture(self) -> tuple[Path, Path]:
        source = """{{Infobox MMA event
|name= SYNTHETIC UFC PILOT
|date= {{start date|2026|9|27}}
}}
==Fight card==
{{MMAevent card|Main card}}
{{MMAevent bout
|Lightweight
|[[Alpha Fighter]]
|vs.
|[[Beta Fighter]]
|
|
|
|
}}
{{MMAevent bout
|Welterweight
|Gamma Plain
|vs.
|[[Delta Fighter]]
|
|
|
|
}}
==Announced bouts==
* Other Fighter vs. Another Fighter
"""
        page = {
            "id": 12345, "title": "UFC Pilot",
            "latest": {"id": 67890, "timestamp": "2026-09-25T00:00:00Z"},
            "license": {"title": "Creative Commons Attribution-Share Alike 4.0",
                        "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            "source": source,
        }
        page_path = self.root / "page.json"
        page_sha = _save_json(page_path, page)
        titles = ["Alpha Fighter", "Beta Fighter", "Delta Fighter"]
        query = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
            "titles": "|".join(titles), "maxlag": "5", "prop": "pageprops",
            "ppprop": "disambiguation",
        })
        lookup_url = "https://en.wikipedia.org/w/api.php?" + query
        lookup = {"query": {"pages": [
            {"title": title, "pageid": page_id, "ns": 0}
            for title, page_id in zip(titles, (11, 22, 44))
        ]}}
        lookup_path = self.root / "lookup.json"
        lookup_sha = _save_json(lookup_path, lookup)

        odds_payload = [{
            "id": "a" * 32, "sport_key": "mma_mixed_martial_arts",
            "commence_time": "2026-09-27T20:00:00Z",
            "home_team": "Alpha Fighter", "away_team": "Beta Fighter",
            "bookmakers": [{"key": "fixture-book", "title": "Fixture Book",
                            "last_update": "2026-09-26T10:00:00Z",
                            "markets": [{"key": "h2h", "last_update": "2026-09-26T10:00:00Z",
                                         "outcomes": [{"name": "Alpha Fighter", "price": 1.9},
                                                      {"name": "Beta Fighter", "price": 2.0}]}]}],
        }]
        odds_raw_path = self.root / "odds.json"
        odds_raw_sha = _save_json(odds_raw_path, odds_payload)
        odds_manifest = {
            "captured_at_utc": "2026-09-26T10:01:00Z",
            "raw_path": str(odds_raw_path), "sha256": odds_raw_sha,
        }
        odds_path = self.root / "odds-manifest.json"
        odds_manifest_sha = _save_json(odds_path, odds_manifest)

        def fighter(name: str, title: str | None, page_id: int | None) -> dict:
            return {"name": name, "linked_title": title, "page_id": page_id,
                    "stable_id": f"wikipedia:{page_id}" if page_id else None}

        manifest = {
            "captured_at_utc": "2026-09-26T10:00:00Z",
            "event": {"name": "SYNTHETIC UFC PILOT", "event_date": "2026-09-27",
                      "source_page_title": "UFC Pilot", "source_page_id": 12345,
                      "source_revision_id": 67890,
                      "source_revision_timestamp_utc": "2026-09-25T00:00:00Z",
                      "source_revision_url": "https://en.wikipedia.org/w/index.php?title=UFC_Pilot&oldid=67890"},
            "source": {"api_url": "https://en.wikipedia.org/w/rest.php/v1/page/UFC_Pilot",
                       "raw_path": str(page_path), "raw_sha256": page_sha,
                       "license_title": page["license"]["title"],
                       "license_url": page["license"]["url"]},
            "fighter_lookup": {"api_url": lookup_url, "raw_path": str(lookup_path),
                               "raw_sha256": lookup_sha, "linked_titles_requested": 3,
                               "linked_titles_resolved": 3},
            "official_start_review": {
                "source_url": "https://www.ufc.com/event/synthetic-pilot",
                "local_timezone": "America/New_York",
                "event_start_utc_for_conservative_cutoff": "2026-09-27T20:00:00Z",
                "main_card_start_utc": "2026-09-27T20:00:00Z",
                "published_segment_starts": [{"segment": "main_card",
                                              "local_time": "2026-09-27T16:00:00",
                                              "utc_time": "2026-09-27T20:00:00Z"}],
            },
            "fight_card_rows": [
                {"position": 1, "weight_class": "Lightweight",
                 "fighter_a": fighter("Alpha Fighter", "Alpha Fighter", 11),
                 "fighter_b": fighter("Beta Fighter", "Beta Fighter", 22),
                 "status": "source_page_ids_resolved_manual_review_pending"},
                {"position": 2, "weight_class": "Welterweight",
                 "fighter_a": fighter("Gamma Plain", None, None),
                 "fighter_b": fighter("Delta Fighter", "Delta Fighter", 44),
                 "status": "hold_identity_review"},
            ],
            "review_summary": {"fight_card_rows": 2, "fully_linked_identity_rows": 1,
                               "identity_hold_rows": 1},
            "odds_coverage_comparison": {
                "source_manifest_sha256": odds_manifest_sha,
                "odds_capture_at_utc": "2026-09-26T10:01:00Z",
                "summary": {"wikipedia_card_rows": 2, "odds_events_near_date": 1,
                            "exact_pair_names": 1, "accent_only_pair_names": 0,
                            "manual_alias_or_name_review": 0, "no_odds_event_in_snapshot": 1},
                "rows": [
                    {"position": 1, "odds_event_id": "a" * 32, "status": "exact"},
                    {"position": 2, "status": "no_odds_event_in_saved_snapshot"},
                ],
            },
        }
        manifest_path = self.root / "manifest.json"
        _save_json(manifest_path, manifest)
        return manifest_path, odds_path

    def _reviewed_csv(self) -> Path:
        path = self.root / "reviewed.csv"
        summary = write_template(self.manifest_path, path)
        self.assertEqual((summary["eligible_bouts"], summary["held_bouts"]), (1, 1))
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames
            rows = list(reader)
        rows[0]["reviewed_by"] = "Fixture Reviewer"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _add_capture_receipts(self, *, lookup_at: str = "2026-09-26T10:00:30Z") -> None:
        manifest = json.loads(self.manifest_path.read_text())
        for key, fetched_at in (("source", manifest["captured_at_utc"]),
                                ("fighter_lookup", lookup_at)):
            info = manifest[key]
            receipt_path = self.root / f"{key}-fetch-receipt.json"
            receipt_sha = _save_json(receipt_path, {
                "schema_version": 1, "request_url": info["api_url"],
                "response_sha256": info["raw_sha256"],
                "fetched_at_utc": fetched_at,
            })
            info.update({"receipt_path": str(receipt_path),
                         "receipt_sha256": receipt_sha,
                         "fetched_at_utc": fetched_at})
        _save_json(self.manifest_path, manifest)

    def test_timestamped_capture_receipts_survive_provisional_import(self) -> None:
        self._add_capture_receipts()
        verified = verify_manifest(self.manifest_path)
        self.assertEqual(verified["lookup_fetched_at_utc"], "2026-09-26T10:00:30Z")
        csv_path = self._reviewed_csv()
        output = self.root / "receipt-pilot"
        report = run_import(self.manifest_path, csv_path, output, self.odds_path)
        self.assertEqual(report["fighter_lookup_fetched_at_utc"],
                         "2026-09-26T10:00:30Z")
        self.assertEqual(report["receipt_count"], 9)
        with sqlite3.connect(output / "paper-intake.sqlite") as connection:
            rows = connection.execute(
                "SELECT source, snapshot_at_utc FROM ingestion_runs "
                "WHERE source LIKE '%fetch-receipt' ORDER BY source"
            ).fetchall()
        self.assertEqual(rows, [
            ("wikipedia-prefight-lookup-fetch-receipt", "2026-09-26T10:00:30Z"),
            ("wikipedia-prefight-rest-fetch-receipt", "2026-09-26T10:00:00Z"),
        ])

    def test_odds_must_not_precede_timestamped_fighter_lookup(self) -> None:
        self._add_capture_receipts(lookup_at="2026-09-26T10:02:00Z")
        csv_path = self._reviewed_csv()
        output = self.root / "invalid-chronology"
        with self.assertRaisesRegex(ValueError, "predates the verified fighter lookup"):
            run_import(self.manifest_path, csv_path, output, self.odds_path)
        self.assertFalse(output.exists())

    def test_odds_must_not_precede_card_capture(self) -> None:
        manifest = json.loads(self.manifest_path.read_text())
        manifest["captured_at_utc"] = "2026-09-26T10:02:00Z"
        _save_json(self.manifest_path, manifest)
        csv_path = self._reviewed_csv()
        output = self.root / "card-after-odds"
        with self.assertRaisesRegex(ValueError, "predates the saved card capture"):
            run_import(self.manifest_path, csv_path, output, self.odds_path)
        self.assertFalse(output.exists())

    def test_changed_capture_receipt_or_partial_metadata_is_rejected(self) -> None:
        self._add_capture_receipts()
        manifest = json.loads(self.manifest_path.read_text())
        receipt_path = Path(manifest["fighter_lookup"]["receipt_path"])
        receipt_path.write_text(receipt_path.read_text().replace(
            "2026-09-26T10:00:30Z", "2026-09-26T10:02:00Z"))
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            verify_manifest(self.manifest_path)
        manifest["fighter_lookup"].pop("receipt_sha256")
        _save_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "receipt is incomplete"):
            verify_manifest(self.manifest_path)

    def test_replays_source_csv_and_saved_odds_into_new_database(self) -> None:
        csv_path = self._reviewed_csv()
        output = self.root / "new-pilot"
        with patch("ufc_odds_model.odds_api.fetch_mma_h2h",
                   side_effect=AssertionError("network fetch attempted")):
            report = run_import(self.manifest_path, csv_path, output, self.odds_path)
        self.assertFalse(report["decision_ready"])
        self.assertEqual(report["full_card_bouts"], 2)
        self.assertEqual(report["reviewed_selected_bouts"], 1)
        self.assertEqual(report["source_held_positions"], [2])
        self.assertEqual(report["odds_replay"]["matched_quotes"], 2)
        self.assertEqual(report["receipt_count"], 7)
        self.assertEqual(report["verified_receipts"], 7)
        connection = sqlite3.connect(output / "paper-intake.sqlite")
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bouts").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM odds_quotes").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)
            event = connection.execute("SELECT start_time_utc, provider_status FROM events").fetchone()
            self.assertEqual((event["start_time_utc"], event["provider_status"]),
                             ("2026-09-27T20:00:00Z", "review_pending"))
            snapshot = connection.execute("SELECT source_observed_at_utc, source_revision_id "
                                          "FROM card_event_snapshots").fetchone()
            self.assertEqual((snapshot["source_observed_at_utc"], snapshot["source_revision_id"]),
                             ("2026-09-26T10:00:00Z", "67890"))
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "already exists"):
            run_import(self.manifest_path, csv_path, output, self.odds_path)

    def test_unreviewed_template_import_is_provisional_and_gate_ineligible(self) -> None:
        template_path = self.root / "unreviewed.csv"
        write_template(self.manifest_path, template_path)
        output = self.root / "provisional"
        imported_at = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        with patch("ufc_odds_model.importers.utc_now", return_value=imported_at):
            report = run_import(self.manifest_path, template_path, output,
                                provisional=True)
        self.assertEqual(report["intake_mode"], "provisional_unreviewed")
        self.assertFalse(report["decision_ready"])
        self.assertFalse(report["roster_gate_eligible"])
        self.assertEqual(report["selected_bouts"], 1)
        self.assertEqual(report["reviewed_selected_bouts"], 0)
        self.assertEqual(report["unreviewed_selected_bouts"], 1)
        self.assertEqual(report["receipt_count"], 4)
        self.assertEqual(report["alerts_created"], 0)
        self.assertEqual(report["paper_decisions_created"], 0)
        connection = sqlite3.connect(output / "paper-intake.sqlite")
        connection.row_factory = sqlite3.Row
        try:
            roster = latest_prefight_roster(
                connection, "wikipedia_pilot:12345", "wikipedia_pilot:12345:1",
                datetime(2026, 9, 26, 12, 1, tzinfo=timezone.utc),
            )
            self.assertFalse(roster["accepted"])
            self.assertEqual(roster["reason"], "roster_provider_status_not_prefight")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM prefight_gate_checks"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)
        finally:
            connection.close()

    def test_source_tamper_or_revision_change_fails_before_database_creation(self) -> None:
        csv_path = self._reviewed_csv()
        manifest = json.loads(self.manifest_path.read_text())
        source_path = Path(manifest["source"]["raw_path"])
        source_path.write_bytes(source_path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            run_import(self.manifest_path, csv_path, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())
        self._fixture()
        manifest = json.loads(self.manifest_path.read_text())
        manifest["event"]["source_revision_id"] = 67891
        _save_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "page or revision differs"):
            verify_manifest(self.manifest_path)

    def test_held_row_bad_id_and_missing_reviewer_are_rejected(self) -> None:
        path = self.root / "candidate.csv"
        write_template(self.manifest_path, path)
        with self.assertRaisesRegex(ValueError, "reviewed_by is required"):
            run_import(self.manifest_path, path, self.root / "unreviewed")
        self.assertFalse((self.root / "unreviewed").exists())
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        rows[0]["reviewed_by"] = "Fixture Reviewer"
        rows[0]["source_position"] = "2"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "held or absent"):
            run_import(self.manifest_path, path, self.root / "held")
        self.assertFalse((self.root / "held").exists())
        rows[0]["source_position"] = "1"
        rows[0]["fighter_a_id"] = "wikipedia:999"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "fighter_a_id differs"):
            run_import(self.manifest_path, path, self.root / "bad-id")

    def test_official_start_and_odds_hash_fail_closed(self) -> None:
        csv_path = self._reviewed_csv()
        manifest = json.loads(self.manifest_path.read_text())
        manifest["official_start_review"]["event_start_utc_for_conservative_cutoff"] = (
            "2026-09-27T21:00:00Z"
        )
        _save_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "Conservative cutoff"):
            run_import(self.manifest_path, csv_path, self.root / "bad-start")
        self._fixture()
        odds_manifest = json.loads(self.odds_path.read_text())
        Path(odds_manifest["raw_path"]).write_bytes(b"tampered")
        csv_path.unlink()
        csv_path = self._reviewed_csv()
        with self.assertRaisesRegex(ValueError, "Odds API response SHA-256 mismatch"):
            run_import(self.manifest_path, csv_path, self.root / "bad-odds", self.odds_path)
        self.assertFalse((self.root / "bad-odds").exists())


if __name__ == "__main__":
    unittest.main()
