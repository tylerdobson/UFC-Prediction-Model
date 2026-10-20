"""Official ranking systems, source receipts, ties, and non-invented gaps."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ufc_odds_model.rankings import DEFAULT_SNAPSHOT, fetch_rankings, load_rankings, parse_rankings, validate_rankings

FETCHED = "2026-10-20T02:43:27Z"


def panel(name="Title Holder", status="Champion", *, image_alt=""):
    athlete = f'<a href="/athlete/{name.lower().replace(" ", "-")}">{name}</a>' if name != "Vacant" else name
    return f'<div class="rankings--athlete--champion"><div class="info"><h5>{athlete}</h5><h6>{status}</h6></div><img alt="{image_alt}"></div>'


def division(name, rows, *, meta=False, panels=None):
    rank_class = "views-field-meta-weight-class-rank" if meta else "views-field-weight-class-rank"
    body = ""
    for rank, fighter in rows:
        value = f'<a href="/athlete/{fighter.lower().replace(" ", "-")}">{fighter}</a>' if fighter != "Vacant" else fighter
        body += f'<tr><td class="views-field {rank_class}">{rank}</td><td class="views-field-title">{value}</td><td class="views-field-weight-class-rank-change">NR</td></tr>'
    return f'<div class="view-grouping"><div class="view-grouping-header">{name}</div><div class="view-grouping-content"><table><caption>{panel() if panels is None else panels}</caption><tbody>{body}</tbody></table></div></div>'


def page(*, media=None, meta=None, media_date="Tuesday, Oct. 06", meta_date="Saturday, Oct. 10"):
    media = division("Flyweight", [(1, "Media First"), (2, "Media Second")]) if media is None else media
    meta = division("Flyweight", [(1, "Meta First"), (2, "Meta Second")], meta=True) if meta is None else meta
    return ('<!doctype html><html><body><h1>Rankings</h1>'
            f'<div class="view-athlete-rankings view-display-id-block_1">{media}</div>'
            f'<div class="view-athlete-rankings view-display-id-meta_rankings">{meta}</div>'
            f'<p class="list-denotions__updated is-hidden" data-rankings-footer="media" hidden>Last\nupdated: {media_date}</p>'
            f'<p class="list-denotions__updated" data-rankings-footer="meta">Last updated: {meta_date}</p>'
            '</body></html>')


class RankingsTests(unittest.TestCase):
    def parse(self, html=None):
        return parse_rankings(page() if html is None else html, fetched_at_utc=FETCHED)

    def test_checked_in_current_snapshot_is_loadable_and_explicitly_not_historical_features(self):
        result = load_rankings(DEFAULT_SNAPSHOT)
        self.assertEqual(result["source"]["url"], "https://www.ufc.com/rankings")
        self.assertEqual(result["source"]["http_status"], 200)
        self.assertIn(result["default_ranking_set"], [s["id"] for s in result["ranking_sets"]])
        self.assertFalse(result["historical_model_feature_eligible"])

    def test_systems_and_publisher_update_dates_stay_separate(self):
        raw = page(meta_date="Saturday, October 10, 2026").encode()
        result = self.parse(raw)
        self.assertEqual(result["default_ranking_set"], "meta")
        media, meta = result["ranking_sets"]
        self.assertEqual(media["divisions"][0]["contenders"][0]["name"], "Media First")
        self.assertEqual(meta["divisions"][0]["contenders"][0]["name"], "Meta First")
        self.assertEqual(media["source_updated_label"], "Last updated: Tuesday, Oct. 06")
        self.assertIsNone(media["source_updated_date"])
        self.assertEqual(meta["source_updated_date"], "2026-10-10")
        self.assertEqual(result["source"]["raw_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["source"]["fetched_at_utc"], FETCHED)
        self.assertFalse(result["historical_model_feature_eligible"])

    def test_ties_in_both_systems_are_kept_without_inventing_skipped_positions(self):
        rows = [(1, "First"), (2, "Tied A"), (2, "Tied B"), (4, "Fourth")]
        result = self.parse(page(media=division("Flyweight", rows), meta=division("Flyweight", rows, meta=True)))
        for system in result["ranking_sets"]:
            d = system["divisions"][0]
            self.assertEqual([r["name"] for r in d["contenders"]], ["First", "Tied A", "Tied B", "Fourth"])
            self.assertTrue(d["contenders"][1]["tied"]); self.assertEqual(d["contenders"][1]["tie_size"], 2)
            self.assertEqual(d["missing_rank_reasons"]["3"], "published_tie_skips_rank")
            self.assertNotIn(3, d["unpublished_rank_slots"])
            self.assertEqual(d["unpublished_rank_slots"], list(range(5, 16)))

    def test_duplicate_athlete_invalid_rank_and_overlapping_tie_fail(self):
        for rows in ([(1, "Same"), (1, "Same")], [(1, "A"), (1, "B"), (2, "C")], [(16, "Bad")], [(2, "B"), (1, "A")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.parse(page(media=division("Flyweight", rows)))

    def test_champion_interim_vacancy_and_missing_champion_are_explicit(self):
        full = division("Heavyweight", [(1, "First")], panels=panel("Full Holder") + panel("Interim Holder", "Interim Champion"))
        vacant = division("Lightweight", [(1, "Vacant"), (2, "Second")], panels=panel("Vacant"))
        unavailable = division("Women's Flyweight", [], panels="")
        result = self.parse(page(media=full + vacant + unavailable))
        a, b, c = result["ranking_sets"][0]["divisions"]
        self.assertEqual(a["championship_status"], "champion_and_interim")
        self.assertEqual([r["status"] for r in a["champions"]], ["champion", "interim_champion"])
        self.assertEqual(b["championship_status"], "vacant"); self.assertEqual(b["champions"], [])
        self.assertEqual(b["missing_rank_reasons"]["1"], "explicit_published_vacancy")
        self.assertEqual(c["championship_status"], "not_published"); self.assertTrue(c["empty_reason"])
        self.assertEqual(c["contenders"], []); self.assertEqual(c["missing_ranks"], list(range(1, 16)))
        interim_only = self.parse(page(media=division("Heavyweight", [(1, "First")], panels=panel("Vacant") + panel("Interim Holder", "Interim Champion"))))
        self.assertEqual(interim_only["ranking_sets"][0]["divisions"][0]["championship_status"], "vacant_with_interim_champion")

    def test_pound_for_pound_caption_is_a_leader_and_never_a_champion(self):
        html = page(media=division("Men's Pound-for-Pound <span>Top Rank</span>", [(1, "Leader")], panels=panel("Leader", "")))
        result = self.parse(html)["ranking_sets"][0]["divisions"][0]
        self.assertEqual(result["kind"], "pound_for_pound"); self.assertEqual(result["leader"]["name"], "Leader")
        self.assertEqual(result["champions"], []); self.assertEqual(result["championship_status"], "not_applicable")

    def test_source_title_label_retained_when_legacy_image_alt_says_interim(self):
        html = page(media=division("Heavyweight", [(1, "First")], panels=panel("Holder", image_alt="UFC interim heavyweight champion")))
        result = self.parse(html)["ranking_sets"][0]["divisions"][0]
        self.assertEqual(result["champions"][0]["status"], "champion")
        self.assertIn("portrait alt", result["data_quality_notes"][0])

    def test_duplicate_divisions_unknown_structures_and_challenge_pages_fail(self):
        examples = [page(media=division("Flyweight", [(1, "A")]) * 2), "<html>Access Denied</html>",
                    page().replace("view-display-id-meta_rankings", "view-display-id-unknown"),
                    page().replace('/athlete/meta-first', 'https://untrusted.example/athlete/meta-first')]
        for html in examples:
            with self.subTest(html=html[:60]), self.assertRaises(ValueError): self.parse(html)

    def test_load_rejects_tampering_and_post_cutoff_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rankings.json"; document = self.parse(); path.write_text(json.dumps(document))
            self.assertEqual(load_rankings(path, as_of="2026-10-20"), document)
            self.assertEqual(load_rankings(path, as_of=datetime(2026, 10, 20, 3, tzinfo=timezone.utc)), document)
            with self.assertRaisesRegex(ValueError, "after"): load_rankings(path, as_of="2026-10-19")
            with self.assertRaisesRegex(ValueError, "timezone"): load_rankings(path, as_of=datetime(2026, 10, 21))
            document["ranking_sets"][0]["divisions"][0]["contenders"][0]["name"] = "Changed"
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "digest"): load_rankings(path)

    def test_fetch_archives_actual_response_and_failure_preserves_previous_capture(self):
        def curl(arguments, **kwargs):
            Path(arguments[arguments.index("--output") + 1]).write_text(page())
            Path(arguments[arguments.index("--dump-header") + 1]).write_bytes(b"HTTP/2 200\r\ncontent-type: text/html\r\n")
            return SimpleNamespace(returncode=0, stdout="200\nhttps://www.ufc.com/rankings", stderr="")
        with tempfile.TemporaryDirectory() as directory, patch("ufc_odds_model.rankings.subprocess.run", side_effect=curl):
            result = fetch_rankings(raw_directory=directory)
            capture = Path(directory) / result["source"]["raw_archive"]["capture_id"]
            self.assertEqual((capture / "page.html").read_text(), page())
            self.assertEqual(result["source"]["access_mode"], "direct_public_https_curl")
            before = {p: p.read_bytes() for p in capture.iterdir()}
            with patch("ufc_odds_model.rankings.subprocess.run", return_value=SimpleNamespace(returncode=22, stdout="403", stderr="blocked")):
                with self.assertRaisesRegex(ValueError, "preserved"): fetch_rankings(raw_directory=directory)
            self.assertEqual(before, {p: p.read_bytes() for p in capture.iterdir()})


if __name__ == "__main__":
    unittest.main()
