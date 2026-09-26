"""Dated observations must not leak into an earlier prediction."""

from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.features import FEATURE_NAMES, event_feature_rows, training_rows
from ufc_odds_model.observations import (
    import_fight_stat_observations_csv,
    import_profile_observations_csv,
)
from ufc_odds_model.pipeline import parse_utc


LICENSE = "https://example.test/permission/reviewed-research"
EVIDENCE = "https://example.test/archive/snapshot-2024"


class ObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.connection)
        for fighter in "abc":
            db.upsert_fighter(self.connection, fighter, fighter.upper())
        db.upsert_event(
            self.connection, "e1", "Earlier", "2024-01-01", "completed",
            start_time_utc="2024-01-01T20:00:00Z",
        )
        db.upsert_bout(self.connection, "ab", "e1", "a", "b", "completed")
        db.upsert_result(self.connection, "ab", "win", "a", "2024-01-02T00:00:00Z")
        db.upsert_event(
            self.connection, "e2", "Second", "2024-01-10", "completed",
            start_time_utc="2024-01-10T06:00:00Z",
        )
        db.upsert_bout(self.connection, "ac", "e2", "a", "c", "completed")
        db.upsert_result(self.connection, "ac", "win", "a", "2024-01-10T09:00:00Z")
        db.upsert_event(self.connection, "e3", "Upcoming", "2024-01-20", "scheduled")
        db.upsert_bout(self.connection, "bc", "e3", "b", "c", "scheduled")
        db.upsert_event(self.connection, "e4", "Reverse", "2024-01-20", "scheduled")
        db.upsert_bout(self.connection, "cb", "e4", "c", "b", "scheduled")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _csv(self, name: str, fields: list[str], rows: list[dict]) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def profiles(
        self, rows: list[dict], name: str = "profiles.csv",
        captured_at: str | None = None,
    ) -> int:
        path = self._csv(
            name,
            ["fighter_id", "observed_at_utc", "source_evidence_uri", "birth_date", "reach_cm"],
            [{"source_evidence_uri": EVIDENCE, **row} for row in rows],
        )
        captured = parse_utc(captured_at) if captured_at else max(
            parse_utc(row["observed_at_utc"]) for row in rows
        )
        with patch("ufc_odds_model.observations.utc_now", return_value=captured):
            return import_profile_observations_csv(
                self.connection, path, "reviewed-archive", LICENSE, self.root / "raw-profiles",
            )

    def stats(
        self, rows: list[dict], name: str = "stats.csv",
        captured_at: str | None = None,
    ) -> int:
        path = self._csv(
            name,
            ["bout_id", "fighter_id", "observed_at_utc", "source_evidence_uri",
             "sig_strikes_landed", "sig_strikes_attempted"],
            [{"source_evidence_uri": EVIDENCE, **row} for row in rows],
        )
        captured = parse_utc(captured_at) if captured_at else max(
            parse_utc(row["observed_at_utc"]) for row in rows
        )
        with patch("ufc_odds_model.observations.utc_now", return_value=captured):
            return import_fight_stat_observations_csv(
                self.connection, path, "reviewed-archive", LICENSE, self.root / "raw-stats",
            )

    def test_source_timestamps_gate_age_reach_and_missingness(self):
        self.profiles([
            {"fighter_id": "b", "observed_at_utc": "2024-01-05T12:00:00Z",
             "birth_date": "1990-04-01", "reach_cm": "180"},
            {"fighter_id": "c", "observed_at_utc": "2024-01-05T12:00:00Z",
             "birth_date": "1995-05-01", "reach_cm": "190"},
        ])
        before = event_feature_rows(self.connection, "e3", cutoff_at_utc="2024-01-05T12:00:00Z")[0]
        after = event_feature_rows(self.connection, "e3", cutoff_at_utc="2024-01-05T12:00:01Z")[0]
        self.assertEqual(before.coverage[:4], (False,) * 4)
        self.assertEqual(after.coverage[:4], (True,) * 4)
        age_index = FEATURE_NAMES.index("age_years_difference_10")
        reach_index = FEATURE_NAMES.index("reach_cm_difference_20")
        self.assertEqual(before.features[age_index], 0.0)
        self.assertEqual(after.features[reach_index], -0.5)
        self.assertNotEqual(after.features[age_index], 0.0)
        self.profiles([
            {"fighter_id": "b", "observed_at_utc": "2024-01-21T00:00:00Z",
             "birth_date": "1991-04-01", "reach_cm": "181"},
        ], "future-profile.csv")
        self.assertEqual(after, event_feature_rows(
            self.connection, "e3", cutoff_at_utc="2024-01-05T12:00:01Z"
        )[0])

    def test_paired_stats_are_opponent_adjusted_and_future_rows_do_not_leak(self):
        baseline = event_feature_rows(self.connection, "e3", cutoff_at_utc="2024-01-12T12:00:00Z")[0]
        self.stats([
            {"bout_id": "ab", "fighter_id": "a", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "70", "sig_strikes_attempted": "100"},
            {"bout_id": "ab", "fighter_id": "b", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "20", "sig_strikes_attempted": "80"},
        ])
        with_stats = event_feature_rows(self.connection, "e3", cutoff_at_utc="2024-01-12T12:00:00Z")[0]
        stat_index = FEATURE_NAMES.index("opponent_adjusted_sig_strike_accuracy_difference")
        self.assertEqual(baseline.features[stat_index], 0.0)
        self.assertEqual(with_stats.coverage[4:], (True, False))
        self.assertAlmostEqual(with_stats.features[stat_index], -0.25 / 3.0)
        self.stats([
            {"bout_id": "ac", "fighter_id": "a", "observed_at_utc": "2024-01-10T10:00:00Z",
             "sig_strikes_landed": "40", "sig_strikes_attempted": "80"},
            {"bout_id": "ac", "fighter_id": "c", "observed_at_utc": "2024-01-10T10:00:00Z",
             "sig_strikes_landed": "50", "sig_strikes_attempted": "100"},
        ], "second-fight-stats.csv")
        adjusted = event_feature_rows(self.connection, "e3", cutoff_at_utc="2024-01-12T12:00:00Z")[0]
        # Fighter C's .50 strike accuracy is adjusted against A's prior
        # defensive .25 (smoothed to 21/82), not a generic .50 baseline.
        self.assertAlmostEqual(
            adjusted.features[stat_index], (-0.25 / 3.0) - ((0.50 - 21 / 82) / 3.0),
        )
        self.assertEqual(adjusted.coverage[4:], (True, True))
        # This corrected strike count has a later source timestamp, so a
        # historical decision on January 12 must remain byte-for-byte stable.
        self.stats([
            {"bout_id": "ab", "fighter_id": "b", "observed_at_utc": "2024-01-13T00:00:00Z",
             "sig_strikes_landed": "40", "sig_strikes_attempted": "80"},
        ], "later-stat.csv")
        self.assertEqual(adjusted, event_feature_rows(
            self.connection, "e3", cutoff_at_utc="2024-01-12T12:00:00Z"
        )[0])
        later = event_feature_rows(self.connection, "e3", cutoff_at_utc="2024-01-14T12:00:00Z")[0]
        self.assertNotEqual(later.features[stat_index], adjusted.features[stat_index])

    def test_same_day_results_and_stats_stay_out_even_if_observed_before_cutoff(self):
        before = event_feature_rows(self.connection, "e2", cutoff_at_utc="2024-01-10T12:00:00Z")[0]
        self.stats([
            {"bout_id": "ac", "fighter_id": "a", "observed_at_utc": "2024-01-10T10:00:00Z",
             "sig_strikes_landed": "40", "sig_strikes_attempted": "80"},
            {"bout_id": "ac", "fighter_id": "c", "observed_at_utc": "2024-01-10T10:00:00Z",
             "sig_strikes_landed": "35", "sig_strikes_attempted": "70"},
        ])
        after = event_feature_rows(self.connection, "e2", cutoff_at_utc="2024-01-10T12:00:00Z")[0]
        self.assertEqual(before, after)

    def test_pair_swap_negates_observed_features_and_swaps_coverage(self):
        self.profiles([
            {"fighter_id": "b", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1990-04-01", "reach_cm": "180"},
            {"fighter_id": "c", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1995-05-01", "reach_cm": "190"},
        ])
        self.stats([
            {"bout_id": "ab", "fighter_id": "a", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "70", "sig_strikes_attempted": "100"},
            {"bout_id": "ab", "fighter_id": "b", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "20", "sig_strikes_attempted": "80"},
        ])
        forward = event_feature_rows(self.connection, "e3", cutoff_at_utc="2024-01-15T00:00:00Z")[0]
        reverse = event_feature_rows(self.connection, "e4", cutoff_at_utc="2024-01-15T00:00:00Z")[0]
        self.assertEqual(forward.coverage, (True, True, True, True, True, False))
        self.assertEqual(reverse.coverage, (True, True, True, True, False, True))
        for a, b in zip(forward.features, reverse.features):
            self.assertAlmostEqual(a, -b)

    def test_training_rows_use_each_historical_cutoff(self):
        self.profiles([
            {"fighter_id": "a", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1990-04-01", "reach_cm": "180"},
            {"fighter_id": "c", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1995-05-01", "reach_cm": "190"},
        ])
        by_bout = {row.bout_id: row for row in training_rows(self.connection)}
        self.assertEqual(by_bout["ab"].coverage[:4], (False,) * 4)
        self.assertEqual(by_bout["ac"].coverage[:4], (True,) * 4)

    def test_late_csv_receipts_cannot_backdate_profiles_or_fight_stats(self):
        self.profiles([
            {"fighter_id": "a", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1990-04-01", "reach_cm": "180"},
            {"fighter_id": "c", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1995-05-01", "reach_cm": "190"},
        ], captured_at="2024-01-15T12:00:00Z")
        self.stats([
            {"bout_id": "ab", "fighter_id": "a", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "70", "sig_strikes_attempted": "100"},
            {"bout_id": "ab", "fighter_id": "b", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "20", "sig_strikes_attempted": "80"},
        ], captured_at="2024-01-15T12:00:00Z")
        historical = event_feature_rows(
            self.connection, "e3", cutoff_at_utc="2024-01-12T12:00:00Z",
        )[0]
        self.assertEqual(historical.coverage, (False,) * 6)
        training = {row.bout_id: row for row in training_rows(self.connection)}
        self.assertEqual(training["ac"].coverage[:4], (False,) * 4)
        current = event_feature_rows(
            self.connection, "e3", cutoff_at_utc="2024-01-16T12:00:00Z",
        )[0]
        self.assertEqual(current.coverage[:4], (False, True, False, True))
        self.assertEqual(current.coverage[4:], (True, False))

    def test_missing_or_changed_observation_receipts_remove_features(self):
        self.profiles([
            {"fighter_id": "b", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1990-04-01", "reach_cm": "180"},
            {"fighter_id": "c", "observed_at_utc": "2024-01-05T00:00:00Z",
             "birth_date": "1995-05-01", "reach_cm": "190"},
        ])
        self.stats([
            {"bout_id": "ab", "fighter_id": "a", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "70", "sig_strikes_attempted": "100"},
            {"bout_id": "ab", "fighter_id": "b", "observed_at_utc": "2024-01-02T01:00:00Z",
             "sig_strikes_landed": "20", "sig_strikes_attempted": "80"},
        ])
        before = event_feature_rows(
            self.connection, "e3", cutoff_at_utc="2024-01-12T12:00:00Z",
        )[0]
        self.assertEqual(before.coverage, (True, True, True, True, True, False))
        receipts = self.connection.execute(
            """SELECT source, payload_path FROM ingestion_runs
               WHERE source LIKE 'reviewed-profile-csv:%'
                  OR source LIKE 'reviewed-fight-stats-csv:%'"""
        ).fetchall()
        for receipt in receipts:
            path = Path(receipt["payload_path"])
            if receipt["source"].startswith("reviewed-profile-csv:"):
                path.write_bytes(b"changed profile source")
            else:
                path.unlink()
        after = event_feature_rows(
            self.connection, "e3", cutoff_at_utc="2024-01-12T12:00:00Z",
        )[0]
        self.assertEqual(after.coverage, (False,) * 6)

    def test_import_replay_receipts_and_immutability(self):
        rows = [{"fighter_id": "b", "observed_at_utc": "2024-01-05T00:00:00Z",
                 "birth_date": "1990-04-01", "reach_cm": "180"}]
        self.assertEqual(self.profiles(rows), 1)
        self.assertEqual(self.profiles(rows), 0)
        receipts = self.connection.execute(
            "SELECT payload_path, sha256 FROM ingestion_runs WHERE source LIKE 'reviewed-profile-csv:%'"
        ).fetchall()
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]["sha256"], receipts[1]["sha256"])
        self.assertEqual(Path(receipts[0]["payload_path"]).read_bytes(),
                         (self.root / "profiles.csv").read_bytes())
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE fighter_profile_observations SET reach_cm = 181 WHERE fighter_id = 'b'"
            )
        with self.assertRaisesRegex(ValueError, "Conflicting immutable"):
            self.profiles([
                {"fighter_id": "b", "observed_at_utc": "2024-01-05T00:00:00Z",
                 "birth_date": "1990-04-01", "reach_cm": "181"},
            ], "conflict.csv")

    def test_rejects_unknown_identity_and_fight_stat_bad_time(self):
        with self.assertRaisesRegex(ValueError, "Unknown stable fighter ID"):
            self.profiles([
                {"fighter_id": "unknown", "observed_at_utc": "2024-01-05T00:00:00Z",
                 "birth_date": "1990-01-01", "reach_cm": ""},
            ])
        with self.assertRaisesRegex(ValueError, "follow the known event start"):
            self.stats([
                {"bout_id": "ab", "fighter_id": "a", "observed_at_utc": "2024-01-01T19:00:00Z",
                 "sig_strikes_landed": "2", "sig_strikes_attempted": "3"},
            ])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM fight_stat_observations"
        ).fetchone()[0], 0)

    def test_conflicting_rows_inside_one_csv_are_rejected_before_receipt(self):
        with self.assertRaisesRegex(ValueError, "conflicting duplicate profile"):
            self.profiles([
                {"fighter_id": "b", "observed_at_utc": "2024-01-05T00:00:00Z",
                 "birth_date": "1990-04-01", "reach_cm": "180"},
                {"fighter_id": "b", "observed_at_utc": "2024-01-05T00:00:00Z",
                 "birth_date": "1990-04-01", "reach_cm": "181"},
            ])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE source LIKE 'reviewed-profile-csv:%'"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
