"""Offline checks for the bounded, research-only MediaWiki source importer."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.cli import _check_prefight_event, main
from ufc_odds_model.wikipedia_history import (
    _resolve_page_ids,
    import_wikipedia_history,
    parse_event_page,
)


def _page(bouts: str, revision: int = 123) -> dict:
    return {
        "id": 74123456,
        "title": "UFC 295",
        "latest": {"id": revision, "timestamp": "2024-01-01T00:00:00Z"},
        "license": {
            "title": "Creative Commons Attribution-Share Alike 4.0",
            "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en",
        },
        "source": (
            "{{Infobox MMA event\n"
            "|name= UFC 295: Research Example\n"
            "|date= {{start date|2023|11|11}}\n"
            "}}\n"
            "==Results==\n"
            "{{MMAevent}}\n"
            f"{bouts}\n"
            "==References==\n"
        ),
    }


WIN = """{{MMAevent bout
|Light Heavyweight
|[[Alex Pereira]]
|def.
|[[Jiří Procházka]]
|TKO (punches)
|2
|4:08
|
}}"""

DRAW = """{{MMAevent bout
|Lightweight
|[[Nazim Sadykhov]]
|vs.
|Viacheslav Borshchev
|Draw (majority)
|3
|5:00
|
}}"""

NO_CONTEST = """{{MMAevent bout
|Welterweight
|[[Alex Pereira]]
|vs.
|[[Jiří Procházka]]
|No Contest (accidental foul)
|1
|1:00
|
}}"""


class FakeMediaWiki:
    def __init__(self, page: dict, disambiguation_title: str | None = None):
        self.page = page
        self.disambiguation_title = disambiguation_title
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        if "/w/rest.php/v1/page/UFC_295" in url:
            return self.page
        if "/w/api.php?" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert query["prop"] == ["pageprops"]
            assert query["ppprop"] == ["disambiguation"]
            self_titles = set(query["titles"][0].split("|"))
            known = {
                "Alex Pereira": 1001,
                "Jiří Procházka": 1002,
                "Nazim Sadykhov": 1003,
                "Viacheslav Borshchev": 1004,
            }
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": page_id, "ns": 0, "title": title,
                            "pageprops": {"disambiguation": ""}
                            if title == self.disambiguation_title else {},
                        }
                        for title, page_id in known.items() if title in self_titles
                    ]
                }
            }
        raise AssertionError(f"Unexpected network request: {url}")


class WikipediaHistoryTests(unittest.TestCase):
    def test_parse_binary_draw_and_no_contest(self) -> None:
        event = parse_event_page(_page("\n".join((WIN, DRAW, NO_CONTEST))), 295)
        self.assertEqual([bout.outcome for bout in event.bouts], ["win", "draw", "no_contest"])
        self.assertEqual(event.event_date, "2023-11-11")
        self.assertEqual(event.bouts[1].fighter_b.title, None)

    def test_import_skips_unlinked_identity_and_writes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                fetch = FakeMediaWiki(_page("\n".join((WIN, DRAW))))
                review = root / "review.json"
                result = import_wikipedia_history(
                    connection, 295, 295, raw_dir=root / "raw",
                    review_out=review, fetch_json=fetch,
                )
                self.assertEqual(result["imported_bouts"], 1)
                self.assertEqual(result["skipped_unresolved_bouts"], 1)
                with self.assertRaisesRegex(ValueError, "Research-only Wikipedia history"):
                    _check_prefight_event(connection, object())
                self.assertTrue(all("en.wikipedia.org" in url for url in fetch.urls))
                rows = connection.execute("SELECT outcome FROM results").fetchall()
                self.assertEqual([row["outcome"] for row in rows], ["win"])
                receipt = connection.execute(
                    """
                    SELECT w.revision_id, w.page_url, w.license_url,
                           i.payload_path, i.sha256
                    FROM wikipedia_source_receipts w
                    JOIN ingestion_runs i USING(run_id)
                    """
                ).fetchone()
                self.assertEqual(receipt["revision_id"], 123)
                self.assertEqual(receipt["page_url"], "https://en.wikipedia.org/wiki/UFC_295")
                self.assertIn("by-sa/4.0", receipt["license_url"])
                self.assertTrue(Path(receipt["payload_path"]).exists())
                self.assertEqual(
                    receipt["sha256"],
                    hashlib.sha256(Path(receipt["payload_path"]).read_bytes()).hexdigest(),
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(
                        "UPDATE wikipedia_source_receipts SET revision_id = 999"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute("DELETE FROM wikipedia_source_receipts")
                self.assertEqual(connection.execute(
                    "SELECT revision_id FROM wikipedia_source_receipts"
                ).fetchone()[0], 123)
                report = json.loads(review.read_text())
                self.assertEqual(report["unresolved_fighters"][0]["fighter_name"], "Viacheslav Borshchev")
                self.assertEqual(report["skipped_bouts"][0]["outcome"], "draw")

    def test_revision_snapshot_replay_is_content_addressed_and_tampering_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                fetch = FakeMediaWiki(_page(WIN))
                options = {
                    "raw_dir": root / "raw",
                    "review_out": root / "review.json",
                    "fetch_json": fetch,
                }
                import_wikipedia_history(connection, 295, 295, **options)
                first = connection.execute(
                    "SELECT payload_path, sha256 FROM ingestion_runs"
                ).fetchone()
                raw_path = Path(first["payload_path"])
                self.assertEqual(raw_path, (root / "raw" / f"{first['sha256']}.json").resolve())
                original = raw_path.read_bytes()
                import_wikipedia_history(connection, 295, 295, **options)
                self.assertEqual(connection.execute(
                    "SELECT payload_path FROM ingestion_runs ORDER BY run_id DESC LIMIT 1"
                ).fetchone()[0], str(raw_path))
                self.assertEqual(raw_path.read_bytes(), original)
                raw_path.write_bytes(b"tampered evidence")
                with self.assertRaisesRegex(ValueError, "Existing revision snapshot changed"):
                    import_wikipedia_history(connection, 295, 295, **options)
                self.assertEqual(raw_path.read_bytes(), b"tampered evidence")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM ingestion_runs"
                ).fetchone()[0], 2)

    def test_reviewed_crosswalk_recovers_draw_without_name_generated_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crosswalk = root / "crosswalk.json"
            crosswalk.write_text(json.dumps({
                "schema_version": 1,
                "fighters": {
                    "Viacheslav Borshchev": {
                        "fighter_id": "manual:borshchev-verified",
                        "canonical_name": "Viacheslav Borshchev",
                        "evidence_url": "https://example.org/identity-check",
                        "reviewed_by": "human reviewer",
                    }
                },
            }))
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                result = import_wikipedia_history(
                    connection, 295, 295, raw_dir=root / "raw",
                    review_out=root / "review.json", crosswalk_path=crosswalk,
                    fetch_json=FakeMediaWiki(_page("\n".join((WIN, DRAW)))),
                )
                self.assertEqual(result["imported_bouts"], 2)
                self.assertEqual(result["skipped_unresolved_bouts"], 0)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) AS n FROM results WHERE outcome='draw'").fetchone()["n"],
                    1,
                )
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM fighters WHERE fighter_id='manual:borshchev-verified'"
                ).fetchone())

    def test_crosswalk_can_verify_existing_wikipedia_page_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crosswalk = root / "crosswalk.json"
            entry = {
                "fighter_id": "wikipedia:1004",
                "canonical_name": "Viacheslav Borshchev",
                "page_title": "Viacheslav Borshchev",
                "evidence_url": "https://en.wikipedia.org/wiki/Viacheslav_Borshchev",
                "reviewed_by": "human reviewer",
            }
            crosswalk.write_text(json.dumps({
                "schema_version": 1, "fighters": {"Viacheslav Borshchev": entry}
            }))
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                imported = import_wikipedia_history(
                    connection, 295, 295, raw_dir=root / "raw",
                    review_out=root / "review.json", crosswalk_path=crosswalk,
                    fetch_json=FakeMediaWiki(_page("\n".join((WIN, DRAW)))),
                )
                self.assertEqual(imported["imported_bouts"], 2)
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM fighters WHERE fighter_id='wikipedia:1004'"
                ).fetchone())
                entry["fighter_id"] = "wikipedia:9999"
                crosswalk.write_text(json.dumps({
                    "schema_version": 1, "fighters": {"Viacheslav Borshchev": entry}
                }))
                with self.assertRaisesRegex(ValueError, "page ID mismatch"):
                    import_wikipedia_history(
                        connection, 295, 295, raw_dir=root / "raw",
                        review_out=root / "review.json", crosswalk_path=crosswalk,
                        fetch_json=FakeMediaWiki(_page("\n".join((WIN, DRAW)))),
                    )

    def test_disambiguation_page_is_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                result = import_wikipedia_history(
                    connection, 295, 295, raw_dir=root / "raw",
                    review_out=root / "review.json",
                    fetch_json=FakeMediaWiki(_page(WIN), disambiguation_title="Jiří Procházka"),
                )
                self.assertEqual(result["imported_bouts"], 0)
                self.assertEqual(result["skipped_unresolved_bouts"], 1)
                self.assertEqual(
                    json.loads((root / "review.json").read_text())["unresolved_fighters"][0]["reason"],
                    "missing_page_id",
                )

    def test_redirected_title_resolves_to_canonical_page_id(self) -> None:
        def fetch(url: str) -> dict:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(query["titles"], ["Alias Fighter"])
            return {
                "query": {
                    "redirects": [{"from": "Alias Fighter", "to": "Canonical Fighter"}],
                    "pages": [{
                        "pageid": 88, "title": "Canonical Fighter", "ns": 0,
                        "pageprops": {},
                    }],
                }
            }

        self.assertEqual(_resolve_page_ids(fetch, {"Alias Fighter"}), {"Alias Fighter": 88})

    def test_no_contest_is_stored_without_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                result = import_wikipedia_history(
                    connection, 295, 295, raw_dir=root / "raw",
                    review_out=root / "review.json", fetch_json=FakeMediaWiki(_page(NO_CONTEST)),
                )
                self.assertEqual(result["imported_bouts"], 1)
                stored = connection.execute(
                    "SELECT outcome, winner_fighter_id FROM results"
                ).fetchone()
                self.assertEqual(stored["outcome"], "no_contest")
                self.assertIsNone(stored["winner_fighter_id"])

    def test_ambiguous_result_fails_before_database_writes(self) -> None:
        ambiguous = WIN.replace("|def.", "|vs.")
        with self.assertRaisesRegex(ValueError, "ambiguous result marker"):
            parse_event_page(_page(ambiguous), 295)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                with self.assertRaisesRegex(ValueError, "ambiguous result marker"):
                    import_wikipedia_history(
                        connection, 295, 295, raw_dir=root / "raw",
                        review_out=root / "review.json", fetch_json=FakeMediaWiki(_page(ambiguous)),
                    )
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_bounded_range_and_cli_default_db_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with db.connect(Path(directory) / "research.sqlite") as connection:
                db.init_db(connection)
                with self.assertRaisesRegex(ValueError, "bounded"):
                    import_wikipedia_history(connection, 294, 295, fetch_json=lambda _: {})
        self.assertEqual(main(["import-wikipedia-history"]), 1)


if __name__ == "__main__":
    unittest.main()
