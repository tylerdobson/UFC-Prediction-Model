"""Frozen, retrospective v4 research experiment using retained data only.

Selection reads labels through 2023-12-02 and is written before evaluation.
The already-published fixed test is a reused benchmark, not a new holdout.
Nothing here migrates a database, captures a source, or authorizes a wager.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from .features import COVERAGE_NAMES, FeatureRow
from .logistic import LogisticModel, TemperatureCalibrator, fit_logistic, fit_temperature
from .model_lab import (
    TEST_START, TRAIN_END, VALIDATION_END, VALIDATION_START, absolute_bootstrap,
    file_sha256, fixed_split, lab_rows, metric_report, read_only, write_new_json,
)
from .pipeline import utc_now, utc_string
from .research_evaluation import _digest_json, _paired_event_bootstrap, _summary


PROTOCOL = "research-v4-frozen-pretest-v1"
MODEL_VERSION = "logistic-prior-v4"
ELO_K = 64
FEATURE_SET = "age-hinge"
PRETEST_YEARS = tuple(range(2016, 2024))
EVALUATION_YEARS = tuple(range(2016, 2027))
BASELINE_ID = "v3-l2-0.02-window-expanding"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TrainingPolicy:
    """Limit fitting examples while replaying the complete earlier history."""

    l2: float = .02
    lookback_years: int | None = None
    earliest_year: int = 2010

    def __post_init__(self):
        if not math.isfinite(self.l2) or self.l2 <= 0:
            raise ValueError("Policy l2 must be finite and positive")
        if self.lookback_years is not None and (
            isinstance(self.lookback_years, bool) or not isinstance(self.lookback_years, int)
            or self.lookback_years < 1
        ):
            raise ValueError("Policy lookback must be a positive whole number of years")
        if isinstance(self.earliest_year, bool) or not isinstance(self.earliest_year, int) or not 1 <= self.earliest_year <= 9999:
            raise ValueError("Policy earliest year is invalid")

    @property
    def candidate_id(self) -> str:
        window = f"{self.lookback_years}y" if self.lookback_years else "expanding"
        return f"l2-{self.l2:g}-window-{window}"

    def lower_date(self, origin_date: str) -> str:
        origin = date.fromisoformat(origin_date)
        year = max(self.earliest_year, origin.year - self.lookback_years) if self.lookback_years else self.earliest_year
        return f"{year:04d}-01-01"


BASELINE_POLICY = TrainingPolicy()
CANDIDATE_POLICIES = tuple(TrainingPolicy(l2, years) for years in (None, 8, 5) for l2 in (.002, .02, .1))


def eligible_training_rows(rows: Sequence[FeatureRow], origin_date: str,
                           policy: TrainingPolicy) -> list[FeatureRow]:
    """Apply policy to labelled examples; feature history is never truncated."""
    lower = policy.lower_date(origin_date)
    return sorted((r for r in rows if lower <= r.event_date < origin_date),
                  key=lambda r: (r.event_date, r.event_id, r.bout_id))


def fit_policy(rows: Sequence[FeatureRow], feature_names: Sequence[str],
               origin_date: str, policy: TrainingPolicy) -> LogisticModel:
    """Fit a frozen policy on a caller's earlier, already-separated training rows."""
    return fit_logistic(eligible_training_rows(rows, origin_date, policy),
                        l2=policy.l2, feature_names=feature_names)


@dataclass(frozen=True)
class CalibratedPolicyFit:
    model: LogisticModel
    calibrator: TemperatureCalibrator
    training: tuple[FeatureRow, ...]
    calibration: tuple[FeatureRow, ...]
    policy: TrainingPolicy
    origin_date: str

    def predict_probability(self, features: Sequence[float]) -> float:
        return self.calibrator.predict_probability(self.model.predict_probability(features))


def fit_separated_policy(training: Sequence[FeatureRow], calibration: Sequence[FeatureRow],
                         feature_names: Sequence[str], origin_date: str,
                         policy: TrainingPolicy) -> CalibratedPolicyFit:
    """Accept an existing whole-date calibration split, including live sample gates."""
    train = eligible_training_rows(training, origin_date, policy)
    validation = sorted(calibration, key=lambda r: (r.event_date, r.event_id, r.bout_id))
    date.fromisoformat(origin_date)
    if not train or not validation:
        raise ValueError("Policy needs nonempty training and calibration rows")
    if train[-1].event_date >= validation[0].event_date or validation[-1].event_date >= origin_date:
        raise ValueError("Training, calibration and prediction dates must be strictly ordered")
    model = fit_logistic(train, l2=policy.l2, feature_names=feature_names)
    calibrator = fit_temperature([model.predict_probability(r.features) for r in validation],
                                 [int(r.target) for r in validation])
    return CalibratedPolicyFit(model, calibrator, tuple(train), tuple(validation), policy, origin_date)


def calibrated_policy_fit(rows: Sequence[FeatureRow], feature_names: Sequence[str],
                          origin_date: str, policy: TrainingPolicy,
                          calibration_date_fraction: float = .15) -> CalibratedPolicyFit:
    """Reserve a later whole-date tail; prospective callers can supply their own split."""
    if not 0 < calibration_date_fraction < 1:
        raise ValueError("Calibration date fraction must be between zero and one")
    eligible = eligible_training_rows(rows, origin_date, policy)
    dates = sorted({r.event_date for r in eligible})
    if len(dates) < 3:
        raise ValueError("Policy fitting requires at least three earlier event dates")
    boundary = dates[-max(1, int(len(dates) * calibration_date_fraction))]
    return fit_separated_policy([r for r in eligible if r.event_date < boundary],
                                 [r for r in eligible if r.event_date >= boundary],
                                 feature_names, origin_date, policy)


def _fit_details(run: CalibratedPolicyFit) -> dict:
    return {
        "policy": asdict(run.policy), "origin_date": run.origin_date,
        "training_window_lower_date": run.policy.lower_date(run.origin_date),
        "training": _summary(list(run.training)), "calibration": _summary(list(run.calibration)),
        "training_rows_sha256": _digest_json([[r.bout_id, r.event_date, r.features, r.target] for r in run.training]),
        "calibration_rows_sha256": _digest_json([[r.bout_id, r.event_date, r.features, r.target] for r in run.calibration]),
        "weights": run.model.weights, "weights_sha256": _digest_json(run.model.weights),
        "temperature_scale": run.calibrator.scale,
        "calibration_rule": "temperature_on_later_calibration_only_min30_identity_fallback",
    }


def _rolling(rows, names, policy, *, years=PRETEST_YEARS, predictions=False):
    folds, pooled_rows, pooled_p = [], [], []
    for year in years:
        origin = f"{year}-01-01"
        test = [r for r in rows if int(r.event_date[:4]) == year]
        if not test:
            continue
        run = calibrated_policy_fit(rows, names, origin, policy)
        p = [run.predict_probability(r.features) for r in test]
        fold = {"year": year, "fit": _fit_details(run), "test_summary": _summary(test),
                "test": metric_report(test, p)}
        if predictions:
            fold["test"]["uncertainty"] = absolute_bootstrap(test, p)
        folds.append(fold)
        pooled_rows.extend(test)
        pooled_p.extend(p)
    if not pooled_rows:
        raise ValueError("Rolling experiment needs at least one evaluation year")
    result = {"folds": folds, "pooled": metric_report(pooled_rows, pooled_p),
              "calibration_date_fraction": .15, "training_policy": asdict(policy)}
    if predictions:
        result["pooled"]["uncertainty"] = absolute_bootstrap(pooled_rows, pooled_p)
    return result, pooled_rows, pooled_p


def _signed(document: dict, key: str) -> dict:
    return {**document, key: _digest_json(document)}


def _read_document(path, digest_key):
    def invalid_constant(value):
        raise ValueError(f"Nonfinite JSON number: {value}")
    try:
        value = json.loads(Path(path).read_text(), parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("Report must be a JSON object")
        stated = value.get(digest_key)
        if not isinstance(stated, str) or stated != _digest_json({k: v for k, v in value.items() if k != digest_key}):
            raise ValueError("Report content hash does not match")
        return value
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Cannot load verified report: {exc}") from exc


def load_selection(path: str | Path, database_path: str | Path | None = None) -> dict:
    """Read a frozen manifest; optional database verification hashes bytes only."""
    selection = _read_document(path, "manifest_sha256")
    try:
        if (selection["schema_version"] != SCHEMA_VERSION or selection["protocol_version"] != PROTOCOL
                or selection["fixed_test_labels_used"] is not False
                or selection["selection_labels_through"] != VALIDATION_END
                or selection["selection_data_last_date"] > VALIDATION_END
                or selection["selection_years"] != list(PRETEST_YEARS)
                or selection["candidate_policies"] != [asdict(p) for p in CANDIDATE_POLICIES]):
            raise ValueError("Selection does not match the frozen pretest protocol")
        trials = selection["trials"]
        if len(trials) != len(CANDIDATE_POLICIES):
            raise ValueError("Selection must contain exactly the frozen nine candidates")
        for trial, policy in zip(trials, CANDIDATE_POLICIES):
            if trial["candidate_id"] != policy.candidate_id or trial["policy"] != asdict(policy):
                raise ValueError("Selection candidate policies do not match")
            loss = trial["pretest_rolling"]["pooled"]["metrics"]["log_loss"]
            if not isinstance(loss, (int, float)) or not math.isfinite(loss):
                raise ValueError("Selection loss must be finite")
            for fold in trial["pretest_rolling"]["folds"]:
                if (fold["year"] not in PRETEST_YEARS
                        or fold["fit"]["training"]["last_date"] >= fold["fit"]["calibration"]["first_date"]
                        or fold["fit"]["calibration"]["last_date"] >= f"{fold['year']}-01-01"
                        or fold["test_summary"]["last_date"] > VALIDATION_END):
                    raise ValueError("Selection folds contain out-of-protocol dates")
        selected = min(enumerate(trials), key=lambda item: (item[1]["pretest_rolling"]["pooled"]["metrics"]["log_loss"], item[0]))[1]
        if selection["selected_candidate_id"] != selected["candidate_id"] or selection["selected_policy"] != selected["policy"]:
            raise ValueError("Selected candidate is not the frozen minimum-loss candidate")
        if database_path is not None and file_sha256(database_path) != selection["database_sha256"]:
            raise ValueError("Selection database hash does not match")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Selection manifest is incomplete") from exc
    return selection


def select_upgrade(database_path: str | Path, output_path: str | Path) -> dict:
    """Exclusive-create the nine-candidate selection without reading test labels."""
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    before = file_sha256(database_path)
    with read_only(database_path) as connection:
        rows, source, names = lab_rows(connection, feature_set=FEATURE_SET, elo_k=ELO_K,
                                       through_date=VALIDATION_END)
    trials = []
    for policy in CANDIDATE_POLICIES:
        rolling, _, _ = _rolling(rows, names, policy)
        trials.append({"candidate_id": policy.candidate_id, "policy": asdict(policy), "pretest_rolling": rolling})
    selected = min(enumerate(trials), key=lambda item: (item[1]["pretest_rolling"]["pooled"]["metrics"]["log_loss"], item[0]))[1]
    manifest = _signed({
        "schema_version": SCHEMA_VERSION, "protocol_version": PROTOCOL,
        "created_at_utc": utc_string(utc_now()), "research_only": True, "promotion_eligible": False,
        "fixed_test_labels_used": False, "selection_labels_through": VALIDATION_END,
        "selection_data_last_date": max(row.event_date for row in rows),
        "selection_rule": "minimum_pooled_pretest_rolling_calibrated_log_loss_then_candidate_order",
        "selection_years": PRETEST_YEARS, "candidate_policies": [asdict(p) for p in CANDIDATE_POLICIES],
        "selected_candidate_id": selected["candidate_id"], "selected_policy": selected["policy"],
        "feature_set": FEATURE_SET, "feature_names": names, "elo_k": ELO_K,
        "feature_history_rule": "complete_earlier_result_history_replayed_in_whole_date_batches",
        "fitting_window_rule": "Jan1_max_2010_origin_year_minus_lookback_years",
        "database_sha256": before, "selection_source_summary": source,
        "implementation_sha256": file_sha256(__file__), "trials": trials,
        "limitations": ["Pretest folds were previously observed during v3 development.",
                        "Only nine declared recency/regularization candidates are compared.",
                        "The published fixed test is a reused benchmark, not a fresh holdout."],
    }, "manifest_sha256")
    if file_sha256(database_path) != before:
        raise ValueError("Database changed during read-only selection")
    write_new_json(output_path, manifest)
    return manifest


def _paired(rows, baseline, candidate):
    original = _paired_event_bootstrap(rows, baseline, candidate)
    return {"method": original["method"], "seed": original["seed"], "repeats": original["repeats"],
            "event_date_blocks": original["test_event_date_blocks"],
            "candidate_minus_baseline_log_loss_95pct_interval": original["logistic_minus_elo_log_loss_95pct_interval"],
            "candidate_minus_baseline_brier_score_95pct_interval": original["logistic_minus_elo_brier_score_95pct_interval"],
            "note": "Conditional resampling uncertainty on a reused retrospective benchmark; excludes data, identity, tuning and timing uncertainty."}


def _cohorts(rows, baseline, candidate, context):
    selectors = {
        "both_birth_dates": lambda r: r.coverage[0] and r.coverage[1],
        "missing_at_least_one_birth_date": lambda r: not (r.coverage[0] and r.coverage[1]),
        "originally_kept": lambda r: context[r.bout_id]["source"] == "wikipedia_research",
        "restored": lambda r: context[r.bout_id]["source"] == "wikipedia_research_restored",
    }
    result = {}
    for label, include in selectors.items():
        indices = [i for i, row in enumerate(rows) if include(row)]
        group = [rows[i] for i in indices]
        result[label] = {"bouts": len(group),
                         "baseline": metric_report(group, [baseline[i] for i in indices]),
                         "candidate": metric_report(group, [candidate[i] for i in indices])}
    return result


def _prediction_records(rows, baseline, candidate, context):
    return [{"bout_id": row.bout_id, "event_id": row.event_id, "event_date": row.event_date,
             "fighter_a_id": row.fighter_a_id, "fighter_b_id": row.fighter_b_id,
             "fighter_a": context[row.bout_id]["fighter_a"], "fighter_b": context[row.bout_id]["fighter_b"],
             "target_fighter_a_won": int(row.target), "baseline_probability_a": p0, "candidate_probability_a": p1,
             "coverage": dict(zip(COVERAGE_NAMES, row.coverage)),
             "source": context[row.bout_id]["source"], "features": row.features}
            for row, p0, p1 in zip(rows, baseline, candidate)]


def _comparison(rows, baseline, candidate, context):
    base_report, candidate_report = metric_report(rows, baseline), metric_report(rows, candidate)
    base_report["uncertainty"] = absolute_bootstrap(rows, baseline)
    candidate_report["uncertainty"] = absolute_bootstrap(rows, candidate)
    paired = _paired(rows, baseline, candidate)
    interval = paired["candidate_minus_baseline_log_loss_95pct_interval"]
    delta = candidate_report["metrics"]["log_loss"] - base_report["metrics"]["log_loss"]
    return {"baseline": base_report, "candidate": candidate_report,
            "candidate_minus_baseline_log_loss": delta, "paired_uncertainty": paired,
            "bootstrap_interval_excludes_zero_in_candidate_favor": interval[1] < 0,
            "coverage": {"bouts": len(rows), "fighter_instances": 2 * len(rows),
                         **{name: sum(bool(row.coverage[i]) for row in rows) for i, name in enumerate(COVERAGE_NAMES)}},
            "cohorts": _cohorts(rows, baseline, candidate, context),
            "predictions": _prediction_records(rows, baseline, candidate, context)}


def evaluate_upgrade(database_path: str | Path, selection_path: str | Path,
                     output_path: str | Path) -> dict:
    """Evaluate exactly one policy only after a verified selection exists on disk."""
    if Path(output_path).exists():
        raise FileExistsError(output_path)
    selection = load_selection(selection_path, database_path)
    if selection["implementation_sha256"] != file_sha256(__file__):
        raise ValueError("Implementation changed after selection; freeze a new experiment")
    selection_hash = file_sha256(selection_path)
    policy = TrainingPolicy(**selection["selected_policy"])
    with read_only(database_path) as connection:
        _, pretest_source, names = lab_rows(connection, feature_set=FEATURE_SET, elo_k=ELO_K,
                                           through_date=VALIDATION_END)
        if pretest_source != selection["selection_source_summary"] or list(names) != selection["feature_names"]:
            raise ValueError("Pretest feature inputs changed after selection")
        rows, source, names = lab_rows(connection, feature_set=FEATURE_SET, elo_k=ELO_K)
        context = {r["bout_id"]: dict(r) for r in connection.execute(
            """SELECT b.bout_id,b.source,fa.canonical_name AS fighter_a,fb.canonical_name AS fighter_b
               FROM bouts b JOIN fighters fa ON fa.fighter_id=b.fighter_a_id
               JOIN fighters fb ON fb.fighter_id=b.fighter_b_id""")}
    # Rows use sorted participant IDs; source cards can use the opposite order.
    with read_only(database_path) as connection:
        fighter_names = {r[0]: r[1] for r in connection.execute("SELECT fighter_id,canonical_name FROM fighters")}
    for row in rows:
        context[row.bout_id]["fighter_a"] = fighter_names[row.fighter_a_id]
        context[row.bout_id]["fighter_b"] = fighter_names[row.fighter_b_id]
    training, calibration, test = fixed_split(rows)
    runs = [fit_separated_policy(training, calibration, names, TEST_START, p)
            for p in (BASELINE_POLICY, policy)]
    fixed_p = [[run.predict_probability(row.features) for row in test] for run in runs]
    fixed = _comparison(test, *fixed_p, context)
    fixed["baseline_fit"], fixed["candidate_fit"] = map(_fit_details, runs)
    fixed["protocol"] = {"train_end": TRAIN_END, "calibration_start": VALIDATION_START,
                         "calibration_end": VALIDATION_END, "test_start": TEST_START,
                         "window_anchor": TEST_START,
                         "calibration_rule": "unchanged_prescribed_v3_fixed_validation_dates"}
    rolling_baseline, rolling_rows, base_p = _rolling(rows, names, BASELINE_POLICY,
                                                     years=EVALUATION_YEARS, predictions=True)
    rolling_candidate, candidate_rows, candidate_p = _rolling(rows, names, policy,
                                                             years=EVALUATION_YEARS, predictions=True)
    if [r.bout_id for r in rolling_rows] != [r.bout_id for r in candidate_rows]:
        raise ValueError("Rolling policies must evaluate identical bouts")
    rolling = _comparison(rolling_rows, base_p, candidate_p, context)
    rolling["baseline_folds"], rolling["candidate_folds"] = rolling_baseline["folds"], rolling_candidate["folds"]
    for base, candidate in zip(rolling["baseline_folds"], rolling["candidate_folds"]):
        if base["year"] != candidate["year"]:
            raise ValueError("Rolling folds are not aligned")
        indices = [i for i, row in enumerate(rolling_rows) if int(row.event_date[:4]) == base["year"]]
        candidate["paired_uncertainty"] = _paired([rolling_rows[i] for i in indices],
                                                  [base_p[i] for i in indices], [candidate_p[i] for i in indices])
    fixed_improved = fixed["candidate_minus_baseline_log_loss"] < 0
    champion = MODEL_VERSION if fixed_improved else "logistic-prior-v3"
    report = _signed({
        "schema_version": SCHEMA_VERSION, "protocol_version": PROTOCOL,
        "created_at_utc": utc_string(utc_now()), "research_only": True, "promotion_eligible": False,
        "fixed_test_is_reused_benchmark": True, "fresh_holdout": False,
        "selection_fixed_test_labels_used": False, "evaluation_fixed_test_labels_used": True,
        "selection_manifest_sha256": selection["manifest_sha256"],
        "selection_file_sha256": selection_hash, "selection_frozen_before_evaluation": True,
        "database_sha256": selection["database_sha256"], "source_summary": source,
        "implementation_sha256": selection["implementation_sha256"],
        "feature_names": names, "feature_set": FEATURE_SET, "elo_k": ELO_K,
        "baseline_model_version": "logistic-prior-v3", "candidate_model_version": MODEL_VERSION,
        "selected_policy": asdict(policy), "baseline_policy": asdict(BASELINE_POLICY),
        "research_champion": champion, "candidate_fixed_benchmark_improved": fixed_improved,
        "champion_rule": "retain_v3_if_selected_candidate_fixed_benchmark_log_loss_is_not_lower",
        "fixed_test": fixed, "rolling_origin": rolling,
        "limitations": [
            "Retrospective source history and immutable profiles are not historical pre-fight observation evidence.",
            "Both pretest development folds and the fixed benchmark were previously reported; neither is a fresh holdout.",
            "Selection is restricted to pre-2023-12-09 labels; fixed-test evaluation does not trigger reselection.",
            "Recency limits fitting examples, never the earlier history used to build features.",
            "The champion label chooses a research policy; it does not establish live edge, profitability or operating promotion.",
        ],
    }, "report_content_sha256")
    if file_sha256(database_path) != selection["database_sha256"] or file_sha256(selection_path) != selection_hash:
        raise ValueError("An experiment input changed during read-only evaluation")
    write_new_json(output_path, report)
    return report


def load_verified_report(report_path: str | Path, selection_path: str | Path,
                          database_path: str | Path | None = None) -> dict:
    """Read-only app loader validating hashes, policy and prediction alignment."""
    selection = load_selection(selection_path, database_path)
    report = _read_document(report_path, "report_content_sha256")
    try:
        if (report["schema_version"] != SCHEMA_VERSION or report["protocol_version"] != PROTOCOL
                or report["research_only"] is not True or report["promotion_eligible"] is not False
                or report["fixed_test_is_reused_benchmark"] is not True or report["fresh_holdout"] is not False
                or report["selection_fixed_test_labels_used"] is not False
                or report["evaluation_fixed_test_labels_used"] is not True
                or report["selection_frozen_before_evaluation"] is not True
                or report["selection_manifest_sha256"] != selection["manifest_sha256"]
                or report["selection_file_sha256"] != file_sha256(selection_path)
                or report["database_sha256"] != selection["database_sha256"]
                or report["implementation_sha256"] != selection["implementation_sha256"]
                or report["selected_policy"] != selection["selected_policy"]
                or report["baseline_policy"] != asdict(BASELINE_POLICY)
                or report["elo_k"] != ELO_K or report["feature_set"] != FEATURE_SET
                or report["feature_names"] != selection["feature_names"]):
            raise ValueError("Evaluation inputs do not match the frozen selection")
        for section in (report["fixed_test"], report["rolling_origin"]):
            records = section["predictions"]
            if len(records) != section["candidate"]["metrics"]["bouts"] or len(records) != section["baseline"]["metrics"]["bouts"]:
                raise ValueError("Prediction counts do not match reported metrics")
            if len({row["bout_id"] for row in records}) != len(records):
                raise ValueError("Duplicate prediction bouts")
            for row in records:
                date.fromisoformat(row["event_date"])
                if row["target_fighter_a_won"] not in (0, 1):
                    raise ValueError("Prediction targets must be binary")
                for key in ("baseline_probability_a", "candidate_probability_a"):
                    value = row[key]
                    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError("Prediction probabilities must be finite and bounded")
                if (len(row["features"]) != len(report["feature_names"])
                        or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in row["features"])
                        or any(not isinstance(row["coverage"][name], bool) for name in COVERAGE_NAMES)):
                    raise ValueError("Prediction features or coverage are invalid")
            reconstructed = [FeatureRow(row["bout_id"], row["event_id"], row["event_date"],
                                        row["fighter_a_id"], row["fighter_b_id"], tuple(row["features"]),
                                        row["target_fighter_a_won"], tuple(row["coverage"][name] for name in COVERAGE_NAMES))
                             for row in records]
            for label, probability_key in (("baseline", "baseline_probability_a"), ("candidate", "candidate_probability_a")):
                measured = metric_report(reconstructed, [row[probability_key] for row in records])
                if any(section[label].get(key) != value for key, value in measured.items()):
                    raise ValueError("Saved metrics do not match the retained bout predictions")
            measured_delta = section["candidate"]["metrics"]["log_loss"] - section["baseline"]["metrics"]["log_loss"]
            if section["candidate_minus_baseline_log_loss"] != measured_delta:
                raise ValueError("Saved loss improvement does not match the retained predictions")
        delta = report["fixed_test"]["candidate_minus_baseline_log_loss"]
        expected = MODEL_VERSION if delta < 0 else "logistic-prior-v3"
        if report["research_champion"] != expected or report["candidate_fixed_benchmark_improved"] != (delta < 0):
            raise ValueError("Research champion does not follow the frozen decision rule")
    except (KeyError, TypeError) as exc:
        raise ValueError("Evaluation report is incomplete") from exc
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="Freeze the pretest-only nine-candidate manifest")
    select.add_argument("--db", required=True)
    select.add_argument("--out", required=True)
    evaluate = commands.add_parser("evaluate", help="Evaluate one already-frozen policy on reused benchmarks")
    evaluate.add_argument("--db", required=True)
    evaluate.add_argument("--selection", required=True)
    evaluate.add_argument("--out", required=True)
    verify = commands.add_parser("verify", help="Read and validate saved experiment inputs")
    verify.add_argument("--report", required=True)
    verify.add_argument("--selection", required=True)
    verify.add_argument("--db")
    args = parser.parse_args(argv)
    if args.command == "select":
        result = select_upgrade(args.db, args.out)
        print(f"Frozen selection: {result['selected_candidate_id']}")
    elif args.command == "evaluate":
        result = evaluate_upgrade(args.db, args.selection, args.out)
        fixed = result["fixed_test"]
        print(f"Reused fixed benchmark loss: v3={fixed['baseline']['metrics']['log_loss']:.6f}, v4={fixed['candidate']['metrics']['log_loss']:.6f}")
        print(f"Research champion: {result['research_champion']}; operating promotion remains unavailable")
    else:
        result = load_verified_report(args.report, args.selection, args.db)
        print(f"Verified retrospective report: {result['report_content_sha256']}")


if __name__ == "__main__":
    main()
