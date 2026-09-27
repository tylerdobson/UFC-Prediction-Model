"""Offline capture of a new source-dated candidate UFC card."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from scripts.capture_prefight_card import capture_prefight_card
from scripts.import_reviewed_prefight_card import verify_manifest


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fighter(name: str, title: str | None, page_id: int | None) -> dict:
    return {"name": name, "linked_title": title, "page_id": page_id,
            "stable_id": f"wikipedia:{page_id}" if page_id else None}


def _card(opponent: str) -> str:
    return """{{Infobox MMA event
|name= UFC 9999: Fixture
|date= {{start date|2099|1|2}}
}}
==Fight card==
{{MMAevent card|Main card}}
{{MMAevent bout
|Lightweight
|[[Alpha Fighter]]
|vs.
|[[%s]]
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
* Other Fixture
""" % opponent


def _page(opponent: str, revision: int, *, page_id: int = 123,
          date: str = "2099|1|2") -> dict:
    page = {
        "id": page_id, "title": "UFC 9999",
        "latest": {"id": revision, "timestamp": "2099-01-01T00:00:00Z"},
        "license": {"title": "Creative Commons Attribution-Share Alike 4.0",
                    "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
        "source": _card(opponent),
    }
    page["source"] = page["source"].replace("2099|1|2", date)
    return page


def _lookup_url(titles: set[str]) -> str:
    query = urlencode({
        "action": "query", "format": "json", "formatversion": "2",
        "redirects": "1", "titles": "|".join(sorted(titles)), "maxlag": "5",
        "prop": "pageprops", "ppprop": "disambiguation",
    })
    return "https://en.wikipedia.org/w/api.php?" + query


class CapturePrefightCardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.seed_path = self._seed()
        self.rest_url = "https://en.wikipedia.org/w/rest.php/v1/page/UFC_9999"
        self.fresh_page = _page("Epsilon Fighter", 201)
        self.fresh_lookup_url = _lookup_url({"Alpha Fighter", "Delta Fighter",
                                             "Epsilon Fighter"})
        self.fresh_lookup = {"query": {"pages": [
            {"title": "Alpha Fighter", "pageid": 11, "ns": 0},
            {"title": "Delta Fighter", "pageid": 44, "ns": 0},
            {"title": "Epsilon Fighter", "pageid": 55, "ns": 0},
        ]}}
        self.clock = lambda: datetime(2099, 1, 1, 3, tzinfo=timezone.utc)

    def _seed(self) -> Path:
        page = _page("Beta Fighter", 200)
        page_path = self.root / "old-page.json"
        page_bytes = _body(page)
        page_path.write_bytes(page_bytes)
        lookup = {"query": {"pages": [
            {"title": "Alpha Fighter", "pageid": 11, "ns": 0},
            {"title": "Beta Fighter", "pageid": 22, "ns": 0},
            {"title": "Delta Fighter", "pageid": 44, "ns": 0},
        ]}}
        lookup_path = self.root / "old-lookup.json"
        lookup_bytes = _body(lookup)
        lookup_path.write_bytes(lookup_bytes)
        seed = {
            "captured_at_utc": "2099-01-01T01:00:00Z",
            "event": {"name": "UFC 9999: Fixture", "event_date": "2099-01-02",
                      "source_page_title": "UFC 9999", "source_page_id": 123,
                      "source_revision_id": 200,
                      "source_revision_timestamp_utc": "2099-01-01T00:00:00Z",
                      "source_revision_url": "https://en.wikipedia.org/w/index.php?title=UFC_9999&oldid=200"},
            "source": {"api_url": "https://en.wikipedia.org/w/rest.php/v1/page/UFC_9999",
                       "raw_path": str(page_path),
                       "raw_sha256": hashlib.sha256(page_bytes).hexdigest(),
                       "license_title": page["license"]["title"],
                       "license_url": page["license"]["url"]},
            "fighter_lookup": {
                "api_url": _lookup_url({"Alpha Fighter", "Beta Fighter", "Delta Fighter"}),
                "raw_path": str(lookup_path),
                "raw_sha256": hashlib.sha256(lookup_bytes).hexdigest(),
                "linked_titles_requested": 3, "linked_titles_resolved": 3,
            },
            "official_start_review": {
                "source_url": "https://www.ufc.com/event/fixture",
                "local_timezone": "UTC",
                "event_start_utc_for_conservative_cutoff": "2099-01-02T18:00:00Z",
                "published_segment_starts": [{"segment": "main_card",
                                              "local_time": "2099-01-02T18:00:00",
                                              "utc_time": "2099-01-02T18:00:00Z"}],
            },
            "fight_card_rows": [
                {"position": 1, "weight_class": "Lightweight",
                 "fighter_a": _fighter("Alpha Fighter", "Alpha Fighter", 11),
                 "fighter_b": _fighter("Beta Fighter", "Beta Fighter", 22),
                 "status": "source_page_ids_resolved_manual_review_pending"},
                {"position": 2, "weight_class": "Welterweight",
                 "fighter_a": _fighter("Gamma Plain", None, None),
                 "fighter_b": _fighter("Delta Fighter", "Delta Fighter", 44),
                 "status": "hold_identity_review"},
            ],
            "review_summary": {"fight_card_rows": 2, "fully_linked_identity_rows": 1,
                               "identity_hold_rows": 1},
            "odds_coverage_comparison": {"stale": True},
        }
        path = self.root / "seed.json"
        path.write_bytes(_body(seed))
        self.assertEqual(verify_manifest(path)["card_count"], 2)
        return path

    def _fetch(self, page: dict | None = None, lookup: dict | None = None,
               *, source_at: str = "2099-01-01T02:00:00Z",
               lookup_at: str = "2099-01-01T02:00:01Z"):
        calls: list[str] = []

        def fetch(url: str) -> tuple[bytes, str]:
            calls.append(url)
            if url == self.rest_url:
                return _body(page if page is not None else self.fresh_page), source_at
            if url == self.fresh_lookup_url:
                return _body(lookup if lookup is not None else self.fresh_lookup), lookup_at
            raise AssertionError(f"Unexpected network request: {url}")

        return fetch, calls

    def test_fresh_receipts_manifest_and_identity_holds(self) -> None:
        fetch, calls = self._fetch()
        output = self.root / "fresh-capture"
        summary = capture_prefight_card(self.seed_path, output,
                                        fetch=fetch, clock=self.clock)
        self.assertEqual(calls, [self.rest_url, self.fresh_lookup_url])
        self.assertEqual((summary["fight_card_rows"],
                          summary["fully_linked_identity_rows"],
                          summary["identity_hold_rows"]), (2, 1, 1))
        manifest = json.loads((output / "manifest.json").read_bytes())
        self.assertEqual(manifest["captured_at_utc"], "2099-01-01T02:00:00Z")
        self.assertEqual(manifest["event"]["source_revision_id"], 201)
        self.assertEqual(manifest["fight_card_rows"][0]["fighter_b"]["stable_id"],
                         "wikipedia:55")
        self.assertNotIn("Beta Fighter", json.dumps(manifest))
        self.assertNotIn("odds_coverage_comparison", manifest)
        self.assertNotIn("reviewed_by", json.dumps(manifest))
        self.assertTrue(manifest["review_summary"]["manual_person_review_required"])
        for section, raw_name, receipt_name, timestamp in (
            ("source", "source.response.json", "source.receipt.json",
             "2099-01-01T02:00:00Z"),
            ("fighter_lookup", "fighters.response.json", "fighters.receipt.json",
             "2099-01-01T02:00:01Z"),
        ):
            info = manifest[section]
            response_bytes = (output / raw_name).read_bytes()
            receipt_bytes = (output / receipt_name).read_bytes()
            receipt = json.loads(receipt_bytes)
            self.assertEqual(info["fetched_at_utc"], timestamp)
            self.assertEqual(info["receipt_sha256"],
                             hashlib.sha256(receipt_bytes).hexdigest())
            self.assertEqual(receipt, {
                "schema_version": 1, "request_url": info["api_url"],
                "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
                "fetched_at_utc": timestamp,
            })
        self.assertEqual(verify_manifest(output / "manifest.json")["card_count"], 2)
        with self.assertRaises(FileExistsError):
            capture_prefight_card(self.seed_path, output, fetch=fetch, clock=self.clock)
        self.assertEqual(len(calls), 2)

    def test_changed_page_id_or_event_date_is_rejected_without_writes(self) -> None:
        for name, page in (("changed-id", _page("Epsilon Fighter", 201, page_id=999)),
                           ("changed-date", _page("Epsilon Fighter", 201,
                                                  date="2099|1|3"))):
            with self.subTest(name=name):
                output = self.root / name
                fetch, calls = self._fetch(page=page)
                with self.assertRaises(ValueError):
                    capture_prefight_card(self.seed_path, output,
                                          fetch=fetch, clock=self.clock)
                self.assertEqual(calls, [self.rest_url])
                self.assertFalse(output.exists())

    def test_elapsed_start_rejects_before_fetch(self) -> None:
        fetch, calls = self._fetch()
        with self.assertRaisesRegex(ValueError, "start has elapsed"):
            capture_prefight_card(
                self.seed_path, self.root / "late", fetch=fetch,
                clock=lambda: datetime(2099, 1, 2, 18, tzinfo=timezone.utc),
            )
        self.assertEqual(calls, [])
        self.assertFalse((self.root / "late").exists())

    def test_lookup_error_or_time_order_rejects_without_manifest(self) -> None:
        cases = (
            ("lookup-error", {"error": {"code": "maxlag"}},
             "2099-01-01T02:00:01Z"),
            ("lookup-before-source", self.fresh_lookup,
             "2099-01-01T01:59:59Z"),
            ("lookup-after-start", self.fresh_lookup,
             "2099-01-02T18:00:00Z"),
        )
        for name, lookup, fetched_at in cases:
            with self.subTest(name=name):
                fetch, calls = self._fetch(lookup=lookup, lookup_at=fetched_at)
                output = self.root / name
                with self.assertRaises(ValueError):
                    capture_prefight_card(self.seed_path, output,
                                          fetch=fetch, clock=self.clock)
                self.assertEqual(len(calls), 2)
                self.assertFalse(output.exists())

    def test_new_source_capture_cannot_precede_seed_or_local_clock(self) -> None:
        for name, observed in (("source-before-seed", "2099-01-01T00:59:59Z"),
                               ("source-after-clock", "2099-01-01T04:00:00Z")):
            with self.subTest(name=name):
                fetch, calls = self._fetch(source_at=observed)
                output = self.root / name
                with self.assertRaisesRegex(ValueError, "outside the verified pre-fight window"):
                    capture_prefight_card(self.seed_path, output,
                                          fetch=fetch, clock=self.clock)
                self.assertEqual(calls, [self.rest_url])
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
