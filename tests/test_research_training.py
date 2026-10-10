"""Opt-in research forecasts exclude future outcomes and preserve live gates."""
import hashlib
import json
import math
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests import test_prefight_cli as prefight_fixture
from ufc_odds_model import db
from ufc_odds_model.cli import make_parser
from ufc_odds_model.logistic import V3_FEATURE_NAMES
from ufc_odds_model.model_lab import file_sha256
from ufc_odds_model.pipeline import score_event
from ufc_odds_model.research_training import prepare_research_logistic

AS_OF = datetime(2019,1,13,12,tzinfo=timezone.utc)


def add_research_card(connection, day):
    event_id = f'history-{day}'
    event_date = f'2019-01-{day:02d}'
    db.upsert_event(connection,event_id,event_id,event_date,'completed',source='wikipedia_research')
    for number in range(10):
        bout_id = f'{event_id}:{number}'
        db.upsert_bout(connection,bout_id,event_id,'fa','fb','completed',
                       source='wikipedia_research_restored' if number==0 else 'wikipedia_research')
        db.upsert_result(connection,bout_id,'win','fa' if (day+number)%3 else 'fb', event_date+'T22:00:00Z')
    connection.commit()


def research_database(path):
    with db.connect(path) as connection:
        db.init_db(connection)
        for fighter in ('fa','fb'):
            db.upsert_fighter(connection,fighter,fighter)
        for day in range(1,13):
            add_research_card(connection,day)
        raw_path = path.parent/'profile.json'
        raw_path.write_bytes(b'{"fixture":"two known birth dates"}')
        run = connection.execute("INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES ('wikidata_profiles',?,?,?)",
                                 ('2019-01-01T00:00:00Z',str(raw_path),hashlib.sha256(raw_path.read_bytes()).hexdigest())).lastrowid
        for fighter,birth in [('fa','1985-01-01'),('fb','1990-01-01')]:
            connection.execute("INSERT INTO fighter_static_attributes(fighter_id,attribute,value,source,license,ingestion_run_id) VALUES (?, 'birth_date', ?, 'wikidata', ?, ?)",
                               (fighter,birth,'https://creativecommons.org/publicdomain/zero/1.0/',run))
        connection.commit()


class ResearchTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.history=self.root/'research.sqlite'; research_database(self.history)
        self.operating=sqlite3.connect(':memory:'); self.operating.row_factory=sqlite3.Row
        db.init_db(self.operating)
        for fighter in ('fa','fb'):
            db.upsert_fighter(self.operating,fighter,fighter)
        db.upsert_event(self.operating,'target','target','2019-01-20','scheduled',start_time_utc='2019-01-20T20:00:00Z')
        db.upsert_bout(self.operating,'target-bout','target','fa','fb','scheduled')
        self.operating.commit()

    def tearDown(self):
        self.operating.close(); self.temp.cleanup()

    def test_live_forecast_without_operating_history_is_labelled_and_read_only(self):
        before=file_sha256(self.history)
        with self.assertRaisesRegex(ValueError,'at least 100 earlier binary bouts'):
            score_event(self.operating,'target',AS_OF,self.root/'default',model_kind='logistic')
        _,rows=score_event(self.operating,'target',AS_OF,self.root/'reports',model_kind='logistic',
                           model_dir=self.root/'models',training_history=self.history)
        self.assertEqual(before,file_sha256(self.history))
        self.assertEqual(self.operating.execute('SELECT COUNT(*) FROM results').fetchone()[0],0)
        row=rows[0]; metadata=json.loads(row['prediction_metadata_json'])
        self.assertTrue(row['model_version'].startswith('logistic-prior-v3:research_retrospective:'))
        self.assertEqual(row['training_history'],'research_retrospective')
        self.assertEqual(metadata['calibration_last_date'],'2019-01-12')
        self.assertFalse(metadata['promotion_eligible'])
        self.assertEqual(metadata['elo_k'],64)
        self.assertEqual(metadata['feature_names'],list(V3_FEATURE_NAMES))
        self.assertEqual(len(metadata['top_feature_contributions']),3)
        p=row['model_probability_fighter_a']
        self.assertAlmostEqual(sum(r['logit_contribution'] for r in metadata['feature_contributions']),math.log(p/(1-p)))
        self.assertTrue(metadata['coverage_by_fighter']['fighter_a']['age'])
        saved=self.operating.execute('SELECT metadata_json FROM predictions').fetchone()[0]
        self.assertEqual(json.loads(saved),metadata)

    def test_future_and_same_day_results_cannot_change_fitted_model_or_target_features(self):
        initial=prepare_research_logistic(self.operating,'target',AS_OF,self.history)
        with db.connect(self.history) as research:
            add_research_card(research,13); add_research_card(research,14)
        later=prepare_research_logistic(self.operating,'target',AS_OF,self.history)
        self.assertEqual(initial.model_version,later.model_version)
        self.assertEqual(initial.predictions,later.predictions)
        self.assertEqual(initial.model.weights,later.model.weights)
        with db.connect(self.history) as research:
            db.upsert_result(research,'history-2:0','win','fb','2019-01-02T22:00:00Z')
            research.commit()
        changed=prepare_research_logistic(self.operating,'target',AS_OF,self.history)
        self.assertNotEqual(initial.model_version,changed.model_version)

    def test_future_fetched_target_profile_is_neutral_and_same_database_is_refused(self):
        with db.connect(self.history) as research:
            # Existing immutable rows cannot be backdated. Add a different,
            # previously unknown target fighter with a future fetch receipt.
            db.upsert_fighter(research,'future-fighter','future-fighter')
            raw=self.root/'future-profile.json'; raw.write_bytes(b'future profile')
            run=research.execute("INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES ('wikidata_profiles',?,?,?)",
                                 ('2019-01-14T00:00:00Z',str(raw),hashlib.sha256(raw.read_bytes()).hexdigest())).lastrowid
            research.execute("INSERT INTO fighter_static_attributes(fighter_id,attribute,value,source,license,ingestion_run_id) VALUES ('future-fighter','birth_date','1980-01-01','wikidata',?,?)",
                             ('https://creativecommons.org/publicdomain/zero/1.0/',run))
            research.commit()
        db.upsert_fighter(self.operating,'future-fighter','future-fighter')
        self.operating.execute("UPDATE bouts SET fighter_a_id='future-fighter' WHERE bout_id='target-bout'")
        self.operating.commit()
        before=prepare_research_logistic(self.operating,'target',AS_OF,self.history)
        self.assertFalse(before.coverage_by_bout['target-bout']['fighter_a_age'])
        after=prepare_research_logistic(self.operating,'target',datetime(2019,1,15,tzinfo=timezone.utc),self.history)
        self.assertTrue(after.coverage_by_bout['target-bout']['fighter_a_age'])
        with self.assertRaisesRegex(ValueError,'requires --model logistic'):
            score_event(self.operating,'target',AS_OF,training_history=self.history)
        with db.connect(self.history) as research:
            db.upsert_event(research,'target','target','2019-01-20','scheduled',start_time_utc='2019-01-20T20:00:00Z')
            db.upsert_bout(research,'target-bout','target','fa','fb','scheduled')
            research.commit()
            with self.assertRaisesRegex(ValueError,'separate read-only research database'):
                prepare_research_logistic(research,'target',AS_OF,self.history)

    def test_cli_exposes_opt_in_on_all_three_commands_and_keeps_default(self):
        parser=make_parser()
        for command in ('score-event','alert-event','paper-trade'):
            extra=['--bankroll-units','100'] if command=='paper-trade' else []
            args=parser.parse_args([command,'event',*extra])
            self.assertIsNone(args.training_history)
            args=parser.parse_args([command,'event',*extra,'--model','logistic','--training-history',str(self.history)])
            self.assertEqual(args.training_history,str(self.history))


class ResearchPrefightGateTests(unittest.TestCase):
    def test_opt_in_does_not_promote_retrospective_training_to_operating_readiness(self):
        helper=prefight_fixture.PrefightCliTests(); helper.setUp()
        self.addCleanup(helper.doCleanups); self.addCleanup(helper.tearDown)
        history=helper.root/'research.sqlite'; research_database(history)
        with db.connect(helper.database) as operating:
            operating.execute("UPDATE events SET source='reviewed_csv'")
            operating.execute("UPDATE fighters SET source='reviewed_csv'")
            operating.commit()
        for command in ('alert-event','paper-trade'):
            extra=['--bankroll-units','100'] if command=='paper-trade' else []
            result=helper._command(command,*extra,'--model','logistic','--model-dir',str(helper.root/'models'),
                                   '--training-history',str(history),'--max-market-disagreement','1')
            row=result['checks'][0]
            self.assertEqual(row['alert_reason'],'model_not_validated')
            self.assertFalse(row['alert_eligible'])
