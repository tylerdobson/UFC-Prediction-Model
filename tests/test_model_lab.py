"""Fixed dates, orientation, subset alignment, and immutable outputs."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.features import FEATURE_NAMES, FeatureRow
from ufc_odds_model.model_lab import (
    compare, fixed_split, hash_oriented_pairs, metric_report, read_only, write_new_json,
)


def row(bout_id, event_date, target=1):
    return FeatureRow(bout_id, event_date, event_date, "a", "b", (0.,) * len(FEATURE_NAMES), target)


class ModelLabTests(unittest.TestCase):
    def test_split_does_not_move_when_more_fights_are_added(self):
        rows = [row("a", "2021-03-13"), row("b", "2021-03-20"),
                row("c", "2023-12-02"), row("d", "2023-12-09")]
        train, validation, test = fixed_split(rows + [row("extra", "2023-12-10")] * 50)
        self.assertEqual([r.bout_id for r in train], ["a"])
        self.assertEqual([r.bout_id for r in validation], ["b", "c"])
        self.assertEqual(test[0].bout_id, "d")
        with self.assertRaisesRegex(ValueError, "split gap"):
            fixed_split(rows + [row("gap", "2021-03-15")])

    def test_hash_orientation_and_favorites_preserve_losses(self):
        rows = [row(str(i), "2024-01-01", i % 2) for i in range(20)]
        probabilities = [.2 if i % 3 else .7 for i in range(20)]
        p, y = hash_oriented_pairs(rows, probabilities)
        for original, old_p, new_p, new_y in zip(rows, probabilities, p, y):
            flip = int(hashlib.sha256(original.bout_id.encode()).hexdigest(), 16) & 1
            self.assertEqual(new_p, 1 - old_p if flip else old_p)
            self.assertEqual(new_y, 1 - original.target if flip else original.target)
        report = metric_report(rows, probabilities)
        self.assertEqual(sum(b["bouts"] for b in report["favorite_reliability"]), 20)
        self.assertEqual(report["exact_probability_ties"], 0)
        with self.assertRaises(ValueError):
            hash_oriented_pairs(rows, [])

    def test_read_only_and_report_never_replace_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE example (value)")
            before = path.read_bytes()
            with read_only(path) as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("INSERT INTO example VALUES (1)")
            self.assertEqual(before, path.read_bytes())
            output = Path(directory) / "out.json"
            write_new_json(output, {"first": True})
            with self.assertRaises(FileExistsError):
                write_new_json(output, {"overwrite": True})
            self.assertEqual(json.loads(output.read_text()), {"first": True})

    def test_originally_kept_subset_uses_the_same_fitted_model(self):
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            db.init_db(connection)
            for fighter in ("a", "b"):
                db.upsert_fighter(connection, fighter, fighter)
            for i, day in enumerate(("2020-01-01", "2021-04-01", "2024-01-01", "2024-01-02")):
                event, bout = f"e{i}", f"b{i}"
                db.upsert_event(connection, event, event, day, "completed", source="wikipedia_research")
                db.upsert_bout(connection, bout, event, "a", "b", "completed", source="wikipedia_research")
                db.upsert_result(connection, bout, "win", "a" if i % 2 else "b", day + "T23:00:00Z")
            report = compare(connection, originally_kept={"b2"})
            self.assertEqual(report["test"]["all"]["elo"]["metrics"]["bouts"], 2)
            self.assertEqual(report["test"]["originally_kept"]["elo"]["metrics"]["bouts"], 1)
            self.assertFalse(report["promotion_eligible"])
            self.assertEqual(report["test"]["all"]["market_no_vig"]["status"], "unavailable")
