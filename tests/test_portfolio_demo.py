"""The portable portfolio demo is offline, reproducible and explicit about gaps."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.historical_stats import HistoryStore
from ufc_odds_model.portfolio_demo import FIXTURE_DIRECTORY, NOTICE, run_demo


ROOT = Path(__file__).resolve().parents[1]


class PortfolioDemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "demo"

    def test_real_normalizer_retains_nulls_quarantines_round_gaps_and_excludes_target_stats(self):
        result = run_demo(self.output)
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["quality_counts"], {
            "events": 5, "bouts": 6, "usable_bouts": 5, "held_bouts": 1, "source_round_rows": 15,
            "missing_reach_profiles": 1, "missing_control_fighter_bouts": 1, "unknown_duration_usable_bouts": 1})
        with HistoryStore(self.output / "normalized/statistics.sqlite") as store:
            self.assertIsNone(store.fighter_profile("ufcstats:fighter:ffffffffffffff03")["reach_cm"])
            aster = store.prior_fighter_bouts("ufcstats:fighter:ffffffffffffff01", "2024-05-01")
            self.assertEqual(len(aster), 2)
            self.assertIsNone(aster[0].totals["control_seconds"])
            self.assertEqual(aster[0].totals["sig_landed"], 16)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM source_round_rows WHERE event_name='SYNTHETIC Incomplete Card'").fetchone()[0], 3)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM fighter_bout_facts WHERE bout_id='ufcstats:fight:ffffffffffffff04'").fetchone()[0], 0)
        sample = json.loads((self.output / "reports/card-statistics.json").read_text())
        self.assertTrue(sample["synthetic"])
        self.assertEqual(sample["summary"]["bouts"], 2)
        aster, dune = sample["bouts"][0]["fighter_a"], sample["bouts"][0]["fighter_b"]
        self.assertEqual(aster["prior_stat_bouts"], 2)
        self.assertEqual(dune["prior_stat_bouts"], 1)
        self.assertIsNone(dune["metrics"]["significant_strikes_landed_per_minute"]["value"])
        self.assertIsNotNone(dune["metrics"]["significant_strike_accuracy_pct"]["value"])
        self.assertTrue(all(fight["event_date"] < "2024-05-01" for bout in sample["bouts"] for side in ("fighter_a", "fighter_b") for fight in bout[side]["prior_fights"]))
        markup = (self.output / "reports/card-statistics.html").read_text()
        self.assertIn(NOTICE, markup)
        self.assertNotIn("href='http://ufcstats.com", markup)
        self.assertIn("SYNTHETIC", (self.output / "index.html").read_text())

    def test_replay_verifies_and_preserves_all_output_bytes_and_modification_times(self):
        first = run_demo(self.output)
        before = {p.relative_to(self.output): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.rglob("*") if p.is_file()}
        second = run_demo(self.output)
        after = {p.relative_to(self.output): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.rglob("*") if p.is_file()}
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "reused")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(before, after)

    def test_changed_fixture_or_saved_artifact_fails_without_replacing_anything(self):
        fixture = self.root / "fixtures"
        shutil.copytree(FIXTURE_DIRECTORY, fixture)
        source = fixture / "ufc_fight_stats.csv"
        source.write_bytes(source.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "fixture bytes changed"):
            run_demo(self.output, fixtures_dir=fixture)
        self.assertFalse(self.output.exists())
        run_demo(self.output)
        quality = self.output / "reports/quality.json"
        quality.write_text("changed")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            run_demo(self.output)
        self.assertEqual(quality.read_text(), "changed")

    def test_default_demo_uses_no_network_and_exports_no_environment_credentials(self):
        secret = "demo-test-credential-must-not-appear"
        with patch("socket.socket", side_effect=AssertionError("Network is forbidden in the offline demo")), patch.dict(os.environ, {"UFC_MYSQL_PASSWORD": secret}):
            result = run_demo(self.output)
        self.assertNotIn("mysql", result)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())

    def test_mysql_opt_in_reads_private_config_only_for_publish_and_restores_environment(self):
        config = self.root / "private-config.json"
        config.write_text(json.dumps({"environment": {"UFC_MYSQL_HOST": "127.0.0.1", "UFC_MYSQL_PORT": "3307", "UFC_MYSQL_PASSWORD": "private-test-value", "UNRELATED": "ignored"}, "options": {"mysql": True}}))
        config.chmod(0o600)
        calls = []
        def publisher(path, *, run_id):
            self.assertEqual(path, self.output / "normalized/statistics.sqlite")
            self.assertEqual(os.environ["UFC_MYSQL_PASSWORD"], "private-test-value")
            self.assertEqual(os.environ["UFC_MYSQL_PORT"], "3307")
            self.assertNotIn("UNRELATED", os.environ)
            calls.append(run_id)
            return {"run_id": run_id, "status": "published", "counts": {"events": 5}, "password": "must-not-be-exported", "connection": {"password": "also-private"}}
        with patch.dict(os.environ, {"UFC_MYSQL_PASSWORD": "original-value"}):
            result = run_demo(self.output, mysql=True, config=config, publisher=publisher)
            self.assertEqual(os.environ["UFC_MYSQL_PASSWORD"], "original-value")
        self.assertEqual(calls, [result["run_id"]])
        self.assertEqual(set(result["mysql"]), {"run_id", "status", "counts"})
        self.assertNotIn("private-test-value", (self.output / "run-manifest.json").read_text())

    def test_config_without_mysql_is_not_read_or_used(self):
        with self.assertRaisesRegex(ValueError, "explicit --mysql"):
            run_demo(self.output, config=self.root / "does-not-exist.json")
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(shutil.which("uv"), "uv is required for the shell entrypoint check")
    def test_one_command_runs_offline_from_a_clean_checkout_with_spaces_and_no_virtualenv(self):
        checkout = self.root / "clean checkout"
        package = checkout / "src/ufc_odds_model"
        package.mkdir(parents=True)
        for name in ("__init__.py", "portfolio_demo.py", "historical_stats.py", "card_statistics.py", 'mysql_runtime.py'):
            shutil.copyfile(ROOT / "src/ufc_odds_model" / name, package / name)
        shutil.copytree(FIXTURE_DIRECTORY, checkout / "demo/fixtures")
        (checkout / "scripts").mkdir()
        script = checkout / "scripts/demo.sh"
        shutil.copyfile(ROOT / "scripts/demo.sh", script)
        script.chmod(0o755)
        environment = {key: value for key, value in os.environ.items() if key not in {"VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT", "UV_ENV_FILE"}}
        environment["UV_CACHE_DIR"] = str(self.root / "uv-cache")
        command = [str(script), '--legacy-sqlite-fixture', "--output-dir", str(checkout / "output/pipeline-demo")]
        result = subprocess.run(command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "created")
        self.assertFalse((checkout / ".venv").exists())
        replay = subprocess.run(command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["status"], "reused")


if __name__ == "__main__":
    unittest.main()
