"""The dashboard must display persisted evidence without changing it."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.card_history import record_card_snapshots
from ufc_odds_model.dashboard_data import load_dashboard
from ufc_odds_model.demo import seed_demo
from ufc_odds_model.integrity import verify_evidence
from ufc_odds_model.pipeline import utc_string
from ufc_odds_model.research_evaluation import RESEARCH_EVALUATION_VERSION, research_feature_rows
from ufc_odds_model.wikipedia_history import import_wikipedia_pages, parse_event_page_title


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
        observed = utc_string(self.now - timedelta(seconds=45))
        roster_file = Path(self.temp.name) / "roster.csv"
        roster_file.write_text("fixture roster", encoding="utf-8")
        roster_receipt = connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES (?, ?, ?, ?)",
            ("reviewed_csv", observed, str(roster_file),
             hashlib.sha256(roster_file.read_bytes()).hexdigest()),
        ).lastrowid
        record_card_snapshots(connection, int(roster_receipt), [{
            "event_id": "event", "event_name": "Fixture card",
            "event_date": (self.now + timedelta(days=7)).date().isoformat(),
            "event_status": "scheduled", "event_provider_status": None,
            "start_time_utc": utc_string(self.now + timedelta(days=7)),
            "source_observed_at_utc": observed,
            "observation_basis": "reviewed_csv", "source_url": "fixture://roster",
            "source_revision_id": "fixture-1", "license_name": "fixture",
            "license_url": "fixture://license", "reviewed_by": "test",
            "bouts": [{
                "event_id": "event", "bout_id": "bout", "fighter_a_id": "a",
                "fighter_a_name": "Alice Example", "fighter_b_id": "b",
                "fighter_b_name": "Bob Example", "bout_status": "scheduled",
                "bout_provider_status": None, "weight_class": None,
                "outcome": None, "winner_fighter_id": None, "method": None,
            }],
        }])
        roster_snapshot_id = connection.execute(
            "SELECT MAX(event_snapshot_id) FROM card_event_snapshots"
        ).fetchone()[0]
        receipt_id = connection.execute(
            "SELECT run_id FROM ingestion_runs WHERE source = 'the-odds-api' ORDER BY run_id DESC LIMIT 1"
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
                conservative_expected_profit_per_dollar, roster_snapshot_id
            ) VALUES (?, 'event', 'bout', ?, ?, ?, ?, ?, 'test-v1', ?,
                      'Fixture Book', ?, ?, 2.20, 0.61, 'candidate',
                      'alert_candidate', 'ok', 60, 0.05, 0.03, 2.15, 0.3115, ?)
            """,
            (receipt_id, prediction_id, quote_id, snapshot, checked, start,
             cutoff, snapshot, snapshot, roster_snapshot_id),
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
        self.assertEqual(snapshot["historical_research"]["events"], 0)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        json.dumps(snapshot)

    def test_historical_research_counts_imported_completed_bouts_only(self) -> None:
        connection = self._connect()
        for fighter_id in ("one", "two", "three", "four"):
            db.upsert_fighter(connection, fighter_id, fighter_id.title(),
                              "wikipedia_research", fighter_id)
        for event_id, page_id, event_date in (
            ("old", "1101", "2011-02-05"),
            ("new", "2501", "2025-11-15"),
        ):
            db.upsert_event(connection, event_id, f"UFC {event_id}", event_date,
                            "completed", source="wikipedia_research",
                            source_event_id=page_id)
        for bout_id, event_id, a, b, outcome, winner in (
            ("old-a", "old", "one", "two", "win", "one"),
            ("old-b", "old", "three", "four", "draw", None),
            ("new-a", "new", "one", "three", "no_contest", None),
        ):
            db.upsert_bout(connection, bout_id, event_id, a, b, "completed",
                           source="wikipedia_research", source_bout_id=bout_id)
            db.upsert_result(connection, bout_id, outcome, winner,
                             "2026-09-26T15:00:00Z")
        db.upsert_event(connection, "fixture", "Non-research event", "2025-01-01",
                        "completed", source="fixture", source_event_id="fixture")
        db.upsert_bout(connection, "fixture-bout", "fixture", "one", "two",
                       "completed", source="fixture", source_bout_id="fixture-bout")
        db.upsert_result(connection, "fixture-bout", "win", "two",
                         "2026-09-26T15:00:00Z")
        receipt_id = connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES ('wikipedia_research', ?, ?, ?)",
            ("2026-09-26T15:00:00Z", str(Path(self.temp.name) / "source.json"), "a" * 64),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO wikipedia_source_receipts(
                run_id, event_page_id, event_title, page_url, revision_id,
                revision_timestamp_utc, license_title, license_url,
                imported_bouts, skipped_unresolved_bouts
            ) VALUES (?, 1101, 'UFC old', 'https://en.wikipedia.org/wiki/UFC_old',
                      123, '2026-09-26T15:00:00Z', 'CC BY-SA 4.0',
                      'https://creativecommons.org/licenses/by-sa/4.0/', 2, 0)
            """, (receipt_id,),
        )
        connection.commit()
        before = self.path.stat().st_mtime_ns

        snapshot = load_dashboard(self.path, as_of=self.now)
        history = snapshot["historical_research"]
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["data_origin"], "research_mixed")
        self.assertEqual((history["events"], history["bouts"], history["results"],
                          history["fighters"], history["event_page_receipts"]),
                         (2, 3, 3, 4, 1))
        self.assertEqual(history["first_date"], "2011-02-05")
        self.assertEqual(history["last_date"], "2025-11-15")
        self.assertEqual(history["by_year"], [
            {"year": "2011", "events": 1, "bouts": 2, "results": 2},
            {"year": "2025", "events": 1, "bouts": 1, "results": 1},
        ])
        self.assertEqual([row["name"] for row in history["recent_events"]],
                         ["UFC new", "UFC old"])
        self.assertEqual(history["receipt_status"], "unavailable")
        self.assertIsNone(history["held_identity_rows"])
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        json.dumps(snapshot, allow_nan=False)

    def test_research_identity_coverage_uses_exact_checked_card_receipts(self) -> None:
        connection = self._connect()
        for fighter_id in ("a", "b"):
            db.upsert_fighter(connection, fighter_id, fighter_id.upper(),
                              "wikipedia_research", fighter_id)
        cards = (
            ("standalone", "101", "UFC Archive", "2025-01-11"),
            ("embedded-a", "202:ufc%20on%20fx%3A%20alpha", "UFC on FX: Alpha", "2026-01-01"),
            ("embedded-b", "202:ufc%20on%20fx%3A%20beta", "UFC on FX: Beta", "2026-02-01"),
        )
        for event_id, source_id, name, event_date in cards:
            db.upsert_event(connection, event_id, name, event_date, "completed",
                            source="wikipedia_research", source_event_id=source_id)
            db.upsert_bout(connection, event_id + "-bout", event_id, "a", "b",
                           "completed", source="wikipedia_research",
                           source_bout_id=event_id + "-bout")
            db.upsert_result(connection, event_id + "-bout", "win", "a",
                             "2026-02-01T15:00:00Z")

        raw = Path(self.temp.name) / "source.json"
        raw.write_text('{"source":"checked"}', encoding="utf-8")
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()

        def receipt(page_id: int, url: str, skipped: int, *,
                    revision: int = 10, imported: int = 1) -> None:
            run_id = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES ('wikipedia_research', ?, ?, ?)",
                ("2026-02-01T15:00:00Z", str(raw), digest),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO wikipedia_source_receipts(
                    run_id, event_page_id, event_title, page_url, revision_id,
                    revision_timestamp_utc, license_title, license_url,
                    imported_bouts, skipped_unresolved_bouts
                ) VALUES (?, ?, 'Fixture', ?, ?, '2026-02-01T15:00:00Z',
                          'CC BY-SA 4.0', 'https://creativecommons.org/licenses/by-sa/4.0/',
                          ?, ?)
                """, (run_id, page_id, url, revision, imported, skipped),
            )

        receipt(101, "https://en.wikipedia.org/wiki/UFC_Archive", 1)
        receipt(202, "https://en.wikipedia.org/wiki/2026_in_UFC#UFC_on_FX:_Alpha", 2)
        receipt(202, "https://en.wikipedia.org/wiki/2026_in_UFC#UFC_on_FX:_Beta", 0)
        receipt(202, "https://en.wikipedia.org/wiki/2026_in_UFC#UFC_on_FX:_Alpha", 2)
        connection.commit()

        history = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual(history["receipt_status"], "verified")
        self.assertEqual(history["receipt_events_verified"], 3)
        self.assertEqual((history["bouts"], history["source_bout_rows"],
                          history["held_identity_rows"], history["accepted_bout_coverage"]),
                         (3, 6, 3, 0.5))
        self.assertEqual([(row["year"], row["held_identity_rows"],
                           row["accepted_bout_coverage"]) for row in history["by_year"]],
                         [("2025", 1, 0.5), ("2026", 2, 0.5)])

        receipt(101, "https://en.wikipedia.org/wiki/UFC_Archive", 1,
                revision=11, imported=2)
        connection.commit()
        mismatched = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual(mismatched["receipt_status"], "unavailable")
        self.assertIsNone(mismatched["source_bout_rows"])
        receipt(101, "https://en.wikipedia.org/wiki/UFC_Archive", 1, revision=12)
        connection.commit()

        raw.write_text('{"source":"tampered"}', encoding="utf-8")
        tampered = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual(tampered["receipt_status"], "unavailable")
        self.assertIsNone(tampered["accepted_bout_coverage"])

        raw.write_text('{"source":"checked"}', encoding="utf-8")
        # One source revision cannot legitimately report two different held counts.
        receipt(202, "https://en.wikipedia.org/wiki/2026_in_UFC#UFC_on_FX:_Alpha", 9)
        connection.commit()
        conflicted = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual(conflicted["receipt_status"], "unavailable")
        self.assertIsNone(conflicted["held_identity_rows"])

    def test_reviewed_identity_replay_requires_intact_scoped_crosswalk(self) -> None:
        connection = self._connect()
        page = {
            "id": 74123456, "title": "UFC 999",
            "latest": {"id": 123, "timestamp": "2024-01-01T00:00:00Z"},
            "license": {"title": "CC BY-SA 4.0",
                        "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            "source": (
                "{{Infobox MMA event\n|name=UFC 999\n|date={{start date|2023|11|11}}\n}}\n"
                "==Results==\n{{MMAevent}}\n"
                "{{MMAevent bout|Women's Strawweight|[[Loma Lookboonmee]]|def.|"
                "Denise Gomes|Decision (unanimous)|3|5:00|}}\n==References=="
            ),
        }
        parsed = parse_event_page_title(page, "UFC 999")

        def fetch(url: str) -> dict:
            self.assertIn("/w/api.php?", url)
            return {"query": {"pages": [
                {"pageid": 62158685, "title": "Loma Lookboonmee", "ns": 0},
                {"pageid": 74765470, "title": "Denise Gomes", "ns": 0},
            ]}}

        raw_dir = Path(self.temp.name) / "raw"
        import_wikipedia_pages(connection, [(page, parsed)], raw_dir=raw_dir,
                               review_out=Path(self.temp.name) / "before.json", fetch_json=fetch)
        initial = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual((initial["bouts"], initial["held_identity_rows"]), (0, 1))

        crosswalk = Path(self.temp.name) / "decision.json"
        crosswalk.write_text(json.dumps({"schema_version": 2, "decisions": [{
            "event_id": "wikipedia_research:74123456", "bout_position": 1,
            "fighter_name": "Denise Gomes", "source_revision_id": 123,
            "source_receipt_sha256": hashlib.sha256(json.dumps(
                page, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
            "fighter_id": "wikipedia:74765470", "canonical_name": "Denise Gomes",
            "page_title": "Denise Gomes", "reviewed_by": "reviewer",
            "evidence_urls": ["https://www.ufc.com/event/ufc-999"],
        }]}))
        import_wikipedia_pages(connection, [(page, parsed)], raw_dir=raw_dir,
                               review_out=Path(self.temp.name) / "after.json",
                               crosswalk_path=crosswalk, fetch_json=fetch)
        reviewed = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual(reviewed["receipt_status"], "verified")
        self.assertEqual((reviewed["bouts"], reviewed["source_bout_rows"],
                          reviewed["held_identity_rows"]), (1, 1, 0))

        receipt = connection.execute(
            "SELECT payload_path FROM ingestion_runs WHERE source = 'wikipedia_identity_crosswalk'"
        ).fetchone()
        Path(receipt["payload_path"]).write_text('{"tampered":true}')
        tampered = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual(tampered["receipt_status"], "unavailable")
        self.assertIsNone(tampered["source_bout_rows"])

    def test_successive_identity_replays_preserve_prior_reviewed_positions(self) -> None:
        connection = self._connect()
        page = {
            "id": 74123457, "title": "UFC 1000",
            "latest": {"id": 124, "timestamp": "2024-01-01T00:00:00Z"},
            "license": {"title": "CC BY-SA 4.0",
                        "url": "https://creativecommons.org/licenses/by-sa/4.0/deed.en"},
            "source": (
                "{{Infobox MMA event\n|name=UFC 1000\n|date={{start date|2023|11|11}}\n}}\n"
                "==Results==\n{{MMAevent}}\n"
                "{{MMAevent bout|Women's Strawweight|[[Loma Lookboonmee]]|def.|"
                "Denise Gomes|Decision (unanimous)|3|5:00|}}\n"
                "{{MMAevent bout|Lightweight|[[Alex Pereira]]|def.|"
                "Second Newcomer|Decision (split)|3|5:00|}}\n==References=="
            ),
        }
        parsed = parse_event_page_title(page, "UFC 1000")

        def fetch(url: str) -> dict:
            return {"query": {"pages": [
                {"pageid": 62158685, "title": "Loma Lookboonmee", "ns": 0},
                {"pageid": 74765470, "title": "Denise Gomes", "ns": 0},
                {"pageid": 57730000, "title": "Alex Pereira", "ns": 0},
            ]}}

        root = Path(self.temp.name)
        raw_dir = root / "raw"
        source_sha = hashlib.sha256(json.dumps(
            page, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        first = {
            "event_id": "wikipedia_research:74123457", "bout_position": 1,
            "fighter_name": "Denise Gomes", "source_revision_id": 124,
            "source_receipt_sha256": source_sha,
            "fighter_id": "wikipedia:74765470", "canonical_name": "Denise Gomes",
            "page_title": "Denise Gomes", "reviewed_by": "reviewer",
            "evidence_urls": ["https://www.ufc.com/event/ufc-1000"],
        }
        second = {
            "event_id": "wikipedia_research:74123457", "bout_position": 2,
            "fighter_name": "Second Newcomer", "source_revision_id": 124,
            "source_receipt_sha256": source_sha,
            "fighter_id": "reviewed:second-newcomer", "canonical_name": "Second Newcomer",
            "reviewed_by": "reviewer",
            "evidence_urls": ["https://www.ufc.com/event/ufc-1000"],
        }
        import_wikipedia_pages(connection, [(page, parsed)], raw_dir=raw_dir,
                               review_out=root / "baseline.json", fetch_json=fetch)
        self.assertEqual(load_dashboard(self.path, as_of=self.now)["historical_research"]["held_identity_rows"], 2)
        crosswalk = root / "decision.json"
        crosswalk.write_text(json.dumps({"schema_version": 2, "decisions": [first]}))
        import_wikipedia_pages(connection, [(page, parsed)], raw_dir=raw_dir,
                               review_out=root / "first.json", crosswalk_path=crosswalk,
                               fetch_json=fetch)
        first_view = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual((first_view["receipt_status"], first_view["bouts"],
                          first_view["held_identity_rows"]), ("verified", 1, 1))
        crosswalk.write_text(json.dumps({"schema_version": 2, "decisions": [second]}))
        with self.assertRaisesRegex(ValueError, "prior imported bout is now absent or unresolved"):
            import_wikipedia_pages(connection, [(page, parsed)], raw_dir=raw_dir,
                                   review_out=root / "invalid.json", crosswalk_path=crosswalk,
                                   fetch_json=fetch)
        crosswalk.write_text(json.dumps({"schema_version": 2, "decisions": [first, second]}))
        import_wikipedia_pages(connection, [(page, parsed)], raw_dir=raw_dir,
                               review_out=root / "second.json", crosswalk_path=crosswalk,
                               fetch_json=fetch)
        second_view = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual((second_view["receipt_status"], second_view["bouts"],
                          second_view["held_identity_rows"]), ("verified", 2, 0))
        connection.execute("DELETE FROM results WHERE bout_id IN (SELECT bout_id FROM bouts WHERE event_id = ?)",
                           ("wikipedia_research:74123457",))
        connection.commit()
        missing_result = load_dashboard(self.path, as_of=self.now)["historical_research"]
        self.assertEqual(missing_result["receipt_status"], "unavailable")
        self.assertIsNone(missing_result["accepted_bout_coverage"])

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
            "evaluation": {
                "status": "insufficient_history", "split": None, "test": None,
                "available": {"bouts": 0},
                "prior_result_evidence": {
                    "threshold": 1.0, "promotion_eligible": False,
                    "available": {
                        "bouts": 0, "available_prior_result_instances": 0,
                        "observed_prior_result_instances": 0, "unverified_feature_rows": 0,
                        "coverage": None, "meets_threshold": False,
                    },
                },
            },
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
            "prior_result_evidence": {
                "threshold": 1.0, "promotion_eligible": True, "warning": None,
                **{
                    name: {
                        "bouts": 1, "available_prior_result_instances": 1,
                        "observed_prior_result_instances": 1,
                        "unverified_feature_rows": 0, "coverage": 1.0,
                        "meets_threshold": True,
                    } for name in ("train", "validation", "test")
                },
            },
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
        insufficient = {
            "status": "insufficient_history", "split": None, "test": None,
            "available": {"bouts": 0},
            "prior_result_evidence": {
                "threshold": 1.0, "promotion_eligible": False,
                "available": {
                    "bouts": 0, "available_prior_result_instances": 0,
                    "observed_prior_result_instances": 0, "unverified_feature_rows": 0,
                    "coverage": None, "meets_threshold": False,
                },
            },
        }
        self.assertEqual(status_for(insufficient)["status"], "insufficient_history")

        old_ok = json.loads(json.dumps(result))
        old_ok.pop("prior_result_evidence")
        self.assertEqual(status_for(old_ok)["status"], "invalid_report")

        exploratory = json.loads(json.dumps(result))
        exploratory["status"] = "insufficient_result_evidence"
        evidence = exploratory["prior_result_evidence"]
        evidence["train"].update(observed_prior_result_instances=0, coverage=0.0,
                                 meets_threshold=False)
        evidence["promotion_eligible"] = False
        evidence["warning"] = "Incomplete source-observed prior results."
        shown = status_for(exploratory)
        self.assertEqual(shown["status"], "insufficient_result_evidence")
        self.assertFalse(shown["result"]["prior_result_evidence"]["train"]["meets_threshold"])

        for changed in (
            {"observed_prior_result_instances": 2},
            {"coverage": 0.5},
            {"meets_threshold": True},
            {"unverified_feature_rows": 2},
        ):
            malformed = json.loads(json.dumps(exploratory))
            malformed["prior_result_evidence"]["train"].update(changed)
            self.assertEqual(status_for(malformed)["status"], "invalid_report")
        for key, value in (("threshold", 0.9), ("promotion_eligible", True)):
            malformed = json.loads(json.dumps(exploratory))
            malformed["prior_result_evidence"][key] = value
            self.assertEqual(status_for(malformed)["status"], "invalid_report")
        malformed = json.loads(json.dumps(result))
        malformed["prior_result_evidence"]["test"]["bouts"] = 2
        self.assertEqual(status_for(malformed)["status"], "invalid_report")
        malformed = json.loads(json.dumps(exploratory))
        malformed["status"] = "ok"
        self.assertEqual(status_for(malformed)["status"], "invalid_report")

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


    def test_saved_integrity_report_expires_and_detects_wal_only_database_change(self) -> None:
        connection = self._connect()
        payload = Path(self.temp.name) / "saved-odds.json"
        payload.write_bytes(b'{"source":"fixture"}')
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES ('fixture', ?, ?, ?)",
            (utc_string(self.now), str(payload), digest),
        )
        connection.commit()
        self.assertEqual(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES ('fixture', ?, ?, ?)",
            (utc_string(self.now), str(payload), digest),
        )
        connection.commit()

        report_path = Path(self.temp.name) / "integrity.json"
        report = verify_evidence(self.path)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["receipt_count"], 2)
        self.assertIn("database_file_state", report)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        checked = datetime.fromisoformat(report["checked_at_utc"].replace("Z", "+00:00"))
        current = load_dashboard(self.path, as_of=checked + timedelta(seconds=1),
                                 integrity_report=report_path)
        self.assertEqual(current["integrity"]["status"], "verified_recently")
        expired = load_dashboard(self.path, as_of=checked + timedelta(minutes=16),
                                 integrity_report=report_path)
        self.assertEqual(expired["integrity"]["status"], "expired")

        main_mtime = self.path.stat().st_mtime_ns
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES ('fixture', ?, ?, ?)",
            (utc_string(checked), str(payload), digest),
        )
        connection.commit()
        self.assertEqual(self.path.stat().st_mtime_ns, main_mtime)
        changed = load_dashboard(self.path, as_of=checked + timedelta(seconds=2),
                                 integrity_report=report_path)
        self.assertEqual(changed["integrity"]["status"], "possibly_stale")

        payload.write_text("tampered", encoding="utf-8")
        failed = verify_evidence(self.path)
        self.assertEqual(failed["status"], "issues")
        report_path.write_text(json.dumps(failed), encoding="utf-8")
        failed_time = datetime.fromisoformat(failed["checked_at_utc"].replace("Z", "+00:00"))
        shown = load_dashboard(self.path, as_of=failed_time + timedelta(seconds=1),
                               integrity_report=report_path)
        self.assertEqual(shown["integrity"]["status"], "issues")
        self.assertGreater(shown["integrity"]["issue_count"], 0)

    def test_saved_integrity_rejects_inconsistent_or_invalid_provenance(self) -> None:
        connection = self._connect()
        payload = Path(self.temp.name) / "saved-odds.json"
        payload.write_bytes(b"fixture")
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
            "VALUES ('fixture', ?, ?, ?)",
            (utc_string(self.now), str(payload), digest),
        )
        connection.commit()
        report = verify_evidence(self.path)
        self.assertEqual(report["status"], "verified")
        report_path = Path(self.temp.name) / "integrity.json"
        checked = datetime.fromisoformat(report["checked_at_utc"].replace("Z", "+00:00"))

        def status_for(saved: dict) -> str:
            report_path.write_text(json.dumps(saved), encoding="utf-8")
            return load_dashboard(self.path, as_of=checked + timedelta(seconds=1),
                                  integrity_report=report_path)["integrity"]["status"]

        self.assertEqual(status_for({**report, "database_path": str(report_path)}), "wrong_database")
        self.assertEqual(status_for({**report, "database_path": "invalid\x00path"}), "invalid_report")
        self.assertEqual(status_for({**report, "project_root": "relative/root"}), "invalid_report")
        self.assertEqual(status_for({**report, "status": "issues"}), "invalid_report")
        self.assertEqual(status_for({**report, "database_file_state": None}), "invalid_report")
        self.assertEqual(status_for({**report, "issue_count": 1}), "invalid_report")
        report_path.write_text("{bad json", encoding="utf-8")
        self.assertEqual(load_dashboard(self.path, as_of=checked + timedelta(seconds=1),
                                        integrity_report=report_path)["integrity"]["status"],
                         "invalid_report")

    def test_logistic_dated_feature_coverage_is_visible_and_malformed_json_is_not_trusted(self) -> None:
        connection = self._connect()
        self._card(connection)
        generated = utc_string(self.now - timedelta(seconds=10))
        prediction_id = db.save_prediction(
            connection, "bout", "logistic-prior-v2:fixture", generated, generated, 0.57,
        )
        coverage = {
            "fighter_a_age": True, "fighter_b_age": False,
            "fighter_a_reach": True, "fighter_b_reach": False,
            "fighter_a_adjusted_stats": False, "fighter_b_adjusted_stats": True,
        }
        connection.execute(
            "UPDATE predictions SET feature_coverage_json = ? WHERE prediction_id = ?",
            (json.dumps(coverage), prediction_id),
        )
        connection.commit()
        saved = load_dashboard(self.path, as_of=self.now)["upcoming_events"][0]["bouts"][0]["prediction"]
        self.assertEqual(saved["feature_coverage_status"], "recorded")
        self.assertEqual(saved["feature_coverage"], coverage)

        connection.execute(
            "UPDATE predictions SET feature_coverage_json = 'not-json' WHERE prediction_id = ?",
            (prediction_id,),
        )
        connection.commit()
        invalid = load_dashboard(self.path, as_of=self.now)["upcoming_events"][0]["bouts"][0]["prediction"]
        self.assertEqual(invalid["feature_coverage_status"], "invalid")
        self.assertIsNone(invalid["feature_coverage"])

    def test_saved_gate_roster_is_flagged_after_matchup_changes(self) -> None:
        connection = self._connect()
        prediction_id, quote_id = self._card(connection)
        self._accepted_gate(connection, prediction_id, quote_id)
        initial = load_dashboard(self.path, as_of=self.now)["upcoming_events"][0]["bouts"][0]
        self.assertEqual(initial["gate_check"]["display_status"], "accepted_recently")
        db.upsert_fighter(connection, "c", "Replacement Example", "fixture", "c")
        connection.execute("UPDATE bouts SET fighter_b_id = 'c' WHERE bout_id = 'bout'")
        connection.commit()
        changed = load_dashboard(self.path, as_of=self.now)["upcoming_events"][0]["bouts"][0]
        self.assertEqual(changed["gate_check"]["display_status"], "roster_superseded")
        self.assertEqual(changed["availability"], "gate_roster_superseded")

    def test_operator_jobs_show_failed_and_started_states_without_provider_response(self) -> None:
        connection = self._connect()
        started = utc_string(self.now - timedelta(minutes=3))
        finished = utc_string(self.now - timedelta(minutes=2))
        connection.execute(
            "INSERT INTO operator_job_runs(command, event_id, started_at_utc, status) "
            "VALUES ('fetch-odds', 'event', ?, 'started')",
            (started,),
        )
        connection.execute(
            "INSERT INTO operator_job_runs(command, event_id, started_at_utc, "
            "finished_at_utc, status, error_category) "
            "VALUES ('fetch-card', 'event', ?, ?, 'failed', 'provider_unavailable')",
            (started, finished),
        )
        connection.commit()
        jobs = load_dashboard(self.path, as_of=self.now)["job_runs"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual({job["status"] for job in jobs}, {"started", "failed"})
        failed = next(job for job in jobs if job["status"] == "failed")
        self.assertEqual(failed["error_category"], "provider_unavailable")
        self.assertNotIn("provider_response", failed)


class ResearchHoldoutDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "research.sqlite"
        self.report_path = Path(self.temp.name) / "research.json"
        self.raw_paths: list[Path] = []
        with db.connect(self.path) as connection:
            db.init_db(connection)
            for index, event_date in enumerate(("2025-01-11", "2025-01-18", "2025-01-25"), 1):
                a, b = f"a{index}", f"b{index}"
                for fighter in (a, b):
                    db.upsert_fighter(connection, fighter, fighter, "wikipedia_research", fighter)
                db.upsert_event(
                    connection, f"e{index}", f"Research {index}", event_date,
                    "completed", source="wikipedia_research", source_event_id=str(100 + index),
                )
                db.upsert_bout(
                    connection, f"bout{index}", f"e{index}", a, b, "completed",
                    source="wikipedia_research", source_bout_id=f"{100 + index}:1",
                )
                db.upsert_result(connection, f"bout{index}", "win", a, f"{event_date}T20:00:00Z")
                raw = Path(self.temp.name) / f"source-{index}.json"
                raw.write_text(json.dumps({"event": index}), encoding="utf-8")
                self.raw_paths.append(raw)
                run_id = connection.execute(
                    "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                    "VALUES ('wikipedia_research', ?, ?, ?)",
                    (f"{event_date}T21:00:00Z", str(raw), hashlib.sha256(raw.read_bytes()).hexdigest()),
                ).lastrowid
                connection.execute(
                    """INSERT INTO wikipedia_source_receipts(
                           run_id, event_page_id, event_title, page_url, revision_id,
                           revision_timestamp_utc, license_title, license_url,
                           imported_bouts, skipped_unresolved_bouts)
                       VALUES (?, ?, ?, ?, 1, ?, 'CC BY-SA 4.0',
                               'https://creativecommons.org/licenses/by-sa/4.0/', 1, ?)""",
                    (run_id, 100 + index, f"Research {index}",
                     f"https://en.wikipedia.org/wiki/Research_{index}",
                     f"{event_date}T21:00:00Z", int(index == 3)),
                )
            _, source_summary = research_feature_rows(connection)
            connection.commit()
        self.report = {
            "schema_version": 1,
            "evaluation_version": RESEARCH_EVALUATION_VERSION,
            "status": "research_only", "research_only": True,
            "promotion_eligible": False, "source": "wikipedia_research",
            "source_summary": source_summary,
            "split": {
                name: {"binary_bouts": 1, "events": 1, "event_dates": 1,
                       "first_date": event_date, "last_date": event_date}
                for name, event_date in (
                    ("train", "2025-01-11"), ("validation", "2025-01-18"),
                    ("test", "2025-01-25"),
                )
            },
            "calibration": {
                "method": "symmetric_temperature_on_validation_only",
                "validation_binary_bouts": 1, "applied": False, "scale": 1.0,
            },
            "test": {
                name: {
                    "metrics": {"bouts": 1, "accuracy": 1.0,
                                "brier_score": 0.25, "log_loss": 0.693147},
                    "calibration_bins": [
                        {"lower": i / 5, "upper": (i + 1) / 5,
                         "bouts": int(i == 2),
                         "mean_probability": 0.5 if i == 2 else None,
                         "observed_win_rate": 1.0 if i == 2 else None}
                        for i in range(5)
                    ],
                }
                for name in ("elo", "logistic_calibrated")
            },
            "test_uncertainty": {
                "method": "paired_event_date_block_bootstrap",
                "test_event_date_blocks": 1,
                "logistic_minus_elo_brier_score_95pct_interval": [-0.1, 0.1],
                "logistic_minus_elo_log_loss_95pct_interval": [-0.2, 0.2],
            },
            "bookmaker": {
                "status": "not_evaluated_without_historical_point_in_time_prices",
                "roi": None,
            },
            "identity_coverage": {
                "held_source_bout_rows": 1,
                "by_split_dates": {
                    name: {"completed_events": 1, "accepted_bouts_all_outcomes": 1,
                           "held_source_bout_rows": held,
                           "accepted_share_of_accepted_plus_held": 1 / (1 + held)}
                    for name, held in (("train", 0), ("validation", 0), ("test", 1))
                },
            },
        }
        self._save_report()

    def _save_report(self) -> None:
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")

    def _load(self) -> dict:
        return load_dashboard(
            self.path, as_of=datetime(2025, 2, 1, tzinfo=timezone.utc),
            research_report=self.report_path,
        )["research_evaluation"]

    def test_research_holdout_is_distinct_and_bound_to_checked_results(self) -> None:
        before = self.path.stat().st_mtime_ns
        result = self._load()
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["research_only"])
        self.assertFalse(result["promotion_eligible"])
        self.assertNotIn("bookmaker", result)
        with db.connect(self.path) as connection:
            connection.execute("UPDATE results SET winner_fighter_id = 'b3' WHERE bout_id = 'bout3'")
            connection.commit()
        self.assertEqual(self._load()["status"], "stale_report")

    def test_research_holdout_suppresses_shifted_identity_holds_and_missing_payload(self) -> None:
        self.report["identity_coverage"]["by_split_dates"]["train"].update(
            held_source_bout_rows=1, accepted_share_of_accepted_plus_held=0.5,
        )
        self.report["identity_coverage"]["by_split_dates"]["test"].update(
            held_source_bout_rows=0, accepted_share_of_accepted_plus_held=1.0,
        )
        self._save_report()
        self.assertEqual(self._load()["status"], "stale_report")
        self.report["identity_coverage"]["by_split_dates"]["train"].update(
            held_source_bout_rows=0, accepted_share_of_accepted_plus_held=1.0,
        )
        self.report["identity_coverage"]["by_split_dates"]["test"].update(
            held_source_bout_rows=1, accepted_share_of_accepted_plus_held=0.5,
        )
        self._save_report()
        self.raw_paths[0].unlink()
        self.assertEqual(self._load()["status"], "unverified_source")

    def test_research_holdout_rejects_operating_label(self) -> None:
        self.report["promotion_eligible"] = True
        self._save_report()
        self.assertEqual(self._load()["status"], "invalid_report")

    def test_research_holdout_rejects_old_schema_and_empty_period(self) -> None:
        self.report["evaluation_version"] = "wikipedia-result-holdout-v1"
        self._save_report()
        self.assertEqual(self._load()["status"], "invalid_report")
        self.report["evaluation_version"] = RESEARCH_EVALUATION_VERSION
        self.report["identity_coverage"]["by_split_dates"]["train"].update(
            accepted_bouts_all_outcomes=0, held_source_bout_rows=0,
            accepted_share_of_accepted_plus_held=0.0,
        )
        self._save_report()
        self.assertEqual(self._load()["status"], "invalid_report")


if __name__ == "__main__":
    unittest.main()
