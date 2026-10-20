"""Native publication, transaction rollback and append-only ledger contracts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch
import uuid

from ufc_odds_model import mysql_delivery as delivery
from ufc_odds_model.dashboard_forecast import load_mysql_saved_forecast


class PublishedEditionTests(unittest.TestCase):
    def test_database_failure_never_selects_a_local_forecast(self):
        with patch.object(delivery, 'load_document', side_effect=RuntimeError('private connection detail')), \
                patch('ufc_odds_model.dashboard_forecast.load_saved_forecast') as local:
            result = load_mysql_saved_forecast()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('private', str(result))
        local.assert_not_called()

    def test_release_and_database_payload_must_match(self):
        release = {'json_path': 'report.json', 'json_sha256': 'a' * 64}
        with patch.object(delivery, 'load_document', side_effect=[release, {'edition': 2}]), \
                patch('ufc_odds_model.dashboard_forecast.load_saved_forecast', return_value={
                    'status': 'available', 'report': {'edition': 1},
                    'artifacts': {'json': {'sha256': 'a' * 64}}}):
            self.assertEqual(load_mysql_saved_forecast()['status'], 'unavailable')


@unittest.skipUnless(os.environ.get('UFC_MYSQL_INTEGRATION') == '1', 'native MySQL integration is opt-in')
class NativeDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        delivery.ensure_schema()

    def test_publication_prevents_older_replacement_and_retains_immutable_versions(self):
        key = 'qa-' + uuid.uuid4().hex
        stamp = datetime.now(timezone.utc) - timedelta(minutes=1)
        receipt = delivery.publish_document('qa', key, {'value': 1}, captured_at_utc=stamp.isoformat())
        self.assertEqual(delivery.load_document('qa', key), {'value': 1})
        with self.assertRaises(ValueError):
            delivery.publish_document('qa', key, {'value': 0}, captured_at_utc=(stamp - timedelta(seconds=1)).isoformat())
        with delivery._connection() as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(Exception):
                    cursor.execute('UPDATE ufc_app_document SET payload_json=%s WHERE document_sha256=%s',
                                   ('{}', receipt['document_sha256']))
            connection.rollback()
        self.assertEqual(delivery.load_document('qa', key), {'value': 1})

    def test_concurrent_appends_preserve_chain_and_duplicate_action_rolls_back(self):
        name = 'qa-' + uuid.uuid4().hex
        def append(number):
            return delivery.append_forecast_entry({'kind': 'freeze', 'forecast_id': str(number)}, name=name)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(8)))
        entries = delivery.read_forecast_entries(name)
        self.assertEqual(len(entries), 8)
        self.assertEqual({e['forecast_id'] for e in entries}, {str(n) for n in range(8)})
        with self.assertRaisesRegex(ValueError, 'already'):
            append(0)
        self.assertEqual(delivery.read_forecast_entries(name), entries)

    def test_alias_rolls_back_when_receipt_publication_fails(self):
        from ufc_odds_model.mysql_runtime import create_dataset, finalize_dataset
        name = 'qa-publication-' + uuid.uuid4().hex
        connection = create_dataset('CREATE TABLE observations(id INTEGER PRIMARY KEY,value REAL);', name, 'statistics')
        try:
            connection.execute('INSERT INTO observations(id,value) VALUES(1,0.5)')
            identity = finalize_dataset(connection, {})
        finally:
            connection.close()
        def active():
            with delivery._connection(readonly=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT target FROM ufc_runtime_aliases WHERE alias='statistics'")
                    return cursor.fetchone()
        previous = active()
        publication = {'dataset': 'mysql:' + name,
                       'mysql_content_sha256': identity['content_sha256'],
                       'published_at_utc': datetime.now(timezone.utc).isoformat()}
        with patch.object(delivery, 'publish_document', side_effect=ValueError('forced receipt failure')):
            with self.assertRaisesRegex(ValueError, 'forced'):
                delivery.activate_statistics_publication(publication)
        self.assertEqual(active(), previous)
