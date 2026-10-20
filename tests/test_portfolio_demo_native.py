"""The normal demo is an isolated native MySQL dataset, never an active alias."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from ufc_odds_model import mysql_runtime as rt
from ufc_odds_model import portfolio_demo as demo


class NativeDemoContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Explicit legacy fixtures prepare renderer inputs for pure unit tests.
        legacy = self.root / 'legacy'
        demo.run_demo(legacy)
        self.quality = json.loads((legacy / 'reports/quality.json').read_text())
        self.coverage = json.loads((legacy / 'reports/coverage.json').read_text())
        self.sample = json.loads((legacy / 'reports/card-statistics.json').read_text())
        self.output = self.root / 'native'
        _, inputs = demo._fixtures(demo.FIXTURE_DIRECTORY)
        self.target, _ = demo._native_target(inputs, None)
        self.receipt = {'dataset': self.target.removeprefix('mysql:'), 'target': self.target, 'role': 'synthetic',
                        'status': 'ready', 'immutable': 1, 'content_sha256': 'a' * 64, 'schema_sha256': 'b' * 64,
                        'counts': {'bouts': 6}, 'metadata': {'synthetic': True, 'source_manifest_sha256': inputs['fixture_manifest_sha256']}}

    def mocks(self, stack):
        builder = stack.enter_context(patch.object(demo, 'build_history'))
        stack.enter_context(patch.object(demo, '_quality', return_value=self.quality))
        stack.enter_context(patch.object(demo, 'export_coverage', return_value=self.coverage))
        stack.enter_context(patch.object(demo, '_sample', return_value=self.sample))
        stack.enter_context(patch.object(rt, 'inspect_dataset', return_value=self.receipt))
        stack.enter_context(patch('sqlite3.connect', side_effect=AssertionError('Native demo must not stage SQLite')))
        alias = stack.enter_context(patch.object(rt, 'activate_alias', side_effect=AssertionError('No alias activation')))
        warehouse = stack.enter_context(patch('ufc_odds_model.mysql_warehouse.publish_statistics', side_effect=AssertionError('No warehouse activation')))
        return builder, alias, warehouse

    def test_native_creation_and_replay_do_not_stage_sqlite_or_activate_real_data(self):
        with ExitStack() as stack:
            builder, alias, warehouse = self.mocks(stack)
            first = demo.run_native_demo(self.output)
            before = {p.relative_to(self.output): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.rglob('*') if p.is_file()}
            replay = demo.run_native_demo(self.output)
            after = {p.relative_to(self.output): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.rglob('*') if p.is_file()}
            self.assertEqual(builder.call_count, 1)
            self.assertEqual(builder.call_args.args[1], self.target)
            self.assertEqual(builder.call_args.kwargs, {'dataset_role': 'synthetic'})
            alias.assert_not_called(); warehouse.assert_not_called()
        self.assertEqual(first['status'], 'created'); self.assertEqual(replay['status'], 'reused')
        self.assertEqual(first['storage_backend'], 'mysql')
        self.assertFalse(first['namespace_activated']); self.assertFalse(first['sqlite_staging_used'])
        self.assertEqual(before, after)
        self.assertFalse(list(self.output.rglob('*.sqlite')))
        self.assertIn('native MySQL dimensions', (self.output / 'index.html').read_text())
        self.assertNotIn('normalized/statistics.sqlite', (self.output / 'index.html').read_text())

    def test_real_or_aliased_namespace_and_changed_receipts_fail_closed(self):
        for changed in ({'role': 'statistics'}, {'dataset': 'real-statistics'}, {'immutable': 0}, {'metadata': {'synthetic': False}}):
            with self.subTest(changed=changed), patch.object(rt, 'inspect_dataset', return_value={**self.receipt, **changed}):
                with self.assertRaisesRegex(ValueError, 'synthetic namespace'):
                    demo._native_receipt(self.target, self.receipt['metadata']['source_manifest_sha256'])
        for name in ('mysql:operating', 'mysql:statistics', 'mysql:research-complete', 'synthetic-demo-../../real'):
            with self.subTest(name=name), patch.object(rt, 'connect_server') as server:
                with self.assertRaisesRegex(ValueError, 'namespaces'):
                    demo.run_native_demo(self.output, dataset=name)
                server.assert_not_called()

    def test_default_cli_chooses_native_and_legacy_mode_is_explicit(self):
        with patch.object(demo, 'run_native_demo', return_value={'status': 'created'}) as native, patch.object(demo, 'run_demo') as legacy:
            with patch('builtins.print'):
                self.assertEqual(demo.main([]), 0)
            self.assertEqual(native.call_args.args[0], demo.DEFAULT_NATIVE_OUTPUT)
            legacy.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'cannot activate'):
            demo.run_demo(self.root / 'unsafe', mysql=True)


@unittest.skipUnless(os.environ.get('UFC_MYSQL_INTEGRATION') == '1', 'Explicit native MySQL integration opt-in required')
class NativeDemoIntegrationTests(unittest.TestCase):
    def test_real_native_demo_replay_keeps_real_aliases_and_never_opens_sqlite(self):
        dataset = 'synthetic-demo-ci-' + uuid.uuid4().hex[:20]
        raw = rt.connect_server()
        try:
            with raw.cursor() as cursor:
                rt._catalog(raw)
                cursor.execute('SELECT alias,target FROM ufc_runtime_aliases ORDER BY alias')
                before = cursor.fetchall()
            with tempfile.TemporaryDirectory() as temporary, patch('sqlite3.connect', side_effect=AssertionError('Native demo opened SQLite')):
                output = Path(temporary) / 'native'
                first = demo.run_native_demo(output, dataset=dataset)
                replay = demo.run_native_demo(output, dataset=dataset)
                self.assertEqual(first['quality_counts']['usable_bouts'], 5)
                self.assertEqual(first['quality_counts']['held_bouts'], 1)
                self.assertEqual(first['mysql']['role'], 'synthetic')
                self.assertEqual(first['mysql']['content_sha256'], replay['mysql']['content_sha256'])
                self.assertEqual(replay['status'], 'reused')
                self.assertFalse(list(output.rglob('*.sqlite')))
                with rt.connect_dataset('mysql:' + dataset, readonly=True) as connection:
                    self.assertIsNone(connection.execute("SELECT reach_cm FROM fighters WHERE fighter_id='ufcstats:fighter:ffffffffffffff03'").fetchone()[0])
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM fighter_bout_facts WHERE bout_id='ufcstats:fight:ffffffffffffff04'").fetchone()[0], 0)
            raw.commit()
            with raw.cursor() as cursor:
                cursor.execute('SELECT alias,target FROM ufc_runtime_aliases ORDER BY alias')
                self.assertEqual(cursor.fetchall(), before)
        finally:
            # This test cleans up only its fresh UUID namespace. It never
            # removes a pre-existing publication or changes a real-data alias.
            with raw.cursor() as cursor:
                cursor.execute('SELECT physical_name FROM ufc_runtime_tables WHERE dataset=%s', (dataset,))
                tables = [row[0] for row in cursor.fetchall()]
                cursor.execute('SET SESSION foreign_key_checks=0')
                try:
                    for table in tables:
                        cursor.execute('DROP TABLE ' + rt._identifier(table))
                finally:
                    cursor.execute('SET SESSION foreign_key_checks=1')
                cursor.execute('DELETE FROM ufc_runtime_tables WHERE dataset=%s', (dataset,))
                cursor.execute('DELETE FROM ufc_runtime_datasets WHERE dataset=%s', (dataset,))
            raw.commit(); raw.close()


if __name__ == '__main__':
    unittest.main()
