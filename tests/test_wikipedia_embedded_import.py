"""Offline integration check for a reviewed card embedded in a year page."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.dashboard_data import load_dashboard
from ufc_odds_model.wikipedia_catalog import CatalogResult, CatalogReview, CatalogYear
from ufc_odds_model.wikipedia_history import import_wikipedia_embedded_history


class EmbeddedImportTests(unittest.TestCase):
    def test_embedded_card_has_unique_event_id_and_page_receipt(self) -> None:
        review = CatalogReview(
            2012, 1, "Event link points to a section, not an event page", "212",
            "[[2012 in UFC#UFC 149: Faber vs. Barão|UFC 149: Faber vs. Barão]]",
            "{{dts|2012|Jul|21}}",
        )
        page = {
            "id": 777,
            "title": "2012 in UFC",
            "latest": {"id": 778, "timestamp": "2026-09-01T00:00:00Z"},
            "license": {"title": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            "source": (
                "==UFC 149: Faber vs. Barão==\n"
                "{{Infobox MMA event\n|name=UFC 149\n|date={{start date|2012|7|21}}\n}}\n"
                "===Results===\n"
                "{{MMAevent bout|Lightweight|[[Alex Pereira]]|def.|[[Jiří Procházka]]|KO|1|1:00}}\n"
                "==Next card==\n"
            ),
        }
        year = CatalogYear(
            2012, 777, 778, "2026-09-01T00:00:00Z", "CC BY-SA 4.0",
            "https://creativecommons.org/licenses/by-sa/4.0/deed.en",
            "https://en.wikipedia.org/w/rest.php/v1/page/2012_in_UFC",
            page, (), (review,),
        )
        catalog = CatalogResult((), (review,), (year,))

        def fetch(url: str) -> dict:
            if "/w/rest.php/v1/page/" in url:
                self.assertTrue(url.endswith("/2012_in_UFC"))
                return page
            titles = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["titles"][0].split("|")
            known = {"2012 in UFC": 777, "Alex Pereira": 1001, "Jiří Procházka": 1002}
            return {"query": {"pages": [
                {"title": title, "pageid": known[title], "ns": 0}
                for title in titles if title in known
            ]}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with db.connect(root / "research.sqlite") as connection:
                db.init_db(connection)
                with patch("ufc_odds_model.wikipedia_catalog.enumerate_event_catalog", return_value=catalog):
                    result = import_wikipedia_embedded_history(
                        connection, 2012, 2012, raw_dir=root / "raw",
                        review_out=root / "review.json", fetch_json=fetch,
                    )
                self.assertEqual(result["parsed_embedded_cards"], 1)
                self.assertEqual(result["imported_bouts"], 1)
                event = connection.execute("SELECT event_id, source_event_id FROM events").fetchone()
                self.assertEqual(event["event_id"], "wikipedia_research:777:ufc%20149%3A%20faber%20vs.%20bar%C3%A3o")
                self.assertEqual(event["source_event_id"], event["event_id"].split(":", 1)[1])
                receipt = connection.execute("SELECT page_url FROM wikipedia_source_receipts").fetchone()[0]
                self.assertIn("#UFC_149", receipt)
                self.assertEqual(len(json.loads((root / "review.json").read_text())["failed_embedded_cards"]), 0)
            history = load_dashboard(root / "research.sqlite")["historical_research"]
            self.assertEqual(history["events"], 1)
            self.assertEqual(history["event_page_receipts"], 1)


if __name__ == "__main__":
    unittest.main()
