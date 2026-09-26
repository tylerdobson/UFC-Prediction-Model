"""Archived title lookups require intact, exact-request identity evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.archive_identity import resolve_archived_identity_titles
from ufc_odds_model.wikipedia_history import ACTION_API


class ArchiveIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        db.init_db(self.connection)
        self.paths: list[Path] = []

    def add_lookup(self, requested: list[str], query: dict,
                   *, request_url: str | None = None) -> int:
        url = request_url or ACTION_API + "?" + urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "redirects": "1", "titles": "|".join(requested), "maxlag": "5",
            "prop": "pageprops", "ppprop": "disambiguation",
        })
        raw = json.dumps({"request_url": url, "response": {"query": query}},
                         sort_keys=True, separators=(",", ":")).encode()
        path = self.root / f"lookup-{len(self.paths)}.json"
        path.write_bytes(raw)
        self.paths.append(path)
        cursor = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES ('wikipedia_action_api', '2026-09-26T22:00:00Z', ?, ?)",
            (str(path), hashlib.sha256(raw).hexdigest()),
        )
        return int(cursor.lastrowid)

    def test_exact_requested_title_resolves_redirect_to_page_id(self) -> None:
        run_id = self.add_lookup(["Alex Perez (fighter)"], {
            "redirects": [{"from": "Alex Perez (fighter)", "to": "Alex Perez"}],
            "pages": [{"title": "Alex Perez", "ns": 0, "pageid": 1234}],
        })
        report = resolve_archived_identity_titles(
            self.connection, ["Alex Perez (fighter)", "Alex Perez"]
        )
        self.assertEqual(report["resolved"]["Alex Perez (fighter)"]["fighter_id"],
                         "wikipedia:1234")
        self.assertEqual(report["resolved"]["Alex Perez (fighter)"]["lookup_run_ids"],
                         [run_id])
        self.assertEqual(report["resolved"]["Alex Perez (fighter)"]["lookup_evidence"][0]
                         ["fetched_at_utc"], "2026-09-26T22:00:00Z")
        self.assertEqual(report["holds"],
                         [{"title": "Alex Perez", "reason": "lookup_request_missing",
                           "reasons": ["lookup_request_missing"]}])

    def test_response_page_without_exact_request_cannot_resolve(self) -> None:
        self.add_lookup(["Other Fighter"], {
            "pages": [{"title": "Joshua Van", "ns": 0, "pageid": 77}],
        })
        report = resolve_archived_identity_titles(self.connection, ["Joshua Van"])
        self.assertEqual(report["resolved"], {})
        self.assertEqual(report["holds"][0]["reason"], "lookup_request_missing")

    def test_conflicting_page_ids_are_held(self) -> None:
        self.add_lookup(["Joshua Van"], {
            "pages": [{"title": "Joshua Van", "ns": 0, "pageid": 77}],
        })
        self.add_lookup(["Joshua Van"], {
            "pages": [{"title": "Joshua Van", "ns": 0, "pageid": 88}],
        })
        report = resolve_archived_identity_titles(self.connection, ["Joshua Van"])
        self.assertEqual(report["resolved"], {})
        self.assertEqual(report["holds"][0]["reason"], "lookup_page_id_conflict")

    def test_disambiguation_or_missing_page_is_held(self) -> None:
        self.add_lookup(["Alex Perez"], {
            "pages": [{"title": "Alex Perez", "ns": 0, "pageid": 12,
                       "pageprops": {"disambiguation": ""}}],
        })
        self.add_lookup(["Joshua Van"], {
            "pages": [{"title": "Joshua Van", "ns": 0, "missing": True}],
        })
        report = resolve_archived_identity_titles(
            self.connection, ["Alex Perez", "Joshua Van"]
        )
        self.assertEqual(report["resolved"], {})
        self.assertEqual({row["title"]: row["reason"] for row in report["holds"]}, {
            "Alex Perez": "lookup_page_disambiguation",
            "Joshua Van": "lookup_page_invalid",
        })

    def test_changed_receipt_blocks_all_titles(self) -> None:
        self.add_lookup(["Joshua Van"], {
            "pages": [{"title": "Joshua Van", "ns": 0, "pageid": 77}],
        })
        self.paths[0].write_text("{}")
        report = resolve_archived_identity_titles(self.connection, ["Joshua Van"])
        self.assertEqual(report["resolved"], {})
        self.assertEqual(report["holds"][0]["reason"], "lookup_receipt_invalid")

    def test_wrong_api_url_is_not_identity_evidence(self) -> None:
        self.add_lookup(["Joshua Van"], {
            "pages": [{"title": "Joshua Van", "ns": 0, "pageid": 77}],
        }, request_url="https://example.com/w/api.php?titles=Joshua+Van")
        report = resolve_archived_identity_titles(self.connection, ["Joshua Van"])
        self.assertEqual(report["resolved"], {})
        self.assertEqual(report["holds"][0]["reason"], "lookup_receipt_invalid")


if __name__ == "__main__":
    unittest.main()
