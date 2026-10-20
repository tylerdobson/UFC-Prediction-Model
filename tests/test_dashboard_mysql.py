"""Native-only data reads, honest evidence states, and populated dashboard views."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

from ufc_odds_model import dashboard_mysql as dashboard
from ufc_odds_model.rankings import parse_rankings

AS_OF = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)
REPORT = {"generated_at_utc": "2026-10-19T20:22:26Z", "event": {"name": "Saved UFC Card"},
          "forecast": {"model_label": "V6 saved research", "model_version": "logistic-prior-v6:test",
                       "cutoff_at_utc": "2026-10-19T20:22:26Z", "forecast_id": "saved-v6"}, "bouts": [{}, {}]}
EVALUATION = {"created_at_utc": "2026-10-19T20:20:15Z", "candidate_model_version": "logistic-prior-v6",
              "baseline_model_version": "logistic-prior-v5", "feature_names": ["reach", "striking"],
              "selected_candidate_id": "recent5", "fixed_test_is_reused_benchmark": True,
              "fixed_test": {"candidate": {"metrics": {"bouts": 100, "accuracy": .61, "log_loss": .65, "brier_score": .23}},
                             "baseline": {"metrics": {"bouts": 100, "accuracy": .59, "log_loss": .66, "brier_score": .24}}},
              "rolling_origin": {}, "limitations": ["Retrospective benchmark fixture."]}
ENTRIES = [{"kind": "freeze", "forecast_id": "earlier", "recorded_at_utc": "2026-10-01T12:00:00Z",
            "event_name": "Earlier card", "model_version": "v2", "cutoff_at_utc": "2026-09-27T12:00:00Z",
            "recovered_saved_forecast": True, "rows": [{}, {}]},
           {"kind": "settle", "forecast_id": "earlier", "recorded_at_utc": "2026-10-10T01:00:00Z", "rows": []}]


def rankings():
    rows = ''.join(f'<tr><td class="views-field-weight-class-rank">{rank}</td><td class="views-field-title"><a href="/athlete/athlete-{index}">Athlete {index}</a></td></tr>'
                   for index, rank in ((1, 1), (2, 2), (3, 2)))
    group = ('<div class="view-grouping"><div class="view-grouping-header">Flyweight</div><table><caption>'
             '<div class="info"><h5><a href="/athlete/holder">Holder</a></h5><h6>Champion</h6></div>'
             '</caption><tbody>' + rows + '</tbody></table></div>')
    return parse_rankings('<div class="view-athlete-rankings view-display-id-block_1">' + group + '</div>',
                          fetched_at_utc="2026-10-20T02:43:27Z")


class Result:
    def __init__(self, rows): self.rows = deepcopy(rows)
    def fetchall(self): return self.rows
    def __iter__(self): return iter(self.rows)


class NativeStatistics:
    def __init__(self):
        self.closed = False
        self.metadata = {"captured_at_utc": "2026-10-19T19:49:25Z", "protocol": "canonical-v1",
                         "source_manifest_sha256": "a" * 64, "store_content_sha256": "b" * 64,
                         "repository_url": "https://example.org/archive", "repository_commit": "c" * 40}
        self.connection = self; self.queries = []
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def event_coverage(self): return [{"event_id": "event-one", "name": "First event", "event_date": "2026-01-01", "bouts": 2}]
    def fighter_coverage(self): return [{"fighter_id": "a", "name": "Alpha", "reach_cm": 180.0}, {"fighter_id": "b", "name": "Beta", "reach_cm": None}]
    def execute(self, query, parameters=()):
        if not query.lstrip().upper().startswith("SELECT "): raise AssertionError("Dashboard attempted a write")
        self.queries.append(query)
        if "FROM bouts b LEFT JOIN" in query:
            base = {"event_id": "event-one", "event_date": "2026-01-01", "event_name": "First event",
                    "fighter_a_id": "a", "fighter_b_id": "b", "fighter_a_name": "Alpha", "fighter_b_name": "Beta",
                    "weight_class": "Lightweight", "scheduled_rounds": 3, "finish_round": 1, "finish_time": "2:00",
                    "elapsed_seconds": 120, "outcome": "win", "winner_fighter_id": "a", "source_url": "http://ufcstats.com/fight-details/0000000000000001",
                    "fighter_a_reach_cm": 180.0, "fighter_b_reach_cm": None, "raw_json": json.dumps({"METHOD": "KO/TKO", "DETAILS": "Punches"})}
            return Result([{**base, "bout_id": "one", "stats_status": "usable", "gap_reason": None},
                           {**base, "bout_id": "two", "stats_status": "held", "gap_reason": "incomplete_rounds"}])
        if "FROM gaps GROUP" in query: return Result([{"field": "rounds", "reason": "incomplete_rounds", "records": 1}])
        if "FROM research_holds" in query: return Result([{"reason": "near_date", "records": 1}])
        if "FROM source_files" in query: return Result([{"file": "source.csv", "sha256": "a" * 64, "source_url": "https://example.org/source.csv", "bytes": 100}])
        if "SELECT totals_json" in query: return Result([{"totals_json": json.dumps({"sig_landed": 10, "kd": 0, "control_seconds": None})}])
        raise AssertionError("Unexpected statistics query")


class Operating:
    def __init__(self): self.closed = False; self.queries = []
    def close(self): self.closed = True
    def execute(self, query, parameters=()):
        self.queries.append(query)
        if not query.lstrip().upper().startswith("SELECT "): raise AssertionError("Dashboard attempted a write")
        if "FROM paper_bets" in query: return Result([])
        if "FROM bets p" in query: return Result([])
        raise AssertionError("Unexpected operating query")


class Server:
    def __init__(self): self.closed = False
    def close(self): self.closed = True
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql):
        if sql != "SELECT VERSION(), DATABASE(), @@port": raise AssertionError(sql)
    def fetchone(self): return ("8.4.11", "ufc_project", 3307)


class UI:
    def __init__(self): self.text = []; self.tables = []; self.options = {}
    def markdown(self, value, **kwargs): self.text.append(value)
    def info(self, value): self.text.append(value)
    def caption(self, value): self.text.append(value)
    def subheader(self, value): self.text.append(value)
    def metric(self, label, value): self.text.append(f"{label}: {value}")
    def dataframe(self, rows, **kwargs): self.tables.append(deepcopy(rows))
    def text_input(self, label, key): return self.options.get(key, "")
    def selectbox(self, label, options, index=0, key=None): return self.options.get(key, options[index])
    def columns(self, spec): return [nullcontext(self) for _ in range(spec if isinstance(spec, int) else len(spec))]


class DashboardMySQLTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(); self.operating = Operating(); self.store = NativeStatistics()
        self.documents = {("forecast", "current"): REPORT, ("evaluation", "v6"): EVALUATION,
                          ("rankings", "current"): rankings()}
        self.patches = [patch.object(dashboard, "_server", return_value=self.server),
                        patch.object(dashboard, "_operating", return_value=self.operating),
                        patch.object(dashboard, "_statistics", return_value=self.store),
                        patch.object(dashboard, "_document", side_effect=lambda kind, key, cutoff: deepcopy(self.documents.get((kind, key)))),
                        patch.object(dashboard, "_entries", return_value=deepcopy(ENTRIES)),
                        patch.object(dashboard, "_summary", return_value={"events": [], "cumulative": {}})]
        for mock in self.patches: mock.start(); self.addCleanup(mock.stop)

    def load(self, **kwargs): return dashboard.load_mysql_overview(AS_OF, **kwargs)

    def test_native_helpers_honor_custom_handles_and_reject_file_targets(self):
        # Stop only helper replacements installed by setUp for this contract.
        for mock in self.patches: mock.stop()
        with patch.dict(os.environ, {'UFC_MODEL_DB': 'mysql:private-operating',
                                   'UFC_MODEL_STATISTICS_DB': 'mysql:pinned-statistics'}), \
                patch('ufc_odds_model.mysql_runtime.connect_dataset') as connect, \
                patch('ufc_odds_model.historical_stats.HistoryStore') as store:
            dashboard._operating(); dashboard._statistics()
            connect.assert_called_once_with('mysql:private-operating', readonly=True)
            store.assert_called_once_with('mysql:pinned-statistics')
        with patch.dict(os.environ, {'UFC_MODEL_STATISTICS_DB': '/legacy/file.sqlite'}):
            with self.assertRaisesRegex(ValueError, 'MySQL'):
                dashboard._statistics()

    def test_native_archive_quality_and_empty_wagers_are_distinct_from_forecasts(self):
        result = self.load()
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["archive"]["dataset"], "mysql:statistics")
        self.assertEqual(result["archive"]["bouts"][0]["method"], "KO/TKO")
        self.assertEqual(result["archive"]["bouts"][0]["method_details"], "Punches")
        self.assertIsNone(result["archive"]["bouts"][0]["fighter_b_reach_cm"])
        self.assertEqual(result["quality"]["missing_reach_profiles"], 1)
        missing = {r["field"]: r["missing_fighter_bouts"] for r in result["quality"]["missing_statistics"]}
        self.assertEqual(missing["control_seconds"], 1); self.assertEqual(missing["kd"], 0)
        self.assertEqual(result["ledgers"]["actual"]["count"], 0)
        self.assertEqual(result["forward_test"]["frozen_forecasts"], 1)
        self.assertEqual(result["forward_test"]["saved_unregistered_forecasts"][0]["forecast_id"], "saved-v6")
        self.assertFalse(result["sqlite_fallback"]); self.assertFalse(result["database_writes"])
        self.assertTrue(self.server.closed and self.operating.closed and self.store.closed)
        json.dumps(result, allow_nan=False)

    def test_mysql_failure_is_explicit_sanitized_and_has_no_file_fallback(self):
        with patch.object(dashboard, "_server", side_effect=RuntimeError("password=secret")), patch.object(dashboard, "_statistics", side_effect=RuntimeError("secret")):
            result = self.load()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["archive"]["status"], "unavailable")
        self.assertNotIn("source_bout_count", result["archive"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertFalse(result["sqlite_fallback"])

    def test_future_document_and_archive_are_not_visible_before_their_capture(self):
        self.store.metadata["captured_at_utc"] = "2026-10-21T00:00:00Z"
        self.documents["forecast", "current"] = {**REPORT, "generated_at_utc": "2026-10-21T00:00:00Z"}
        result = self.load()
        self.assertEqual(result["archive"]["status"], "unavailable")
        self.assertIsNone(result["model_evidence"]["model_report"])
        self.assertFalse(result["forward_test"]["saved_unregistered_forecasts"])

    def test_later_ledger_entries_are_excluded_and_aggregate_not_mislabeled(self):
        future = {**ENTRIES[0], "forecast_id": "later", "recorded_at_utc": "2026-10-21T00:00:00Z"}
        with patch.object(dashboard, "_entries", return_value=[*ENTRIES, future]): result = self.load()
        view = result["forward_test"]
        self.assertEqual(view["entry_count"], 2); self.assertEqual(view["frozen_forecasts"], 1)
        self.assertIsNone(view["summary"])
        self.assertEqual(view["aggregate_status"], "suppressed_post_cutoff_entries")

    def test_missing_documents_stay_uninitialized_and_explicit_extras_override(self):
        self.documents = {}
        result = self.load(extras={"model_report": REPORT, "evaluation_v6": EVALUATION})
        self.assertEqual(result["model_evidence"]["model_report"], REPORT)
        self.assertEqual(result["model_evidence"]["document_status"]["model_report"]["source"], "explicit_extra")
        self.assertEqual(result["rankings"]["status"], "uninitialized")
        with self.assertRaises(ValueError): self.load(extras={"sqlite_fallback_path": "forbidden.sqlite"})

    def test_archive_filters_use_full_rows_and_preserve_null_reach(self):
        archive = self.load()["archive"]
        self.assertEqual(len(dashboard.filter_archive_bouts(archive, search="alpha", status="held", reach="missing")), 1)
        self.assertEqual(dashboard.filter_archive_bouts(archive, reach="both"), [])
        self.assertEqual(len(dashboard.filter_archive_bouts(archive, year=2026, division="Lightweight")), 2)
        self.assertEqual(dashboard.filter_archive_bouts(archive, search="unknown"), [])

    def test_renderers_populate_existing_sections_without_changing_style_or_data(self):
        overview = self.load(); before = deepcopy(overview); ui = UI()
        for render in (dashboard.render_historical_archive, dashboard.render_data_quality, dashboard.render_model_evidence,
                       dashboard.render_ledgers, dashboard.render_forward_test, dashboard.render_rankings): render(overview, st=ui)
        combined = " ".join(ui.text)
        for expected in ("Historical archive", "Data quality", "Model evidence", "Recorded actual wagers", "Forward test", "Official UFC rankings", "not counted"):
            self.assertIn(expected, combined)
        self.assertTrue(any(r.get("Method") == "KO/TKO" for table in ui.tables for r in table))
        self.assertTrue(any(r.get("Rank") == "2 =" for table in ui.tables for r in table))
        self.assertEqual(overview, before)


if __name__ == "__main__": unittest.main()
