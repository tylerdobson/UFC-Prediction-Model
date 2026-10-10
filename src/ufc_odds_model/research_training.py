"""Explicit retrospective research training for an upcoming operating card.

Research labels/features never become operating source-observation evidence.
All labels precede the UTC decision date and the event date. Target profiles,
including immutable birth dates, still need a verified pre-decision fetch.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from .features import COVERAGE_NAMES, FeatureBuilder, FeatureRow, _Profile, _profiles_as_of
from .live_logistic import _split_for_live
from .logistic import V3_FEATURE_NAMES, fit_logistic, fit_temperature, v3_features
from .model_lab import file_sha256, lab_rows, read_only
from .pipeline import parse_utc, utc_string
from .research_evaluation import _digest_json, _source_results
from .wikidata_profiles import static_profiles

# Frozen Phase 3 selections: validation K and pre-test rolling-origin window.
ELO_K = 64
TRAINING_START_YEAR = 2010
TRAINING_HISTORY = 'research_retrospective'

PLAIN_LABELS = {
    'elo_difference_400': 'Elo history gap',
    'log_prior_bouts_difference': 'UFC experience gap',
    'smoothed_win_rate_difference': 'UFC win rate gap',
    'rest_years_difference': 'Time since last fight',
    'known_rest_difference': 'Recorded fight history',
    'recent_form_5_difference': 'Recent results',
    'age_years_difference_10': 'Age gap',
    'reach_cm_difference_20': 'Reach gap',
    'opponent_adjusted_sig_strike_accuracy_difference': 'Adjusted strike accuracy',
    'log_stat_bouts_difference': 'Recorded strike history',
    'age_decline_32_difference': 'Age above 32',
    'known_age_difference': 'Age coverage',
    'known_reach_difference': 'Reach coverage',
    'known_sig_strike_stat_difference': 'Strike coverage',
}


def feature_contributions(features, names, weights, scale):
    if len(features) != len(names) or len(weights) != len(names):
        raise ValueError('Contribution dimensions do not match the model')
    values = [{'feature': name, 'label': PLAIN_LABELS[name], 'value': value,
               'weight': weight, 'calibration_scale': scale,
               'logit_contribution': weight*value*scale}
              for name, value, weight in zip(names, features, weights)]
    return sorted(values, key=lambda r: abs(r['logit_contribution']), reverse=True)


@dataclass(frozen=True)
class ResearchTrainingRun:
    model: object
    calibrator: object
    model_version: str
    predictions: dict
    coverage_by_bout: dict
    metadata_by_bout: dict
    artifact: dict


def prepare_research_logistic(connection, event_id, as_of_datetime: datetime, training_history):
    if as_of_datetime.tzinfo is None or as_of_datetime.utcoffset() is None:
        raise ValueError('Research training requires a timezone-aware cutoff')
    as_of = as_of_datetime.astimezone(timezone.utc)
    event = connection.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
    if event is None or event['status'] != 'scheduled':
        raise ValueError('Research-trained scoring requires a scheduled operating card')
    if event['source'] == 'wikipedia_research':
        raise ValueError('The operating card must be separate from the research history')
    if event['provider_status'] not in (None, 'scheduled', 'not_started'):
        raise ValueError('Provider event status is not eligible for pre-fight scoring')
    if event['event_date'] < as_of.date().isoformat():
        raise ValueError('Target event predates the forecast')
    if event['start_time_utc'] and parse_utc(event['start_time_utc']) <= as_of:
        raise ValueError('Target event has already started')
    bouts = connection.execute("SELECT * FROM bouts WHERE event_id=? AND status!='cancelled' ORDER BY bout_id",
                               (event_id,)).fetchall()
    if not bouts or any(b['status'] != 'scheduled' or b['provider_status'] not in
                        (None, 'scheduled', 'not_started') for b in bouts):
        raise ValueError('All target bouts must be scheduled and unstarted')
    history_path = Path(training_history).expanduser().resolve()
    operating_path = connection.execute('PRAGMA database_list').fetchone()['file']
    if operating_path and history_path == Path(operating_path).resolve():
        raise ValueError('Training history must be a separate read-only research database')
    before = min(event['event_date'], as_of.date().isoformat())
    through = (date.fromisoformat(before)-timedelta(days=1)).isoformat()
    database_hash = file_sha256(history_path)
    with read_only(history_path) as research:
        rows, source, names = lab_rows(research, feature_set='age-hinge', elo_k=ELO_K, through_date=through)
        # The live path keeps the existing minimum samples and separate tail
        # calibration. This 20% bout tail is recorded, not reselected on test.
        eligible = [r for r in rows if int(r.event_date[:4]) >= TRAINING_START_YEAR]
        training, calibration = _split_for_live(eligible)
        model = fit_logistic(training, feature_names=names)
        calibrator = fit_temperature([model.predict_probability(r.features) for r in calibration],
                                     [int(r.target) for r in calibration])
        builder = FeatureBuilder(elo_k=ELO_K)
        results = _source_results(research, include_restored=True, through_date=through)
        for _, group in groupby(results, key=lambda r: r['event_date']):
            builder.update_group(list(group))
        # Research admits immutable attributes retrospectively for fitting;
        # this prospective card requires they were fetched before its cutoff.
        profiles = static_profiles(research, cutoff_at_utc=utc_string(as_of))
        operating_profiles = _profiles_as_of(connection, utc_string(as_of), {})
        for fighter, profile in operating_profiles.items():
            existing = profiles.get(fighter, _Profile())
            if existing.birth_date and profile.birth_date and existing.birth_date != profile.birth_date:
                raise ValueError(f'Conflicting research and operating birth dates for {fighter}')
            profiles[fighter] = _Profile(profile.birth_date or existing.birth_date, profile.reach_cm)
        target_rows = []
        for bout in bouts:
            a, b = bout['fighter_a_id'], bout['fighter_b_id']
            values, coverage = builder.values_with_coverage(a, b, event['event_date'], profiles)
            hinge = 0.
            if coverage[0] and coverage[1]:
                day = date.fromisoformat(event['event_date'])
                age_a = (day-profiles[a].birth_date).days/365.2425
                age_b = (day-profiles[b].birth_date).days/365.2425
                hinge = (max(age_a-32, 0)-max(age_b-32, 0))/5
            target_rows.append(FeatureRow(bout['bout_id'], event_id, event['event_date'], a, b,
                                          v3_features(values, hinge), coverage=coverage))
    if database_hash != file_sha256(history_path):
        raise ValueError('Research training database changed during read-only scoring')
    model_inputs = {
        'algorithm': 'logistic-prior-v3', 'training_history': TRAINING_HISTORY,
        'elo_k': ELO_K, 'training_start_year': TRAINING_START_YEAR, 'l2': .02,
        'feature_names': names, 'history_before_date': before,
        'training_rows_sha256': _digest_json([(r.bout_id,r.event_date,r.features,r.target) for r in training]),
        'calibration_rows_sha256': _digest_json([(r.bout_id,r.event_date,r.features,r.target) for r in calibration]),
        'target_features_sha256': _digest_json([(r.bout_id,r.fighter_a_id,r.fighter_b_id,r.features) for r in target_rows]),
        'weights': model.weights, 'calibration_scale': calibrator.scale,
    }
    version = f"logistic-prior-v3:{TRAINING_HISTORY}:{_digest_json(model_inputs)[:16]}"
    artifact = {**model_inputs, 'model_version': version, 'database_sha256': database_hash,
                'training_history_path': str(history_path), 'research_only': True,
                'promotion_eligible': False, 'history_source_summary': source,
                'training_bouts': len(training), 'training_event_dates': len({r.event_date for r in training}),
                'training_last_date': training[-1].event_date, 'calibration_bouts': len(calibration),
                'calibration_first_date': calibration[0].event_date,
                'calibration_last_date': calibration[-1].event_date,
                'calibration_rule': 'last_20pct_of_earlier_binary_bouts_on_whole_dates_min30',
                'target_static_attribute_rule': 'receipt_fetched_strictly_before_cutoff'}
    coverage_by_bout = {r.bout_id: dict(zip(COVERAGE_NAMES,r.coverage)) for r in target_rows}
    metadata = {}
    for row in target_rows:
        contributions = feature_contributions(row.features, names, model.weights, calibrator.scale)
        metadata[row.bout_id] = {
            'training_history': TRAINING_HISTORY, 'training_history_path': str(history_path),
            'training_history_sha256': database_hash, 'history_before_date': before,
            'training_last_date': artifact['training_last_date'],
            'calibration_last_date': artifact['calibration_last_date'],
            'elo_k': ELO_K, 'training_start_year': TRAINING_START_YEAR,
            'promotion_eligible': False, 'feature_names': names, 'feature_values': row.features,
            'feature_contributions': contributions, 'top_feature_contributions': contributions[:3],
            'coverage_by_fighter': {
                'fighter_a': {'age': row.coverage[0], 'reach': row.coverage[2], 'fight_statistics': False,
                              'prior_ufc_bouts': builder.history.get(row.fighter_a_id).scored_bouts if row.fighter_a_id in builder.history else 0},
                'fighter_b': {'age': row.coverage[1], 'reach': row.coverage[3], 'fight_statistics': False,
                              'prior_ufc_bouts': builder.history.get(row.fighter_b_id).scored_bouts if row.fighter_b_id in builder.history else 0}},
        }
    predictions = {r.bout_id: calibrator.predict_probability(model.predict_probability(r.features)) for r in target_rows}
    return ResearchTrainingRun(model, calibrator, version, predictions, coverage_by_bout, metadata, artifact)
