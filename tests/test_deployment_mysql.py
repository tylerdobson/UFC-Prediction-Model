"""Native startup checks are read-only and validate mounted evidence bytes."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ufc_odds_model import deployment
from ufc_odds_model.mysql_runtime import RuntimeDatabaseError


class Rows:
    def __init__(self, values): self.values = values
    def __iter__(self): return iter(self.values)
    def fetchall(self): return self.values


class Operating:
    def __init__(self, receipts): self.receipts = receipts; self.closed = False; self.rolled_back = False
    def execute(self, sql):
        if sql == 'PRAGMA foreign_key_check': return Rows([])
        if sql == 'SELECT payload_path,sha256 FROM ingestion_runs': return Rows(self.receipts)
        raise AssertionError('Unexpected or mutating startup SQL: ' + sql)
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


class NativeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data/raw').mkdir(parents=True); (self.root / 'reports').mkdir()
        self.payload = self.root / 'data/raw/source.json'; self.payload.write_bytes(b'native retained source')
        self.connection = Operating([{'payload_path': str(self.payload), 'sha256': hashlib.sha256(self.payload.read_bytes()).hexdigest()}])
        self.receipt = {'role': 'operating', 'immutable': 0, 'dataset': 'operating-version'}

    def check(self):
        with patch('ufc_odds_model.mysql_runtime.inspect_dataset', return_value=self.receipt), patch('ufc_odds_model.mysql_runtime.connect_dataset', return_value=self.connection) as connect, patch.object(deployment, 'pending_migrations', return_value=[]), patch('sqlite3.connect', side_effect=AssertionError('Native startup opened SQLite')):
            result = deployment.check_dashboard_deployment(self.root)
        connect.assert_called_once_with('mysql:operating', readonly=True)
        return result

    def test_default_startup_checks_native_schema_and_source_bytes_without_writes(self):
        report = self.check()
        self.assertEqual(report['storage_backend'], 'mysql')
        self.assertEqual(report['verified_receipts'], 1)
        self.assertFalse(report['sqlite_fallback_used'])
        self.assertTrue(self.connection.closed); self.assertTrue(self.connection.rolled_back)

    def test_tampered_outside_and_synthetic_inputs_fail_closed(self):
        self.payload.write_bytes(b'tampered')
        with self.assertRaisesRegex(deployment.DeploymentError, 'hash does not match'):
            self.check()
        self.connection.receipts[0]['payload_path'] = str(self.root / 'outside.json')
        with self.assertRaisesRegex(deployment.DeploymentError, 'outside deployment data'):
            self.check()
        self.receipt['role'] = 'synthetic'
        with self.assertRaisesRegex(deployment.DeploymentError, 'operating MySQL'):
            self.check()

    def test_missing_mysql_has_no_fallback(self):
        with patch('ufc_odds_model.mysql_runtime.inspect_dataset', side_effect=RuntimeDatabaseError('unavailable')), patch('sqlite3.connect', side_effect=AssertionError('No fallback')):
            with self.assertRaisesRegex(deployment.DeploymentError, 'SQLite fallback is disabled'):
                deployment.check_dashboard_deployment(self.root)


if __name__ == '__main__':
    unittest.main()
