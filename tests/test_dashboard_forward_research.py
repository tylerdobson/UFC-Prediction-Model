"""Saved UFC 332 forward scenarios stay separate and fail closed on stale proof."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.dashboard_data import (
    _forward_digest, _forward_file, _forward_strict_proofs,
    load_ufc332_forward_research,
)
from ufc_odds_model import db


CAPTURE = "2026-09-26T22:47:55Z"
FRESH_CAPTURE = "2026-09-27T16:01:50Z"
FRESH_CARD_CAPTURE = "2026-09-27T15:56:16Z"


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

    def _make_fresh_fixture(self) -> None:
        """Move evidence to a second sealed snapshot with capture receipts."""
        fresh = self.root / "data/raw/ufc332-fresh"
        fresh.mkdir(parents=True)
        old_card = json.loads((self.root / "data/raw/ufc332-intake/manifest.json").read_text())
        old_odds = json.loads((self.root / "data/raw/ufc332-odds-intake/intake_manifest.json").read_text())
        old_odds_bytes = (self.root / old_odds["raw_path"]).read_bytes()
        _saved(fresh / "odds.response.json", old_odds_bytes)
        odds_receipt = {
            "schema_version": 1, "provider": "the-odds-api", "sport_key": "mma_mixed_martial_arts",
            "market": "h2h", "region": "us", "response_sha256": old_odds["sha256"],
            "fetched_at_utc": FRESH_CAPTURE,
        }
        old_odds.update({
            "captured_at_utc": FRESH_CAPTURE, "raw_path": "odds.response.json",
            "receipt_path": "odds.receipt.json", "receipt_sha256": _json(
                fresh / "odds.receipt.json", odds_receipt), "region": "us",
        })
        odds_manifest_sha = _json(fresh / "odds-intake-manifest.json", old_odds)
        old_card["captured_at_utc"] = FRESH_CARD_CAPTURE
        for key, url, receipt_name in (
            ("source", "https://en.wikipedia.org/w/rest.php/v1/page/UFC_332", "source.receipt.json"),
            ("fighter_lookup", "https://en.wikipedia.org/w/api.php?action=query&titles=A", "lookup.receipt.json"),
        ):
            item = old_card[key]
            receipt = {
                "schema_version": 1, "request_url": url,
                "response_sha256": item["raw_sha256"],
                "fetched_at_utc": FRESH_CARD_CAPTURE,
            }
            item.update({
                "api_url": url, "fetched_at_utc": FRESH_CARD_CAPTURE,
                "receipt_path": str((fresh / receipt_name).relative_to(self.root)),
                "receipt_sha256": _json(fresh / receipt_name, receipt),
            })
        old_card["odds_coverage_comparison"]["source_manifest_sha256"] = odds_manifest_sha
        card_sha = _json(fresh / "card-with-odds-manifest.json", old_card)
        csv_bytes = (self.root / "data/raw/ufc332-intake/reviewed-card-template.csv").read_bytes()
        csv_sha = _saved(fresh / "reviewed-card-template.csv", csv_bytes)
        self.evidence_files = {
            "card_manifest": "data/raw/ufc332-fresh/card-with-odds-manifest.json",
            "odds_manifest": "data/raw/ufc332-fresh/odds-intake-manifest.json",
            "card_csv": "data/raw/ufc332-fresh/reviewed-card-template.csv",
        }
        inputs = self.strict["checked_inputs"]
        inputs.update({
            "card_manifest_sha256": card_sha, "odds_manifest_sha256": odds_manifest_sha,
            "selected_card_csv_sha256": csv_sha,
        })
        self.strict.update({
            "card_captured_at_utc": FRESH_CARD_CAPTURE,
            "history_cutoff_at_utc": FRESH_CAPTURE,
            "odds_captured_at_utc": FRESH_CAPTURE,
            "checked_input_sha256": _forward_digest(inputs),
        })
        self.full.update({
            "card_captured_at_utc": FRESH_CARD_CAPTURE,
            "cutoff_at_utc": FRESH_CAPTURE,
            "odds_captured_at_utc": FRESH_CAPTURE,
            "card_manifest_sha256": card_sha,
            "odds_manifest_sha256": odds_manifest_sha,
            "selected_card_csv_sha256": csv_sha,
        })
        for row in self.strict_bouts:
            for book in row["saved_two_sided_books"]:
                book["captured_at_utc"] = FRESH_CAPTURE
        self.strict["forecast_rows_sha256"] = _forward_digest(self.strict_bouts)
        self.full["forecast_rows_sha256"] = _forward_digest(self.full_forecasts)
        self._seal_full()
        self._save_reports()

    def _make_relocated_fixture(self) -> Path:
        """Copy a signed fixture with original absolute paths and a real DB."""
        self._make_fresh_fixture()
        card_path = self.root / self.evidence_files["card_manifest"]
        card = json.loads(card_path.read_text(encoding="utf-8"))
        for key in ("source", "fighter_lookup"):
            info = card[key]
            for field in ("raw_path", "receipt_path"):
                info[field] = str(self.root / info[field])
        card_sha = _json(card_path, card)
        self.strict["checked_inputs"]["card_manifest_sha256"] = card_sha
        self.full["card_manifest_sha256"] = card_sha

        database = self.root / "data/ufc_research_2011_2025.sqlite"
        database.unlink()
        payload = self.root / "data/raw/research-proof.json"
        payload_sha = _saved(payload, b"immutable research response")
        fetched = "2025-01-01T00:00:00Z"
        with db.connect(database) as connection:
            db.init_db(connection)
            run_id = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)", ("fixture", fetched, str(payload), payload_sha),
            ).lastrowid
            connection.commit()
        database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
        self.strict["checked_inputs"]["research_database_sha256"] = database_sha
        self.full["database_sha256"] = database_sha
        catalog_line = json.dumps([run_id, "fixture", fetched, payload_sha],
                                  separators=(",", ":")).encode() + b"\n"
        self.full["receipt_catalog"] = {
            "catalog_sha256": hashlib.sha256(catalog_line).hexdigest(),
            "ingestion_runs": 1, "by_source": {"fixture": 1},
            "latest_fetch_at_utc": fetched,
        }
        self.full["integrity_verified_receipts"] = 1
        self.strict["checked_input_sha256"] = _forward_digest(self.strict["checked_inputs"])
        self._seal_full()
        self._save_reports()

        relocated_temp = tempfile.TemporaryDirectory()
        self.addCleanup(relocated_temp.cleanup)
        moved = Path(relocated_temp.name) / "checkout"
        shutil.copytree(self.root, moved)
        return moved

    def _load_relocated(self, moved: Path, original: Path | None = None) -> dict:
        with patch("ufc_odds_model.dashboard_data._forward_strict_proofs"):
            return load_ufc332_forward_research(
                self.strict_path, self.full_path, evidence_root=moved,
                evidence_files=self.evidence_files,
                original_evidence_root=self.root if original is None else original,
            )

    def _load(self) -> dict:
        with patch("ufc_odds_model.dashboard_data._forward_strict_proofs"), patch(
            "ufc_odds_model.dashboard_data._forward_full_history"):
            return load_ufc332_forward_research(
                self.strict_path, self.full_path, evidence_root=self.root,
                evidence_files=getattr(self, "evidence_files", None),
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

    def test_fresh_snapshot_uses_exact_cutoff_and_receipts(self) -> None:
        self._make_fresh_fixture()
        view = self._load()
        self.assertEqual(view["status"], "available")
        self.assertEqual(view["cutoff_at_utc"], FRESH_CAPTURE)
        self.assertTrue(view["capture_receipts_verified"])
        self.assertEqual(view["fighter_lookup_fetched_at_utc"], FRESH_CARD_CAPTURE)

    def test_fresh_snapshot_fails_closed_on_receipt_or_cutoff_change(self) -> None:
        self._make_fresh_fixture()
        receipt = self.root / "data/raw/ufc332-fresh/lookup.receipt.json"
        receipt.write_text('{"modified":true}')
        self.assertEqual(self._load()["status"], "stale_evidence")
        self.strict["history_cutoff_at_utc"] = "2026-09-27T16:02:00Z"
        self._save_reports()
        self.assertEqual(self._load()["status"], "invalid_report")

    def test_relocated_snapshot_keeps_signed_bytes_and_verifies_db_receipts(self) -> None:
        moved = self._make_relocated_fixture()
        view = self._load_relocated(moved)
        self.assertEqual(view["status"], "available")
        self.assertTrue(view["capture_receipts_verified"])
        self.assertFalse(view["alert_eligible"])
        self.assertEqual(view["cutoff_at_utc"], FRESH_CAPTURE)
        without_map = load_ufc332_forward_research(
            moved / "reports/strict.json", moved / "reports/full.json",
            evidence_root=moved, evidence_files=self.evidence_files,
        )
        self.assertEqual(without_map["status"], "stale_evidence")

    def test_relocated_report_paths_allow_alias_above_the_new_root(self) -> None:
        moved = self._make_relocated_fixture()
        alias_parent = moved.parent / "alias-parent"
        alias_parent.symlink_to(moved.parent, target_is_directory=True)
        alias_root = alias_parent / moved.name
        with patch("ufc_odds_model.dashboard_data._forward_strict_proofs"):
            view = load_ufc332_forward_research(
                alias_root / "reports/strict.json",
                alias_root / "reports/full.json",
                evidence_root=alias_root, evidence_files=self.evidence_files,
                original_evidence_root=self.root,
            )
        self.assertEqual(view["status"], "available")

    def test_relocated_snapshot_rejects_tampered_or_linked_payloads(self) -> None:
        moved = self._make_relocated_fixture()
        payload = moved / "data/raw/research-proof.json"
        payload.write_bytes(b"changed payload")
        self.assertEqual(self._load_relocated(moved)["status"], "stale_evidence")
        payload.unlink()
        payload.symlink_to(self.root / "data/raw/research-proof.json")
        self.assertEqual(self._load_relocated(moved)["status"], "stale_evidence")

        payload.unlink()
        _saved(payload, b"immutable research response")
        intake = moved / "data/raw/ufc332-intake"
        shutil.rmtree(intake)
        intake.symlink_to(self.root / "data/raw/ufc332-intake", target_is_directory=True)
        self.assertEqual(self._load_relocated(moved)["status"], "stale_evidence")

    def test_relocated_strict_result_sidecars_and_identity_lookup_paths(self) -> None:
        moved = self._make_relocated_fixture().resolve()
        events: list[dict] = []
        proofs: list[dict] = []
        marker = FRESH_CAPTURE.replace(":", "").replace("-", "")
        for position in range(1, 24):
            event = {"slug": f"event-{position}", "page_id": 1000 + position}
            events.append(event)
            proof = {
                **event,
                "cutoff_at_utc": FRESH_CAPTURE,
                "revision_timestamp_utc": "2025-01-01T00:00:00Z",
            }
            for kind in ("selection", "content"):
                prefix = (f"data/raw/historical-revisions/{event['slug']}/"
                          f"result-{marker}.{kind}")
                response_relative = Path(prefix + ".response.json")
                response_sha = _saved(moved / response_relative, b"{}")
                receipt = {
                    "page_id": event["page_id"],
                    "cutoff_utc": FRESH_CAPTURE,
                    "role": "result",
                    "response_kind": kind,
                    "response_sha256": response_sha,
                    "response_path": str(self.root / response_relative),
                    "fetched_at_utc": "2026-09-27T16:02:00Z",
                }
                receipt_sha = _json(moved / (prefix + ".receipt.json"), receipt)
                proof[f"{kind}_sha256"] = response_sha
                proof[f"{kind}_sidecar_sha256"] = receipt_sha
                proof[f"{kind}_fetched_at_utc"] = receipt["fetched_at_utc"]
            proofs.append(proof)
        manifest = {"events": events}
        manifest_relative = "docs/HISTORICAL_2026_PILOT_CARDS.json"
        manifest_sha = _json(moved / manifest_relative, manifest)
        lookup_relative = Path("data/raw/historical-identity.json")
        lookup_sha = _saved(moved / lookup_relative, b"lookup bytes")
        inputs = self.strict["checked_inputs"]
        inputs["historical_manifest_sha256"] = manifest_sha
        inputs["historical_result_proofs"] = proofs
        inputs["historical_identity_lookup_receipts"] = [{
            "run_id": 1,
            "source": "wikipedia_action_api",
            "fetched_at_utc": "2025-01-01T00:00:00Z",
            "payload_path": str(self.root / lookup_relative),
            "payload_sha256": lookup_sha,
        }]
        files = {"historical_manifest": manifest_relative}
        _forward_strict_proofs(moved, self.strict, manifest, files,
                               FRESH_CAPTURE, self.root)

        receipt_path = moved / (f"data/raw/historical-revisions/event-1/"
                                f"result-{marker}.selection.receipt.json")
        receipt_path.write_bytes(b"tampered receipt")
        with self.assertRaises(ValueError) as raised:
            _forward_strict_proofs(moved, self.strict, manifest, files,
                                   FRESH_CAPTURE, self.root)
        self.assertEqual(raised.exception.status, "stale_evidence")

    def test_relocated_snapshot_rejects_wrong_original_root_and_outside_paths(self) -> None:
        moved = self._make_relocated_fixture()
        self.assertEqual(self._load_relocated(moved, self.root / "wrong")["status"],
                         "stale_evidence")
        outside = moved.parent / "outside.txt"
        outside.write_bytes(b"same")
        with self.assertRaises(ValueError) as raised:
            _forward_file(moved, outside, hashlib.sha256(b"same").hexdigest(), self.root)
        self.assertEqual(raised.exception.status, "stale_evidence")



if __name__ == "__main__":
    unittest.main()
