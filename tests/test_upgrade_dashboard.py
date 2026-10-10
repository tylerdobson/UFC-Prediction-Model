"""Read-only model evidence, contributions, gaps and forward UI on tiny fixtures."""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_prefight_cli as prefight_fixture
from tests import test_forecast_ledger as ledger_fixture
from tests import test_research_training as training_fixture
from ufc_odds_model import db
from ufc_odds_model.dashboard_data import load_dashboard, load_model_upgrade_report, load_forward_ledger
from ufc_odds_model.forecast_ledger import freeze_saved_research, settle_event
from ufc_odds_model.model_lab import compare, file_sha256, rolling_comparison, write_new_json
from ufc_odds_model.pipeline import score_event, utc_now


def model_evidence(root):
    database=root/'research.sqlite'; selected=root/'selection.json'; saved=root/'model.json'
    with db.connect(database) as c:
        db.init_db(c)
        for fighter in ('a','b'):
            db.upsert_fighter(c,fighter,fighter)
        for index,day in enumerate(('2020-01-01','2020-02-01','2020-03-01','2021-04-01','2024-01-01','2024-01-02')):
            event,bout=f'e{index}',f'b{index}'
            db.upsert_event(c,event,event,day,'completed',source='wikipedia_research')
            db.upsert_bout(c,bout,event,'a','b','completed',source='wikipedia_research')
            db.upsert_result(c,bout,'win','a' if index%2 else 'b',day+'T23:00:00Z')
        c.commit()
        report=compare(c,feature_set='age-hinge',elo_k=64,start_year=2010,originally_kept={'b4'})
        report['rolling_origin']=rolling_comparison(c,elo_k=64,start_year=2010,originally_kept={'b4'})
    digest=file_sha256(database)
    write_new_json(selected,{'database_sha256':digest,'fixed_test_labels_used':False,'selection_labels_through':'2023-12-02',
                             'elo_k':64,'training_start_year':2010})
    report.update(database_sha256=digest,selection_manifest_sha256=file_sha256(selected))
    write_new_json(saved,report)
    return saved,database,selected


class UpgradeEvidenceTests(unittest.TestCase):
    def test_saved_metrics_need_the_exact_data_and_selection_and_valid_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths=model_evidence(Path(temporary))
            before=[p.read_bytes() for p in paths]
            view=load_model_upgrade_report(*paths)
            self.assertEqual(view['status'],'available')
            self.assertEqual(view['result']['test']['all']['logistic_calibrated']['metrics']['bouts'],2)
            self.assertEqual(before,[p.read_bytes() for p in paths])
            report=json.loads(paths[0].read_text())
            report['test']['all']['logistic_calibrated']['metrics']['log_loss']=float('nan')
            paths[0].write_text(json.dumps(report))
            self.assertEqual(load_model_upgrade_report(*paths)['status'],'unavailable')
            paths[0].write_bytes(before[0])
            paths[2].write_text('{}')
            self.assertEqual(load_model_upgrade_report(*paths)['status'],'unavailable')

    def test_card_shows_verified_gap_and_mathematical_explanation_without_writes(self):
        helper=prefight_fixture.PrefightCliTests(); helper.setUp()
        self.addCleanup(helper.tearDown)
        history=helper.root/'research.sqlite'; training_fixture.research_database(history)
        payload=helper._payload()
        payload[0]['bookmakers'][0]['markets'][0]['outcomes'][0]['price']=6.
        payload[0]['bookmakers'][0]['markets'][0]['outcomes'][1]['price']=1.2
        result=helper._command('alert-event','--model','logistic','--model-dir',str(helper.root/'models'),
                               '--training-history',str(history),payload=payload)
        before=file_sha256(helper.database)
        view=load_dashboard(helper.database)
        self.assertEqual(file_sha256(helper.database),before)
        bout=view['upcoming_events'][0]['bouts'][0]
        self.assertAlmostEqual(bout['market_no_vig_probability'],1/6)
        self.assertTrue(bout['market_disagreement_exceeds_limit'])
        self.assertEqual(bout['availability'],'pass_market_disagreement')
        self.assertEqual(bout['prediction']['explanation_status'],'recorded')
        self.assertEqual(len(bout['prediction']['top_feature_contributions']),3)
        self.assertTrue(bout['prediction']['metadata']['coverage_by_fighter']['fighter_a']['age'])
        with db.connect(helper.database) as c:
            metadata=json.loads(c.execute('SELECT metadata_json FROM predictions').fetchone()[0])
            metadata['feature_contributions'][0]['logit_contribution']+=1
            c.execute('UPDATE predictions SET metadata_json=?',(json.dumps(metadata),)); c.commit()
        bad=load_dashboard(helper.database)['upcoming_events'][0]['bouts'][0]['prediction']
        self.assertEqual(bad['explanation_status'],'invalid')
        self.assertEqual(bad['top_feature_contributions'],[])

    def test_ledger_source_change_is_rejected_even_when_the_chain_is_unchanged(self):
        helper=ledger_fixture.ForecastLedgerTests(); helper.setUp()
        self.addCleanup(helper.tearDown)
        freeze_saved_research('event',helper.saved,ledger=helper.ledger)
        self.assertEqual(load_forward_ledger(helper.ledger)['status'],'available')
        helper.saved.write_text(helper.saved.read_text()+' ')
        self.assertEqual(load_forward_ledger(helper.ledger)['status'],'invalid')

    @unittest.skipUnless(importlib.util.find_spec('streamlit'),'optional dashboard dependency')
    def test_runtime_renders_year_chart_reliability_and_matched_forward_results(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); report,database,selection=model_evidence(root)
            helper=ledger_fixture.ForecastLedgerTests(); helper.setUp()
            self.addCleanup(helper.tearDown)
            freeze_saved_research('event',helper.saved,ledger=helper.ledger)
            with db.connect(root/'results.sqlite') as c:
                db.init_db(c)
                for fighter in ('a','b'):
                    db.upsert_fighter(c,fighter,fighter)
                db.upsert_event(c,'event','event','2024-01-02','completed')
                db.upsert_bout(c,'result','event','a','b','completed')
                db.upsert_result(c,'result','win','b','2024-01-03T00:00:00Z'); c.commit()
                settle_event('event',ledger=helper.ledger,connection=c)
            with patch.dict(os.environ,{'UFC_MODEL_DB':str(root/'missing.sqlite'),
                 'UFC_MODEL_UPGRADE_REPORT':str(report),'UFC_MODEL_UPGRADE_DB':str(database),
                 'UFC_MODEL_UPGRADE_SELECTION':str(selection),'UFC_MODEL_FORECAST_LEDGER':str(helper.ledger)}):
                app=AppTest.from_file(str(Path(__file__).parents[1]/'app.py'),default_timeout=15).run()
            self.assertEqual(list(app.exception),[])
            model='\n'.join(item.value for item in app.tabs[2].markdown)
            forward='\n'.join(item.value for item in app.tabs[5].markdown)
            self.assertIn('Favorite-oriented reliability',model)
            self.assertIn('Rolling-origin evaluation',model)
            self.assertIn('Model · priced subset',forward)
            self.assertIn('Market · priced subset',forward)
            self.assertIn('1.2040',forward)
            self.assertIn('0.6931',forward)
            self.assertTrue(app.get('vega_lite_chart'))
