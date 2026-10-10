"""A post-event capture retains receipts and never approves operating results."""
import json
import unittest
from datetime import datetime, timezone

import test_capture_prefight_card as fixture
from scripts.capture_prefight_card import capture_prefight_card
from scripts.capture_completed_event import capture_completed_event, verify_completed_capture


class CompletedCaptureTests(unittest.TestCase):
    def setUp(self):
        helper = fixture.CapturePrefightCardTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.root = helper.root
        fetch, _ = helper._fetch()
        self.prefight = self.root/'data/raw/prefight/manifest.json'
        capture_prefight_card(helper.seed_path, self.prefight.parent,
                              fetch=fetch, clock=helper.clock)
        page = fixture._page('Epsilon Fighter', 202)
        page['latest']['timestamp'] = '2099-01-02T19:00:00Z'
        page['source'] = page['source'].replace('==Fight card==', '==Results==')
        page['source'] = page['source'].replace(
            '|[[Alpha Fighter]]\n|vs.\n|[[Epsilon Fighter]]',
            '|[[Epsilon Fighter]]\n|def.\n|[[Alpha Fighter]]')
        page['source'] = page['source'].replace('|[[Alpha Fighter]]\n|\n|\n|\n|\n',
                                                '|[[Alpha Fighter]]\n|TKO\n|1\n|0:30\n|\n')
        page['source'] = page['source'].replace('|vs.\n', '|def.\n').replace(
            '|[[Delta Fighter]]\n|\n|\n|\n|\n', '|[[Delta Fighter]]\n|Decision\n|3\n|5:00\n|\n')
        self.raw = fixture._body(page)
        self.fetch = lambda url: (self.raw, '2099-01-02T20:00:00Z')
        self.clock = lambda: datetime(2099, 1, 2, 20, 1, tzinfo=timezone.utc)
        self.output = self.root/'data/raw/completed'

    def test_reversed_result_identity_and_method_and_hash_verified_relocation(self):
        result = capture_completed_event(self.prefight, self.output, fetch=self.fetch, clock=self.clock)
        manifest_path = self.output/'manifest.json'
        capture = verify_completed_capture(manifest_path)
        bout = capture['completed_bouts'][0]
        self.assertEqual(bout['winner_fighter_id'], 'wikipedia:55')
        self.assertEqual(bout['method'], 'TKO')
        self.assertEqual(bout['status'], 'matched_unreviewed')
        self.assertTrue(result['review_pending'])
        self.assertFalse(capture['review_summary']['operating_import_eligible'])
        # Immutable historical paths can be relocated only if their bytes match.
        capture['prefight_anchor']['manifest_path'] = '/missing/old-project/data/raw/prefight/manifest.json'
        manifest_path.write_text(json.dumps(capture))
        self.assertEqual(verify_completed_capture(manifest_path)['completed_bouts'], capture['completed_bouts'])
        (self.output/'source.response.json').write_bytes(self.raw+b' ')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            verify_completed_capture(manifest_path)

    def test_reject_before_start_and_existing_output_without_fetch(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return self.fetch(url)
        early = lambda: datetime(2099, 1, 2, 17, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, 'has not elapsed'):
            capture_completed_event(self.prefight, self.output, fetch=fetch, clock=early)
        self.assertEqual(calls, [])
        capture_completed_event(self.prefight, self.output, fetch=fetch, clock=self.clock)
        with self.assertRaises(FileExistsError):
            capture_completed_event(self.prefight, self.output, fetch=fetch, clock=self.clock)
        self.assertEqual(len(calls), 1)
