"""Paper decisions must stay within exposure caps and settle conservatively."""

from __future__ import annotations

import math
import hashlib
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.alerts import record_prefight_checks
from ufc_odds_model.card_history import record_card_snapshots
from ufc_odds_model.paper import record_paper_candidates, settle_paper_bets
from ufc_odds_model.pipeline import parse_utc, utc_now, utc_string


class PaperLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = utc_now()
        self.cutoff = utc_string(self.now - timedelta(seconds=3))
        self.quote_time = utc_string(self.now - timedelta(seconds=7))
        self.book_time = utc_string(self.now - timedelta(seconds=8))
        self.event_start = utc_string(self.now + timedelta(days=7))
        self.event_date = (self.now + timedelta(days=7)).date().isoformat()
        self.result_time = utc_string(self.now + timedelta(days=7, hours=3))
        self.connection = db.connect(Path(self.temp.name) / "paper.sqlite")
        db.init_db(self.connection)
        self.event_id = "event-1"
        db.upsert_event(
            self.connection, self.event_id, "Fixture card", self.event_date, "scheduled",
            start_time_utc=self.event_start,
        )
        self.report_rows = []
        raw_events = []
        quote_links = []
        for index in range(6):
            fighter_a = f"fighter-{index}-a"
            fighter_b = f"fighter-{index}-b"
            bout_id = f"bout-{index}"
            db.upsert_fighter(self.connection, fighter_a, fighter_a)
            db.upsert_fighter(self.connection, fighter_b, fighter_b)
            db.upsert_bout(self.connection, bout_id, self.event_id, fighter_a, fighter_b, "scheduled")
            price = 2.30 - index * 0.05
            db.add_quote(
                self.connection, bout_id, "fixture-book", fighter_a, price,
                self.quote_time, "fixture", self.book_time,
            )
            quote_id = self.connection.execute(
                "SELECT quote_id FROM odds_quotes WHERE bout_id = ?", (bout_id,)
            ).fetchone()["quote_id"]
            source_event_id = f"odds-fight-{index}"
            quote_links.append((quote_id, source_event_id))
            raw_events.append({
                "id": source_event_id,
                "home_team": fighter_a, "away_team": fighter_b,
                "commence_time": self.event_start,
                "bookmakers": [{"key": "fixture-book", "markets": [{
                    "key": "h2h", "last_update": self.book_time,
                    "outcomes": [
                        {"name": fighter_a, "price": price},
                        {"name": fighter_b, "price": 1.8},
                    ],
                }]}],
            })
            prediction_id = db.save_prediction(
                self.connection, bout_id, "fixture-v1", self.cutoff,
                self.cutoff, 0.6,
            )
            self.report_rows.append({
                "decision": "candidate", "event_id": self.event_id, "bout_id": bout_id,
                "prediction_id": prediction_id, "quote_id": quote_id,
                "model_version": "fixture-v1", "bookmaker": "fixture-book",
                "as_of_utc": self.cutoff,
                "quote_captured_at_utc": self.quote_time,
                "bookmaker_updated_at_utc": self.book_time,
                "model_selection_probability": 0.6,
                "decimal_odds": price,
            })
        raw = Path(self.temp.name) / "odds.json"
        raw.write_text(json.dumps(raw_events), encoding="utf-8")
        self.receipt_id = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256, snapshot_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            ("the-odds-api", self.quote_time, str(raw), hashlib.sha256(raw.read_bytes()).hexdigest(),
             self.quote_time),
        ).lastrowid
        for quote_id, source_event_id in quote_links:
            self.connection.execute(
                "INSERT INTO odds_quote_receipts(quote_id, ingestion_run_id, source_event_id) "
                "VALUES (?, ?, ?)",
                (quote_id, self.receipt_id, source_event_id),
            )
        roster_observed = utc_string(self.now - timedelta(seconds=20))
        roster_raw = Path(self.temp.name) / "roster.csv"
        roster_raw.write_text("fixture roster observed before the card", encoding="utf-8")
        roster_receipt_id = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES (?, ?, ?, ?)",
            ("reviewed_csv", roster_observed, str(roster_raw),
             hashlib.sha256(roster_raw.read_bytes()).hexdigest()),
        ).lastrowid
        record_card_snapshots(self.connection, int(roster_receipt_id), [{
            "event_id": self.event_id, "event_name": "Fixture card",
            "event_date": self.event_date, "event_status": "scheduled",
            "event_provider_status": None, "start_time_utc": self.event_start,
            "source_observed_at_utc": roster_observed,
            "observation_basis": "reviewed_csv", "source_url": "fixture://roster",
            "source_revision_id": "fixture-1", "license_name": "fixture",
            "license_url": "fixture://license", "reviewed_by": "test",
            "bouts": [{
                "event_id": self.event_id, "bout_id": f"bout-{index}",
                "fighter_a_id": f"fighter-{index}-a",
                "fighter_a_name": f"fighter-{index}-a",
                "fighter_b_id": f"fighter-{index}-b",
                "fighter_b_name": f"fighter-{index}-b",
                "bout_status": "scheduled", "bout_provider_status": None,
                "weight_class": None, "outcome": None,
                "winner_fighter_id": None, "method": None,
            } for index in range(6)],
        }])
        self.connection.commit()
        self.report_rows = record_prefight_checks(
            self.connection, self.report_rows, ingestion_run_id=self.receipt_id,
        )
        self.now = parse_utc(self.report_rows[0]["alert_checked_at_utc"])
        self.paper_clock = patch("ufc_odds_model.paper.utc_now", return_value=self.now)
        self.paper_clock.start()

    def tearDown(self) -> None:
        self.connection.close()
        self.paper_clock.stop()
        self.temp.cleanup()

    def _receipt(self, snapshot_at_utc: str) -> int:
        return int(self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256, snapshot_at_utc) "
            "SELECT source, ?, payload_path, sha256, ? FROM ingestion_runs WHERE run_id = ?",
            (snapshot_at_utc, snapshot_at_utc, self.receipt_id),
        ).lastrowid)

    def test_caps_across_calls_and_idempotent_report(self):
        first = record_paper_candidates(
            self.connection, self.report_rows[:2], 1000, 0.01, 0.035
        )
        self.assertEqual([row["stake_units"] for row in first], [10.0, 10.0])
        second = record_paper_candidates(
            self.connection, self.report_rows, 1000, 0.01, 0.035
        )
        self.assertEqual([row["stake_units"] for row in second], [10.0, 5.0])
        self.assertEqual([row["bout_id"] for row in second], ["bout-2", "bout-3"])
        self.assertEqual(record_paper_candidates(
            self.connection, self.report_rows, 1000, 0.01, 0.035
        ), [])
        total = self.connection.execute(
            "SELECT COUNT(*) AS n, SUM(stake_units) AS total FROM paper_bets"
        ).fetchone()
        self.assertEqual(total["n"], 4)
        self.assertAlmostEqual(total["total"], 35.0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 0)

    def test_binary_settlement_and_ambiguous_results(self):
        created = record_paper_candidates(
            self.connection, self.report_rows[:4], 1000, 0.01, 0.05
        )
        db.upsert_result(self.connection, "bout-0", "win", "fighter-0-a", self.result_time)
        db.upsert_result(self.connection, "bout-1", "win", "fighter-1-b", self.result_time)
        db.upsert_result(self.connection, "bout-2", "draw", None, self.result_time)
        db.upsert_result(self.connection, "bout-3", "no_contest", None, self.result_time)
        db.upsert_event(
            self.connection, self.event_id, "Fixture card", self.event_date, "completed",
            start_time_utc=self.event_start,
        )
        self.connection.commit()
        self.assertEqual(settle_paper_bets(self.connection, self.event_id), {
            "won": 1, "lost": 1, "pending_review": 2, "open": 0,
        })
        ledger = self.connection.execute(
            "SELECT bout_id, settlement_status, payout_units, settled_at_utc, settlement_note "
            "FROM paper_bets ORDER BY bout_id"
        ).fetchall()
        self.assertEqual(ledger[0]["settlement_status"], "won")
        self.assertAlmostEqual(ledger[0]["payout_units"], created[0]["stake_units"] * 2.30)
        self.assertIsNotNone(ledger[0]["settled_at_utc"])
        self.assertEqual(ledger[1]["settlement_status"], "lost")
        self.assertEqual(ledger[1]["payout_units"], 0.0)
        self.assertEqual(ledger[2]["settlement_note"], "draw")
        self.assertEqual(ledger[3]["settlement_note"], "no_contest")
        self.assertTrue(all(row["payout_units"] is None for row in ledger[2:]))
        self.assertTrue(all(row["settled_at_utc"] is None for row in ledger[2:]))
        self.assertEqual(settle_paper_bets(self.connection, self.event_id), {
            "won": 0, "lost": 0, "pending_review": 0, "open": 0,
        })

    def test_saved_paper_decision_cannot_be_changed_or_deleted(self):
        record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        paper_bet_id = self.connection.execute(
            "SELECT paper_bet_id FROM paper_bets"
        ).fetchone()[0]
        for column, value in (
            ("stake_units", 500),
            ("quoted_decimal_odds", 9.0),
            ("gate_check_id", None),
        ):
            with self.subTest(column=column), self.assertRaisesRegex(Exception, "immutable"):
                self.connection.execute(
                    f"UPDATE paper_bets SET {column} = ? WHERE paper_bet_id = ?",
                    (value, paper_bet_id),
                )
        with self.assertRaisesRegex(Exception, "immutable"):
            self.connection.execute(
                "DELETE FROM paper_bets WHERE paper_bet_id = ?", (paper_bet_id,)
            )
        self.assertEqual(self.connection.execute(
            "SELECT stake_units, quoted_decimal_odds, gate_check_id FROM paper_bets"
        ).fetchone()["stake_units"], 10)

    def test_cancelled_bout_is_flagged_for_review(self):
        record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        self.connection.execute("UPDATE bouts SET status = 'cancelled' WHERE bout_id = 'bout-0'")
        self.connection.commit()
        self.assertEqual(settle_paper_bets(self.connection, self.event_id)["pending_review"], 1)
        row = self.connection.execute(
            "SELECT settlement_status, settlement_note FROM paper_bets"
        ).fetchone()
        self.assertEqual((row["settlement_status"], row["settlement_note"]),
                         ("pending_review", "cancelled"))

    def test_event_bankroll_cannot_change_between_recording_calls(self):
        record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        with self.assertRaisesRegex(ValueError, "different bankroll"):
            record_paper_candidates(self.connection, self.report_rows[1:2], 2000)
        with self.assertRaisesRegex(ValueError, "stake caps"):
            record_paper_candidates(self.connection, self.report_rows[1:2], 1000,
                                    max_fraction_per_event=0.10)
        self.assertEqual(len(record_paper_candidates(
            self.connection, self.report_rows[1:2], 1000
        )), 1)

    def test_stale_report_and_quote_cannot_be_logged_as_live_paper(self):
        old_cutoff = utc_string(self.now - timedelta(hours=1))
        old_prediction = db.save_prediction(
            self.connection, "bout-0", "fixture-v1", old_cutoff, old_cutoff, 0.6,
        )
        stale_report = dict(self.report_rows[0], prediction_id=old_prediction,
                            as_of_utc=old_cutoff)
        with self.assertRaisesRegex(ValueError, "matching accepted pre-fight gate"):
            record_paper_candidates(self.connection, [stale_report], 1000)

        stale_quote_time = utc_string(self.now - timedelta(hours=25))
        db.add_quote(
            self.connection, "bout-0", "fixture-book", "fighter-0-a", 2.30,
            stale_quote_time, "fixture",
        )
        stale_quote_id = self.connection.execute(
            "SELECT quote_id FROM odds_quotes WHERE bout_id = ? AND captured_at_utc = ?",
            ("bout-0", stale_quote_time),
        ).fetchone()["quote_id"]
        stale_quote = dict(self.report_rows[0], quote_id=stale_quote_id,
                           quote_captured_at_utc=stale_quote_time)
        with self.assertRaisesRegex(ValueError, "matching accepted pre-fight gate"):
            record_paper_candidates(self.connection, [stale_quote], 1000)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)

    def test_ungated_or_rejected_candidate_cannot_write_paper_entry(self):
        bare = dict(self.report_rows[0])
        bare.pop("gate_check_id")
        with self.assertRaisesRegex(ValueError, "accepted gate"):
            record_paper_candidates(self.connection, [bare], 1000)
        raw = dict(self.report_rows[0])
        raw.pop("alert_eligible")
        with self.assertRaisesRegex(ValueError, "stored pre-fight gate"):
            record_paper_candidates(self.connection, [raw], 1000)
        rejected = dict(self.report_rows[0], alert_eligible=False)
        self.assertEqual(record_paper_candidates(self.connection, [rejected], 1000), [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)

    def test_stale_snapshot_is_saved_as_rejection_and_cannot_be_paper_traded(self):
        old = utc_string(self.now - timedelta(minutes=2))
        older = utc_string(self.now - timedelta(minutes=2, seconds=1))
        db.add_quote(
            self.connection, "bout-0", "fixture-book", "fighter-0-a", 2.3,
            old, "fixture", older,
        )
        quote_id = self.connection.execute(
            "SELECT quote_id FROM odds_quotes WHERE bout_id = ? AND captured_at_utc = ?",
            ("bout-0", old),
        ).fetchone()["quote_id"]
        raw = dict(self.report_rows[0], quote_id=quote_id,
                   quote_captured_at_utc=old, bookmaker_updated_at_utc=older)
        rejected = record_prefight_checks(
            self.connection, [raw], ingestion_run_id=self._receipt(old),
        )
        self.assertEqual(rejected[0]["alert_reason"], "stale_data")
        self.assertEqual(record_paper_candidates(self.connection, rejected, 1000), [])
        saved = self.connection.execute(
            "SELECT gate_decision, gate_reason FROM prefight_gate_checks WHERE gate_check_id = ?",
            (rejected[0]["gate_check_id"],),
        ).fetchone()
        self.assertEqual((saved["gate_decision"], saved["gate_reason"]), ("reject", "stale_data"))

    def test_mismatched_snapshot_or_price_drift_is_saved_as_rejection(self):
        newer = utc_string(self.now - timedelta(seconds=1))
        mismatch = record_prefight_checks(
            self.connection, [self.report_rows[0]], ingestion_run_id=self._receipt(newer),
        )
        self.assertEqual(mismatch[0]["alert_reason"], "different_snapshot")
        self.assertEqual(record_paper_candidates(self.connection, mismatch, 1000), [])
        drift_failure = record_prefight_checks(
            self.connection, [self.report_rows[0]], ingestion_run_id=self._receipt(self.quote_time),
            decimal_odds_drift=0.75,
        )
        self.assertEqual(drift_failure[0]["alert_reason"], "edge_below_floor")
        self.assertEqual(record_paper_candidates(self.connection, drift_failure, 1000), [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)

    def test_fresh_unlinked_quote_receipt_is_rejected_and_cannot_be_paper_traded(self):
        duplicate_receipt = self._receipt(self.quote_time)
        checked = record_prefight_checks(
            self.connection, [self.report_rows[0]], ingestion_run_id=duplicate_receipt,
        )
        self.assertFalse(checked[0]["alert_eligible"])
        self.assertEqual(checked[0]["alert_reason"], "quote_source_unverified")
        self.assertEqual(record_paper_candidates(self.connection, checked, 1000), [])
        saved = self.connection.execute(
            "SELECT gate_decision, gate_reason FROM prefight_gate_checks WHERE gate_check_id = ?",
            (checked[0]["gate_check_id"],),
        ).fetchone()
        self.assertEqual((saved["gate_decision"], saved["gate_reason"]),
                         ("reject", "quote_source_unverified"))

    def test_quote_payload_changed_after_gate_blocks_paper_record(self):
        receipt = self.connection.execute(
            "SELECT payload_path FROM ingestion_runs WHERE run_id = ?", (self.receipt_id,),
        ).fetchone()
        Path(receipt["payload_path"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "quote source is missing or changed"):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)

    def test_gate_checks_are_immutable_and_scored_evidence_cannot_be_changed(self):
        with self.assertRaisesRegex(Exception, "immutable"):
            self.connection.execute(
                "UPDATE prefight_gate_checks SET gate_decision = 'reject' WHERE gate_check_id = ?",
                (self.report_rows[0]["gate_check_id"],),
            )
        tampered = dict(self.report_rows[0], decimal_odds=9.0)
        with self.assertRaisesRegex(ValueError, "differ from saved evidence"):
            record_prefight_checks(
                self.connection, [tampered], ingestion_run_id=self.receipt_id,
            )
        raw_path = Path(self.connection.execute(
            "SELECT payload_path FROM ingestion_runs WHERE run_id = ?", (self.receipt_id,)
        ).fetchone()[0])
        raw_path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "snapshot is missing or changed"):
            record_prefight_checks(
                self.connection, [self.report_rows[0]], ingestion_run_id=self.receipt_id,
            )

    def test_tampered_roster_blocks_new_gate_and_paper_decision(self):
        roster_file = Path(self.temp.name) / "roster.csv"
        roster_file.write_text("changed card evidence", encoding="utf-8")
        rejected = record_prefight_checks(
            self.connection, [self.report_rows[0]], ingestion_run_id=self.receipt_id,
        )
        self.assertEqual(rejected[0]["alert_reason"], "roster_payload_changed")
        self.assertFalse(rejected[0]["alert_eligible"])
        with self.assertRaisesRegex(ValueError, "roster changed or is no longer verifiable"):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)

    def test_newer_roster_supersedes_existing_gate(self):
        previous_id = self.report_rows[0]["roster_snapshot_id"]
        new_time = utc_string(self.now - timedelta(seconds=1))
        raw = Path(self.temp.name) / "roster-new.csv"
        raw.write_text("new review of the same card", encoding="utf-8")
        receipt_id = self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES (?, ?, ?, ?)",
            ("reviewed_csv", new_time, str(raw), hashlib.sha256(raw.read_bytes()).hexdigest()),
        ).lastrowid
        new_snapshot_id = self.connection.execute(
            """INSERT INTO card_event_snapshots(
                ingestion_run_id, event_id, source_observed_at_utc, observation_basis,
                source_url, source_revision_id, license_name, license_url, reviewed_by,
                event_name, event_date, start_time_utc, event_status, event_provider_status
            ) SELECT ?, event_id, ?, observation_basis, source_url, 'fixture-2',
                     license_name, license_url, reviewed_by, event_name, event_date,
                     start_time_utc, event_status, event_provider_status
              FROM card_event_snapshots WHERE event_snapshot_id = ?""",
            (receipt_id, new_time, previous_id),
        ).lastrowid
        self.connection.execute(
            """INSERT INTO card_bout_snapshots(
                event_snapshot_id, bout_id, fighter_a_id, fighter_a_name,
                fighter_b_id, fighter_b_name, bout_status, bout_provider_status,
                weight_class, outcome, winner_fighter_id, method
            ) SELECT ?, bout_id, fighter_a_id, fighter_a_name,
                     fighter_b_id, fighter_b_name, bout_status, bout_provider_status,
                     weight_class, outcome, winner_fighter_id, method
              FROM card_bout_snapshots WHERE event_snapshot_id = ?""",
            (new_snapshot_id, previous_id),
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "roster changed or is no longer verifiable"):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)

    def test_direct_accepted_gate_without_roster_is_rejected_by_database(self):
        columns = [row[1] for row in self.connection.execute(
            "PRAGMA table_info(prefight_gate_checks)"
        ) if row[1] not in {"gate_check_id", "roster_snapshot_id"}]
        names = ", ".join(columns)
        with self.assertRaisesRegex(Exception, "dated roster evidence"):
            self.connection.execute(
                f"INSERT INTO prefight_gate_checks({names}) "
                f"SELECT {names} FROM prefight_gate_checks WHERE gate_check_id = ?",
                (self.report_rows[0]["gate_check_id"],),
            )

    def test_newer_odds_import_or_expired_gate_blocks_paper_recording(self):
        with patch("ufc_odds_model.paper.utc_now", return_value=self.now + timedelta(seconds=61)):
            with self.assertRaisesRegex(ValueError, "gate check is too old"):
                record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        self.connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256, snapshot_at_utc) "
            "SELECT source, fetched_at_utc, payload_path, sha256, snapshot_at_utc "
            "FROM ingestion_runs WHERE run_id = ?", (self.receipt_id,),
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "newer live odds import"):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)

    def test_newer_saved_quote_blocks_reuse_of_checked_price(self):
        db.add_quote(
            self.connection, "bout-0", "fixture-book", "fighter-0-a", 2.2,
            utc_string(self.now), "fixture", utc_string(self.now),
        )
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "newer quote superseded"):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)

    def test_database_rejects_direct_paper_insert_without_gate(self):
        row = self.report_rows[0]
        with self.assertRaisesRegex(Exception, "matching accepted pre-fight gate"):
            self.connection.execute(
                """INSERT INTO paper_bets(prediction_id, quote_id, event_id, bout_id,
                   selection_fighter_id, bookmaker, decision_at_utc, recorded_at_utc,
                   quote_captured_at_utc, quoted_decimal_odds, model_probability,
                   expected_profit_per_unit, bankroll_units, per_bet_cap_units,
                   event_cap_units, stake_units)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["prediction_id"], row["quote_id"], row["event_id"], row["bout_id"],
                 "fighter-0-a", "fixture-book", self.cutoff, utc_string(self.now),
                 self.quote_time, 2.3, 0.6, 0.38, 1000, 10, 50, 10),
            )

    def test_invalid_inputs_and_mismatched_quote_are_rejected(self):
        for value in (0, -1, math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(ValueError):
                record_paper_candidates(self.connection, self.report_rows[:1], value)
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000, 1.01)
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000,
                                    max_report_age_minutes=math.nan)
        wrong = dict(self.report_rows[0], decimal_odds=99.0)
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, [wrong], 1000)
        future = dict(self.report_rows[0], as_of_utc=utc_string(self.now - timedelta(hours=2)))
        with self.assertRaises(ValueError):
            record_paper_candidates(self.connection, [future], 1000)
        with patch(
            "ufc_odds_model.paper.utc_now",
            return_value=self.now - timedelta(minutes=6),
        ), self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        with patch(
            "ufc_odds_model.paper.utc_now",
            return_value=self.now + timedelta(days=7, minutes=1),
        ), self.assertRaises(ValueError):
            record_paper_candidates(self.connection, self.report_rows[:1], 1000)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
