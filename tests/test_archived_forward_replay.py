"""The forward research replay requires every prior result at the odds cutoff."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.archived_forward_replay import (
    _market_observations, _require_unique_saved_odds_events, replay_forward,
)


CAPTURE = "2026-09-26T22:47:55Z"


class ArchivedForwardReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "research.sqlite"
        connection = db.connect(self.database)
        db.init_db(connection)
        connection.close()
        self.paths = [self.root / name for name in
                      ("history.json", "card.json", "card.csv", "odds.json")]
        self.events = [
            {"slug": "april4", "page_id": 4, "event_date": "2026-04-04",
             "earliest_start_at_utc": "2026-04-04T21:00:00Z",
             "prefight_cutoff_at_utc": "2026-04-03T21:00:00Z",
             "result_cutoff_at_utc": "2026-04-10T21:00:00Z",
             "official_start_url": "https://www.ufc.com/news/event-4"},
            {"slug": "april11", "page_id": 11, "event_date": "2026-04-11",
             "earliest_start_at_utc": "2026-04-11T21:00:00Z",
             "prefight_cutoff_at_utc": "2026-04-10T21:00:00Z",
             "result_cutoff_at_utc": "2026-09-26T23:00:00Z",
             "official_start_url": "https://www.ufc.com/news/event-11"},
        ]
        self.paths[0].write_text(json.dumps({"schema_version": 1, "events": self.events}))
        for path in self.paths[1:]:
            path.write_bytes(b"fixture input")
        self.raw_root = self.root / "raw"
        self.card_rows = [
            {"position": 1,
             "fighter_a": {"name": "Alpha Fighter", "stable_id": "wikipedia:101"},
             "fighter_b": {"name": "Beta Fighter", "stable_id": "wikipedia:102"}},
            {"position": 2,
             "fighter_a": {"name": "Gamma Fighter", "stable_id": None},
             "fighter_b": {"name": "Delta Fighter", "stable_id": "wikipedia:104"}},
        ]
        self.verified = {
            "manifest": {
                "captured_at_utc": "2026-09-26T22:47:45Z",
                "event": {"name": "Synthetic UFC 332", "event_date": "2026-10-03",
                          "source_page_id": 332,
                          "source_revision_url": "https://en.wikipedia.org/w/index.php?oldid=10"},
                "source": {"license_url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
                "fight_card_rows": self.card_rows,
            },
            "manifest_bytes": self.paths[1].read_bytes(),
            "raw_bytes": b"source bytes", "lookup_bytes": b"lookup bytes",
            "eligible": {1: self.card_rows[0]}, "card_count": 2,
            "event_start_utc": "2026-10-03T20:00:00Z",
        }
        odds_event = {
            "id": "odds-1", "sport_key": "mma_mixed_martial_arts",
            "commence_time": "2026-10-04T00:00:00Z",
            "home_team": "Alpha Fighter", "away_team": "Beta Fighter",
            "bookmakers": [{
                "key": "fixture-book", "last_update": "2026-09-26T22:47:30Z",
                "markets": [{"key": "h2h", "last_update": "2026-09-26T22:47:30Z",
                             "outcomes": [{"name": "Alpha Fighter", "price": 1.8},
                                          {"name": "Beta Fighter", "price": 2.1}]}],
            }],
        }
        self.odds = {
            "manifest_bytes": self.paths[3].read_bytes(), "raw_bytes": b"raw odds bytes",
            "captured_at_utc": CAPTURE, "payload": [odds_event],
            "events": {"odds-1": odds_event},
            "comparison": {1: {"status": "exact", "odds_event_id": "odds-1"},
                           2: {"status": "no_odds_event_in_saved_snapshot"}},
        }
        self.calls: list[tuple[str, str]] = []

    def _review(self, _connection, item, role, cutoff, raw_root):
        self.calls.append((item["slug"], cutoff))
        self.assertEqual(role, "result")
        self.assertEqual(raw_root, self.raw_root)
        winner = "wikipedia:101"
        other = "wikipedia:102" if item["page_id"] == 4 else "wikipedia:103"
        return {
            "slug": item["slug"], "role": "result", "kind": "completed",
            "event_id": f"wikipedia_research:{item['page_id']}",
            "event_date": item["event_date"], "cutoff_at_utc": cutoff,
            "decision_cutoff_at_utc": cutoff, "source_bouts": 1,
            "revision_id": item["page_id"] + 1000,
            "revision_timestamp_utc": item["event_date"] + "T23:00:00Z",
            "selection_sidecar_sha256": "1" * 64, "selection_sha256": "2" * 64,
            "content_sidecar_sha256": "3" * 64, "content_sha256": "4" * 64,
            "wikitext_sha256": "5" * 64,
            "selection_fetched_at_utc": "2026-09-26T23:20:00Z",
            "content_fetched_at_utc": "2026-09-26T23:20:01Z",
            "matches": [{
                "bout_id": f"bout-{item['page_id']}",
                "fighter_a_id": "wikipedia:101", "fighter_b_id": other,
                "identity_verified": True, "identity_lookup_run_ids": [42],
                "outcome": "win", "winner_fighter_id": winner,
            }],
        }

    def _run(self, reviewer=None, *, odds=None, lookup_time="2026-09-26T22:30:00Z"):
        lookup = [{"run_id": 42, "source": "wikipedia_action_api",
                   "fetched_at_utc": lookup_time,
                   "payload_sha256": "a" * 64,
                   "payload_path": str(self.root / "lookup.json"),
                   "request_url": "https://en.wikipedia.org/w/api.php"}]
        with patch("ufc_odds_model.archived_forward_replay.verify_manifest",
                   return_value=self.verified), patch(
                       "ufc_odds_model.archived_forward_replay._selected_rows",
                       return_value=[{"source_position": "1", "reviewed_by": ""}]), patch(
                       "ufc_odds_model.archived_forward_replay._odds_input",
                       return_value=odds or self.odds), patch(
                       "ufc_odds_model.archived_forward_replay.review_archived_card",
                       side_effect=reviewer or self._review), patch(
                       "ufc_odds_model.archived_forward_replay._lookup_receipts",
                       return_value=lookup):
            before = hashlib.sha256(self.database.read_bytes()).hexdigest()
            report = replay_forward(*self.paths, self.database, self.raw_root,
                                    expected_capture_at_utc=CAPTURE)
            after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(report["checked_inputs"]["research_database_sha256"], before)
        return report

    def test_exact_capture_result_proofs_and_no_operating_writes(self):
        report = self._run()
        self.assertEqual(self.calls, [("april4", CAPTURE), ("april11", CAPTURE)])
        self.assertEqual(report["status"], "research_only")
        self.assertTrue(report["research_only"])
        self.assertFalse(report["promotion_eligible"])
        self.assertFalse(report["alert_eligible"])
        self.assertEqual(report["paper_decisions_created"], 0)
        self.assertEqual(report["coverage"]["historical_events_verified"], 2)
        self.assertEqual(report["coverage"]["selected_bouts_with_exploratory_elo"], 1)
        self.assertEqual(report["models"]["logistic"]["status"], "unavailable")
        self.assertGreater(report["bouts"][0]["elo_fighter_a_probability"], 0.5)
        self.assertEqual(report["bouts"][1]["forecast_status"], "identity_hold")
        self.assertEqual(report["bouts"][0]["odds_pairing_status"], "observed_two_sided")
        self.assertEqual(len(report["bouts"][0]["saved_two_sided_books"]), 1)
        self.assertFalse(report["bouts"][0]["saved_two_sided_books"][0]["executable_price_verified"])

    def test_one_missing_prior_result_holds_entire_forecast(self):
        def missing(connection, item, role, cutoff, root):
            if item["slug"] == "april11":
                raise FileNotFoundError("exact cutoff proof absent")
            return self._review(connection, item, role, cutoff, root)

        report = self._run(reviewer=missing)
        self.assertEqual(report["status"], "held_incomplete_exact_cutoff_history")
        self.assertEqual(report["coverage"]["historical_events_verified"], 1)
        self.assertEqual(report["coverage"]["selected_bouts_with_exploratory_elo"], 0)
        self.assertIsNone(report["bouts"][0]["elo_fighter_a_probability"])
        self.assertEqual(report["holds"][0]["slug"], "april11")

    def test_post_cutoff_result_revision_cannot_enter_forecast(self):
        def future(connection, item, role, cutoff, root):
            report = self._review(connection, item, role, cutoff, root)
            if item["slug"] == "april11":
                report["revision_timestamp_utc"] = "2026-09-26T23:00:00Z"
            return report

        report = self._run(reviewer=future)
        self.assertEqual(report["status"], "held_incomplete_exact_cutoff_history")
        self.assertIsNone(report["bouts"][0]["elo_fighter_a_probability"])

    def test_odds_capture_must_equal_exact_result_cutoff(self):
        odds = dict(self.odds, captured_at_utc="2026-09-26T23:00:00Z")
        with self.assertRaisesRegex(ValueError, "capture differs"):
            self._run(odds=odds)

    def test_later_fetched_identity_lookup_holds_forecast(self):
        report = self._run(lookup_time="2026-09-26T23:00:00Z")
        self.assertEqual(report["status"], "held_incomplete_exact_cutoff_history")
        self.assertIsNone(report["bouts"][0]["elo_fighter_a_probability"])

    def test_future_bookmaker_market_is_not_reported_as_a_saved_pair(self):
        odds = dict(self.odds)
        event = dict(self.odds["events"]["odds-1"])
        book = dict(event["bookmakers"][0])
        book["markets"] = [dict(book["markets"][0],
                                last_update="2026-09-26T23:00:00Z")]
        event["bookmakers"] = [book]
        odds["events"] = {"odds-1": event}
        report = self._run(odds=odds)
        self.assertEqual(report["bouts"][0]["odds_pairing_status"],
                         "no_valid_two_sided_book")
        self.assertEqual(report["bouts"][0]["saved_two_sided_books"], [])

    def test_started_odds_event_is_not_reported_as_a_prefight_pair(self):
        odds = dict(self.odds)
        event = dict(self.odds["events"]["odds-1"],
                     commence_time="2026-09-26T22:47:54Z")
        odds["events"] = {"odds-1": event}
        books, status = _market_observations(odds, self.card_rows[0], "2026-09-26")
        self.assertEqual(status, "odds_event_started_at_capture")
        self.assertEqual(books, [])

    def test_one_saved_market_cannot_price_two_selected_bouts(self):
        comparison = {1: {"status": "exact", "odds_event_id": "odds-1"},
                      2: {"status": "accent_normalization", "odds_event_id": "odds-1"}}
        with self.assertRaisesRegex(ValueError, "multiple selected card bouts"):
            _require_unique_saved_odds_events(comparison, {1, 2})


if __name__ == "__main__":
    unittest.main()
