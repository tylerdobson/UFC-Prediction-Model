"""One exact Odds API response matched only to a fresh verified UFC card."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from scripts.capture_prefight_odds import capture_prefight_odds
from scripts.import_reviewed_prefight_card import _odds_input, verify_manifest


def _body(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _save(path: Path, payload: object) -> str:
    body = _body(payload)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _receipt(path: Path, request_url: str, response_sha256: str, when: str) -> str:
    return _save(path, {"schema_version": 1, "request_url": request_url,
                        "response_sha256": response_sha256,
                        "fetched_at_utc": when})


def _fighter(name: str, title: str | None, page_id: int | None) -> dict:
    return {"name": name, "linked_title": title, "page_id": page_id,
            "stable_id": f"wikipedia:{page_id}" if page_id else None}


def _bout(weight: str, first: str, second: str, *, plain_first: bool = False) -> str:
    a = first if plain_first else f"[[{first}]]"
    return ("{{MMAevent bout\n|" + weight + "\n|" + a + "\n|vs.\n|[["
            + second + "]]\n|\n|\n|\n|\n}}\n")


def _odds_event(event_id: str, first: str, second: str,
                commence: str = "2099-01-03T00:00:00Z") -> dict:
    return {
        "id": event_id, "sport_key": "mma_mixed_martial_arts",
        "commence_time": commence,
        "home_team": first, "away_team": second,
        "bookmakers": [{"key": "fixture-book", "title": "Fixture Book",
                        "last_update": "2099-01-02T10:59:00Z",
                        "markets": [{"key": "h2h", "last_update": "2099-01-02T10:59:00Z",
                                     "outcomes": [{"name": first, "price": 1.9},
                                                  {"name": second, "price": 2.0}]}]}],
    }


class CapturePrefightOddsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.card_path = self._card_manifest()
        self.payload = [
            _odds_event("exact-1", "Alpha Fighter", "Beta Fighter"),
            _odds_event("accent-1", "Natalia Silva", "Wang Cong"),
            _odds_event("alias-1", "Michael Parkin", "Johnny Walker"),
            _odds_event("held-1", "Gamma Plain", "Delta Fighter"),
            _odds_event("other-promotion", "Other One", "Other Two"),
        ]
        self.fake_key = "NEVER_STORE_ODDS_KEY"
        self.clock = lambda: datetime(2099, 1, 2, 11, 0, 2, tzinfo=timezone.utc)

    def _card_manifest(self) -> Path:
        source = ("{{Infobox MMA event\n|name= UFC 9999: Fixture\n"
                  "|date= {{start date|2099|1|2}}\n}}\n"
                  "==Fight card==\n{{MMAevent card|Main card}}\n"
                  + _bout("Lightweight", "Alpha Fighter", "Beta Fighter")
                  + _bout("Flyweight", "Natália Silva", "Wang Cong")
                  + _bout("Heavyweight", "Mick Parkin", "Johnny Walker")
                  + _bout("Welterweight", "Gamma Plain", "Delta Fighter",
                          plain_first=True)
                  + "==Announced bouts==\n* Other Fixture\n")
        page = {
            "id": 123, "title": "UFC 9999",
            "latest": {"id": 201, "timestamp": "2099-01-01T00:00:00Z"},
            "license": {"title": "Creative Commons Attribution-Share Alike 4.0",
                        "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            "source": source,
        }
        rest_url = "https://en.wikipedia.org/w/rest.php/v1/page/UFC_9999"
        source_sha = _save(self.root / "source.json", page)
        source_at = "2099-01-02T10:00:00Z"
        source_receipt_sha = _receipt(self.root / "source.receipt.json",
                                      rest_url, source_sha, source_at)
        titles = {"Alpha Fighter", "Beta Fighter", "Natália Silva",
                  "Wang Cong", "Mick Parkin", "Johnny Walker", "Delta Fighter"}
        query = urlencode({"action": "query", "format": "json", "formatversion": "2",
                           "redirects": "1", "titles": "|".join(sorted(titles)),
                           "maxlag": "5", "prop": "pageprops", "ppprop": "disambiguation"})
        lookup_url = "https://en.wikipedia.org/w/api.php?" + query
        ids = {"Alpha Fighter": 11, "Beta Fighter": 22, "Natália Silva": 33,
               "Wang Cong": 44, "Mick Parkin": 55, "Johnny Walker": 66,
               "Delta Fighter": 77}
        lookup = {"query": {"pages": [{"title": name, "pageid": ids[name], "ns": 0}
                                      for name in sorted(titles)]}}
        lookup_sha = _save(self.root / "lookup.json", lookup)
        lookup_at = "2099-01-02T10:00:01Z"
        lookup_receipt_sha = _receipt(self.root / "lookup.receipt.json",
                                      lookup_url, lookup_sha, lookup_at)
        rows = [
            {"position": 1, "weight_class": "Lightweight",
             "fighter_a": _fighter("Alpha Fighter", "Alpha Fighter", 11),
             "fighter_b": _fighter("Beta Fighter", "Beta Fighter", 22),
             "status": "source_page_ids_resolved_manual_review_pending"},
            {"position": 2, "weight_class": "Flyweight",
             "fighter_a": _fighter("Natália Silva", "Natália Silva", 33),
             "fighter_b": _fighter("Wang Cong", "Wang Cong", 44),
             "status": "source_page_ids_resolved_manual_review_pending"},
            {"position": 3, "weight_class": "Heavyweight",
             "fighter_a": _fighter("Mick Parkin", "Mick Parkin", 55),
             "fighter_b": _fighter("Johnny Walker", "Johnny Walker", 66),
             "status": "source_page_ids_resolved_manual_review_pending"},
            {"position": 4, "weight_class": "Welterweight",
             "fighter_a": _fighter("Gamma Plain", None, None),
             "fighter_b": _fighter("Delta Fighter", "Delta Fighter", 77),
             "status": "hold_identity_review"},
        ]
        manifest = {
            "captured_at_utc": source_at,
            "event": {"name": "UFC 9999: Fixture", "event_date": "2099-01-02",
                      "source_page_title": "UFC 9999", "source_page_id": 123,
                      "source_revision_id": 201,
                      "source_revision_timestamp_utc": "2099-01-01T00:00:00Z",
                      "source_revision_url": "https://en.wikipedia.org/w/index.php?title=UFC_9999&oldid=201"},
            "source": {"api_url": rest_url,
                       "raw_path": "source.json", "raw_sha256": source_sha,
                       "receipt_path": "source.receipt.json",
                       "receipt_sha256": source_receipt_sha,
                       "fetched_at_utc": source_at,
                       "license_title": page["license"]["title"],
                       "license_url": page["license"]["url"]},
            "fighter_lookup": {
                "api_url": lookup_url, "raw_path": "lookup.json",
                "raw_sha256": lookup_sha, "receipt_path": "lookup.receipt.json",
                "receipt_sha256": lookup_receipt_sha, "fetched_at_utc": lookup_at,
                "linked_titles_requested": len(titles),
                "linked_titles_resolved": len(ids),
            },
            "official_start_review": {
                "source_url": "https://www.ufc.com/event/fixture",
                "local_timezone": "America/New_York",
                "event_start_utc_for_conservative_cutoff": "2099-01-02T20:00:00Z",
                "published_segment_starts": [{"segment": "main_card",
                                              "local_time": "2099-01-02T15:00:00",
                                              "utc_time": "2099-01-02T20:00:00Z"}],
            },
            "fight_card_rows": rows,
            "review_summary": {"fight_card_rows": 4,
                               "fully_linked_identity_rows": 3,
                               "identity_hold_rows": 1,
                               "manual_person_review_required": True},
        }
        path = self.root / "card.json"
        _save(path, manifest)
        self.assertEqual(verify_manifest(path)["card_count"], 4)
        return path

    def _capture(self, name: str, payload: list[dict] | None = None,
                 captured_at: str = "2099-01-02T11:00:01Z") -> tuple[dict, Path]:
        output = self.root / name
        result = capture_prefight_odds(
            self.card_path, output,
            fetch=lambda key, region: (_body(self.payload if payload is None else payload),
                                       captured_at),
            key_provider=lambda name: self.fake_key,
            clock=self.clock,
        )
        return result, output

    def test_exact_accent_alias_and_held_identity_are_separate(self) -> None:
        result, output = self._capture("captured")
        self.assertEqual(result["same_local_date_future_events"], 5)
        self.assertEqual(result["coverage"]["exact_pair_names"], 1)
        self.assertEqual(result["coverage"]["accent_only_pair_names"], 1)
        self.assertEqual(result["coverage"]["manual_alias_or_name_review"], 1)
        self.assertEqual(result["coverage"]["identity_hold_rows"], 1)
        copy = json.loads((output / "card-with-odds-manifest.json").read_bytes())
        self.assertEqual([row["status"] for row in copy["odds_coverage_comparison"]["rows"]],
                         ["exact", "accent_normalization",
                          "alias_or_opponent_review_required", "hold_identity_review"])
        self.assertEqual(json.loads(self.card_path.read_bytes()).get(
            "odds_coverage_comparison"), None)
        for section in ("source", "fighter_lookup"):
            self.assertTrue(Path(copy[section]["raw_path"]).is_absolute())
            self.assertTrue(Path(copy[section]["receipt_path"]).is_absolute())
        verified = verify_manifest(output / "card-with-odds-manifest.json")
        checked = _odds_input(output / "odds-intake-manifest.json", verified)
        self.assertEqual(checked["raw_bytes"], _body(self.payload))
        odds_manifest = json.loads((output / "odds-intake-manifest.json").read_bytes())
        self.assertEqual(odds_manifest["sha256"],
                         hashlib.sha256((output / "odds.response.json").read_bytes()).hexdigest())
        for path in output.iterdir():
            self.assertNotIn(self.fake_key, path.read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            self._capture("captured")

    def test_duplicate_pair_is_held_as_ambiguous(self) -> None:
        duplicate = _odds_event("exact-duplicate", "Beta Fighter", "Alpha Fighter")
        _, output = self._capture("ambiguous", self.payload + [duplicate])
        comparison = json.loads((output / "card-with-odds-manifest.json").read_bytes())[
            "odds_coverage_comparison"]
        self.assertEqual(comparison["rows"][0]["status"],
                         "ambiguous_pair_review_required")
        self.assertEqual(comparison["summary"]["exact_pair_names"], 0)

    def test_possible_opponent_substitution_holds_an_otherwise_exact_pair(self) -> None:
        substitute = _odds_event("new-opponent", "Alpha Fighter", "Replacement")
        _, output = self._capture("substitution", self.payload + [substitute])
        comparison = json.loads((output / "card-with-odds-manifest.json").read_bytes())[
            "odds_coverage_comparison"]
        self.assertEqual(comparison["rows"][0]["status"],
                         "ambiguous_pair_review_required")
        self.assertEqual(comparison["summary"]["exact_pair_names"], 0)

    def test_wrong_local_date_and_past_event_cannot_match(self) -> None:
        wrong_date = _odds_event("wrong-date", "Alpha Fighter", "Beta Fighter",
                                 "2099-01-04T00:00:00Z")
        past = _odds_event("past", "Natália Silva", "Wang Cong",
                           "2099-01-02T09:00:00Z")
        _, output = self._capture("timing", [wrong_date, past])
        comparison = json.loads((output / "card-with-odds-manifest.json").read_bytes())[
            "odds_coverage_comparison"]
        self.assertEqual(comparison["summary"]["exact_pair_names"], 0)
        self.assertEqual(comparison["summary"]["accent_only_pair_names"], 0)

    def test_missing_card_receipts_and_late_capture_fail_before_output(self) -> None:
        card = json.loads(self.card_path.read_bytes())
        for section in ("source", "fighter_lookup"):
            for field in ("receipt_path", "receipt_sha256", "fetched_at_utc"):
                card[section].pop(field)
        legacy = self.root / "legacy.json"
        _save(legacy, card)
        with self.assertRaisesRegex(ValueError, "receipts"):
            capture_prefight_odds(
                legacy, self.root / "legacy-odds",
                fetch=lambda *_: (_body(self.payload), "2099-01-02T11:00:01Z"),
                key_provider=lambda _: self.fake_key, clock=self.clock,
            )
        self.assertFalse((self.root / "legacy-odds").exists())
        with self.assertRaisesRegex(ValueError, "precede card start"):
            self._capture("late-odds", captured_at="2099-01-02T20:00:00Z")
        self.assertFalse((self.root / "late-odds").exists())
        with self.assertRaisesRegex(ValueError, "follow the fighter lookup"):
            self._capture("future-odds", captured_at="2099-01-02T11:00:03Z")
        self.assertFalse((self.root / "future-odds").exists())

    def test_odds_fetch_receipt_is_verified_against_exact_response(self) -> None:
        _, output = self._capture("receipt-check")
        verified = verify_manifest(output / "card-with-odds-manifest.json")
        odds_manifest = output / "odds-intake-manifest.json"
        checked = _odds_input(odds_manifest, verified)
        self.assertEqual(json.loads(checked["receipt_bytes"])["fetched_at_utc"],
                         "2099-01-02T11:00:01Z")
        receipt_path = output / "odds.receipt.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["response_sha256"] = "0" * 64
        _save(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "Odds capture receipt SHA-256 mismatch"):
            _odds_input(odds_manifest, verified)

    def test_provider_error_cannot_expose_key(self) -> None:
        def bad_fetch(key: str, region: str):
            raise RuntimeError(f"Request URL included {key}")

        with self.assertRaises(RuntimeError) as raised:
            capture_prefight_odds(self.card_path, self.root / "provider-error",
                                  fetch=bad_fetch,
                                  key_provider=lambda _: self.fake_key,
                                  clock=self.clock)
        self.assertNotIn(self.fake_key, str(raised.exception))
        self.assertFalse((self.root / "provider-error").exists())


if __name__ == "__main__":
    unittest.main()
