"""Historical archive comparisons never silently resolve fighter identity."""

from __future__ import annotations

import sqlite3
import hashlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import urlencode

from ufc_odds_model import db
from ufc_odds_model.historical_archive import compare_archived_card, parse_archived_card
from ufc_odds_model.wikipedia_history import ACTION_API


PAGE_ID = 83648230
HEADER = """{{Infobox MMA event
| name = UFC 331: Example
| date = {{start date|2026|9|19}}
}}
"""
SCHEDULE = HEADER + """
==Fight card==
{{MMAevent bout|Flyweight|[[Joshua Van]]|vs.|[[Alex Perez (fighter)|Alex Perez]]|||||}}
{{MMAevent bout|Lightweight|Unlinked A|vs.|[[Other Fighter]]|||||}}
==See also==
"""
RESULTS = HEADER + """
==Results==
{{MMAevent bout|Flyweight|[[Joshua Van]]|def.|[[Alex Perez (fighter)|Alex Perez]]|Decision}}
{{MMAevent bout|Lightweight|[[Other Fighter]]|def.|Unlinked A|KO}}
==See also==
"""


def proof(wikitext: str) -> SimpleNamespace:
    return SimpleNamespace(
        page_id=PAGE_ID, page_title="UFC 331", revision_id=123,
        revision_timestamp_utc="2026-09-18T15:00:00Z",
        cutoff_at_utc="2026-09-18T20:00:00Z",
        license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/deed.en",
        wikitext=wikitext,
    )


class HistoricalArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.connection)
        event_id = f"wikipedia_research:{PAGE_ID}"
        db.upsert_event(self.connection, event_id, "UFC 331: Example", "2026-09-19",
                        "completed", source="wikipedia", source_event_id=str(PAGE_ID))
        for fighter_id, name in (("wikipedia:1", "Joshua Van"),
                                 ("wikipedia:2", "Alex Perez"),
                                 ("wikipedia:3", "Unlinked A"),
                                 ("wikipedia:4", "Other Fighter")):
            db.upsert_fighter(self.connection, fighter_id, name)
        db.upsert_bout(self.connection, "bout-one", event_id, "wikipedia:1", "wikipedia:2", "completed")
        db.upsert_result(self.connection, "bout-one", "win", "wikipedia:1", "2026-09-19T22:00:00Z")
        db.upsert_bout(self.connection, "bout-two", event_id, "wikipedia:3", "wikipedia:4", "completed")
        db.upsert_result(self.connection, "bout-two", "win", "wikipedia:4", "2026-09-19T22:00:00Z")

    def tearDown(self) -> None:
        self.connection.close()

    def add_lookup(self, *, swapped: bool = False) -> None:
        titles = "Joshua Van|Alex Perez (fighter)"
        url = ACTION_API + "?" + urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "redirects": "1", "titles": titles, "maxlag": "5",
            "prop": "pageprops", "ppprop": "disambiguation",
        })
        pages = [
            {"title": "Joshua Van", "ns": 0, "pageid": 2 if swapped else 1},
            {"title": "Alex Perez", "ns": 0, "pageid": 1 if swapped else 2},
        ]
        payload = {"request_url": url, "response": {"query": {
            "redirects": [{"from": "Alex Perez (fighter)", "to": "Alex Perez"}],
            "pages": pages,
        }}}
        raw = json.dumps(payload, sort_keys=True).encode()
        path = Path(self.directory.name) / "lookup.json"
        path.write_bytes(raw)
        self.connection.execute(
            "INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) "
            "VALUES('wikipedia_action_api','2026-09-26T23:00:00Z',?,?)",
            (str(path), hashlib.sha256(raw).hexdigest()),
        )

    def test_scheduled_revision_holds_unlinked_fighter(self) -> None:
        card = parse_archived_card(proof(SCHEDULE), "scheduled")
        report = compare_archived_card(self.connection, card)
        self.assertEqual(report["source_bouts"], 2)
        self.assertEqual(report["matched_bouts"], 1)
        self.assertEqual(report["identity_verified_bouts"], 0)
        self.assertEqual(report["holds"][0]["reason"], "unlinked_fighter_identity")
        self.assertFalse(report["model_eligible"])

    def test_scheduled_revision_accepts_absent_optional_final_placeholder(self) -> None:
        compact = SCHEDULE.replace("|||||}}", "|||}}", 1)
        card = parse_archived_card(proof(compact), "scheduled")
        self.assertEqual(len(card.bouts), 2)
        self.assertEqual(card.bouts[0].fighter_a.name, "Joshua Van")
        self.assertIsNone(card.bouts[0].outcome)

    def test_retained_title_lookup_verifies_stable_pair_ids(self) -> None:
        self.add_lookup()
        card = parse_archived_card(proof(SCHEDULE), "scheduled")
        report = compare_archived_card(self.connection, card)
        self.assertEqual(report["matched_bouts"], 1)
        self.assertEqual(report["identity_verified_bouts"], 1)
        self.assertTrue(report["matches"][0]["identity_verified"])

    def test_swapped_link_targets_cannot_pass_identity_review(self) -> None:
        self.add_lookup(swapped=True)
        card = parse_archived_card(proof(SCHEDULE), "scheduled")
        report = compare_archived_card(self.connection, card)
        self.assertEqual(report["identity_verified_bouts"], 0)
        self.assertEqual(report["identity_holds"][0]["reason"],
                         "source_title_id_mismatch")

    def test_completed_revision_checks_winner(self) -> None:
        card = parse_archived_card(proof(RESULTS), "completed")
        report = compare_archived_card(self.connection, card)
        self.assertEqual(report["matched_bouts"], 1)
        self.connection.execute(
            "UPDATE results SET winner_fighter_id = 'wikipedia:2' WHERE bout_id = 'bout-one'"
        )
        changed = compare_archived_card(self.connection, card)
        self.assertEqual(changed["matched_bouts"], 0)
        self.assertIn("winner_mismatch", {row["reason"] for row in changed["holds"]})

    def test_changed_event_date_is_rejected(self) -> None:
        card = parse_archived_card(proof(SCHEDULE.replace("2026|9|19", "2026|9|20")),
                                   "scheduled")
        with self.assertRaisesRegex(ValueError, "event ID/date"):
            compare_archived_card(self.connection, card)


if __name__ == "__main__":
    unittest.main()
