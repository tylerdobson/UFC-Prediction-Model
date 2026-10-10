"""UFC 332 disagreement shapes can never become default candidates."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta

from ufc_odds_model import db
from ufc_odds_model.alerts import gate_prefight_alerts
from ufc_odds_model.ingest import import_odds_payload
from ufc_odds_model.pipeline import score_event,utc_now,utc_string


class MarketGuardTests(unittest.TestCase):
    def test_figueiredo_and_green_shapes_reject_even_when_nominal_ev_is_large(self):
        now = utc_now()
        for market,odds in ((.17,6.12),(.30,3.3)):
            row = {"decision":"candidate","model_probability_fighter_a":.52,
                   "market_no_vig_probability":market,"model_selection_probability":.52,
                   "decimal_odds":odds,"quote_id":1,"bookmaker":"book","as_of_utc":utc_string(now),
                   "quote_captured_at_utc":utc_string(now),"bookmaker_updated_at_utc":utc_string(now)}
            result = gate_prefight_alerts([row],snapshot_at_utc=now,event_start_time_utc=now+timedelta(days=1),now_utc=now)[0]
            self.assertEqual(result["decision"],"pass_market_disagreement")
            self.assertEqual(result["alert_reason"],"pass_market_disagreement")
            self.assertFalse(result["alert_eligible"])
            self.assertGreater(.52*odds-1,.5)
            with self.assertRaises(ValueError):
                gate_prefight_alerts([row],snapshot_at_utc=now,event_start_time_utc=now+timedelta(days=1),
                                    now_utc=now,max_market_disagreement=float('nan'))

    def test_score_event_uses_intact_two_sided_same_book_receipt(self):
        with tempfile.TemporaryDirectory() as directory, sqlite3.connect(":memory:") as connection:
            connection.row_factory=sqlite3.Row
            db.init_db(connection)
            now=utc_now(); start=now+timedelta(days=1)
            for fighter in ('a','b'):
                db.upsert_fighter(connection,fighter,fighter)
            db.upsert_event(connection,'event','event',start.date().isoformat(),'scheduled',start_time_utc=utc_string(start))
            db.upsert_bout(connection,'bout','event','a','b','scheduled')
            payload=[{"id":"feed","home_team":"a","away_team":"b","commence_time":utc_string(start),
                      "bookmakers":[{"key":"book","markets":[{"key":"h2h","last_update":utc_string(now),
                      "outcomes":[{"name":"a","price":6.},{"name":"b","price":1.2}]}]}]}]
            odds=import_odds_payload(connection,payload,utc_string(now),Path(directory)/'raw')
            _,rows=score_event(connection,'event',utc_now(),Path(directory)/'reports',
                               required_snapshot_at_utc=odds['snapshot_at_utc'])
            self.assertAlmostEqual(rows[0]['market_no_vig_probability'],1/6)
            self.assertEqual(rows[0]['decision'],'pass_market_disagreement')
            _,relaxed=score_event(connection,'event',utc_now(),Path(directory)/'reports',
                                  required_snapshot_at_utc=odds['snapshot_at_utc'],max_market_disagreement=.5)
            self.assertEqual(relaxed[0]['decision'],'candidate')
