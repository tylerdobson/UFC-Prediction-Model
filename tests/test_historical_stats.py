"""Canonical per-round history, explicit gaps, and read-only lookup contracts."""
import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from ufc_odds_model.historical_stats import HistoryStore, build_history, elapsed_time, export_coverage


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def fixture(root):
    archive = root / "archive"; archive.mkdir()
    events = [{"EVENT": name, "DATE": day, "URL": f"http://ufcstats.com/event-details/{i:016x}"}
              for i, name, day in [(1, "First", "January 01, 2020"), (2, "Second", "February 01, 2020"), (3, "Recent", "January 01, 2026")]]
    results, rounds = [], []
    for i, event, names, end, stopped, fmt in [(1, "First", ("Alpha One", "Beta Two"), 2, "2:00", "3 Rnd (5-5-5)"),
                                               (2, "Second", ("Alpha One", "Gamma Three"), 1, "5:00", "3 Rnd (5-5-5)"),
                                               (3, "Recent", ("Beta Two", "Gamma Three"), 1, "2:00", "Unknown schedule")]:
        results.append({"EVENT": event, "BOUT": " vs. ".join(names), "OUTCOME": "W/L", "WEIGHTCLASS": "Lightweight Bout", "ROUND": str(end), "TIME": stopped, "TIME FORMAT": fmt, "URL": f"http://ufcstats.com/fight-details/{i:016x}"})
        for name in names:
            for rnd in range(1, end + 1):
                rounds.append({"EVENT": event, "BOUT": " vs. ".join(names), "ROUND": f"Round {rnd}", "FIGHTER": name,
                               "KD": "1.0", "SIG.STR.": "10 of 20", "TOTAL STR.": "12 of 25", "TD": "1 of 2", "SUB.ATT": "0.0", "REV.": "0.0", "CTRL": "" if i == 1 and name == "Alpha One" and rnd == 1 else "0:20",
                               "HEAD": "5 of 10", "BODY": "3 of 6", "LEG": "2 of 4", "DISTANCE": "6 of 12", "CLINCH": "3 of 6", "GROUND": "1 of 2"})
    results.append({**results[0], "EVENT": "Alias of First"})
    profiles = [{"FIGHTER": n, "REACH": '70"', "URL": f"http://ufcstats.com/fighter-details/{i:016x}"} for i, n in [(1, "Alpha One"), (2, "Beta Two"), (3, "Gamma Three")]]
    for name, rows in [("ufc_event_details.csv", events), ("ufc_fight_results.csv", results), ("ufc_fight_stats.csv", rounds), ("ufc_fighter_tott.csv", profiles)]:
        write_csv(archive / name, rows)
    manifest = {"schema_version": 1, "source": "retained-test-archive", "captured_at_utc": "2026-10-19T00:00:00Z", "repository_commit": "a" * 40, "repository_url": "https://example.org/archive", "files": []}
    for path in archive.iterdir():
        manifest["files"].append({"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source_url": "https://example.org/" + path.name, "bytes": path.stat().st_size})
    manifest_path = archive / "manifest.json"; manifest_path.write_text(json.dumps(manifest))
    history = root / "research.sqlite"
    with sqlite3.connect(history) as c:
        c.executescript("CREATE TABLE fighters(fighter_id TEXT,canonical_name TEXT); CREATE TABLE events(event_id TEXT,event_date TEXT,source TEXT,status TEXT); CREATE TABLE bouts(bout_id TEXT,event_id TEXT,fighter_a_id TEXT,fighter_b_id TEXT,source TEXT,status TEXT); CREATE TABLE results(bout_id TEXT,outcome TEXT,winner_fighter_id TEXT);")
        c.executemany("INSERT INTO fighters VALUES(?,?)", [("wikipedia:1", "Alpha One"), ("wikipedia:2", "Beta Two"), ("wikipedia:3", "Gamma Three")])
        c.executemany("INSERT INTO events VALUES(?,?,?,?)", [("research:first", "2020-01-01", "wikipedia_research", "completed"), ("research:second", "2020-02-02", "wikipedia_research", "completed")])
        c.executemany("INSERT INTO bouts VALUES(?,?,?,?,?,?)", [("research:b1", "research:first", "wikipedia:1", "wikipedia:2", "wikipedia_research", "completed"), ("research:b2", "research:second", "wikipedia:1", "wikipedia:3", "wikipedia_research", "completed")])
        c.executemany("INSERT INTO results VALUES(?,?,?)", [("research:b1", "win", "wikipedia:1"), ("research:b2", "win", "wikipedia:1")])
    return manifest_path, history


class HistoricalStatisticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest, self.history = fixture(self.root)
        self.database = self.root / "stats.sqlite"

    def build(self):
        return build_history(self.manifest, self.database, self.history)

    def test_alias_dedup_round_totals_null_control_and_prior_date(self):
        result = self.build()
        self.assertEqual(result["counts"]["canonical_archive_bouts"], 3)
        self.assertEqual(result["counts"]["duplicate_fight_url_rows_removed"], 1)
        with HistoryStore(self.database) as store:
            self.assertEqual(store.prior_fighter_bouts("wikipedia:1", "2020-01-01"), ())
            first = store.prior_fighter_bouts("wikipedia:1", "2020-02-01")
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0].totals["sig_landed"], 20)
            self.assertEqual(first[0].opponent_totals["sig_attempted"], 40)
            self.assertIsNone(first[0].totals["control_seconds"])
            self.assertEqual(first[0].elapsed_seconds, 420)
            self.assertAlmostEqual(store.fighter_profile("wikipedia:1")["reach_cm"], 177.8)
            self.assertIsNone(store.fighter_profile("provisional:alpha"))
            self.assertEqual(store.fighter_aggregate("wikipedia:1", "2021-01-01")["missing_fields"]["control_seconds"], 1)

    def test_archive_only_recent_counts_retained_without_target_extension(self):
        self.build()
        with HistoryStore(self.database) as store:
            self.assertEqual(len(store.research_bout_records()), 2)
            self.assertEqual(len(store.all_research_fighter_bouts()), 6)
            recent = store.prior_fighter_bouts("wikipedia:2", "2027-01-01")[-1]
            self.assertIsNone(recent.elapsed_seconds)
            self.assertEqual(recent.totals["sig_landed"], 10)
            self.assertIsNone(recent.research_bout_id)
            self.assertFalse(store.metadata["archive_target_labels_added_to_research"])
            hold = store.connection.execute("SELECT reason FROM research_holds WHERE research_bout_id='research:b2'").fetchone()[0]
            self.assertEqual(hold, "near_date_pair_requires_explicit_exclusion")

    def test_roster_coverage_is_read_only_and_does_not_export_outcomes(self):
        self.build()
        before = self.database.read_bytes()
        with HistoryStore(self.database) as store:
            event = store.event("research:first")
            self.assertEqual(event["name"], "First")
            bout = store.event_bouts("research:first")[0]
            self.assertNotIn("outcome", bout); self.assertNotIn("winner_fighter_id", bout)
            self.assertEqual(len(store.event_coverage()), 3)
            self.assertEqual(len(store.fighter_coverage()), 3)
        self.assertEqual(self.database.read_bytes(), before)
        report = export_coverage(self.database, self.root / "coverage.json", self.root / "coverage.csv")
        self.assertEqual(len(report["events"]), 3)
        self.assertTrue((self.root / "coverage.csv").read_text().startswith("event_id,"))

    def test_build_is_idempotent_and_checks_source_and_content_integrity(self):
        self.build(); before = self.database.read_bytes()
        self.assertTrue(self.build()["idempotent_reuse"])
        self.assertEqual(self.database.read_bytes(), before)
        with sqlite3.connect(self.database) as con:
            con.execute("UPDATE fighters SET reach_cm=180 WHERE name='Alpha One'")
        with self.assertRaisesRegex(ValueError, "content hash"):
            HistoryStore(self.database)
        source = self.manifest.parent / "ufc_fight_stats.csv"
        source.write_bytes(source.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Archive bytes changed"):
            build_history(self.manifest, self.root / "changed.sqlite", self.history)

    def test_duration_schedule_validation_and_overtime(self):
        self.assertEqual(elapsed_time("2", "2:00", "3 Rnd (5-5-5)")[0], 420)
        self.assertEqual(elapsed_time("2", "2:00", "1 Rnd + OT (12-3)")[0], 840)
        self.assertEqual(elapsed_time("1", "15:30", "No Time Limit")[0], 930)
        self.assertIsNone(elapsed_time("2", "6:00", "3 Rnd (5-5-5)")[0])
        self.assertIsNone(elapsed_time("3", "2:00", "1 Rnd + OT (12-3)")[0])

    def test_failed_new_build_leaves_no_partial_database_or_changes_to_inputs(self):
        before = self.history.read_bytes()
        with patch("ufc_odds_model.historical_stats._link_research", side_effect=ValueError("injected failure")):
            with self.assertRaisesRegex(ValueError, "injected failure"):
                self.build()
        self.assertFalse(self.database.exists())
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(self.build()["counts"]["usable_paired_bouts"], 3)

    def test_optional_invalid_control_does_not_drop_paired_counts(self):
        path = self.manifest.parent / "ufc_fight_stats.csv"
        with path.open() as fp:
            rows = list(csv.DictReader(fp))
        rows[0]["CTRL"] = "9:00"  # Above actual five-minute first-round duration.
        write_csv(path, rows)
        manifest = json.loads(self.manifest.read_text())
        for entry in manifest["files"]:
            if entry["file"] == path.name:
                entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))
        self.build()
        with HistoryStore(self.database) as store:
            record = store.prior_fighter_bouts("wikipedia:1", "2020-02-01")[0]
            self.assertEqual(record.totals["sig_landed"], 20)
            self.assertIsNone(record.totals["control_seconds"])


if __name__ == "__main__":
    unittest.main()
