"""Offline verification and isolated import of one source-dated fight card."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from scripts.import_reviewed_prefight_card import (
    import_approved_card, run_import, verify_manifest, write_review_template, write_template,
)
from ufc_odds_model.alerts import record_prefight_checks
from ufc_odds_model.card_history import latest_prefight_roster
from ufc_odds_model.ingest import import_odds_payload
from ufc_odds_model.paper import record_paper_candidates
from ufc_odds_model.pipeline import score_event


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

    def _fully_link_second_bout(self) -> None:
        """Make the two-row source card fully identified for a positive gate fixture."""
        manifest = json.loads(self.manifest_path.read_text())
        source_path = Path(manifest["source"]["raw_path"])
        page = json.loads(source_path.read_text())
        page["source"] = page["source"].replace("|Gamma Plain\n", "|[[Gamma Plain]]\n")
        manifest["source"]["raw_sha256"] = _save_json(source_path, page)
        lookup_path = Path(manifest["fighter_lookup"]["raw_path"])
        lookup = json.loads(lookup_path.read_text())
        lookup["query"]["pages"].append({"title": "Gamma Plain", "pageid": 33, "ns": 0})
        manifest["fighter_lookup"]["raw_sha256"] = _save_json(lookup_path, lookup)
        titles = sorted(("Alpha Fighter", "Beta Fighter", "Delta Fighter", "Gamma Plain"))
        query = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
            "titles": "|".join(titles), "maxlag": "5", "prop": "pageprops",
            "ppprop": "disambiguation",
        })
        manifest["fighter_lookup"]["api_url"] = "https://en.wikipedia.org/w/api.php?" + query
        manifest["fighter_lookup"]["linked_titles_requested"] = 4
        manifest["fighter_lookup"]["linked_titles_resolved"] = 4
        manifest["fight_card_rows"][1]["fighter_a"].update({
            "linked_title": "Gamma Plain", "page_id": 33, "stable_id": "wikipedia:33",
        })
        manifest["fight_card_rows"][1]["status"] = "source_page_ids_resolved_manual_review_pending"
        manifest["review_summary"].update({"fully_linked_identity_rows": 2,
                                           "identity_hold_rows": 0})
        _save_json(self.manifest_path, manifest)

    def _replace_exact_odds_with_alias(self) -> None:
        odds_manifest = json.loads(self.odds_path.read_text())
        raw_path = Path(odds_manifest["raw_path"])
        raw = json.loads(raw_path.read_text())
        raw[0]["away_team"] = "Replacement Opponent"
        raw[0]["bookmakers"][0]["markets"][0]["outcomes"][1]["name"] = "Replacement Opponent"
        odds_manifest["sha256"] = _save_json(raw_path, raw)
        _save_json(self.odds_path, odds_manifest)
        manifest = json.loads(self.manifest_path.read_text())
        manifest["odds_coverage_comparison"]["rows"][0] = {
            "position": 1, "status": "alias_or_opponent_review_required",
            "candidate_odds_event_ids": [raw[0]["id"]],
        }
        _save_json(self.manifest_path, manifest)

    def _add_odds_receipt(self) -> None:
        odds_manifest = json.loads(self.odds_path.read_text())
        odds_manifest.update({"source": "the-odds-api", "region": "us"})
        receipt_path = self.root / "odds-fetch-receipt.json"
        receipt_sha = _save_json(receipt_path, {
            "schema_version": 1, "provider": "the-odds-api",
            "sport_key": "mma_mixed_martial_arts", "market": "h2h",
            "region": "us", "fetched_at_utc": odds_manifest["captured_at_utc"],
            "response_sha256": odds_manifest["sha256"],
        })
        odds_manifest.update({"receipt_path": str(receipt_path),
                              "receipt_sha256": receipt_sha})
        odds_sha = _save_json(self.odds_path, odds_manifest)
        manifest = json.loads(self.manifest_path.read_text())
        manifest["odds_coverage_comparison"]["source_manifest_sha256"] = odds_sha
        _save_json(self.manifest_path, manifest)

    def _completed_review(self, *, full: bool) -> Path:
        if full:
            self._fully_link_second_bout()
        self._add_capture_receipts()
        self._add_odds_receipt()
        path = self.root / "operating-review.json"
        write_review_template(self.manifest_path, path, self.odds_path)
        review = json.loads(path.read_text())
        review["reviewed_by"] = "Fixture Reviewer"
        review["reviewed_at_utc"] = "2026-09-26T10:05:00Z"
        review["rights"] = {
            "decision": "approved", "use_scope": "private_betting_decision_support",
            "basis_url": "https://example.test/fixture-permission",
            "basis_note": "Fixture grant for isolated paper decision tests.",
        }
        review["rows"][0]["roster_disposition"] = "approve"
        review["rows"][1]["roster_disposition"] = "approve" if full else "hold"
        review["rows"][1]["review_note"] = "" if full else "Unresolved source fighter ID"
        _save_json(path, review)
        return path

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

    def test_partial_review_keeps_event_wide_gate_closed(self) -> None:
        review_path = self._completed_review(full=False)
        output = self.root / "partial-operating"
        fixed = datetime(2026, 9, 26, 11, tzinfo=timezone.utc)
        with patch("scripts.import_reviewed_prefight_card.utc_now", return_value=fixed), patch(
            "ufc_odds_model.importers.utc_now", return_value=fixed
        ):
            report = import_approved_card(self.manifest_path, review_path, output, self.odds_path)
        self.assertEqual(report["approved_bouts"], 1)
        self.assertEqual(report["held_positions"], [2])
        self.assertEqual(report["event_provider_status"], "review_pending")
        self.assertFalse(report["roster_gate_eligible"])
        self.assertEqual(report["paper_decisions_created"], 0)
        with db.connect(output / "operating-card.sqlite") as connection:
            roster = latest_prefight_roster(
                connection, report["event_id"], f"{report['event_id']}:1", fixed,
            )
            self.assertEqual(roster["reason"], "roster_provider_status_not_prefight")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)
            with self.assertRaisesRegex(sqlite3.DatabaseError, "immutable"):
                connection.execute("UPDATE card_event_snapshots SET reviewed_by = 'changed'")

    def test_operating_review_requires_timestamped_card_and_odds_receipts(self) -> None:
        review_path = self.root / "review-template.json"
        with self.assertRaisesRegex(ValueError, "source and fighter lookup fetch receipts"):
            write_review_template(self.manifest_path, review_path, self.odds_path)
        self.assertFalse(review_path.exists())
        self._add_capture_receipts()
        with self.assertRaisesRegex(ValueError, "timestamped odds fetch receipt"):
            write_review_template(self.manifest_path, review_path, self.odds_path)
        self.assertFalse(review_path.exists())

    def test_full_review_makes_roster_eligible_but_history_blocks_paper(self) -> None:
        review_path = self._completed_review(full=True)
        fixed = datetime(2026, 9, 26, 11, tzinfo=timezone.utc)
        with patch("scripts.import_reviewed_prefight_card.utc_now", return_value=fixed), patch(
            "ufc_odds_model.importers.utc_now", return_value=fixed
        ):
            report = import_approved_card(self.manifest_path, review_path,
                                          self.root / "full-operating", self.odds_path)
        self.assertTrue(report["full_card_reconciled"])
        self.assertTrue(report["roster_gate_eligible"])
        self.assertEqual(report["event_provider_status"], "scheduled")
        database = self.root / "full-operating" / "operating-card.sqlite"
        with db.connect(database) as connection:
            roster = latest_prefight_roster(
                connection, report["event_id"], f"{report['event_id']}:1", fixed,
            )
            self.assertTrue(roster["accepted"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)
            payload = json.loads((self.root / "odds.json").read_text())
            payload[0]["bookmakers"][0]["markets"][0]["last_update"] = "2026-09-26T10:59:55Z"
            payload[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 2.3
            payload[0]["bookmakers"][0]["markets"][0]["outcomes"][1]["price"] = 1.6
            with patch("ufc_odds_model.ingest.utc_now", return_value=fixed):
                odds = import_odds_payload(connection, payload, "2026-09-26T11:00:00Z",
                                           self.root / "fresh-odds")
            with patch("ufc_odds_model.pipeline.utc_now", return_value=fixed + timedelta(seconds=2)):
                _, rows = score_event(
                    connection, report["event_id"], fixed + timedelta(seconds=1),
                    self.root / "reports", required_snapshot_at_utc=odds["snapshot_at_utc"],
                )
            self.assertEqual(rows[0]["decision"], "candidate")

            class FixedGateDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 9, 26, 11, 0, 2,
                               tzinfo=timezone.utc).astimezone(tz or timezone.utc)

            with patch("ufc_odds_model.alerts.datetime", FixedGateDatetime):
                checked = record_prefight_checks(
                    connection, rows, ingestion_run_id=int(odds["ingestion_run_id"]),
                )
            self.assertEqual(checked[0]["alert_reason"], "model_not_validated")
            self.assertFalse(checked[0]["alert_eligible"])
            self.assertEqual(record_paper_candidates(connection, checked, 1000), [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)

    def test_review_hash_rights_and_full_card_decisions_fail_before_new_db(self) -> None:
        review_path = self._completed_review(full=False)
        original = json.loads(review_path.read_text())
        fixed = datetime(2026, 9, 26, 11, tzinfo=timezone.utc)
        changes = [
            ({"source_manifest_sha256": "0" * 64}, "source hash"),
            ({"source_revision_id": "wrong"}, "revision"),
            ({"event_start_utc": "2026-09-27T21:00:00Z"}, "start"),
            ({"rows": original["rows"][:1]}, "every captured card row"),
            ({"rights": {**original["rights"], "decision": "pending"}}, "Source rights"),
        ]
        for index, (changed, message) in enumerate(changes):
            review = {**original, **changed}
            _save_json(review_path, review)
            output = self.root / f"invalid-{index}"
            with patch("scripts.import_reviewed_prefight_card.utc_now", return_value=fixed):
                with self.assertRaisesRegex(ValueError, message):
                    import_approved_card(self.manifest_path, review_path, output, self.odds_path)
            self.assertFalse(output.exists())
        _save_json(review_path, original)
        manifest = json.loads(self.manifest_path.read_text())
        Path(manifest["source"]["raw_path"]).write_bytes(b"changed capture evidence")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            import_approved_card(self.manifest_path, review_path,
                                 self.root / "changed-source", self.odds_path)
        self.assertFalse((self.root / "changed-source").exists())

    def test_alias_odds_hold_blocks_event_wide_schedule(self) -> None:
        self._fully_link_second_bout()
        self._replace_exact_odds_with_alias()
        self._add_capture_receipts()
        self._add_odds_receipt()
        review_path = self.root / "operating-review.json"
        write_review_template(self.manifest_path, review_path, self.odds_path)
        review = json.loads(review_path.read_text())
        review["reviewed_by"] = "Fixture Reviewer"
        review["reviewed_at_utc"] = "2026-09-26T10:05:00Z"
        review["rights"] = {
            "decision": "approved", "use_scope": "private_betting_decision_support",
            "basis_url": "https://example.test/fixture-permission",
            "basis_note": "Fixture grant for isolated paper decision tests.",
        }
        for row in review["rows"]:
            row["roster_disposition"] = "approve"
        _save_json(review_path, review)
        fixed = datetime(2026, 9, 26, 11, tzinfo=timezone.utc)
        with patch("scripts.import_reviewed_prefight_card.utc_now", return_value=fixed), patch(
            "ufc_odds_model.importers.utc_now", return_value=fixed
        ):
            report = import_approved_card(self.manifest_path, review_path,
                                          self.root / "alias-held", self.odds_path)
        self.assertEqual(report["odds_alias_or_opponent_hold_positions"], [1])
        self.assertEqual(report["event_provider_status"], "review_pending")
        self.assertFalse(report["roster_gate_eligible"])

    def test_operating_review_rejects_relabelled_negative_odds_coverage(self) -> None:
        self._fully_link_second_bout()
        self._replace_exact_odds_with_alias()
        self._add_capture_receipts()
        self._add_odds_receipt()
        manifest = json.loads(self.manifest_path.read_text())
        actual_rows = manifest["odds_coverage_comparison"]["rows"]
        alias_row = dict(actual_rows[0])
        actual_rows[0] = {"position": 1, "status": "no_odds_event_in_saved_snapshot"}
        _save_json(self.manifest_path, manifest)
        review_path = self.root / "relabelled-review.json"
        with self.assertRaisesRegex(ValueError, "differs from saved response"):
            write_review_template(self.manifest_path, review_path, self.odds_path)
        self.assertFalse(review_path.exists())

        actual_rows[0] = alias_row
        actual_rows[1] = {"position": 2, "status": "alias_or_opponent_review_required",
                          "candidate_odds_event_ids": [alias_row["candidate_odds_event_ids"][0]]}
        _save_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "differs from saved response"):
            write_review_template(self.manifest_path, review_path, self.odds_path)
        self.assertFalse(review_path.exists())

    def test_no_usable_price_is_a_quote_gap_not_a_roster_hold(self) -> None:
        self._fully_link_second_bout()
        self._add_capture_receipts()
        self._add_odds_receipt()
        manifest = json.loads(self.manifest_path.read_text())
        manifest["odds_coverage_comparison"]["rows"][0]["status"] = "no_usable_two_sided_price"
        _save_json(self.manifest_path, manifest)
        review_path = self.root / "operating-review.json"
        with self.assertRaisesRegex(ValueError, "hides a usable two-sided bookmaker"):
            write_review_template(self.manifest_path, review_path, self.odds_path)

        odds_manifest = json.loads(self.odds_path.read_text())
        raw_path = Path(odds_manifest["raw_path"])
        raw = json.loads(raw_path.read_text())
        raw[0]["bookmakers"] = []
        odds_manifest["sha256"] = _save_json(raw_path, raw)
        receipt_path = Path(odds_manifest["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        receipt["response_sha256"] = odds_manifest["sha256"]
        odds_manifest["receipt_sha256"] = _save_json(receipt_path, receipt)
        manifest["odds_coverage_comparison"]["source_manifest_sha256"] = _save_json(
            self.odds_path, odds_manifest
        )
        _save_json(self.manifest_path, manifest)
        write_review_template(self.manifest_path, review_path, self.odds_path)
        review = json.loads(review_path.read_text())
        review["reviewed_by"] = "Fixture Reviewer"
        review["reviewed_at_utc"] = "2026-09-26T10:05:00Z"
        review["rights"] = {
            "decision": "approved", "use_scope": "private_betting_decision_support",
            "basis_url": "https://example.test/fixture-permission",
            "basis_note": "Fixture grant for isolated paper decision tests.",
        }
        for row in review["rows"]:
            row["roster_disposition"] = "approve"
        _save_json(review_path, review)
        fixed = datetime(2026, 9, 26, 11, tzinfo=timezone.utc)
        with patch("scripts.import_reviewed_prefight_card.utc_now", return_value=fixed), patch(
            "ufc_odds_model.importers.utc_now", return_value=fixed
        ):
            report = import_approved_card(self.manifest_path, review_path,
                                          self.root / "no-price", self.odds_path)
        self.assertEqual(report["event_provider_status"], "scheduled")
        self.assertEqual(report["odds_alias_or_opponent_hold_positions"], [])
        with db.connect(self.root / "no-price" / "operating-card.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM odds_quotes").fetchone()[0], 0)

    def test_operating_import_rejects_stale_capture_and_unreviewed_substitution(self) -> None:
        review_path = self._completed_review(full=False)
        review = json.loads(review_path.read_text())
        review["rows"][1]["roster_disposition"] = "substituted"
        review["rows"][1]["review_note"] = "Opponent replacement requires a new card capture"
        _save_json(review_path, review)
        fixed = datetime(2026, 9, 26, 11, tzinfo=timezone.utc)
        with patch("scripts.import_reviewed_prefight_card.utc_now", return_value=fixed), patch(
            "ufc_odds_model.importers.utc_now", return_value=fixed
        ):
            report = import_approved_card(self.manifest_path, review_path,
                                          self.root / "substitution-held", self.odds_path)
        self.assertEqual(report["held_positions"], [2])
        self.assertEqual(report["event_provider_status"], "review_pending")
        output = self.root / "stale-card"
        with patch("scripts.import_reviewed_prefight_card.utc_now",
                   return_value=datetime(2026, 9, 27, 11, tzinfo=timezone.utc)):
            with self.assertRaisesRegex(ValueError, "capture is stale"):
                import_approved_card(self.manifest_path, review_path, output, self.odds_path)
        self.assertFalse(output.exists())

    def test_captured_event_spec_is_bound_and_tamper_evident(self) -> None:
        manifest = json.loads(self.manifest_path.read_text())
        spec_path = self.root / "event-spec.json"
        spec = {
            "schema_version": 1,
            "event": {"source_page_id": 12345, "source_page_title": "UFC Pilot",
                      "event_date": "2026-09-27"},
            "source": {"api_url": manifest["source"]["api_url"]},
            "official_start_review": manifest["official_start_review"],
            "reviewed_by": "Fixture Reviewer", "reviewed_at_utc": "2026-09-26T09:00:00Z",
        }
        manifest["event_spec_path"] = "event-spec.json"
        manifest["event_spec_sha256"] = _save_json(spec_path, spec)
        _save_json(self.manifest_path, manifest)
        verified = verify_manifest(self.manifest_path)
        self.assertEqual(verified["event_spec_bytes"], spec_path.read_bytes())
        spec["event"]["source_page_id"] = 999
        _save_json(spec_path, spec)
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            verify_manifest(self.manifest_path)
        manifest["event_spec_sha256"] = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        _save_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "differs from captured event"):
            verify_manifest(self.manifest_path)


if __name__ == "__main__":
    unittest.main()
