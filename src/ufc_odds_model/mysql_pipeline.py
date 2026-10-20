"""Normalize, verify and activate native MySQL statistics versions.

No SQLite staging is used. A failed build or quality check leaves the previous
statistics alias available. Raw captures and human-readable exports remain files.
"""
from __future__ import annotations

from pathlib import Path
import json
import time

from . import historical_stats, pipeline_ops as ops, statistics_workflow


def refresh_pipeline(*, ops_root=ops.DEFAULT_OPS_ROOT, archive=None, commit=None,
                     repository=statistics_workflow.DEFAULT_REPOSITORY,
                     history=None, local_alert=True,
                     webhook_alert=False, min_retained_fraction=.9, refresh_rankings=True):
    from .model_lab import file_sha256
    from .mysql_delivery import load_document, publish_document, activate_statistics_publication
    from .mysql_runtime import inspect_dataset, is_mysql_target
    root = Path(ops_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    journal = ops._Journal(root)
    journal.value['database_backend'] = 'mysql'
    started = time.monotonic()
    try:
        minimum = ops._retained_fraction(min_retained_fraction)
        if history and not is_mysql_target(history):
            raise ValueError('Native refresh requires a MySQL research handle; import retained legacy files first')
        with ops._single_run(root, journal.run_id):
            previous = load_document('pipeline', 'statistics')
            with journal.stage('source') as stage:
                if archive and commit:
                    raise ValueError('Choose an offline archive or an upstream commit')
                manifest_path = (ops._retain_offline_archive(archive, root / 'sources') if archive else
                                 statistics_workflow.refresh_archive(root / 'sources', commit=commit, repository=repository))
                manifest_path, manifest = ops._verify_archive(manifest_path)
                stage.update(source_commit=manifest['repository_commit'], source_manifest_sha256=ops._sha(manifest_path))
            with journal.stage('native_mysql_normalization') as stage:
                inputs = {'source_commit': manifest['repository_commit'],
                          'source_manifest_sha256': ops._sha(manifest_path),
                          'research_database_sha256': file_sha256(history) if history else None,
                          'builder_implementation_sha256': ops._sha(historical_stats.__file__)}
                fingerprint = ops._digest(inputs)
                target = 'mysql:statistics-' + fingerprint[:24]
                built = historical_stats.build_history(manifest_path, target, history)
                with historical_stats.HistoryStore(target) as store:
                    coverage = ops._coverage_summary(store)
                    content_hash = store.metadata['store_content_sha256']
                identity = inspect_dataset(target, verify=True)
                stage.update(dataset=target, content_sha256=identity['content_sha256'], coverage=coverage,
                             reused=built['idempotent_reuse'])
            with journal.stage('quality') as stage:
                quality = ops._quality_report(coverage, previous['coverage'] if previous else None, minimum)
                stage.update(quality)
                if not quality['passed']:
                    raise ValueError('Dataset coverage failed publication quality gates')
            ranking = portraits = None
            if refresh_rankings:
                with journal.stage('rankings_source') as stage:
                    from .rankings import fetch_rankings
                    ranking = fetch_rankings(raw_directory=root / 'rankings')
                    from .mysql_project import prepare_ranking_portraits
                    portraits = prepare_ranking_portraits(ranking, raw_directory=root / 'rankings' / 'portraits')
                    stage.update(raw_sha256=ranking['source']['raw_sha256'],
                                 captured_at_utc=ranking['source']['fetched_at_utc'], portrait_coverage=portraits['coverage'])
            with journal.stage('warehouse_views') as stage:
                from .mysql_warehouse import publish_statistics
                warehouse = publish_statistics(target, run_id='native-' + identity['content_sha256'][:32])
                stage['publication'] = ops._mysql_receipt(warehouse)
            with journal.stage('exports'):
                export = root / 'native-exports' / fingerprint[:24]
                export.mkdir(parents=True, exist_ok=True)
                historical_stats.export_coverage(target, export / 'coverage.json', export / 'coverage.csv')
            publication = {'schema_version': 2, 'database_backend': 'mysql', 'dataset': target,
                           'inputs': inputs, 'input_fingerprint': fingerprint,
                           'coverage': coverage, 'quality': quality,
                           'store_content_sha256': content_hash,
                           'mysql_content_sha256': identity['content_sha256'],
                           'warehouse': ops._mysql_receipt(warehouse),
                           'source_captured_at_utc': manifest['captured_at_utc'],
                           'published_at_utc': ops._utc(), 'research_only': True, 'model_retraining': False}
            with journal.stage('activate') as stage:
                if ranking:
                    publish_document('rankings', 'current', ranking,
                                     captured_at_utc=ranking['source']['fetched_at_utc'],
                                     source_sha256=ranking['source']['raw_sha256'])
                    from .mysql_project import publish_ranking_portraits
                    publish_ranking_portraits(portraits)
                activate_statistics_publication(publication)
                stage['dataset'] = target
            journal.value.update(status='succeeded', exit_code=0, version=target,
                                 coverage=coverage, reused_version=built['idempotent_reuse'])
    except ops.AlreadyRunning:
        journal.value.update(status='skipped', exit_code=3, reason='another_refresh_running')
    except Exception as exc:
        failed_stage = next((s['name'] for s in reversed(journal.value['stages']) if s['status'] == 'failed'), 'configuration')
        journal.value.update(status='failed', exit_code=1, failed_stage=failed_stage,
                             error_type=type(exc).__name__, error_code=failed_stage + '_failed')
    journal.value.update(finished_at_utc=ops._utc(), duration_seconds=round(time.monotonic() - started, 6))
    journal.save()
    if journal.value['status'] == 'failed':
        journal.value['alerts'] = ops.send_failure_alerts(journal.value, local=local_alert, webhook=webhook_alert)
        journal.save()
    return journal.value


def status():
    from .mysql_delivery import load_document
    from .mysql_runtime import inspect_dataset
    identity = inspect_dataset('mysql:statistics', verify=True)
    return {'ready': identity['status'] == 'ready', 'database_backend': 'mysql',
            'dataset': identity['resolved_name'], 'content_sha256': identity['content_sha256'],
            'publication': load_document('pipeline', 'statistics')}
