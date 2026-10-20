"""Measured style and prior opponents must not depend on target/future outcomes."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ufc_odds_model.fight_context import FightContextHistory, build_fighter_context, build_matchup_context, method_group
from ufc_odds_model.historical_stats import HistoryStore, build_history
from ufc_odds_model.portfolio_demo import FIXTURE_DIRECTORY


A, B, C, D = ["ufcstats:fighter:ffffffffffffff0" + str(n) for n in range(1, 5)]
CUTOFF = "2024-06-02T00:00:00Z"


class FightContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "statistics.sqlite"
        build_history(FIXTURE_DIRECTORY / "fixture_manifest.json", self.path)
        with sqlite3.connect(self.path) as connection:
            methods = {"01": ("KO/TKO", "Punches"), "02": ("Submission", "Kimura"),
                       "03": ("TKO - Doctor's Stoppage", "Eye injury"), "04": ("DQ", "Foul"),
                       "05": ("Decision - Unanimous", "Judges"), "06": ("Submission", "Armbar")}
            for row in connection.execute("SELECT bout_id,raw_json FROM bouts").fetchall():
                raw = json.loads(row[1]); raw["METHOD"], raw["DETAILS"] = methods[row[0][-2:]]
                connection.execute("UPDATE bouts SET raw_json=? WHERE bout_id=?", (json.dumps(raw), row[0]))

    def context(self, fighter=A, **kwargs):
        # The synthetic test deliberately changes labels to check invariance;
        # real callers pass the default verified HistoryStore/facade snapshot.
        with HistoryStore(self.path, verify=False) as store:
            return build_fighter_context(store, fighter, target_date=kwargs.pop("target_date", "2024-07-01"),
                                         cutoff_at_utc=CUTOFF, **kwargs)

    def test_method_taxonomy_separates_medical_administrative_and_nonbinary_cases(self):
        cases = [("KO (punch)", "win", "ko_tko"), ("TKO (doctor stoppage)", "win", "doctor_stoppage"),
                 ("Submission (rear-naked choke)", "win", "submission"), ("DQ", "win", "disqualification"),
                 ("Could Not Continue", "win", "could_not_continue"), ("Decision (split)", "win", "decision_split"),
                 ("KO/TKO", "no_contest", "no_contest"), (None, "win", "unknown")]
        for method, outcome, expected in cases:
            with self.subTest(method=method):
                self.assertEqual(method_group(method, outcome), expected)

    def test_prior_rows_keep_results_methods_and_held_rows_without_feature_use(self):
        before = self.path.read_bytes()
        context = self.context(D)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([row["result"] for row in context["prior_fights"]], ["loss", "loss", "loss"])
        held = context["prior_fights"][1]
        self.assertEqual(held["method_group"], "disqualification")
        self.assertFalse(held["eligible_for_features"])
        self.assertIsNone(held["opponent_pre_bout_strength"])
        self.assertEqual(context["coverage"]["held_bouts"], 1)
        self.assertEqual(context["method_summary"]["methods"]["disqualification"]["losses"], 0)
        self.assertEqual(context["method_summary"]["methods"]["doctor_stoppage"]["losses"], 1)
        self.assertEqual(context["method_summary"]["methods"]["ko_tko"]["losses"], 0)
        self.assertEqual(context["measured_style"]["sig_landed_per_minute"]["bouts"], 1)

    def test_style_is_a_measured_share_and_preserves_unknown_duration(self):
        context = self.context()
        metric = context["measured_style"]["distance_attempt_share"]
        self.assertTrue(metric["observed"])
        self.assertAlmostEqual(metric["value"], metric["numerator"] / metric["denominator"])
        d = self.context(D)
        self.assertIn("duration_unavailable", d["measured_style"]["sig_landed_per_minute"]["missing_reasons"])
        self.assertNotIn("striker", context["candidate_measurements"])
        self.assertNotIn(str(Path(self.temporary.name)), json.dumps(context))

    def test_strength_is_from_before_prior_fight_and_same_date_rows_are_withheld(self):
        a = self.context()
        self.assertEqual(a["prior_fights"][0]["opponent_pre_bout_strength"]["archive_elo"], 1500.)
        self.assertEqual(a["prior_fights"][0]["opponent_pre_bout_strength"]["prior_record"]["scored_bouts"], 0)
        # Cedar's February win and Briar's March win are included; their
        # head-to-head result on the same May date cannot enter either record.
        b = self.context(B)
        may = b["prior_fights"][-1]["opponent_pre_bout_strength"]
        self.assertEqual(may["prior_record"]["wins"], 1)
        self.assertEqual(may["prior_record"]["losses"], 0)

    def test_target_future_and_near_date_mutations_cannot_change_prior_context(self):
        baseline = self.context(target_date="2024-05-03")
        self.assertEqual([row["event_date"] for row in baseline["prior_fights"]], ["2024-01-01", "2024-02-01"])
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE bouts SET winner_fighter_id=fighter_b_id,raw_json=? WHERE event_date>=?",
                               (json.dumps({"METHOD": "KO/TKO", "DETAILS": "Changed future label"}), "2024-05-01"))
        changed = self.context(target_date="2024-05-03")
        self.assertEqual(changed, baseline)

    def test_unknown_identity_and_swapped_matchups_preserve_missingness_and_antisymmetry(self):
        with HistoryStore(self.path, verify=False) as store:
            unknown = build_fighter_context(store, "unknown", target_date="2024-07-01", cutoff_at_utc=CUTOFF)
            self.assertEqual(unknown["status"], "unavailable")
            left = build_matchup_context(store, A, B, target_date="2024-07-01", cutoff_at_utc=CUTOFF)
            right = build_matchup_context(store, B, A, target_date="2024-07-01", cutoff_at_utc=CUTOFF)
        for key, value in left["untrained_feature_differences"].items():
            if value is None:
                self.assertIsNone(right["untrained_feature_differences"][key])
            else:
                self.assertAlmostEqual(value, -right["untrained_feature_differences"][key])

    def test_missing_methods_do_not_create_false_zero_finish_shares(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE bouts SET raw_json='{}'")
        context = self.context(A)
        self.assertGreater(context["method_summary"]["eligible_binary_bouts"], 0)
        self.assertEqual(context["method_summary"]["classified_method_binary_bouts"], 0)
        for name in ("ko_win_share", "ko_loss_share", "submission_win_share", "submission_loss_share"):
            self.assertIsNone(context["candidate_measurements"][name])

    def test_source_name_only_opponent_is_visible_but_not_feature_evidence(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE bouts SET fighter_b_id=NULL,stats_status='held',gap_reason='unresolved_opponent' WHERE bout_id LIKE '%01'")
        context = self.context(A)
        first = context["prior_fights"][0]
        self.assertEqual(first["result"], "win")
        self.assertEqual(first["opponent"], "Demo Briar Stone")
        self.assertIsNone(first["opponent_id"])
        self.assertIsNone(first["opponent_pre_bout_strength"])
        self.assertFalse(first["eligible_for_features"])
        self.assertEqual(context["coverage"]["held_bouts"], 1)

    def test_no_contest_is_visible_and_not_counted_as_a_ko_win(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE bouts SET outcome='no_contest',winner_fighter_id=NULL WHERE bout_id LIKE '%01'")
        context = self.context(A)
        self.assertEqual(context["prior_fights"][0]["method_group"], "no_contest")
        summary = context["method_summary"]
        self.assertEqual(summary["outcomes"]["no_contest"], 1)
        self.assertEqual(summary["methods"]["no_contest"]["bouts"], 1)
        self.assertEqual(summary["methods"]["ko_tko"]["wins"], 0)


if __name__ == "__main__":
    unittest.main()
