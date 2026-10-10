"""Complete-card reconstruction preserves raw receipts, identities and original DB."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.research_complete import complete_history, normalize_name, saved_title_map
from ufc_odds_model.research_evaluation import research_feature_rows


class CompleteHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "original.sqlite"
        self.output = self.root / "complete.sqlite"
        self.report = self.root / "report.json"
        self.review = self.root / "review.json"
        self.raw = self.root / "page.json"
        page = {"id": 10, "title": "Example", "latest": {"id": 20, "timestamp": "2024-01-02T00:00:00Z"},
                "license": {"title": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/"},
                "source": "{{Infobox MMA event|name=Example|date={{start date|2024|1|1}}}}\n"
                          "==Results==\n{{MMAevent bout|Lightweight|[[Alias]]|def.|Same Name|TKO (punches)|1|1:00}}\n"}
        self.raw.write_text(json.dumps(page))
        self.review.write_text(json.dumps({"schema_version": 1, "source": "wikipedia_research",
            "review_required": True, "summary": {"held_bouts": 1}, "fighters": [{"occurrences": [
                {"event_id": "wikipedia_research:10", "bout_position": 1}]}]}))
        action = self.root / "action.json"
        action.write_text(json.dumps({"response": {"query": {
            "normalized": [{"from": "Alias_name", "to": "Alias"}],
            "redirects": [{"from": "Alias", "to": "Real Title"}],
            "pages": [{"pageid": 42, "title": "Real Title", "ns": 0}]}}}))
        with db.connect(self.source) as c:
            db.init_db(c)
            db.upsert_fighter(c, "wikipedia:42", "Winner", "wikipedia", "42")
            db.upsert_fighter(c, "wikipedia:99", "Same Name", "wikipedia", "99")
            db.upsert_event(c, "wikipedia_research:10", "Example", "2024-01-01", "completed",
                            source="wikipedia_research", source_event_id="10")
            run = c.execute("INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES (?,?,?,?)",
                            ("wikipedia_research", "2024-01-02T00:00:00Z", str(self.raw),
                             hashlib.sha256(self.raw.read_bytes()).hexdigest())).lastrowid
            c.execute("""INSERT INTO wikipedia_source_receipts(run_id,event_page_id,event_title,page_url,revision_id,
                       revision_timestamp_utc,license_title,license_url,imported_bouts,skipped_unresolved_bouts)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""", (run,10,"Example","https://en.wikipedia.org/wiki/Example",20,
                       "2024-01-02T00:00:00Z","CC BY-SA 4.0","https://creativecommons.org/licenses/by-sa/4.0/",0,1))
            c.execute("INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES (?,?,?,?)",
                      ("wikipedia_action_api", "2024-01-02T00:00:00Z", str(action),
                       hashlib.sha256(action.read_bytes()).hexdigest()))

    def tearDown(self):
        self.temp.cleanup()

    def complete(self):
        return complete_history(self.source, self.output, review_path=self.review,
                                report_path=self.report, raw_dir=self.root / "raw")

    def test_restore_method_outcome_and_do_not_merge_provisional_collision(self):
        before = self.source.read_bytes()
        report = self.complete()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(report["restored_bouts"], 1)
        self.assertEqual(report["parser_fallbacks"], 0)
        self.assertEqual(report["identity_flags"][0]["reason"], "real_canonical_name_collision")
        with db.connect(self.output) as c:
            result = c.execute("SELECT * FROM results").fetchone()
            self.assertEqual(result["winner_fighter_id"], "wikipedia:42")
            self.assertEqual(result["method"], "TKO (punches)")
            bout = c.execute("SELECT * FROM bouts").fetchone()
            self.assertEqual(bout["fighter_b_id"], "provisional:wikipedia_name:same name")
            self.assertEqual(bout["source"], "wikipedia_research_restored")
            self.assertEqual(len(research_feature_rows(c)[0]), 0)
            self.assertEqual(len(research_feature_rows(c, include_restored=True)[0]), 1)
        with self.assertRaisesRegex(ValueError, "new, separate"):
            self.complete()

    def test_changed_raw_payload_cannot_use_a_weaker_fallback(self):
        self.raw.write_text('{"changed": true}')
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.complete()
        self.assertFalse(self.output.exists())

    def test_nfkc_names_and_redirect_chains(self):
        self.assertEqual(normalize_name(" ＳＡＭＥ  Name\t"), "same name")
        with db.connect(self.source) as c:
            titles, pages = saved_title_map(c)
        self.assertEqual(titles["Alias"], 42)
        self.assertEqual(titles["Alias name"], 42)
        self.assertEqual(pages[42], "Real Title")
