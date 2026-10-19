import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.statistics_workflow import REQUIRED_COLUMNS, refresh_archive


class StatisticsRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commit = 'a' * 40

    def download(self, url):
        if 'api.github.com' in url:
            body = json.dumps({'sha': self.commit, 'commit': {'committer': {'date': '2026-10-18T00:00:00Z'}}}).encode()
        else:
            name = url.rsplit('/', 1)[1]
            body = (','.join(sorted(REQUIRED_COLUMNS[name])) + '\n').encode() if name in REQUIRED_COLUMNS else b'publisher text\n'
        return body, {'request_url': url, 'response_url': url, 'http_status': 200,
                      'headers': 'Content-Type: text/plain\n', 'fetched_at_utc': '2026-10-19T12:00:00Z'}

    @patch('ufc_odds_model.statistics_workflow._download')
    def test_commit_pinning_retains_sources_and_repeat_detects_changed_bytes(self, download):
        download.side_effect = self.download
        path = refresh_archive(self.root, commit=self.commit)
        saved = json.loads(path.read_text())
        self.assertEqual(saved['repository_commit'], self.commit)
        self.assertEqual(len(saved['files']), 7)
        for entry in saved['files']:
            self.assertIn(self.commit, entry['source_url'])
            self.assertEqual(hashlib.sha256((path.parent / entry['file']).read_bytes()).hexdigest(), entry['sha256'])
        download.reset_mock()
        self.assertEqual(refresh_archive(self.root, commit=self.commit), path)
        self.assertEqual(download.call_count, 1)
        (path.parent / 'ufc_fight_stats.csv').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            refresh_archive(self.root, commit=self.commit)

    @patch('ufc_odds_model.statistics_workflow._download')
    def test_bad_schema_does_not_leave_a_partial_archive(self, download):
        def bad(url):
            body, receipt = self.download(url)
            return (b'<html>browser check</html>' if url.endswith('ufc_fight_stats.csv') else body), receipt
        download.side_effect = bad
        with self.assertRaisesRegex(ValueError, 'schema'):
            refresh_archive(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    @patch('ufc_odds_model.statistics_workflow._download')
    def test_requested_commit_mismatch_is_rejected(self, download):
        download.side_effect = self.download
        with self.assertRaisesRegex(ValueError, 'requested'):
            refresh_archive(self.root, commit='b' * 40)


if __name__ == '__main__':
    unittest.main()
