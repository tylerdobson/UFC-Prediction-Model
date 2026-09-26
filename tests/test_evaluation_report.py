"""The dashboard consumes a saved evaluation, never fits on a page request."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ufc_odds_model.cli import main


class EvaluationReportTests(unittest.TestCase):
    def test_cli_writes_timestamped_evaluation_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "ufc.sqlite"
            report = root / "reports" / "evaluation.json"
            with redirect_stdout(io.StringIO()) as output:
                status = main([
                    "--db", str(database),
                    "evaluate",
                    "--decision-hours-before-event", "12",
                    "--output", str(report),
                ])

            self.assertEqual(status, 0)
            self.assertTrue(report.is_file())
            saved = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 1)
            self.assertIn("+00:00", saved["generated_at_utc"])
            self.assertEqual(saved["source_db_path"], str(database.resolve()))
            self.assertEqual(saved["source_db_mtime_ns"], database.stat().st_mtime_ns)
            self.assertEqual(saved["parameters"]["decision_hours_before_event"], 12.0)
            self.assertEqual(saved["evaluation"]["status"], "insufficient_history")
            self.assertEqual(json.loads(output.getvalue())["status"], "insufficient_history")

    def test_output_cannot_replace_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "ufc.sqlite"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = main([
                    "--db", str(database), "evaluate", "--output", str(database)
                ])
            self.assertEqual(status, 1)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )


if __name__ == "__main__":
    unittest.main()
