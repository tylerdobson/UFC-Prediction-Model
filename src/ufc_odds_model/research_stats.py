"""V5 research replay from retained, version-pinned per-fight UFCStats CSVs.

Archive reach is applied retrospectively as an approximate static
attribute. Fight statistics enter history only after their own whole event
date. Current career averages are never read. These are research features,
not proof of historical source availability or operating readiness.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

from .features import COVERAGE_NAMES, FeatureBuilder, FeatureRow, _Profile
from .logistic import V3_FEATURE_NAMES, v3_features
from .model_lab import TEST_START, VALIDATION_END, file_sha256, fixed_split, lab_rows, metric_report, read_only, write_new_json
from .pipeline import parse_utc, utc_now, utc_string
from .research_evaluation import _digest_json, _source_results
from .research_upgrade import (
    EVALUATION_YEARS, PRETEST_YEARS, TrainingPolicy, _comparison, _fit_details,
    _paired, _read_document, _rolling, _signed, fit_separated_policy,
)
from .wikidata_profiles import static_profiles
from .ufcstats import UFCStatsParseError, _source_identity


PROTOCOL = "research-v5-single-stats-candidate-v1"
MODEL_VERSION = "logistic-prior-v5"
POLICY = TrainingPolicy(.002, 8)
MIN_COMPLETE_BOUTS = 100
MIN_COMPLETE_SHARE = .25
SOURCE_FILES = ("ufc_event_details.csv", "ufc_fight_results.csv", "ufc_fight_stats.csv", "ufc_fighter_tott.csv")


def _name(value):
    """Exact full names after Unicode, case and whitespace normalization only."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _csv_rows(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


@dataclass(frozen=True)
class ResearchStatsOverlay:
    profiles: dict[str, _Profile]
    stats: dict[tuple[str, str], tuple[int, int]]
    reach_sources: dict[str, str]
    stat_sources: dict[str, str]
    source_summary: dict
    captured_at_utc: str
    database_sha256: str
    overlay_inputs_sha256: str
    through_date: str | None = None


def load_research_stats(connection: sqlite3.Connection, manifest_path: str | Path,
                        *, through_date: str | None = None) -> ResearchStatsOverlay:
    """Verify raw bytes and conservatively join exact date/pair/outcome records.

    No fuzzy names, inferred aliases or identity merges are used. Ambiguous,
    conflicting, incomplete-round and outcome-disagreeing rows are held out.
    The pretest selector supplies through_date before consulting result labels.
    """
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported statistics archive manifest")
    captured = manifest.get("captured_at_utc")
    parse_utc(captured)
    if through_date is not None:
        date.fromisoformat(through_date)
    files = {entry["file"]: entry for entry in manifest["files"]}
    for name in SOURCE_FILES:
        entry = files.get(name)
        if not entry or Path(name).name != name or file_sha256(manifest_path.parent / name) != entry["sha256"]:
            raise ValueError(f"Missing or changed statistics source: {name}")
    event_dates = defaultdict(set)
    for row in _csv_rows(manifest_path.parent / SOURCE_FILES[0]):
        event_dates[_name(row["EVENT"])].add(datetime.strptime(row["DATE"], "%B %d, %Y").date().isoformat())
    archive = defaultdict(list)
    rejected = Counter()
    for row in _csv_rows(manifest_path.parent / SOURCE_FILES[1]):
        dates = event_dates.get(_name(row["EVENT"]), set())
        if len(dates) != 1:
            rejected["unknown_or_ambiguous_event_date"] += 1
            continue
        event_date = next(iter(dates))
        if through_date and event_date > through_date:
            continue
        names = re.split(r"\s+vs\.\s+", row["BOUT"].strip())
        if len(names) != 2 or len(set(map(_name, names))) != 2:
            rejected["invalid_fighter_pair"] += 1
            continue
        archive[(event_date, frozenset(map(_name, names)))].append((row, names))
    rounds = defaultdict(dict)
    invalid_stats = set()
    for row in _csv_rows(manifest_path.parent / SOURCE_FILES[2]):
        key = (_name(row["EVENT"]), _name(row["BOUT"]))
        round_match = re.fullmatch(r"Round ([1-9][0-9]*)", row["ROUND"].strip())
        count_match = re.fullmatch(r"([0-9]+) of ([0-9]+)", row["SIG.STR."].strip())
        if not round_match or not count_match:
            invalid_stats.add(key)
            continue
        landed, attempted = map(int, count_match.groups())
        if landed > attempted:
            invalid_stats.add(key)
            continue
        round_key = (_name(row["FIGHTER"]), int(round_match[1]))
        if round_key in rounds[key]:
            invalid_stats.add(key)
        rounds[key][round_key] = (landed, attempted)
    fighters = {str(row[0]): str(row[1]) for row in connection.execute("SELECT fighter_id,canonical_name FROM fighters")}
    result_rows = _source_results(connection, include_restored=True, through_date=through_date)
    research_by_pair = Counter((row["event_date"], frozenset((_name(fighters[row["fighter_a_id"]]),
                                                               _name(fighters[row["fighter_b_id"]])))) for row in result_rows)
    paired_stats, stat_sources = {}, {}
    matched = 0
    for result in result_rows:
        ids = (str(result["fighter_a_id"]), str(result["fighter_b_id"]))
        lookup = (result["event_date"], frozenset(_name(fighters[f]) for f in ids))
        matches = archive.get(lookup, [])
        if len(matches) != 1 or research_by_pair[lookup] != 1:
            rejected["unmatched_or_ambiguous_research_pair"] += 1
            continue
        archived, names = matches[0]
        by_name = {_name(fighters[f]): f for f in ids}
        a_id, b_id = (by_name[_name(name)] for name in names)
        outcome = archived["OUTCOME"].strip()
        archive_outcome = "win" if outcome in {"W/L", "L/W"} else "draw" if outcome == "D/D" else "no_contest" if outcome == "NC/NC" else None
        winner = a_id if outcome == "W/L" else b_id if outcome == "L/W" else None
        if archive_outcome != result["outcome"] or winner != result["winner_fighter_id"]:
            rejected["outcome_disagreement"] += 1
            continue
        try:
            _source_identity(archived["URL"].strip(), "fight")
        except UFCStatsParseError:
            rejected["invalid_official_fight_url"] += 1
            continue
        matched += 1
        key = (_name(archived["EVENT"]), _name(archived["BOUT"]))
        try:
            count = int(archived["ROUND"].strip())
        except ValueError:
            count = 0
        expected = {(_name(name), number) for name in names for number in range(1, count + 1)}
        if key in invalid_stats or not count or set(rounds[key]) != expected:
            rejected["missing_or_incomplete_paired_round_stats"] += 1
            continue
        for name, fighter_id in zip(names, (a_id, b_id)):
            values = [rounds[key][_name(name), number] for number in range(1, count + 1)]
            paired_stats[str(result["bout_id"]), fighter_id] = tuple(sum(v[i] for v in values) for i in (0, 1))
        stat_sources[str(result["bout_id"])] = archived["URL"].strip()
    reach_rows = defaultdict(list)
    for row in _csv_rows(manifest_path.parent / SOURCE_FILES[3]):
        reach_rows[_name(row["FIGHTER"])].append(row)
    research_names = Counter(_name(value) for value in fighters.values())
    profiles, reach_sources = {}, {}
    for fighter_id, name in fighters.items():
        matches = reach_rows.get(_name(name), [])
        if not fighter_id.startswith("wikipedia:") or len(matches) != 1 or research_names[_name(name)] != 1:
            continue
        value = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)"', matches[0]["REACH"].strip())
        if not value:
            continue
        cm = float(value[1]) * 2.54
        if not 100 <= cm <= 250:
            continue
        try:
            _source_identity(matches[0]["URL"].strip(), "fighter")
        except UFCStatsParseError:
            continue
        profiles[fighter_id] = _Profile(reach_cm=cm)
        reach_sources[fighter_id] = matches[0]["URL"].strip()
    database_file = connection.execute("PRAGMA database_list").fetchone()[2]
    database_hash = file_sha256(database_file) if database_file else _digest_json([tuple(r) for r in result_rows])
    inputs_hash = _digest_json({"stats": sorted((bout, fighter, *value) for (bout, fighter), value in paired_stats.items()),
                               "reach": sorted((fighter, profile.reach_cm) for fighter, profile in profiles.items())})
    summary = {
        "source": manifest["source"], "repository_url": manifest["repository_url"],
        "repository_commit": manifest["repository_commit"], "captured_at_utc": captured,
        "source_manifest_sha256": file_sha256(manifest_path), "raw_files": [files[name] for name in SOURCE_FILES],
        "through_date": through_date, "research_result_bouts": len(result_rows),
        "exact_pair_date_outcome_matches": matched, "paired_stat_bouts": len(stat_sources),
        "fighter_profiles_with_reach": len(profiles), "held": dict(sorted(rejected.items())),
        "identity_rule": "exact_NFKC_casefold_whitespace_full_names_and_date_and_outcome_no_alias_merges",
        "round_rule": "both_fighters_all_rounds_1_through_recorded_finish_round",
        "reach_rule": "current_archive_reach_applied_retrospectively_as_approximate_static_attribute",
        "historical_source_availability": "retrospective_capture_not_prefight_observation",
    }
    return ResearchStatsOverlay(profiles, paired_stats, reach_sources, stat_sources, summary,
                                captured, database_hash, inputs_hash, through_date)


def merge_reach_profiles(profiles: dict[str, _Profile], overlay: ResearchStatsOverlay) -> dict[str, _Profile]:
    merged = dict(profiles)
    for fighter, profile in overlay.profiles.items():
        merged[fighter] = _Profile(merged.get(fighter, _Profile()).birth_date, profile.reach_cm)
    return merged


def stats_feature_rows(connection, overlay: ResearchStatsOverlay, *, elo_k=64, through_date=None):
    """Replay per-fight counts strictly after each target date's features exist."""
    if overlay.through_date and (through_date is None or through_date > overlay.through_date):
        raise ValueError("Statistics overlay does not cover the requested feature replay dates")
    profiles = merge_reach_profiles(static_profiles(connection, research=True), overlay)
    source = _source_results(connection, include_restored=True, through_date=through_date)
    builder, rows = FeatureBuilder(elo_k=elo_k), []
    for day, grouped in groupby(source, key=lambda row: str(row["event_date"])):
        group = list(grouped)
        for result in group:
            if result["outcome"] != "win":
                continue
            a, b = sorted((str(result["fighter_a_id"]), str(result["fighter_b_id"])))
            values, coverage = builder.values_with_coverage(a, b, day, profiles)
            hinge = 0.
            if coverage[0] and coverage[1]:
                event = date.fromisoformat(day)
                age_a = (event - profiles[a].birth_date).days / 365.2425
                age_b = (event - profiles[b].birth_date).days / 365.2425
                hinge = (max(age_a - 32, 0) - max(age_b - 32, 0)) / 5
            rows.append(FeatureRow(str(result["bout_id"]), str(result["event_id"]), day, a, b,
                                   v3_features(values, hinge), int(result["winner_fighter_id"] == a), coverage,
                                   builder.prior_result_rows_available))
        builder.update_group(group, overlay.stats)
        builder.prior_result_rows_available += sum(r["outcome"] != "no_contest" for r in group)
    summary = {"accepted_bouts_with_results": len(source), "binary_bouts": len(rows),
               "feature_first_date": min((r.event_date for r in rows), default=None),
               "feature_last_date": max((r.event_date for r in rows), default=None),
               "retrospective_feature_rows_sha256": _digest_json([[r.bout_id, r.event_date, r.features, r.target] for r in rows]),
               "overlay_inputs_sha256": overlay.overlay_inputs_sha256, "statistics_archive": overlay.source_summary}
    return rows, summary, V3_FEATURE_NAMES


def _complete(row):
    return all(row.coverage[i] for i in (2, 3, 4, 5))


def _coverage_comparison(rows, baseline, candidate):
    result = {}
    for name, include in (("both_reach_and_prior_paired_stats", _complete),
                          ("incomplete_reach_or_prior_stats", lambda row: not _complete(row))):
        indices = [i for i, row in enumerate(rows) if include(row)]
        group = [rows[i] for i in indices]
        p0, p1 = [baseline[i] for i in indices], [candidate[i] for i in indices]
        result[name] = {"bouts": len(group), "share": len(group) / len(rows) if rows else 0,
                        "baseline": metric_report(group, p0), "candidate": metric_report(group, p1)}
        if group:
            result[name]["paired_uncertainty"] = _paired(group, p0, p1)
    return result


def _context(connection, rows):
    fighters = {r[0]: r[1] for r in connection.execute("SELECT fighter_id,canonical_name FROM fighters")}
    sources = {r[0]: r[1] for r in connection.execute("SELECT bout_id,source FROM bouts")}
    return {r.bout_id: {"fighter_a": fighters[r.fighter_a_id], "fighter_b": fighters[r.fighter_b_id],
                        "source": sources[r.bout_id]} for r in rows}


def _aligned(base, candidate):
    if [(r.bout_id, r.target) for r in base] != [(r.bout_id, r.target) for r in candidate]:
        raise ValueError("V4 and V5 must evaluate identical bout IDs and outcomes")


def select_stats_upgrade(database_path, archive_manifest_path, output_path, *, coverage_feasibility_audit=None):
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    with read_only(database_path) as connection:
        overlay = load_research_stats(connection, archive_manifest_path, through_date=VALIDATION_END)
        rows, source, names = stats_feature_rows(connection, overlay, through_date=VALIDATION_END)
        baseline_rows, _, baseline_names = lab_rows(connection, feature_set="age-hinge", elo_k=64, through_date=VALIDATION_END)
        _aligned(baseline_rows, rows)
    base, base_rows, p0 = _rolling(baseline_rows, baseline_names, POLICY)
    candidate, selected_rows, p1 = _rolling(rows, names, POLICY)
    _aligned(base_rows, selected_rows)
    coverage = _coverage_comparison(selected_rows, p0, p1)
    complete = coverage["both_reach_and_prior_paired_stats"]
    improved = candidate["pooled"]["metrics"]["log_loss"] < base["pooled"]["metrics"]["log_loss"]
    selection = _signed({
        "schema_version": 1, "protocol_version": PROTOCOL, "created_at_utc": utc_string(utc_now()),
        "research_only": True, "promotion_eligible": False, "fixed_test_labels_used": False,
        "selection_labels_through": VALIDATION_END, "candidate_count": 1,
        "candidate_model_version": MODEL_VERSION, "baseline_model_version": "logistic-prior-v4",
        "selected_policy": asdict(POLICY), "elo_k": 64, "feature_names": names,
        "database_sha256": file_sha256(database_path),
        "source_manifest_sha256": file_sha256(archive_manifest_path), "overlay_inputs_sha256": overlay.overlay_inputs_sha256,
        "implementation_sha256": file_sha256(__file__), "source_summary": source,
        "pretest": {"baseline": base, "candidate": candidate, "cohorts": coverage,
                    "candidate_minus_baseline_log_loss": candidate["pooled"]["metrics"]["log_loss"] - base["pooled"]["metrics"]["log_loss"]},
        "pretest_improved": improved, "minimum_complete_bouts": MIN_COMPLETE_BOUTS,
        "minimum_complete_share": MIN_COMPLETE_SHARE,
        "pretest_coverage_sufficient": complete["bouts"] >= MIN_COMPLETE_BOUTS and complete["share"] >= MIN_COMPLETE_SHARE,
        "coverage_design_provenance": {
            "initial_feasibility_share_target": .5, "frozen_minimum_complete_share": MIN_COMPLETE_SHARE,
            "change_timing": "before_inspecting_any_v5_pretest_or_fixed_prediction_loss",
            "reason": "Exact identity and complete-round source matching limits coverage; evaluate all bouts and disclose a substantial matched subset rather than filling missing data.",
            "full_fixed_roster_ingestion_audit": coverage_feasibility_audit,
            "audit_note": "Source reconciliation and coverage feasibility were audited on the existing benchmark roster; no fixed-test model loss was used to choose this candidate or threshold.",
        },
        "promotion_rule": "single_frozen_candidate_requires_lower_pretest_and_fixed_loss_and_minimum_complete_coverage_in_both",
        "feature_rules": ["Complete earlier result history; paired per-fight round statistics update only after their event date.",
                          "Current career aggregates are never model inputs.",
                          "Archive reach is an approximate retrospective static feature, not historical measurement evidence."],
    }, "manifest_sha256")
    write_new_json(output_path, selection)
    return selection


def load_stats_selection(selection_path, database_path=None, archive_manifest_path=None):
    selection = _read_document(selection_path, "manifest_sha256")
    try:
        if (selection["schema_version"] != 1 or selection["protocol_version"] != PROTOCOL or selection["candidate_count"] != 1
                or selection["fixed_test_labels_used"] is not False or selection["selection_labels_through"] != VALIDATION_END
                or selection["source_summary"]["feature_last_date"] > VALIDATION_END
                or selection["selected_policy"] != asdict(POLICY)
                or selection["minimum_complete_bouts"] != MIN_COMPLETE_BOUTS
                or selection["minimum_complete_share"] != MIN_COMPLETE_SHARE):
            raise ValueError("Statistics selection differs from the frozen single-candidate protocol")
        base = selection["pretest"]["baseline"]["pooled"]["metrics"]
        candidate = selection["pretest"]["candidate"]["pooled"]["metrics"]
        complete = selection["pretest"]["cohorts"]["both_reach_and_prior_paired_stats"]
        if (base["bouts"] != candidate["bouts"]
                or selection["pretest_improved"] != (candidate["log_loss"] < base["log_loss"])
                or selection["pretest_coverage_sufficient"] != (complete["bouts"] >= MIN_COMPLETE_BOUTS and complete["share"] >= MIN_COMPLETE_SHARE)):
            raise ValueError("Statistics pretest decision does not follow its frozen rule")
        for model in ("baseline", "candidate"):
            for fold in selection["pretest"][model]["folds"]:
                if (fold["year"] not in PRETEST_YEARS or fold["test_summary"]["last_date"] > VALIDATION_END
                        or fold["fit"]["training"]["last_date"] >= fold["fit"]["calibration"]["first_date"]
                        or fold["fit"]["calibration"]["last_date"] >= f"{fold['year']}-01-01"):
                    raise ValueError("Statistics selection fold violates chronological boundaries")
        for key, path in (("database_sha256", database_path), ("source_manifest_sha256", archive_manifest_path)):
            if path is not None and file_sha256(path) != selection[key]:
                raise ValueError("Statistics selection input hash mismatch")
    except (KeyError, TypeError) as exc:
        raise ValueError("Incomplete statistics selection") from exc
    return selection


def evaluate_stats_upgrade(database_path, archive_manifest_path, selection_path, output_path):
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    selection = load_stats_selection(selection_path, database_path, archive_manifest_path)
    if selection["implementation_sha256"] != file_sha256(__file__):
        raise ValueError("Statistics implementation changed after selection")
    with read_only(database_path) as connection:
        pretest_overlay = load_research_stats(connection, archive_manifest_path, through_date=VALIDATION_END)
        if pretest_overlay.overlay_inputs_sha256 != selection["overlay_inputs_sha256"]:
            raise ValueError("Pretest statistics inputs changed after selection")
        overlay = load_research_stats(connection, archive_manifest_path)
        rows, source, names = stats_feature_rows(connection, overlay)
        base_rows, _, base_names = lab_rows(connection, feature_set="age-hinge", elo_k=64)
        context = _context(connection, rows)
    _aligned(base_rows, rows)
    train, calibration, test = fixed_split(rows)
    base_train, base_calibration, base_test = fixed_split(base_rows)
    runs = (fit_separated_policy(base_train, base_calibration, base_names, TEST_START, POLICY),
            fit_separated_policy(train, calibration, names, TEST_START, POLICY))
    p0 = [runs[0].predict_probability(r.features) for r in base_test]
    p1 = [runs[1].predict_probability(r.features) for r in test]
    fixed = _comparison(test, p0, p1, context)
    fixed["coverage_cohorts"] = _coverage_comparison(test, p0, p1)
    fixed["baseline_fit"], fixed["candidate_fit"] = map(_fit_details, runs)
    base, base_rolling_rows, p0 = _rolling(base_rows, base_names, POLICY, years=EVALUATION_YEARS)
    candidate, rolling_rows, p1 = _rolling(rows, names, POLICY, years=EVALUATION_YEARS)
    _aligned(base_rolling_rows, rolling_rows)
    rolling = _comparison(rolling_rows, p0, p1, context)
    rolling["coverage_cohorts"] = _coverage_comparison(rolling_rows, p0, p1)
    rolling["baseline_folds"], rolling["candidate_folds"] = base["folds"], candidate["folds"]
    rolling["predictions_sha256"] = _digest_json(rolling.pop("predictions"))
    complete = fixed["coverage_cohorts"]["both_reach_and_prior_paired_stats"]
    sufficient = complete["bouts"] >= MIN_COMPLETE_BOUTS and complete["share"] >= MIN_COMPLETE_SHARE
    fixed_improved = fixed["candidate_minus_baseline_log_loss"] < 0
    accepted = selection["pretest_improved"] and selection["pretest_coverage_sufficient"] and sufficient and fixed_improved
    report = _signed({
        "schema_version": 1, "protocol_version": PROTOCOL, "created_at_utc": utc_string(utc_now()),
        "research_only": True, "promotion_eligible": False,
        "fixed_test_is_reused_benchmark": True, "fresh_holdout": False,
        "selection_fixed_test_labels_used": False, "selection_frozen_before_evaluation": True,
        "selection_manifest_sha256": selection["manifest_sha256"], "selection_file_sha256": file_sha256(selection_path),
        "source_manifest_sha256": file_sha256(archive_manifest_path), "database_sha256": file_sha256(database_path),
        "implementation_sha256": selection["implementation_sha256"], "source_summary": source,
        "feature_names": names, "elo_k": 64, "selected_policy": asdict(POLICY), "baseline_policy": asdict(POLICY),
        "baseline_model_version": "logistic-prior-v4", "candidate_model_version": MODEL_VERSION,
        "research_champion": MODEL_VERSION if accepted else "logistic-prior-v4",
        "candidate_fixed_benchmark_improved": fixed_improved, "pretest_improved": selection["pretest_improved"],
        "fixed_coverage_sufficient": sufficient, "pretest_coverage_sufficient": selection["pretest_coverage_sufficient"],
        "fixed_test": fixed, "rolling_origin": rolling,
        "limitations": ["The fixed benchmark is reused; rolling includes development years.",
                        "The initial 50% complete-coverage feasibility target was lowered to 25% before prediction losses were inspected; exact-source ingestion gaps remain.",
                        "Current archive reach is backcast approximately; historical measurement dates are unknown.",
                        "Only exact date/pair/outcome matches with complete paired rounds supply fight statistics.",
                        "Missing optional evidence remains neutral; coverage is reported on identical evaluation bouts.",
                        "No current career average enters the fit or forecast. Neither the archive nor this evaluation proves a live edge."],
    }, "report_content_sha256")
    if file_sha256(database_path) != selection["database_sha256"] or file_sha256(archive_manifest_path) != selection["source_manifest_sha256"]:
        raise ValueError("Statistics experiment input changed during evaluation")
    write_new_json(output_path, report)
    return report


def load_verified_stats_report(report_path, selection_path, database_path=None, archive_manifest_path=None):
    selection = load_stats_selection(selection_path, database_path, archive_manifest_path)
    report = _read_document(report_path, "report_content_sha256")
    try:
        if (report["schema_version"] != 1 or report["protocol_version"] != PROTOCOL or report["research_only"] is not True
                or report["promotion_eligible"] is not False or report["fresh_holdout"] is not False
                or report["fixed_test_is_reused_benchmark"] is not True
                or report["selection_fixed_test_labels_used"] is not False
                or report["selection_frozen_before_evaluation"] is not True
                or report["selection_manifest_sha256"] != selection["manifest_sha256"]
                or report["selection_file_sha256"] != file_sha256(selection_path)
                or report["database_sha256"] != selection["database_sha256"]
                or report["source_manifest_sha256"] != selection["source_manifest_sha256"]
                or report["selected_policy"] != asdict(POLICY)):
            raise ValueError("Statistics evaluation does not match its frozen selection")
        for section in (report["fixed_test"], report["rolling_origin"]):
            if section["baseline"]["metrics"]["bouts"] != section["candidate"]["metrics"]["bouts"]:
                raise ValueError("Statistics comparator bout counts differ")
            delta = section["candidate"]["metrics"]["log_loss"] - section["baseline"]["metrics"]["log_loss"]
            if delta != section["candidate_minus_baseline_log_loss"]:
                raise ValueError("Statistics comparator loss difference is inconsistent")
        fixed = report["fixed_test"]
        records = fixed["predictions"]
        if len(records) != fixed["candidate"]["metrics"]["bouts"] or len({r["bout_id"] for r in records}) != len(records):
            raise ValueError("Statistics fixed predictions are missing or duplicated")
        reconstructed = [FeatureRow(r["bout_id"],r["event_id"],r["event_date"],r["fighter_a_id"],r["fighter_b_id"],
                                    tuple(r["features"]),r["target_fighter_a_won"],
                                    tuple(r["coverage"][name] for name in COVERAGE_NAMES)) for r in records]
        for label, key in (("baseline","baseline_probability_a"),("candidate","candidate_probability_a")):
            measured = metric_report(reconstructed,[r[key] for r in records])
            if any(fixed[label].get(field) != value for field,value in measured.items()):
                raise ValueError("Statistics saved metrics do not match retained fixed predictions")
        complete_count = sum(_complete(r) for r in reconstructed)
        complete_share = complete_count/len(reconstructed)
        cohort = fixed["coverage_cohorts"]["both_reach_and_prior_paired_stats"]
        if cohort["bouts"] != complete_count or cohort["share"] != complete_share:
            raise ValueError("Statistics complete-coverage cohort does not match retained predictions")
        sufficient = complete_count >= MIN_COMPLETE_BOUTS and complete_share >= MIN_COMPLETE_SHARE
        if report["fixed_coverage_sufficient"] != sufficient:
            raise ValueError("Statistics fixed coverage decision is inconsistent")
        for label in ("baseline", "candidate"):
            folds = report["rolling_origin"][label+"_folds"]
            total = sum(fold["test"]["metrics"]["bouts"] for fold in folds)
            pooled = report["rolling_origin"][label]["metrics"]
            if total != pooled["bouts"]:
                raise ValueError("Statistics rolling fold counts do not match the pooled result")
            for key in ("accuracy","log_loss","brier_score"):
                value = sum(fold["test"]["metrics"][key]*fold["test"]["metrics"]["bouts"] for fold in folds)/total
                if not math.isclose(value,pooled[key],rel_tol=0,abs_tol=1e-12):
                    raise ValueError("Statistics pooled result does not match yearly folds")
        accepted = (selection["pretest_improved"] and selection["pretest_coverage_sufficient"]
                    and report["fixed_coverage_sufficient"] and report["fixed_test"]["candidate_minus_baseline_log_loss"] < 0)
        if report["research_champion"] != (MODEL_VERSION if accepted else "logistic-prior-v4"):
            raise ValueError("Statistics champion violates the frozen acceptance rule")
    except (KeyError, TypeError) as exc:
        raise ValueError("Incomplete statistics evaluation") from exc
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("select", "evaluate"):
        p = sub.add_parser(command)
        p.add_argument("--db", required=True)
        p.add_argument("--archive", required=True)
        p.add_argument("--out", required=True)
        if command == "select":
            p.add_argument("--coverage-audit-bouts", type=int)
            p.add_argument("--coverage-audit-total", type=int)
        if command == "evaluate":
            p.add_argument("--selection", required=True)
    args = parser.parse_args(argv)
    if args.command == "select":
        audit = None
        if args.coverage_audit_bouts is not None or args.coverage_audit_total is not None:
            if (args.coverage_audit_bouts is None or args.coverage_audit_total is None
                    or not 0 <= args.coverage_audit_bouts <= args.coverage_audit_total or not args.coverage_audit_total):
                parser.error("Coverage audit requires ordered nonnegative bouts and positive total")
            audit = {"complete_bouts": args.coverage_audit_bouts, "total_bouts": args.coverage_audit_total,
                     "complete_share": args.coverage_audit_bouts / args.coverage_audit_total,
                     "source": "pre-loss exact-date-pair-outcome archive reconciliation audit"}
        report = select_stats_upgrade(args.db, args.archive, args.out, coverage_feasibility_audit=audit)
        print("Frozen one V5 candidate; pretest loss difference:", report["pretest"]["candidate_minus_baseline_log_loss"])
    else:
        report = evaluate_stats_upgrade(args.db, args.archive, args.selection, args.out)
        print("Research champion:", report["research_champion"])
        print("Reused fixed benchmark:", report["fixed_test"]["baseline"]["metrics"], report["fixed_test"]["candidate"]["metrics"])


if __name__ == "__main__":
    main()
