"""Retrospective, research-only Elo and logistic holdout on Wikipedia results.

This module deliberately does not call the operating ``evaluate`` path. The
Wikipedia import proves what was in a retained revision at import time, not
what a bettor could have known before an old fight. No output from here can
authorize an alert, a bet, a model promotion, or an ROI claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
from contextlib import closing
from datetime import date
from itertools import groupby
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import db
from .elo import MODEL_VERSION as ELO_VERSION
from .features import FEATURE_NAMES, FeatureBuilder, FeatureRow
from .logistic import (
    MODEL_VERSION as LOGISTIC_VERSION,
    binary_metrics,
    chronological_event_split,
    fit_logistic,
    fit_temperature,
)
from .wikipedia_recount import reviewed_same_revision_recount


RESEARCH_EVALUATION_VERSION = "wikipedia-result-holdout-v2"
SOURCE = "wikipedia_research"


def _digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _source_results(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT e.event_id, e.event_date, b.bout_id, b.fighter_a_id,
                  b.fighter_b_id, r.outcome, r.winner_fighter_id
           FROM events e
           JOIN bouts b ON b.event_id = e.event_id
           JOIN results r ON r.bout_id = b.bout_id
           WHERE e.source = ? AND b.source = ?
             AND e.status = 'completed' AND b.status = 'completed'
           ORDER BY e.event_date, e.event_id, b.bout_id""",
        (SOURCE, SOURCE),
    ).fetchall()


def research_feature_rows(
    connection: sqlite3.Connection,
) -> tuple[list[FeatureRow], dict[str, object]]:
    """Replay accepted research results with all same-date outcomes withheld.

    Only result-derived features are populated. The mutable fighters table,
    current career totals, post-fight statistics, and any other source's
    outcomes are never consulted. Draws update later history, while draws and
    no contests are excluded from binary targets.
    """
    source = _source_results(connection)
    builder = FeatureBuilder()
    examples: list[FeatureRow] = []
    outcome_counts = {"win": 0, "draw": 0, "no_contest": 0}
    event_ids: set[str] = set()
    digest = hashlib.sha256()
    for result in source:
        event_ids.add(str(result["event_id"]))
        date.fromisoformat(str(result["event_date"]))
        outcome = str(result["outcome"])
        if outcome not in outcome_counts:
            raise ValueError(f"Unexpected research outcome: {outcome}")
        outcome_counts[outcome] += 1
        a_id, b_id = str(result["fighter_a_id"]), str(result["fighter_b_id"])
        if a_id == b_id:
            raise ValueError(f"Duplicate participant in {result['bout_id']}")
        winner = result["winner_fighter_id"]
        if outcome == "win" and winner not in (a_id, b_id):
            raise ValueError(f"Winner is not a participant in {result['bout_id']}")
        if outcome != "win" and winner is not None:
            raise ValueError(f"Non-win has a winner in {result['bout_id']}")
        digest.update(json.dumps(
            [result[key] for key in (
                "event_id", "event_date", "bout_id", "fighter_a_id",
                "fighter_b_id", "outcome", "winner_fighter_id",
            )], separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8") + b"\n")

    for event_date, group in groupby(source, key=lambda row: str(row["event_date"])):
        date_results = list(group)
        for result in date_results:
            if result["outcome"] != "win":
                continue
            first, second = sorted((
                str(result["fighter_a_id"]), str(result["fighter_b_id"]),
            ))
            features, coverage = builder.values_with_coverage(first, second, event_date)
            examples.append(FeatureRow(
                bout_id=str(result["bout_id"]),
                event_id=str(result["event_id"]),
                event_date=event_date,
                fighter_a_id=first,
                fighter_b_id=second,
                features=features,
                target=int(result["winner_fighter_id"] == first),
                coverage=coverage,
                prior_result_rows_available=builder.prior_result_rows_available,
            ))
        builder.update_group(date_results)
        builder.prior_result_rows_available += sum(
            result["outcome"] != "no_contest" for result in date_results
        )

    if len(examples) != outcome_counts["win"]:
        raise AssertionError("Binary research rows do not match accepted wins")
    if any(any(row.coverage) or any(row.features[6:]) for row in examples):
        raise AssertionError("Research replay unexpectedly used profile or statistic features")
    return examples, {
        "accepted_events_with_results": len(event_ids),
        "accepted_bouts_with_results": len(source),
        "outcomes": outcome_counts,
        "accepted_source_rows_sha256": digest.hexdigest(),
        "retrospective_feature_rows_sha256": _digest_json([
            [row.bout_id, row.event_date, row.fighter_a_id, row.fighter_b_id,
             row.features, row.target] for row in examples
        ]),
    }


def _summary(rows: list[FeatureRow]) -> dict[str, object]:
    dates = sorted({row.event_date for row in rows})
    return {
        "binary_bouts": len(rows),
        "events": len({row.event_id for row in rows}),
        "event_dates": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def _calibration_bins(
    probabilities: list[float], outcomes: list[int],
) -> list[dict[str, float | int | None]]:
    bins: list[dict[str, float | int | None]] = []
    for index in range(5):
        pairs = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes)
            if min(int(probability * 5), 4) == index
        ]
        count = len(pairs)
        bins.append({
            "lower": index / 5,
            "upper": (index + 1) / 5,
            "bouts": count,
            "mean_probability": sum(value for value, _ in pairs) / count if count else None,
            "observed_win_rate": sum(value for _, value in pairs) / count if count else None,
        })
    return bins


def _paired_event_bootstrap(
    rows: list[FeatureRow], elo_probs: list[float], logistic_probs: list[float],
    *, repeats: int = 2000,
) -> dict[str, object]:
    """Resample whole test dates for paired logistic-minus-Elo loss intervals."""
    blocks: dict[str, list[tuple[float, float, float, float]]] = {}
    for row, elo, logistic in zip(rows, elo_probs, logistic_probs):
        target = int(row.target)
        def log_loss(probability: float) -> float:
            clipped = min(max(probability, 1e-12), 1.0 - 1e-12)
            return -math.log(clipped if target else 1.0 - clipped)
        blocks.setdefault(row.event_date, []).append((
            (logistic - target) ** 2, (elo - target) ** 2,
            log_loss(logistic), log_loss(elo),
        ))
    block_sums = [
        (
            len(values),
            sum(value[0] - value[1] for value in values),
            sum(value[2] - value[3] for value in values),
        )
        for _, values in sorted(blocks.items())
    ]
    count = len(block_sums)
    rng = random.Random(20260926)
    brier_deltas: list[float] = []
    log_loss_deltas: list[float] = []
    for _ in range(repeats):
        drawn = [block_sums[rng.randrange(count)] for _ in range(count)]
        denominator = sum(value[0] for value in drawn)
        brier_deltas.append(sum(value[1] for value in drawn) / denominator)
        log_loss_deltas.append(sum(value[2] for value in drawn) / denominator)
    def interval(values: list[float]) -> list[float]:
        ordered = sorted(values)
        return [ordered[int(0.025 * (repeats - 1))], ordered[int(0.975 * (repeats - 1))]]
    return {
        "method": "paired_event_date_block_bootstrap",
        "seed": 20260926,
        "repeats": repeats,
        "test_event_date_blocks": count,
        "logistic_minus_elo_brier_score_95pct_interval": interval(brier_deltas),
        "logistic_minus_elo_log_loss_95pct_interval": interval(log_loss_deltas),
        "note": "Intervals describe resampling variation within the accepted test subset; they do not account for held fighter identities or retrospective timing gaps.",
    }


def _held_bouts_by_event(
    connection: sqlite3.Connection, review: dict,
) -> dict[str, int]:
    if (review.get("schema_version") != 1 or review.get("source") != SOURCE
            or review.get("review_required") is not True):
        raise ValueError("Expected an unresolved Wikipedia identity review worksheet")
    seen: set[tuple[str, int]] = set()
    for fighter in review.get("fighters", []):
        for occurrence in fighter.get("occurrences", []):
            event_id = str(occurrence["event_id"])
            position = int(occurrence["bout_position"])
            if position <= 0:
                raise ValueError("Held bout position must be positive")
            seen.add((event_id, position))
    reported = review.get("summary", {}).get("held_bouts")
    if reported != len(seen):
        raise ValueError("Identity review summary disagrees with distinct held bouts")
    known_events = {
        str(row["event_id"]) for row in connection.execute(
            "SELECT event_id FROM events WHERE source = ? AND status = 'completed'", (SOURCE,)
        )
    }
    missing = {event_id for event_id, _ in seen} - known_events
    if missing:
        raise ValueError(f"Identity review names events absent from this database: {len(missing)}")
    recovered = _reconcile_identity_receipts(connection, seen)
    if recovered - seen:
        raise ValueError("Reviewed recovered position is absent from the identity worksheet")
    counts: dict[str, int] = {}
    for event_id, position in seen - recovered:
        counts[event_id] = counts.get(event_id, 0) + 1
    return counts


def _source_page_key(source_event_id: str) -> tuple[int, str] | None:
    page, separator, section = source_event_id.partition(":")
    if not page.isdecimal() or (separator and not section):
        return None
    return int(page), unquote(section).replace("_", " ").casefold() if separator else ""


def _receipt_page_key(page_id: object, page_url: object) -> tuple[int, str] | None:
    if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id <= 0:
        return None
    if not isinstance(page_url, str):
        return None
    try:
        parsed = urlsplit(page_url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "en.wikipedia.org":
        return None
    return page_id, unquote(parsed.fragment).replace("_", " ").casefold()


def _intact_payload(path_text: str, stated_hash: str) -> bool:
    if (len(stated_hash) != 64 or any(char not in "0123456789abcdef" for char in stated_hash)):
        return False
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        if path.is_symlink() or not path.is_file():
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == stated_hash
    except (OSError, ValueError):
        return False


def _reconcile_identity_receipts(
    connection: sqlite3.Connection, held_positions: set[tuple[str, int]],
) -> set[tuple[str, int]]:
    """Reconcile a historical worksheet against checked identity replays."""
    latest: dict[tuple[int, str], sqlite3.Row] = {}
    revision_rows: dict[tuple[tuple[int, str], int], list[sqlite3.Row]] = {}
    for row in connection.execute(
        """SELECT w.run_id, w.event_page_id, w.page_url, w.revision_id,
                  w.imported_bouts, w.skipped_unresolved_bouts, w.crosswalk_run_id,
                  i.payload_path, i.sha256
           FROM wikipedia_source_receipts w
           JOIN ingestion_runs i ON i.run_id = w.run_id
           WHERE i.source = ? ORDER BY w.run_id""", (SOURCE,),
    ):
        key = _receipt_page_key(row["event_page_id"], row["page_url"])
        if key is None:
            continue
        revision = int(row["revision_id"])
        revision_rows.setdefault((key, revision), []).append(row)
        latest[key] = row

    checked_payloads: dict[tuple[str, str], bool] = {}
    recovered: set[tuple[str, int]] = set()
    for event in connection.execute(
        """SELECT e.event_id, e.source_event_id, COUNT(b.bout_id) AS imported_bouts,
                  COUNT(r.bout_id) AS result_bouts
           FROM events e LEFT JOIN bouts b ON b.event_id = e.event_id
             AND b.source = ? AND b.status = 'completed'
           LEFT JOIN results r ON r.bout_id = b.bout_id
           WHERE e.source = ? AND e.status = 'completed'
           GROUP BY e.event_id""", (SOURCE, SOURCE),
    ):
        event_id = str(event["event_id"])
        key = _source_page_key(str(event["source_event_id"]))
        receipt = latest.get(key) if key is not None else None
        if (receipt is None or int(receipt["imported_bouts"]) != int(event["imported_bouts"])
                or int(event["result_bouts"]) != int(event["imported_bouts"])):
            raise ValueError(f"Missing, conflicting, or mismatched source receipt for {event_id}")
        recovered_here = reviewed_same_revision_recount(
            connection, revision_rows[(key, int(receipt["revision_id"]))],
            event_id, checked_payloads, _intact_payload,
        )
        if recovered_here is None:
            raise ValueError(f"Missing, conflicting, or mismatched source receipt for {event_id}")
        payload = str(receipt["payload_path"]), str(receipt["sha256"])
        if payload not in checked_payloads:
            checked_payloads[payload] = _intact_payload(*payload)
        if not checked_payloads[payload]:
            raise ValueError(f"Changed or unreadable source receipt payload for {event_id}")
        remaining = sum(
            1 for held_event, position in held_positions
            if held_event == event_id and position not in recovered_here
        )
        if int(receipt["skipped_unresolved_bouts"]) != remaining:
            raise ValueError(f"Identity worksheet disagrees with source receipt for {event_id}")
        recovered.update((event_id, position) for position in recovered_here)
    return recovered


def _coverage_by_period(
    connection: sqlite3.Connection,
    held_by_event: dict[str, int],
    split: dict[str, dict[str, object]],
) -> dict[str, dict[str, int | float | None]]:
    accepted = connection.execute(
        """SELECT e.event_id, e.event_date, COUNT(b.bout_id) AS accepted_bouts
           FROM events e LEFT JOIN bouts b
             ON b.event_id = e.event_id AND b.source = ?
             AND b.status = 'completed' AND EXISTS (
               SELECT 1 FROM results r WHERE r.bout_id = b.bout_id)
           WHERE e.source = ? AND e.status = 'completed'
           GROUP BY e.event_id, e.event_date""",
        (SOURCE, SOURCE),
    ).fetchall()
    answer: dict[str, dict[str, int | float | None]] = {}
    for name in ("train", "validation", "test"):
        first, last = split[name]["first_date"], split[name]["last_date"]
        period = [row for row in accepted if first <= row["event_date"] <= last]
        imported = sum(int(row["accepted_bouts"]) for row in period)
        held = sum(held_by_event.get(str(row["event_id"]), 0) for row in period)
        answer[name] = {
            "completed_events": len(period),
            "accepted_bouts_all_outcomes": imported,
            "held_source_bout_rows": held,
            "accepted_share_of_accepted_plus_held": imported / (imported + held)
            if imported + held else None,
        }
    return answer


def evaluate_research_history(
    connection: sqlite3.Connection,
    *,
    identity_review: dict | None = None,
    identity_review_sha256: str | None = None,
) -> dict[str, object]:
    """Fit on earlier event dates, calibrate on later dates, score final dates."""
    rows, source = research_feature_rows(connection)
    try:
        train, validation, test = chronological_event_split(rows)
    except ValueError as exc:
        return {
            "status": "insufficient_research_history",
            "research_only": True,
            "promotion_eligible": False,
            "reason": str(exc),
            "source": SOURCE,
            "source_summary": source,
            "bookmaker": {"status": "not_evaluated", "roi": None},
        }
    split = {name: _summary(group) for name, group in (
        ("train", train), ("validation", validation), ("test", test),
    )}
    model = fit_logistic(train)
    validation_probs = [model.predict_probability(row.features) for row in validation]
    calibrator = fit_temperature(validation_probs, [int(row.target) for row in validation])
    outcomes = [int(row.target) for row in test]
    elo_probs = [1.0 / (1.0 + 10.0 ** (-row.features[0])) for row in test]
    raw_probs = [model.predict_probability(row.features) for row in test]
    calibrated_probs = [calibrator.predict_probability(probability) for probability in raw_probs]
    quote_count = int(connection.execute(
        """SELECT COUNT(*) FROM odds_quotes q JOIN bouts b ON b.bout_id = q.bout_id
           JOIN events e ON e.event_id = b.event_id
           WHERE e.source = ? AND b.source = ?""", (SOURCE, SOURCE),
    ).fetchone()[0])
    result: dict[str, object] = {
        "schema_version": 1,
        "evaluation_version": RESEARCH_EVALUATION_VERSION,
        "status": "research_only",
        "research_only": True,
        "promotion_eligible": False,
        "source": SOURCE,
        "source_summary": source,
        "models": {
            "elo": ELO_VERSION,
            "logistic": LOGISTIC_VERSION,
            "logistic_weights_sha256": _digest_json(model.weights),
        },
        "feature_history_rule": "only_accepted_results_on_strictly_earlier_event_dates",
        "feature_names": FEATURE_NAMES,
        "feature_available": list(FEATURE_NAMES[:6]),
        "feature_unavailable": list(FEATURE_NAMES[6:]),
        "split": split,
        "calibration": {
            "method": "symmetric_temperature_on_validation_only",
            "validation_binary_bouts": len(validation),
            "applied": len(validation) >= 30,
            "scale": calibrator.scale,
        },
        "test": {
            "elo": {
                "metrics": binary_metrics(elo_probs, outcomes),
                "calibration_bins": _calibration_bins(elo_probs, outcomes),
            },
            "logistic_uncalibrated": {
                "metrics": binary_metrics(raw_probs, outcomes),
                "calibration_bins": _calibration_bins(raw_probs, outcomes),
            },
            "logistic_calibrated": {
                "metrics": binary_metrics(calibrated_probs, outcomes),
                "calibration_bins": _calibration_bins(calibrated_probs, outcomes),
            },
        },
        "test_uncertainty": _paired_event_bootstrap(test, elo_probs, calibrated_probs),
        "bookmaker": {
            "status": "not_evaluated_without_historical_point_in_time_prices",
            "research_quote_rows_present_but_ignored": quote_count,
            "roi": None,
        },
        "limits": [
            "Retrospective Wikipedia revisions do not prove when an outcome or roster was available before a historical event.",
            "There are no independently timed pre-fight roster observations or verified event start times in this research history.",
            "Fighter identity holds create nonrandom selection; metrics apply only to accepted bouts.",
            "Current profiles, current career averages, and post-fight statistics were excluded from features.",
            "This command does not re-verify source payload hashes; run the integrity command on the same database before interpreting metrics.",
            "No bookmaker comparison, betting edge, paper profit, or ROI is inferred from these results.",
        ],
    }
    if identity_review is not None:
        held = _held_bouts_by_event(connection, identity_review)
        result["identity_coverage"] = {
            "worksheet_sha256": identity_review_sha256,
            "held_source_bout_rows": sum(held.values()),
            "by_split_dates": _coverage_by_period(connection, held, split),
            "warning": "Held source rows may differ systematically from accepted fights; test metrics are conditional on accepted identities.",
        }
    else:
        result["identity_coverage"] = {
            "held_source_bout_rows": None,
            "by_split_dates": None,
            "warning": "Pass a checked identity worksheet to quantify omitted source bouts in each date period.",
        }
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Existing Wikipedia research SQLite database")
    parser.add_argument("--identity-review", type=Path, help="Optional checked, combined unresolved-identity worksheet")
    parser.add_argument("--out", type=Path, help="Write JSON report here instead of stdout")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error("Research database does not exist")
    paths = [path.resolve() for path in (args.db, args.identity_review) if path is not None]
    if args.out is not None and args.out.resolve() in paths:
        parser.error("Output must not replace the database or identity review")
    review = None
    review_hash = None
    if args.identity_review is not None:
        raw = args.identity_review.read_bytes()
        review = json.loads(raw)
        review_hash = hashlib.sha256(raw).hexdigest()
    uri = f"file:{quote(str(args.db.resolve()), safe='/')}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        db.require_current_schema(connection)
        connection.execute("BEGIN")
        report = evaluate_research_history(
            connection, identity_review=review, identity_review_sha256=review_hash,
        )
        connection.rollback()
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out is None:
        print(output, end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
        print(f"Research-only report written to {args.out}")


if __name__ == "__main__":
    main()
