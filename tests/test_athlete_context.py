import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.athlete_context import attach_reach_observations, load_athlete_context, measurements, verify_retained_receipt
from ufc_odds_model.features import _profiles_as_of


class AthleteContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source = {'schema_version':1,'http_status': 200, 'fetched_at_utc': '2026-10-19T19:00:00Z',
                  'request_url':'https://www.ufc.com/athlete/athlete-a',
                  'response_url':'https://www.ufc.com/athlete/athlete-a'}
        for field in ('response', 'headers'):
            path = self.root / f'data/raw/profile.{field}'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'official athlete evidence')
            source[field + '_path'] = str(path.relative_to(self.root))
            source[field + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        source['response_bytes']=(self.root/source['response_path']).stat().st_size
        receipt=self.root/'data/raw/profile.receipt.json'
        receipt.write_text(json.dumps(source))
        source['receipt_path']=str(receipt.relative_to(self.root))
        source['receipt_sha256']=hashlib.sha256(receipt.read_bytes()).hexdigest()
        self.fighter = {'fighter_id': 'stable:a', 'name': 'Athlete A',
                        'official_profile_url': 'https://www.ufc.com/athlete/athlete-a',
                        'observed_at_utc': source['fetched_at_utc'],
                        'reach_inches': 72, 'reach_cm': 182.88, 'source': source,
                        'stats': {'significant_strikes_landed_per_minute': 4.7,
                                  'takedown_defense_pct': None}}
        self.path = self.root / 'facts.json'
        self.save()
        self.cutoff = datetime(2026, 10, 19, 20, tzinfo=timezone.utc)

    def save(self):
        self.path.write_text(json.dumps({'schema_version': 1, 'fighters': [self.fighter]}))

    def test_source_bytes_capture_order_and_unit_conversion_are_enforced(self):
        self.assertEqual(load_athlete_context(self.path, self.root, self.cutoff)['stable:a']['reach_cm'], 182.88)
        self.fighter['reach_cm'] = 180
        self.save()
        with self.assertRaisesRegex(ValueError, 'unit conversion'):
            load_athlete_context(self.path, self.root, self.cutoff)
        self.fighter['reach_cm'] = 182.88
        self.fighter['source']['fetched_at_utc'] = '2026-10-19T21:00:00Z'
        self.save()
        with self.assertRaisesRegex(ValueError, 'does not match source metadata'):
            load_athlete_context(self.path, self.root, self.cutoff)

    def test_backdated_metadata_cannot_override_the_retained_capture_time(self):
        original_receipt=(self.root/self.fighter['source']['receipt_path']).read_bytes()
        self.fighter['source']['fetched_at_utc']='2026-10-19T18:00:00Z'
        self.fighter['observed_at_utc']='2026-10-19T18:00:00Z'
        self.save()
        cutoff=datetime(2026,10,19,18,30,tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError,'does not match source metadata'):
            load_athlete_context(self.path,self.root,cutoff)
        self.assertEqual((self.root/self.fighter['source']['receipt_path']).read_bytes(),original_receipt)

    def test_actual_receipt_time_is_strictly_before_cutoff(self):
        equal=datetime(2026,10,19,19,tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError,'precede the forecast cutoff'):
            verify_retained_receipt(self.fighter['source'],self.root,equal)

    def test_swapped_profile_source_is_rejected_even_when_its_bytes_are_intact(self):
        # The original valid source cannot be attributed to another profile.
        self.fighter['official_profile_url']='https://www.ufc.com/athlete/athlete-b'
        self.save()
        with self.assertRaisesRegex(ValueError,'profile identity does not match'):
            load_athlete_context(self.path,self.root,self.cutoff)
        # Relabelling source URLs cannot change the immutable receipt either.
        self.fighter['source']['request_url']=self.fighter['official_profile_url']
        self.fighter['source']['response_url']=self.fighter['official_profile_url']
        self.save()
        with self.assertRaisesRegex(ValueError,'does not match source metadata'):
            load_athlete_context(self.path,self.root,self.cutoff)

    def test_receipt_fields_and_response_byte_count_are_bound_to_real_files(self):
        receipt=self.root/self.fighter['source']['receipt_path']
        content=json.loads(receipt.read_text());content['response_bytes']+=1
        receipt.write_text(json.dumps(content))
        self.fighter['source']['receipt_sha256']=hashlib.sha256(receipt.read_bytes()).hexdigest()
        self.save()
        with self.assertRaisesRegex(ValueError,'byte count'):
            load_athlete_context(self.path,self.root,self.cutoff)

    def test_tampered_source_is_rejected_and_absent_statistics_are_not_invented(self):
        facts = measurements(self.fighter, reach_used_by_model=True)['facts']
        self.assertEqual(facts[0], {'key': 'reach_cm', 'value': 182.88, 'used_by_model': True})
        self.assertEqual(facts[1]['used_by_model'], False)
        self.assertEqual(len(facts), 2)
        (self.root / self.fighter['source']['response_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'evidence is missing, changed'):
            load_athlete_context(self.path, self.root, self.cutoff)

    def test_only_dated_reach_enters_transient_card_not_career_rate_features(self):
        context = load_athlete_context(self.path, self.root, self.cutoff)
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory = sqlite3.Row
            db.init_db(connection)
            db.upsert_fighter(connection, 'stable:a', 'Athlete A')
            attach_reach_observations(connection, context, self.root)
            profiles = _profiles_as_of(connection, '2026-10-19T20:00:00Z', {})
            self.assertEqual(profiles['stable:a'].reach_cm, 182.88)
            self.assertEqual(_profiles_as_of(connection, '2026-10-19T18:59:00Z', {}), {})
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM fight_stat_observations').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM results').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
