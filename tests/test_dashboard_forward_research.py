"""Saved UFC 332 forward scenarios stay separate and fail closed on stale proof."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.dashboard_data import (
    _forward_digest, load_ufc332_forward_research,
)


CAPTURE = "2026-09-26T22:47:55Z"


def _saved(path: Path, body: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _json(path: Path, value: object) -> str:
    return _saved(path, json.dumps(value, sort_keys=True).encode("utf-8"))


class DashboardForwardResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.strict_path = self.root / "reports/strict.json"
        self.full_path = self.root / "reports/full.json"
        self.card_source = self.root / "data/raw/ufc332-intake/source.json"
        self._build_fixture()

    def _build_fixture(self) -> None:
        source_sha = _saved(self.card_source, b"saved card source")
        lookup_sha = _saved(self.root / "data/raw/ufc332-intake/lookup.json",
                            b"saved fighter lookup")
        odds_sha = _saved(self.root / "data/raw/ufc332-odds-intake/response.json",
                          b"saved odds response")
        odds_manifest = {
            "captured_at_utc": CAPTURE,
            "raw_path": "data/raw/ufc332-odds-intake/response.json",
            "sha256": odds_sha,
        }
        odds_manifest_sha = _json(
            self.root / "data/raw/ufc332-odds-intake/intake_manifest.json",
            odds_manifest,
        )
        source_rows = []
        self.books = [{
            "bookmaker": "fixture-book", "captured_at_utc": CAPTURE,
            "executable_price_verified": False,
            "fighter_a_no_vig_implied_probability": 0.5,
        }]
        self.strict_bouts = []
        self.full_forecasts = []
        for position in range(1, 14):
            a_id = f"wikipedia:{100 + position}" if position <= 8 else None
            b_id = f"wikipedia:{200 + position}"
            a_name, b_name = f"Fighter A{position}", f"Fighter B{position}"
            source_rows.append({
                "position": position,
                "fighter_a": {"name": a_name, "stable_id": a_id},
                "fighter_b": {"name": b_name, "stable_id": b_id},
            })
            books = self.books if position <= 4 else []
            self.strict_bouts.append({
                "source_position": position,
                "fighter_a": a_name, "fighter_b": b_name,
                "fighter_a_id": a_id, "fighter_b_id": b_id,
                "selected_for_replay": position <= 8,
                "forecast_status": "exploratory_elo_only" if position <= 8 else "identity_hold",
                "elo_fighter_a_probability": 0.48 if position <= 8 else None,
                "fighter_a_prior_result_bouts_in_cohort": 1,
                "fighter_b_prior_result_bouts_in_cohort": 2,
                "saved_two_sided_books": books,
                "alert_eligible": False,
            })
            if position <= 8:
                self.full_forecasts.append({
                    "position": position,
                    "fighter_a_name": a_name, "fighter_b_name": b_name,
                    "fighter_a_id": a_id, "fighter_b_id": b_id,
                    "elo_probability_fighter_a": 0.55,
                    "logistic_raw_probability_fighter_a": 0.58,
                    "logistic_calibrated_probability_fighter_a": 0.57,
                    "prior_binary_or_draw_bouts_fighter_a": 10,
                    "prior_binary_or_draw_bouts_fighter_b": 5,
                    "saved_two_sided_books": books,
                    "alert_eligible": False,
                })
        card_manifest = {
            "captured_at_utc": "2026-09-26T22:47:45Z",
            "event": {
                "source_page_id": 83826247, "event_date": "2026-10-03",
                "name": "UFC 332 Fixture",
                "source_revision_url": "https://en.wikipedia.org/w/index.php?oldid=10",
            },
            "source": {"raw_path": "data/raw/ufc332-intake/source.json",
                       "raw_sha256": source_sha},
            "fighter_lookup": {"raw_path": "data/raw/ufc332-intake/lookup.json",
                               "raw_sha256": lookup_sha},
            "odds_coverage_comparison": {"source_manifest_sha256": odds_manifest_sha},
            "official_start_review": {
                "event_start_utc_for_conservative_cutoff": "2026-10-03T20:00:00Z"},
            "fight_card_rows": source_rows,
        }
        card_manifest_sha = _json(
            self.root / "data/raw/ufc332-intake/manifest.json", card_manifest,
        )
        csv_path = self.root / "data/raw/ufc332-intake/reviewed-card-template.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=(
                "source_position", "fighter_a_id", "fighter_a_name",
                "fighter_b_id", "fighter_b_name",
            ))
            writer.writeheader()
            for row in source_rows[:8]:
                writer.writerow({
                    "source_position": row["position"],
                    "fighter_a_id": row["fighter_a"]["stable_id"],
                    "fighter_a_name": row["fighter_a"]["name"],
                    "fighter_b_id": row["fighter_b"]["stable_id"],
                    "fighter_b_name": row["fighter_b"]["name"],
                })
        csv_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        db_sha = _saved(self.root / "data/ufc_research_2011_2025.sqlite", b"read-only DB")
        holdout_sha = _json(self.root / "reports/ufc_research_1993_2026_holdout.json", {
            "research_only": True, "promotion_eligible": False,
            "source_summary": {"accepted_events_with_results": 790,
                               "accepted_bouts_with_results": 7258},
        })
        historical_manifest_sha = _json(
            self.root / "docs/HISTORICAL_2026_PILOT_CARDS.json", {"events": []},
        )
        strict_inputs = {
            "card_manifest_sha256": card_manifest_sha,
            "card_source_sha256": source_sha,
            "card_fighter_lookup_sha256": lookup_sha,
            "selected_card_csv_sha256": csv_sha,
            "odds_manifest_sha256": odds_manifest_sha,
            "odds_response_sha256": odds_sha,
            "research_database_sha256": db_sha,
            "historical_manifest_sha256": historical_manifest_sha,
            "historical_result_proofs": [],
            "historical_identity_lookup_receipts": [],
            "exact_cutoff_proof_failures": [],
        }
        self.strict = {
            "schema_version": 1, "replay_version": "archived-forward-research-v1",
            "status": "research_only", "research_only": True,
            "model_eligible": False, "promotion_eligible": False,
            "alert_eligible": False, "paper_decisions_created": 0, "bets_placed": 0,
            "database_access": "read_only",
            "event_id": "wikipedia_pilot:83826247", "event_date": "2026-10-03",
            "event_name": "UFC 332 Fixture",
            "event_start_at_utc": "2026-10-03T20:00:00Z",
            "card_captured_at_utc": "2026-09-26T22:47:45Z",
            "history_cutoff_at_utc": CAPTURE, "odds_captured_at_utc": CAPTURE,
            "historical_result_selection_rule":
                "latest_publisher_result_revision_at_exact_odds_capture_for_every_prior_event",
            "card_source_revision_url": card_manifest["event"]["source_revision_url"],
            "card_license_url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en",
            "checked_inputs": strict_inputs,
            "checked_input_sha256": _forward_digest(strict_inputs),
            "bouts": self.strict_bouts,
            "forecast_rows_sha256": _forward_digest(self.strict_bouts),
            "holds": [],
            "coverage": {
                "source_card_bouts": 13, "stable_id_eligible_card_bouts": 8,
                "selected_card_bouts": 8, "historical_events_required": 23,
                "historical_events_verified": 23,
                "selected_bouts_with_exploratory_elo": 8,
                "selected_bouts_with_two_sided_saved_books": 4,
                "historical_source_result_bouts": 287,
                "historical_stable_id_result_bouts": 118,
            },
        }
        source_summary = {"accepted_events_with_results": 790,
                          "accepted_bouts_with_results": 7258}
        catalog = {"catalog_sha256": "c" * 64, "ingestion_runs": 2837}
        self.full = {
            "schema_version": 1, "version": "captured-forward-research-v1",
            "scenario": "captured_full_result_history",
            "status": "research_only", "research_only": True,
            "promotion_eligible": False, "alert_eligible": False,
            "event_id": "wikipedia_pilot:83826247",
            "target_event_date": "2026-10-03", "cutoff_at_utc": CAPTURE,
            "event_name": "UFC 332 Fixture",
            "event_start_at_utc": "2026-10-03T20:00:00Z",
            "card_captured_at_utc": "2026-09-26T22:47:45Z",
            "odds_captured_at_utc": CAPTURE,
            "card_source_revision_url": card_manifest["event"]["source_revision_url"],
            "card_manifest_sha256": card_manifest_sha,
            "card_source_sha256": source_sha,
            "card_fighter_lookup_sha256": lookup_sha,
            "selected_card_csv_sha256": csv_sha,
            "odds_manifest_sha256": odds_manifest_sha,
            "odds_response_sha256": odds_sha,
            "database_path": str(self.root / "data/ufc_research_2011_2025.sqlite"),
            "database_sha256": db_sha,
            "holdout_report_path": str(
                self.root / "reports/ufc_research_1993_2026_holdout.json"),
            "holdout_report_sha256": holdout_sha,
            "receipt_catalog": catalog, "integrity_verified_receipts": 2837,
            "source_summary": source_summary,
            "model": {"logistic_training_bouts": 5272,
                      "calibration_validation_bouts": 1054,
                      "historical_test_bouts": 800},
            "forecasts": self.full_forecasts,
            "forecast_rows_sha256": _forward_digest(self.full_forecasts),
            "source_card_bouts": 13, "selected_card_bouts": 8,
            "source_identity_hold_positions": [9, 10, 11, 12, 13],
            "selected_bouts_with_two_sided_saved_books": 4,
        }
        self._seal_full()
        self._save_reports()

    def _seal_full(self) -> None:
        pairs = [{"position": row["position"], "fighter_a_id": row["fighter_a_id"],
                  "fighter_b_id": row["fighter_b_id"]} for row in self.full["forecasts"]]
        inner = _forward_digest({
            "cutoff_at_utc": self.full["cutoff_at_utc"],
            "target_event_date": self.full["target_event_date"],
            "pairs": pairs, "database_sha256": self.full["database_sha256"],
            "holdout_report_sha256": self.full["holdout_report_sha256"],
            "receipt_catalog_sha256": self.full["receipt_catalog"]["catalog_sha256"],
            "source_summary": self.full["source_summary"],
        })
        self.full["checked_input_sha256"] = _forward_digest({
            "history_checked_input_sha256": inner,
            **{key: self.full[key] for key in (
                "card_manifest_sha256", "card_source_sha256",
                "card_fighter_lookup_sha256", "selected_card_csv_sha256",
                "odds_manifest_sha256", "odds_response_sha256")},
        })

    def _save_reports(self) -> None:
        _json(self.strict_path, self.strict)
        _json(self.full_path, self.full)

    def _load(self) -> dict:
        with patch("ufc_odds_model.dashboard_data._forward_strict_proofs"), patch(
            "ufc_odds_model.dashboard_data._forward_full_history"):
            return load_ufc332_forward_research(
                self.strict_path, self.full_path, evidence_root=self.root,
            )

    def test_eight_research_rows_are_separate_and_source_bound(self) -> None:
        before = (self.root / "data/ufc_research_2011_2025.sqlite").read_bytes()
        view = self._load()
        self.assertEqual(view["status"], "available")
        self.assertEqual(len(view["rows"]), 8)
        self.assertEqual(view["selected_bouts_with_two_sided_saved_books"], 4)
        self.assertEqual(view["identity_hold_positions"], [9, 10, 11, 12, 13])
        self.assertTrue(view["research_only"])
        self.assertFalse(view["promotion_eligible"])
        self.assertFalse(view["alert_eligible"])
        self.assertNotIn("upcoming_events", view)
        self.assertEqual((self.root / "data/ufc_research_2011_2025.sqlite").read_bytes(), before)

    def test_changed_probability_or_input_digest_never_displays(self) -> None:
        self.strict["bouts"][0]["elo_fighter_a_probability"] = 0.9
        self._save_reports()
        self.assertEqual(self._load()["status"], "invalid_report")
        self.strict["forecast_rows_sha256"] = _forward_digest(self.strict["bouts"])
        self.strict["checked_input_sha256"] = "0" * 64
        self._save_reports()
        self.assertEqual(self._load()["status"], "invalid_report")

    def test_operating_promotion_or_wrong_cutoff_never_displays(self) -> None:
        self.full["promotion_eligible"] = True
        self._save_reports()
        self.assertEqual(self._load()["status"], "invalid_report")
        self.full["promotion_eligible"] = False
        self.full["cutoff_at_utc"] = "2026-09-26T23:00:00Z"
        self._save_reports()
        self.assertEqual(self._load()["status"], "invalid_report")

    def test_changed_card_source_is_stale_evidence(self) -> None:
        self.card_source.write_bytes(b"changed card source")
        view = self._load()
        self.assertEqual(view["status"], "stale_evidence")
        self.assertNotIn("rows", view)

    def test_missing_report_is_unavailable(self) -> None:
        self.full_path.unlink()
        self.assertEqual(self._load()["status"], "missing_report")


if __name__ == "__main__":
    unittest.main()
