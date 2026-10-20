"""Initialize and migrate the project to its MySQL-first runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path


DEFAULT_REPORT = 'output/ufc333-20261019-v6-final/prediction-report.json'
DEFAULT_CARD = 'reports/ufc333_card_20261019.json'


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def initialize():
    from .mysql_runtime import ensure_operating_dataset, inspect_dataset
    from .mysql_delivery import ensure_schema
    ensure_schema()
    ensure_operating_dataset('operating')
    operating = inspect_dataset('operating', verify=True)
    return {'backend': 'mysql', 'operating_dataset': operating['resolved_name'],
            'status': 'ready', 'sqlite_fallback': False}


def bootstrap(project_root, *, report_path=DEFAULT_REPORT, card_path=DEFAULT_CARD):
    """One-time imports preserve original files; repeated runs verify and reuse."""
    from .dashboard_forecast import load_saved_forecast
    from .dashboard_history import build_dashboard_history
    from .fight_context import FightContextHistory
    from .forecast_ledger import read_entries
    from .historical_stats import HistoryStore
    from .mysql_delivery import (import_forecast_entries, publish_document,
                                 register_report_forecast)
    from .mysql_runtime import (activate_alias, dataset_exists, import_sqlite_snapshot,
                               inspect_dataset)
    root = Path(project_root).expanduser().resolve()
    result = initialize()
    sources = [
        ('research-complete', 'data/ufc_research_complete_profiles.sqlite', 'research'),
        ('statistics-v6', 'data/ufc_fight_statistics.sqlite', 'statistics'),
    ]
    missing = [name for _, name, _ in sources if not (root / name).is_file()]
    if missing:
        raise ValueError('Retained migration inputs are unavailable: ' + ', '.join(missing)
                         + '. Initialize MySQL and use the native demo or provide explicit import sources.')
    if (root / 'data/ufc332_paper_intake.sqlite').is_file():
        sources.append(('intake-ufc332', 'data/ufc332_paper_intake.sqlite', 'legacy'))
    receipts = {}
    for alias, relative, role in sources:
        source = root / relative
        digest = _sha(source)
        version = alias + '-' + digest[:12]
        receipt = import_sqlite_snapshot(source, dataset=version, role=role, expected_sha256=digest)
        if not dataset_exists(alias):
            activate_alias(alias, version)
        elif inspect_dataset(alias)['source_sha256'] != digest:
            raise ValueError('Existing migration alias identifies different source evidence: ' + alias)
        receipts[alias] = {'target': 'mysql:' + version, 'source_sha256': digest,
                           'content_sha256': receipt['content_sha256'], 'counts': receipt['counts']}
        if _sha(source) != digest:
            raise ValueError('An original retained source changed during migration')
    if not dataset_exists('statistics'):
        activate_alias('statistics', receipts['statistics-v6']['target'])
    report_file, card_file = root / report_path, root / card_path
    saved = load_saved_forecast(report_file, project_root=root)
    if saved['status'] != 'available':
        raise ValueError('Saved report failed release verification')
    report, digest = saved['report'], saved['artifacts']['json']['sha256']
    publish_document('forecast', 'current', report, captured_at_utc=report['generated_at_utc'], source_sha256=digest)
    publish_document('forecast-release', 'current', {'json_path': str(report_file.relative_to(root)),
                     'json_sha256': digest}, captured_at_utc=report['generated_at_utc'], source_sha256=digest)
    evaluation_file = root / 'reports/research_v6_evaluation.json'
    if evaluation_file.is_file():
        evaluation = json.loads(evaluation_file.read_bytes())
        publish_document('evaluation', 'v6', evaluation,
                         captured_at_utc=evaluation.get('created_at_utc', report['generated_at_utc']),
                         source_sha256=_sha(evaluation_file))
    pinned_statistics = receipts['statistics-v6']['target']
    card = json.loads(card_file.read_bytes())
    by_bout = {item['bout_id']: item for item in card['bouts']}
    contexts = {}
    with HistoryStore(pinned_statistics) as store:
        history = FightContextHistory(store, cutoff_at_utc=report['forecast']['cutoff_at_utc'])
        for bout in report['bouts']:
            identity = by_bout[bout['bout_id']]
            for side in ('fighter_a', 'fighter_b'):
                if identity[side]['name'] != bout[side]:
                    raise ValueError('Context identity and forecast differ')
                contexts[bout[side]] = history.fighter_context(identity[side]['fighter_id'],
                                                               target_date=report['event']['event_date'])
    context = {'report_sha256': digest, 'cutoff_at_utc': report['forecast']['cutoff_at_utc'],
               'statistics_dataset': pinned_statistics, 'fighters': contexts}
    publish_document('fight-context', 'current', context, captured_at_utc=report['generated_at_utc'], source_sha256=digest)
    earlier = build_dashboard_history(report_file, card_file, pinned_statistics)
    publish_document('fight-history', 'current', earlier, captured_at_utc=report['generated_at_utc'], source_sha256=digest)
    old_ledger = root / 'reports/forecast_ledger.jsonl'
    if old_ledger.is_file():
        # A prior bootstrap may also have registered newer report freezes.
        from .mysql_delivery import read_forecast_entries
        old = read_entries(old_ledger)
        existing = read_forecast_entries()
        if existing[:len(old)] != old:
            import_forecast_entries(old)
    if saved['event_status'] == 'upcoming':
        register_report_forecast(report_file, card_file, root=root)
    from .mysql_delivery import read_forecast_entries
    entries = read_forecast_entries()
    result.update({'migrated_sources': receipts, 'forecast_ledger_entries': len(entries),
                   'fighter_contexts': len(contexts), 'report_sha256': digest,
                   'original_sources_preserved': True, 'migrated_at_utc': datetime.now(timezone.utc).isoformat()})
    return result


def prepare_ranking_portraits(rankings, *, raw_directory):
    """Reuse verified, roster-bound display evidence for at most seven days."""
    from .mysql_delivery import load_document
    from .ranking_portraits import fetch_portraits, validate_portraits
    from .pipeline import parse_utc
    existing = load_document('rankings-portraits', 'current')
    if existing:
        try:
            validate_portraits(existing, rankings=rankings)
            if datetime.now(timezone.utc) - parse_utc(existing['fetched_at_utc']) <= timedelta(days=7):
                return existing
        except (ValueError, KeyError, TypeError):
            pass
    return fetch_portraits(rankings, raw_directory=raw_directory)


def publish_ranking_portraits(document):
    from .mysql_delivery import publish_document
    return publish_document('rankings-portraits', 'current', document,
                            captured_at_utc=document['fetched_at_utc'], source_sha256=document['snapshot_sha256'])


def refresh_rankings(*, raw_directory='data/raw/rankings'):
    from .mysql_delivery import publish_document
    from .rankings import fetch_rankings
    document = fetch_rankings(raw_directory=raw_directory)
    portraits = prepare_ranking_portraits(document, raw_directory=Path(raw_directory) / 'portraits')
    receipt = publish_document('rankings', 'current', document,
                               captured_at_utc=document['source']['fetched_at_utc'],
                               source_sha256=document['source']['raw_sha256'])
    publish_ranking_portraits(portraits)
    return {'backend': 'mysql', 'status': 'published', 'source': document['source'],
            'portrait_coverage': portraits['coverage'], **receipt}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Initialize native operating and delivery schemas')
    migration = commands.add_parser('bootstrap', help='Verify/import retained local evidence and publish dashboard sections')
    migration.add_argument('--project-root', default=Path.cwd(), type=Path)
    migration.add_argument('--receipt', type=Path)
    ingest = commands.add_parser('import-legacy', help='Explicit one-time read-only SQLite migration')
    ingest.add_argument('source', type=Path); ingest.add_argument('--dataset', required=True)
    ingest.add_argument('--role', choices=['research', 'statistics', 'legacy'], required=True)
    ranks = commands.add_parser('rankings', help='Capture the official rankings and publish to MySQL')
    ranks.add_argument('--raw-directory', default='data/raw/rankings')
    inspect = commands.add_parser('inspect', help='Verify a MySQL dataset and display its counts')
    inspect.add_argument('dataset', nargs='?', default='mysql:statistics')
    args = parser.parse_args(argv)
    if args.command == 'init':
        result = initialize()
    elif args.command == 'bootstrap':
        result = bootstrap(args.project_root)
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            with args.receipt.open('x') as stream:
                json.dump(result, stream, indent=2, allow_nan=False); stream.write('\n')
    elif args.command == 'import-legacy':
        from .mysql_runtime import import_sqlite_snapshot
        result = import_sqlite_snapshot(args.source, dataset=args.dataset, role=args.role)
    elif args.command == 'rankings':
        result = refresh_rankings(raw_directory=args.raw_directory)
    else:
        from .mysql_runtime import inspect_dataset
        result = inspect_dataset(args.dataset, verify=True)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
