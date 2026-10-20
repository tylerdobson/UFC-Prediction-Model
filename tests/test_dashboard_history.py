"""Descriptive chart history uses observed earlier counts and validated exposure."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ufc_odds_model.dashboard_history import build_dashboard_history, load_dashboard_history
from ufc_odds_model.historical_stats import build_history
from ufc_odds_model.portfolio_demo import FIXTURE_DIRECTORY


class DashboardHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "statistics.sqlite"
        build_history(FIXTURE_DIRECTORY / "fixture_manifest.json", self.db)
        self.report = {"schema_version": 1, "event": {"name": "Synthetic comparison", "event_date": "2024-07-01"},
            "generated_at_utc": "2024-06-02T00:00:00Z",
            "forecast": {"model_version": "synthetic-research", "research_only": True,
                         "status": "prospective_research", "cutoff_at_utc": "2024-06-02T00:00:00Z"},
            "bouts": [{"bout_id": "example", "fighter_a": "Demo Aster Vale", "fighter_b": "Demo Dune Reed",
                       "p_fighter_a": .6, "p_fighter_b": .4}]}
        self.card = {"event": self.report["event"], "bouts": [{"bout_id": "example",
             "fighter_a": {"name": "Demo Aster Vale", "fighter_id": "ufcstats:fighter:ffffffffffffff01"},
             "fighter_b": {"name": "Demo Dune Reed", "fighter_id": "ufcstats:fighter:ffffffffffffff04"}}]}
        self.report_path = self.root / "report.json"
        self.card_path = self.root / "card.json"

    def export(self, **kwargs):
        self.report_path.write_text(json.dumps(self.report))
        self.card_path.write_text(json.dumps(self.card))
        return build_dashboard_history(self.report_path, self.card_path, self.db, **kwargs)

    def test_observed_fights_and_rounds_preserve_counts_and_unknown_exposure_without_padding(self):
        original = self.db.read_bytes()
        output = self.export()
        self.assertEqual(self.db.read_bytes(), original)
        self.assertTrue(output["research_only"])
        self.assertFalse(output["model_features_reproduced"])
        aster = output["fighters"]["Demo Aster Vale"]
        self.assertEqual(aster["selected_bouts"], 3)
        fight = aster["fights"][0]
        self.assertEqual(fight["significant_strikes_landed"], 16)
        self.assertAlmostEqual(fight["significant_strikes_landed_per_minute"], 16 * 60 / fight["elapsed_seconds"])
        self.assertEqual(sum(x["significant_strikes_landed"] for x in fight["rounds"]), 16)
        self.assertEqual(sum(x["elapsed_seconds"] for x in fight["rounds"]), fight["elapsed_seconds"])
        dune = output["fighters"]["Demo Dune Reed"]
        self.assertEqual(dune["selected_bouts"], 2)
        unknown = dune["fights"][0]
        self.assertIsNone(unknown["elapsed_seconds"])
        self.assertIsNone(unknown["significant_strikes_landed_per_minute"])
        self.assertIsNotNone(unknown["significant_strikes_landed"])
        self.assertTrue(all(row["elapsed_seconds"] is None and row["significant_strikes_landed_per_minute"] is None for row in unknown["rounds"]))
        limited = self.export(max_fights=1)["fighters"]["Demo Aster Vale"]
        self.assertEqual(limited["selected_bouts"], 1)
        self.assertEqual(limited["fights"][0]["event_date"], "2024-05-01")
        self.assertNotIn(str(self.root), json.dumps(output))

    def test_names_do_not_substitute_for_missing_or_wrong_stable_identities(self):
        original = copy.deepcopy(self.card)
        for identity in (None, "unknown-id", "ufcstats:fighter:ffffffffffffff02"):
            self.card = copy.deepcopy(original)
            self.card["bouts"][0]["fighter_a"]["fighter_id"] = identity
            with self.assertRaises(ValueError):
                self.export()

    def test_capture_after_cutoff_and_mismatched_target_event_are_rejected(self):
        self.report["forecast"]["cutoff_at_utc"] = "2024-05-31T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "capture"):
            self.export()
        self.report["forecast"]["cutoff_at_utc"] = "2024-06-02T00:00:00Z"
        self.card["event"] = {"event_date": "2024-07-02"}
        with self.assertRaisesRegex(ValueError, "event dates"):
            self.export()

    def saved_asset(self):
        asset = self.export()
        path = self.root / "history.json"
        path.write_text(json.dumps(asset))
        return path, asset

    def load(self, path):
        return load_dashboard_history(path, report_sha256=hashlib.sha256(self.report_path.read_bytes()).hexdigest(),
                                      cutoff_at_utc=self.report["forecast"]["cutoff_at_utc"])

    def test_reader_preserves_public_observations_nulls_and_asset_fingerprint(self):
        path, asset = self.saved_asset()
        result = self.load(path)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["fighters"], asset["fighters"])
        self.assertEqual(result["artifact_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertIsNone(result["fighters"]["Demo Dune Reed"]["fights"][0]["significant_strikes_landed_per_minute"])

    def test_reader_rejects_wrong_binding_missing_and_malformed_files_without_path_disclosure(self):
        path, _ = self.saved_asset()
        for kwargs in ({"report_sha256": "0" * 64, "cutoff_at_utc": self.report["forecast"]["cutoff_at_utc"]},
                       {"report_sha256": hashlib.sha256(self.report_path.read_bytes()).hexdigest(), "cutoff_at_utc": "2024-06-03T00:00:00Z"}):
            result = load_dashboard_history(path, **kwargs)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["fighters"], {})
            self.assertNotIn(str(self.root), json.dumps(result))
        path.write_text("invalid JSON")
        self.assertEqual(self.load(path)["status"], "unavailable")
        path.unlink()
        self.assertEqual(self.load(path)["fighters"], {})

    def test_reader_rejects_forged_rates_dates_round_order_counts_and_window(self):
        path, original = self.saved_asset()
        mutations = [
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"][0].update(significant_strikes_landed_per_minute=float("nan")),
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"][0].update(significant_strikes_landed_per_minute=-1),
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"][0].update(significant_strikes_landed_per_minute=999),
            lambda a: a["fighters"]["Demo Dune Reed"]["fights"][0].update(significant_strikes_landed_per_minute=0),
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"][0].update(event_date="2024-06-02"),
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"][0]["rounds"][0].update(round_number=2),
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"][0]["rounds"].pop(),
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"][0]["rounds"][0].update(significant_strikes_landed=-1),
            lambda a: a.update(max_fights_per_fighter=6),
            lambda a: a["fighters"]["Demo Aster Vale"].update(selected_bouts=6),
            lambda a: a["fighters"]["Demo Aster Vale"]["fights"].reverse(),
            lambda a: a["fighters"]["Demo Aster Vale"].update(source_url="file:///private/fixture"),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                asset = copy.deepcopy(original)
                mutation(asset)
                path.write_text(json.dumps(asset))
                result = self.load(path)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["fighters"], {})

    def test_reader_accepts_checked_in_real_history_for_its_exact_frozen_report(self):
        root = Path(__file__).resolve().parents[1]
        report = root / "output/ufc333-20261019-v6-final/prediction-report.json"
        result = load_dashboard_history(root / "assets/data/dashboard-fight-history.json",
            report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(), cutoff_at_utc="2026-10-19T20:22:26Z")
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["fighters"]), 12)
        self.assertEqual(sum(len(f["rounds"]) for p in result["fighters"].values() for f in p["fights"]), 136)


if __name__ == "__main__":
    unittest.main()
