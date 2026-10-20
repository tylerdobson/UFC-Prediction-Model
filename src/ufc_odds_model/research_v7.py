"""Opt-in chronological V7 style, finish-method and prior-opponent research.

No frozen release is changed. The unchanged V6 balanced_recent5 feature set is
the baseline. New measurements use eligible canonical earlier fights, not the
latest five display rows, and retain all ties on the fifth fight's date.
Selection is frozen on earlier development years before the single selected
candidate is scored on the explicitly reused historical benchmark.
"""
from __future__ import annotations

import argparse
import copy
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta, timezone
import json
import math
from pathlib import Path

from .features import COVERAGE_NAMES, FeatureRow
from .fight_context import FightContextHistory, _eligible, _result, method_group
from .live_logistic import _split_for_live
from .logistic import V3_FEATURE_NAMES
from .model_lab import TEST_START, VALIDATION_END, file_sha256, fixed_split, metric_report, read_only, write_new_json
from .pipeline import parse_utc, utc_now, utc_string
from .research_evaluation import _digest_json
from .research_stats import POLICY, load_research_stats, stats_feature_rows
from .research_training import PLAIN_LABELS, ResearchTrainingRun, prepare_research_logistic
from .research_upgrade import EVALUATION_YEARS, PRETEST_YEARS, _comparison, _fit_details, _read_document, _rolling, _signed, eligible_training_rows, fit_separated_policy
from .research_v6 import RichHistory, CANDIDATES as V6_CANDIDATES, METRICS as V6_METRICS, _enrich as enrich_v6, load_rich_overlay


MODEL_VERSION = 'logistic-prior-v7'
PROTOCOL_VERSION = 'research-v7-two-context-families-v1'
RECENT_BOUTS = 5
NEAR_TARGET_DAYS = 2
PSEUDO_ATTEMPTS = 50.
PSEUDO_MINUTES = 30.
PSEUDO_FINISH_BOUTS = 4.
PSEUDO_OPPONENTS = 2.
MIN_COMPLETE_BOUTS = 100
MIN_COMPLETE_SHARE = .25
BASELINE_CANDIDATE = V6_CANDIDATES[2]
COMBAT_METHODS = frozenset(('ko_tko', 'submission', 'decision', 'decision_unanimous', 'decision_split', 'decision_majority'))

# Fixed neutral shrinkage and scales are declared before any fit. Missing
# observations use these priors and a separate, antisymmetric observedness flag.
METRICS = (
    ('clinch_attempt_share', 'Clinch share of significant-strike attempts', .10, .25),
    ('ground_attempt_share', 'Ground share of significant-strike attempts', .15, .25),
    ('body_attempt_share', 'Body share of significant-strike attempts', .20, .25),
    ('leg_attempt_share', 'Leg share of significant-strike attempts', .15, .25),
    ('takedown_attempts_per_15', 'Takedown attempts per 15 minutes', 2.5, 5.),
    ('ko_win_share', 'KO/TKO win share of classified combat bouts', .15, .25),
    ('ko_loss_share', 'KO/TKO loss share of classified combat bouts', .15, .25),
    ('submission_win_share', 'Submission win share of classified combat bouts', .10, .25),
    ('submission_loss_share', 'Submission loss share of classified combat bouts', .10, .25),
    ('opponent_pre_bout_elo', 'Earlier opponents pre-bout archive Elo', 1500., 400.),
    ('opponent_pre_bout_win_rate', 'Earlier opponents pre-bout archive win rate', .5, .25),
)


@dataclass(frozen=True)
class ContextCandidate:
    candidate_id: str
    metric_count: int

    @property
    def feature_names(self):
        return tuple(name + suffix for name, _, _, _ in METRICS[:self.metric_count]
                     for suffix in ('_difference', '_observed_difference'))


CANDIDATES = (ContextCandidate('measured_style_recent5', 5), ContextCandidate('style_finish_opponents_recent5', 11))


def _candidate(value):
    for candidate in CANDIDATES:
        if candidate.candidate_id == value:
            return candidate
    raise ValueError('Unknown frozen V7 candidate')


def _field(record, key, default=None):
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


@dataclass(frozen=True)
class ContextOverlay:
    records_by_fighter: dict
    canonical_by_research: dict
    captured_at_utc: str
    statistics_sha256: str
    inputs_sha256: str
    source_summary: dict
    through_date: str | None = None


def load_context_overlay(store, *, through_date=None, statistics_sha256=None):
    """Bulk-load a verified HistoryStore/facade; no per-target SQL is performed.

    The canonical connection retains bouts.raw_json METHOD/DETAILS. A single
    whole-date replay freezes each opponent's record and Elo before that bout.
    """
    metadata = dict(store.metadata)
    snapshot = FightContextHistory(store, cutoff_at_utc=metadata['captured_at_utc'], through_date=through_date)
    canonical = {row['research_fighter_id']: row['fighter_id'] for row in store.connection.execute(
        'SELECT research_fighter_id,fighter_id FROM research_fighter_links').fetchall()}
    rows = {row['bout_id']: row for row in snapshot.rows if _eligible(row)}
    records = defaultdict(list)
    for record in store.all_research_fighter_bouts(through_date):
        bout_id, fid = _field(record, 'bout_id'), _field(record, 'fighter_id')
        row = rows.get(bout_id)
        if row is None:
            continue
        raw = row.get('raw_json')
        raw = json.loads(raw) if isinstance(raw, str) else raw or {}
        method = row.get('source_method', raw.get('METHOD'))
        other = _field(record, 'opponent_id')
        strength = snapshot.snapshots.get(bout_id, {}).get(other)
        item = {key: _field(record, key) for key in ('bout_id', 'event_date', 'fighter_id', 'opponent_id',
                'research_bout_id', 'elapsed_seconds', 'totals', 'opponent_totals', 'source_url')}
        item.update(method_group=method_group(method, row.get('outcome')), result=_result(row, fid),
                    opponent_strength=strength)
        records[fid].append(item)
    records = {fighter: tuple(sorted(items, key=lambda r: (r['event_date'], r['bout_id'])))
               for fighter, items in records.items()}
    inputs = _digest_json({'records': records, 'canonical_by_research': canonical})
    identity = statistics_sha256 or metadata.get('store_content_sha256') or inputs
    summary = {**snapshot.source, 'statistics_sha256': identity, 'inputs_sha256': inputs,
               'through_date': through_date, 'fighter_bout_records': sum(map(len, records.values())),
               'crosswalked_fighters': len(canonical), 'historical_availability': 'retrospective_source_capture',
               'held_rows_used': False, 'current_career_averages_used': False}
    return ContextOverlay(records, canonical, metadata['captured_at_utc'], identity, inputs, summary, through_date)


def _metrics(records):
    result = {}
    shares = ('clinch_attempted', 'ground_attempted', 'body_attempted', 'leg_attempted')
    for (name, _, prior, _), field in zip(METRICS[:4], shares):
        numerator, denominator, count = 0., 0., 0
        for record in records:
            totals = record['totals']
            value, exposure = _number(totals.get(field)), _number(totals.get('sig_attempted'))
            if value is not None and exposure is not None and exposure > 0 and value <= exposure:
                numerator += value; denominator += exposure; count += 1
        result[name] = {'value': (numerator + prior * PSEUDO_ATTEMPTS) / (denominator + PSEUDO_ATTEMPTS), 'bouts': count}
    attempts, minutes, count = 0., 0., 0
    for record in records:
        value, seconds = _number(record['totals'].get('td_attempted')), _number(record['elapsed_seconds'])
        if value is not None and seconds is not None and seconds > 0:
            attempts += value; minutes += seconds / 60.; count += 1
    result['takedown_attempts_per_15'] = {'value': (attempts + 2.5 * PSEUDO_MINUTES / 15.) / (minutes + PSEUDO_MINUTES) * 15., 'bouts': count}
    combat = [record for record in records if record['method_group'] in COMBAT_METHODS and record['result'] in ('win', 'loss')]
    for name, _, prior, _ in METRICS[5:9]:
        group, outcome = name.removesuffix('_share').rsplit('_', 1)
        group = 'ko_tko' if group == 'ko' else group
        count = sum(record['method_group'] == group and record['result'] == outcome for record in combat)
        result[name] = {'value': (count + prior * PSEUDO_FINISH_BOUTS) / (len(combat) + PSEUDO_FINISH_BOUTS), 'bouts': len(combat)}
    strengths = [r['opponent_strength'] for r in records if r.get('opponent_strength') and r['opponent_strength']['prior_record']['scored_bouts'] > 0]
    elo = [s['archive_elo'] for s in strengths]
    rates = [(s['prior_record']['wins'] + .5 * s['prior_record']['draws']) / s['prior_record']['scored_bouts'] for s in strengths]
    for name, values, prior in (('opponent_pre_bout_elo', elo, 1500.), ('opponent_pre_bout_win_rate', rates, .5)):
        result[name] = {'value': (sum(values) + prior * PSEUDO_OPPONENTS) / (len(values) + PSEUDO_OPPONENTS), 'bouts': len(values)}
    return result


class ContextHistory:
    """One chronological timeline, cached fighter views, and zero target SQL."""
    def __init__(self, overlay, candidate):
        self.overlay, self.candidate = overlay, candidate
        self.events = [(r['event_date'], fid, r) for fid, rows in overlay.records_by_fighter.items() for r in rows]
        self.events.sort(key=lambda row: (row[0], row[1], row[2]['bout_id']))
        self.index, self.before = 0, None
        self.history, self.cache = defaultdict(list), {}

    def view(self, fighter, target_date, target_bout_id=None, history_before_date=None):
        target = date.fromisoformat(target_date)
        before = min(target - timedelta(days=NEAR_TARGET_DAYS),
                     date.fromisoformat(history_before_date) if history_before_date else target).isoformat()
        if self.overlay.through_date and before > (date.fromisoformat(self.overlay.through_date) + timedelta(days=1)).isoformat():
            raise ValueError('V7 context overlay does not cover this target')
        if self.before and before < self.before:
            raise ValueError('V7 features must replay chronologically')
        self.before = before
        while self.index < len(self.events) and self.events[self.index][0] < before:
            _, fid, record = self.events[self.index]
            self.history[fid].append(record); self.index += 1
        fid = self.overlay.canonical_by_research.get(fighter, fighter)
        key = (fid, target_date, target_bout_id, before)
        if key not in self.cache:
            valid = [r for r in self.history[fid] if not target_bout_id or r.get('research_bout_id') != target_bout_id]
            if len(valid) > RECENT_BOUTS:
                boundary = valid[-RECENT_BOUTS]['event_date']
                selected = [r for r in valid if r['event_date'] >= boundary]
            else:
                selected = valid
            self.cache[key] = {'metrics': _metrics(selected), 'eligible_prior_bouts': len(valid),
                               'selected_prior_bouts': len(selected), 'source_bout_ids': [r['bout_id'] for r in selected],
                               'history_strictly_before_date': before, 'window': 'eligible_prior_five_with_whole_boundary_date_ties'}
        return self.cache[key]

    def difference(self, a, b, target_date, target_bout_id=None, history_before_date=None):
        left = self.view(a, target_date, target_bout_id, history_before_date)
        right = self.view(b, target_date, target_bout_id, history_before_date)
        values = []
        for name, _, _, scale in METRICS[:self.candidate.metric_count]:
            x, y = left['metrics'][name], right['metrics'][name]
            values.extend(((x['value'] - y['value']) / scale, float(x['bouts'] > 0) - float(y['bouts'] > 0)))
        if not all(math.isfinite(v) for v in values):
            raise ValueError('Nonfinite V7 context features')
        return tuple(values), left, right


def _enrich(rows, overlay, candidate):
    builder = ContextHistory(overlay, candidate)
    result, available = [], {}
    for row in rows:
        extra, left, right = builder.difference(row.fighter_a_id, row.fighter_b_id, row.event_date, row.bout_id)
        result.append(replace(row, features=tuple(row.features) + extra))
        available[row.bout_id] = all(left['metrics'][name]['bouts'] > 0 and right['metrics'][name]['bouts'] > 0
                                      for name, _, _, _ in METRICS[:candidate.metric_count])
    return result, {'complete_context_bouts': sum(available.values()), 'bouts': len(rows), 'availability_by_bout': available}


def v7_feature_rows(connection, stats_overlay, rich_overlay, context_overlay, candidate_id, through_date=None):
    if context_overlay.through_date and (through_date is None or through_date > context_overlay.through_date):
        raise ValueError('Context overlay does not cover the requested dates')
    base, source, names = stats_feature_rows(connection, stats_overlay, through_date=through_date)
    baseline, _ = enrich_v6(base, rich_overlay, BASELINE_CANDIDATE)
    names = tuple(names) + BASELINE_CANDIDATE.feature_names
    candidate = _candidate(candidate_id)
    rows, coverage = _enrich(baseline, context_overlay, candidate)
    return rows, {**source, 'v7_context': context_overlay.source_summary, 'v7_coverage': coverage,
                  'v7_feature_rows_sha256': _digest_json([[r.bout_id, r.event_date, r.features, r.target] for r in rows])}, names + candidate.feature_names


def _load_inputs(database, archive, statistics, through_date=None):
    from .historical_stats import HistoryStore
    rich = load_rich_overlay(statistics, through_date)
    metadata = rich.source_summary.get('store_metadata', {})
    if (metadata.get('research_database_sha256') != file_sha256(database)
            or metadata.get('source_manifest_sha256') != file_sha256(archive)):
        raise ValueError('V7 canonical crosswalk store does not match its declared research/archive inputs')
    with HistoryStore(statistics) as store:
        context = load_context_overlay(store, through_date=through_date, statistics_sha256=rich.stats_db_sha256)
    with read_only(database) as connection:
        stats = load_research_stats(connection, archive, through_date=through_date)
        base, source, names = stats_feature_rows(connection, stats, through_date=through_date)
    baseline, _ = enrich_v6(base, rich, BASELINE_CANDIDATE)
    return baseline, tuple(names) + BASELINE_CANDIDATE.feature_names, source, context


def _definitions():
    return {'baseline': 'unchanged_V6_balanced_recent5', 'candidates': [asdict(c) for c in CANDIDATES],
            'metrics': [{'metric': k, 'label': label, 'neutral_prior': prior, 'difference_divisor': scale,
                         'observedness_difference': True} for k, label, prior, scale in METRICS],
            'recent_eligible_bouts': RECENT_BOUTS, 'whole_boundary_date_ties': True,
            'near_target_days_excluded': NEAR_TARGET_DAYS, 'held_rows_used': False,
            'combat_methods': sorted(COMBAT_METHODS), 'finish_denominator': 'eligible_binary_bouts_with_classified_combat_method',
            'pseudo_attempts': PSEUDO_ATTEMPTS, 'pseudo_minutes': PSEUDO_MINUTES,
            'pseudo_finish_bouts': PSEUDO_FINISH_BOUTS, 'pseudo_opponents': PSEUDO_OPPONENTS,
            'opponent_elo_k': 64., 'opponent_strength_rule': 'usable_archive_results_strictly_before_prior_fight_whole_date',
            'missingness_rule': 'fixed_neutral_shrinkage_and_separate_antisymmetric_observedness_difference'}


def _identities(database, archive, statistics):
    return {'database_sha256': file_sha256(database), 'source_manifest_sha256': file_sha256(archive),
            'statistics_sha256': file_sha256(statistics)}


def _implementation():
    from . import fight_context, logistic, research_stats, research_training, research_upgrade, research_v6
    return {'v7': file_sha256(__file__), **{module.__name__.rsplit('.', 1)[-1]: file_sha256(module.__file__)
            for module in (fight_context, logistic, research_stats, research_training, research_upgrade, research_v6)}}


def freeze_v7_protocol(database, archive, statistics, output_path):
    protocol = _signed({'schema_version': 1, 'protocol_version': PROTOCOL_VERSION,
        'created_at_utc': utc_string(utc_now()), 'research_only': True, 'promotion_eligible': False,
        'selected_policy': asdict(POLICY), 'definitions': _definitions(), 'implementation_sha256': _implementation(),
        **_identities(database, archive, statistics), 'selection_years': PRETEST_YEARS,
        'selection_labels_through': VALIDATION_END, 'candidate_count': len(CANDIDATES),
        'selection_rule': 'minimum_pretest_rolling_log_loss_then_declared_candidate_order',
        'evaluation_rule': 'only_the_single_frozen_candidate_on_reused_historical_benchmark',
        'fixed_test_is_reused_benchmark': True, 'fresh_holdout': False,
        'minimum_complete_bouts': MIN_COMPLETE_BOUTS, 'minimum_complete_share': MIN_COMPLETE_SHARE,
        'comparison_rule': 'pretest_loss_lower_and_fixed_loss_lower_and_fixed_accuracy_not_lower_and_context_coverage_sufficient',
        'automatic_model_promotion': False}, 'protocol_manifest_sha256')
    write_new_json(output_path, protocol)
    return protocol


def _load_protocol(path, database, archive, statistics):
    value = _read_document(path, 'protocol_manifest_sha256')
    if (value.get('protocol_version') != PROTOCOL_VERSION or value.get('definitions') != _definitions()
            or value.get('selected_policy') != asdict(POLICY) or value.get('implementation_sha256') != _implementation()
            or any(value.get(k) != v for k, v in _identities(database, archive, statistics).items())):
        raise ValueError('V7 frozen protocol or source identity changed')
    return value


def _cohort(rows, availability):
    count = sum(bool(availability.get(row.bout_id)) for row in rows)
    return {'bouts': count, 'share': count / len(rows) if rows else 0.}


def _sufficient(cohort):
    return cohort['bouts'] >= MIN_COMPLETE_BOUTS and cohort['share'] >= MIN_COMPLETE_SHARE


def select_v7(database, archive, statistics, protocol_path, output_path):
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    protocol = _load_protocol(protocol_path, database, archive, statistics)
    baseline, names, source, context = _load_inputs(database, archive, statistics, VALIDATION_END)
    baseline_result, baseline_rows, _ = _rolling(baseline, names, POLICY)
    trials = []
    for candidate in CANDIDATES:
        rows, coverage = _enrich(baseline, context, candidate)
        trial, evaluated, _ = _rolling(rows, names + candidate.feature_names, POLICY)
        if [r.bout_id for r in evaluated] != [r.bout_id for r in baseline_rows]:
            raise ValueError('V7 selection comparators do not contain the same bouts')
        trials.append({'candidate_id': candidate.candidate_id, 'pretest': trial,
                       'complete_context': _cohort(evaluated, coverage['availability_by_bout']),
                       'feature_names': names + candidate.feature_names})
    selected = min(enumerate(trials), key=lambda p: (p[1]['pretest']['pooled']['metrics']['log_loss'], p[0]))[1]
    selection = _signed({'schema_version': 1, 'protocol_version': PROTOCOL_VERSION,
        'created_at_utc': utc_string(utc_now()), 'research_only': True, 'promotion_eligible': False,
        **{key: protocol[key] for key in ('database_sha256', 'source_manifest_sha256', 'statistics_sha256', 'implementation_sha256')},
        'protocol_manifest_sha256': protocol['protocol_manifest_sha256'], 'protocol_file_sha256': file_sha256(protocol_path),
        'definitions': _definitions(), 'selected_policy': asdict(POLICY), 'candidate_count': len(CANDIDATES),
        'selection_labels_through': VALIDATION_END, 'fixed_test_labels_used': False,
        'selected_candidate_id': selected['candidate_id'], 'feature_names': selected['feature_names'],
        'baseline_feature_names': names, 'baseline': baseline_result, 'trials': trials,
        'pretest_improved': selected['pretest']['pooled']['metrics']['log_loss'] < baseline_result['pooled']['metrics']['log_loss'],
        'pretest_coverage_sufficient': _sufficient(selected['complete_context']),
        'pretest_context_inputs_sha256': context.inputs_sha256, 'source_summary': source,
        'context_source_summary': context.source_summary}, 'manifest_sha256')
    _load_protocol(protocol_path, database, archive, statistics)
    write_new_json(output_path, selection)
    return selection


def load_v7_selection(path, database=None, archive=None, statistics=None):
    value = _read_document(path, 'manifest_sha256')
    try:
        if (value['protocol_version'] != PROTOCOL_VERSION or value['definitions'] != _definitions()
                or value['selected_policy'] != asdict(POLICY) or value['candidate_count'] != len(CANDIDATES)
                or value['selection_labels_through'] != VALIDATION_END or value['fixed_test_labels_used'] is not False
                or value['research_only'] is not True or value['promotion_eligible'] is not False
                or value['implementation_sha256'] != _implementation()):
            raise ValueError('V7 selection differs from its frozen protocol')
        trials = value['trials']
        if [trial['candidate_id'] for trial in trials] != [c.candidate_id for c in CANDIDATES]:
            raise ValueError('V7 selection contains a different candidate search')
        losses = [value['baseline']['pooled']['metrics']['log_loss']] + [trial['pretest']['pooled']['metrics']['log_loss'] for trial in trials]
        if any(type(loss) not in (int, float) or not math.isfinite(loss) or loss < 0 for loss in losses):
            raise ValueError('V7 selection contains invalid pretest losses')
        chosen = min(enumerate(trials), key=lambda p: (p[1]['pretest']['pooled']['metrics']['log_loss'], p[0]))[1]
        expected_base_names = tuple(V3_FEATURE_NAMES) + BASELINE_CANDIDATE.feature_names
        if (tuple(value['baseline_feature_names']) != expected_base_names
                or any(tuple(trial['feature_names']) != expected_base_names + candidate.feature_names
                       for trial, candidate in zip(trials, CANDIDATES))):
            raise ValueError('V7 selection feature definitions changed')
        if (chosen['candidate_id'] != value['selected_candidate_id'] or list(chosen['feature_names']) != value['feature_names']
                or value['pretest_improved'] is not (chosen['pretest']['pooled']['metrics']['log_loss'] < value['baseline']['pooled']['metrics']['log_loss'])
                or value['pretest_coverage_sufficient'] is not _sufficient(chosen['complete_context'])):
            raise ValueError('V7 selection decision does not reproduce its pretest trials')
        for trial in [value['baseline']] + [t['pretest'] for t in trials]:
            for fold in trial['folds']:
                if (fold['year'] not in PRETEST_YEARS or fold['test_summary']['last_date'] > VALIDATION_END
                        or fold['fit']['training']['last_date'] >= fold['fit']['calibration']['first_date']
                        or fold['fit']['calibration']['last_date'] >= f"{fold['year']}-01-01"):
                    raise ValueError('V7 selection fold violates chronological boundaries')
        for key, source in (('database_sha256', database), ('source_manifest_sha256', archive), ('statistics_sha256', statistics)):
            if source is not None and value[key] != file_sha256(source):
                raise ValueError('V7 selection source identity mismatch')
    except (KeyError, TypeError) as exc:
        raise ValueError('Incomplete V7 selection') from exc
    return value


def evaluate_v7(database, archive, statistics, selection_path, output_path):
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    selection = load_v7_selection(selection_path, database, archive, statistics)
    *_, pretest = _load_inputs(database, archive, statistics, VALIDATION_END)
    if pretest.inputs_sha256 != selection['pretest_context_inputs_sha256']:
        raise ValueError('V7 pretest context changed after selection')
    baseline, base_names, source, context = _load_inputs(database, archive, statistics)
    candidate = _candidate(selection['selected_candidate_id'])
    rows, coverage = _enrich(baseline, context, candidate)
    names = base_names + candidate.feature_names
    from .research_stats import _context
    with read_only(database) as connection:
        labels = _context(connection, rows)
    bt, bc, btest = fixed_split(baseline)
    train, calibration, test = fixed_split(rows)
    if [r.bout_id for r in btest] != [r.bout_id for r in test]:
        raise ValueError('V7 fixed comparators are not identical')
    fits = (fit_separated_policy(bt, bc, base_names, TEST_START, POLICY),
            fit_separated_policy(train, calibration, names, TEST_START, POLICY))
    p0 = [fits[0].predict_probability(r.features) for r in btest]
    p1 = [fits[1].predict_probability(r.features) for r in test]
    fixed = _comparison(test, p0, p1, labels)
    fixed['baseline_fit'], fixed['candidate_fit'] = map(_fit_details, fits)
    fixed['complete_context'] = _cohort(test, coverage['availability_by_bout'])
    for record in fixed['predictions']:
        record['context_complete'] = bool(coverage['availability_by_bout'].get(record['bout_id']))
    rb, br, p0 = _rolling(baseline, base_names, POLICY, years=EVALUATION_YEARS)
    rc, cr, p1 = _rolling(rows, names, POLICY, years=EVALUATION_YEARS)
    if [r.bout_id for r in br] != [r.bout_id for r in cr]:
        raise ValueError('V7 rolling comparators are not identical')
    rolling = _comparison(cr, p0, p1, labels)
    rolling['baseline_folds'], rolling['candidate_folds'] = rb['folds'], rc['folds']
    rolling['predictions_sha256'] = _digest_json(rolling.pop('predictions'))
    accuracy_ok = fixed['candidate']['metrics']['accuracy'] >= fixed['baseline']['metrics']['accuracy']
    comparison_passed = (selection['pretest_improved'] and selection['pretest_coverage_sufficient']
                         and fixed['candidate_minus_baseline_log_loss'] < 0 and accuracy_ok and _sufficient(fixed['complete_context']))
    report = _signed({'schema_version': 1, 'protocol_version': PROTOCOL_VERSION,
        'created_at_utc': utc_string(utc_now()), 'research_only': True, 'promotion_eligible': False,
        'fixed_test_is_reused_benchmark': True, 'fresh_holdout': False,
        'selection_fixed_test_labels_used': False, 'selection_frozen_before_evaluation': True,
        'automatic_model_promotion': False, 'research_comparison_passed': bool(comparison_passed),
        'selection_manifest_sha256': selection['manifest_sha256'], 'selection_file_sha256': file_sha256(selection_path),
        **{key: selection[key] for key in ('database_sha256', 'source_manifest_sha256', 'statistics_sha256', 'implementation_sha256')},
        'baseline_model_version': 'logistic-prior-v6:balanced_recent5', 'candidate_model_version': MODEL_VERSION,
        'selected_candidate_id': candidate.candidate_id, 'selected_policy': asdict(POLICY),
        'feature_names': names, 'baseline_feature_names': base_names, 'definitions': _definitions(),
        'pretest_improved': selection['pretest_improved'], 'fixed_accuracy_not_lower': accuracy_ok,
        'fixed_test': fixed, 'rolling_origin': rolling, 'source_summary': source,
        'context_source_summary': context.source_summary, 'context_feature_coverage': {k: v for k, v in coverage.items() if k != 'availability_by_bout'},
        'limitations': ['The fixed historical benchmark was already reused; this is not fresh validation or a promised accuracy gain.',
                        'Rolling summaries reuse development years as well as evaluation years.',
                        'Source crosswalks, reach and captures are retrospective; historical observation timing is not established.',
                        'Five eligible prior bouts with whole-date ties differ from the latest five source rows displayed to users.',
                        'Doctor/DQ/administrative/NC/unknown methods do not enter combat-finish denominators.',
                        'Measured style proportions are sample descriptions, not invented biographical labels or wagering evidence.']}, 'report_content_sha256')
    load_v7_selection(selection_path, database, archive, statistics)
    write_new_json(output_path, report)
    return report


def load_verified_v7_report(report_path, selection_path, database=None, archive=None, statistics=None):
    """Verify frozen source binding and recompute fixed metrics from predictions."""
    selection = load_v7_selection(selection_path, database, archive, statistics)
    report = _read_document(report_path, 'report_content_sha256')
    try:
        if (report['protocol_version'] != PROTOCOL_VERSION or report['research_only'] is not True
                or report['promotion_eligible'] is not False or report['automatic_model_promotion'] is not False
                or report['fixed_test_is_reused_benchmark'] is not True or report['fresh_holdout'] is not False
                or report['selection_fixed_test_labels_used'] is not False or report['selection_frozen_before_evaluation'] is not True
                or report['selection_manifest_sha256'] != selection['manifest_sha256']
                or report['selection_file_sha256'] != file_sha256(selection_path)
                or report['definitions'] != _definitions() or report['feature_names'] != selection['feature_names']
                or report['selected_candidate_id'] != selection['selected_candidate_id']
                or any(report[key] != selection[key] for key in ('database_sha256', 'source_manifest_sha256', 'statistics_sha256', 'implementation_sha256'))):
            raise ValueError('V7 report is not bound to its frozen research selection')
        fixed, records = report['fixed_test'], report['fixed_test']['predictions']
        if not records or len({r['bout_id'] for r in records}) != len(records):
            raise ValueError('V7 fixed predictions are missing or duplicated')
        if any(r['event_date'] < TEST_START or type(r['context_complete']) is not bool
               or r['target_fighter_a_won'] not in (0, 1)
               or any(not isinstance(r[key], (int, float)) or not math.isfinite(r[key]) or not 0 < r[key] < 1
                      for key in ('baseline_probability_a', 'candidate_probability_a')) for r in records):
            raise ValueError('V7 prediction timing or probabilities are invalid')
        rows = [FeatureRow(r['bout_id'], r['event_id'], r['event_date'], r['fighter_a_id'], r['fighter_b_id'],
                           tuple(r['features']), r['target_fighter_a_won'], tuple(r['coverage'][name] for name in COVERAGE_NAMES)) for r in records]
        for label, key in (('baseline', 'baseline_probability_a'), ('candidate', 'candidate_probability_a')):
            metrics = metric_report(rows, [r[key] for r in records])
            if any(fixed[label].get(field) != value for field, value in metrics.items()):
                raise ValueError('V7 saved metrics do not reproduce their fixed prediction records')
        complete = {'bouts': sum(r['context_complete'] for r in records),
                    'share': sum(r['context_complete'] for r in records) / len(records)}
        delta = fixed['candidate']['metrics']['log_loss'] - fixed['baseline']['metrics']['log_loss']
        accuracy_ok = fixed['candidate']['metrics']['accuracy'] >= fixed['baseline']['metrics']['accuracy']
        passed = (selection['pretest_improved'] and selection['pretest_coverage_sufficient'] and delta < 0
                  and accuracy_ok and _sufficient(complete))
        if (fixed['complete_context'] != complete or fixed['candidate_minus_baseline_log_loss'] != delta
                or report['fixed_accuracy_not_lower'] is not accuracy_ok or report['research_comparison_passed'] is not bool(passed)
                or report['pretest_improved'] is not selection['pretest_improved']):
            raise ValueError('V7 report comparison facts do not reproduce their records')
    except (KeyError, TypeError) as exc:
        raise ValueError('Incomplete V7 evaluation') from exc
    return report


def prepare_v7_research(connection, event_id, as_of, history_path, stats_overlay, rich_overlay, context_overlay, selection):
    """Fit an explicitly selected V7 research candidate for a scheduled card.

    Accepts SQLite or MySQL read-only research handles through existing facades.
    The operating-card guards and eleven baseline features are unchanged.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError('V7 forecast requires a timezone-aware cutoff')
    as_of = as_of.astimezone(timezone.utc)
    if as_of > utc_now() or parse_utc(context_overlay.captured_at_utc) >= as_of or parse_utc(rich_overlay.captured_at_utc) >= as_of:
        raise ValueError('V7 source capture must precede a nonfuture forecast cutoff')
    if (selection.get('protocol_version') != PROTOCOL_VERSION or selection.get('selected_policy') != asdict(POLICY)
            or selection.get('implementation_sha256') != _implementation()
            or selection.get('statistics_sha256') != context_overlay.statistics_sha256
            or context_overlay.statistics_sha256 != rich_overlay.stats_db_sha256
            or selection.get('database_sha256') != file_sha256(history_path)):
        raise ValueError('V7 forecast requires its frozen selection and source identities')
    candidate = _candidate(selection['selected_candidate_id'])
    base = prepare_research_logistic(connection, event_id, as_of, history_path, model_policy=POLICY,
        selection_manifest_sha256=selection['manifest_sha256'], stats_overlay=stats_overlay)
    before = base.artifact['history_before_date']
    through = (date.fromisoformat(before) - timedelta(days=1)).isoformat()
    with read_only(history_path) as research:
        rows, source, names = v7_feature_rows(research, stats_overlay, rich_overlay, context_overlay, candidate.candidate_id, through)
    eligible = eligible_training_rows(rows, before, POLICY)
    train, calibration = _split_for_live(eligible)
    fitted = fit_separated_policy(train, calibration, names, before, POLICY)
    rich_builder, context_builder = RichHistory(rich_overlay, BASELINE_CANDIDATE), ContextHistory(context_overlay, candidate)
    event = connection.execute('SELECT event_date FROM events WHERE event_id=?', (event_id,)).fetchone()
    predictions, metadata, coverage, target_rows = {}, {}, {}, []
    labels = {name + '_difference': label for name, label, _ in V6_METRICS}
    labels.update({name + suffix: ('Recorded coverage: ' if 'observed' in suffix else '') + label
                   for name, label, _, _ in METRICS for suffix in ('_difference', '_observed_difference')})
    for bout_id, meta in base.metadata_by_bout.items():
        bout = connection.execute('SELECT * FROM bouts WHERE bout_id=?', (bout_id,)).fetchone()
        extra, ra, rb = rich_builder.difference(bout['fighter_a_id'], bout['fighter_b_id'], event['event_date'], bout_id, before)
        context_extra, ca, cb = context_builder.difference(bout['fighter_a_id'], bout['fighter_b_id'], event['event_date'], bout_id, before)
        features = tuple(meta['feature_values']) + extra + context_extra
        probability = fitted.predict_probability(features)
        contributions = [{'feature': name, 'label': PLAIN_LABELS.get(name, labels.get(name, name)),
                          'value': value, 'weight': weight, 'calibration_scale': fitted.calibrator.scale,
                          'logit_contribution': value * weight * fitted.calibrator.scale}
                         for name, value, weight in zip(names, features, fitted.model.weights)]
        contributions.sort(key=lambda r: abs(r['logit_contribution']), reverse=True)
        item = copy.deepcopy(meta)
        item.update(algorithm=MODEL_VERSION, feature_names=names, feature_values=features,
                    feature_contributions=contributions, top_feature_contributions=contributions[:3],
                    rich_candidate_id=BASELINE_CANDIDATE.candidate_id, context_candidate_id=candidate.candidate_id,
                    context_statistics_sha256=context_overlay.statistics_sha256)
        for side, rich, context in (('fighter_a', ra, ca), ('fighter_b', rb, cb)):
            item['coverage_by_fighter'][side].update({k: v for k, v in rich.items() if k != 'metrics'})
            item['coverage_by_fighter'][side].update(prior_rich_stat_bouts=rich['rich_selected_stat_bouts'],
                rich_metrics=rich['metrics'], context_metrics=context['metrics'],
                prior_context_eligible_stat_bouts=context['eligible_prior_bouts'],
                prior_context_selected_stat_bouts=context['selected_prior_bouts'], context_window=context['window'],
                context_source_bout_ids=context['source_bout_ids'])
            item['coverage_by_fighter'][side]['fight_statistics'] = bool(item['coverage_by_fighter'][side]['fight_statistics'] or rich['rich_stat_bouts'])
        predictions[bout_id], metadata[bout_id] = probability, item
        coverage[bout_id] = dict(base.coverage_by_bout[bout_id])
        coverage[bout_id].update(fighter_a_rich_stats=ra['rich_stat_bouts'] > 0, fighter_b_rich_stats=rb['rich_stat_bouts'] > 0)
        target_rows.append((bout_id, features))
    inputs = {'algorithm': MODEL_VERSION, 'selected_candidate_id': candidate.candidate_id,
        'selection_manifest_sha256': selection['manifest_sha256'], 'selected_policy': asdict(POLICY),
        'history_before_date': before, 'feature_names': names, 'weights': fitted.model.weights,
        'calibration_scale': fitted.calibrator.scale, 'fit': _fit_details(fitted),
        'statistics_sha256': context_overlay.statistics_sha256, 'context_inputs_sha256': context_overlay.inputs_sha256,
        'rich_inputs_sha256': rich_overlay.inputs_sha256, 'target_features_sha256': _digest_json(target_rows)}
    version = f'{MODEL_VERSION}:research_retrospective:{_digest_json(inputs)[:16]}'
    artifact = {**base.artifact, **inputs, 'model_version': version, 'history_source_summary': source,
        'training_rows_sha256': _digest_json([[r.bout_id, r.event_date, r.features, r.target] for r in fitted.training]),
        'calibration_rows_sha256': _digest_json([[r.bout_id, r.event_date, r.features, r.target] for r in fitted.calibration]),
        'training_bouts': len(fitted.training), 'training_last_date': fitted.training[-1].event_date,
        'calibration_bouts': len(fitted.calibration), 'calibration_first_date': fitted.calibration[0].event_date,
        'calibration_last_date': fitted.calibration[-1].event_date, 'current_career_averages_used': False,
        'research_only': True, 'promotion_eligible': False, 'operating_eligibility': False}
    return ResearchTrainingRun(fitted.model, fitted.calibrator, version, predictions, coverage, metadata, artifact)


def run_v7(database='mysql:research-complete', statistics='mysql:statistics', *, archive, output_dir):
    """Create an exclusive protocol → pretest selection → research comparison."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    protocol, selection, report = (directory / name for name in ('protocol.json', 'selection.json', 'evaluation.json'))
    freeze_v7_protocol(database, archive, statistics, protocol)
    selected = select_v7(database, archive, statistics, protocol, selection)
    result = evaluate_v7(database, archive, statistics, selection, report)
    return {'report': result, 'selection': selected,
            'artifacts': {'protocol': str(protocol), 'selection': str(selection), 'evaluation': str(report)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='mysql:research-complete')
    parser.add_argument('--statistics', default='mysql:statistics')
    parser.add_argument('--archive', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    result = run_v7(args.database, args.statistics, archive=args.archive, output_dir=args.output_dir)
    report = result['report']
    print('V7 research candidate:', report['selected_candidate_id'])
    print('Reused historical benchmark:', report['fixed_test']['baseline']['metrics'], report['fixed_test']['candidate']['metrics'])
    print('Research only; no automatic model promotion.')


if __name__ == '__main__':
    main()
