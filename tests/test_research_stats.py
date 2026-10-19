"""Retained per-fight replay, conservative joins, and one frozen V5 candidate."""

import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.model_lab import VALIDATION_END, file_sha256, read_only
from ufc_odds_model import research_stats as stats


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def archive_fixture(root):
    database = root / "history.sqlite"
    archive = root / "archive"; archive.mkdir()
    events, results, rounds = [], [], []
    with db.connect(database) as connection:
        db.init_db(connection)
        for fighter, name in (("wikipedia:1", "Alpha One"), ("wikipedia:2", "Beta Two")):
            db.upsert_fighter(connection, fighter, name)
        for year in range(2010, 2027):
            for month in (1, 3, 5, 7, 9, 11):
                day = f"{year}-{month:02d}-01"; event = f"Event {day}"
                winner = "wikipedia:1" if (year + month) % 3 else "wikipedia:2"
                db.upsert_event(connection, day, event, day, "completed", source="wikipedia_research")
                db.upsert_bout(connection, day, day, "wikipedia:1", "wikipedia:2", "completed", source="wikipedia_research")
                db.upsert_result(connection, day, "win", winner, day + "T23:00:00Z")
                events.append({"EVENT": event, "DATE": datetime.strptime(day, "%Y-%m-%d").strftime("%B %d, %Y"),
                               "URL": "http://ufcstats.com/event-details/0000000000000001"})
                results.append({"EVENT": event, "BOUT": "Alpha One vs. Beta Two", "OUTCOME": "W/L" if winner == "wikipedia:1" else "L/W",
                                "ROUND": "1", "URL": "http://ufcstats.com/fight-details/0000000000000002"})
                for name, value in (("Alpha One", "40 of 80"), ("Beta Two", "20 of 60")):
                    rounds.append({"EVENT": event, "BOUT": "Alpha One vs. Beta Two", "ROUND": "Round 1", "FIGHTER": name, "SIG.STR.": value})
        connection.commit()
    write_csv(archive / stats.SOURCE_FILES[0], events)
    write_csv(archive / stats.SOURCE_FILES[1], results)
    write_csv(archive / stats.SOURCE_FILES[2], rounds)
    write_csv(archive / stats.SOURCE_FILES[3], [
        {"FIGHTER": "Alpha One", "REACH": '70"', "URL": "http://ufcstats.com/fighter-details/0000000000000001"},
        {"FIGHTER": "Beta Two", "REACH": '75"', "URL": "http://ufcstats.com/fighter-details/0000000000000002"},
    ])
    manifest = {"schema_version": 1, "source": "retained-test-archive", "repository_url": "https://example.org/archive",
                "repository_commit": "a" * 40, "captured_at_utc": "2026-10-01T00:00:00Z"}
    path = archive / "manifest.json"
    refresh_manifest(path, manifest)
    return database, path


def refresh_manifest(path, manifest=None):
    manifest = manifest or json.loads(path.read_text())
    manifest["files"] = [{"file": name, "sha256": file_sha256(path.parent / name),
                          "source_url": "https://example.org/" + name, "bytes": (path.parent / name).stat().st_size}
                         for name in stats.SOURCE_FILES]
    path.write_text(json.dumps(manifest))


class ResearchStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database, self.manifest = archive_fixture(self.root)

    def load(self, through=None):
        with read_only(self.database) as connection:
            return stats.load_research_stats(connection, self.manifest, through_date=through)

    def rewrite(self, name, alter):
        path = self.manifest.parent / name
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        alter(rows)
        write_csv(path, rows)
        refresh_manifest(self.manifest)

    def test_archive_bytes_are_verified_before_use(self):
        path = self.manifest.parent / stats.SOURCE_FILES[2]
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "changed statistics source"):
            self.load()

    def test_exact_pair_date_outcome_and_complete_rounds_are_required(self):
        initial = self.load()
        self.assertEqual(len(initial.stats), 204)
        self.assertAlmostEqual(initial.profiles["wikipedia:1"].reach_cm, 177.8)
        self.rewrite(stats.SOURCE_FILES[1], lambda rows: rows[0].update(OUTCOME="L/W"))
        mismatched = self.load()
        self.assertEqual(len(mismatched.stats), 202)
        self.assertEqual(mismatched.source_summary["held"]["outcome_disagreement"], 1)
        self.rewrite(stats.SOURCE_FILES[2], lambda rows: rows.pop(3))
        incomplete = self.load()
        self.assertEqual(len(incomplete.stats), 200)
        self.assertEqual(incomplete.source_summary["held"]["missing_or_incomplete_paired_round_stats"], 1)

    def test_duplicate_date_pair_and_duplicate_round_are_held(self):
        self.rewrite(stats.SOURCE_FILES[1], lambda rows: rows.append(dict(rows[0])))
        duplicate_pair = self.load()
        self.assertNotIn(("2010-01-01", "wikipedia:1"), duplicate_pair.stats)
        self.rewrite(stats.SOURCE_FILES[2], lambda rows: rows.append(dict(rows[2])))
        duplicate_round = self.load()
        self.assertNotIn(("2010-03-01", "wikipedia:1"), duplicate_round.stats)

    def test_names_are_exact_and_not_fuzzily_merged(self):
        self.rewrite(stats.SOURCE_FILES[1], lambda rows: rows[0].update(BOUT="Alpha Ones vs. Beta Two"))
        overlay = self.load()
        self.assertNotIn(("2010-01-01", "wikipedia:1"), overlay.stats)

    def test_future_statistics_do_not_change_pretest_overlay_or_earlier_features(self):
        before = self.load(VALIDATION_END)
        with read_only(self.database) as connection:
            old, _, _ = stats.stats_feature_rows(connection, before, through_date=VALIDATION_END)
        def change_future(rows):
            for row in rows:
                if "2025" in row["EVENT"]:
                    row["SIG.STR."] = "900 of 1000"
        self.rewrite(stats.SOURCE_FILES[2], change_future)
        after = self.load(VALIDATION_END)
        self.assertEqual(before.overlay_inputs_sha256, after.overlay_inputs_sha256)
        with read_only(self.database) as connection:
            new, _, _ = stats.stats_feature_rows(connection, after, through_date=VALIDATION_END)
        self.assertEqual(old, new)

    def test_current_bout_counts_are_withheld_until_later_dates(self):
        overlay = self.load()
        with read_only(self.database) as connection:
            first, _, names = stats.stats_feature_rows(connection, overlay)
        self.assertFalse(first[0].coverage[4])
        self.assertFalse(first[0].coverage[5])
        self.assertTrue(first[1].coverage[4] and first[1].coverage[5])
        self.assertNotEqual(first[1].features[names.index("opponent_adjusted_sig_strike_accuracy_difference")], 0)
        self.assertNotEqual(first[0].features[names.index("reach_cm_difference_20")], 0)
        self.rewrite(stats.SOURCE_FILES[2], lambda rows: rows[0].update(**{"SIG.STR.": "70 of 80"}))
        changed = self.load()
        with read_only(self.database) as connection:
            second, _, _ = stats.stats_feature_rows(connection, changed)
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1].features, second[1].features)

    def test_single_candidate_is_frozen_before_evaluation_and_reports_match(self):
        before = file_sha256(self.database)
        selection_path, report_path = self.root / "selection.json", self.root / "evaluation.json"
        original_loader = stats.load_research_stats
        with patch.object(stats, "load_research_stats", wraps=original_loader) as loader:
            selection = stats.select_stats_upgrade(self.database, self.manifest, selection_path)
            self.assertEqual(loader.call_args.kwargs["through_date"], VALIDATION_END)
        self.assertEqual(selection["candidate_count"], 1)
        self.assertFalse(selection["fixed_test_labels_used"])
        self.assertFalse(selection["pretest_coverage_sufficient"])  # Fewer than 100 fixture bouts.
        report = stats.evaluate_stats_upgrade(self.database, self.manifest, selection_path, report_path)
        verified = stats.load_verified_stats_report(report_path, selection_path, self.database, self.manifest)
        self.assertEqual(verified["research_champion"], "logistic-prior-v4")
        self.assertEqual(report["fixed_test"]["baseline"]["metrics"]["bouts"], report["fixed_test"]["candidate"]["metrics"]["bouts"])
        self.assertIn("both_reach_and_prior_paired_stats", report["fixed_test"]["coverage_cohorts"])
        self.assertEqual(file_sha256(self.database), before)
        with self.assertRaises(FileExistsError):
            stats.select_stats_upgrade(self.database, self.manifest, selection_path)
        with self.assertRaises(FileExistsError):
            stats.evaluate_stats_upgrade(self.database, self.manifest, selection_path, report_path)

    def test_evaluation_cannot_read_full_labels_without_a_saved_selection(self):
        with patch.object(stats, "load_research_stats") as loader:
            with self.assertRaises(ValueError):
                stats.evaluate_stats_upgrade(self.database, self.manifest, self.root / "missing.json", self.root / "out.json")
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
