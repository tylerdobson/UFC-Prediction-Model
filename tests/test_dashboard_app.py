"""Small runtime smoke checks for the optional Streamlit dashboard."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
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
            "Upcoming card", "Model evidence", "Data quality", "Ledgers",
        ])
        markup = self._all_markup(app)
        self.assertIn("No verified upcoming card", markup)
        self.assertIn("Evaluation unavailable", markup)
        self.assertIn("No paper decisions recorded", markup)
        self.assertIn("No manually entered wagers", markup)

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
        self.assertIn("bookmaker time unverified", markup)
        self.assertNotIn("Place wager", markup)

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


if __name__ == "__main__":
    unittest.main()
