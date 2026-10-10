"""Exact slot times, quota refusal and CLV based on retained pre-start pairs."""
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db,odds_api
from ufc_odds_model.ingest import import_odds_payload
from ufc_odds_model.odds_schedule import schedule,capture_slot,closing_line_value
from ufc_odds_model.pipeline import utc_now,utc_string


class OddsScheduleTests(unittest.TestCase):
    def test_quota_floor_prevents_reading_a_price_payload(self):
        response=io.BytesIO(b'[]'); response.headers={'x-requests-remaining':'49'}
        with patch('ufc_odds_model.odds_api.urlopen',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'50-request floor'):
                odds_api.fetch_mma_h2h('fixture-key')
        missing=io.BytesIO(b'[]')
        with patch('ufc_odds_model.odds_api.urlopen',return_value=missing):
            with self.assertRaisesRegex(RuntimeError,'lacks'):
                odds_api.quota_status('fixture-key')

    def test_schedule_has_fixed_offsets_and_below_floor_never_fetches_odds(self):
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory=sqlite3.Row; db.init_db(connection)
            now=utc_now(); start=now+timedelta(hours=24)
            db.upsert_event(connection,'event','event',start.date().isoformat(),'scheduled',start_time_utc=utc_string(start))
            plan=schedule(connection,'event')
            self.assertEqual([r['slot'] for r in plan['slots']],['t7d','t24h','t2h'])
            self.assertEqual(plan['slots'][1]['due_at_utc'],utc_string(now))
            with patch('ufc_odds_model.odds_api.quota_status',return_value={'remaining_requests':49}), \
                 patch('ufc_odds_model.odds_schedule.import_live_odds') as fetch:
                with self.assertRaisesRegex(RuntimeError,'floor'):
                    capture_slot(connection,'event','t24h','fixture-key')
                fetch.assert_not_called()
            with self.assertRaisesRegex(ValueError,'outside'):
                capture_slot(connection,'event','t7d','fixture-key')

    def test_actual_clv_needs_a_later_verified_same_book_two_sided_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, sqlite3.connect(':memory:') as connection:
            connection.row_factory=sqlite3.Row; db.init_db(connection)
            now=utc_now(); start=now+timedelta(days=4)
            for f in ('a','b'): db.upsert_fighter(connection,f,f)
            db.upsert_event(connection,'event','event',start.date().isoformat(),'scheduled',start_time_utc=utc_string(start))
            db.upsert_bout(connection,'bout','event','a','b','scheduled')
            def payload(captured,a,b):
                return [{"id":"feed","home_team":"a","away_team":"b","commence_time":utc_string(start),
                    "bookmakers":[{"key":"book","markets":[{"key":"h2h","last_update":utc_string(captured),
                    "outcomes":[{"name":"a","price":a},{"name":"b","price":b}]}]}]}]
            import_odds_payload(connection,payload(now,2.,2.),utc_string(now),Path(directory)/'raw')
            quote=connection.execute("SELECT quote_id FROM odds_quotes WHERE selection_fighter_id='a'").fetchone()[0]
            prediction=db.save_prediction(connection,'bout','elo',utc_string(now),utc_string(now),.5)
            connection.execute("""INSERT INTO bets(prediction_id,quote_id,selection_fighter_id,placed_at_utc,actual_decimal_odds,stake)
                               VALUES (?,?, 'a',?,2.05,1)""",(prediction,quote,utc_string(now)))
            self.assertEqual(closing_line_value(connection)['observed'],0)
            later=now+timedelta(hours=24)
            with patch('ufc_odds_model.ingest.utc_now',return_value=later):
                import_odds_payload(connection,payload(later,1.8,2.2),utc_string(later),Path(directory)/'raw')
            result=closing_line_value(connection)
            self.assertEqual(result['observed'],1)
            self.assertAlmostEqual(result['rows'][0]['price_clv'],2.05/1.8-1)
            self.assertAlmostEqual(result['rows'][0]['closing_no_vig_probability'],.55)
