"""Dated roster observations must be receipt-linked and never backdated."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.audit import audit_database
from ufc_odds_model.card_history import _receipt_payload_problem, latest_prefight_roster
from ufc_odds_model.importers import import_bouts_csv


HEADER = (
    "event_id,event_name,event_date,event_status,start_time_utc,"
    "source_observed_at_utc,source_url,source_revision_id,license_name,"
    "license_url,reviewed_by,bout_id,fighter_a_id,fighter_a_name,"
    "fighter_b_id,fighter_b_name,bout_status,weight_class,outcome,"
    "winner_fighter_id,method\n"
)


def row(
    bout_id: str = "old", fighter_b_id: str = "b", fighter_b_name: str = "Beta",
    bout_status: str = "scheduled", observed: str = "2026-09-26T11:00:00Z",
    source_url: str = "", source_revision_id: str = "", license_name: str = "",
    license_url: str = "", reviewed_by: str = "",
) -> str:
    return (
        f"event,Verified card,2026-10-10,scheduled,2026-10-10T20:00:00Z,"
        f"{observed},{source_url},{source_revision_id},{license_name},{license_url},"
        f"{reviewed_by},{bout_id},a,Alpha,{fighter_b_id},{fighter_b_name},"
        f"{bout_status},Lightweight,,,\n"
    )


class CardHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "card.csv"
        self.connection = db.connect(self.root / "ufc.sqlite")
        self.addCleanup(self.connection.close)
        db.init_db(self.connection)
        self.imported = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)

    def import_rows(self, *rows: str, at: datetime | None = None) -> int:
        self.source.write_text(HEADER + "".join(rows), encoding="utf-8")
        with patch("ufc_odds_model.importers.utc_now", return_value=at or self.imported):
            return import_bouts_csv(self.connection, self.source, self.root / "raw")

    def test_explicit_time_is_preserved_with_status_matchup_and_receipt(self) -> None:
        self.assertEqual(self.import_rows(row(
            observed="2026-09-26T07:00:00-04:00",
            source_url="https://example.test/revision/123", source_revision_id="123",
            license_name="CC BY-SA 4.0", license_url="https://example.test/license",
            reviewed_by="reviewer-1",
        )), 1)
        saved = self.connection.execute(
            """SELECT s.*, r.source, r.fetched_at_utc, r.sha256
               FROM card_event_snapshots s JOIN ingestion_runs r
               ON r.run_id = s.ingestion_run_id"""
        ).fetchone()
        self.assertEqual(saved["source_observed_at_utc"], "2026-09-26T11:00:00Z")
        self.assertEqual(saved["fetched_at_utc"], "2026-09-26T12:00:00Z")
        self.assertEqual(saved["observation_basis"], "reviewed_csv")
        self.assertEqual(saved["source"], "manual-csv")
        self.assertEqual(saved["source_url"], "https://example.test/revision/123")
        self.assertEqual(saved["source_revision_id"], "123")
        self.assertEqual(saved["license_name"], "CC BY-SA 4.0")
        self.assertEqual(saved["license_url"], "https://example.test/license")
        self.assertEqual(saved["reviewed_by"], "reviewer-1")
        self.assertEqual(len(saved["sha256"]), 64)
        bout = self.connection.execute("SELECT * FROM card_bout_snapshots").fetchone()
        self.assertEqual(bout["fighter_a_id"], "a")
        self.assertEqual(bout["fighter_b_id"], "b")
        self.assertEqual(bout["bout_status"], "scheduled")
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", self.imported)["reason"],
            "ok",
        )
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", self.imported)["ingestion_run_id"],
            saved["ingestion_run_id"],
        )
        for table, id_field, value in (
            ("card_event_snapshots", "event_snapshot_id", saved["event_snapshot_id"]),
            ("card_bout_snapshots", "bout_snapshot_id", bout["bout_snapshot_id"]),
        ):
            with self.assertRaisesRegex(Exception, "immutable"):
                self.connection.execute(
                    f"UPDATE {table} SET {id_field} = {id_field} WHERE {id_field} = ?",
                    (value,),
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                self.connection.execute(f"DELETE FROM {table} WHERE {id_field} = ?", (value,))

    def test_missing_time_stays_unknown_and_cannot_support_prefight_claim(self) -> None:
        self.import_rows(row(observed=""))
        snapshot = self.connection.execute("SELECT * FROM card_event_snapshots").fetchone()
        self.assertIsNone(snapshot["source_observed_at_utc"])
        self.assertEqual(snapshot["observation_basis"], "unknown")
        result = latest_prefight_roster(self.connection, "event", "old", self.imported)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "missing_roster_observation_time")
        report = audit_database(self.connection, as_of=self.imported)
        self.assertEqual(report["summary"]["card_snapshots_without_source_observation_time"], 1)
        self.assertIn("missing_roster_observation_time", {issue["code"] for issue in report["issues"]})

    def test_stale_roster_observation_cannot_support_a_decision(self) -> None:
        self.import_rows(row())
        later = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", later)["reason"],
            "stale_roster_observation",
        )

    def test_selected_receipt_payload_must_exist_match_hash_and_be_regular(self) -> None:
        self.import_rows(row())
        receipt = self.connection.execute("SELECT payload_path FROM ingestion_runs").fetchone()
        path = Path(receipt["payload_path"])
        original = path.read_bytes()
        path.write_bytes(b"changed source")
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", self.imported)["reason"],
            "roster_payload_changed",
        )
        path.unlink()
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", self.imported)["reason"],
            "roster_payload_missing",
        )
        alternate = self.root / "alternate.csv"
        alternate.write_bytes(original)
        path.symlink_to(alternate)
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", self.imported)["reason"],
            "roster_payload_symlink",
        )
        path.unlink()
        path.write_bytes(original)
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", self.imported)["reason"],
            "ok",
        )

    def test_parent_directory_alias_does_not_make_regular_payload_a_symlink(self) -> None:
        actual_dir = self.root / "actual"
        actual_dir.mkdir()
        actual = actual_dir / "roster.csv"
        actual.write_bytes(b"reviewed roster")
        alias = self.root / "alias"
        alias.symlink_to(actual_dir, target_is_directory=True)
        self.assertIsNone(_receipt_payload_problem(
            str(alias / "roster.csv"), hashlib.sha256(actual.read_bytes()).hexdigest(),
        ))

    def test_late_observation_is_preserved_and_flagged(self) -> None:
        at = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        self.import_rows(row(observed="2026-10-10T21:00:00Z"), at=at)
        report = audit_database(self.connection, as_of=at)
        self.assertEqual(report["summary"]["card_snapshots_observed_at_or_after_start"], 1)
        self.assertIn("roster_observed_at_or_after_start", {issue["code"] for issue in report["issues"]})
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", at)["reason"],
            "roster_observed_at_or_after_start",
        )

    def test_two_receipts_preserve_substitution_and_latest_matchup(self) -> None:
        self.import_rows(row())
        later = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
        self.import_rows(
            row(bout_status="cancelled", observed="2026-09-27T11:00:00Z"),
            row("new", "c", "Gamma", observed="2026-09-27T11:00:00Z"),
            at=later,
        )
        history = self.connection.execute(
            """SELECT r.run_id, e.source_observed_at_utc, b.bout_id,
                      b.bout_status, b.fighter_b_id
               FROM card_bout_snapshots b
               JOIN card_event_snapshots e USING(event_snapshot_id)
               JOIN ingestion_runs r ON r.run_id = e.ingestion_run_id
               ORDER BY r.run_id, b.bout_id"""
        ).fetchall()
        self.assertEqual(
            [(r["bout_id"], r["bout_status"], r["fighter_b_id"]) for r in history],
            [("old", "scheduled", "b"), ("new", "scheduled", "c"),
             ("old", "cancelled", "b")],
        )
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "old", later)["reason"],
            "roster_matchup_mismatch",
        )
        self.assertEqual(
            latest_prefight_roster(self.connection, "event", "new", later)["reason"],
            "ok",
        )
        report = audit_database(self.connection, as_of=later)
        self.assertEqual(report["summary"]["roster_possible_opponent_substitutions"], 1)
        self.assertIn("roster_possible_opponent_substitution",
                      {issue["code"] for issue in report["issues"]})

    def test_conflicting_or_future_time_rejected_before_writes(self) -> None:
        for rows, expected in (
            ((row(observed="2026-09-27T00:00:00Z"),), "after import time"),
            ((row(observed="2026-09-26T12:00:00.100000Z"),), "after import time"),
            ((row(observed="2026-09-26"),), "timezone-aware"),
            ((row(), row("new", "c", "Gamma", observed="")), "conflicting event metadata"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    self.import_rows(*rows)
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0], 0)
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                self.assertFalse((self.root / "raw").exists())


if __name__ == "__main__":
    unittest.main()
