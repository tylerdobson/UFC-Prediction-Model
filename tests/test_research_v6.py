"""Rich past-bout formulas, date-disagreement embargo and frozen experiments."""
import copy
import json
import math
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from tests.test_research_stats import archive_fixture
from tests import test_research_training as training_fixture
from ufc_odds_model import research_v6 as v6
from ufc_odds_model.model_lab import VALIDATION_END, file_sha256, read_only
from ufc_odds_model.research_evaluation import _digest_json


def record(day, bout, fighter='fa', opponent='fb', *, landed=60, linked=None, missing=False):
    totals = {'sig_landed':landed,'sig_attempted':100,'kd':1,'td_landed':2,'td_attempted':4,
              'sub_attempts':1,'control_seconds':120}
    other = {'sig_landed':30,'sig_attempted':80,'kd':0,'td_landed':1,'td_attempted':5,
             'sub_attempts':0,'control_seconds':60}
    if missing: totals['control_seconds'] = None
    return {'event_date':day,'bout_id':bout,'fighter_id':fighter,'opponent_id':opponent,
            'research_bout_id':linked,'elapsed_seconds':600,'totals':totals,'opponent_totals':other}


def overlay(records, *, captured='2019-01-12T23:00:00Z', database_sha='a'*64, metadata=None, through=None):
    groups={}
    for row in records: groups.setdefault(row['fighter_id'],[]).append(row)
    groups={fighter:tuple(sorted(rows,key=lambda r:(r['event_date'],r['bout_id']))) for fighter,rows in groups.items()}
    return v6.RichStatsOverlay(groups,{fighter:fighter for fighter in groups},captured,database_sha,
        _digest_json(groups),{'store_metadata':metadata or {},'captured_at_utc':captured},through)


class RichFeatureTests(unittest.TestCase):
    def test_rates_have_correct_exposure_shrinkage_and_missing_is_not_zero(self):
        view=v6.RichHistory(overlay([record('2019-01-01','old',missing=True)]),v6.CANDIDATES[1]).view('fa','fb','2019-01-10')
        metrics=view['metrics']
        self.assertAlmostEqual(metrics['sig_landed_per_min']['value'],(60+90)/40)
        self.assertAlmostEqual(metrics['sig_absorbed_per_min']['value'],(30+90)/40)
        self.assertAlmostEqual(metrics['sig_accuracy']['value'],(60+25)/150)
        self.assertAlmostEqual(metrics['sig_defense']['value'],1-(30+25)/130)
        self.assertAlmostEqual(metrics['knockdowns_per_15']['value'],(1+.2)/40*15)
        self.assertAlmostEqual(metrics['takedown_accuracy']['value'],(2+4)/14)
        self.assertAlmostEqual(metrics['takedown_defense']['value'],1-(1+4)/15)
        self.assertEqual(metrics['control_share'],{'value':.25,'bouts':0})
        self.assertEqual(view['rich_stat_bouts'],1)
        self.assertEqual(view['rich_stat_minutes'],10)

    def test_current_future_and_two_preceding_dates_exclude_all_opponents(self):
        old=record('2019-01-01','old')
        adverse=[record('2019-01-09','own-target',landed=9999,linked='target'),
                 record('2019-01-09','other-tournament-bout',opponent='fc',landed=9999),
                 record('2019-01-08','two-days',opponent='fd',landed=9999),
                 record('2019-01-10','same-day',landed=9999),record('2019-01-11','future',landed=9999),
                 record('2018-12-01','very-misdated-explicit-target',landed=9999,linked='target')]
        plain=v6.RichHistory(overlay([old]),v6.CANDIDATES[1]).view('fa','fb','2019-01-10','target')
        guarded=v6.RichHistory(overlay([old]+adverse),v6.CANDIDATES[1]).view('fa','fb','2019-01-10','target')
        self.assertEqual(guarded['metrics'],plain['metrics'])
        self.assertEqual(guarded['near_target_records_excluded'],4)
        self.assertEqual(guarded['rich_stat_bouts'],1)

    def test_decision_cutoff_holds_results_between_decision_and_scheduled_event(self):
        source=overlay([record('2019-01-01','old'),record('2019-01-14','after-decision',landed=9999)])
        history=v6.RichHistory(source,v6.CANDIDATES[1])
        _,left,_=history.difference('fa','fb','2019-01-20',history_before_date='2019-01-13')
        self.assertEqual(left['rich_stat_bouts'],1)
        self.assertAlmostEqual(left['metrics']['sig_landed_per_min']['value'],3.75)

    def test_recent_window_includes_whole_boundary_date_and_reports_selected_count(self):
        records=[record(f'2018-01-{day:02d}',str(day)) for day in (1,2,3,3,4,5,6)]
        records[3]['bout_id']='3b'
        view=v6.RichHistory(overlay(records),v6.CANDIDATES[2]).view('fa','fb','2019-01-10')
        self.assertEqual(view['rich_total_stat_bouts'],7)
        self.assertEqual(view['rich_selected_stat_bouts'],5)
        self.assertEqual(view['rich_stat_bouts'],5)
        records[4]['event_date']='2018-01-03'
        view=v6.RichHistory(overlay(records),v6.CANDIDATES[2]).view('fa','fb','2019-01-10')
        self.assertEqual(view['rich_selected_stat_bouts'],5)
        # A boundary date with three bouts is retained without arbitrary order.
        records=[record(f'2018-01-{day:02d}',str(i)) for i,day in enumerate((1,2,3,3,3,4,5,6))]
        view=v6.RichHistory(overlay(records),v6.CANDIDATES[2]).view('fa','fb','2019-01-10')
        self.assertEqual(view['rich_selected_stat_bouts'],6)

    def test_swap_symmetry_and_no_history_neutrality(self):
        source=overlay([record('2019-01-01','old')])
        left,_,_=v6.RichHistory(source,v6.CANDIDATES[1]).difference('fa','fb','2019-01-10')
        right,_,_=v6.RichHistory(source,v6.CANDIDATES[1]).difference('fb','fa','2019-01-10')
        self.assertEqual(left,tuple(-x for x in right))
        neutral,_,_=v6.RichHistory(overlay([]),v6.CANDIDATES[1]).difference('fa','fb','2019-01-10')
        self.assertEqual(neutral,(0.,)*10)


class FrozenExperimentTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.database,self.manifest=archive_fixture(self.root)
        self.statsdb=self.root/'stats.sqlite';self.statsdb.write_bytes(b'fixture normalized store')
        records=[]
        for year in range(2010,2027):
            for month in (1,3,5,7,9,11):
                for fighter,other,value in [('wikipedia:1','wikipedia:2',50+year%9),('wikipedia:2','wikipedia:1',30+month)]:
                    records.append(record(f'{year}-{month:02d}-01',f'{year}-{month}',fighter,other,landed=value))
        self.records=records

    def loader(self,path,through_date=None):
        records=[r for r in self.records if through_date is None or r['event_date'] <= through_date]
        return overlay(records,database_sha=file_sha256(self.statsdb),captured='2026-10-01T00:00:00Z',through=through_date,
                       metadata={'research_database_sha256':file_sha256(self.database),'source_manifest_sha256':file_sha256(self.manifest)})

    def test_protocol_precedes_fits_selection_uses_only_pretest_and_report_recomputes(self):
        protocol_path,selection_path,report_path=[self.root/name for name in ('protocol.json','selection.json','report.json')]
        original=file_sha256(self.database)
        with patch.object(v6,'load_rich_overlay',side_effect=self.loader),patch.object(v6,'_rolling') as fit:
            protocol=v6.freeze_v6_protocol(self.database,self.manifest,self.statsdb,protocol_path)
            fit.assert_not_called()
        self.assertEqual(protocol['candidate_count'],3)
        self.assertIn('accuracy_not_lower',protocol['acceptance_rule'])
        with patch.object(v6,'load_rich_overlay',side_effect=self.loader) as loader:
            selection=v6.select_v6(self.database,self.manifest,self.statsdb,protocol_path,selection_path)
            self.assertEqual(loader.call_args.args[1],VALIDATION_END)
            report=v6.evaluate_v6(self.database,self.manifest,self.statsdb,selection_path,report_path)
        verified=v6.load_verified_v6_report(report_path,selection_path,self.database,self.manifest,self.statsdb)
        self.assertEqual(verified['research_champion'],'logistic-prior-v5')  # Fixture does not meet 100-bout coverage.
        self.assertEqual(verified['fixed_test'],json.loads(json.dumps(report['fixed_test'])))
        self.assertEqual(file_sha256(self.database),original)
        with self.assertRaises(FileExistsError):v6.freeze_v6_protocol(self.database,self.manifest,self.statsdb,protocol_path)
        with self.assertRaises(FileExistsError):v6.select_v6(self.database,self.manifest,self.statsdb,protocol_path,selection_path)
        with self.assertRaises(FileExistsError):v6.evaluate_v6(self.database,self.manifest,self.statsdb,selection_path,report_path)
        # A content-valid report still must reproduce its saved bout-level data.
        report['fixed_test']['predictions'][0]['candidate_probability_a']=.99
        report=v6._signed({k:v for k,v in report.items() if k!='report_content_sha256'},'report_content_sha256')
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError,'saved metrics'):
            v6.load_verified_v6_report(report_path,selection_path,self.database,self.manifest,self.statsdb)

    def test_missing_selection_never_loads_full_statistics(self):
        with patch.object(v6,'load_rich_overlay') as loader:
            with self.assertRaises(ValueError):v6.evaluate_v6(self.database,self.manifest,self.statsdb,self.root/'missing',self.root/'out')
            loader.assert_not_called()

    def test_future_stats_do_not_change_pretest_feature_rows(self):
        first=self.loader(self.statsdb,VALIDATION_END)
        with read_only(self.database) as connection:
            stats=v6.load_research_stats(connection,self.manifest,through_date=VALIDATION_END)
            before,_,_=v6.rich_feature_rows(connection,stats,first,'balanced_career',VALIDATION_END)
        for row in self.records:
            if row['event_date']>VALIDATION_END:row['totals']['sig_landed']=9999
        second=self.loader(self.statsdb,VALIDATION_END)
        with read_only(self.database) as connection:
            after,_,_=v6.rich_feature_rows(connection,stats,second,'balanced_career',VALIDATION_END)
        self.assertEqual(first.inputs_sha256,second.inputs_sha256)
        self.assertEqual(before,after)


class GenericForecastTests(unittest.TestCase):
    def setUp(self):
        self.fixture=training_fixture.ResearchTrainingTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.stats=self.fixture.statistics_overlay()
        records=[record(f'2019-01-{day:02d}',str(day),fighter,other,landed=value)
                 for day in range(1,13) for fighter,other,value in [('fa','fb',70+day),('fb','fa',20+day)]]
        self.rich=overlay(records)
        self.selection={'protocol_version':v6.PROTOCOL_VERSION,'selected_policy':asdict(v6.POLICY),
                        'stats_db_sha256':self.rich.stats_db_sha256,'database_sha256':file_sha256(self.fixture.history),
                        'selected_candidate_id':'balanced_recent5','manifest_sha256':'b'*64}

    def prepare(self,rich=None,as_of=training_fixture.AS_OF):
        return v6.prepare_v6_research(self.fixture.operating,'target',as_of,self.fixture.history,
                                     self.stats,rich or self.rich,self.selection)

    def test_rich_forecast_dimensions_metadata_and_symmetric_contributions(self):
        fitted=self.prepare();meta=fitted.metadata_by_bout['target-bout']
        self.assertTrue(fitted.model_version.startswith('logistic-prior-v6:'))
        self.assertEqual(len(meta['feature_names']),21)
        self.assertEqual(meta['rich_candidate_id'],'balanced_recent5')
        for side in ('fighter_a','fighter_b'):
            self.assertEqual(meta['coverage_by_fighter'][side]['prior_rich_stat_bouts'],5)
            self.assertEqual(meta['coverage_by_fighter'][side]['rich_total_stat_bouts'],12)
            self.assertEqual(meta['coverage_by_fighter'][side]['prior_fights_with_stats'],120)
            self.assertTrue(meta['coverage_by_fighter'][side]['fight_statistics'])
        p=fitted.predictions['target-bout']
        self.assertAlmostEqual(sum(row['logit_contribution'] for row in meta['feature_contributions']),math.log(p/(1-p)))
        self.fixture.operating.execute("UPDATE bouts SET fighter_a_id='fb',fighter_b_id='fa'")
        reversed_run=self.prepare()
        self.assertAlmostEqual(p,1-reversed_run.predictions['target-bout'])
        self.assertEqual(fitted.model.weights,reversed_run.model.weights)
        self.assertFalse(fitted.artifact['current_career_averages_used'])

    def test_post_decision_stats_are_withheld_and_future_source_or_cutoff_refused(self):
        first=self.prepare()
        records=[r for rows in self.rich.records_by_fighter.values() for r in rows]
        records.append(record('2019-01-14','after-cutoff',landed=9999))
        changed=overlay(records)
        second=self.prepare(changed)
        self.assertEqual(first.predictions,second.predictions)
        self.assertEqual(first.model.weights,second.model.weights)
        with self.assertRaisesRegex(ValueError,'capture must precede'):
            self.prepare(replace(self.rich,captured_at_utc='2019-01-14T00:00:00Z'))
        with patch.object(v6,'utc_now',return_value=training_fixture.AS_OF):
            with self.assertRaisesRegex(ValueError,'cannot be in the future'):
                self.prepare(as_of=training_fixture.AS_OF.replace(day=14))


if __name__=='__main__':unittest.main()
