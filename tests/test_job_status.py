"""Operator job failures remain visible without storing provider secrets."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.cli import main


class OperatorJobStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "jobs.sqlite"

    def test_succeeded_and_failed_source_jobs_are_persisted_without_exception_text(self) -> None:
        with patch.dict(os.environ, {"ODDS_API_KEY": "fixture-key"}), patch(
            "ufc_odds_model.odds_api.fetch_mma_h2h", return_value=[]
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                "--db", str(self.database), "import-odds",
                "--raw-dir", str(Path(self.temp.name) / "raw"),
            ]), 0)
        error_output = io.StringIO()
        with patch.dict(os.environ, {"ODDS_API_KEY": "secret-api-key"}), patch(
            "ufc_odds_model.odds_api.fetch_mma_h2h",
            side_effect=RuntimeError("provider URL contained secret-api-key"),
        ), redirect_stderr(error_output):
            self.assertEqual(main(["--db", str(self.database), "import-odds"]), 1)
        self.assertNotIn("secret-api-key", error_output.getvalue())
        self.assertIn("[redacted]", error_output.getvalue())
        with db.connect(self.database) as connection:
            rows = connection.execute(
                "SELECT command, status, error_category, started_at_utc, finished_at_utc "
                "FROM operator_job_runs ORDER BY job_run_id"
            ).fetchall()
            self.assertEqual(
                [(row["command"], row["status"], row["error_category"]) for row in rows],
                [("import-odds", "succeeded", None),
                 ("import-odds", "failed", "source_or_runtime_error")],
            )
            self.assertTrue(all(row["started_at_utc"] and row["finished_at_utc"] for row in rows))
            self.assertNotIn("secret-api-key", self.database.read_bytes().decode("latin1"))
            with self.assertRaisesRegex(Exception, "immutable"):
                connection.execute(
                    "UPDATE operator_job_runs SET status = 'failed' WHERE job_run_id = 1"
                )


if __name__ == "__main__":
    unittest.main()
