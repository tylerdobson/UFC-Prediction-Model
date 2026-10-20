"""Reusable archive refresh, historical backfill, card reports and evaluation.

Only CSV/text source artifacts are downloaded. Each refresh resolves a Git
commit first and writes a new, immutable capture directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

DEFAULT_REPOSITORY = 'Greco1899/scrape_ufc_stats'
REQUIRED_COLUMNS = {
    'ufc_event_details.csv': {'EVENT', 'URL', 'DATE'},
    'ufc_fight_results.csv': {'EVENT', 'BOUT', 'OUTCOME', 'ROUND', 'TIME', 'TIME FORMAT', 'URL'},
    'ufc_fight_stats.csv': {'EVENT', 'BOUT', 'ROUND', 'FIGHTER', 'KD', 'SIG.STR.', 'TOTAL STR.', 'TD', 'SUB.ATT', 'REV.', 'CTRL'},
    'ufc_fighter_tott.csv': {'FIGHTER', 'REACH', 'URL'},
    'ufc_fighter_details.csv': {'FIRST', 'LAST', 'URL'},
}


def _download(url: str) -> tuple[bytes, dict]:
    request = Request(url, headers={'User-Agent': 'UFCForecastResearch/0.2', 'Accept': 'application/vnd.github+json' if 'api.github.com/' in url else '*/*'})
    with urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise ValueError(f'Archive request failed: HTTP {response.status}')
        body = response.read(30_000_001)
        if len(body) > 30_000_000:
            raise ValueError('Archive artifact exceeds the 30 MB per-file limit')
        return body, {'request_url': url, 'response_url': response.url,
                      'http_status': response.status, 'headers': str(response.headers),
                      'fetched_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}


def refresh_archive(raw_root: str | Path, *, commit: str | None = None,
                    repository: str = DEFAULT_REPOSITORY) -> Path:
    """Resolve latest or an explicit commit, then retain a verified new capture.

    This never mutates an older capture or executes downloaded repository code.
    A repeated explicit commit reuses only a fully verified existing directory.
    """
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository must be an owner/name pair')
    if commit is not None and not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
        raise ValueError('Pin a full 40-character Git commit SHA')
    raw_root = Path(raw_root)
    raw_root.mkdir(parents=True, exist_ok=True)
    commit_bytes, commit_receipt = _download(f'https://api.github.com/repos/{repository}/commits/{commit or "HEAD"}')
    details = json.loads(commit_bytes)
    resolved = details.get('sha', '').lower()
    if not re.fullmatch(r'[0-9a-f]{40}', resolved) or commit and resolved != commit.lower():
        raise ValueError('Repository did not resolve to the requested immutable commit')
    target = raw_root / f'ufcstats-{resolved}'
    if target.exists():
        manifest = json.loads((target / 'manifest.json').read_text())
        if manifest.get('repository_commit') != resolved or manifest.get('repository_url') != f'https://github.com/{repository}':
            raise ValueError('Existing archive directory belongs to different source inputs')
        entries = {entry['file']: entry for entry in manifest['files']}
        if not set(REQUIRED_COLUMNS) | {'LICENSE', 'README.md'} <= entries.keys():
            raise ValueError('Existing archive is incomplete')
        for name, entry in entries.items():
            path = target / name
            if Path(name).name != name or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError('Existing archive source bytes have changed')
        return target / 'manifest.json'
    temporary = Path(tempfile.mkdtemp(prefix='.ufcstats-capture-', dir=raw_root))
    try:
        (temporary / 'repo-commit.json').write_bytes(commit_bytes)
        (temporary / 'repo-commit.receipt.json').write_text(json.dumps(commit_receipt, indent=2) + '\n')
        entries = []
        for name in [*REQUIRED_COLUMNS, 'LICENSE', 'README.md']:
            url = f'https://raw.githubusercontent.com/{repository}/{resolved}/{name}'
            body, receipt = _download(url)
            if name in REQUIRED_COLUMNS:
                text = body.decode('utf-8-sig')
                columns = next(csv.reader(io.StringIO(text)), [])
                if not REQUIRED_COLUMNS[name] <= set(columns):
                    raise ValueError(f'Unexpected archive schema: {name}')
            (temporary / name).write_bytes(body)
            (temporary / (name + '.headers')).write_text(receipt.pop('headers'))
            receipt.update({'response_sha256': hashlib.sha256(body).hexdigest(), 'response_bytes': len(body)})
            (temporary / (name + '.receipt.json')).write_text(json.dumps(receipt, indent=2) + '\n')
            entries.append({'file': name, 'source_url': url, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)})
        manifest = {
            'schema_version': 1, 'source': repository, 'upstream_source': 'UFCStats',
            'access_mode': 'direct_public_https_version_pinned_csv',
            'repository_url': f'https://github.com/{repository}', 'repository_commit': resolved,
            'repository_commit_date': details['commit']['committer']['date'],
            'captured_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'publisher_license': 'See retained LICENSE; upstream rights are separate',
            'historical_availability': 'retrospective_capture_not_historical_prefight_observation',
            'files': entries,
        }
        (temporary / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        temporary.rename(target)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return target / 'manifest.json'


def _refresh_main(argv) -> int:
    parser = argparse.ArgumentParser(description='Capture a new immutable UFCStats CSV archive for later backfill.')
    parser.add_argument('--raw-root', type=Path, default=Path('data/raw/statistics-refresh'))
    parser.add_argument('--commit', help='Optional full Git commit SHA; otherwise resolve current HEAD once')
    parser.add_argument('--repository', default=DEFAULT_REPOSITORY)
    args = parser.parse_args(argv)
    print(refresh_archive(args.raw_root, commit=args.commit, repository=args.repository))
    return 0


def _inspect_main(command, argv) -> int:
    from .historical_stats import HistoryStore
    parser = argparse.ArgumentParser(description='Inspect the retained all-card statistics store.')
    parser.add_argument('--stats-db', default='mysql:statistics')
    if command in {'events', 'fighters'}:
        parser.add_argument('--query', default='', help='Filter event name or ID')
        if command == 'events':
            parser.add_argument('--from-date')
            parser.add_argument('--to-date')
        parser.add_argument('--limit', type=int, default=25)
    else:
        parser.add_argument('--fighter-id', required=True, help='Canonical UFCStats or safely linked research ID')
        parser.add_argument('--before-date', default=datetime.now(timezone.utc).date().isoformat(),
                            help='Exclusive event-date cutoff, YYYY-MM-DD')
    args = parser.parse_args(argv)
    with HistoryStore(args.stats_db) as store:
        if command in {'events', 'fighters'}:
            if args.limit < 1:
                parser.error('--limit must be positive')
            for value in (getattr(args, 'from_date', None), getattr(args, 'to_date', None)):
                if value:
                    date.fromisoformat(value)
            rows = [r for r in (store.event_coverage() if command == 'events' else store.fighter_coverage())
                    if (not args.query or args.query.casefold() in json.dumps(r).casefold())
                    and (not getattr(args, 'from_date', None) or r['event_date'] >= args.from_date)
                    and (not getattr(args, 'to_date', None) or r['event_date'] <= args.to_date)]
            if command == 'events':
                rows.sort(key=lambda r: (r['event_date'], r['event_id']), reverse=True)
            else:
                rows.sort(key=lambda r: (r['name'], r['fighter_id']))
            result = rows[:args.limit]
        else:
            date.fromisoformat(args.before_date)
            profile = store.fighter_profile(args.fighter_id)
            if profile is None:
                raise ValueError('Unknown or unresolved fighter identity in the statistics store')
            result = {'fighter': profile, 'before_date': args.before_date,
                      'statistics': store.fighter_aggregate(args.fighter_id, args.before_date),
                      'historical_source_availability': 'retrospective_capture'}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _dispatch(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ('refresh', 'build', 'events', 'fighters', 'fighter', 'card', 'protocol', 'select', 'evaluate', 'verify')
    if not argv or argv[0] not in commands:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('command', choices=commands,
                            help='refresh source; build all-card store; report card; select/evaluate/verify model')
        parser.parse_args(argv)
        return 0
    command, remaining = argv[0], argv[1:]
    if command == 'refresh':
        return _refresh_main(remaining)
    if command in {'events', 'fighters', 'fighter'}:
        return _inspect_main(command, remaining)
    if command == 'build':
        from .historical_stats import main as build_main
        return build_main(remaining) or 0
    if command == 'card':
        from .card_statistics import main as card_main
        return card_main(remaining) or 0
    # Keep hash-pinned research implementations intact while choosing MySQL
    # defaults at the command boundary.
    if '--db' not in remaining:
        remaining += ['--db', 'mysql:research-complete']
    if '--statistics-db' not in remaining:
        remaining += ['--statistics-db', 'mysql:statistics']
    if '--archive' not in remaining:
        remaining += ['--archive', 'data/raw/ufcstats-archive-20261019/manifest.json']
    from .research_v6 import main as model_main
    return model_main([command, *remaining]) or 0


def main(argv=None) -> int:
    try:
        return _dispatch(argv)
    except (ValueError, OSError, sqlite3.Error, URLError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
