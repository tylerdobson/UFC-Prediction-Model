"""Precise dates, conflicts, immutable receipts, cutoffs, and v3 leakage exclusion."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model import db
from ufc_odds_model.features import FEATURE_NAMES, FeatureRow, _profiles_as_of
from ufc_odds_model.logistic import V3_FEATURE_NAMES, fit_logistic, v3_features
from ufc_odds_model.wikidata_profiles import (
    import_profiles, parse_attributes, profile_query, static_profiles,
)


def binding(dob="1990-01-02T00:00:00Z", precision="11", **extra):
    fields = {"item": "http://www.wikidata.org/entity/Q42", "dob": dob, "prec": precision,
              "height": "1.85", "ufc": "some-athlete", **extra}
    return {key:{"value":value} for key,value in fields.items() if value is not None}


class WikidataProfileTests(unittest.TestCase):
    def test_day_precision_conflict_and_si_height(self):
        values, conflicts = parse_attributes([binding(), binding()])
        self.assertEqual(values["birth_date"], "1990-01-02")
        self.assertEqual(values["height_cm"], "185")
        self.assertEqual(conflicts, [])
        values, _ = parse_attributes([binding(precision="9")])
        self.assertNotIn("birth_date", values)
        values, conflicts = parse_attributes([binding(), binding("1990-02-03T00:00:00Z")])
        self.assertEqual(values, {})
        self.assertIn("birth_date", conflicts)

    def test_v3_predictions_cannot_depend_on_coverage_flags(self):
        self.assertFalse(set(V3_FEATURE_NAMES) & {
            "known_age_difference", "known_reach_difference", "known_sig_strike_stat_difference"})
        first = [0.] * len(FEATURE_NAMES)
        second = first[:]
        for name in ("known_age_difference", "known_reach_difference", "known_sig_strike_stat_difference"):
            second[FEATURE_NAMES.index(name)] = 1.
        self.assertEqual(v3_features(first), v3_features(second))
        rows = [FeatureRow(str(i), str(i), "2020-01-01", "a", "b", v3_features(first), i%2)
                for i in range(10)]
        model = fit_logistic(rows, feature_names=V3_FEATURE_NAMES)
        self.assertEqual(model.predict_probability(v3_features(first)),
                         model.predict_probability(v3_features(second)))

    def test_batch_limits_and_title_escaping(self):
        query = profile_query(['A "quoted" title', "A\\B"])
        self.assertIn('A \\"quoted\\" title', query)
        with self.assertRaises(ValueError):
            profile_query(["a"] * 251)
        with self.assertRaises(ValueError):
            profile_query(["Q1 } DROP"], qids=True)

    def test_exact_response_receipts_static_cutoffs_and_immutability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(":memory:") as connection:
                connection.row_factory = sqlite3.Row
                db.init_db(connection)
                db.upsert_fighter(connection, "wikidata:Q42", "Example")
                raw = json.dumps({"results":{"bindings":[binding()]}}).encode()
                requests = []
                def fetch(request):
                    requests.append(request)
                    return raw
                with patch("ufc_odds_model.wikidata_profiles.utc_now") as now:
                    from datetime import datetime, timezone
                    now.return_value = datetime(2026,1,1,tzinfo=timezone.utc)
                    result = import_profiles(connection, fetch=fetch, raw_dir=root / "raw",
                                             report_path=root / "report.json")
                self.assertEqual(result["fighters_with_birth_date"], 1)
                self.assertEqual(requests[0]["method"], "POST")
                receipt = connection.execute("SELECT * FROM ingestion_runs WHERE source='wikidata_profiles'").fetchone()
                self.assertEqual(Path(receipt["payload_path"]).read_bytes(), raw)
                self.assertEqual(static_profiles(connection, cutoff_at_utc="2026-01-01T00:00:00Z"), {})
                after = _profiles_as_of(connection, "2026-01-02T00:00:00Z", {})
                self.assertEqual(after["wikidata:Q42"].birth_date.isoformat(), "1990-01-02")
                self.assertEqual(len(static_profiles(connection, research=True)), 1)
                self.assertEqual(static_profiles(connection, cutoff_at_utc="2020-01-01T00:00:00Z"), {})
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE fighter_static_attributes SET value='2000-01-01'")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM fighter_static_attributes")
                # Cached requests replay offline without fetching or adding duplicate attributes.
                repeated = import_profiles(connection, offline=True, raw_dir=root / "raw",
                                           report_path=root / "repeated.json")
                self.assertEqual(repeated["new_attribute_rows"], 0)
                self.assertEqual(len(requests), 1)
                Path(receipt["payload_path"]).write_bytes(b"changed")
                self.assertEqual(static_profiles(connection, research=True), {})
