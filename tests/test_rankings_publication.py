"""Portrait cache stays roster-bound and refreshes expired display evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ufc_odds_model import mysql_project
from ufc_odds_model.ranking_portraits import _digest

ROOT = Path(__file__).resolve().parents[1]


class RankingPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rankings = json.loads((ROOT / 'assets/data/ufc-rankings.json').read_bytes())
        cls.portraits = json.loads((ROOT / 'assets/data/rankings-portraits.json').read_bytes())
        cls.captured = datetime.fromisoformat(cls.portraits['fetched_at_utc'].replace('Z', '+00:00'))

    def prepare(self, existing, age):
        with patch('ufc_odds_model.mysql_delivery.load_document', return_value=existing), \
                patch('ufc_odds_model.mysql_project.datetime') as clock, \
                patch('ufc_odds_model.ranking_portraits.fetch_portraits', return_value={'fresh': True}) as fetch:
            clock.now.return_value = self.captured + age
            result = mysql_project.prepare_ranking_portraits(self.rankings, raw_directory='private-capture')
        return result, fetch

    def test_same_roster_recent_verified_images_reuse_without_source_requests(self):
        result, fetch = self.prepare(self.portraits, timedelta(days=1))
        self.assertEqual(result['coverage'], self.portraits['coverage'])
        fetch.assert_not_called()

    def test_expired_images_are_recaptured(self):
        result, fetch = self.prepare(self.portraits, timedelta(days=8))
        self.assertEqual(result, {'fresh': True})
        fetch.assert_called_once_with(self.rankings, raw_directory='private-capture')

    def test_different_roster_binding_cannot_reuse_the_old_image_map(self):
        wrong = deepcopy(self.portraits)
        wrong['rankings_source_sha256'] = 'f' * 64
        wrong['snapshot_sha256'] = _digest(wrong)
        result, fetch = self.prepare(wrong, timedelta(days=1))
        self.assertEqual(result, {'fresh': True})
        fetch.assert_called_once()
