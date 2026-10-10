"""Fixed-date comparisons for retrospective research; never authorize wagers."""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from dataclasses import replace
from datetime import date
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import quote

from .evaluation import _bookmaker_probability
from .features import FEATURE_NAMES, FeatureRow
from .logistic import V3_FEATURE_NAMES, binary_metrics, fit_logistic, fit_temperature, v3_features
from .pipeline import parse_utc, utc_now, utc_string
from .research_evaluation import (
    _calibration_bins, _digest_json, _held_bouts_by_event,
    _paired_event_bootstrap, _summary, research_feature_rows,
)

TRAIN_END = "2021-03-13"
VALIDATION_START = "2021-03-20"
VALIDATION_END = "2023-12-02"
TEST_START = "2023-12-09"
PROTOCOL_VERSION = "research-fixed-dates-v1"


@contextmanager
def read_only(path: str | Path):
    """Read even a frozen old-schema research DB without running migrations."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Database does not exist: {path}")
    with closing(sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.rollback()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fixed_split(rows: list[FeatureRow]):
    """Never move date boundaries when restored fights change composition."""
    train, validation, test = [], [], []
    for row in rows:
        if row.event_date <= TRAIN_END:
            train.append(row)
        elif VALIDATION_START <= row.event_date <= VALIDATION_END:
            validation.append(row)
        elif row.event_date >= TEST_START:
            test.append(row)
        else:
            raise ValueError(f"Bout {row.bout_id} is in an unspecified split gap")
    if not train or not validation or not test:
        raise ValueError("Fixed-date protocol needs nonempty train, validation and test splits")
    return train, validation, test


def hash_oriented_pairs(rows: list[FeatureRow], probabilities: list[float]):
    if len(rows) != len(probabilities):
        raise ValueError("Rows and probabilities must align")
    oriented_p, oriented_y = [], []
    for row, probability in zip(rows, probabilities):
        flip = int(hashlib.sha256(row.bout_id.encode()).hexdigest(), 16) & 1
        oriented_p.append(1 - probability if flip else probability)
        oriented_y.append(1 - int(row.target) if flip else int(row.target))
    return oriented_p, oriented_y


def metric_report(rows: list[FeatureRow], probabilities: list[float]) -> dict:
    if not rows:
        return {"status": "unavailable", "metrics": None, "bouts": 0}
    oriented_p, oriented_y = hash_oriented_pairs(rows, probabilities)
    favorites = [max(p, 1 - p) for p in oriented_p]
    favorite_won = [int((p >= .5) == bool(y)) for p, y in zip(oriented_p, oriented_y)]
    reliability = []
    for lower, upper in ((.5, .6), (.6, .7), (.7, .8), (.8, .9), (.9, 1.0)):
        pairs = [(p, y) for p, y in zip(favorites, favorite_won)
                 if lower <= p and (p < upper or upper == 1.0 and p <= upper)]
        reliability.append({
            "lower": lower, "upper": upper, "bouts": len(pairs),
            "mean_probability": sum(p for p, _ in pairs) / len(pairs) if pairs else None,
            "observed_win_rate": sum(y for _, y in pairs) / len(pairs) if pairs else None,
        })
    return {
        "status": "evaluated", "metrics": binary_metrics(oriented_p, oriented_y),
        "calibration_bins": _calibration_bins(oriented_p, oriented_y),
        "favorite_reliability": reliability,
        "orientation": "sha256(bout_id)_low_bit",
        "exact_probability_ties": sum(p == .5 for p in probabilities),
        "tie_rule": "hash_oriented_fighter_a",
    }


def market_pairs(connection: sqlite3.Connection, rows: list[FeatureRow]):
    selected, probabilities = [], []
    receipt_cache = {}
    for row in rows:
        event = connection.execute("SELECT start_time_utc FROM events WHERE event_id = ?",
                                   (row.event_id,)).fetchone()
        # Lacking a verified start, require a price before the event's UTC date.
        cutoff = parse_utc(event[0] if event and event[0] else row.event_date + "T00:00:00Z")
        probability = _bookmaker_probability(connection, row, cutoff, 24.0, receipt_cache)
        if probability is not None:
            selected.append(row)
            probabilities.append(probability)
    return selected, probabilities


def lab_rows(connection, *, feature_set="result", elo_k=32, through_date=None):
    profiles = None
    if feature_set != "result":
        from .wikidata_profiles import static_profiles
        profiles = static_profiles(connection, research=True)
    rows, source = research_feature_rows(connection, include_restored=True, profiles=profiles,
                                         elo_k=elo_k, through_date=through_date)
    if feature_set == "result":
        return rows, source, FEATURE_NAMES
    converted = []
    for row in rows:
        hinge = 0.0
        if feature_set == "age-hinge" and row.coverage[0] and row.coverage[1]:
            event_date = date.fromisoformat(row.event_date)
            a = (event_date - profiles[row.fighter_a_id].birth_date).days / 365.2425
            b = (event_date - profiles[row.fighter_b_id].birth_date).days / 365.2425
            hinge = (max(a - 32, 0) - max(b - 32, 0)) / 5
        converted.append(replace(row, features=v3_features(row.features, hinge)))
    source["retrospective_feature_rows_sha256"] = _digest_json([
        [r.bout_id, r.event_date, r.fighter_a_id, r.fighter_b_id, r.features, r.target] for r in converted])
    return converted, source, V3_FEATURE_NAMES


def compare(connection: sqlite3.Connection, *, originally_kept: set[str] | None = None,
            identity_review: dict | None = None, feature_set="result", elo_k=32, start_year=1993) -> dict:
    rows, source, names = lab_rows(connection, feature_set=feature_set, elo_k=elo_k)
    train, validation, test = fixed_split(rows)
    originally_kept = originally_kept if originally_kept is not None else {r.bout_id for r in rows}
    train = [r for r in train if int(r.event_date[:4]) >= start_year]
    model = fit_logistic(train, feature_names=names)
    calibrator = fit_temperature([model.predict_probability(r.features) for r in validation],
                                 [int(r.target) for r in validation])
    probabilities = {
        "coin_flip": [.5] * len(test),
        "elo": [1 / (1 + 10 ** (-r.features[0])) for r in test],
        "logistic_uncalibrated": [model.predict_probability(r.features) for r in test],
        "logistic_calibrated": [calibrator.predict_probability(model.predict_probability(r.features))
                                for r in test],
    }
    if feature_set != "result":
        v2_rows, _ = research_feature_rows(connection, include_restored=True)
        v2_train, v2_validation, v2_test = fixed_split(v2_rows)
        v2 = fit_logistic(v2_train)
        v2_temperature = fit_temperature([v2.predict_probability(r.features) for r in v2_validation],
                                        [int(r.target) for r in v2_validation])
        by_id = {r.bout_id: v2_temperature.predict_probability(v2.predict_probability(r.features)) for r in v2_test}
        probabilities["logistic_v2"] = [by_id[r.bout_id] for r in test]
    subsets = {"all": list(range(len(test))), "originally_kept": [
        i for i, row in enumerate(test) if row.bout_id in originally_kept]}
    metrics = {}
    for name, indices in subsets.items():
        subset = [test[i] for i in indices]
        metrics[name] = {key: metric_report(subset, [values[i] for i in indices])
                         for key, values in probabilities.items()}
        both_ages = [r for r in subset if r.coverage[0] and r.coverage[1]]
        # Classifier baseline only: deterministic picks, not calibrated probabilities.
        younger = [1.0 if r.features[6] < 0 else 0.0 if r.features[6] > 0 else .5 for r in both_ages]
        metrics[name]["younger_fighter_wins"] = {
            "status": "evaluated" if both_ages else "unavailable_without_birth_dates",
            "bouts": len(both_ages),
            "accuracy": metric_report(both_ages, younger)["metrics"]["accuracy"] if both_ages else None,
            "log_loss": None, "brier_score": None,
            "note": "Classifier baseline; no claimed probability calibration",
        }
        market_rows, market_probabilities = market_pairs(connection, subset)
        metrics[name]["market_no_vig"] = metric_report(market_rows, market_probabilities)
        if market_rows:
            by_id = dict(zip((r.bout_id for r in subset),
                             (probabilities["logistic_calibrated"][i] for i in indices)))
            metrics[name]["logistic_on_market_subset"] = metric_report(
                market_rows, [by_id[r.bout_id] for r in market_rows])
        if both_ages:
            by_id = dict(zip((r.bout_id for r in test), probabilities["logistic_calibrated"]))
            metrics[name]["logistic_on_both_birth_dates"] = metric_report(
                both_ages, [by_id[r.bout_id] for r in both_ages])
    report = {
        "schema_version": 1, "protocol_version": PROTOCOL_VERSION,
        "research_only": True, "promotion_eligible": False,
        "split_boundaries": {"train_end": TRAIN_END, "validation_start": VALIDATION_START,
                             "validation_end": VALIDATION_END, "test_start": TEST_START},
        "split": {name: _summary(group) for name, group in
                  (("train", train), ("validation", validation), ("test", test))},
        "source_summary": source, "feature_names": names,
        "model": {"version": "logistic-prior-v2" if feature_set == "result" else "logistic-prior-v3",
                  "elo_k": elo_k, "l2": .02, "feature_set": feature_set, "training_start_year": start_year,
                  "weights": model.weights, "weights_sha256": _digest_json(model.weights)},
        "calibration": {"scale": calibrator.scale, "fit_on": "validation_only"},
        "test": metrics,
        "paired_uncertainty": _paired_event_bootstrap(test, probabilities["elo"],
                                                       probabilities["logistic_calibrated"]),
        "published_holdout_reproduction": {
            "orientation": "legacy_sorted_ids_for_exact_reproduction_only",
            **{key: binary_metrics(probabilities[key], [int(r.target) for r in test])
               for key in ("elo", "logistic_calibrated")},
            "temperature_scale": calibrator.scale,
        },
    }
    report["model"]["version_hash"] = _digest_json(report["model"])
    report["feature_coverage_by_split"] = {
        name: {"bouts": len(group), "fighter_instances": 2*len(group),
               "fighter_instances_with_birth_date": sum(r.coverage[0]+r.coverage[1] for r in group),
               "bouts_with_both_birth_dates": sum(r.coverage[0] and r.coverage[1] for r in group)}
        for name, group in (("train", train), ("validation", validation), ("test", test))}
    if "logistic_v2" in probabilities:
        report["paired_v3_minus_v2"] = _paired_event_bootstrap(
            test, probabilities["logistic_v2"], probabilities["logistic_calibrated"])
    if identity_review is not None:
        held = _held_bouts_by_event(connection, identity_review)
        restored = {str(r[0]): int(r[1]) for r in connection.execute(
            "SELECT event_id,COUNT(*) FROM bouts WHERE source='wikipedia_research_restored' GROUP BY event_id")}
        if any(restored.get(event, 0) > count for event, count in held.items()):
            raise ValueError("Restored fights exceed the checked held count")
        restored_count = sum(restored.values())
        held = {event: count - restored.get(event, 0) for event, count in held.items()}
        dates = {str(r["event_id"]): str(r["event_date"]) for r in connection.execute(
            "SELECT event_id, event_date FROM events WHERE source = 'wikipedia_research'")}
        report["identity_coverage"] = {"held_bout_rows": sum(held.values()),
                                       "restored_bouts": restored_count, "by_split": {}}
        for name, group in (("train", train), ("validation", validation), ("test", test)):
            lower, upper = min(r.event_date for r in group), max(r.event_date for r in group)
            count = sum(count for event, count in held.items() if lower <= dates[event] <= upper)
            report["identity_coverage"]["by_split"][name] = {
                "held_bout_rows": count, "binary_bouts": len(group),
                "note": "Held includes nonbinary outcomes; binary accepted count shown separately"}
    return report


def absolute_bootstrap(rows, probabilities, *, repeats=2000):
    """Whole-date uncertainty for probability losses and hash-tie accuracy."""
    if not rows:
        return None
    p, y = hash_oriented_pairs(rows, probabilities)
    blocks = {}
    for row, probability, outcome in zip(rows, p, y):
        block = blocks.setdefault(row.event_date, [0, 0., 0., 0.])
        block[0] += 1
        block[1] += -math.log(max(1e-12, min(1-1e-12, probability if outcome else 1-probability)))
        block[2] += (probability-outcome)**2
        block[3] += (probability >= .5) == bool(outcome)
    blocks = [value for _, value in sorted(blocks.items())]
    rng = random.Random(20260926)
    samples = [[], [], []]
    for _ in range(repeats):
        drawn = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        count = sum(b[0] for b in drawn)
        for index in range(3):
            samples[index].append(sum(b[index+1] for b in drawn)/count)
    result = {"method": "event_date_block_bootstrap", "seed": 20260926, "repeats": repeats,
              "event_date_blocks": len(blocks)}
    for name, values in zip(("log_loss", "brier_score", "accuracy"), samples):
        values.sort()
        result[name+"_95pct_interval"] = [values[int(.025*(repeats-1))], values[int(.975*(repeats-1))]]
    return result


def rolling_origin(rows, names, *, start_year=1993, years=range(2016, 2027),
                   through_date=None, intervals=True):
    folds, pooled_rows, pooled_probabilities = [], [], []
    for year in years:
        before = f"{year}-01-01"
        prior = [r for r in rows if f"{start_year}-01-01" <= r.event_date < before]
        test = [r for r in rows if int(r.event_date[:4]) == year and
                (through_date is None or r.event_date <= through_date)]
        dates = sorted({r.event_date for r in prior})
        if len(dates) < 3 or not test:
            folds.append({"year": year, "status": "insufficient_history", "test_bouts": len(test)})
            continue
        validation_dates = max(1, int(len(dates)*.15))
        boundary = dates[-validation_dates]
        train = [r for r in prior if r.event_date < boundary]
        validation = [r for r in prior if r.event_date >= boundary]
        model = fit_logistic(train, feature_names=names)
        temperature = fit_temperature([model.predict_probability(r.features) for r in validation],
                                      [int(r.target) for r in validation])
        probabilities = [temperature.predict_probability(model.predict_probability(r.features)) for r in test]
        result = metric_report(test, probabilities)
        if intervals:
            result["uncertainty"] = absolute_bootstrap(test, probabilities)
        folds.append({"year": year, "status": "evaluated", "train": _summary(train),
                      "validation": _summary(validation), "test": result,
                      "temperature_scale": temperature.scale,
                      "training_weights_sha256": _digest_json(model.weights)})
        pooled_rows.extend(test)
        pooled_probabilities.extend(probabilities)
    pooled = metric_report(pooled_rows, pooled_probabilities)
    if intervals and pooled_rows:
        pooled["uncertainty"] = absolute_bootstrap(pooled_rows, pooled_probabilities)
    return {"training_start_year": start_year, "folds": folds, "pooled": pooled}, pooled_rows, pooled_probabilities


def select_parameters(connection):
    """Freeze K/window using validation and folds entirely before the fixed test."""
    k_trials = []
    for k in (32, 48, 64, 96):
        rows, source, _ = lab_rows(connection, elo_k=k, through_date=VALIDATION_END)
        validation = [r for r in rows if VALIDATION_START <= r.event_date <= VALIDATION_END]
        probabilities = [1/(1+10**(-r.features[0])) for r in validation]
        k_trials.append({"elo_k": k, "validation": metric_report(validation, probabilities)})
    selected_k = min(k_trials, key=lambda t:(t["validation"]["metrics"]["log_loss"], t["elo_k"]))["elo_k"]
    rows, source, names = lab_rows(connection, feature_set="age-hinge", elo_k=selected_k,
                                   through_date=VALIDATION_END)
    window_trials = []
    for year in (1993, 2001, 2005, 2010):
        result, _, _ = rolling_origin(rows, names, start_year=year, years=range(2016,2024),
                                      through_date=VALIDATION_END, intervals=False)
        window_trials.append(result)
    selected_year = min(window_trials, key=lambda t:(t["pooled"]["metrics"]["log_loss"], t["training_start_year"]))["training_start_year"]
    return {"schema_version": 1, "research_only": True, "protocol_version": PROTOCOL_VERSION,
            "selection_labels_through": VALIDATION_END, "fixed_test_labels_used": False,
            "selection_rule": "minimum_validation_elo_log_loss_then_minimum_pretest_rolling_log_loss",
            "tie_rule": "smaller_K_then_earlier_start_year", "feature_set": "age-hinge",
            "elo_k": selected_k, "training_start_year": selected_year,
            "k_trials": k_trials, "window_trials": window_trials,
            "selection_feature_rows_sha256": source["retrospective_feature_rows_sha256"]}


def rolling_comparison(connection, *, elo_k, start_year, originally_kept=None):
    v3_rows, _, names = lab_rows(connection, feature_set="age-hinge", elo_k=elo_k)
    v2_rows, _, _ = lab_rows(connection)
    v3, pooled_rows, p3 = rolling_origin(v3_rows, names, start_year=start_year)
    v2, baseline_rows, p2 = rolling_origin(v2_rows, FEATURE_NAMES)
    if [r.bout_id for r in pooled_rows] != [r.bout_id for r in baseline_rows]:
        raise ValueError("Rolling comparisons must use identical bout/date blocks")
    p_elo = [1/(1+10**(-r.features[0])) for r in pooled_rows]
    baseline_probabilities = {"coin_flip": [.5]*len(pooled_rows), "elo": p_elo, "logistic_v2": p2}
    originally_kept = originally_kept if originally_kept is not None else {r.bout_id for r in pooled_rows}
    for fold in v3["folds"]:
        if fold["status"] != "evaluated":
            continue
        indices = [i for i,r in enumerate(pooled_rows) if int(r.event_date[:4]) == fold["year"]]
        yearly_rows = [pooled_rows[i] for i in indices]
        kept_indices = [i for i in indices if pooled_rows[i].bout_id in originally_kept]
        kept_rows = [pooled_rows[i] for i in kept_indices]
        fold["originally_kept"] = metric_report(kept_rows, [p3[i] for i in kept_indices])
        fold["originally_kept"]["uncertainty"] = absolute_bootstrap(kept_rows, [p3[i] for i in kept_indices])
        fold["baselines"] = {}
        for name, probs in baseline_probabilities.items():
            values = [probs[i] for i in indices]
            result = metric_report(yearly_rows, values)
            result["uncertainty"] = absolute_bootstrap(yearly_rows, values)
            fold["baselines"][name] = result
        both = [r for r in yearly_rows if r.coverage[0] and r.coverage[1]]
        younger = [1. if r.features[6] < 0 else 0. if r.features[6] > 0 else .5 for r in both]
        fold["baselines"]["younger_fighter_wins"] = {
            "bouts": len(both), "accuracy": metric_report(both,younger)["metrics"]["accuracy"] if both else None,
            "log_loss": None, "brier_score": None,
            "accuracy_95pct_interval": absolute_bootstrap(both,younger)["accuracy_95pct_interval"] if both else None}
        market_rows, market = market_pairs(connection, yearly_rows)
        fold["baselines"]["market_no_vig"] = metric_report(market_rows, market)
    v3["baselines"] = {name: metric_report(pooled_rows, probs) for name,probs in baseline_probabilities.items()}
    for name, probs in baseline_probabilities.items():
        v3["baselines"][name]["uncertainty"] = absolute_bootstrap(pooled_rows, probs)
    both = [r for r in pooled_rows if r.coverage[0] and r.coverage[1]]
    younger = [1. if r.features[6] < 0 else 0. if r.features[6] > 0 else .5 for r in both]
    v3["baselines"]["younger_fighter_wins"] = {
        "bouts":len(both), "accuracy":metric_report(both,younger)["metrics"]["accuracy"] if both else None,
        "log_loss":None,"brier_score":None,
        "accuracy_95pct_interval":absolute_bootstrap(both,younger)["accuracy_95pct_interval"] if both else None}
    market_rows, market = market_pairs(connection, pooled_rows)
    v3["baselines"]["market_no_vig"] = metric_report(market_rows,market)
    v3["paired_v3_minus_v2"] = _paired_event_bootstrap(pooled_rows,p2,p3)
    kept_indices = [i for i,r in enumerate(pooled_rows) if r.bout_id in originally_kept]
    kept_rows = [pooled_rows[i] for i in kept_indices]
    kept_p3 = [p3[i] for i in kept_indices]
    v3["originally_kept"] = metric_report(kept_rows,kept_p3)
    v3["originally_kept"]["uncertainty"] = absolute_bootstrap(kept_rows,kept_p3)
    v3["originally_kept"]["paired_v3_minus_v2"] = _paired_event_bootstrap(
        kept_rows,[p2[i] for i in kept_indices],kept_p3)
    return v3


def write_new_json(path: str | Path, report: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")


def append_phase_metrics(path: str | Path, phase: int, report: dict, command: str) -> None:
    """Append one phase to the task's explicitly appendable metrics artifact."""
    path = Path(path)
    previous = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "phases": []}
    if any(entry["phase"] == phase for entry in previous["phases"]):
        raise ValueError(f"Phase {phase} is already recorded; preserve that measurement")
    previous["phases"].append({"phase": phase, "recorded_at_utc": utc_string(utc_now()),
                               "command": command, "report": report})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(previous, indent=2, sort_keys=True) + "\n")


def run_comparison(path, complete_path=None, out=None, identity_review=None, phase=None,
                   *, feature_set="result", elo_k=32, start_year=1993, selection_path=None, rolling=False):
    before = file_sha256(path)
    with read_only(path) as original:
        kept = {str(r[0]) for r in original.execute(
            "SELECT bout_id FROM bouts WHERE source = 'wikipedia_research'")}
    review = json.loads(Path(identity_review).read_text()) if identity_review else None
    if selection_path:
        selection = json.loads(Path(selection_path).read_text())
        if (selection.get("fixed_test_labels_used") is not False or selection.get("database_sha256") != file_sha256(complete_path or path)
                or selection.get("selection_labels_through") != VALIDATION_END):
            raise ValueError("Selection manifest does not match this database and pretest protocol")
        elo_k, start_year, feature_set = selection["elo_k"], selection["training_start_year"], selection["feature_set"]
    with read_only(complete_path or path) as connection:
        report = compare(connection, originally_kept=kept, identity_review=review,
                         feature_set=feature_set, elo_k=elo_k, start_year=start_year)
        if rolling:
            if feature_set != "age-hinge":
                raise ValueError("Rolling v3 comparison requires age-hinge features")
            report["rolling_origin"] = rolling_comparison(connection, elo_k=elo_k, start_year=start_year,
                                                          originally_kept=kept)
        if selection_path:
            report["selection_manifest_sha256"] = file_sha256(selection_path)
    report["database_sha256"] = file_sha256(complete_path or path)
    report["original_database_sha256"] = before
    if file_sha256(path) != before:
        raise RuntimeError("Original research database changed during comparison")
    if out:
        write_new_json(out, report)
    print("Model                         Bouts   Accuracy   Log loss   Brier")
    for name, result in report["test"]["all"].items():
        if result.get("metrics"):
            m = result["metrics"]
            print(f"{name:29s} {m['bouts']:5d} {m['accuracy']:9.2%} {m['log_loss']:10.4f} {m['brier_score']:8.4f}")
    if phase is not None:
        append_phase_metrics("reports/model_upgrade_metrics.json", phase, report,
                             f"ufc-model research-compare --db {path}" +
                             (f" --complete-db {complete_path}" if complete_path else "") +
                             (f" --identity-review {identity_review}" if identity_review else "") +
                             f" --features {feature_set} --elo-k {elo_k} --start-year {start_year}" +
                             (f" --selection {selection_path}" if selection_path else "") +
                             (" --rolling-origin" if rolling else "") +
                             (f" --out {out}" if out else "") + f" --phase {phase}")
    return report
