"""Public report contracts: truthful probabilities, data provenance and exports."""

from __future__ import annotations

import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.prediction_report import main, render_csv, render_html, render_json, validate_report


def public_report():
    return {
        "schema_version": 1,
        "event": {"name": "UFC research card", "event_date": "2026-11-01",
                  "start_time_utc": "2026-11-01T20:00:00Z",
                  "source_url": "https://example.org/event?oldid=12"},
        "generated_at_utc": "2026-10-25T12:01:00Z",
        "forecast": {"model_version": "logistic-prior-v3:research_retrospective",
                     "model_label": "V3 · result history and age", "research_only": True,
                     "status": "research_retrospective", "cutoff_at_utc": "2026-10-25T12:00:00Z"},
        "bouts": [{"bout_id": "one", "fighter_a": "Fighter Alpha", "fighter_b": "Fighter Beta",
                   "p_fighter_a": .62, "p_fighter_b": .38, "weight_class": "Lightweight", "scheduled_rounds": 3,
                   "contributions": [
                       {"feature": "elo_difference_400", "label": "Elo history gap", "logit_contribution": .4},
                       {"feature": "age_years_difference_10", "label": "Age gap", "logit_contribution": -.1}],
                   "coverage": {"fighter_a": {"prior_ufc_bouts": 5, "age": True, "reach": False},
                                "fighter_b": {"prior_ufc_bouts": 0, "age": False}},
                   "notes": ["Reviewed participants; full card completeness remains unverified."]}],
        "evaluation": {"label": "Retrospective fixed test", "bouts": 1447, "accuracy": .5743,
                       "log_loss": .67123, "brier_score": .23946, "period": "2023–2026",
                       "baseline_label": "V2", "baseline_log_loss": .68779},
        "source_notes": ["Result history and birth dates; no fight-statistics signal."],
        "limitations": ["Research training does not qualify for operating model readiness."],
        "sources": [{"label": "Public model methodology", "url": "https://example.org/model"}],
    }


def measured_profile(*, captured="2026-10-25T12:00:30Z", used=False):
    return {"source_url": "https://example.org/athlete/profile", "source_label": "Official athlete profile",
            "source_verified": True, "captured_at_utc": captured,
            "facts": [{"key": "reach_cm", "value": 181.61, "used_by_model": used},
                      {"key": "significant_strikes_landed_per_minute", "value": 5.99, "used_by_model": False},
                      {"key": "significant_strike_accuracy_display_pct", "value": 57, "used_by_model": False}]}


class PredictionReportTests(unittest.TestCase):
    def test_report_is_standalone_and_explains_both_probabilities_and_limits(self):
        report = public_report()
        before = copy.deepcopy(report)
        output = render_html(report)
        self.assertEqual(report, before)
        for expected in ("62.0%", "38.0%", "Moderate lean", "Age comparison favors Fighter Beta.",
                         "5 prior UFC fights", "age missing", "Market comparison:", "Unavailable",
                         "57.43%", "0.67123", "research", "No wager is authorized", "@media print",
                         "width:62.000000%", "All times UTC"):
            self.assertIn(expected, output)
        self.assertNotIn("<script", output)
        self.assertNotIn("<link", output)
        self.assertNotIn("<img", output)
        exported = json.loads(render_json(report))
        self.assertEqual(exported["bouts"][0]["p_fighter_b"], .38)

    def test_probability_contract_rejects_nonfinite_noncomplementary_and_boolean_values(self):
        for field, value in (("p_fighter_a", float("nan")), ("p_fighter_b", float("inf")),
                             ("p_fighter_a", True), ("p_fighter_b", .39), ("p_fighter_a", -1)):
            with self.subTest(field=field, value=value):
                report = public_report()
                report["bouts"][0][field] = value
                with self.assertRaises(ValueError):
                    validate_report(report)
        for research_only in (False, 1, "true", None):
            report = public_report()
            report["forecast"]["research_only"] = research_only
            with self.assertRaises(ValueError):
                render_html(report)
        report = public_report()
        report["forecast"]["status"] = "production"
        with self.assertRaises(ValueError):
            render_json(report)

    def test_duplicate_bouts_and_invalid_cutoffs_are_rejected(self):
        report = public_report()
        report["bouts"].append(copy.deepcopy(report["bouts"][0]))
        with self.assertRaisesRegex(ValueError, "unique bout"):
            validate_report(report)
        report = public_report()
        report["generated_at_utc"] = "2026-10-25T11:59:59Z"
        with self.assertRaisesRegex(ValueError, "generation"):
            validate_report(report)
        report = public_report()
        report["forecast"]["cutoff_at_utc"] = report["event"]["start_time_utc"]
        report["generated_at_utc"] = "2026-11-02T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "precede event"):
            validate_report(report)

    def test_only_verified_two_sided_market_fresh_at_cutoff_is_exported(self):
        report = public_report()
        market = {"source_verified": True, "fresh": True, "p_fighter_a": .6, "p_fighter_b": .4,
                  "bookmaker": "Example book", "captured_at_utc": "2026-10-25T11:59:50Z",
                  "bookmaker_updated_at_utc": "2026-10-25T11:59:45Z"}
        report["bouts"][0]["market"] = market
        self.assertIn("Example book, no vig", render_html(report))
        self.assertEqual(validate_report(report)["bouts"][0]["market"]["p_fighter_a"], .6)
        market["source_verified"] = False
        self.assertIsNone(validate_report(report)["bouts"][0]["market"])
        self.assertNotIn("Example book", render_csv(report))
        market["source_verified"] = True
        market["bookmaker_updated_at_utc"] = "2026-10-25T11:00:00Z"
        self.assertIsNone(validate_report(report)["bouts"][0]["market"])
        market["bookmaker_updated_at_utc"] = "2026-10-25T12:01:00Z"
        with self.assertRaisesRegex(ValueError, "timestamp order"):
            render_json(report)

    def test_escaping_csv_formula_safety_and_allowlist_prevent_private_exports(self):
        report = public_report()
        report["event"]["name"] = "=HYPERLINK(\"evil\")"
        report["bouts"][0]["fighter_a"] = "<script>alert('x')</script>"
        report["bouts"][0]["fighter_b"] = "  +Formula"
        report["secret"] = "do-not-export"
        report["forecast"]["database_path"] = "/Users/private/source.sqlite"
        report["bouts"][0]["coverage"]["fighter_a"]["api_key"] = "do-not-export"
        report["bouts"][0]["raw_metadata"] = {"secret": "do-not-export"}
        markup = render_html(report)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)
        rows = list(csv.DictReader(io.StringIO(render_csv(report))))
        self.assertTrue(rows[0]["event"].startswith("'="))
        self.assertTrue(rows[0]["fighter_b"].startswith("'+"))
        for result in (markup, render_json(report), render_csv(report)):
            self.assertNotIn("do-not-export", result)
            self.assertNotIn("/Users/", result)
        for private in ("/Users/private/source.sqlite", "file:///tmp/secret", "ODDS_API_KEY=secret12345",
                        "sk-proj-abcdefghijklmnopqrstuvwxyz"):
            report["source_notes"] = [private]
            with self.assertRaises(ValueError):
                render_json(report)
        report = public_report()
        report["sources"][0]["url"] = "https://example.org/?api_key=private"
        with self.assertRaises(ValueError):
            render_html(report)

    def test_even_matchup_has_no_invented_favorite_and_missing_explanation_is_explicit(self):
        report = public_report()
        report["bouts"][0].update(p_fighter_a=.5, p_fighter_b=.5, contributions=[])
        self.assertIn("Factor explanations unavailable", render_html(report))
        row = next(csv.DictReader(io.StringIO(render_csv(report))))
        self.assertEqual(row["favorite"], "Even matchup")
        self.assertEqual(row["forecast_strength"], "Even")

    def test_unscored_matchups_remain_visible_without_manufactured_probabilities(self):
        report = public_report()
        report["unscored_bouts"] = [{"fighter_a": "Unmatched Fighter", "fighter_b": "Debut Fighter",
                                      "weight_class": "Welterweight", "reason": "Stable fighter identities have not been verified."}]
        output = render_html(report)
        self.assertIn("Not scored", output)
        self.assertIn("1 listed matchup not scored", output)
        self.assertIn("Unmatched Fighter", output)
        rows = list(csv.DictReader(io.StringIO(render_csv(report))))
        self.assertEqual([row["forecast_status"] for row in rows], ["scored", "not_scored"])
        self.assertEqual(rows[1]["p_fighter_a"], "")
        self.assertEqual(rows[1]["p_fighter_b"], "")
        self.assertEqual(rows[1]["favorite"], "")
        self.assertEqual(rows[1]["notes"], report["unscored_bouts"][0]["reason"])
        for row in rows:
            self.assertEqual(row["scored_bouts"], "1")
            self.assertEqual(row["listed_bouts"], "2")
            self.assertEqual(row["not_scored_bouts"], "1")
            self.assertEqual(row["event_source_url"], report["event"]["source_url"])
            self.assertIn("No wager is authorized", row["report_limitations"])
            self.assertIn("no fight-statistics signal", row["source_notes"])
        report["unscored_bouts"][0].update(fighter_a="Fighter Alpha", fighter_b="Fighter Beta")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_report(report)

    def test_saved_comparison_and_recent_periods_preserve_their_context(self):
        report = public_report()
        report["evaluation"].update(
            baseline_accuracy=.55, paired_log_loss_interval=[-.01169, -.00546],
            rolling={"bouts": 5278, "accuracy": .60, "log_loss": .66079, "baseline_log_loss": .66591},
            recent_year={"year": 2026, "bouts": 398, "accuracy": .5678, "log_loss": .67579},
            note="The historical benchmark has been reused for development; fresh prospective validation is pending.")
        output = render_html(report)
        for expected in ("55.00%", "[-0.01169, -0.00546]", "5,278 bouts", "2026 subset", "benchmark has been reused"):
            self.assertIn(expected, output)
        exported = json.loads(render_json(report))["evaluation"]
        self.assertEqual(exported["recent_year"]["bouts"], 398)
        report["evaluation"]["paired_log_loss_interval"] = [.01, -.01]
        with self.assertRaisesRegex(ValueError, "ordered"):
            validate_report(report)

    def test_observed_stats_have_sources_units_and_do_not_become_model_inputs(self):
        report = public_report()
        report["bouts"][0]["measurements"] = {"fighter_a": measured_profile(), "fighter_b": measured_profile()}
        output = render_html(report)
        for expected in ("181.6", "5.99", "57", "Reach (cm)", "Sig. strikes landed (/ min)",
                         "Striking accuracy (%)", "descriptive and are not used", "after the forecast freeze",
                         "reach observed, not used by model", "fight stats observed, not used by model"):
            self.assertIn(expected, output)
        normalized = json.loads(render_json(report))
        self.assertFalse(normalized["bouts"][0]["coverage"]["fighter_a"]["reach"])
        self.assertFalse(normalized["bouts"][0]["measurements"]["fighter_a"]["facts"][0]["used_by_model"])
        self.assertTrue(normalized["bouts"][0]["measurements"]["fighter_a"]["observed_after_forecast"])
        row = next(csv.DictReader(io.StringIO(render_csv(report))))
        self.assertIn("descriptive only", row["fighter_a_observed_measurements"])
        self.assertIn("https://example.org/athlete/profile", row["fighter_a_observed_measurements"])

    def test_model_used_reach_requires_coverage_and_prefreeze_source(self):
        report = public_report()
        bout = report["bouts"][0]
        bout["measurements"] = {"fighter_a": measured_profile(used=True)}
        with self.assertRaisesRegex(ValueError, "before forecast"):
            validate_report(report)
        bout["measurements"]["fighter_a"]["captured_at_utc"] = "2026-10-25T11:59:30Z"
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_report(report)
        bout["coverage"]["fighter_a"]["reach"] = True
        bout["coverage"]["fighter_a"]["fight_statistics"] = True
        output = render_html(report)
        self.assertIn("181.6 *", output)
        self.assertIn("reach available to model", output)
        self.assertIn("earlier-fight stats available to model", output)
        self.assertIn("Unmarked current career averages are descriptive", output)

    def test_measurements_reject_unknown_or_nonfinite_stats_and_private_sources(self):
        for mutation in ("unverified", "future", "percent", "unknown", "nonfinite", "duplicate", "private"):
            with self.subTest(mutation=mutation):
                report = public_report()
                profile = measured_profile()
                report["bouts"][0]["measurements"] = {"fighter_a": profile}
                if mutation == "unverified": profile["source_verified"] = False
                elif mutation == "future": profile["captured_at_utc"] = "2026-10-25T12:02:00Z"
                elif mutation == "percent": profile["facts"][2]["value"] = 101
                elif mutation == "unknown": profile["facts"][0]["key"] = "invented_stat"
                elif mutation == "nonfinite": profile["facts"][0]["value"] = float("nan")
                elif mutation == "duplicate": profile["facts"].append(copy.deepcopy(profile["facts"][0]))
                elif mutation == "private": profile["source_url"] = "file:///Users/private/source.html"
                with self.assertRaises(ValueError):
                    render_html(report)

    def test_model_stat_counts_and_reach_provenance_survive_all_public_exports(self):
        report = public_report()
        report["bouts"][0]["coverage"]["fighter_a"].update(
            prior_fights_with_stats=4, fight_statistics=True, reach=True, reach_cm=181.61, reach_source="dated_profile")
        report["bouts"][0]["coverage"]["fighter_b"].update(
            prior_fights_with_stats=0, fight_statistics=False, reach=True, reach_cm=184.15, reach_source="retrospective_archive")
        clean = json.loads(render_json(report))
        self.assertEqual(clean["bouts"][0]["coverage"]["fighter_a"]["prior_fights_with_stats"], 4)
        self.assertEqual(clean["bouts"][0]["coverage"]["fighter_b"]["reach_source"], "retrospective_archive")
        markup = render_html(report)
        self.assertIn("4 earlier fights with strike counts", markup)
        self.assertIn("181.6 cm available to model (dated profile)", markup)
        self.assertIn("184.2 cm available to model (retrospective archive)", markup)
        row = next(csv.DictReader(io.StringIO(render_csv(report))))
        self.assertEqual(row["fighter_a_prior_fights_with_stats"], "4")
        self.assertEqual(row["fighter_b_prior_fights_with_stats"], "0")
        self.assertEqual(row["fighter_a_model_reach_cm"], "181.61")
        self.assertEqual(row["fighter_b_model_reach_source"], "retrospective_archive")
        self.assertIn("earlier-fight stats available to model", row["fighter_a_coverage"])

    def test_prior_stat_counts_cannot_be_negative_fractional_boolean_or_exceed_history(self):
        for invalid in (-1, 1.5, True, 6, float("nan")):
            with self.subTest(invalid=invalid):
                report = public_report()
                report["bouts"][0]["coverage"]["fighter_a"]["prior_fights_with_stats"] = invalid
                with self.assertRaises(ValueError):
                    validate_report(report)
        report = public_report()
        report["bouts"][0]["coverage"]["fighter_a"].update(prior_fights_with_stats=0, fight_statistics=True)
        with self.assertRaisesRegex(ValueError, "positive prior-fight"):
            validate_report(report)

    def test_full_archive_history_is_distinct_from_result_database_stat_coverage(self):
        report = public_report()
        report['bouts'][0]['coverage']['fighter_a'].update(
            prior_fights_with_stats=0, prior_rich_stat_bouts=12, rich_stat_minutes=54.5, fight_statistics=True)
        clean = validate_report(report)
        self.assertEqual(clean['bouts'][0]['coverage']['fighter_a']['prior_rich_stat_bouts'], 12)
        self.assertIn('12 earlier archive fights with detailed statistics', render_html(report))
        self.assertIn('54.5 minutes in the selected statistics window', render_html(report))
        row = next(csv.DictReader(io.StringIO(render_csv(report))))
        self.assertEqual(row['fighter_a_prior_rich_stat_bouts'], '12')
        self.assertEqual(row['fighter_a_rich_stat_minutes'], '54.5')
        report['bouts'][0]['coverage']['fighter_a']['prior_rich_stat_bouts'] = -1
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_module_cli_renders_new_outputs_and_refuses_existing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, html_path, csv_path, json_path = [root / name for name in ("source.json", "report.html", "report.csv", "public.json")]
            source.write_text(json.dumps(public_report()), encoding="utf-8")
            self.assertEqual(main([str(source), "--html", str(html_path), "--csv", str(csv_path), "--json", str(json_path)]), 0)
            self.assertIn("<!doctype html>", html_path.read_text())
            self.assertEqual(len(list(csv.DictReader(io.StringIO(csv_path.read_text())))), 1)
            before = html_path.read_bytes()
            with self.assertRaises(SystemExit):
                with patch("sys.stderr", io.StringIO()):
                    main([str(source), "--html", str(html_path)])
            self.assertEqual(html_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
