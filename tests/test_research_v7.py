"""Chronological measured context, missingness, and protocol-before-test guard."""
import copy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests import test_research_training as training_fixture
from tests.test_research_v6 import overlay as rich_overlay, record as rich_record
from tests.test_research_stats import archive_fixture
from ufc_odds_model import research_v7 as v7
from ufc_odds_model.features import FeatureRow
from ufc_odds_model.historical_stats import HistoryStore, build_history
from ufc_odds_model.logistic import LogisticModel, TemperatureCalibrator
from ufc_odds_model.model_lab import VALIDATION_END, read_only
from ufc_odds_model.portfolio_demo import FIXTURE_DIRECTORY
from ufc_odds_model.research_evaluation import _digest_json
from ufc_odds_model.research_upgrade import CalibratedPolicyFit


def record(day, bout, fighter='a', *, method='ko_tko', result='win', strength=True):
    return {'event_date': day, 'bout_id': bout, 'fighter_id': fighter, 'opponent_id': 'other',
            'research_bout_id': None, 'elapsed_seconds': 600,
            'totals': {'sig_attempted': 100, 'clinch_attempted': 10, 'ground_attempted': 20,
                       'body_attempted': 20, 'leg_attempted': 10, 'td_attempted': 4},
            'opponent_totals': {}, 'method_group': method, 'result': result,
            'opponent_strength': {'archive_elo': 1600., 'prior_record': {'wins': 2, 'losses': 1, 'draws': 0, 'no_contests': 0, 'scored_bouts': 3}} if strength else None}


def overlay(records, through=None):
    groups = {}
    for item in records:
        groups.setdefault(item['fighter_id'], []).append(item)
    groups = {fid: tuple(sorted(rows, key=lambda r: (r['event_date'], r['bout_id']))) for fid, rows in groups.items()}
    return v7.ContextOverlay(groups, {'research-a': 'a', 'research-b': 'b'}, '2026-10-01T00:00:00Z',
                             'a' * 64, _digest_json(groups), {}, through)


class ContextFeaturesTests(unittest.TestCase):
    def test_count_exposure_shrinkage_and_medical_administrative_exclusion(self):
        records = [record('2019-01-01', 'one'), record('2019-01-02', 'two', method='doctor_stoppage'),
                   record('2019-01-03', 'three', method='disqualification'),
                   record('2019-01-04', 'four', method='no_contest', result='no_contest')]
        metrics = v7._metrics(records)
        self.assertAlmostEqual(metrics['clinch_attempt_share']['value'], (40 + .1 * 50) / 450)
        self.assertAlmostEqual(metrics['takedown_attempts_per_15']['value'], (16 + 5) / 70 * 15)
        self.assertEqual(metrics['ko_win_share']['bouts'], 1)
        self.assertAlmostEqual(metrics['ko_win_share']['value'], (1 + .15 * 4) / 5)
        for item in records:
            item['method_group'] = 'unknown'
            item['elapsed_seconds'] = None
            item['totals']['clinch_attempted'] = None
        missing = v7._metrics(records)
        self.assertEqual(missing['ko_win_share'], {'value': .15, 'bouts': 0})
        self.assertEqual(missing['clinch_attempt_share'], {'value': .1, 'bouts': 0})
        self.assertEqual(missing['takedown_attempts_per_15'], {'value': 2.5, 'bouts': 0})

    def test_target_future_near_date_and_explicitly_linked_target_are_excluded(self):
        old = record('2019-01-01', 'old')
        linked = record('2018-12-01', 'wrong-date'); linked['research_bout_id'] = 'target'
        adverse = [record(day, str(index)) for index, day in enumerate(('2019-01-08', '2019-01-09', '2019-01-10', '2019-01-11'))]
        plain = v7.ContextHistory(overlay([old]), v7.CANDIDATES[1]).view('a', '2019-01-10', 'target')
        guarded = v7.ContextHistory(overlay([old, linked] + adverse), v7.CANDIDATES[1]).view('a', '2019-01-10', 'target')
        self.assertEqual(guarded, plain)
        self.assertEqual(guarded['source_bout_ids'], ['old'])
        decided = v7.ContextHistory(overlay([old] + adverse), v7.CANDIDATES[1]).view('a', '2019-01-20', history_before_date='2019-01-08')
        self.assertEqual(decided['source_bout_ids'], ['old'])

    def test_last_five_eligible_fights_keep_boundary_date_ties(self):
        rows = [record(f'2018-01-{day:02d}', str(index)) for index, day in enumerate((1, 2, 3, 3, 3, 4, 5, 6))]
        result = v7.ContextHistory(overlay(rows), v7.CANDIDATES[1]).view('research-a', '2019-01-10')
        self.assertEqual(result['eligible_prior_bouts'], 8)
        self.assertEqual(result['selected_prior_bouts'], 6)
        self.assertEqual(result['source_bout_ids'], ['2', '3', '4', '5', '6', '7'])

    def test_new_style_method_opponent_features_reach_the_model_and_swap_symmetrically(self):
        original = record('2019-01-01', 'old')
        first, _, _ = v7.ContextHistory(overlay([original]), v7.CANDIDATES[1]).difference('a', 'b', '2019-01-10')
        altered = copy.deepcopy(original)
        altered['totals']['clinch_attempted'] = 80
        altered['method_group'] = 'submission'
        altered['opponent_strength']['archive_elo'] = 1800.
        second, _, _ = v7.ContextHistory(overlay([altered]), v7.CANDIDATES[1]).difference('a', 'b', '2019-01-10')
        names = v7.CANDIDATES[1].feature_names
        for name in ('clinch_attempt_share_difference', 'ko_win_share_difference', 'opponent_pre_bout_elo_difference'):
            position = names.index(name)
            self.assertNotEqual(first[position], second[position])
            # A deterministic single-weight model confirms the measurements
            # enter the actual model feature vector, without fitting a model.
            weights = tuple(float(i == position) for i in range(len(names)))
            model = LogisticModel(weights)
            self.assertNotEqual(model.predict_probability(first), model.predict_probability(second))
        swapped, _, _ = v7.ContextHistory(overlay([original]), v7.CANDIDATES[1]).difference('b', 'a', '2019-01-10')
        self.assertEqual(first, tuple(-v for v in swapped))
        neutral, _, _ = v7.ContextHistory(overlay([]), v7.CANDIDATES[1]).difference('a', 'b', '2019-01-10')
        self.assertEqual(neutral, (0.,) * len(names))
        self.assertEqual(first[names.index('clinch_attempt_share_observed_difference')], 1.)

    def test_enrichment_preserves_base_and_coverage_with_no_target_sql(self):
        source = overlay([record('2018-01-01', 'old')])
        rows = [FeatureRow('target', 'event', '2019-01-10', 'research-a', 'research-b', (1., 2.), 1, (True,) * 6)]
        enriched, coverage = v7._enrich(rows, source, v7.CANDIDATES[0])
        self.assertEqual(enriched[0].features[:2], rows[0].features)
        self.assertEqual(enriched[0].coverage, rows[0].coverage)
        self.assertEqual(len(enriched[0].features), 12)
        self.assertEqual(coverage['complete_context_bouts'], 0)


class BulkStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'statistics.sqlite'
        build_history(FIXTURE_DIRECTORY / 'fixture_manifest.json', self.path)

    def load(self, through=None):
        with HistoryStore(self.path, verify=False) as store:
            # The demo deliberately has no research identity links. Its
            # canonical records exercise the same bulk facade contract.
            class Facade:
                metadata = store.metadata
                connection = store.connection

                def all_research_fighter_bouts(self, through_date=None):
                    return store._records('WHERE b.event_date<=?' if through_date else '', (through_date,) if through_date else ())
            return v7.load_context_overlay(Facade(), through_date=through)

    def test_bulk_store_keeps_past_strength_and_excludes_held_rows(self):
        before = self.path.read_bytes()
        result = self.load()
        flat = [record for records in result.records_by_fighter.values() for record in records]
        self.assertEqual(len(flat), 10)  # Five usable pairs, one rejected pair.
        self.assertNotIn('ufcstats:fight:ffffffffffffff04', {r['bout_id'] for r in flat})
        first = min(flat, key=lambda r: r['event_date'])
        self.assertEqual(first['opponent_strength']['prior_record']['scored_bouts'], 0)
        self.assertEqual(first['opponent_strength']['archive_elo'], 1500.)
        self.assertEqual(self.path.read_bytes(), before)

    def test_future_label_and_count_mutations_cannot_change_pretest_overlay(self):
        before = self.load('2024-02-01')
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE bouts SET winner_fighter_id=fighter_b_id,raw_json=? WHERE event_date>=?", (json.dumps({'METHOD': 'Submission'}), '2024-03-01'))
            connection.execute("UPDATE fighter_bout_facts SET totals_json='{}' WHERE bout_id IN (SELECT bout_id FROM bouts WHERE event_date>='2024-03-01')")
        after = self.load('2024-02-01')
        self.assertEqual(before.inputs_sha256, after.inputs_sha256)
        self.assertEqual(before.records_by_fighter, after.records_by_fighter)


class FrozenProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database, self.archive = archive_fixture(self.root)
        self.statistics = self.root / 'statistics.sqlite'; self.statistics.write_bytes(b'synthetic')

    def test_protocol_does_not_fit_and_selection_is_only_earlier_years(self):
        protocol, selected = self.root / 'protocol.json', self.root / 'selection.json'
        with patch.object(v7, '_rolling') as rolling, patch.object(v7, '_load_inputs') as loader:
            frozen = v7.freeze_v7_protocol(self.database, self.archive, self.statistics, protocol)
            rolling.assert_not_called(); loader.assert_not_called()
        self.assertEqual(frozen['candidate_count'], 2)
        self.assertFalse(frozen['automatic_model_promotion'])
        names = tuple(v7.V3_FEATURE_NAMES) + v7.BASELINE_CANDIDATE.feature_names
        baseline = [FeatureRow('old', 'event', '2020-01-04', 'a', 'b', (0.,) * len(names), 1, (True,) * 6)]
        source = overlay([record('2019-01-01', 'one'), record('2019-01-01', 'two', 'b')], VALIDATION_END)

        def rolling(rows, names, policy):
            loss = {21: .70, 31: .68, 43: .67}[len(names)]
            return {'pooled': {'metrics': {'log_loss': loss}}, 'folds': [
                {'year': 2020, 'test_summary': {'last_date': '2020-01-04'},
                 'fit': {'training': {'last_date': '2018-01-01'}, 'calibration': {'first_date': '2018-02-01', 'last_date': '2019-12-01'}}}]}, rows, [.5] * len(rows)

        with patch.object(v7, '_load_inputs', return_value=(baseline, names, {}, source)) as loader, patch.object(v7, '_rolling', side_effect=rolling):
            selection = v7.select_v7(self.database, self.archive, self.statistics, protocol, selected)
            self.assertEqual(loader.call_args.args[-1], VALIDATION_END)
        self.assertEqual(selection['selected_candidate_id'], 'style_finish_opponents_recent5')
        self.assertFalse(selection['fixed_test_labels_used'])
        v7.load_v7_selection(selected, self.database, self.archive, self.statistics)
        with self.assertRaises(FileExistsError):
            v7.select_v7(self.database, self.archive, self.statistics, protocol, selected)

    def test_missing_selection_cannot_load_or_evaluate_full_history(self):
        with patch.object(v7, '_load_inputs') as loader:
            with self.assertRaises(ValueError):
                v7.evaluate_v7(self.database, self.archive, self.statistics, self.root / 'missing.json', self.root / 'evaluation.json')
            loader.assert_not_called()

    def test_selected_evaluation_is_bound_and_saved_metrics_are_recomputed(self):
        protocol, selected, report_path = (self.root / name for name in ('protocol.json', 'selection.json', 'evaluation.json'))
        v7.freeze_v7_protocol(self.database, self.archive, self.statistics, protocol)

        def inputs(database, archive, statistics, through=None):
            with read_only(database) as connection:
                stats = v7.load_research_stats(connection, archive, through_date=through)
                rows, source, names = v7.stats_feature_rows(connection, stats, through_date=through)
            rows = [replace(row, features=tuple(row.features) + (0.,) * 10) for row in rows]
            past = [record(f'{year}-01-01', str(year) + fid, fid)
                    for year in range(2010, 2027) for fid in ('wikipedia:1', 'wikipedia:2')
                    if through is None or f'{year}-01-01' <= through]
            context = overlay(past, through)
            return rows, tuple(names) + v7.BASELINE_CANDIDATE.feature_names, source, context

        def rolling(rows, names, policy, *, years=v7.PRETEST_YEARS):
            evaluated = [row for row in rows if int(row.event_date[:4]) in years]
            folds = [{'year': year, 'test_summary': {'last_date': max(r.event_date for r in evaluated if r.event_date.startswith(str(year)))},
                      'fit': {'training': {'last_date': f'{year-2}-01-01'},
                              'calibration': {'first_date': f'{year-1}-01-01', 'last_date': f'{year-1}-12-01'}}}
                     for year in years if any(r.event_date.startswith(str(year)) for r in evaluated)]
            return {'pooled': v7.metric_report(evaluated, [.5] * len(evaluated)), 'folds': folds}, evaluated, [.5] * len(evaluated)

        def constant_fit(training, calibration, names, origin, policy):
            return CalibratedPolicyFit(LogisticModel((0.,) * len(names)), TemperatureCalibrator(1.),
                                       tuple(training), tuple(calibration), policy, origin)

        with patch.object(v7, '_load_inputs', side_effect=inputs), patch.object(v7, '_rolling', side_effect=rolling), patch.object(v7, 'fit_separated_policy', side_effect=constant_fit):
            v7.select_v7(self.database, self.archive, self.statistics, protocol, selected)
            report = v7.evaluate_v7(self.database, self.archive, self.statistics, selected, report_path)
        verified = v7.load_verified_v7_report(report_path, selected, self.database, self.archive, self.statistics)
        self.assertFalse(verified['fresh_holdout'])
        self.assertFalse(verified['automatic_model_promotion'])
        self.assertEqual(verified['fixed_test']['candidate']['metrics']['bouts'], 18)
        report['fixed_test']['predictions'][0]['candidate_probability_a'] = .99
        report = v7._signed({k: value for k, value in report.items() if k != 'report_content_sha256'}, 'report_content_sha256')
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'saved metrics'):
            v7.load_verified_v7_report(report_path, selected)


class GenericForecastTests(unittest.TestCase):
    def setUp(self):
        self.fixture = training_fixture.ResearchTrainingTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.statistics = self.fixture.statistics_overlay()
        rows = [rich_record(f'2019-01-{day:02d}', str(day), fighter, other, landed=value)
                for day in range(1, 13) for fighter, other, value in [('fa', 'fb', 70 + day), ('fb', 'fa', 20 + day)]]
        self.rich = rich_overlay(rows)
        contexts = [record(f'2019-01-{day:02d}', str(day), fighter) for day in range(1, 13) for fighter in ('fa', 'fb')]
        self.context = replace(overlay(contexts), captured_at_utc=self.rich.captured_at_utc)
        self.selection = {'protocol_version': v7.PROTOCOL_VERSION, 'selected_policy': asdict(v7.POLICY),
                          'implementation_sha256': v7._implementation(), 'statistics_sha256': self.rich.stats_db_sha256,
                          'database_sha256': v7.file_sha256(self.fixture.history),
                          'selected_candidate_id': v7.CANDIDATES[1].candidate_id, 'manifest_sha256': 'b' * 64}

    def prepare(self, context=None):
        return v7.prepare_v7_research(self.fixture.operating, 'target', training_fixture.AS_OF,
            self.fixture.history, self.statistics, self.rich, context or self.context, self.selection)

    def test_synthetic_forecast_dimensions_cutoff_contributions_and_complementarity(self):
        run = self.prepare()
        meta = run.metadata_by_bout['target-bout']
        self.assertEqual(len(meta['feature_names']), 43)
        self.assertTrue(run.artifact['research_only'])
        self.assertFalse(run.artifact['operating_eligibility'])
        self.assertEqual(meta['coverage_by_fighter']['fighter_a']['prior_context_selected_stat_bouts'], 5)
        p = run.predictions['target-bout']
        self.assertAlmostEqual(sum(r['logit_contribution'] for r in meta['feature_contributions']), math.log(p / (1 - p)))
        self.fixture.operating.execute("UPDATE bouts SET fighter_a_id='fb',fighter_b_id='fa'")
        reverse = self.prepare()
        self.assertAlmostEqual(reverse.predictions['target-bout'], 1 - p)

    def test_future_context_cannot_change_fitted_features_or_probability(self):
        baseline = self.prepare()
        records = [r for rows in self.context.records_by_fighter.values() for r in rows]
        records.append(record('2019-01-14', 'after-decision', 'fa'))
        later = replace(overlay(records), captured_at_utc=self.context.captured_at_utc)
        changed = self.prepare(later)
        self.assertEqual(baseline.predictions, changed.predictions)
        self.assertEqual(baseline.model.weights, changed.model.weights)
        self.assertEqual(baseline.metadata_by_bout['target-bout']['feature_values'], changed.metadata_by_bout['target-bout']['feature_values'])


if __name__ == '__main__':
    unittest.main()
