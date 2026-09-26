"""Offline contract tests for the Sportradar MMA Daily Summaries adapter."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db, pipeline, sportradar


def summary(
    bout_number: int,
    *,
    category: str = sportradar.UFC_CATEGORY_ID,
    season_number: int = 10,
    fighter_numbers: tuple[int, int] = (1, 2),
    status: str = "not_started",
    winner: str | None = None,
    winner_id: str | None = None,
    replaced_by: str | None = None,
) -> dict:
    status_data: dict = {"status": status, "scheduled_length": 3, "weight_class": "lightweight"}
    if winner is not None:
        status_data["winner"] = winner
    if winner_id is not None:
        status_data["winner_id"] = winner_id
    sport_event = {
        "id": f"sr:sport_event:{bout_number}",
        "start_time": "2026-10-10T23:00:00+00:00",
        "sport_event_context": {
            "category": {"id": category, "name": "UFC"},
            "season": {
                "id": f"sr:season:{season_number}",
                "name": "UFC Test Card 2026",
                "start_date": "2026-10-10",
            },
        },
        "competitors": [
            {"id": f"sr:competitor:{fighter_numbers[0]}", "name": "Alpha, Alice", "qualifier": "home"},
            {"id": f"sr:competitor:{fighter_numbers[1]}", "name": "Bravo, Bob", "qualifier": "away"},
        ],
    }
    if replaced_by is not None:
        sport_event["replaced_by"] = replaced_by
    return {"sport_event": sport_event, "sport_event_status": status_data}


class SportradarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.connection)
        self.addCleanup(self.connection.close)

    def test_normalize_verified_ufc_only_and_stable_ids(self) -> None:
        payload = {"summaries": [
            summary(100),
            summary(200, category="sr:category:9999"),
        ]}
        events = sportradar.normalize_daily_summaries(payload)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_event_id"], "sr:season:10")
        self.assertEqual(events[0]["event_date"], "2026-10-10")
        self.assertEqual(events[0]["bouts"][0]["source_bout_id"], "sr:sport_event:100")
        self.assertEqual(events[0]["bouts"][0]["fighters"][0]["name"], "Alice Alpha")
        self.assertEqual(events[0]["bouts"][0]["status"], "scheduled")

    def test_draw_no_contest_and_winner_by_id(self) -> None:
        payload = {"summaries": [
            summary(100, status="closed", winner="draw"),
            summary(101, status="closed", winner="no_contest", fighter_numbers=(3, 4)),
            summary(102, status="closed", winner="away_team", winner_id="sr:competitor:6", fighter_numbers=(5, 6)),
        ]}
        bouts = sportradar.normalize_daily_summaries(payload)[0]["bouts"]
        self.assertEqual([bout["outcome"] for bout in bouts], ["draw", "no_contest", "win"])
        self.assertEqual(bouts[2]["winner_source_id"], "sr:competitor:6")

    def test_replacement_keeps_old_bout_cancelled_under_its_own_id(self) -> None:
        payload = {"summaries": [
            summary(100, replaced_by="sr:sport_event:101"),
            summary(101, fighter_numbers=(1, 3)),
        ]}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sportradar, "fetch_daily_summaries", return_value=payload
        ):
            result = sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
            self.assertTrue(Path(result["raw_path"]).exists())
        bouts = self.connection.execute(
            "SELECT source_bout_id, status FROM bouts ORDER BY source_bout_id"
        ).fetchall()
        self.assertEqual([(row["source_bout_id"], row["status"]) for row in bouts], [
            ("sr:sport_event:100", "cancelled"), ("sr:sport_event:101", "scheduled")
        ])
        event = self.connection.execute("SELECT status FROM events").fetchone()
        self.assertEqual(event["status"], "scheduled")

    def test_import_results_and_idempotent_replay(self) -> None:
        payload = {"summaries": [summary(
            100, status="closed", winner="home_team", winner_id="sr:competitor:1"
        )]}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sportradar, "fetch_daily_summaries", return_value=payload
        ):
            first = sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
            raw_path = Path(first["raw_path"])
            raw_bytes = raw_path.read_bytes()
            self.assertEqual(raw_path.name, f"{hashlib.sha256(raw_bytes).hexdigest()}.json")
            os.utime(raw_path, ns=(1_000_000_000, 1_000_000_000))
            retained_mtime = raw_path.stat().st_mtime_ns
            second = sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
            self.assertEqual(first["results"], 1)
            self.assertEqual(second["raw_path"], first["raw_path"])
            self.assertEqual(raw_path.stat().st_mtime_ns, retained_mtime)
            self.assertEqual(json.loads(raw_bytes)["summaries"], payload["summaries"])
        for table in ("events", "bouts", "results"):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)
        result = self.connection.execute("SELECT outcome, winner_fighter_id FROM results").fetchone()
        self.assertEqual((result["outcome"], result["winner_fighter_id"]),
                         ("win", "sportradar:sr:competitor:1"))

    def test_tampered_raw_snapshot_refuses_replay_without_new_receipt(self) -> None:
        payload = {"summaries": [summary(100)]}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sportradar, "fetch_daily_summaries", return_value=payload
        ):
            imported = sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
            raw_path = Path(imported["raw_path"])
            raw_path.write_bytes(b"tampered")
            before = self.connection.execute(
                "SELECT COUNT(*) FROM ingestion_runs WHERE source = ?", (sportradar.SOURCE,)
            ).fetchone()[0]
            with self.assertRaises(ValueError):
                sportradar.import_daily_summaries(
                    self.connection, "test-key", "2026-10-10", raw_dir=directory
                )
            self.assertEqual(raw_path.read_bytes(), b"tampered")
            self.assertEqual(self.connection.execute(
                "SELECT COUNT(*) FROM ingestion_runs WHERE source = ?", (sportradar.SOURCE,)
            ).fetchone()[0], before)

    def test_symlink_raw_snapshot_refuses_replay(self) -> None:
        payload = {"summaries": [summary(100)]}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sportradar, "fetch_daily_summaries", return_value=payload
        ):
            imported = sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
            raw_path = Path(imported["raw_path"])
            original = raw_path.read_bytes()
            raw_path.unlink()
            alternate = Path(directory) / "alternate.json"
            alternate.write_bytes(original)
            raw_path.symlink_to(alternate)
            with self.assertRaises(ValueError):
                sportradar.import_daily_summaries(
                    self.connection, "test-key", "2026-10-10", raw_dir=directory
                )
            self.assertEqual(alternate.read_bytes(), original)

    def test_reviewed_external_id_reuses_existing_fighter_history(self) -> None:
        db.upsert_fighter(self.connection, "ufcstats:alice", "Alice Alpha", "ufcstats", "alice")
        db.link_external_fighter(self.connection, "sportradar", "sr:competitor:1", "ufcstats:alice")
        payload = {"summaries": [summary(
            100, status="closed", winner="home_team", winner_id="sr:competitor:1"
        )]}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sportradar, "fetch_daily_summaries", return_value=payload
        ):
            sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM fighters").fetchone()[0], 2)
        bout = self.connection.execute("SELECT fighter_a_id FROM bouts").fetchone()
        result = self.connection.execute("SELECT winner_fighter_id FROM results").fetchone()
        self.assertEqual(bout["fighter_a_id"], "ufcstats:alice")
        self.assertEqual(result["winner_fighter_id"], "ufcstats:alice")

    def test_live_status_is_recorded_and_blocks_scoring(self) -> None:
        payload = {"summaries": [summary(100, status="live"), summary(101, fighter_numbers=(3, 4))]}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sportradar, "fetch_daily_summaries", return_value=payload
        ):
            imported = sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
        self.assertEqual(imported["non_scoreable_bouts"], 1)
        row = self.connection.execute(
            "SELECT status, provider_status FROM bouts WHERE source_bout_id = 'sr:sport_event:100'"
        ).fetchone()
        self.assertEqual((row["status"], row["provider_status"]), ("scheduled", "live"))
        self.assertEqual(self.connection.execute("SELECT provider_status FROM events").fetchone()[0], "live")
        available = db.event_bouts(self.connection, "sportradar:sr:season:10")
        self.assertEqual([row["source_bout_id"] for row in available], ["sr:sport_event:101"])
        with self.assertRaisesRegex(ValueError, "not available"):
            pipeline.score_event(
                self.connection, "sportradar:sr:season:10",
                datetime(2026, 10, 10, 22, tzinfo=timezone.utc),
            )

    def test_refresh_from_scheduled_to_live_blocks_existing_bout(self) -> None:
        scheduled = {"summaries": [summary(100)]}
        live = {"summaries": [summary(100, status="live")]}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sportradar, "fetch_daily_summaries", return_value=scheduled):
                sportradar.import_daily_summaries(self.connection, "test-key", "2026-10-10", raw_dir=directory)
            with patch.object(sportradar, "fetch_daily_summaries", return_value=live):
                sportradar.import_daily_summaries(self.connection, "test-key", "2026-10-10", raw_dir=directory)
        row = self.connection.execute("SELECT status, provider_status FROM bouts").fetchone()
        self.assertEqual((row["status"], row["provider_status"]), ("scheduled", "live"))
        self.assertEqual(db.event_bouts(self.connection, "sportradar:sr:season:10"), [])

    def test_cancelled_without_current_competitors_updates_existing_bout(self) -> None:
        scheduled = {"summaries": [summary(100)]}
        cancelled = summary(100, status="cancelled")
        cancelled["sport_event"]["competitors"] = []
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sportradar, "fetch_daily_summaries", return_value=scheduled):
                sportradar.import_daily_summaries(self.connection, "test-key", "2026-10-10", raw_dir=directory)
            with patch.object(sportradar, "fetch_daily_summaries", return_value={"summaries": [cancelled]}):
                result = sportradar.import_daily_summaries(
                    self.connection, "test-key", "2026-10-10", raw_dir=directory
                )
        self.assertEqual(result["skipped_incomplete"], 1)
        row = self.connection.execute("SELECT status, provider_status FROM bouts").fetchone()
        self.assertEqual((row["status"], row["provider_status"]), ("cancelled", "cancelled"))

    def test_stale_scheduled_snapshot_does_not_erase_completed_result(self) -> None:
        finished = {"summaries": [summary(100, status="closed", winner="home_team")]}
        old = {"summaries": [summary(100)]}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sportradar, "fetch_daily_summaries", return_value=finished):
                sportradar.import_daily_summaries(self.connection, "test-key", "2026-10-10", raw_dir=directory)
            with patch.object(sportradar, "fetch_daily_summaries", return_value=old):
                sportradar.import_daily_summaries(self.connection, "test-key", "2026-10-10", raw_dir=directory)
        row = self.connection.execute("SELECT status, provider_status FROM bouts").fetchone()
        self.assertEqual((row["status"], row["provider_status"]), ("completed", "closed"))
        self.assertEqual(self.connection.execute("SELECT status FROM events").fetchone()[0], "completed")

    def test_ended_without_outcome_is_auditable_and_not_scoreable(self) -> None:
        payload = {"summaries": [summary(100, status="ended")]}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sportradar, "fetch_daily_summaries", return_value=payload
        ):
            imported = sportradar.import_daily_summaries(
                self.connection, "test-key", "2026-10-10", raw_dir=directory
            )
        self.assertEqual(imported["skipped_incomplete"], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)
        bout = self.connection.execute("SELECT status, provider_status FROM bouts").fetchone()
        self.assertEqual((bout["status"], bout["provider_status"]), ("scheduled", "ended"))
        self.assertEqual(self.connection.execute("SELECT provider_status FROM events").fetchone()[0], "ended")
        self.assertEqual(db.event_bouts(self.connection, "sportradar:sr:season:10"), [])

    def test_identity_and_winner_conflicts_fail_closed(self) -> None:
        invalid = summary(100, status="closed", winner="away_team", winner_id="sr:competitor:1")
        with self.assertRaisesRegex(ValueError, "conflicting winner"):
            sportradar.normalize_daily_summaries({"summaries": [invalid]})
        duplicate = summary(100)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            sportradar.normalize_daily_summaries({"summaries": [duplicate, duplicate]})
        replaced_finished = summary(
            101, status="closed", winner="home_team", replaced_by="sr:sport_event:102"
        )
        with self.assertRaisesRegex(ValueError, "also has replaced_by"):
            sportradar.normalize_daily_summaries({"summaries": [replaced_finished]})

    def test_fetch_uses_documented_endpoint_and_header(self) -> None:
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        seen = {}

        def fake_urlopen(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return Response(b'{"summaries": []}')

        with patch.object(sportradar, "urlopen", fake_urlopen):
            self.assertEqual(sportradar.fetch_daily_summaries("secret", "2026-10-10"), {"summaries": []})
        self.assertEqual(seen["request"].full_url,
                         "https://api.sportradar.com/mma/trial/v2/en/schedules/2026-10-10/summaries.json")
        self.assertEqual(seen["request"].get_header("X-api-key"), "secret")
        self.assertEqual(seen["timeout"], 20)


if __name__ == "__main__":
    unittest.main()
