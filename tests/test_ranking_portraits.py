"""Exact official portrait identities, byte receipts, roster binding and gaps."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ufc_odds_model import ranking_portraits as subject
from ufc_odds_model.rankings import parse_rankings, _digest as rankings_digest
from tests.test_rankings import page, FETCHED


URL = "https://www.ufc.com/athlete/exact-athlete"
OWN = "https://ufc.com/images/styles/event_results_athlete_headshot/s3/2026-10/OWN.png?itok=observed"
OPPONENT = "https://ufc.com/images/styles/event_results_athlete_headshot/s3/2026-10/OPPONENT.png?itok=also-observed"
HERO = "https://ufc.com/images/styles/athlete_bio_full_body/s3/2026-10/OWN_L.png?itok=observed-too"


def athlete_page(url=URL, *, own=OWN, canonical=None):
    return (f'<link rel="canonical" href="{canonical or url}">'
            '<meta property="og:image" content="https://ufc.com/images/2026-10/OWN.png">'
            f'<img class="image-style-event-results-athlete-headshot" src="{OPPONENT}" alt="Opponent">'
            f'<img class="hero-profile__image" src="{HERO}" alt="Own athlete">'
            f'<img class="image-style-event-results-athlete-headshot" src="{own}">')


def png(width=256, height=160):
    def chunk(kind, data): return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data) & 0xffffffff)
    # Small solid-color fixture only; no generated or synthetic portrait enters
    # the captured source manifest or production assets.
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * width * 3) * height)) + chunk(b"IEND", b""))


def rankings(): return parse_rankings(page(), fetched_at_utc=FETCHED)


def entries(roster):
    result = {}
    for url, athlete in subject.ranked_athletes(roster).items():
        result[url] = {**athlete, "source_page": url, "credited_owner": "UFC", "fetched_at_utc": FETCHED,
                       "status": "available", "image_url": OWN, "image_kind": "headshot", "page_sha256": "b" * 64,
                       **subject.inspect_image(png())}
    return result


class RankingPortraitTests(unittest.TestCase):
    def test_own_metadata_headshot_wins_over_opponent_and_hero(self):
        parsed = subject.extract_athlete_portraits(athlete_page(), athlete_url=URL)
        self.assertEqual([p["image_url"] for p in parsed["candidates"]], [OWN, HERO])
        self.assertEqual(parsed["candidates"][0]["image_kind"], "headshot")
        self.assertNotIn(OPPONENT, [p["image_url"] for p in parsed["candidates"]])

    def test_no_filename_guess_when_headshot_does_not_match_own_page(self):
        parsed = subject.extract_athlete_portraits(athlete_page(own=OPPONENT), athlete_url=URL)
        self.assertEqual(parsed["candidates"], [{"image_url": HERO, "image_kind": "profile_full_body", "identity_basis": "official_athlete_page_own_hero_image"}])

    def test_challenge_wrong_identity_and_unsafe_images_cannot_be_used(self):
        for html in ("<html>Access denied</html>", athlete_page(canonical="https://www.ufc.com/athlete/other-athlete")):
            with self.subTest(html=html), self.assertRaises(ValueError): subject.extract_athlete_portraits(html, athlete_url=URL)
        for url in ("https://evil.example/images/person.png", "javascript:alert(1)", "https://ufc.com@evil.example/images/a.png", "http://ufc.com/images/a.png"):
            with self.subTest(url=url), self.assertRaises(ValueError): subject.image_url(url)

    def test_png_bytes_dimensions_type_and_crc_are_verified(self):
        inspected = subject.inspect_image(png(), "image/png")
        self.assertEqual((inspected["image_width"], inspected["image_height"]), (256, 160))
        broken = bytearray(png()); broken[40] ^= 1
        for data, mime in ((broken, "image/png"), (png()[:-1], "image/png"), (png(), "text/html"), (b"<html>blocked</html>", "image/png")):
            with self.subTest(mime=mime), self.assertRaises(ValueError): subject.inspect_image(data, mime)

    def test_complete_roster_binding_and_same_raw_refetch_are_distinct_from_capture_id(self):
        roster = rankings(); document = subject.build_snapshot(roster, entries(roster), fetched_at_utc=FETCHED)
        self.assertEqual(document["coverage"]["available"], len(subject.ranked_athletes(roster)))
        same_source = copy.deepcopy(roster); same_source["source"]["fetched_at_utc"] = "2026-10-20T03:00:00Z"; same_source["snapshot_sha256"] = rankings_digest(same_source)
        self.assertEqual(subject.validate_portraits(document, rankings=same_source), document)
        changed = copy.deepcopy(roster); changed["source"]["raw_sha256"] = "f" * 64; changed["snapshot_sha256"] = rankings_digest(changed)
        with self.assertRaisesRegex(ValueError, "different"): subject.validate_portraits(document, rankings=changed)
        missing = entries(roster); missing.pop(next(iter(missing)))
        with self.assertRaises(ValueError): subject.build_snapshot(roster, missing)

    def test_tamper_wrong_identity_bad_url_and_future_entry_are_rejected(self):
        roster = rankings(); document = subject.build_snapshot(roster, entries(roster), fetched_at_utc=FETCHED)
        for field, value in (("image_url", "https://evil.example/images/person.png"), ("athlete_url", URL), ("fetched_at_utc", "2026-10-21T00:00:00Z")):
            changed = copy.deepcopy(document); changed["portraits"][next(iter(changed["portraits"]))][field] = value; changed["snapshot_sha256"] = subject._digest(changed)
            with self.subTest(field=field), self.assertRaises(ValueError): subject.validate_portraits(changed, rankings=roster)
        changed = copy.deepcopy(document); changed["coverage"]["available"] = 0
        with self.assertRaises(ValueError): subject.validate_portraits(changed)

    def test_cutoff_rejects_future_capture_and_accepts_actual_snapshot(self):
        roster = rankings(); document = subject.build_snapshot(roster, entries(roster), fetched_at_utc=FETCHED)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portraits.json"; path.write_text(json.dumps(document))
            self.assertEqual(subject.load_portraits(path, rankings=roster, as_of=datetime(2026, 10, 20, 3, tzinfo=timezone.utc)), document)
            with self.assertRaisesRegex(ValueError, "later"): subject.load_portraits(path, as_of="2026-10-20T01:00:00Z")

    def test_bounded_capture_keeps_real_receipts_and_explicit_failures(self):
        roster = rankings(); failing = next(iter(subject.ranked_athletes(roster)))
        def download(url, destination, *, kind, timeout):
            if url == failing: raise ValueError("Official portrait source did not return HTTP 200")
            destination.write_text(athlete_page(url=url)) if kind == "page" else destination.write_bytes(png())
            return {"requested_url": url, "response_url": url, "http_status": 200, "content_type": "text/html" if kind == "page" else "image/png", "fetched_at_utc": FETCHED}
        with tempfile.TemporaryDirectory() as directory:
            document = subject.fetch_portraits(roster, raw_directory=directory, downloader=download, workers=2)
            self.assertEqual(document["coverage"]["unavailable"], 1)
            self.assertIsNone(document["portraits"][failing]["image_url"])
            capture = Path(directory) / document["raw_capture_id"]
            self.assertTrue((capture / "manifest.json").is_file())
            self.assertEqual(len(list(capture.glob("*/receipt.json"))), len(subject.ranked_athletes(roster)))
            with self.assertRaises(ValueError): subject.fetch_portraits(roster, raw_directory=directory, workers=5)

    def test_public_manifest_has_all_203_authentic_headshot_receipts(self):
        document = subject.load_portraits(subject.DEFAULT_SNAPSHOT)
        self.assertEqual(document["coverage"], {"ranked_unique_athletes": 203, "available": 203, "unavailable": 0, "headshots": 203})
        self.assertFalse(document["images_bundled"]); self.assertFalse(document["historical_model_feature_eligible"])
        for row in document["portraits"].values():
            self.assertEqual(row["image_receipt"]["http_status"], 200)
            self.assertEqual((row["image_width"], row["image_height"]), (256, 160))
        self.assertEqual(len({row["image_sha256"] for row in document["portraits"].values()}), 203)


if __name__ == "__main__": unittest.main()
