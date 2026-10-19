"""Statistics opt-in must stay explicit and cannot bypass model evidence."""
import sqlite3
import unittest
from datetime import datetime, timezone

from ufc_odds_model.cli import make_parser
from ufc_odds_model.pipeline import score_event


class StatisticsScoringTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.cutoff = datetime(2026, 10, 19, tzinfo=timezone.utc)

    def test_statistics_require_logistic_research_and_all_frozen_evidence(self):
        with self.assertRaisesRegex(ValueError, 'requires --training-history'):
            score_event(self.connection, 'target', self.cutoff, statistics_db='stats.sqlite')
        with self.assertRaisesRegex(ValueError, 'archive, selection and evaluation'):
            score_event(self.connection, 'target', self.cutoff, model_kind='logistic',
                        training_history='research.sqlite', statistics_db='stats.sqlite')
        with self.assertRaisesRegex(ValueError, 'requires --statistics-db'):
            score_event(self.connection, 'target', self.cutoff, model_kind='logistic',
                        training_history='research.sqlite', statistics_selection='selection.json')

    def test_cli_exposes_reusable_statistics_inputs_without_changing_defaults(self):
        parser = make_parser()
        legacy = parser.parse_args(['score-event', 'any-card'])
        self.assertIsNone(legacy.statistics_db)
        args = parser.parse_args(['score-event', 'any-card', '--model', 'logistic',
                                  '--training-history', 'research.sqlite',
                                  '--statistics-db', 'stats.sqlite',
                                  '--statistics-archive', 'manifest.json',
                                  '--statistics-selection', 'selection.json',
                                  '--statistics-evaluation', 'evaluation.json'])
        self.assertEqual(args.statistics_db, 'stats.sqlite')
        self.assertEqual(args.statistics_evaluation, 'evaluation.json')


if __name__ == '__main__':
    unittest.main()
