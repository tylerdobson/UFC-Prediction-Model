"""Bounded V6 research experiments using chronological, rich per-fight history.

The first eleven features reproduce V5. New features summarize earlier
canonical UFCStats bouts for safely crosswalked fighters. Target bouts and
all records within two preceding target dates are excluded. Current career averages
never enter a feature. These remain retrospective research models.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta, timezone
from pathlib import Path

from .features import COVERAGE_NAMES, FeatureRow
from .live_logistic import _split_for_live
from .model_lab import TEST_START, VALIDATION_END, file_sha256, fixed_split, metric_report, read_only, write_new_json
from .pipeline import parse_utc, utc_now, utc_string
from .research_evaluation import _digest_json
from .research_stats import POLICY, _coverage_comparison, load_research_stats, stats_feature_rows
from .research_training import PLAIN_LABELS, ResearchTrainingRun, prepare_research_logistic
from .research_upgrade import (
    EVALUATION_YEARS, PRETEST_YEARS, TrainingPolicy, _comparison, _fit_details,
    _paired, _read_document, _rolling, _signed, eligible_training_rows, fit_separated_policy,
)


MODEL_VERSION = 'logistic-prior-v6'
PROTOCOL_VERSION = 'research-v6-three-rich-families-v1'
MIN_COMPLETE_BOUTS = 100
MIN_COMPLETE_SHARE = .25
NEAR_TARGET_DAYS = 2
PSEUDO_MINUTES = 30.

METRICS = (
    ('sig_landed_per_min', 'Significant strikes landed per minute', 5.),
    ('sig_absorbed_per_min', 'Significant strikes absorbed per minute', 5.),
    ('sig_accuracy', 'Significant strike accuracy', .25),
    ('sig_defense', 'Significant strike defense', .25),
    ('knockdowns_per_15', 'Knockdowns per 15 minutes', 1.),
    ('takedowns_per_15', 'Takedowns landed per 15 minutes', 3.),
    ('takedown_accuracy', 'Takedown accuracy', .25),
    ('takedown_defense', 'Takedown defense', .25),
    ('submission_attempts_per_15', 'Submission attempts per 15 minutes', 2.),
    ('control_share', 'Share of fight time in control', .5),
)


@dataclass(frozen=True)
class RichCandidate:
    candidate_id: str
    metric_count: int
    recent_bouts: int | None = None

    @property
    def feature_names(self):
        return tuple(key + '_difference' for key, _, _ in METRICS[:self.metric_count])


CANDIDATES = (
    RichCandidate('striking_career', 5),
    RichCandidate('balanced_career', 10),
    RichCandidate('balanced_recent5', 10, 5),
)


def _candidate(value):
    matches = [candidate for candidate in CANDIDATES if candidate.candidate_id == value]
    if len(matches) != 1:
        raise ValueError('Unknown frozen V6 candidate')
    return matches[0]


def _field(record, key, default=None):
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _record_digest(record):
    return {key: _field(record, key) for key in ('bout_id', 'event_date', 'elapsed_seconds',
            'totals', 'opponent_totals', 'opponent_id', 'research_bout_id')}


@dataclass(frozen=True)
class RichStatsOverlay:
    records_by_fighter: dict[str, tuple]
    canonical_by_research: dict[str, str]
    captured_at_utc: str
    stats_db_sha256: str
    inputs_sha256: str
    source_summary: dict
    through_date: str | None = None


def load_rich_overlay(stats_db_path, through_date=None):
    """Load all canonical prior records for exact, unambiguous fighter links."""
    from .historical_stats import HistoryStore
    before = ((date.fromisoformat(through_date) + timedelta(days=1)).isoformat()
              if through_date else '9999-12-31')
    records, canonical = defaultdict(dict), {}
    with HistoryStore(stats_db_path) as store:
        metadata = dict(store.metadata)
        for fighter in store.research_fighter_ids():
            profile = store.fighter_profile(fighter)
            if profile:
                canonical[fighter] = profile['canonical_id']
        for record in store.all_research_fighter_bouts(through_date):
            fighter,bout = _field(record,'research_fighter_id'),_field(record,'bout_id')
            if not fighter or not bout or _field(record,'event_date') >= before:
                raise ValueError('Rich history returned an invalid future record')
            if bout in records[fighter] and _record_digest(records[fighter][bout]) != _record_digest(record):
                raise ValueError('Conflicting canonical fight statistics')
            records[fighter][bout] = record
    records = {fighter:tuple(sorted(items.values(),key=lambda r:(_field(r,'event_date'),_field(r,'bout_id'))))
               for fighter,items in records.items()}
    captured = metadata['captured_at_utc']
    parse_utc(captured)
    digest = _digest_json({fighter: [_record_digest(r) for r in values] for fighter, values in sorted(records.items())})
    summary = {'statistics_database_sha256': file_sha256(stats_db_path), 'store_metadata': metadata,
               'captured_at_utc': captured, 'through_date': through_date,
               'crosswalked_fighters': len(records), 'fighter_bout_records': sum(map(len, records.values())),
               'inputs_sha256': digest, 'identity_rule': 'exact_unambiguous_persistent_source_crosswalk',
               'history_rule': 'all_validated_canonical_bouts_for_crosswalked_fighters_not_only_Wikipedia_matched_bouts'}
    return RichStatsOverlay(records, canonical, captured, file_sha256(stats_db_path), digest, summary, through_date)


def _number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


def _totals(record, opponent=False):
    return _field(record, 'opponent_totals' if opponent else 'totals', {}) or {}


def _rate(records, field, prior, *, per=1., opponent=False):
    total, minutes, count = 0., 0., 0
    for record in records:
        value = _number(_totals(record, opponent).get(field))
        seconds = _number(_field(record, 'elapsed_seconds'))
        if value is not None and seconds is not None and seconds > 0:
            total += value; minutes += seconds / 60.; count += 1
    return (total + prior * PSEUDO_MINUTES / per) / (minutes + PSEUDO_MINUTES) * per, count


def _accuracy(records, landed_key, attempted_key, prior, pseudo_attempts, *, opponent=False, defense=False):
    landed, attempted, count = 0., 0., 0
    for record in records:
        values = _totals(record, opponent)
        a, b = _number(values.get(landed_key)), _number(values.get(attempted_key))
        if a is not None and b is not None and a <= b:
            landed += a; attempted += b; count += int(b > 0)
    probability = (landed + prior * pseudo_attempts) / (attempted + pseudo_attempts)
    return (1 - probability if defense else probability), count


def _control(records):
    controlled, seconds, count = 0., 0., 0
    for record in records:
        value = _number(_totals(record).get('control_seconds'))
        elapsed = _number(_field(record, 'elapsed_seconds'))
        if value is not None and elapsed is not None and elapsed > 0 and value <= elapsed:
            controlled += value; seconds += elapsed; count += 1
    return (controlled + .25 * 60 * PSEUDO_MINUTES) / (seconds + 60 * PSEUDO_MINUTES), count


def _metric_values(records):
    values = (
        _rate(records, 'sig_landed', 3.),
        _rate(records, 'sig_landed', 3., opponent=True),
        _accuracy(records, 'sig_landed', 'sig_attempted', .5, 50.),
        _accuracy(records, 'sig_landed', 'sig_attempted', .5, 50., opponent=True, defense=True),
        _rate(records, 'kd', .1, per=15.),
        _rate(records, 'td_landed', 1., per=15.),
        _accuracy(records, 'td_landed', 'td_attempted', .4, 10.),
        _accuracy(records, 'td_landed', 'td_attempted', .4, 10., opponent=True, defense=True),
        _rate(records, 'sub_attempts', .5, per=15.),
        _control(records),
    )
    return {key: {'value': value, 'bouts': count} for (key, _, _), (value, count) in zip(METRICS, values)}


class RichHistory:
    """Maintain past history with a conservative whole-date target embargo."""
    def __init__(self, overlay, candidate):
        self.overlay, self.candidate = overlay, candidate
        self.events = sorted(((_field(record, 'event_date'), fighter, record)
                              for fighter, records in overlay.records_by_fighter.items() for record in records),
                             key=lambda item: (item[0], item[1], _field(item[2], 'bout_id')))
        self.index, self.day = 0, None
        self.history = defaultdict(list)

    def advance(self, target_date):
        date.fromisoformat(target_date)
        if self.day and target_date < self.day:
            raise ValueError('Rich features must replay in chronological order')
        self.day = target_date
        while self.index < len(self.events) and self.events[self.index][0] < target_date:
            _, fighter, record = self.events[self.index]
            self.history[fighter].append(record)
            self.index += 1

    def view(self, fighter, opponent, target_date, target_bout_id=None, history_before_date=None):
        self.advance(history_before_date or target_date)
        target = date.fromisoformat(target_date)
        selected, excluded = [], 0
        for record in self.history[fighter]:
            age_days = (target - date.fromisoformat(_field(record, 'event_date'))).days
            target_match = target_bout_id and _field(record, 'research_bout_id') == target_bout_id
            # Date disagreements can also leak a different tournament bout
            # from the same real event. Embargo every record in these two
            # preceding days, regardless of opponent or identity mapping.
            near_date = age_days <= NEAR_TARGET_DAYS
            if target_match or near_date:
                excluded += 1
                continue
            selected.append(record)
        total_count = len(selected)
        if self.candidate.recent_bouts and len(selected) > self.candidate.recent_bouts:
            boundary = _field(selected[-self.candidate.recent_bouts], 'event_date')
            selected = [record for record in selected if _field(record, 'event_date') >= boundary]
        metrics = _metric_values(selected)
        timed = [record for record in selected if (_number(_field(record, 'elapsed_seconds')) or 0) > 0
                 and _number(_totals(record).get('sig_landed')) is not None
                 and _number(_totals(record, True).get('sig_landed')) is not None]
        return {'metrics': metrics, 'rich_stat_bouts': len(timed), 'rich_total_stat_bouts': total_count,
                'rich_selected_stat_bouts': len(selected),
                'rich_stat_minutes': sum(_field(record, 'elapsed_seconds') for record in timed) / 60.,
                'near_target_records_excluded': excluded, 'recent_bouts': self.candidate.recent_bouts}

    def difference(self, a, b, day, bout_id=None, history_before_date=None):
        left = self.view(a,b,day,bout_id,history_before_date)
        right = self.view(b,a,day,bout_id,history_before_date)
        values = tuple((left['metrics'][key]['value'] - right['metrics'][key]['value']) / scale
                       for key, _, scale in METRICS[:self.candidate.metric_count])
        if not all(math.isfinite(value) for value in values):
            raise ValueError('Nonfinite rich fight features')
        return values, left, right


def _enrich(base_rows, rich_overlay, candidate):
    builder = RichHistory(rich_overlay,candidate)
    enriched, availability, excluded = [], Counter(), 0
    for row in base_rows:
        extra, left, right = builder.difference(row.fighter_a_id,row.fighter_b_id,row.event_date,row.bout_id)
        coverage = tuple(row.coverage[:4]) + (left['rich_stat_bouts'] > 0,right['rich_stat_bouts'] > 0)
        enriched.append(FeatureRow(row.bout_id,row.event_id,row.event_date,row.fighter_a_id,row.fighter_b_id,
                                   tuple(row.features)+extra,row.target,coverage,row.prior_result_rows_available))
        for key,_,_ in METRICS[:candidate.metric_count]:
            availability[key] += int(left['metrics'][key]['bouts'] > 0 and right['metrics'][key]['bouts'] > 0)
        excluded += left['near_target_records_excluded'] + right['near_target_records_excluded']
    return enriched, {'both_fighter_metric_coverage': dict(availability), 'near_target_record_exclusions': excluded}


def rich_feature_rows(connection, stats_overlay, rich_overlay, candidate_id, through_date=None):
    if rich_overlay.through_date and (through_date is None or through_date > rich_overlay.through_date):
        raise ValueError('Rich overlay does not cover the requested dates')
    base, source, names = stats_feature_rows(connection,stats_overlay,through_date=through_date)
    candidate = _candidate(candidate_id)
    rows, coverage = _enrich(base,rich_overlay,candidate)
    source = {**source, 'rich_statistics': rich_overlay.source_summary, 'rich_feature_coverage': coverage,
              'rich_feature_rows_sha256': _digest_json([[r.bout_id,r.event_date,r.features,r.target] for r in rows])}
    return rows, source, tuple(names)+candidate.feature_names


def prepare_v6_research(connection,event_id,as_of,history_path,stats_overlay,rich_overlay,selection):
    """Fit a frozen rich policy for any scheduled card, preserving V5 guards."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError('V6 forecast requires a timezone-aware cutoff')
    as_of = as_of.astimezone(timezone.utc)
    if as_of > utc_now():
        raise ValueError('V6 forecast cutoff cannot be in the future')
    if parse_utc(rich_overlay.captured_at_utc) >= as_of:
        raise ValueError('Rich history capture must precede the forecast cutoff')
    if selection.get('protocol_version') != PROTOCOL_VERSION or selection.get('selected_policy') != asdict(POLICY):
        raise ValueError('V6 forecasting requires its frozen selection')
    if selection.get('stats_db_sha256') != rich_overlay.stats_db_sha256:
        raise ValueError('Rich statistics database does not match the frozen selection')
    if selection.get('database_sha256') != file_sha256(history_path):
        raise ValueError('V6 selection does not match the research result history')
    candidate = _candidate(selection['selected_candidate_id'])
    base = prepare_research_logistic(connection,event_id,as_of,history_path,model_policy=POLICY,
                                     selection_manifest_sha256=selection['manifest_sha256'],stats_overlay=stats_overlay)
    before = base.artifact['history_before_date']
    through = (date.fromisoformat(before)-timedelta(days=1)).isoformat()
    with read_only(history_path) as research:
        rows, source, names = rich_feature_rows(research,stats_overlay,rich_overlay,candidate.candidate_id,through)
    eligible = eligible_training_rows(rows,before,POLICY)
    train, calibration = _split_for_live(eligible)
    fitted = fit_separated_policy(train,calibration,names,before,POLICY)
    builder = RichHistory(rich_overlay,candidate)
    predictions, metadata, coverage, target_rows = {}, {}, {}, []
    labels = {key+'_difference':label for key,label,_ in METRICS}
    for bout_id, meta in base.metadata_by_bout.items():
        bout = connection.execute('SELECT * FROM bouts WHERE bout_id=?',(bout_id,)).fetchone()
        event = connection.execute('SELECT event_date FROM events WHERE event_id=?',(event_id,)).fetchone()
        extra,left,right = builder.difference(bout['fighter_a_id'],bout['fighter_b_id'],event['event_date'],bout_id,before)
        features = tuple(meta['feature_values'])+extra
        probability = fitted.predict_probability(features)
        contributions = [{'feature':name,'label':PLAIN_LABELS.get(name,labels.get(name,name)),
                          'value':value,'weight':weight,'calibration_scale':fitted.calibrator.scale,
                          'logit_contribution':value*weight*fitted.calibrator.scale}
                         for name,value,weight in zip(names,features,fitted.model.weights)]
        contributions.sort(key=lambda row:abs(row['logit_contribution']),reverse=True)
        item = copy.deepcopy(meta)
        item.update({'algorithm':MODEL_VERSION,'feature_names':names,'feature_values':features,
                     'feature_contributions':contributions,'top_feature_contributions':contributions[:3],
                     'rich_candidate_id':candidate.candidate_id,'rich_statistics_store_sha256':rich_overlay.stats_db_sha256})
        for side,view in (('fighter_a',left),('fighter_b',right)):
            item['coverage_by_fighter'][side].update({k:v for k,v in view.items() if k != 'metrics'})
            item['coverage_by_fighter'][side]['prior_rich_stat_bouts'] = view['rich_selected_stat_bouts']
            item['coverage_by_fighter'][side]['fight_statistics'] = bool(
                item['coverage_by_fighter'][side]['fight_statistics'] or view['rich_stat_bouts'])
            item['coverage_by_fighter'][side]['rich_metrics'] = view['metrics']
        predictions[bout_id],metadata[bout_id] = probability,item
        coverage[bout_id] = dict(base.coverage_by_bout[bout_id])
        coverage[bout_id]['fighter_a_rich_stats'] = left['rich_stat_bouts'] > 0
        coverage[bout_id]['fighter_b_rich_stats'] = right['rich_stat_bouts'] > 0
        target_rows.append((bout_id,features))
    inputs = {'algorithm':MODEL_VERSION,'selected_candidate_id':candidate.candidate_id,
              'selection_manifest_sha256':selection['manifest_sha256'],'selected_policy':asdict(POLICY),
              'history_before_date':before,'feature_names':names,'weights':fitted.model.weights,
              'calibration_scale':fitted.calibrator.scale,'fit':_fit_details(fitted),
              'rich_statistics_store_sha256':rich_overlay.stats_db_sha256,'rich_inputs_sha256':rich_overlay.inputs_sha256,
              'target_features_sha256':_digest_json(target_rows)}
    version = f'{MODEL_VERSION}:research_retrospective:{_digest_json(inputs)[:16]}'
    artifact = {**base.artifact,**inputs,'model_version':version,'history_source_summary':source,
                'training_rows_sha256':_digest_json([[r.bout_id,r.event_date,r.features,r.target] for r in fitted.training]),
                'calibration_rows_sha256':_digest_json([[r.bout_id,r.event_date,r.features,r.target] for r in fitted.calibration]),
                'training_bouts':len(fitted.training),'training_last_date':fitted.training[-1].event_date,
                'calibration_bouts':len(fitted.calibration),'calibration_first_date':fitted.calibration[0].event_date,
                'calibration_last_date':fitted.calibration[-1].event_date,
                'current_career_averages_used':False,'research_only':True,'promotion_eligible':False}
    return ResearchTrainingRun(fitted.model,fitted.calibrator,version,predictions,coverage,metadata,artifact)


def _definitions():
    return {'candidate_families':[asdict(candidate) for candidate in CANDIDATES],
            'metrics':[{'metric':key,'label':label,'difference_divisor':scale} for key,label,scale in METRICS],
            'pseudo_exposure_minutes':PSEUDO_MINUTES,
            'neutral_priors':{'sig_landed_per_min':3.,'sig_absorbed_per_min':3.,'sig_accuracy':.5,
                              'sig_defense':.5,'knockdowns_per_15':.1,'takedowns_per_15':1.,
                              'takedown_accuracy':.4,'takedown_defense':.6,'submission_attempts_per_15':.5,'control_share':.25},
            'accuracy_pseudo_attempts':{'significant_strikes':50.,'takedowns':10.},
            'missing_rule':'Missing fields skip their numerator and exposure; neutral shrinkage is not a recorded observation.',
            'recent_rule':'Last five statistic bouts, including all ties on the boundary UTC date.',
            'timing_rule':'All records from target day and future are withheld; additionally embargo all records in the preceding two dates.',
            'explicit_target_rule':'An explicitly linked target research bout is excluded regardless of its archive date.',
            'current_career_averages_used':False}


def freeze_v6_protocol(database_path,archive_manifest_path,stats_db_path,output_path):
    """Persist declarations before even pretest prediction losses are fitted."""
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    rich = load_rich_overlay(stats_db_path)
    database_hash,archive_hash = file_sha256(database_path),file_sha256(archive_manifest_path)
    metadata = rich.source_summary['store_metadata']
    if metadata.get('research_database_sha256') != database_hash or metadata.get('source_manifest_sha256') != archive_hash:
        raise ValueError('Rich history store does not match the declared research/archive inputs')
    protocol = _signed({'schema_version':1,'protocol_version':PROTOCOL_VERSION,
        'created_at_utc':utc_string(utc_now()),'research_only':True,'promotion_eligible':False,
        'candidate_model_version':MODEL_VERSION,'baseline_model_version':'logistic-prior-v5',
        'candidate_count':len(CANDIDATES),'definitions':_definitions(),'selected_policy':asdict(POLICY),
        'selection_years':PRETEST_YEARS,'selection_labels_through':VALIDATION_END,
        'selection_rule':'minimum_pooled_pretest_rolling_calibrated_log_loss_then_declared_candidate_order',
        'fixed_test_rule':'evaluate_only_the_single_selected_candidate_once_on_reused_1447_bout_benchmark',
        'acceptance_rule':'lower_pretest_and_fixed_all_bout_log_loss_and_fixed_accuracy_not_lower_than_V5_and_useful_coverage',
        'minimum_complete_bouts':MIN_COMPLETE_BOUTS,'minimum_complete_share':MIN_COMPLETE_SHARE,
        'database_sha256':database_hash,'source_manifest_sha256':archive_hash,'stats_db_sha256':rich.stats_db_sha256,
        'implementation_sha256':file_sha256(__file__),
        'limitations':['Pretest and fixed historical periods were previously reported; no fresh holdout is claimed.',
                      'Archive fighter crosswalk and reach are retrospective; historical source availability is not established.',
                      'Rich history includes unmatched canonical past bouts only for exact unambiguous fighter links.',
                      'A conservative two-date embargo prevents the known one-day event-date and tournament leakage cases.']},'protocol_manifest_sha256')
    write_new_json(output_path,protocol)
    return protocol


def _load_protocol(path,database_path,archive_manifest_path,stats_db_path):
    protocol = _read_document(path,'protocol_manifest_sha256')
    if (protocol.get('protocol_version') != PROTOCOL_VERSION or protocol.get('definitions') != _definitions()
            or protocol.get('selected_policy') != asdict(POLICY)
            or protocol.get('implementation_sha256') != file_sha256(__file__)):
        raise ValueError('V6 protocol definitions or implementation changed after freezing')
    for key,value in (('database_sha256',database_path),('source_manifest_sha256',archive_manifest_path),('stats_db_sha256',stats_db_path)):
        if protocol.get(key) != file_sha256(value):
            raise ValueError('V6 protocol input hash changed')
    return protocol


def _rich_cohorts(rows,p0,p1):
    measured = _coverage_comparison(rows,p0,p1)
    return {'both_reach_and_prior_rich_stats':measured['both_reach_and_prior_paired_stats'],
            'incomplete_reach_or_rich_stats':measured['incomplete_reach_or_prior_stats']}


def _coverage_sufficient(cohort):
    return cohort['bouts'] >= MIN_COMPLETE_BOUTS and cohort['share'] >= MIN_COMPLETE_SHARE


def select_v6(database_path,archive_manifest_path,stats_db_path,protocol_path,output_path):
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    protocol = _load_protocol(protocol_path,database_path,archive_manifest_path,stats_db_path)
    rich = load_rich_overlay(stats_db_path,VALIDATION_END)
    with read_only(database_path) as connection:
        base_overlay = load_research_stats(connection,archive_manifest_path,through_date=VALIDATION_END)
        base_rows,source,base_names = stats_feature_rows(connection,base_overlay,through_date=VALIDATION_END)
    baseline,baseline_rows,p0 = _rolling(base_rows,base_names,POLICY)
    trials = []
    for candidate in CANDIDATES:
        rows,coverage = _enrich(base_rows,rich,candidate)
        names = tuple(base_names)+candidate.feature_names
        result,evaluation_rows,p1 = _rolling(rows,names,POLICY)
        if [r.bout_id for r in baseline_rows] != [r.bout_id for r in evaluation_rows]:
            raise ValueError('V6 pretest comparators are not the same bouts')
        trials.append({'candidate_id':candidate.candidate_id,'definition':asdict(candidate),
                       'pretest':result,'coverage_cohorts':_rich_cohorts(evaluation_rows,p0,p1),
                       'feature_coverage':coverage,'feature_names':names})
    selected = min(enumerate(trials),key=lambda item:(item[1]['pretest']['pooled']['metrics']['log_loss'],item[0]))[1]
    base_loss = baseline['pooled']['metrics']['log_loss']
    selection = _signed({'schema_version':1,'protocol_version':PROTOCOL_VERSION,
        'created_at_utc':utc_string(utc_now()),'research_only':True,'promotion_eligible':False,
        'protocol_manifest_sha256':protocol['protocol_manifest_sha256'],'protocol_file_sha256':file_sha256(protocol_path),
        'implementation_sha256':protocol['implementation_sha256'],'database_sha256':protocol['database_sha256'],
        'source_manifest_sha256':protocol['source_manifest_sha256'],'stats_db_sha256':protocol['stats_db_sha256'],
        'selected_policy':asdict(POLICY),'selected_candidate_id':selected['candidate_id'],
        'feature_names':selected['feature_names'],'candidate_count':len(CANDIDATES),
        'selection_labels_through':VALIDATION_END,'fixed_test_labels_used':False,
        'baseline':baseline,'trials':trials,'selected_pretest_log_loss':selected['pretest']['pooled']['metrics']['log_loss'],
        'pretest_improved':selected['pretest']['pooled']['metrics']['log_loss'] < base_loss,
        'pretest_coverage_sufficient':_coverage_sufficient(selected['coverage_cohorts']['both_reach_and_prior_rich_stats']),
        'rich_pretest_inputs_sha256':rich.inputs_sha256,'source_summary':source,
        'rich_source_summary':rich.source_summary,'definitions':_definitions(),
        'minimum_complete_bouts':MIN_COMPLETE_BOUTS,'minimum_complete_share':MIN_COMPLETE_SHARE,
        'fixed_accuracy_must_not_decrease':True},'manifest_sha256')
    _load_protocol(protocol_path,database_path,archive_manifest_path,stats_db_path)
    write_new_json(output_path,selection)
    return selection


def load_v6_selection(selection_path,database_path=None,archive_manifest_path=None,stats_db_path=None):
    selection = _read_document(selection_path,'manifest_sha256')
    try:
        if (selection['schema_version'] != 1 or selection['protocol_version'] != PROTOCOL_VERSION
                or selection['selected_policy'] != asdict(POLICY) or selection['definitions'] != _definitions()
                or selection['candidate_count'] != len(CANDIDATES)
                or selection['selection_labels_through'] != VALIDATION_END or selection['fixed_test_labels_used'] is not False
                or selection['fixed_accuracy_must_not_decrease'] is not True):
            raise ValueError('V6 selection violates the frozen protocol')
        trials = selection['trials']
        if [trial['candidate_id'] for trial in trials] != [candidate.candidate_id for candidate in CANDIDATES]:
            raise ValueError('V6 selection contains a different candidate search')
        chosen = min(enumerate(trials),key=lambda item:(item[1]['pretest']['pooled']['metrics']['log_loss'],item[0]))[1]
        if chosen['candidate_id'] != selection['selected_candidate_id']:
            raise ValueError('V6 chosen candidate is not minimum pretest loss')
        if (selection['feature_names'] != chosen['feature_names']
                or selection['selected_pretest_log_loss'] != chosen['pretest']['pooled']['metrics']['log_loss']
                or selection['pretest_improved'] is not (chosen['pretest']['pooled']['metrics']['log_loss'] < selection['baseline']['pooled']['metrics']['log_loss'])
                or selection['pretest_coverage_sufficient'] is not _coverage_sufficient(chosen['coverage_cohorts']['both_reach_and_prior_rich_stats'])
                or selection['minimum_complete_bouts'] != MIN_COMPLETE_BOUTS
                or selection['minimum_complete_share'] != MIN_COMPLETE_SHARE):
            raise ValueError('V6 selection derived facts do not match the candidate records')
        for key,path in (('database_sha256',database_path),('source_manifest_sha256',archive_manifest_path),('stats_db_sha256',stats_db_path)):
            if path is not None and selection[key] != file_sha256(path):
                raise ValueError('V6 selection input hash mismatch')
        for trial in trials:
            for fold in trial['pretest']['folds']:
                if (fold['year'] not in PRETEST_YEARS or fold['test_summary']['last_date'] > VALIDATION_END
                        or fold['fit']['training']['last_date'] >= fold['fit']['calibration']['first_date']
                        or fold['fit']['calibration']['last_date'] >= f"{fold['year']}-01-01"):
                    raise ValueError('V6 selection contains future or overlapping date folds')
    except (KeyError,TypeError) as exc:
        raise ValueError('Incomplete V6 selection') from exc
    return selection


def evaluate_v6(database_path,archive_manifest_path,stats_db_path,selection_path,output_path):
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    selection = load_v6_selection(selection_path,database_path,archive_manifest_path,stats_db_path)
    if selection['implementation_sha256'] != file_sha256(__file__):
        raise ValueError('V6 implementation changed after selection')
    pretest_rich = load_rich_overlay(stats_db_path,VALIDATION_END)
    if pretest_rich.inputs_sha256 != selection['rich_pretest_inputs_sha256']:
        raise ValueError('V6 pretest statistics changed after selection')
    rich = load_rich_overlay(stats_db_path)
    candidate = _candidate(selection['selected_candidate_id'])
    with read_only(database_path) as connection:
        overlay = load_research_stats(connection,archive_manifest_path)
        base_rows,source,base_names = stats_feature_rows(connection,overlay)
        rows,feature_coverage = _enrich(base_rows,rich,candidate)
        from .research_stats import _context
        context = _context(connection,rows)
    names = tuple(base_names)+candidate.feature_names
    train,calibration,test = fixed_split(rows)
    bt,bv,base_test = fixed_split(base_rows)
    runs = (fit_separated_policy(bt,bv,base_names,TEST_START,POLICY),
            fit_separated_policy(train,calibration,names,TEST_START,POLICY))
    p0 = [runs[0].predict_probability(row.features) for row in base_test]
    p1 = [runs[1].predict_probability(row.features) for row in test]
    fixed = _comparison(test,p0,p1,context)
    fixed['coverage_cohorts'] = _rich_cohorts(test,p0,p1)
    fixed['baseline_fit'],fixed['candidate_fit'] = map(_fit_details,runs)
    fixed['baseline_fit']['feature_names'],fixed['candidate_fit']['feature_names'] = base_names,names
    base,base_rolling,p0 = _rolling(base_rows,base_names,POLICY,years=EVALUATION_YEARS)
    selected,rolling_rows,p1 = _rolling(rows,names,POLICY,years=EVALUATION_YEARS)
    if [row.bout_id for row in base_rolling] != [row.bout_id for row in rolling_rows]:
        raise ValueError('V6 yearly evaluation comparators are not the same bouts')
    rolling = _comparison(rolling_rows,p0,p1,context)
    rolling['coverage_cohorts'] = _rich_cohorts(rolling_rows,p0,p1)
    rolling['baseline_folds'],rolling['candidate_folds'] = base['folds'],selected['folds']
    rolling['predictions_sha256'] = _digest_json(rolling.pop('predictions'))
    accuracy_ok = fixed['candidate']['metrics']['accuracy'] >= fixed['baseline']['metrics']['accuracy']
    coverage_ok = _coverage_sufficient(fixed['coverage_cohorts']['both_reach_and_prior_rich_stats'])
    accepted = (selection['pretest_improved'] and selection['pretest_coverage_sufficient']
                and fixed['candidate_minus_baseline_log_loss'] < 0 and accuracy_ok and coverage_ok)
    report = _signed({'schema_version':1,'protocol_version':PROTOCOL_VERSION,'created_at_utc':utc_string(utc_now()),
        'research_only':True,'promotion_eligible':False,'fixed_test_is_reused_benchmark':True,'fresh_holdout':False,
        'selection_fixed_test_labels_used':False,'selection_frozen_before_evaluation':True,
        'selection_manifest_sha256':selection['manifest_sha256'],'selection_file_sha256':file_sha256(selection_path),
        'protocol_manifest_sha256':selection['protocol_manifest_sha256'],'implementation_sha256':selection['implementation_sha256'],
        'database_sha256':selection['database_sha256'],'source_manifest_sha256':selection['source_manifest_sha256'],
        'stats_db_sha256':selection['stats_db_sha256'],'selected_policy':asdict(POLICY),'selected_candidate_id':candidate.candidate_id,
        'baseline_model_version':'logistic-prior-v5','candidate_model_version':MODEL_VERSION,
        'research_champion':MODEL_VERSION if accepted else 'logistic-prior-v5',
        'candidate_fixed_benchmark_improved':fixed['candidate_minus_baseline_log_loss'] < 0,
        'fixed_accuracy_not_lower':accuracy_ok,'fixed_coverage_sufficient':coverage_ok,
        'pretest_improved':selection['pretest_improved'],'pretest_coverage_sufficient':selection['pretest_coverage_sufficient'],
        'feature_names':names,'baseline_feature_names':base_names,'definitions':_definitions(),
        'fixed_test':fixed,'rolling_origin':rolling,'source_summary':source,
        'rich_source_summary':rich.source_summary,'rich_feature_coverage':feature_coverage,
        'limitations':['Reused historical benchmark and previously observed development years, not fresh validation.',
                      'Current career averages never enter features; missing metrics retain explicit neutral shrinkage.',
                      'Every rich feature excludes all records from the two preceding target dates and explicitly linked target bouts.',
                      'Archive dates, reach and source crosswalk are retrospective; source timing and coverage uncertainty remain.',
                      'A research champion is a policy choice, not operating validation, a reliable accuracy gain or a betting edge.']},'report_content_sha256')
    load_v6_selection(selection_path,database_path,archive_manifest_path,stats_db_path)
    write_new_json(output_path,report)
    return report


def load_verified_v6_report(report_path,selection_path,database_path=None,archive_manifest_path=None,stats_db_path=None):
    selection = load_v6_selection(selection_path,database_path,archive_manifest_path,stats_db_path)
    report = _read_document(report_path,'report_content_sha256')
    try:
        if (report['protocol_version'] != PROTOCOL_VERSION or report['research_only'] is not True
                or report['promotion_eligible'] is not False or report['fixed_test_is_reused_benchmark'] is not True
                or report['fresh_holdout'] is not False or report['selection_fixed_test_labels_used'] is not False
                or report['selection_frozen_before_evaluation'] is not True
                or report['selected_policy'] != selection['selected_policy']
                or report['definitions'] != _definitions()
                or report['feature_names'] != selection['feature_names']
                or report['selection_manifest_sha256'] != selection['manifest_sha256']
                or report['selection_file_sha256'] != file_sha256(selection_path)
                or report['selected_candidate_id'] != selection['selected_candidate_id']):
            raise ValueError('V6 evaluation is not bound to its frozen selection')
        for key in ('database_sha256','source_manifest_sha256','stats_db_sha256','implementation_sha256'):
            if report[key] != selection[key]:
                raise ValueError('V6 evaluation input mismatch')
        fixed = report['fixed_test']
        records = fixed['predictions']
        if len(records) != fixed['candidate']['metrics']['bouts'] or len({r['bout_id'] for r in records}) != len(records):
            raise ValueError('V6 fixed predictions are missing or duplicated')
        if any(r['event_date'] < TEST_START or r['target_fighter_a_won'] not in (0,1)
               or any(not math.isfinite(r[key]) or not 0 < r[key] < 1
                      for key in ('baseline_probability_a','candidate_probability_a')) for r in records):
            raise ValueError('V6 fixed prediction timing, probabilities or labels are invalid')
        reconstructed = [FeatureRow(r['bout_id'],r['event_id'],r['event_date'],r['fighter_a_id'],r['fighter_b_id'],
            tuple(r['features']),r['target_fighter_a_won'],tuple(r['coverage'][name] for name in COVERAGE_NAMES)) for r in records]
        for label,key in (('baseline','baseline_probability_a'),('candidate','candidate_probability_a')):
            measured = metric_report(reconstructed,[r[key] for r in records])
            if any(fixed[label].get(field) != value for field,value in measured.items()):
                raise ValueError('V6 saved metrics do not match fixed prediction records')
        from .research_stats import _complete
        count = sum(_complete(row) for row in reconstructed)
        coverage_ok = count >= MIN_COMPLETE_BOUTS and count/len(records) >= MIN_COMPLETE_SHARE
        accuracy_ok = fixed['candidate']['metrics']['accuracy'] >= fixed['baseline']['metrics']['accuracy']
        delta = fixed['candidate']['metrics']['log_loss'] - fixed['baseline']['metrics']['log_loss']
        if (report['fixed_accuracy_not_lower'] is not accuracy_ok
                or report['fixed_coverage_sufficient'] is not coverage_ok
                or report['pretest_improved'] is not selection['pretest_improved']
                or report['pretest_coverage_sufficient'] is not selection['pretest_coverage_sufficient']
                or fixed['candidate_minus_baseline_log_loss'] != delta
                or report['candidate_fixed_benchmark_improved'] is not (delta < 0)):
            raise ValueError('V6 evaluation derived facts do not match its records')
        accepted = (selection['pretest_improved'] and selection['pretest_coverage_sufficient'] and coverage_ok
                    and fixed['candidate']['metrics']['log_loss'] < fixed['baseline']['metrics']['log_loss'] and accuracy_ok)
        if report['research_champion'] != (MODEL_VERSION if accepted else 'logistic-prior-v5'):
            raise ValueError('V6 champion violates the frozen loss/accuracy/coverage rule')
    except (KeyError,TypeError) as exc:
        raise ValueError('Incomplete V6 evaluation') from exc
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    for command in ('protocol','select','evaluate','verify'):
        item = commands.add_parser(command)
        item.add_argument('--db',required=True); item.add_argument('--archive',required=True)
        item.add_argument('--statistics-db',required=True)
        if command != 'verify': item.add_argument('--out',required=True)
        if command == 'select': item.add_argument('--protocol',required=True)
        if command in ('evaluate','verify'): item.add_argument('--selection',required=True)
        if command == 'verify': item.add_argument('--report',required=True)
    args = parser.parse_args(argv)
    if args.command == 'protocol':
        result = freeze_v6_protocol(args.db,args.archive,args.statistics_db,args.out)
        print('Frozen V6 protocol:',result['protocol_manifest_sha256'])
    elif args.command == 'select':
        result = select_v6(args.db,args.archive,args.statistics_db,args.protocol,args.out)
        print('Frozen V6 candidate:',result['selected_candidate_id'])
    elif args.command == 'evaluate':
        result = evaluate_v6(args.db,args.archive,args.statistics_db,args.selection,args.out)
        print('Research champion:',result['research_champion'])
        print('Reused fixed benchmark:',result['fixed_test']['baseline']['metrics'],result['fixed_test']['candidate']['metrics'])
    else:
        result=load_verified_v6_report(args.report,args.selection,args.db,args.archive,args.statistics_db)
        print('Verified V6 report:',result['report_content_sha256'])


if __name__ == '__main__':
    main()
