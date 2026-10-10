"""Fixed dates, orientation, subset alignment, and immutable outputs."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.features import FEATURE_NAMES, FeatureRow
from ufc_odds_model.model_lab import (
    compare, fixed_split, hash_oriented_pairs, metric_report, read_only, write_new_json,
    rolling_origin, rolling_comparison, absolute_bootstrap,
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

    def test_rolling_origin_never_fits_on_current_or_later_year(self):
        examples = []
        for year in range(2010,2019):
            for month in range(1,13):
                day = f"{year}-{month:02d}-01"
                examples.append(FeatureRow(day,day,day,"a","b",(.3 if month%2 else -.2,),month%2))
        first, _, _ = rolling_origin(examples,("x",),years=[2016,2017],intervals=False)
        changed = [replace(r,target=1-int(r.target)) if r.event_date >= "2017-01-01" else r for r in examples]
        second, _, _ = rolling_origin(changed,("x",),years=[2016,2017],intervals=False)
        self.assertEqual(first["folds"][0],second["folds"][0])
        for fold in first["folds"]:
            self.assertLess(fold["train"]["last_date"],fold["validation"]["first_date"])
            self.assertLess(fold["validation"]["last_date"],f"{fold['year']}-01-01")

    def test_bootstrap_is_deterministic_and_resamples_whole_dates(self):
        rows = [row(str(i),"2024-01-01",i%2) for i in range(10)]
        result = absolute_bootstrap(rows,[.7]*10,repeats=50)
        self.assertEqual(result["event_date_blocks"],1)
        low,high = result["log_loss_95pct_interval"]
        self.assertEqual(low,high)

    def test_rolling_kept_baselines_use_the_same_bouts_as_the_kept_model(self):
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory=sqlite3.Row; db.init_db(connection)
            for fighter in ('a','b'):
                db.upsert_fighter(connection,fighter,fighter)
            for i,day in enumerate(('2011-01-01','2012-01-01','2013-01-01','2024-01-01','2024-01-02')):
                event,bout=f'e{i}',f'b{i}'
                db.upsert_event(connection,event,event,day,'completed',source='wikipedia_research')
                db.upsert_bout(connection,bout,event,'a','b','completed',source='wikipedia_research')
                db.upsert_result(connection,bout,'win','a' if i%2 else 'b',day+'T23:00:00Z')
            report=rolling_comparison(connection,elo_k=64,start_year=2010,originally_kept={'b3'})
            fold=next(f for f in report['folds'] if f['year']==2024)
            self.assertEqual(report['pooled']['metrics']['bouts'],2)
            for kept in (fold['originally_kept'],report['originally_kept']):
                self.assertEqual(kept['metrics']['bouts'],1)
                for name in ('coin_flip','elo','logistic_v2'):
                    self.assertEqual(kept['baselines'][name]['metrics']['bouts'],1)
                    self.assertEqual(kept['baselines'][name]['uncertainty']['event_date_blocks'],1)
                self.assertEqual(kept['baselines']['market_no_vig']['status'],'unavailable')
                self.assertEqual(kept['baselines']['younger_fighter_wins']['bouts'],0)
