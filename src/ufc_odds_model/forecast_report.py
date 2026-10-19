"""Freeze a shareable research forecast from a retained, source-listed card.

This command fits only outside the dashboard. It never writes to the research
database, approves an operating card, requests odds, or creates a wager.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .model_lab import file_sha256, read_only
from .pipeline import parse_utc, utc_string
from .prediction_report import render_csv, render_html, render_json, validate_report
from .research_training import prepare_research_logistic
from .research_upgrade import TrainingPolicy, load_selection, load_verified_report
from .research_stats import load_research_stats, load_stats_selection, load_verified_stats_report
from .athlete_context import (load_athlete_context, attach_reach_observations, measurements,
                              verify_retained_receipt)


def verify_card_sources(card: dict, root: Path, cutoff: datetime) -> None:
    """Check the actual retained bytes and capture order, not a saved badge."""
    sources = card.get('sources')
    if not isinstance(sources, dict) or not {'official_event', 'official_start_guide'} <= sources.keys():
        raise ValueError('The card requires retained official event and start-time sources')
    raw_root = (root / 'data/raw').resolve()
    for name, source in sources.items():
        if source.get('http_status') != 200 or parse_utc(source['fetched_at_utc']) > cutoff:
            raise ValueError(f'Source {name} was unavailable or captured after the forecast cutoff')
        for path_key, hash_key in (('response_path', 'response_sha256'),
                                   ('receipt_path', 'receipt_sha256'),
                                   ('headers_path', 'headers_sha256')):
            path = root / source[path_key]
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(raw_root):
                raise ValueError(f'Source {name} is outside the retained evidence directory or missing')
            if file_sha256(path) != source[hash_key]:
                raise ValueError(f'Source {name} has changed since capture')
        verify_retained_receipt(source, root, cutoff)
    if card.get('source_url') and card['source_url'] != sources['official_event']['request_url']:
        raise ValueError('The public card URL must match its retained official event receipt')


def _unscored_reason(bout: dict) -> str:
    missing = [fighter['name'] for fighter in (bout['fighter_a'], bout['fighter_b'])
               if not fighter.get('fighter_id')]
    return ('Stable research identity not established for ' + ' and '.join(missing)
            + '. Name-only or provisional history was not merged.')


def build_report(card_path: str | Path, history_path: str | Path,
                 selection_path: str | Path, evaluation_path: str | Path,
                 *, root: str | Path, as_of: datetime | None = None,
                 now: datetime | None = None, stats_archive: str | Path | None = None,
                 athlete_facts: str | Path | None = None,
                 statistics_db: str | Path | None = None,
                 require_complete_statistics: bool = False) -> tuple[dict, dict]:
    """Return a public report and private lineage. Execute from ``root``."""
    root = Path(root).resolve()
    if Path.cwd().resolve() != root:
        raise ValueError('Generate reports from the project root so retained evidence resolves consistently')
    now = now or datetime.now(timezone.utc)
    as_of = as_of or now
    if (as_of.tzinfo is None or now.tzinfo is None or as_of > now):
        raise ValueError('The forecast cutoff must be timezone-aware and cannot be in the future')
    cutoff = utc_string(as_of)
    card_path, history_path = Path(card_path), Path(history_path)
    card_bytes = card_path.read_bytes()
    card = json.loads(card_bytes)
    verify_card_sources(card, root, as_of)
    event = card['event']
    if parse_utc(event['event_start_utc']) <= as_of:
        raise ValueError('A new prediction report requires an unstarted future event')
    if card['database']['sha256'] != file_sha256(history_path):
        raise ValueError('The identity mapping database has changed')
    if statistics_db is not None:
        if stats_archive is None:
            raise ValueError('All-card statistics require their retained archive manifest')
        from .research_v6 import load_v6_selection, load_verified_v6_report
        selection = load_v6_selection(selection_path, history_path, stats_archive, statistics_db)
        evaluation = load_verified_v6_report(evaluation_path, selection_path, history_path,
                                             stats_archive, statistics_db)
    elif stats_archive is None:
        selection = load_selection(selection_path, history_path)
        evaluation = load_verified_report(evaluation_path, selection_path, history_path)
    else:
        selection = load_stats_selection(selection_path, history_path, stats_archive)
        evaluation = load_verified_stats_report(evaluation_path, selection_path, history_path, stats_archive)
    if parse_utc(evaluation['created_at_utc']) > as_of:
        raise ValueError('Model evaluation must precede the forecast cutoff')
    champion = evaluation['research_champion']
    use_v5 = champion == 'logistic-prior-v5'
    use_v6 = champion == 'logistic-prior-v6'
    use_stats = use_v5 or use_v6
    use_policy = champion in {'logistic-prior-v4', 'logistic-prior-v5', 'logistic-prior-v6'}
    policy = TrainingPolicy(**selection['selected_policy']) if use_policy else None
    athlete_context = load_athlete_context(athlete_facts, root, as_of) if athlete_facts else {}
    overlay = None
    eligible, withheld = [], []
    seen = set()
    with read_only(history_path) as history:
        if use_stats:
            overlay = load_research_stats(history, stats_archive)
        for bout in card['bouts']:
            a, b = bout['fighter_a'], bout['fighter_b']
            if not a.get('fighter_id') or not b.get('fighter_id'):
                withheld.append({'fighter_a': a['name'], 'fighter_b': b['name'],
                                 'weight_class': bout['weight_class'], 'reason': _unscored_reason(bout)})
                continue
            for fighter in (a, b):
                identity = fighter['fighter_id']
                row = history.execute('SELECT canonical_name FROM fighters WHERE fighter_id=?',
                                      (identity,)).fetchone()
                if (not fighter['mapping_status'].startswith('established_id_') or row is None
                        or row['canonical_name'] != fighter['canonical_name'] or identity in seen):
                    raise ValueError('A stable, unambiguous identity is required for every scored participant')
                seen.add(identity)
            eligible.append(bout)
        last_result = history.execute('SELECT MAX(e.event_date) FROM results r JOIN bouts b '
                                      'ON b.bout_id=r.bout_id JOIN events e ON e.event_id=b.event_id '
                                      'WHERE e.event_date < ?', (as_of.date().isoformat(),)).fetchone()[0]
    if not eligible:
        raise ValueError('No source-listed matchup has two established research identities')
    if athlete_facts and any(f['fighter_id'] not in athlete_context
                             for b in eligible for f in (b['fighter_a'], b['fighter_b'])):
        raise ValueError('Every scored fighter requires verified official reach and statistics context')
    if athlete_facts and any(
            athlete_context[f['fighter_id']]['canonical_name'] != f['canonical_name'] or
            athlete_context[f['fighter_id']]['official_profile_url'] != f.get('official_profile_url')
            for b in eligible for f in (b['fighter_a'], b['fighter_b'])):
        raise ValueError('Official athlete facts must match the card identity and profile URL')
    core_stats = ('significant_strikes_landed_per_minute', 'significant_strikes_absorbed_per_minute',
                  'significant_strike_accuracy_display_pct', 'significant_strike_defense_pct',
                  'takedowns_average_per_15_minutes')
    if athlete_facts and any(athlete_context[f['fighter_id']]['stats'].get(key) is None
                            for b in eligible for f in (b['fighter_a'], b['fighter_b']) for key in core_stats):
        raise ValueError('Core official fight statistics are required for every scored fighter')
    # A transient card supplies target rows, never approved operating evidence.
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.row_factory = sqlite3.Row
        db.init_db(connection)
        db.upsert_event(connection, event['event_id'], event['name'], event['event_date'],
                        'scheduled', source='research_report_card',
                        start_time_utc=event['event_start_utc'])
        for bout in eligible:
            for fighter in (bout['fighter_a'], bout['fighter_b']):
                db.upsert_fighter(connection, fighter['fighter_id'], fighter['canonical_name'])
            db.upsert_bout(connection, bout['bout_id'], event['event_id'],
                           bout['fighter_a']['fighter_id'], bout['fighter_b']['fighter_id'],
                           'scheduled', weight_class=bout['weight_class'],
                           scheduled_rounds=bout['scheduled_rounds'], source='research_report_card')
        connection.commit()
        if use_stats and athlete_context:
            attach_reach_observations(connection, athlete_context, root)
        if use_v6:
            from .research_v6 import load_rich_overlay, prepare_v6_research
            run = prepare_v6_research(connection, event['event_id'], as_of, history_path,
                                      overlay, load_rich_overlay(statistics_db), selection)
        else:
            run = prepare_research_logistic(
                connection, event['event_id'], as_of, history_path, model_policy=policy,
                selection_manifest_sha256=selection['manifest_sha256'] if use_policy else None,
                stats_overlay=overlay)
    forecasts = []
    for bout in eligible:
        p = run.predictions[bout['bout_id']]
        meta = run.metadata_by_bout[bout['bout_id']]
        if require_complete_statistics and use_stats and any(not c['reach'] or not c['fight_statistics']
                          for c in meta['coverage_by_fighter'].values()):
            raise ValueError('This statistics edition requires actual reach and earlier-fight inputs for every scored fighter')
        forecasts.append({'bout_id': bout['bout_id'],
                          'fighter_a': bout['fighter_a']['name'], 'fighter_b': bout['fighter_b']['name'],
                          'p_fighter_a': p, 'p_fighter_b': 1 - p,
                          'weight_class': bout['weight_class'], 'scheduled_rounds': bout['scheduled_rounds'],
                          'contributions': [c for c in meta['feature_contributions']
                                            if abs(c['logit_contribution']) > 1e-10][:3],
                          'coverage': meta['coverage_by_fighter'],
                          'measurements': {side: measurements(
                              athlete_context[bout[side]['fighter_id']],
                              reach_used_by_model=use_stats and meta['coverage_by_fighter'][side]['reach'])
                              for side in ('fighter_a', 'fighter_b')
                              if bout[side]['fighter_id'] in athlete_context},
                          'notes': ['Model factors are associations in log-odds units, not causal explanations.']})
    fixed, rolling = evaluation['fixed_test'], evaluation['rolling_origin']
    selected_candidate = champion == evaluation['candidate_model_version']
    label = 'candidate' if selected_candidate else 'baseline'
    last_fold = rolling['candidate_folds' if selected_candidate else 'baseline_folds'][-1]
    metrics = fixed[label]['metrics']
    paired = fixed['paired_uncertainty']['candidate_minus_baseline_log_loss_95pct_interval']
    benchmark_dates = [row['event_date'] for row in fixed['predictions']]
    source_urls = [(name.replace('_', ' ').title(), source.get('revision_url', source['request_url']))
                   for name, source in card['sources'].items()]
    document = {
        'schema_version': 1,
        'event': {'name': event['name'], 'event_date': event['event_date'],
                  'start_time_utc': event['event_start_utc'], 'source_url': card['source_url']},
        'generated_at_utc': utc_string(now),
        'forecast': {'model_version': run.model_version,
                     'model_label': ('V6 - reach and last-five-fight striking/grappling' if use_v6 and
                                     selection['selected_candidate_id'] == 'balanced_recent5' else
                                     'V6 - history, reach and detailed earlier-fight statistics' if use_v6 else
                                     'V5 - history, age, reach and earlier-fight statistics' if use_v5 else
                                     'V4 - calibrated UFC history and age' if use_policy else 'V3 research baseline'),
                     'cutoff_at_utc': cutoff, 'research_only': True,
                     'status': 'prospective_research' if now - as_of <= timedelta(minutes=5)
                     else 'research_retrospective'},
        'bouts': forecasts, 'unscored_bouts': withheld,
        'evaluation': {'label': 'Reused historical benchmark', **metrics,
                       'period': f'{min(benchmark_dates)} to {max(benchmark_dates)}',
                       'baseline_label': ('V5' if statistics_db else 'V4' if stats_archive else 'V3'),
                       'baseline_log_loss': fixed['baseline']['metrics']['log_loss'],
                       'baseline_accuracy': fixed['baseline']['metrics']['accuracy'],
                       'paired_log_loss_interval': paired,
                       'rolling': {**rolling[label]['metrics'],
                                   'baseline_log_loss': rolling['baseline']['metrics']['log_loss']},
                       'recent_year': {'year': last_fold['year'], **last_fold['test']['metrics']},
                       'note': 'The fixed benchmark was reported previously and is not a fresh holdout. '
                               'The 2016-2026 rolling summary includes the 2016-2023 development years used '
                               'to select settings. Bootstrap intervals exclude data and selection uncertainty.'},
        'source_notes': [f'Official card captured {card["sources"]["official_event"]["fetched_at_utc"]}. '
                         f'{len(forecasts)} of {len(card["bouts"])} listed matchups scored.',
                         f'Research result history ends {last_result}; later event results are not included in result-history features.',
                         f'Earliest card start: {event["event_start_utc"]}; '
                         f'main card: {event.get("main_card_start_utc", "not supplied")}.',
                         'Current card and identity links were checked by software; human roster and identity review remains pending.',
                         'No bookmaker API was queried. Quoted odds on public event pages were excluded because '
                         'they have no verified bookmaker capture/update times.'],
        'limitations': [
            'These are forecasts for an unstarted event using retrospectively assembled research history; '
            'they do not establish operating readiness or a wagering edge.',
            f'{len(withheld)} matchups are not scored because stable research identities are unresolved. '
            'Provisional identities were not silently merged.',
            ('Reach and earlier-fight statistics enter this model. Archive reach is treated as an approximate '
             'static adult attribute in historical replay; historical measurement dates are unknown. Current career '
             'averages are descriptive only. Injuries, camps, non-UFC records and later results are not modeled.'
             if use_stats else 'Reach and current fight statistics are shown as context when available; they do not '
             'enter this history-and-age model. Injuries, camps, non-UFC records and later results are not modeled.'),
            'Probabilities describe model estimates, not certainty. A 60% favorite can lose four times in ten '
            'even under perfect calibration. Results vary by year; the latest yearly benchmark covers a partial year.',
            'The card can change before fight night. This frozen edition does not update automatically.',
        ],
        'sources': [{'label': label, 'url': url} for label, url in source_urls],
    }
    if not selected_candidate:
        document['evaluation'].pop('paired_log_loss_interval')
    if use_v5 and statistics_db is None:
        complete = fixed['coverage_cohorts']['both_reach_and_prior_paired_stats']
        document['evaluation']['note'] += (
            ' The V5 loss gain is small and its paired interval includes zero; fixed-benchmark '
            'winner accuracy is one correct pick lower than V4. This does not establish a reliable improvement.')
        document['source_notes'].append(
            f'Historical archive supplies {len(overlay.stat_sources):,} exactly matched paired-stat bouts. '
            f'Both-fighter reach and earlier-stat coverage in the benchmark: {complete["bouts"]:,}/'
            f'{metrics["bouts"]:,} ({complete["share"]:.1%}). Missing historical evidence remains neutral; '
            f'{sum(c["reach"] and c["fight_statistics"] for b in forecasts for c in b["coverage"].values())}'
            f'/{len(forecasts) * 2} scored fighters have both inputs.')
        document['sources'].append({'label': 'Version-pinned UFCStats research archive',
                                    'url': overlay.source_summary['repository_url'] + '/tree/' +
                                           overlay.source_summary['repository_commit']})
    if athlete_context:
        document['source_notes'].append(
            'Current official UFC profile rates are shown for context and never replace earlier-fight model inputs. '
            'Official numeric reach is interpreted as inches and converted by 2.54. ' +
            ('Dated official reach supplies these forecasts.' if use_stats else
             'Displayed official reach is context only for this forecast.'))
        document['source_notes'].append(
            'Takedown accuracy is omitted because official profile panels contain inconsistent or incomplete '
            'counts for several fighters. Other published current rates are displayed as source-reported context.')
        for fighter in athlete_context.values():
            document['sources'].append({'label': fighter['name'] + ' - official athlete profile',
                                        'url': fighter['official_profile_url']})
    if statistics_db is not None:
        document['source_notes'].append(
            ('The all-card statistics store supplies strictly earlier fight records, including striking, '
            'takedowns, control, knockdowns and submission attempts. Same-date records are excluded, '
            'with a two-day timing embargo before the target date. Optional missing history is disclosed in each matchup.'
             if use_v6 else 'The richer all-card candidate did not satisfy its model selection rule. '
             'This forecast retains the evaluated V5 model; the full statistics store remains available '
             'for per-card comparisons.'))
        if use_v6 and selection['selected_candidate_id'] == 'balanced_recent5':
            document['source_notes'].append(
                'Selected rich metrics use the last five available fights, keeping whole-date ties. '
                'The original V5 history features still use all earlier eligible history. '
                'Selected rich fight counts and minutes are shown separately from that original history.')
        document['sources'].append({'label': 'Version-pinned historical statistics archive',
                                    'url': overlay.source_summary['repository_url'] + '/tree/' +
                                           overlay.source_summary['repository_commit']})
        document['evaluation']['note'] += (
            ' The feature candidate was selected on development years before this fixed evaluation. '
            'A loss interval crossing zero does not establish a reliable gain.')
        if selected_candidate:
            baseline_recent = rolling['baseline_folds'][-1]['test']['metrics']
            if last_fold['test']['metrics']['log_loss'] > baseline_recent['log_loss']:
                document['evaluation']['note'] += (
                    f' In {last_fold["year"]}, probability log loss worsens from '
                    f'{baseline_recent["log_loss"]:.5f} to {last_fold["test"]["metrics"]["log_loss"]:.5f}; '
                    'the pooled gain does not establish improvement in the latest year.')
    public = validate_report(document)
    public['forecast']['forecast_id'] = hashlib.sha256(json.dumps(
        public, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    training_artifact = dict(run.artifact)
    training_artifact['training_history_path'] = str(history_path.resolve().relative_to(root))
    lineage = {'schema_version': 1, 'forecast_id': public['forecast']['forecast_id'],
               'cutoff_at_utc': cutoff, 'research_only': True, 'promotion_eligible': False,
               'card_manifest_sha256': hashlib.sha256(card_bytes).hexdigest(),
               'selection_manifest_sha256': selection['manifest_sha256'],
               'evaluation_report_content_sha256': evaluation['report_content_sha256'],
               'athlete_facts_sha256': file_sha256(athlete_facts) if athlete_facts else None,
               'statistics_archive_manifest_sha256': file_sha256(stats_archive) if stats_archive else None,
               'statistics_database_sha256': file_sha256(statistics_db) if statistics_db else None,
               'training_artifact': training_artifact,
               'stable_bout_identities': [{'bout_id': b['bout_id'],
                                           'fighter_a_id': b['fighter_a']['fighter_id'],
                                           'fighter_b_id': b['fighter_b']['fighter_id']} for b in eligible]}
    return public, lineage


def write_bundle(report: dict, lineage: dict, output: str | Path) -> Path:
    """Create an immutable edition, with hashes for all three public formats."""
    output = Path(output)
    if output.exists():
        raise FileExistsError('Choose a new output directory; saved forecasts are not overwritten')
    files = {'prediction-report.json': render_json(report).encode(),
             'prediction-report.html': render_html(report).encode(),
             'prediction-report.csv': render_csv(report).encode()}
    receipt = {**lineage, 'outputs': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
    files['forecast-receipt.json'] = (json.dumps(receipt, indent=2, ensure_ascii=False) + '\n').encode()
    output.mkdir(parents=True, exist_ok=False)
    for name, value in files.items():
        with (output / name).open('xb') as stream:
            stream.write(value)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    parser.add_argument('--card', required=True, type=Path)
    parser.add_argument('--history', type=Path, default=Path('data/ufc_research_complete_profiles.sqlite'))
    parser.add_argument('--selection', type=Path)
    parser.add_argument('--evaluation', type=Path)
    parser.add_argument('--stats-archive', type=Path,
                        default=Path('data/raw/ufcstats-archive-20261019/manifest.json'))
    parser.add_argument('--athlete-facts', type=Path,
                        help='Optional verified current official facts for this card; historical statistics are automatic')
    parser.add_argument('--statistics-db', type=Path,
                        help='Use the evaluated all-card statistics model from this store')
    parser.add_argument('--require-complete-statistics', action='store_true',
                        help='Reject a card if a scored fighter lacks reach or earlier strike counts')
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    os.chdir(args.project_root)
    if args.output_dir.exists():
        parser.error('Output directory already exists; choose a new frozen edition')
    version = 'v6' if args.statistics_db else 'v5'
    selection = args.selection or Path(f'reports/research_{version}_selection.json')
    evaluation = args.evaluation or Path(f'reports/research_{version}_evaluation.json')
    report, lineage = build_report(args.card, args.history, selection, evaluation,
                                   root=Path.cwd(), stats_archive=args.stats_archive,
                                   athlete_facts=args.athlete_facts, statistics_db=args.statistics_db,
                                   require_complete_statistics=args.require_complete_statistics)
    result = write_bundle(report, lineage, args.output_dir)
    print(f'Frozen {len(report["bouts"])} forecasts; {len(report.get("unscored_bouts", []))} unscored matchups: {result}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
