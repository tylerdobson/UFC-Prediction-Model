"""Chronological v4 selection, frozen inputs, and faithful report verification."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.features import FeatureRow
from ufc_odds_model.model_lab import VALIDATION_END, file_sha256
from ufc_odds_model.research_evaluation import _digest_json
from ufc_odds_model import research_upgrade as upgrade


def example(day, value=.3, target=1, suffix=""):
    return FeatureRow(day + suffix, day, day, "a", "b", (value,), target)


def make_history(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        db.init_db(connection)
        for name in ("a", "b", "c", "d"):
            db.upsert_fighter(connection, name, name.upper())
        for year in range(2010, 2027):
            for month in (1, 3, 5, 7, 9, 11):
                day = f"{year}-{month:02d}-01"
                a, b = ("a", "b") if month % 3 else ("c", "d")
                source = "wikipedia_research_restored" if month == 7 else "wikipedia_research"
                db.upsert_event(connection, day, day, day, "completed", source="wikipedia_research")
                db.upsert_bout(connection, day, day, a, b, "completed", source=source)
                db.upsert_result(connection, day, "win", a if (year + month) % 3 else b,
                                 day + "T23:00:00Z", "Decision (unanimous)")


class TrainingPolicyTests(unittest.TestCase):
    def test_window_anchors_origin_year_and_preserves_rows(self):
        policy = upgrade.TrainingPolicy(.002, 8)
        rows = [example(day) for day in ("2014-12-31", "2015-01-01", "2021-01-01", "2023-12-09")]
        eligible = upgrade.eligible_training_rows(rows, "2023-12-09", policy)
        self.assertEqual([r.event_date for r in eligible], ["2015-01-01", "2021-01-01"])
        self.assertIs(eligible[0], rows[1])
        self.assertEqual(policy.lower_date("2016-01-01"), "2010-01-01")
        self.assertEqual(upgrade.BASELINE_POLICY.lower_date("2026-01-01"), "2010-01-01")

    def test_invalid_policies_are_rejected(self):
        for values in ((0, None), (float("nan"), 8), (.02, 0), (.02, True), (.02, 1.5)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                upgrade.TrainingPolicy(*values)

    def test_whole_date_calibration_and_future_labels_cannot_change_fit(self):
        rows = [example(f"{year}-01-01", .2 if year % 2 else -.3, year % 2, suffix)
                for year in range(2010, 2027) for suffix in ("a", "b")]
        policy = upgrade.TrainingPolicy(.002, 8)
        run = upgrade.calibrated_policy_fit(rows, ("x",), "2024-01-01", policy)
        changed = [replace(row, target=1 - row.target) if row.event_date >= "2024-01-01" else row for row in rows]
        second = upgrade.calibrated_policy_fit(changed, ("x",), "2024-01-01", policy)
        self.assertEqual(run.model.weights, second.model.weights)
        self.assertEqual(run.calibrator, second.calibrator)
        self.assertLess(max(r.event_date for r in run.training), min(r.event_date for r in run.calibration))
        self.assertLess(max(r.event_date for r in run.calibration), run.origin_date)
        self.assertEqual(len(run.calibration) % 2, 0)
        probability = run.predict_probability((.7,))
        self.assertAlmostEqual(run.predict_probability((-.7,)), 1 - probability)

    def test_separate_fit_rejects_overlapping_dates_and_uses_explicit_penalty(self):
        train = [example("2020-01-01"), example("2020-02-01", -.3, 0)]
        validation = [example("2021-01-01")]
        run = upgrade.fit_separated_policy(train, validation, ("x",), "2022-01-01", upgrade.TrainingPolicy(.002))
        stronger = upgrade.fit_separated_policy(train, validation, ("x",), "2022-01-01", upgrade.TrainingPolicy(.1))
        self.assertGreater(abs(run.model.weights[0]), abs(stronger.model.weights[0]))
        for calibration, origin in ((train, "2022-01-01"), (validation, "2021-01-01")):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "strictly ordered"):
                upgrade.fit_separated_policy(train, calibration, ("x",), origin, upgrade.BASELINE_POLICY)


class FrozenExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.database = cls.directory / "history.sqlite"
        make_history(cls.database)
        cls.selection = cls.directory / "selection.json"
        cls.evaluation = cls.directory / "evaluation.json"
        cls.before = file_sha256(cls.database)
        actual_lab_rows = upgrade.lab_rows
        with patch.object(upgrade, "lab_rows", wraps=actual_lab_rows) as wrapped:
            cls.manifest = upgrade.select_upgrade(cls.database, cls.selection)
            cls.selection_calls = list(wrapped.call_args_list)
        cls.report = upgrade.evaluate_upgrade(cls.database, cls.selection, cls.evaluation)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def altered(self, original, filename, digest_key, change, *, resign=False):
        content = json.loads(Path(original).read_text())
        change(content)
        if resign:
            content[digest_key] = _digest_json({k: v for k, v in content.items() if k != digest_key})
        path = self.directory / filename
        path.write_text(json.dumps(content))
        return path

    def test_selection_reads_only_pretest_labels_and_nine_declared_candidates(self):
        self.assertEqual(len(self.manifest["trials"]), 9)
        self.assertEqual(len(self.selection_calls), 1)
        self.assertEqual(self.selection_calls[0].kwargs["through_date"], VALIDATION_END)
        self.assertLessEqual(self.manifest["selection_data_last_date"], VALIDATION_END)
        self.assertFalse(self.manifest["fixed_test_labels_used"])
        for trial in self.manifest["trials"]:
            for fold in trial["pretest_rolling"]["folds"]:
                self.assertLess(fold["fit"]["calibration"]["last_date"], f"{fold['year']}-01-01")
                self.assertLessEqual(fold["test_summary"]["last_date"], VALIDATION_END)

    def test_saved_report_verifies_and_preserves_database(self):
        result = upgrade.load_verified_report(self.evaluation, self.selection, self.database)
        self.assertEqual(result, json.loads(self.evaluation.read_text()))
        self.assertEqual(file_sha256(self.database), self.before)
        self.assertTrue(result["fixed_test_is_reused_benchmark"])
        self.assertFalse(result["fresh_holdout"])
        self.assertFalse(result["promotion_eligible"])
        self.assertEqual(result["selection_file_sha256"], file_sha256(self.selection))
        for label in ("fixed_test", "rolling_origin"):
            section = result[label]
            self.assertEqual(section["baseline"]["metrics"]["bouts"], section["candidate"]["metrics"]["bouts"])
            self.assertEqual(len(section["predictions"]), section["candidate"]["metrics"]["bouts"])
            self.assertIn("restored", section["cohorts"])
            self.assertEqual(section["paired_uncertainty"]["method"], "paired_event_date_block_bootstrap")

    def test_selection_and_evaluation_refuse_overwrite(self):
        for action in (lambda: upgrade.select_upgrade(self.database, self.selection),
                       lambda: upgrade.evaluate_upgrade(self.database, self.selection, self.evaluation)):
            with self.assertRaises(FileExistsError):
                action()

    def test_evaluation_requires_selection_on_disk_before_reading_labels(self):
        with patch.object(upgrade, "lab_rows") as reader:
            with self.assertRaises(ValueError):
                upgrade.evaluate_upgrade(self.database, self.directory / "missing.json", self.directory / "missing-eval.json")
            reader.assert_not_called()

    def test_tampered_selection_and_changed_database_are_rejected(self):
        bad = self.altered(self.selection, "bad-selection.json", "manifest_sha256",
                           lambda x: x.update(fixed_test_labels_used=True))
        with self.assertRaisesRegex(ValueError, "content hash"):
            upgrade.load_selection(bad)
        changed = self.directory / "changed.sqlite"
        changed.write_bytes(self.database.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "database hash"):
            upgrade.load_verified_report(self.evaluation, self.selection, changed)

    def test_resigned_future_selection_is_rejected(self):
        bad = self.altered(self.selection, "future-selection.json", "manifest_sha256",
                           lambda x: x.update(selection_data_last_date="2024-01-01"), resign=True)
        with self.assertRaisesRegex(ValueError, "pretest protocol"):
            upgrade.load_selection(bad)

    def test_report_tampering_and_resigned_metric_mismatch_are_rejected(self):
        bad = self.altered(self.evaluation, "bad-eval.json", "report_content_sha256",
                           lambda x: x.update(selected_policy={}))
        with self.assertRaisesRegex(ValueError, "content hash"):
            upgrade.load_verified_report(bad, self.selection)
        def change_metric(content):
            content["fixed_test"]["candidate"]["metrics"]["log_loss"] = .001
        mismatch = self.altered(self.evaluation, "mismatch-eval.json", "report_content_sha256", change_metric, resign=True)
        with self.assertRaisesRegex(ValueError, "metrics do not match"):
            upgrade.load_verified_report(mismatch, self.selection)

    def test_nonfinite_prediction_is_rejected_even_with_recomputed_hash(self):
        def change_probability(content):
            content["fixed_test"]["predictions"][0]["candidate_probability_a"] = float("inf")
        bad = self.altered(self.evaluation, "nonfinite-eval.json", "report_content_sha256", change_probability, resign=True)
        with self.assertRaisesRegex(ValueError, "Nonfinite"):
            upgrade.load_verified_report(bad, self.selection)

    def test_cli_verify_reads_existing_report(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            upgrade.main(["verify", "--report", str(self.evaluation), "--selection", str(self.selection),
                          "--db", str(self.database)])
        self.assertIn("Verified retrospective report", output.getvalue())


if __name__ == "__main__":
    unittest.main()
