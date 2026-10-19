"""Opt-in research forecasts exclude future outcomes and preserve live gates."""
import hashlib
import json
import math
import sqlite3
import tempfile
import unittest
from dataclasses import replace
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
from ufc_odds_model.research_upgrade import TrainingPolicy
from ufc_odds_model.research_stats import ResearchStatsOverlay
from ufc_odds_model.features import _Profile

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

    def test_legacy_default_preserves_v3_probability_and_repeatable_version(self):
        run=prepare_research_logistic(self.operating,'target',AS_OF,self.history)
        explicit_default=prepare_research_logistic(self.operating,'target',AS_OF,self.history,model_policy=None)
        # Python 3.12 changed float summation; tiny fitting differences also
        # change the digest. Preserve numeric behavior across supported runtimes
        # and require exact repeatability within the same runtime.
        self.assertRegex(run.model_version,r'^logistic-prior-v3:research_retrospective:[0-9a-f]{16}$')
        self.assertAlmostEqual(run.predictions['target-bout'],.6657645778363136,delta=1e-8)
        self.assertEqual(run.model_version,explicit_default.model_version)
        self.assertEqual(run.predictions,explicit_default.predictions)
        self.assertEqual(run.model.weights,explicit_default.model.weights)
        self.assertEqual(run.calibrator.scale,explicit_default.calibrator.scale)
        self.assertEqual(run.artifact['algorithm'],'logistic-prior-v3')
        self.assertEqual(run.artifact['feature_names'],V3_FEATURE_NAMES)
        self.assertEqual(run.artifact['elo_k'],64)
        self.assertEqual(run.artifact['l2'],.02)
        self.assertEqual(run.artifact['training_start_year'],2010)
        self.assertEqual(run.artifact['training_bouts'],90)
        self.assertEqual(run.artifact['calibration_bouts'],30)
        for key in ('training_policy','lookback_years','training_window_lower_date','selection_manifest_sha256'):
            self.assertNotIn(key,run.artifact)
            self.assertNotIn(key,run.metadata_by_bout['target-bout'])

    def test_explicit_v4_policy_limits_fitting_and_retains_complete_feature_history(self):
        with db.connect(self.history) as research:
            day='2010-12-01'
            db.upsert_event(research,'old','old',day,'completed',source='wikipedia_research')
            for number in range(10):
                bout=f'old:{number}'
                db.upsert_bout(research,bout,'old','fa','fb','completed',source='wikipedia_research')
                db.upsert_result(research,bout,'win','fa',day+'T22:00:00Z')
            research.commit()
        before=file_sha256(self.history)
        policy=TrainingPolicy(.002,8)
        run=prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                      model_policy=policy,selection_manifest_sha256='a'*64)
        legacy=prepare_research_logistic(self.operating,'target',AS_OF,self.history)
        self.assertTrue(run.model_version.startswith('logistic-prior-v4:research_retrospective:'))
        self.assertEqual(run.artifact['training_bouts'],90)
        self.assertEqual(run.artifact['calibration_bouts'],30)
        self.assertEqual(legacy.artifact['training_bouts'],100)
        self.assertNotEqual(run.model.weights,legacy.model.weights)
        self.assertEqual(run.artifact['calibration_rule'],'last_20pct_of_earlier_binary_bouts_on_whole_dates_min30')
        for container in (run.artifact,run.metadata_by_bout['target-bout']):
            self.assertEqual(container['algorithm'],'logistic-prior-v4')
            self.assertEqual(container['l2'],.002)
            self.assertEqual(container['lookback_years'],8)
            self.assertEqual(container['training_window_lower_date'],'2011-01-01')
            self.assertEqual(container['training_start_year'],2011)
            self.assertEqual(container['training_policy'],{'l2':.002,'lookback_years':8,'earliest_year':2010})
            self.assertEqual(container['selection_manifest_sha256'],'a'*64)
            self.assertFalse(container['promotion_eligible'])
        # Old results inform features even though they cannot enter the fit.
        self.assertEqual(run.metadata_by_bout['target-bout']['coverage_by_fighter']['fighter_a']['prior_ufc_bouts'],130)
        self.assertEqual(file_sha256(self.history),before)

    def test_v4_complementary_probabilities_and_contributions_reproduce_calibrated_logit(self):
        policy=TrainingPolicy(.002,8)
        first=prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                        model_policy=policy,selection_manifest_sha256='b'*64)
        self.operating.execute("UPDATE bouts SET fighter_a_id='fb',fighter_b_id='fa' WHERE bout_id='target-bout'")
        self.operating.commit()
        reversed_run=prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                               model_policy=policy,selection_manifest_sha256='b'*64)
        self.assertEqual(first.model.weights,reversed_run.model.weights)
        self.assertAlmostEqual(first.predictions['target-bout'],1-reversed_run.predictions['target-bout'])
        for run in (first,reversed_run):
            probability=run.predictions['target-bout']
            contributions=run.metadata_by_bout['target-bout']['feature_contributions']
            self.assertEqual(len(contributions),len(V3_FEATURE_NAMES))
            self.assertAlmostEqual(sum(row['logit_contribution'] for row in contributions),math.log(probability/(1-probability)))
        original={r['feature']:r['logit_contribution'] for r in first.metadata_by_bout['target-bout']['feature_contributions']}
        flipped={r['feature']:r['logit_contribution'] for r in reversed_run.metadata_by_bout['target-bout']['feature_contributions']}
        for name,value in original.items():
            self.assertAlmostEqual(value,-flipped[name])
        alternate_manifest=prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                                     model_policy=policy,selection_manifest_sha256='c'*64)
        self.assertEqual(alternate_manifest.predictions,reversed_run.predictions)
        self.assertNotEqual(alternate_manifest.model_version,reversed_run.model_version)

    def test_v4_same_day_and_future_results_are_excluded(self):
        policy=TrainingPolicy(.002,8)
        before=prepare_research_logistic(self.operating,'target',AS_OF,self.history,model_policy=policy)
        with db.connect(self.history) as research:
            add_research_card(research,13); add_research_card(research,14)
        after=prepare_research_logistic(self.operating,'target',AS_OF,self.history,model_policy=policy)
        self.assertEqual(before.model_version,after.model_version)
        self.assertEqual(before.predictions,after.predictions)
        self.assertEqual(before.artifact['training_rows_sha256'],after.artifact['training_rows_sha256'])
        self.assertEqual(before.artifact['target_features_sha256'],after.artifact['target_features_sha256'])
        self.assertEqual(after.artifact['calibration_last_date'],'2019-01-12')

    def test_v4_keeps_minimum_samples_and_rejects_invalid_manifest_metadata(self):
        with self.assertRaisesRegex(ValueError,'at least 100 earlier binary bouts'):
            prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                      model_policy=TrainingPolicy(.002,8,2020))
        with self.assertRaisesRegex(ValueError,'explicit model policy'):
            prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                      selection_manifest_sha256='a'*64)
        with self.assertRaisesRegex(ValueError,'SHA256'):
            prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                      model_policy=TrainingPolicy(.002,8),selection_manifest_sha256='invalid')

    def statistics_overlay(self):
        paired = {}
        for day in range(1,13):
            for number in range(10):
                paired[f'history-{day}:{number}','fa']=(40+day,100)
                paired[f'history-{day}:{number}','fb']=(20+number,80)
        return ResearchStatsOverlay(
            {'fa':_Profile(reach_cm=180.),'fb':_Profile(reach_cm=190.)},paired,{}, {},
            {'source_manifest_sha256':'d'*64,'repository_commit':'e'*40,
             'reach_rule':'retrospective_archive_reach'},
            '2019-01-12T23:00:00Z',file_sha256(self.history),'f'*64)

    def test_v5_trains_on_prior_per_fight_stats_and_uses_dated_target_reach(self):
        overlay=self.statistics_overlay()
        # A verified pre-cutoff observation is allowed to refine the target's
        # reach without rewriting retrospective archive training profiles.
        raw=self.root/'dated-reach.json';raw.write_bytes(b'official target reach fixture')
        run_id=self.operating.execute("INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES ('official_athlete',?,?,?)",
                                      ('2019-01-13T10:00:00Z',str(raw),hashlib.sha256(raw.read_bytes()).hexdigest())).lastrowid
        self.operating.execute("INSERT INTO fighter_profile_observations(fighter_id,source,source_evidence_uri,observed_at_utc,source_license_uri,reach_cm,ingestion_run_id) VALUES ('fa','official_athlete','https://example.org/athlete','2019-01-13T10:00:00Z','https://example.org/terms',182.5,?)",(run_id,))
        self.operating.commit()
        fitted=prepare_research_logistic(self.operating,'target',AS_OF,self.history,
                                          model_policy=TrainingPolicy(.002,8),stats_overlay=overlay,
                                          selection_manifest_sha256='a'*64)
        self.assertTrue(fitted.model_version.startswith('logistic-prior-v5:'))
        metadata=fitted.metadata_by_bout['target-bout']
        for side in ('fighter_a','fighter_b'):
            self.assertTrue(metadata['coverage_by_fighter'][side]['reach'])
            self.assertTrue(metadata['coverage_by_fighter'][side]['fight_statistics'])
            self.assertEqual(metadata['coverage_by_fighter'][side]['prior_fights_with_stats'],120)
        self.assertEqual(metadata['coverage_by_fighter']['fighter_a']['reach_cm'],182.5)
        self.assertEqual(metadata['coverage_by_fighter']['fighter_a']['reach_source'],'dated_profile')
        self.assertEqual(metadata['coverage_by_fighter']['fighter_b']['reach_cm'],190.)
        self.assertFalse(metadata['current_career_averages_used'])
        contributions={r['feature']:r for r in metadata['feature_contributions']}
        self.assertNotEqual(contributions['reach_cm_difference_20']['weight'],0)
        self.assertNotEqual(contributions['opponent_adjusted_sig_strike_accuracy_difference']['weight'],0)
        probability=fitted.predictions['target-bout']
        self.assertAlmostEqual(sum(r['logit_contribution'] for r in contributions.values()),math.log(probability/(1-probability)))

    def test_v5_rejects_future_capture_or_wrong_database_and_ignores_current_bout_stats(self):
        overlay=self.statistics_overlay()
        policy=TrainingPolicy(.002,8)
        for changed,message in ((replace(overlay,captured_at_utc='2019-01-14T00:00:00Z'),'before the forecast'),
                                (replace(overlay,database_sha256='0'*64),'match the research history')):
            with self.subTest(message=message),self.assertRaisesRegex(ValueError,message):
                prepare_research_logistic(self.operating,'target',AS_OF,self.history,model_policy=policy,stats_overlay=changed)
        first=prepare_research_logistic(self.operating,'target',AS_OF,self.history,model_policy=policy,stats_overlay=overlay)
        future_stats={**overlay.stats,('target-bout','fa'):(900,1000),('target-bout','fb'):(1,1000)}
        second=prepare_research_logistic(self.operating,'target',AS_OF,self.history,model_policy=policy,
                                         stats_overlay=replace(overlay,stats=future_stats))
        self.assertEqual(first.predictions,second.predictions)
        self.assertEqual(first.model.weights,second.model.weights)
        self.assertEqual(first.artifact['target_features_sha256'],second.artifact['target_features_sha256'])

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
        for policy in (None,TrainingPolicy(.002,8)):
            with self.subTest(policy=policy):
                before=prepare_research_logistic(self.operating,'target',AS_OF,self.history,model_policy=policy)
                self.assertFalse(before.coverage_by_bout['target-bout']['fighter_a_age'])
                after=prepare_research_logistic(self.operating,'target',datetime(2019,1,15,tzinfo=timezone.utc),self.history,
                                                model_policy=policy)
                self.assertTrue(after.coverage_by_bout['target-bout']['fighter_a_age'])
        with self.assertRaisesRegex(ValueError,'requires --model logistic'):
            score_event(self.operating,'target',AS_OF,training_history=self.history)
        with db.connect(self.history) as research:
            db.upsert_event(research,'target','target','2019-01-20','scheduled',start_time_utc='2019-01-20T20:00:00Z')
            db.upsert_bout(research,'target-bout','target','fa','fb','scheduled')
            research.commit()
            with self.assertRaisesRegex(ValueError,'separate read-only research database'):
                prepare_research_logistic(research,'target',AS_OF,self.history)
            with self.assertRaisesRegex(ValueError,'separate read-only research database'):
                prepare_research_logistic(research,'target',AS_OF,self.history,model_policy=TrainingPolicy(.002,8))

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
