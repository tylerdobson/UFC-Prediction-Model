"""Frozen probabilities, hash-chain integrity, idempotence and correct pairing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.forecast_ledger import freeze_saved_research,freeze_event,settle_event,summary,read_entries
from ufc_odds_model.research_evaluation import _digest_json


class ForecastLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.ledger=self.root/'ledger.jsonl'; self.saved=self.root/'forecast.json'
        rows=[{"position":1,"fighter_a_id":"a","fighter_b_id":"b","fighter_a_name":"A","fighter_b_name":"B",
               "logistic_calibrated_probability_fighter_a":.7,"saved_two_sided_books":[{
                   "bookmaker":"book","fighter_a_decimal_odds":2.,"fighter_b_decimal_odds":2.,
                   "captured_at_utc":"2024-01-01T00:00:00Z","bookmaker_updated_at_utc":"2024-01-01T00:00:00Z"}]}]
        self.saved.write_text(json.dumps({"event_id":"event","event_name":"event","research_only":True,
            "forecasts":rows,"forecast_rows_sha256":_digest_json(rows),"cutoff_at_utc":"2024-01-01T00:00:00Z",
            "event_start_at_utc":"2024-01-02T00:00:00Z","model":{"logistic_weights_sha256":"a"*64}}))

    def tearDown(self):
        self.temp.cleanup()

    def test_preserve_forecast_and_settle_when_winner_first_orientation_changes(self):
        frozen=freeze_saved_research('event',self.saved,ledger=self.ledger)
        initial=self.ledger.read_bytes()
        capture={"completed_bouts":[{"fighter_a":{"stable_id":"b"},"fighter_b":{"stable_id":"a"},
            "winner_fighter_id":"b","outcome":"win","method":"TKO","status":"matched_unreviewed","hold_reasons":[]}],
            "source":{"raw_sha256":"b"*64,"fetched_at_utc":"2024-01-03T00:00:00Z","revision_id":9},
            'event':{'event_date':'2024-01-02','source_page_id':99}}
        manifest=self.root/'completed.json'; manifest.write_text('{}')
        with patch('scripts.capture_completed_event.verify_completed_capture',return_value=capture):
            settle_event('event',ledger=self.ledger,completed_manifest=manifest)
        self.assertTrue(self.ledger.read_bytes().startswith(initial))
        report=summary(self.ledger)
        self.assertAlmostEqual(report['cumulative']['model']['log_loss'], -__import__('math').log(.3))
        self.assertEqual(report['cumulative']['model']['accuracy'],0.)
        self.assertEqual(report['cumulative']['market']['accuracy'],0.)
        self.assertTrue(report['hash_chain_verified'])
        self.assertEqual(read_entries(self.ledger)[0]['rows'][0]['model_probability'],.7)
        with self.assertRaisesRegex(ValueError,'already'):
            freeze_saved_research('event',self.saved,ledger=self.ledger)
        with self.assertRaisesRegex(ValueError,'No unsettled'):
            settle_event('event',ledger=self.ledger,completed_manifest=manifest)
        self.ledger.write_text(self.ledger.read_text().replace('"model_probability":0.7','"model_probability":0.8'))
        with self.assertRaisesRegex(ValueError,'hash chain'):
            summary(self.ledger)

    def test_saved_probability_hash_and_nonbinary_results_fail_closed(self):
        saved=json.loads(self.saved.read_text()); saved['forecasts'][0]['logistic_calibrated_probability_fighter_a']=.9
        self.saved.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError,'changed'):
            freeze_saved_research('event',self.saved,ledger=self.ledger)
        self.assertFalse(self.ledger.exists())

    def test_live_freeze_uses_latest_prediction_once_and_refuses_future_cutoff(self):
        import sqlite3
        from datetime import timedelta
        from ufc_odds_model import db
        from ufc_odds_model.pipeline import score_event,utc_now,utc_string
        with sqlite3.connect(':memory:') as c:
            c.row_factory=sqlite3.Row; db.init_db(c)
            now=utc_now(); start=now+timedelta(days=1)
            for fighter in ('a','b'):
                db.upsert_fighter(c,fighter,fighter)
            db.upsert_event(c,'event','event',start.date().isoformat(),'scheduled',start_time_utc=utc_string(start))
            db.upsert_bout(c,'bout','event','a','b','scheduled'); c.commit()
            score_event(c,'event',now,self.root/'reports')
            _,latest=score_event(c,'event',now,self.root/'reports')
            freeze=freeze_event(c,'event',ledger=self.ledger)
            self.assertEqual(len(freeze['rows']),1)
            self.assertEqual(freeze['rows'][0]['prediction_id'],latest[0]['prediction_id'])
            self.assertEqual(summary(self.ledger)['pending_forecasts'][0]['bouts'],1)
            c.execute('UPDATE predictions SET feature_cutoff_at_utc=?',(utc_string(start-timedelta(hours=1)),)); c.commit()
            with self.assertRaisesRegex(ValueError,'already have occurred'):
                freeze_event(c,'event',ledger=self.ledger)

    def test_same_fighters_on_another_event_date_cannot_settle_the_forecast(self):
        freeze_saved_research('event',self.saved,ledger=self.ledger)
        capture={'event':{'event_date':'2024-01-03','source_page_id':99},'completed_bouts':[],
                 'source':{'raw_sha256':'b'*64,'fetched_at_utc':'2024-01-04T00:00:00Z','revision_id':9}}
        manifest=self.root/'completed.json'; manifest.write_text('{}')
        with patch('scripts.capture_completed_event.verify_completed_capture',return_value=capture):
            with self.assertRaisesRegex(ValueError,'different event date'):
                settle_event('event',ledger=self.ledger,completed_manifest=manifest)
        self.assertEqual(len(read_entries(self.ledger)),1)
