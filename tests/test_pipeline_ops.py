"""Offline operation, publication gates, failure isolation, and launchd contracts."""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ufc_odds_model import pipeline_ops as ops
from tests.test_historical_stats import fixture

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_refresh_schedule.py"
SPEC = importlib.util.spec_from_file_location("ufc_schedule_tests", SCRIPT)
schedule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schedule)


class PipelineOperationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest, self.history = fixture(self.root)
        self.ops_root = self.root / "operations"
        self.alert = patch.object(ops, "send_failure_alerts", return_value={"local": "sent", "webhook": "disabled"}).start()
        self.addCleanup(patch.stopall)

    def refresh(self, **kwargs):
        return ops.refresh_pipeline(ops_root=self.ops_root, archive=self.manifest, **kwargs)

    def pointer(self):
        return (self.ops_root / "latest-ready.json").read_bytes()

    def variant(self, *, empty=False):
        """Retain truthful new hashes for a deliberately empty or truncated feed."""
        target = self.root / ("empty" if empty else "truncated"); target.mkdir()
        manifest = json.loads(self.manifest.read_text())
        manifest["repository_commit"] = ("b" if empty else "c") * 40
        for entry in manifest["files"]:
            with (self.manifest.parent / entry["file"]).open() as source:
                reader = csv.DictReader(source); fields = reader.fieldnames; rows = list(reader)
            if empty:
                rows = []
            elif entry["file"] == "ufc_event_details.csv":
                rows = rows[:1]
            elif entry["file"] in {"ufc_fight_results.csv", "ufc_fight_stats.csv"}:
                rows = [r for r in rows if r["EVENT"] == "First"]
            elif entry["file"] == "ufc_fighter_tott.csv":
                rows = rows[:2]
            path = target / entry["file"]
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
            entry.update(sha256=ops._sha(path), bytes=path.stat().st_size)
        path = target / "manifest.json"; path.write_text(json.dumps(manifest)); return path

    def test_offline_refresh_is_idempotent_and_reverifies_inputs(self):
        source_before = self.manifest.read_bytes(); history_before = self.history.read_bytes()
        with patch.object(ops.statistics_workflow, "refresh_archive") as network:
            first = self.refresh(); pointer = self.pointer()
            second = self.refresh()
        network.assert_not_called()
        self.assertEqual(first["status"], "succeeded")
        self.assertFalse(first["reused_version"]); self.assertTrue(second["reused_version"])
        self.assertEqual(first["coverage"]["cards"], 3)
        self.assertEqual(first["coverage"]["usable_paired_bouts"], 3)
        self.assertEqual(pointer, self.pointer())
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(source_before, self.manifest.read_bytes())
        self.assertEqual(history_before, self.history.read_bytes())
        self.assertFalse(first["model_retraining"])
        quality = next(s for s in second["stages"] if s["name"] == "quality")
        self.assertTrue(quality["passed"]); self.assertEqual(len(quality["checks"]), 7)
        self.alert.assert_not_called()

    def test_mysql_failure_has_nonzero_status_alert_and_preserves_ready_pointer(self):
        self.refresh(); before = self.pointer()
        with patch.object(ops, "_publish_mysql", side_effect=RuntimeError("secret-password-DO-NOT-LOG")):
            result = self.refresh(mysql=True)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["failed_stage"], "mysql")
        self.assertEqual(before, self.pointer())
        self.alert.assert_called_once()
        self.assertEqual(self.alert.call_args.kwargs, {"local": True, "webhook": False})
        for receipt in self.ops_root.glob("runs/*/run.json"):
            self.assertNotIn("secret-password", receipt.read_text())

    def test_mysql_publication_key_is_stable_across_run_ids(self):
        with patch.object(ops, "_publish_mysql", return_value={"status": "published", "counts": {"bouts": 3}}) as publish:
            first = self.refresh(mysql=True); second = self.refresh(mysql=True)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(publish.call_args_list[0].args, publish.call_args_list[1].args)
        self.assertTrue(publish.call_args.args[1].startswith("statistics-"))

    def test_empty_feed_cannot_activate_or_publish_even_with_retention_disabled(self):
        self.manifest = self.variant(empty=True)
        with patch.object(ops, "_publish_mysql") as publish:
            result = self.refresh(mysql=True, min_retained_fraction=0)
        self.assertEqual(result["failed_stage"], "quality")
        self.assertEqual(result["exit_code"], 1)
        publish.assert_not_called()
        self.assertFalse((self.ops_root / "latest-ready.json").exists())
        quality = next(s for s in result["stages"] if s["name"] == "quality")
        self.assertFalse(quality["passed"])
        self.assertEqual(quality["observed_counts"]["cards"], 0)

    def test_shrunken_feed_is_quarantined_then_explicit_reset_can_activate(self):
        self.refresh(); before = self.pointer()
        self.manifest = self.variant()
        with patch.object(ops, "_publish_mysql") as publish:
            result = self.refresh(mysql=True)
        publish.assert_not_called()
        self.assertEqual(result["failed_stage"], "quality")
        self.assertEqual(before, self.pointer())
        quality = next(s for s in result["stages"] if s["name"] == "quality")
        self.assertEqual(quality["previous_counts"]["cards"], 3)
        self.assertEqual(quality["observed_counts"]["cards"], 1)
        self.assertFalse(quality["individual_quarantined_bouts_are_fatal"])
        reset = self.refresh(min_retained_fraction=0)
        self.assertEqual(reset["status"], "succeeded")
        self.assertNotEqual(before, self.pointer())

    def test_tampered_version_fails_before_reuse_or_publication(self):
        result = self.refresh(); before = self.pointer()
        coverage = self.ops_root / result["version"] / "coverage.csv"
        coverage.write_bytes(coverage.read_bytes() + b"changed")
        with patch.object(ops, "_publish_mysql") as publish:
            failure = self.refresh(mysql=True)
        self.assertEqual(failure["failed_stage"], "previous_snapshot")
        self.assertEqual(before, self.pointer()); publish.assert_not_called()

    def test_another_process_cannot_run_while_lock_is_held(self):
        self.refresh(); before = self.pointer()
        config = self.root / "private.json"; config.write_text("{}"); config.chmod(0o600)
        with ops._single_run(self.ops_root, "owner"):
            child = subprocess.run([sys.executable, "-B", "-m", "ufc_odds_model.pipeline_ops", "refresh",
                                    "--ops-root", str(self.ops_root), "--archive", str(self.manifest),
                                    "--config", str(config), "--no-local-alert", "--no-webhook-alert", "--no-mysql"],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 75, child.stderr)
        self.assertEqual(json.loads(child.stdout)["exit_code"], 75)
        self.assertEqual(before, self.pointer())

    def test_journal_failure_after_activation_rolls_pointer_back(self):
        self.refresh(); before = self.pointer(); self.manifest = self.variant()
        save = ops._Journal.save; injected = False
        def fail_once(journal):
            nonlocal injected
            if journal.value["status"] == "succeeded" and not injected:
                injected = True; raise OSError("unavailable receipt storage")
            return save(journal)
        with patch.object(ops._Journal, "save", fail_once):
            failure = self.refresh(min_retained_fraction=0)
        self.assertEqual(failure["exit_code"], 1)
        self.assertTrue(injected); self.assertEqual(before, self.pointer())

    def test_cli_failure_returns_nonzero_and_sanitized_summary(self):
        with patch.object(ops, "DEFAULT_CONFIG", self.root / "no-default-config"), contextlib.redirect_stdout(io.StringIO()) as output:
            result = ops.main(["refresh", "--ops-root", str(self.ops_root), "--archive", str(self.root / "absent")])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")
        self.alert.assert_called_once()

    def test_private_json_config_and_fraction_validation(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"environment": {"UFC_MYSQL_PASSWORD": "private"},
                                    "options": {"mysql": True, "min_retained_fraction": 0.95}})); path.chmod(0o600)
        environment, options = ops.read_config(path)
        self.assertEqual(environment["UFC_MYSQL_PASSWORD"], "private")
        self.assertEqual(options["min_retained_fraction"], 0.95)
        path.chmod(0o644)
        with self.assertRaises(ValueError): ops.read_config(path)
        for bad in (-0.01, 1.01, float("nan"), True, "0.9"):
            with self.assertRaises(ValueError): ops._retained_fraction(bad)


class FailureAlertTests(unittest.TestCase):
    def test_default_alert_uses_local_notification_and_never_webhook(self):
        run = {"run_id": "safe-run", "failed_stage": "quality"}
        with patch.object(ops.sys, "platform", "darwin"), patch.object(ops.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as notify, patch.object(ops, "urlopen") as webhook, patch.dict(os.environ, {"UFC_PIPELINE_ALERT_WEBHOOK": "https://example.org/private-token"}):
            result = ops.send_failure_alerts(run)
        self.assertEqual(result, {"local": "sent", "webhook": "disabled"})
        self.assertEqual(notify.call_args.args[0][0], "/usr/bin/osascript")
        self.assertNotIn("private-token", " ".join(notify.call_args.args[0])); webhook.assert_not_called()


class RefreshScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.project = self.root / "project"
        for relative in (".venv/bin/python", "src/ufc_odds_model/pipeline_ops.py"):
            path = self.project / relative; path.parent.mkdir(parents=True, exist_ok=True); path.touch()
        self.config = self.root / "private.json"
        self.config.write_text(json.dumps({"environment": {"UFC_MYSQL_USER": "ufc", "UFC_MYSQL_PASSWORD": "secret-never-embed"}}))
        self.config.chmod(0o600); self.agents = self.root / "agents"

    def test_daily_local_plist_reads_secrets_at_runtime_and_dry_run_has_no_mutations(self):
        with patch.object(schedule, "DEFAULT_CONFIG", self.config):
            result = schedule.manage_schedule("install", project=self.project, dry_run=True, launch_agents=self.agents)
        definition = result["definition"]
        self.assertEqual(definition["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
        self.assertFalse(definition["RunAtLoad"])
        self.assertIn("--mysql", definition["ProgramArguments"])
        self.assertIn("--no-webhook-alert", definition["ProgramArguments"])
        self.assertIn(str(self.config), definition["ProgramArguments"])
        self.assertNotIn("secret-never-embed", plistlib.dumps(definition).decode())
        self.assertFalse(self.agents.exists())

    def test_install_status_uninstall_touch_only_owned_launchagent(self):
        calls = []
        def runner(arguments): calls.append(arguments); return SimpleNamespace(returncode=0)
        schedule.manage_schedule("install", project=self.project, config=self.config, mysql=True, launch_agents=self.agents, runner=runner)
        own = self.agents / (schedule.LABEL + ".plist")
        other = self.agents / "com.unrelated.agent.plist"; other.write_text("untouched")
        self.assertTrue(own.is_file())
        status = schedule.manage_schedule("status", launch_agents=self.agents, runner=runner)
        self.assertTrue(status["installed"]); self.assertTrue(status["loaded"])
        schedule.manage_schedule("uninstall", launch_agents=self.agents, runner=runner)
        self.assertFalse(own.exists()); self.assertEqual(other.read_text(), "untouched")
        self.assertTrue(all(schedule.LABEL in " ".join(c) for c in calls))

    def test_failed_install_restores_previous_plist(self):
        self.agents.mkdir(); own = self.agents / (schedule.LABEL + ".plist"); own.write_bytes(b"previous")
        count = 0
        def runner(arguments):
            nonlocal count
            if arguments[1] == "bootstrap": count += 1
            return SimpleNamespace(returncode=1 if count == 1 else 0)
        with self.assertRaises(RuntimeError):
            schedule.manage_schedule("install", project=self.project, config=self.config, launch_agents=self.agents, runner=runner)
        self.assertEqual(own.read_bytes(), b"previous")


if __name__ == "__main__":
    unittest.main()
