"""Offline checks for the read-only Wikidata identity candidate worksheet."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ufc_odds_model.wikidata_identity_candidates import build_candidate_worksheet, main


def _response(*rows: tuple[str, str, str]) -> bytes:
    return json.dumps({
        "head": {"vars": ["item", "ufcId", "itemLabel"]},
        "results": {"bindings": [{
            "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"},
            "ufcId": {"type": "literal", "value": athlete_id},
            "itemLabel": {"type": "literal", "value": label},
        } for qid, athlete_id, label in rows]},
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _occurrence(name: str, event: int, position: int) -> dict:
    return {
        "fighter_name": name,
        "event_id": f"wikipedia_research:{event}",
        "event_title": f"Event {event}",
        "bout_position": position,
        "source_url": f"https://en.wikipedia.org/wiki/Event_{event}",
        "source_revision_url": f"https://en.wikipedia.org/w/index.php?oldid={event + 1000}",
        "source_revision_id": event + 1000,
        "source_receipt_sha256": "a" * 64,
    }


def _review(*fighters: tuple[str, list[dict]]) -> bytes:
    return json.dumps({
        "schema_version": 1,
        "source": "wikipedia_research",
        "review_required": True,
        "fighters": [{
            "fighter_name": name,
            "occurrence_count": len(occurrences),
            "occurrences": occurrences,
        } for name, occurrences in fighters],
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _build(review: bytes, response: bytes) -> dict:
    return build_candidate_worksheet(
        review, response, review_path="review.json", response_path="response.json",
        built_at_utc="2026-09-26T00:00:00Z",
    )


class WikidataIdentityCandidatesTest(unittest.TestCase):
    def test_counts_distinct_bout_positions_without_approving_names(self) -> None:
        review = _review(
            ("João Silva", [_occurrence("João Silva", 1, 2), _occurrence("João Silva", 2, 1)]),
            ("Morgan", [_occurrence("Morgan", 1, 2)]),
            ("Missing Fighter", [_occurrence("Missing Fighter", 3, 4)]),
        )
        response = _response(("Q101", "joao-silva", "JOÃO SILVA"), ("Q102", "morgan", "Morgan"))
        result = _build(review, response)
        summary = result["summary"]
        self.assertEqual(summary["matched_held_name_keys"], 2)
        self.assertEqual(summary["matched_held_occurrences"], 3)
        self.assertEqual(summary["distinct_held_bout_positions_with_a_candidate"], 2)
        self.assertEqual(summary["unmatched_held_name_keys"], 1)
        self.assertEqual(result["automatically_resolved_bouts"], 0)
        self.assertTrue(result["candidate_only"])
        self.assertEqual(result["wikidata"]["response_sha256"], hashlib.sha256(response).hexdigest())
        self.assertEqual(result["candidates"][0]["occurrences"][0]["source_receipt_sha256"], "a" * 64)

    def test_duplicate_qids_and_labels_are_flagged(self) -> None:
        review = _review(("Alex Lee", [_occurrence("Alex Lee", 1, 1)]))
        response = _response(
            ("Q201", "alex-lee", "Alex Lee"),
            ("Q201", "alex-lee-alt", "Alex Lee"),
            ("Q202", "another-alex-lee", "ALEX LEE"),
        )
        result = _build(review, response)
        self.assertEqual(result["summary"]["wikidata_qids_with_duplicate_rows"], 1)
        self.assertEqual(result["summary"]["wikidata_labels_with_multiple_qids"], 1)
        self.assertEqual(result["source_anomalies"]["repeated_qids"][0]["ufc_athlete_ids"], [
            "alex-lee", "alex-lee-alt",
        ])
        self.assertEqual(result["source_anomalies"]["labels_shared_by_qids"][0]["qids"], [
            "Q201", "Q202",
        ])
        candidate = result["candidates"][0]
        self.assertTrue(candidate["multiple_candidate_qids"])
        self.assertEqual(len(candidate["wikidata_candidates"]), 3)
        self.assertTrue(all(row["duplicate_label_across_qids"] for row in candidate["wikidata_candidates"]))
        self.assertEqual(sum(row["duplicate_qid_in_response"] for row in candidate["wikidata_candidates"]), 2)

    def test_no_match_and_punctuation_do_not_create_aliases(self) -> None:
        review = _review(("Da'Mon Blackshear", [_occurrence("Da'Mon Blackshear", 1, 1)]))
        result = _build(review, _response(("Q301", "damon-blackshear", "Damon Blackshear")))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["summary"]["matched_held_name_keys"], 0)

    def test_duplicate_printed_names_remain_visible_for_review(self) -> None:
        review = _review(
            ("MarQuel Mederos", [_occurrence("MarQuel Mederos", 1, 1)]),
            ("Marquel Mederos", [_occurrence("Marquel Mederos", 2, 1)]),
        )
        result = _build(review, _response(("Q350", "marquel-mederos", "Marquel Mederos")))
        self.assertEqual(result["summary"]["held_name_keys_with_duplicate_entries"], 1)
        self.assertEqual(result["summary"]["matched_held_name_keys"], 1)
        self.assertEqual(result["candidates"][0]["fighter_names_in_review"], [
            "MarQuel Mederos", "Marquel Mederos",
        ])
        self.assertTrue(result["candidates"][0]["duplicate_casefold_name_in_review"])
        self.assertEqual(len(result["input_review_anomalies"]["casefold_duplicate_name_entries"]), 1)

    def test_offline_replay_retains_exact_response_bytes(self) -> None:
        review = _review(("Sam Patterson", [_occurrence("Sam Patterson", 4, 6)]))
        response = _response(("Q401", "sam-patterson", "Sam Patterson"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "review.json"
            response_path = root / "response.json"
            output_path = root / "candidates.json"
            raw_dir = root / "raw"
            review_path.write_bytes(review)
            response_path.write_bytes(response)
            self.assertEqual(main([
                "--review", str(review_path), "--response-file", str(response_path),
                "--raw-dir", str(raw_dir), "--output", str(output_path),
            ]), 0)
            worksheet = json.loads(output_path.read_text(encoding="utf-8"))
            retained = Path(worksheet["wikidata"]["response_path"])
            self.assertEqual(retained.read_bytes(), response)
            self.assertEqual(retained.name, hashlib.sha256(response).hexdigest() + ".json")
            self.assertEqual(worksheet["summary"]["matched_held_name_keys"], 1)
            self.assertEqual(review_path.read_bytes(), review)


if __name__ == "__main__":
    unittest.main()
