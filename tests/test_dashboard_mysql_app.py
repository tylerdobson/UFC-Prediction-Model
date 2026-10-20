"""Default seven-tab UI and native read-only behavior."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / 'app.py'
TABS = ['Upcoming card', 'Historical data', 'Model evidence', 'Data quality', 'Ledgers', 'Forward test', 'Rankings']
HAS_STREAMLIT = importlib.util.find_spec('streamlit') is not None


@unittest.skipUnless(HAS_STREAMLIT, 'dashboard extra required')
class MySQLDashboardAppTests(unittest.TestCase):
    def test_mysql_failure_renders_all_sections_without_file_fallback(self):
        from streamlit.testing.v1 import AppTest
        import streamlit as st
        st.cache_data.clear()
        unavailable = {'status': 'unavailable', 'reason': 'Evidence unavailable'}
        overview = {'status': 'unavailable', 'health': unavailable, 'archive': unavailable,
                    'quality': unavailable, 'model_evidence': unavailable, 'ledgers': unavailable,
                    'forward_test': unavailable, 'rankings': unavailable}
        with patch.dict(os.environ, {'UFC_MODEL_DB': 'mysql:operating'}), \
                patch('ufc_odds_model.dashboard_mysql.load_mysql_overview', return_value=overview), \
                patch('ufc_odds_model.dashboard_forecast.load_mysql_saved_forecast', return_value=unavailable), \
                patch('ufc_odds_model.mysql_delivery.load_document', return_value=None), \
                patch('ufc_odds_model.dashboard_data.load_dashboard') as legacy:
            app = AppTest.from_file(str(APP), default_timeout=20).run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual([t.label for t in app.tabs], TABS)
        self.assertTrue(any('MySQL is unavailable' in e.value for e in app.error))
        legacy.assert_not_called()
        st.cache_data.clear()

    @unittest.skipUnless(os.environ.get('UFC_MYSQL_PROJECT_INTEGRATION') == '1', 'initialized native project evidence required')
    def test_published_native_evidence_loads_without_sqlite_or_schema_writes(self):
        from streamlit.testing.v1 import AppTest
        import streamlit as st
        st.cache_data.clear()
        with patch.dict(os.environ, {'UFC_MODEL_DB': 'mysql:operating'}), \
                patch('sqlite3.connect', side_effect=AssertionError('No SQLite runtime')), \
                patch('ufc_odds_model.mysql_runtime.ensure_operating_dataset') as ddl:
            app = AppTest.from_file(str(APP), default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual([t.label for t in app.tabs], TABS)
        self.assertGreater(len(app.dataframe), 15)
        self.assertTrue(any('8,950' in c.value for c in app.caption))
        self.assertTrue(any(s.key == 'mysql_rankings_system' for s in app.selectbox))
        ddl.assert_not_called()
        st.cache_data.clear()
