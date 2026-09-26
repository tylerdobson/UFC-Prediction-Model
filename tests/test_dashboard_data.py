"""The dashboard must display persisted evidence without changing it."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.dashboard_data import load_dashboard
from ufc_odds_model.demo import seed_demo
from ufc_odds_model.pipeline import utc_string


class DashboardDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dashboard.sqlite"
        self.now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        connection = db.connect(self.path)
        db.init_db(connection)
        self.addCleanup(connection.close)
        return connection

    def _card(self, connection: sqlite3.Connection) -> tuple[int, int]:
        start = self.now + timedelta(days=7)
        db.upsert_fighter(connection, "a", "Alice Example", "fixture", "a")
        db.upsert_fighter(connection, "b", "Bob Example", "fixture", "b")
        db.upsert_event(connection, "event", "Fixture card", start.date().isoformat(),
                        "scheduled", source="fixture", source_event_id="event",
                        start_time_utc=utc_string(start))
        db.upsert_bout(connection, "bout", "event", "a", "b", "scheduled",
                       source="fixture", source_bout_id="bout")
        old = utc_string(self.now - timedelta(hours=2))
        fresh = utc_string(self.now - timedelta(seconds=30))
        db.add_quote(connection, "bout", "Fixture Book", "a", 1.70, old,
                     "fixture", bookmaker_updated_at_utc=old)
        db.add_quote(connection, "bout", "Fixture Book", "a", 2.20, fresh,
                     "fixture", bookmaker_updated_at_utc=fresh)
        db.add_quote(connection, "bout", "Fixture Book", "b", 1.80, fresh,
                     "fixture", bookmaker_updated_at_utc=fresh)
        prediction_time = utc_string(self.now - timedelta(seconds=20))
        prediction_id = db.save_prediction(
            connection, "bout", "test-v1", prediction_time, prediction_time, 0.61,
        )
        quote_id = connection.execute(
            "SELECT quote_id FROM odds_quotes WHERE bout_id = 'bout' "
            "AND selection_fighter_id = 'a' AND decimal_odds = 2.20"
        ).fetchone()["quote_id"]
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256, snapshot_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            ("the-odds-api", fresh, "data/raw/fixture.json", "a" * 64, fresh),
        )
        connection.commit()
        return prediction_id, quote_id

    def _accepted_gate(self, connection: sqlite3.Connection, prediction_id: int,
                       quote_id: int) -> int:
        receipt_id = connection.execute(
            "SELECT run_id FROM ingestion_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()["run_id"]
        checked = utc_string(self.now - timedelta(seconds=5))
        snapshot = utc_string(self.now - timedelta(seconds=30))
        cutoff = utc_string(self.now - timedelta(seconds=20))
        start = utc_string(self.now + timedelta(days=7))
        cursor = connection.execute(
            """
            INSERT INTO prefight_gate_checks(
                ingestion_run_id, event_id, bout_id, prediction_id, quote_id,
                snapshot_at_utc, checked_at_utc, event_start_time_utc,
                model_version, model_cutoff_at_utc, bookmaker,
                bookmaker_updated_at_utc, quote_captured_at_utc,
                quoted_decimal_odds, model_probability, model_decision,
                gate_decision, gate_reason, max_age_seconds,
                decimal_odds_drift, min_edge, conservative_decimal_odds,
                conservative_expected_profit_per_dollar
            ) VALUES (?, 'event', 'bout', ?, ?, ?, ?, ?, 'test-v1', ?,
                      'Fixture Book', ?, ?, 2.20, 0.61, 'candidate',
                      'alert_candidate', 'ok', 60, 0.05, 0.03, 2.15, 0.3115)
            """,
            (receipt_id, prediction_id, quote_id, snapshot, checked, start,
             cutoff, snapshot, snapshot),
        )
        connection.commit()
        return int(cursor.lastrowid)

    def test_missing_and_uninitialized_database_have_explicit_states(self) -> None:
        missing = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(missing["status"], "missing_database")
        self.assertEqual(missing["quality"]["status"], "unavailable")
        self.assertFalse(self.path.exists())

        sqlite3.connect(self.path).close()
        uninitialized = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(uninitialized["status"], "schema_missing")
        self.assertIn("events", uninitialized["reason"])
        self.assertEqual(uninitialized["upcoming_events"], [])

    def test_empty_initialized_database_is_not_a_live_card(self) -> None:
        connection = self._connect()
        before = self.path.stat().st_mtime_ns
        snapshot = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(snapshot["status"], "empty")
        self.assertEqual(snapshot["data_origin"], "empty")
        self.assertEqual(snapshot["quality"]["summary"]["events"], 0)
        self.assertEqual(snapshot["paper_ledger"]["summary"]["bets"], 0)
        self.assertEqual(snapshot["manual_ledger"]["summary"]["bets"], 0)
        self.assertEqual(snapshot["evaluation"]["status"], "unavailable")
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        json.dumps(snapshot)

    def test_demo_origin_cannot_be_mistaken_for_real_history(self) -> None:
        connection = self._connect()
        seed_demo(connection, self.now)
        snapshot = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["data_origin"], "demo_only")
        self.assertEqual(len(snapshot["upcoming_events"]), 1)
        card = snapshot["upcoming_events"][0]
        self.assertTrue(card["is_demo"])
        self.assertTrue(card["name"].startswith("Fictional"))
        self.assertEqual(card["bouts"][0]["availability"], "no_valid_prediction")
        self.assertTrue(all(quote["freshness"] == "unverified_bookmaker_time"
                            for bout in card["bouts"] for quote in bout["quotes"]))
        json.dumps(snapshot)

    def test_research_source_is_explicit_even_when_mixed_with_other_events(self) -> None:
        connection = self._connect()
        self._card(connection)
        connection.execute("UPDATE events SET source = 'wikipedia_research' WHERE event_id = 'event'")
        connection.commit()
        research = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(research["data_origin"], "research_only")
        self.assertEqual(research["upcoming_events"][0]["source"], "wikipedia_research")

        db.upsert_event(connection, "other", "Other sourced card",
                        (self.now + timedelta(days=8)).date().isoformat(),
                        "scheduled", source="fixture", source_event_id="other",
                        start_time_utc=utc_string(self.now + timedelta(days=8)))
        connection.commit()
        mixed = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(mixed["data_origin"], "research_mixed")

    def test_upcoming_card_uses_latest_stored_prediction_and_quotes(self) -> None:
        connection = self._connect()
        prediction_id, quote_id = self._card(connection)
        before = self.path.stat().st_mtime_ns
        snapshot = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["data_origin"], "non_demo")
        self.assertEqual(snapshot["ingestion_runs"][0]["sha256"], "a" * 64)
        card = snapshot["upcoming_events"][0]
        bout = card["bouts"][0]
        self.assertEqual((bout["fighter_a_name"], bout["fighter_b_name"]),
                         ("Alice Example", "Bob Example"))
        self.assertEqual(bout["prediction"]["prediction_id"], prediction_id)
        self.assertAlmostEqual(bout["prediction"]["p_fighter_b"], 0.39)
        self.assertEqual(bout["availability"], "requires_alert_gate")
        self.assertEqual(len(bout["quotes"]), 2)
        selected = next(q for q in bout["quotes"] if q["selection_fighter_id"] == "a")
        self.assertEqual(selected["quote_id"], quote_id)
        self.assertEqual(selected["decimal_odds"], 2.20)
        self.assertEqual(selected["freshness"], "fresh")
        self.assertEqual(selected["age_seconds"], 30)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        json.dumps(snapshot)

    def test_stale_evidence_and_provider_block_are_visible(self) -> None:
        connection = self._connect()
        self._card(connection)
        late = self.now + timedelta(minutes=2)
        stale = load_dashboard(self.path, as_of=late)
        self.assertEqual(stale["upcoming_events"][0]["bouts"][0]["availability"],
                         "stale_prediction")
        cutoff = utc_string(late - timedelta(seconds=10))
        db.save_prediction(connection, "bout", "test-v2", cutoff, cutoff, 0.58)
        connection.commit()
        stale_quote = load_dashboard(self.path, as_of=late)
        self.assertEqual(stale_quote["upcoming_events"][0]["bouts"][0]["availability"],
                         "no_verified_fresh_quote")
        connection.execute("UPDATE bouts SET provider_status = 'live' WHERE bout_id = 'bout'")
        connection.commit()
        blocked = load_dashboard(self.path, as_of=late)
        self.assertEqual(blocked["upcoming_events"][0]["bouts"][0]["availability"],
                         "provider_status_blocked")

    def test_saved_gate_check_expires_and_newer_receipt_supersedes_it(self) -> None:
        connection = self._connect()
        prediction_id, quote_id = self._card(connection)
        gate_check_id = self._accepted_gate(connection, prediction_id, quote_id)
        recent = load_dashboard(self.path, as_of=self.now)["upcoming_events"][0]["bouts"][0]
        self.assertEqual(recent["gate_check"]["gate_check_id"], gate_check_id)
        self.assertEqual(recent["gate_check"]["display_status"], "accepted_recently")

        later = load_dashboard(self.path, as_of=self.now + timedelta(seconds=61))
        self.assertEqual(later["upcoming_events"][0]["bouts"][0]["gate_check"]["display_status"],
                         "expired")
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256, snapshot_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            ("the-odds-api", utc_string(self.now), "data/raw/new.json", "b" * 64,
             utc_string(self.now)),
        )
        connection.commit()
        superseded = load_dashboard(self.path, as_of=self.now)["upcoming_events"][0]["bouts"][0]
        self.assertEqual(superseded["gate_check"]["display_status"], "superseded")
        self.assertEqual(superseded["availability"], "gate_superseded")

    def test_paper_and_manual_ledgers_remain_separate(self) -> None:
        connection = self._connect()
        prediction_id, quote_id = self._card(connection)
        gate_check_id = self._accepted_gate(connection, prediction_id, quote_id)
        placed = utc_string(self.now - timedelta(seconds=5))
        quoted = utc_string(self.now - timedelta(seconds=30))
        connection.execute(
            """
            INSERT INTO paper_bets(
                prediction_id, quote_id, event_id, bout_id, selection_fighter_id,
                bookmaker, decision_at_utc, recorded_at_utc, quote_captured_at_utc,
                quoted_decimal_odds, model_probability, expected_profit_per_unit,
                bankroll_units, per_bet_cap_units, event_cap_units, stake_units,
                settlement_status, payout_units, settled_at_utc, gate_check_id
            ) VALUES (?, ?, 'event', 'bout', 'a', 'Fixture Book', ?, ?, ?,
                      2.20, 0.61, 0.342, 1000, 10, 50, 10, 'won', 22, ?, ?)
            """, (prediction_id, quote_id, placed, placed, quoted, placed, gate_check_id),
        )
        connection.execute(
            """
            INSERT INTO bets(prediction_id, quote_id, selection_fighter_id,
                             placed_at_utc, actual_decimal_odds, stake,
                             settlement_status, payout, settled_at_utc)
            VALUES (?, ?, 'a', ?, 2.10, 5, 'lost', 0, ?)
            """, (prediction_id, quote_id, placed, placed),
        )
        connection.commit()
        snapshot = load_dashboard(self.path, as_of=self.now)
        paper = snapshot["paper_ledger"]
        manual = snapshot["manual_ledger"]
        self.assertEqual(paper["summary"]["bets"], 1)
        self.assertEqual(manual["summary"]["bets"], 1)
        self.assertEqual(paper["summary"]["realized_profit_units"], 12)
        self.assertEqual(manual["summary"]["realized_profit_units"], -5)
        self.assertEqual(paper["rows"][0]["selection_name"], "Alice Example")
        self.assertEqual(manual["rows"][0]["bookmaker"], "Fixture Book")
        self.assertEqual(snapshot["upcoming_events"][0]["bouts"][0]["gate_check"]["gate_check_id"],
                         gate_check_id)
        self.assertEqual(snapshot["upcoming_events"][0]["bouts"][0]["availability"],
                         "gate_accepted_recently")
        json.dumps(snapshot)

    def test_saved_evaluation_status_reports_provenance_and_staleness(self) -> None:
        connection = self._connect()
        self._card(connection)
        report = Path(self.temp.name) / "evaluation.json"
        saved = {
            "schema_version": 1,
            "generated_at_utc": utc_string(self.now - timedelta(hours=1)),
            "source_db_path": str(self.path.resolve()),
            "source_db_mtime_ns": self.path.stat().st_mtime_ns,
            "parameters": {"decision_hours_before_event": 24.0, "max_quote_age_hours": 24.0},
            "evaluation": {"status": "insufficient_history", "split": None, "test": None},
        }
        report.write_text(json.dumps(saved), encoding="utf-8")
        available = load_dashboard(self.path, as_of=self.now, evaluation_report=report)
        self.assertEqual(available["evaluation"]["status"], "insufficient_history")
        self.assertEqual(available["evaluation"]["result_status"], "insufficient_history")
        self.assertEqual(available["evaluation"]["source_db_path"], str(self.path.resolve()))

        other_path = Path(self.temp.name) / "other.sqlite"
        with db.connect(other_path) as other:
            db.init_db(other)
        wrong = load_dashboard(other_path, as_of=self.now, evaluation_report=report)
        self.assertEqual(wrong["evaluation"]["status"], "wrong_database")
        self.assertNotIn("result", wrong["evaluation"])

        legacy = dict(saved)
        legacy.pop("source_db_path")
        report.write_text(json.dumps(legacy), encoding="utf-8")
        unverified = load_dashboard(self.path, as_of=self.now, evaluation_report=report)
        self.assertEqual(unverified["evaluation"]["status"], "unverified_provenance")
        self.assertNotIn("result", unverified["evaluation"])

        report.write_text(json.dumps(saved), encoding="utf-8")
        os.utime(self.path, ns=(self.path.stat().st_atime_ns, self.path.stat().st_mtime_ns + 1_000_000))
        stale = load_dashboard(self.path, as_of=self.now, evaluation_report=report)
        self.assertEqual(stale["evaluation"]["status"], "possibly_stale")
        self.assertIn("Database changed", stale["evaluation"]["reason"])
        report.write_text("{bad json", encoding="utf-8")
        invalid = load_dashboard(self.path, as_of=self.now, evaluation_report=report)
        self.assertEqual(invalid["evaluation"]["status"], "invalid_report")
        self.assertEqual(load_dashboard(self.path, as_of=self.now,
                                        evaluation_report=report.with_name("missing.json"))
                         ["evaluation"]["status"], "missing_report")

    def test_saved_evaluation_rejects_malformed_nested_metrics(self) -> None:
        self._connect()
        report = Path(self.temp.name) / "evaluation.json"
        metrics = {"bouts": 1, "accuracy": 1.0, "brier_score": 0.04, "log_loss": 0.2}
        bins = [
            {"lower": index / 5, "upper": (index + 1) / 5,
             "bouts": int(index == 3),
             "mean_probability": 0.7 if index == 3 else None,
             "observed_win_rate": 1.0 if index == 3 else None}
            for index in range(5)
        ]
        result = {
            "status": "ok",
            "split": {
                "train": {"bouts": 1, "events": 1, "first_date": "2026-01-01", "last_date": "2026-01-01"},
                "validation": {"bouts": 1, "events": 1, "first_date": "2026-02-01", "last_date": "2026-02-01"},
                "test": {"bouts": 1, "events": 1, "first_date": "2026-03-01", "last_date": "2026-03-01"},
            },
            "calibration": {"method": "symmetric_temperature", "validation_bouts": 1,
                            "applied": False, "scale": 1.0},
            "test": {
                "elo": {"metrics": metrics, "calibration_bins": bins},
                "logistic": {"metrics": metrics, "calibration_bins": bins},
                "bookmaker": {"available_bouts": 0, "coverage": 0.0, "metrics": None,
                              "elo_on_available_bouts": None,
                              "logistic_on_available_bouts": None,
                              "calibration_bins": None},
            },
        }
        saved = {
            "schema_version": 1,
            "generated_at_utc": utc_string(self.now - timedelta(hours=1)),
            "source_db_path": str(self.path.resolve()),
            "source_db_mtime_ns": self.path.stat().st_mtime_ns,
            "parameters": {"decision_hours_before_event": 24.0, "max_quote_age_hours": 24.0},
            "evaluation": result,
        }

        def status_for(evaluation: dict) -> dict:
            saved["evaluation"] = evaluation
            report.write_text(json.dumps(saved), encoding="utf-8")
            return load_dashboard(self.path, as_of=self.now, evaluation_report=report)["evaluation"]

        self.assertEqual(status_for(result)["status"], "available")
        malformed = json.loads(json.dumps(result))
        malformed["split"] = "missing split details"
        self.assertEqual(status_for(malformed)["status"], "invalid_report")
        malformed = json.loads(json.dumps(result))
        malformed["test"]["logistic"]["metrics"]["brier_score"] = "0.04"
        invalid = status_for(malformed)
        self.assertEqual(invalid["status"], "invalid_report")
        self.assertNotIn("result", invalid)
        self.assertEqual(status_for({"status": "insufficient_history", "split": None,
                                     "test": None})["status"], "insufficient_history")

    def test_corrupt_stored_number_returns_invalid_data_state(self) -> None:
        connection = self._connect()
        prediction_id, quote_id = self._card(connection)
        gate_check_id = self._accepted_gate(connection, prediction_id, quote_id)
        placed = utc_string(self.now - timedelta(seconds=5))
        quoted = utc_string(self.now - timedelta(seconds=30))
        connection.execute(
            """
            INSERT INTO paper_bets(
                prediction_id, quote_id, event_id, bout_id, selection_fighter_id,
                bookmaker, decision_at_utc, recorded_at_utc, quote_captured_at_utc,
                quoted_decimal_odds, model_probability, expected_profit_per_unit,
                bankroll_units, per_bet_cap_units, event_cap_units, stake_units,
                gate_check_id
            ) VALUES (?, ?, 'event', 'bout', 'a', 'Fixture Book', ?, ?, ?,
                      2.20, 0.61, 0.342, 1000, 10, 50, 10, ?)
            """, (prediction_id, quote_id, placed, placed, quoted, gate_check_id),
        )
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE paper_bets SET payout_units = 'corrupt'")
        connection.commit()

        snapshot = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(snapshot["status"], "invalid_data")
        self.assertEqual(snapshot["quality"]["status"], "unavailable")
        self.assertIn("invalid values", snapshot["reason"])

    def test_bad_quote_price_is_reported_without_crashing_other_views(self) -> None:
        connection = self._connect()
        self._card(connection)
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE odds_quotes SET decimal_odds = 'corrupt' "
            "WHERE selection_fighter_id = 'a' AND decimal_odds = 2.20"
        )
        connection.commit()
        snapshot = load_dashboard(self.path, as_of=self.now)
        self.assertEqual(snapshot["status"], "available")
        self.assertFalse(snapshot["quality"]["ok"])
        self.assertIn("invalid_quote_price", {issue["code"] for issue in snapshot["quality"]["issues"]})
        self.assertTrue(all(q["selection_fighter_id"] == "b"
                            for q in snapshot["upcoming_events"][0]["bouts"][0]["quotes"]))

    def test_naive_time_and_invalid_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_dashboard(self.path, as_of=datetime(2026, 9, 26, 15))
        with self.assertRaises(ValueError):
            load_dashboard(self.path, event_limit=0)
        with self.assertRaises(ValueError):
            load_dashboard(self.path, max_age_seconds=0)


if __name__ == "__main__":
    unittest.main()
