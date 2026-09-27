"""Small runtime smoke checks for the optional Streamlit dashboard."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db, demo


APP = Path(__file__).resolve().parents[1] / "app.py"
HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None


@unittest.skipUnless(HAS_STREAMLIT, "install the optional dashboard extra for runtime UI checks")
class DashboardAppSmokeTests(unittest.TestCase):
    def _run_app(self, db_path: Path, report_path: Path):
        from streamlit.testing.v1 import AppTest

        with patch.dict(os.environ, {
            "UFC_MODEL_DB": str(db_path),
            "UFC_MODEL_EVALUATION_REPORT": str(report_path),
        }):
            return AppTest.from_file(str(APP), default_timeout=15).run()

    @staticmethod
    def _all_markup(app) -> str:
        return "\n".join(item.value for item in app.markdown)

    def test_missing_database_renders_all_views_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self._run_app(Path(temporary) / "missing.sqlite", Path(temporary) / "missing.json")
        self.assertEqual(list(app.exception), [])
        self.assertEqual([tab.label for tab in app.tabs], [
            "Upcoming card", "Historical data", "Model evidence", "Data quality", "Ledgers",
        ])
        markup = self._all_markup(app)
        self.assertIn("No verified upcoming card", markup)
        self.assertIn("Evaluation unavailable", markup)
        self.assertIn("No paper decisions recorded", markup)
        self.assertIn("No manually entered wagers", markup)

    def test_saved_forward_research_stays_in_model_evidence_with_lookup_caveat(self) -> None:
        report = {
            "status": "available", "cutoff_at_utc": "2026-09-26T22:47:55Z",
            "strict_history_scope": {"events": 23, "stable_id_result_bouts": 118},
            "captured_history_scope": {"events": 790, "accepted_result_bouts": 7258},
            "source_card_bouts": 13, "selected_card_bouts": 8,
            "identity_hold_positions": [9, 10, 11, 12, 13],
            "selected_bouts_with_two_sided_saved_books": 4,
            "rows": [{
                "bout": f"Fighter A{i} vs Fighter B{i}",
                "strict_elo_probability_fighter_a": 0.45,
                "captured_elo_probability_fighter_a": 0.55,
                "captured_logistic_probability_fighter_a": 0.52,
                "strict_prior_bouts": (1, 2), "captured_prior_bouts": (10, 12),
                "saved_two_sided_book_count": int(i <= 4),
            } for i in range(1, 9)],
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ufc_odds_model.dashboard_data.load_ufc332_forward_research",
            return_value=report,
        ) as loader, patch.dict(os.environ, {
            "UFC_MODEL_UFC332_STRICT_REPORT":
                "reports/ufc332-forward-research-20260926-sealed-v2.json",
            "UFC_MODEL_UFC332_CAPTURED_REPORT":
                "reports/ufc332-captured-forward-research-20260926-sealed-v2.json",
        }):
            app = self._run_app(
                Path(temporary) / "missing.sqlite", Path(temporary) / "missing.json",
            )
        self.assertEqual(list(app.exception), [])
        self.assertEqual(loader.call_args.args[:2], (
            Path("reports/ufc332-forward-research-20260926-sealed-v2.json"),
            Path("reports/ufc332-captured-forward-research-20260926-sealed-v2.json"),
        ))
        evidence_markup = "\n".join(item.value for item in app.tabs[2].markdown)
        other_markup = "\n".join(
            item.value for index, tab in enumerate(app.tabs) if index != 2
            for item in tab.markdown
        )
        self.assertIn("UFC 332 forward research · exploratory only", evidence_markup)
        self.assertIn("Fighter A8 vs Fighter B8", evidence_markup)
        self.assertIn("4 of 8", evidence_markup)
        self.assertIn("target fighter ID lookup is hash-verified", evidence_markup)
        self.assertIn("availability at the saved odds cutoff is unproven", evidence_markup)
        self.assertNotIn("UFC 332 forward research", other_markup)
        self.assertNotIn("Fighter A8 vs Fighter B8", other_markup)

    def test_new_forward_capture_is_default_when_all_inputs_exist(self) -> None:
        paths = (
            Path("reports/ufc332-forward-research-20260927T1601Z.json"),
            Path("reports/ufc332-forward-research-full-20260927T1601Z.json"),
            Path("data/raw/ufc332-odds-20260927T1602Z/card-with-odds-manifest.json"),
            Path("data/raw/ufc332-odds-20260927T1602Z/odds-intake-manifest.json"),
            Path("data/raw/ufc332-odds-20260927T1602Z/reviewed-card-template.csv"),
        )
        original_is_file = Path.is_file

        def present_for_selection(path: Path) -> bool:
            return path in paths or original_is_file(path)

        report = {
            "status": "available", "cutoff_at_utc": "2026-09-27T16:01:50Z",
            "capture_receipts_verified": True,
            "strict_history_scope": {"events": 23, "stable_id_result_bouts": 118},
            "captured_history_scope": {"events": 790, "accepted_result_bouts": 7258},
            "source_card_bouts": 13, "selected_card_bouts": 8,
            "identity_hold_positions": [9, 10, 11, 12, 13],
            "selected_bouts_with_two_sided_saved_books": 4,
            "rows": [],
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            Path, "is_file", present_for_selection,
        ), patch(
            "ufc_odds_model.dashboard_data.load_ufc332_forward_research",
            return_value=report,
        ) as loader, patch.dict(os.environ, {}) as environ:
            environ.pop("UFC_MODEL_UFC332_STRICT_REPORT", None)
            environ.pop("UFC_MODEL_UFC332_CAPTURED_REPORT", None)
            app = self._run_app(
                Path(temporary) / "missing.sqlite", Path(temporary) / "missing.json",
            )
        self.assertEqual(list(app.exception), [])
        self.assertEqual(loader.call_args.args[:2], paths[:2])
        self.assertEqual(loader.call_args.kwargs["evidence_files"], {
            "card_manifest": str(paths[2]), "odds_manifest": str(paths[3]),
            "card_csv": str(paths[4]),
        })
        markup = "\n".join(item.value for item in app.tabs[2].markdown)
        self.assertIn("hash-checked capture receipts", markup)
        self.assertNotIn("availability at the saved odds cutoff is unproven", markup)

    def test_imported_history_is_visible_and_labelled_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.sqlite"
            with db.connect(path) as connection:
                db.init_db(connection)
                for fighter_id in ("a", "b"):
                    db.upsert_fighter(connection, fighter_id, fighter_id.upper(),
                                      "wikipedia_research", fighter_id)
                db.upsert_event(connection, "history", "UFC Archive", "2025-01-11",
                                "completed", source="wikipedia_research",
                                source_event_id="12345")
                db.upsert_bout(connection, "history-bout", "history", "a", "b",
                               "completed", source="wikipedia_research",
                               source_bout_id="12345:1")
                db.upsert_result(connection, "history-bout", "win", "a",
                                 "2025-01-11T20:00:00Z")
                raw = Path(temporary) / "research-source.json"
                raw.write_text('{"checked":true}', encoding="utf-8")
                digest = hashlib.sha256(raw.read_bytes()).hexdigest()
                run_id = connection.execute(
                    "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                    "VALUES ('wikipedia_research', '2025-01-12T00:00:00Z', ?, ?)",
                    (str(raw), digest),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO wikipedia_source_receipts(
                        run_id, event_page_id, event_title, page_url, revision_id,
                        revision_timestamp_utc, license_title, license_url,
                        imported_bouts, skipped_unresolved_bouts
                    ) VALUES (?, 12345, 'UFC Archive',
                              'https://en.wikipedia.org/wiki/UFC_Archive',
                              1, '2025-01-12T00:00:00Z', 'CC BY-SA 4.0',
                              'https://creativecommons.org/licenses/by-sa/4.0/', 1, 1)
                    """, (run_id,),
                )
                connection.commit()
            before = path.stat().st_mtime_ns
            app = self._run_app(path, Path(temporary) / "missing.json")
            self.assertEqual(path.stat().st_mtime_ns, before)
            raw.unlink()
            unverified_app = self._run_app(path, Path(temporary) / "missing.json")
        self.assertEqual(list(app.exception), [])
        markup = self._all_markup(app)
        self.assertIn("Research-only source", markup)
        self.assertIn("UFC Archive", markup)
        self.assertIn("Imported bouts by year", markup)
        self.assertIn("Fighters in bouts", markup)
        self.assertIn("Recorded results", markup)
        self.assertIn("Identity and source-row coverage", markup)
        self.assertIn("Held for identity review", markup)
        self.assertIn("50.0%", markup)
        self.assertEqual(list(unverified_app.exception), [])
        unverified_markup = self._all_markup(unverified_app)
        self.assertIn("Identity coverage unavailable", unverified_markup)
        self.assertNotIn("Identity and source-row coverage", unverified_markup)

    def test_demo_card_is_labelled_and_database_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.sqlite"
            with db.connect(path) as connection:
                db.init_db(connection)
                demo.seed_demo(connection)
            mtime = path.stat().st_mtime_ns
            app = self._run_app(path, Path(temporary) / "missing.json")
            self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(list(app.exception), [])
        markup = self._all_markup(app)
        self.assertIn("Demo data only", markup)
        self.assertIn("Demo · unavailable", markup)
        self.assertIn("No historical research data in this database", markup)
        self.assertIn("bookmaker time unverified", markup)
        self.assertNotIn("Place wager", markup)

    def test_imported_upcoming_card_discloses_unknown_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "partial.sqlite"
            start = datetime.now(timezone.utc) + timedelta(days=7)
            with db.connect(path) as connection:
                db.init_db(connection)
                db.upsert_fighter(connection, "fighter-a", "Fighter A")
                db.upsert_fighter(connection, "fighter-b", "Fighter B")
                db.upsert_event(
                    connection, "partial-card", "Prospective card",
                    start.date().isoformat(), "scheduled", source="manual",
                    source_event_id="partial-card", start_time_utc=start.isoformat(),
                )
                db.upsert_bout(connection, "bout-1", "partial-card", "fighter-a",
                               "fighter-b", "scheduled", source="manual")
                connection.commit()
            before = path.stat().st_mtime_ns
            app = self._run_app(path, Path(temporary) / "missing.json")
            self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(list(app.exception), [])
        markup = self._all_markup(app)
        self.assertIn("Imported card · completeness unverified", markup)
        self.assertIn("Imported bouts only · verify the full card and substitutions", markup)
        self.assertIn("Card completeness", markup)

    def test_malformed_saved_evaluation_is_not_displayed_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.sqlite"
            report_path = Path(temporary) / "evaluation.json"
            with db.connect(path) as connection:
                db.init_db(connection)
                demo.seed_demo(connection)
            report_path.write_text(json.dumps({
                "schema_version": 1,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_db_path": str(path.resolve()),
                "source_db_mtime_ns": path.stat().st_mtime_ns,
                "parameters": {},
                "evaluation": {"status": "ok", "split": "broken", "test": "broken"},
            }), encoding="utf-8")
            app = self._run_app(path, report_path)
        self.assertEqual(list(app.exception), [])
        markup = self._all_markup(app)
        self.assertIn("Saved evaluation has incomplete metrics or split evidence", markup)
        self.assertNotIn("<h2>Same priced-bout subset</h2>", markup)

    def test_incomplete_prior_results_show_exploratory_scores_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.sqlite"
            report_path = Path(temporary) / "evaluation.json"
            with db.connect(path) as connection:
                db.init_db(connection)
                demo.seed_demo(connection)
            metrics = {"bouts": 1, "accuracy": 1.0, "brier_score": 0.04, "log_loss": 0.2}
            bins = [
                {"lower": index / 5, "upper": (index + 1) / 5,
                 "bouts": int(index == 3),
                 "mean_probability": 0.7 if index == 3 else None,
                 "observed_win_rate": 1.0 if index == 3 else None}
                for index in range(5)
            ]
            evidence_split = {
                "bouts": 1, "available_prior_result_instances": 1,
                "observed_prior_result_instances": 1, "unverified_feature_rows": 0,
                "coverage": 1.0, "meets_threshold": True,
            }
            evidence = {
                "threshold": 1.0, "promotion_eligible": False,
                "warning": "Incomplete source-observed prior results.",
                "train": {**evidence_split, "observed_prior_result_instances": 0,
                          "coverage": 0.0, "meets_threshold": False},
                "validation": evidence_split, "test": evidence_split,
            }
            split = {
                name: {"bouts": 1, "events": 1, "first_date": date,
                       "last_date": date}
                for name, date in (("train", "2026-01-01"),
                                   ("validation", "2026-02-01"),
                                   ("test", "2026-03-01"))
            }
            result = {
                "status": "insufficient_result_evidence", "prior_result_evidence": evidence,
                "split": split,
                "calibration": {"method": "symmetric_temperature", "validation_bouts": 1,
                                "applied": False, "scale": 1.0},
                "test": {
                    "elo": {"metrics": metrics, "calibration_bins": bins},
                    "logistic": {"metrics": metrics, "calibration_bins": bins},
                    "bookmaker": {"available_bouts": 0, "coverage": 0.0,
                                  "metrics": None, "elo_on_available_bouts": None,
                                  "logistic_on_available_bouts": None,
                                  "calibration_bins": None},
                },
            }
            report_path.write_text(json.dumps({
                "schema_version": 1,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_db_path": str(path.resolve()),
                "source_db_mtime_ns": path.stat().st_mtime_ns,
                "parameters": {}, "evaluation": result,
            }), encoding="utf-8")
            app = self._run_app(path, report_path)
        self.assertEqual(list(app.exception), [])
        markup = self._all_markup(app)
        self.assertIn("Exploratory metrics only · model promotion blocked", markup)
        self.assertIn("Prior-result source evidence", markup)
        self.assertIn("0 / 1", markup)
        self.assertIn("Model comparison (exploratory)", markup)
        self.assertIn("Brier 0.040", markup)


if __name__ == "__main__":
    unittest.main()
