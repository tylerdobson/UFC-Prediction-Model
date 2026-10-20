"""Generic card reports use actual store APIs and protect historical cutoffs."""

import copy
import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import card_statistics as report
from ufc_odds_model.historical_stats import FILES, HistoryStore, build_history


def archive_fixture(root):
    archive = root / "archive"
    archive.mkdir()
    events, results, rounds = [], [], []
    names = {1: "Alpha One", 2: "Beta Two", 3: "Gamma Three"}
    rows = [(1, "2017-01-01", 1, 2, 60, 30, "10:00", "1 Rnd (10)"),
            (2, "2018-02-01", 1, 2, 20, 20, "02:00", "unknown"),
            (3, "2018-03-01", 1, 2, 900, 800, "05:00", "1 Rnd (5)"),
            (3, "2018-03-01", 1, 3, 700, 600, "05:00", "1 Rnd (5)"),
            (4, "2019-01-01", 1, 2, 800, 700, "05:00", "1 Rnd (5)")]
    for event, day in dict((row[0], row[1]) for row in rows).items():
        events.append({"EVENT": f"Card {event}", "DATE": datetime.strptime(day, "%Y-%m-%d").strftime("%B %d, %Y"),
                       "URL": f"http://ufcstats.com/event-details/{event:016x}"})
    for index, (event, day, a, b, landed_a, landed_b, time, schedule) in enumerate(rows, 1):
        bout = names[a] + " vs. " + names[b]
        results.append({"EVENT": f"Card {event}", "BOUT": bout, "OUTCOME": "W/L", "ROUND": "1", "TIME": time,
                        "TIME FORMAT": schedule, "URL": f"http://ufcstats.com/fight-details/{index:016x}", "WEIGHTCLASS": "Lightweight"})
        for fighter, landed, attempted, td in ((a, landed_a, landed_a * 2, "2 of 4"), (b, landed_b, 100 if landed_b <= 100 else landed_b * 2, "1 of 5")):
            row = {"EVENT": f"Card {event}", "BOUT": bout, "ROUND": "Round 1", "FIGHTER": names[fighter],
                   "SIG.STR.": f"{landed} of {attempted}", "TOTAL STR.": f"{landed} of {attempted}", "TD": td,
                   "KD": "1", "SUB.ATT": "2", "REV.": "0", "CTRL": "02:00" if index == 1 else "--"}
            for key in ("HEAD", "BODY", "LEG", "DISTANCE", "CLINCH", "GROUND"):
                row[key] = "1 of 2"
            rounds.append(row)
    profiles = [{"FIGHTER": name, "URL": f"http://ufcstats.com/fighter-details/{fighter:016x}", "REACH": '70"'} for fighter, name in names.items()]
    for filename, content in zip(FILES, (events, results, rounds, profiles)):
        with (archive / filename).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(content[0]))
            writer.writeheader()
            writer.writerows(content)
    manifest = {"schema_version": 1, "source": "test archive", "repository_url": "https://example.org/source",
                "repository_commit": "a" * 40, "captured_at_utc": "2026-10-19T00:00:00Z",
                "files": [{"file": filename, "sha256": hashlib.sha256((archive / filename).read_bytes()).hexdigest(),
                           "source_url": "https://example.org/source/" + filename, "bytes": (archive / filename).stat().st_size} for filename in FILES]}
    path = archive / "manifest.json"
    path.write_text(json.dumps(manifest))
    database = root / "statistics.sqlite"
    build_history(path, database)
    return database


class CardStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = archive_fixture(self.root)
        self.a = "ufcstats:fighter:0000000000000001"
        self.b = "ufcstats:fighter:0000000000000002"

    def build(self, **kwargs):
        return report.build_card_report(self.store, as_of="2026-10-19T12:00:00Z", **kwargs)

    def test_canonical_card_uses_only_earlier_fights_and_metric_specific_samples(self):
        before = self.store.read_bytes()
        result = self.build(event_id="0000000000000003")
        self.assertEqual(result["summary"]["bouts"], 2)
        alpha = result["bouts"][0]["fighter_a"]
        self.assertEqual(alpha["prior_stat_bouts"], 2)
        self.assertEqual(alpha["known_duration_bouts"], 1)
        self.assertAlmostEqual(alpha["metrics"]["significant_strikes_landed_per_minute"]["value"], 6)
        self.assertEqual(alpha["metrics"]["significant_strikes_landed_per_minute"]["contributing_bouts"], 1)
        self.assertAlmostEqual(alpha["metrics"]["significant_strike_accuracy_pct"]["value"], 50)
        self.assertEqual(alpha["metrics"]["significant_strike_accuracy_pct"]["contributing_bouts"], 2)
        self.assertAlmostEqual(alpha["metrics"]["significant_strike_defense_pct"]["value"], 100 * (1 - 50 / 200))
        self.assertTrue(all(f["event_date"] < "2018-03-01" for f in alpha["prior_fights"]))
        self.assertEqual(self.store.read_bytes(), before)
        self.assertNotIn('"outcome"', report.render_json(result))
        self.assertNotIn('"winner', report.render_json(result))

    def test_research_date_offset_cannot_leak_target_or_other_tournament_fights(self):
        history = self.root / "research.sqlite"
        with sqlite3.connect(history) as connection:
            connection.executescript("CREATE TABLE events(event_id,name,event_date); CREATE TABLE fighters(fighter_id,canonical_name); CREATE TABLE bouts(bout_id,event_id,fighter_a_id,fighter_b_id,weight_class,scheduled_rounds);")
            connection.execute("INSERT INTO events VALUES('target','Research date differs','2018-03-02')")
            connection.executemany("INSERT INTO fighters VALUES(?,?)", [(self.a, "Alpha One"), (self.b, "Beta Two")])
            connection.execute("INSERT INTO bouts VALUES('target-bout','target',?,?,'Lightweight',3)", (self.a, self.b))
        result = self.build(history=history, event_id="target")
        self.assertEqual(result["temporal_guard"], "two_day_whole_date_embargo")
        self.assertEqual(result["prior_fight_date_exclusive"], "2018-02-28")
        alpha = result["bouts"][0]["fighter_a"]
        self.assertEqual(alpha["prior_stat_bouts"], 2)
        self.assertAlmostEqual(alpha["metrics"]["significant_strikes_landed_per_minute"]["value"], 6)
        self.assertNotIn("2018-03-01", [row["event_date"] for row in alpha["prior_fights"]])

    def test_as_of_cutoff_excludes_all_of_its_own_date(self):
        result = report.build_card_report(self.store, event_id="ufcstats:event:0000000000000004", as_of="2018-02-01T23:59:59Z")
        alpha = result["bouts"][0]["fighter_a"]
        self.assertEqual(result["prior_fight_date_exclusive"], "2018-02-01")
        self.assertEqual(alpha["prior_stat_bouts"], 1)

    def test_future_manifest_keeps_unknown_identity_unavailable_without_name_matching(self):
        card = {"event": {"event_id": "new-event", "name": "Future card", "event_date": "2027-01-01"},
                "bouts": [{"bout_id": "new-bout", "fighter_a": {"name": "Alpha One", "fighter_id": self.a},
                           "fighter_b": {"name": "Beta Two"}}]}
        result = self.build(card=card)
        missing = result["bouts"][0]["fighter_b"]
        self.assertEqual(missing["status"], "unavailable")
        self.assertIsNone(missing["prior_stat_bouts"])
        self.assertIsNone(missing["metrics"]["significant_strike_accuracy_pct"]["value"])
        self.assertIn("names were not matched", missing["reason"])
        self.assertEqual(result["summary"]["unresolved_identities"], 1)

    def test_known_debutant_retains_zero_history_and_missing_rates(self):
        result = self.build(event_id="0000000000000003")
        gamma = result["bouts"][1]["fighter_b"]
        self.assertEqual(gamma["status"], "no_prior_statistics")
        self.assertEqual(gamma["prior_stat_bouts"], 0)
        self.assertIsNone(gamma["metrics"]["significant_strikes_landed_per_minute"]["value"])

    def test_exports_are_self_contained_escape_html_and_neutralize_csv_formulas(self):
        result = self.build(event_id="0000000000000003")
        result["bouts"][0]["fighter_a"]["name"] = "=Example<script>"
        markup = report.render_html(result)
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)
        self.assertIn("current athlete career averages are excluded", markup)
        self.assertIn("two-day whole-date embargo", markup)
        self.assertIn("retrospective", markup)
        self.assertNotIn(str(self.root), report.render_json(result))
        rows = list(csv.DictReader(io.StringIO(report.render_csv(result))))
        self.assertTrue(rows[0]["fighter"].startswith("'="))
        self.assertEqual(rows[0]["significant_strikes_landed_per_minute_contributing_bouts"], "1")
        self.assertIn("Missing counts are not zero", rows[0]["temporal_limitations"])

    def test_cli_creates_three_formats_and_preserves_existing_editions(self):
        directory = self.root / "edition"
        history = self.root / "empty-research.sqlite"
        with sqlite3.connect(history) as connection:
            connection.execute("CREATE TABLE events(event_id TEXT PRIMARY KEY,name TEXT,event_date TEXT)")
        args = ["--stats-db", str(self.store), "--history", str(history),
                "--event-id", "0000000000000003", "--out-dir", str(directory)]
        with patch("sys.stdout", io.StringIO()), patch(
            "ufc_odds_model.mysql_runtime.connect_server",
            side_effect=AssertionError("Fixture CLI must not use the default MySQL history"),
        ) as mysql:
            self.assertEqual(report.main(args), 0)
            mysql.assert_not_called()
        self.assertEqual({p.name for p in directory.iterdir()}, {"card-statistics.json", "card-statistics.html", "card-statistics.csv"})
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            report.main(args)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_bad_cutoff_unknown_event_and_changed_store_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            report.build_card_report(self.store, event_id="0000000000000003", as_of="2026-10-19")
        with self.assertRaisesRegex(ValueError, "not found"):
            self.build(event_id="0000000000000099")
        with sqlite3.connect(self.store) as connection:
            connection.execute("UPDATE fighters SET reach_cm=222")
        with self.assertRaisesRegex(ValueError, "content hash"):
            self.build(event_id="0000000000000003")


if __name__ == "__main__":
    unittest.main()
