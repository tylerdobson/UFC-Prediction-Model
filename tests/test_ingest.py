"""Historical odds must preserve snapshot time and match exactly one bout."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.ingest import import_odds_payload


class OddsIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = db.connect(self.root / "test.sqlite")
        db.init_db(self.connection)
        db.upsert_fighter(self.connection, "a", "Doe, Jane", "source", "a")
        db.upsert_fighter(self.connection, "b", "Alex Smith", "source", "b")
        db.upsert_event(
            self.connection, "event", "UFC card", "2026-10-04", "completed",
            start_time_utc="2026-10-04T01:00:00Z",
        )
        db.upsert_bout(self.connection, "bout", "event", "a", "b", "completed")
        db.upsert_result(self.connection, "bout", "win", "a", "2026-10-04T03:00:00Z")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_historical_match_keeps_snapshot_time_and_is_idempotent(self):
        snapshot = "2026-10-03T00:00:00Z"
        payload = {"timestamp": snapshot, "data": [self._event("2026-10-02T23:59:00Z")]}
        first = import_odds_payload(
            self.connection, payload, snapshot, self.root / "raw", historical=True
        )
        second = import_odds_payload(
            self.connection, payload, snapshot, self.root / "raw", historical=True
        )
        self.assertEqual(first["matched_quotes"], 2)
        self.assertEqual(second["matched_quotes"], 2)
        rows = self.connection.execute(
            "SELECT selection_fighter_id, captured_at_utc, source FROM odds_quotes ORDER BY quote_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["selection_fighter_id"] for row in rows}, {"a", "b"})
        self.assertTrue(all(row["captured_at_utc"] == snapshot for row in rows))
        self.assertTrue(all(row["source"] == "the-odds-api-historical" for row in rows))

    def test_future_bookmaker_update_is_not_saved(self):
        snapshot = "2026-10-03T00:00:00Z"
        payload = {"timestamp": snapshot, "data": [self._event("2026-10-03T00:01:00Z")]}
        result = import_odds_payload(
            self.connection, payload, snapshot, self.root / "raw", historical=True
        )
        self.assertEqual(result["future_timestamp_quotes"], 2)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM odds_quotes").fetchone()[0], 0)

    def test_live_and_historical_snapshots_are_content_addressed_and_not_overwritten(self):
        snapshot = "2026-10-03T00:00:00Z"
        event = self._event("2026-10-02T23:59:00Z")
        for historical, payload in (
            (False, [event]),
            (True, {"timestamp": snapshot, "data": [event]}),
        ):
            with self.subTest(historical=historical):
                raw_dir = self.root / ("historical" if historical else "live")
                first = import_odds_payload(
                    self.connection, payload, snapshot, raw_dir, historical=historical,
                )
                raw_path = Path(first["raw_path"])
                serialized = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
                digest = hashlib.sha256(serialized).hexdigest()
                self.assertEqual(raw_path.name, f"{digest}.json")
                self.assertEqual(raw_path.read_bytes(), serialized)
                second = import_odds_payload(
                    self.connection, payload, snapshot, raw_dir, historical=historical,
                )
                self.assertEqual(second["raw_path"], first["raw_path"])
                self.assertEqual(raw_path.read_bytes(), serialized)
                before = self.connection.execute(
                    "SELECT COUNT(*) FROM ingestion_runs WHERE source = ?",
                    ("the-odds-api-historical" if historical else "the-odds-api",),
                ).fetchone()[0]
                raw_path.write_bytes(b"tampered evidence")
                with self.assertRaisesRegex(ValueError, "differs from its content address"):
                    import_odds_payload(
                        self.connection, payload, snapshot, raw_dir, historical=historical,
                    )
                self.assertEqual(raw_path.read_bytes(), b"tampered evidence")
                self.assertEqual(self.connection.execute(
                    "SELECT COUNT(*) FROM ingestion_runs WHERE source = ?",
                    ("the-odds-api-historical" if historical else "the-odds-api",),
                ).fetchone()[0], before)

    def _event(self, updated_at: str) -> dict:
        return {
            "id": "odds-fight", "commence_time": "2026-10-04T01:00:00Z",
            "home_team": "Jane Doe", "away_team": "Alex Smith",
            "bookmakers": [{"key": "book", "markets": [{
                "key": "h2h", "last_update": updated_at,
                "outcomes": [{"name": "Jane Doe", "price": 1.8},
                             {"name": "Alex Smith", "price": 2.1}],
            }]}],
        }


if __name__ == "__main__":
    unittest.main()
