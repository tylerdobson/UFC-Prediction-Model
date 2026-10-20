"""Saved dashboard forecasts must be public, frozen and independently verified."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ufc_odds_model.dashboard_forecast import load_saved_forecast


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def report_fixture():
    return {"schema_version": 1,
            "event": {"name": "Synthetic research card", "event_date": "2026-10-24",
                      "start_time_utc": "2026-10-24T14:00:00Z"},
            "generated_at_utc": "2026-10-19T20:00:00Z",
            "forecast": {"model_version": "fixture-v6:research_retrospective", "model_label": "Synthetic fixture",
                         "cutoff_at_utc": "2026-10-19T20:00:00Z", "research_only": True,
                         "status": "prospective_research", "forecast_id": "synthetic-forecast"},
            "bouts": [{"bout_id": "fixture-one", "fighter_a": "Fixture Alpha", "fighter_b": "Fixture Beta",
                       "p_fighter_a": 0.620000000000003, "p_fighter_b": 0.379999999999997}],
            "unscored_bouts": [{"fighter_a": "Fixture Gamma", "fighter_b": "Fixture Delta",
                                "reason": "Identity withheld in this synthetic fixture."}]}


class DashboardForecastTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.relative = Path("output/test-edition/prediction-report.json")
        self.path = self.root / self.relative
        self.path.parent.mkdir(parents=True)

    def publish(self, report=None, *, include_extra=False):
        report = copy.deepcopy(report if report is not None else report_fixture())
        self.path.write_text(json.dumps(report), encoding="utf-8")
        self.path.with_suffix(".html").write_text("<!doctype html><title>Synthetic fixture report</title>", encoding="utf-8")
        outputs = {}
        for path in (self.path, self.path.with_suffix(".html")):
            outputs[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if include_extra:
            pdf = self.root / "output/pdf/fixture.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"synthetic-not-an-actual-pdf-download-test")
            outputs[pdf.relative_to(self.root).as_posix()] = hashlib.sha256(pdf.read_bytes()).hexdigest()
        manifest = {"schema_version": 1, "forecast": report["forecast"], "scored_matchups": len(report["bouts"]),
                    "unscored_matchups": len(report.get("unscored_bouts", [])), "outputs": outputs}
        self.manifest = self.path.with_name("release-manifest.json")
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        return report, manifest

    def load(self, **kwargs):
        return load_saved_forecast(self.relative, project_root=self.root, as_of=kwargs.pop("as_of", NOW), **kwargs)

    def assertUnavailable(self, result):
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["report"])
        self.assertEqual(result["artifacts"], {})
        self.assertFalse(result["operating_eligible"])
        self.assertFalse(result["promotion_eligible"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_independent_fixture_preserves_frozen_values_without_invented_metrics_or_file_writes(self):
        raw = report_fixture()
        raw["private_metadata"] = {"source_path": "/Users/example/private.sqlite"}
        expected, _ = self.publish(raw, include_extra=True)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        with patch("socket.socket", side_effect=AssertionError("Saved forecasts cannot use the network")):
            snapshot = self.load()
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["label"], "Saved research forecast")
        self.assertEqual(snapshot["event_status"], "upcoming")
        self.assertTrue(snapshot["research_only"])
        self.assertFalse(snapshot["operating_eligible"])
        self.assertFalse(snapshot["promotion_eligible"])
        self.assertEqual(snapshot["report"]["bouts"][0]["p_fighter_a"], expected["bouts"][0]["p_fighter_a"])
        self.assertEqual(snapshot["report"]["bouts"][0]["p_fighter_b"], expected["bouts"][0]["p_fighter_b"])
        self.assertNotIn("evaluation", snapshot["report"])
        self.assertNotIn("private_metadata", snapshot["report"])
        self.assertNotIn("/Users/", json.dumps(snapshot))
        self.assertEqual(set(snapshot["artifacts"]), {"json", "html", "pdf"})
        self.assertEqual(snapshot["artifacts"]["html"]["path"], self.relative.with_suffix(".html").as_posix())

    def test_event_passed_status_changes_at_start_and_date_only_uses_midnight_utc(self):
        self.publish()
        at_start = datetime(2026, 10, 24, 14, tzinfo=timezone.utc)
        result = self.load(as_of=at_start)
        self.assertEqual(result["event_status"], "event_passed")
        self.assertIn("event date passed", result["label"])
        self.assertFalse(result["operating_eligible"])
        report = report_fixture()
        del report["event"]["start_time_utc"]
        self.publish(report)
        self.assertEqual(self.load(as_of=datetime(2026, 10, 24, tzinfo=timezone.utc))["event_status"], "event_passed")

    def test_missing_release_or_hash_mismatches_hide_all_forecast_values(self):
        self.assertUnavailable(self.load())
        self.publish()
        self.manifest.unlink()
        self.assertUnavailable(self.load())
        for relative in (self.relative, self.relative.with_suffix(".html"), Path("output/pdf/fixture.pdf")):
            with self.subTest(artifact=str(relative)):
                self.publish(include_extra=True)
                path = self.root / relative
                path.write_bytes(path.read_bytes() + b"changed")
                self.assertUnavailable(self.load())

    def test_even_rehashed_invalid_probabilities_and_names_are_rejected(self):
        for field, value in (("p_fighter_a", float("nan")), ("p_fighter_b", float("inf")),
                             ("p_fighter_a", True), ("p_fighter_a", -0.1), ("p_fighter_b", 1.1),
                             ("p_fighter_b", 0.5), ("fighter_a", ""), ("fighter_b", "fixture alpha")):
            with self.subTest(field=field, value=value):
                report = report_fixture()
                report["bouts"][0][field] = value
                self.publish(report)
                self.assertUnavailable(self.load())

    def test_cutoff_generation_and_clock_chronology_fail_closed(self):
        for mutation in (lambda r: r["forecast"].update(cutoff_at_utc="2026-10-21T00:00:00Z"),
                         lambda r: r.update(generated_at_utc="2026-10-21T00:00:00Z"),
                         lambda r: r.update(generated_at_utc="2026-10-18T00:00:00Z"),
                         lambda r: r["forecast"].update(cutoff_at_utc=r["event"]["start_time_utc"]),
                         lambda r: r["event"].update(event_date="invalid-date")):
            report = report_fixture()
            mutation(report)
            self.publish(report)
            self.assertUnavailable(self.load())
        report = report_fixture()
        del report["event"]["start_time_utc"]
        report["event"]["event_date"] = "2026-10-19"
        self.publish(report)
        self.assertUnavailable(self.load())
        self.publish()
        self.assertUnavailable(self.load(as_of=datetime(2026, 10, 20)))

    def test_research_identity_and_release_counts_must_agree(self):
        report = report_fixture()
        report["forecast"]["research_only"] = False
        self.publish(report)
        self.assertUnavailable(self.load())
        for mutation in (lambda m: m["forecast"].update(status="production"),
                         lambda m: m["forecast"].update(model_version="different-model"),
                         lambda m: m.update(scored_matchups=2),
                         lambda m: m.update(unscored_matchups=True)):
            _, manifest = self.publish()
            mutation(manifest)
            self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertUnavailable(self.load())

    def test_traversal_symlinks_and_outside_report_paths_are_rejected(self):
        for path in ("../escape.html", "/tmp/escape.html", "https://example.org/report.html", "output\\escape.html"):
            _, manifest = self.publish()
            manifest["outputs"][path] = "0" * 64
            self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertUnavailable(self.load())
        self.publish()
        html = self.path.with_suffix(".html")
        target = self.root / "outside.html"
        html.rename(target)
        html.symlink_to(target)
        self.assertUnavailable(self.load())
        self.assertUnavailable(load_saved_forecast("../outside.json", project_root=self.root, as_of=NOW))
        self.assertUnavailable(load_saved_forecast(self.root.parent / "outside.json", project_root=self.root, as_of=NOW))

    def test_default_checked_in_v6_edition_verifies_without_private_databases(self):
        snapshot = load_saved_forecast(as_of=NOW)
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(len(snapshot["report"]["bouts"]), 6)
        self.assertEqual(len(snapshot["report"]["unscored_bouts"]), 8)
        self.assertTrue(snapshot["report"]["forecast"]["model_version"].startswith("logistic-prior-v6:"))
        self.assertEqual(snapshot["artifacts"]["json"]["path"], "output/ufc333-20261019-v6-final/prediction-report.json")


if __name__ == "__main__":
    unittest.main()
