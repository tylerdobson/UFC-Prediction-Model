"""A report must preserve partial coverage and its actual source evidence."""
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ufc_odds_model.forecast_report import build_report, verify_card_sources, write_bundle
from ufc_odds_model.model_lab import file_sha256
from tests.test_prediction_report import public_report


class ForecastReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cutoff = datetime(2026, 10, 19, 20, tzinfo=timezone.utc)
        self.card = {'sources': {}}
        for name in ('official_event', 'official_start_guide'):
            source = {'schema_version': 1, 'http_status': 200, 'fetched_at_utc': '2026-10-19T19:00:00Z',
                      'request_url': 'https://www.ufc.com/' + name,
                      'response_url': 'https://www.ufc.com/' + name}
            for path_key, hash_key in (('response_path', 'response_sha256'),
                                       ('headers_path', 'headers_sha256')):
                path = self.root / f'data/raw/{name}-{path_key}'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'source evidence')
                source[path_key] = str(path.relative_to(self.root))
                source[hash_key] = file_sha256(path)
            receipt = self.root / f'data/raw/{name}-receipt.json'
            receipt.write_text(json.dumps(source))
            source['receipt_path'] = str(receipt.relative_to(self.root))
            source['receipt_sha256'] = file_sha256(receipt)
            self.card['sources'][name] = source

    def test_sources_are_checked_from_bytes_and_cannot_postdate_cutoff(self):
        verify_card_sources(self.card, self.root, self.cutoff)
        path = self.root / self.card['sources']['official_event']['response_path']
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed since capture'):
            verify_card_sources(self.card, self.root, self.cutoff)
        path.write_bytes(b'source evidence')
        self.card['sources']['official_event']['fetched_at_utc'] = '2026-10-19T21:00:00Z'
        with self.assertRaisesRegex(ValueError, 'after the forecast cutoff'):
            verify_card_sources(self.card, self.root, self.cutoff)

    def test_outside_or_linked_source_is_not_accepted(self):
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'source evidence')
        self.card['sources']['official_event']['response_path'] = str(outside)
        with self.assertRaisesRegex(ValueError, 'outside the retained'):
            verify_card_sources(self.card, self.root, self.cutoff)
        link = self.root / 'data/raw/link'
        link.symlink_to(outside)
        self.card['sources']['official_event']['response_path'] = str(link)
        with self.assertRaisesRegex(ValueError, 'outside the retained'):
            verify_card_sources(self.card, self.root, self.cutoff)

    def test_backdated_capture_metadata_and_swapped_receipt_url_are_rejected(self):
        source = self.card['sources']['official_event']
        source['fetched_at_utc'] = '2026-10-19T17:00:00Z'
        with self.assertRaisesRegex(ValueError, 'receipt'):
            verify_card_sources(self.card, self.root,
                                datetime(2026, 10, 19, 18, tzinfo=timezone.utc))
        source['fetched_at_utc'] = '2026-10-19T19:00:00Z'
        source['request_url'] = 'https://www.ufc.com/other-event'
        with self.assertRaisesRegex(ValueError, 'receipt'):
            verify_card_sources(self.card, self.root, self.cutoff)

    def test_bundle_is_exclusive_and_each_public_output_has_a_receipt_hash(self):
        report = public_report()
        output = self.root / 'frozen'
        write_bundle(report, {'forecast_id': 'frozen-test', 'research_only': True}, output)
        receipt = json.loads((output / 'forecast-receipt.json').read_text())
        for name, expected in receipt['outputs'].items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)
        with self.assertRaises(FileExistsError):
            write_bundle(report, {}, output)

    def test_future_cutoff_and_wrong_root_fail_before_any_model_fit(self):
        with self.assertRaisesRegex(ValueError, 'project root'):
            build_report('missing', 'missing', 'missing', 'missing', root=self.root)
        cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with self.assertRaisesRegex(ValueError, 'cannot be in the future'):
                build_report('missing', 'missing', 'missing', 'missing', root=self.root,
                             as_of=datetime(2026, 10, 20, tzinfo=timezone.utc), now=self.cutoff)
        finally:
            os.chdir(cwd)


if __name__ == '__main__':
    unittest.main()
