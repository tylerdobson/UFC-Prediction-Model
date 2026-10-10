"""Fixed-date comparisons for retrospective research; never authorize wagers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import quote

from .evaluation import _bookmaker_probability
from .features import FEATURE_NAMES, FeatureRow
from .logistic import binary_metrics, fit_logistic, fit_temperature
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


def compare(connection: sqlite3.Connection, *, originally_kept: set[str] | None = None,
            identity_review: dict | None = None) -> dict:
    rows, source = research_feature_rows(connection)
    train, validation, test = fixed_split(rows)
    originally_kept = originally_kept if originally_kept is not None else {r.bout_id for r in rows}
    model = fit_logistic(train)
    calibrator = fit_temperature([model.predict_probability(r.features) for r in validation],
                                 [int(r.target) for r in validation])
    probabilities = {
        "coin_flip": [.5] * len(test),
        "elo": [1 / (1 + 10 ** (-r.features[0])) for r in test],
        "logistic_uncalibrated": [model.predict_probability(r.features) for r in test],
        "logistic_calibrated": [calibrator.predict_probability(model.predict_probability(r.features))
                                for r in test],
    }
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
        "source_summary": source, "feature_names": FEATURE_NAMES,
        "model": {"version": "logistic-prior-v2", "elo_k": 32, "l2": .02,
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
    if identity_review is not None:
        held = _held_bouts_by_event(connection, identity_review)
        dates = {str(r["event_id"]): str(r["event_date"]) for r in connection.execute(
            "SELECT event_id, event_date FROM events WHERE source = 'wikipedia_research'")}
        report["identity_coverage"] = {"held_bout_rows": sum(held.values()), "by_split": {}}
        for name, group in (("train", train), ("validation", validation), ("test", test)):
            lower, upper = min(r.event_date for r in group), max(r.event_date for r in group)
            count = sum(count for event, count in held.items() if lower <= dates[event] <= upper)
            report["identity_coverage"]["by_split"][name] = {
                "held_bout_rows": count, "binary_bouts": len(group),
                "note": "Held includes nonbinary outcomes; binary accepted count shown separately"}
    return report


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


def run_comparison(path, complete_path=None, out=None, identity_review=None, phase=None):
    before = file_sha256(path)
    with read_only(path) as original:
        kept = {str(r[0]) for r in original.execute(
            "SELECT bout_id FROM bouts WHERE source = 'wikipedia_research'")}
    review = json.loads(Path(identity_review).read_text()) if identity_review else None
    with read_only(complete_path or path) as connection:
        report = compare(connection, originally_kept=kept, identity_review=review)
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
                             (f" --out {out}" if out else "") + f" --phase {phase}")
    return report
