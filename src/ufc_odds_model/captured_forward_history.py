"""Research-only forward forecasts from results actually captured before cutoff.

This is separate from the archived historical holdout: a source observed in
September 2026 cannot prove what was knowable before a fight in 2018. It can,
however, supply result-only features for an October 2026 research forecast if
every used receipt and identity decision was captured before that forecast.
No operating database, alert gate, or ledger is changed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path
from urllib.parse import quote

from scripts.import_reviewed_prefight_card import _odds_input, _selected_rows, verify_manifest

from . import db
from .archived_pit_evaluation import _guard_standalone_database, _sha256_regular_file, _utc
from .features import FEATURE_NAMES, FeatureBuilder
from .integrity import verify_evidence
from .logistic import binary_metrics, chronological_event_split, fit_logistic, fit_temperature
from .research_evaluation import (
    RESEARCH_EVALUATION_VERSION, SOURCE, _digest_json, _receipt_page_key,
    _summary as _split_summary,
    _source_page_key, _source_results, research_feature_rows,
)


VERSION = "captured-forward-research-v1"


def _saved_report(path: Path) -> tuple[dict, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("A regular saved research holdout report is required")
    raw = path.read_bytes()
    try:
        report = json.loads(raw)
        json.dumps(report, allow_nan=False)
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Saved research holdout report is invalid") from exc
    if (not isinstance(report, dict) or report.get("schema_version") != 1
            or report.get("evaluation_version") != RESEARCH_EVALUATION_VERSION
            or report.get("source") != SOURCE
            or report.get("research_only") is not True
            or report.get("promotion_eligible") is not False
            or report.get("status") != "research_only"):
        raise ValueError("Saved holdout is not the checked research evaluation")
    return report, hashlib.sha256(raw).hexdigest()


def _receipt_catalog(connection: sqlite3.Connection, cutoff: datetime) -> dict:
    """Require all results and identity receipts in this DB before the decision."""
    runs = connection.execute(
        "SELECT run_id, source, fetched_at_utc, sha256 FROM ingestion_runs ORDER BY run_id"
    ).fetchall()
    if not runs:
        raise ValueError("Research database has no retained ingestion runs")
    digest = hashlib.sha256()
    sources: Counter[str] = Counter()
    run_times: dict[int, datetime] = {}
    run_sources: dict[int, str] = {}
    latest_fetch = ""
    for row in runs:
        observed = _utc(row["fetched_at_utc"], "ingestion fetch")
        if observed >= cutoff:
            raise ValueError(f"Research ingestion run {row['run_id']} was fetched at or after cutoff")
        source = str(row["source"])
        if not source.startswith("wikipedia_"):
            raise ValueError(f"Unexpected source in research database: {source}")
        stated_hash = row["sha256"]
        if (not isinstance(stated_hash, str) or len(stated_hash) != 64
                or any(char not in "0123456789abcdef" for char in stated_hash)):
            raise ValueError("Research ingestion run has invalid payload digest")
        sources[source] += 1
        run_times[int(row["run_id"])] = observed
        run_sources[int(row["run_id"])] = source
        latest_fetch = max(latest_fetch, row["fetched_at_utc"])
        digest.update(json.dumps([row["run_id"], source, row["fetched_at_utc"], stated_hash],
                                 separators=(",", ":")).encode("utf-8") + b"\n")
    if not sources[SOURCE]:
        raise ValueError("No research result source runs were found")

    latest_receipts: dict[tuple[int, str], sqlite3.Row] = {}
    for row in connection.execute(
        """SELECT w.run_id, w.event_page_id, w.page_url, w.revision_timestamp_utc,
                  w.imported_bouts, w.skipped_unresolved_bouts, w.crosswalk_run_id,
                  i.fetched_at_utc
           FROM wikipedia_source_receipts w
           JOIN ingestion_runs i ON i.run_id = w.run_id
           WHERE i.source = ? ORDER BY w.run_id""", (SOURCE,),
    ):
        key = _receipt_page_key(row["event_page_id"], row["page_url"])
        if key is None:
            raise ValueError("Research event receipt has an invalid page identity")
        revised = _utc(row["revision_timestamp_utc"], "publisher result revision")
        fetched = _utc(row["fetched_at_utc"], "event receipt fetch")
        if revised > fetched:
            raise ValueError("Research result revision was published after its recorded fetch")
        if revised >= cutoff or fetched >= cutoff:
            raise ValueError("Research result receipt is at or after forecast cutoff")
        if row["crosswalk_run_id"] is not None:
            crosswalk_id = int(row["crosswalk_run_id"])
            if (crosswalk_id >= row["run_id"] or crosswalk_id not in run_times
                    or run_times[crosswalk_id] > fetched
                    or run_sources[crosswalk_id] != "wikipedia_identity_crosswalk"):
                raise ValueError("Research identity crosswalk source or timing is invalid")
        latest_receipts[key] = row

    event_count = 0
    total_held = 0
    for event in connection.execute(
        """SELECT e.event_id, e.source_event_id, e.event_date,
                  COUNT(r.bout_id) AS accepted_bouts
           FROM events e
           LEFT JOIN bouts b ON b.event_id = e.event_id
             AND b.source = ? AND b.status = 'completed'
           LEFT JOIN results r ON r.bout_id = b.bout_id
           WHERE e.source = ? AND e.status = 'completed'
           GROUP BY e.event_id""", (SOURCE, SOURCE),
    ):
        key = _source_page_key(str(event["source_event_id"]))
        receipt = latest_receipts.get(key)
        if receipt is None:
            raise ValueError(f"No exact event result receipt for {event['event_id']}")
        if int(receipt["imported_bouts"]) != int(event["accepted_bouts"]):
            raise ValueError(f"Result count differs from source receipt for {event['event_id']}")
        event_count += 1
        total_held += int(receipt["skipped_unresolved_bouts"])
    if event_count != len(latest_receipts):
        raise ValueError("Research source receipts and completed events differ")
    return {"ingestion_runs": len(runs), "by_source": dict(sorted(sources.items())),
            "latest_fetch_at_utc": latest_fetch, "catalog_sha256": digest.hexdigest(),
            "completed_events_with_exact_receipts": event_count,
            "held_source_bout_rows": total_held}


def captured_research_probabilities(
    database_path: str | Path,
    holdout_report_path: str | Path,
    cutoff_at_utc: str,
    target_event_date: str,
    pairs: list[dict],
) -> dict:
    """Compute transparent forecasts for already-verified future stable-ID pairs.

    The caller must independently verify its target card and bookmaker source.
    ``pairs`` has ``position``, ``fighter_a_id``, and ``fighter_b_id`` only;
    this routine cannot authorize any named matchup or price by itself.
    """
    cutoff = _utc(cutoff_at_utc, "forward decision cutoff")
    if cutoff >= datetime.now(timezone.utc):
        raise ValueError("Forward cutoff must already have occurred")
    try:
        target = date.fromisoformat(target_event_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("Target event date is invalid") from exc
    if (not isinstance(pairs, list) or not pairs
            or any(not isinstance(item, dict) for item in pairs)):
        raise ValueError("At least one verified target pair is required")
    seen: set[int] = set()
    for item in pairs:
        position = item.get("position")
        a_id, b_id = item.get("fighter_a_id"), item.get("fighter_b_id")
        if (type(position) is not int or position < 1 or position in seen
                or not isinstance(a_id, str) or not a_id.startswith("wikipedia:")
                or not a_id.removeprefix("wikipedia:").isdecimal()
                or not isinstance(b_id, str) or not b_id.startswith("wikipedia:")
                or not b_id.removeprefix("wikipedia:").isdecimal()
                or a_id == b_id):
            raise ValueError("Target pair has invalid position or stable fighter IDs")
        seen.add(position)
    database_input = Path(database_path).expanduser()
    if database_input.is_symlink() or not database_input.is_file():
        raise ValueError("Existing research database is required")
    database = database_input.resolve()
    _guard_standalone_database(database)
    database_sha = _sha256_regular_file(database)
    integrity = verify_evidence(database)
    if not integrity["ok"] or integrity["verified_receipts"] != integrity["receipt_count"]:
        raise ValueError("Research database or retained source evidence failed integrity verification")
    report_path = Path(holdout_report_path).expanduser()
    saved, report_sha = _saved_report(report_path)
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        db.require_current_schema(connection)
        catalog = _receipt_catalog(connection, cutoff)
        if catalog["ingestion_runs"] != integrity["receipt_count"]:
            raise ValueError("Source receipt catalog changed during verification")
        source = _source_results(connection)
        if not source:
            raise ValueError("Research history has no completed results")
        if any(date.fromisoformat(str(row["event_date"])) >= min(target, cutoff.date())
               for row in source):
            raise ValueError("Research result date is not strictly before target and cutoff")
        feature_rows, summary = research_feature_rows(connection)
        if summary != saved.get("source_summary"):
            raise ValueError("Saved research holdout is stale for this database")
        train, validation, test = chronological_event_split(feature_rows)
        model = fit_logistic(train)
        calibration = fit_temperature(
            [model.predict_probability(row.features) for row in validation],
            [int(row.target) for row in validation],
        )
        if (_digest_json(model.weights) != saved.get("models", {}).get("logistic_weights_sha256")
                or saved.get("calibration", {}).get("scale") != calibration.scale
                or saved.get("calibration", {}).get("applied") is not True
                or saved.get("calibration", {}).get("validation_binary_bouts") != len(validation)
                or any(saved.get("split", {}).get(name) != _split_summary(group)
                       for name, group in (("train", train), ("validation", validation), ("test", test)))):
            raise ValueError("Saved holdout model or chronological split does not reproduce")
        outcomes = [int(row.target) for row in test]
        test_probs = {
            "elo": [1.0 / (1.0 + 10.0 ** (-row.features[0])) for row in test],
            "logistic_uncalibrated": [model.predict_probability(row.features) for row in test],
        }
        test_probs["logistic_calibrated"] = [
            calibration.predict_probability(value)
            for value in test_probs["logistic_uncalibrated"]
        ]
        if (saved.get("identity_coverage", {}).get("held_source_bout_rows")
                != catalog["held_source_bout_rows"]
                or any(saved.get("test", {}).get(name, {}).get("metrics")
                       != binary_metrics(probabilities, outcomes)
                       for name, probabilities in test_probs.items())):
            raise ValueError("Saved holdout metrics or identity coverage do not reproduce")
        builder = FeatureBuilder()
        for _, same_date in groupby(source, key=lambda row: row["event_date"]):
            builder.update_group(list(same_date))
        forecasts = []
        for item in sorted(pairs, key=lambda value: value["position"]):
            a_id, b_id = item["fighter_a_id"], item["fighter_b_id"]
            features, coverage = builder.values_with_coverage(a_id, b_id, target_event_date)
            if any(coverage) or any(features[6:]):
                raise ValueError("Research forecast unexpectedly used profile or statistic features")
            raw = model.predict_probability(features)
            elo = 1.0 / (1.0 + 10.0 ** (-features[0]))
            adjusted = calibration.predict_probability(raw)
            if not all(math.isfinite(p) and 0.0 < p < 1.0 for p in (elo, raw, adjusted)):
                raise ValueError("Research forecast produced invalid probabilities")
            forecasts.append({
                "position": item["position"], "fighter_a_id": a_id,
                "fighter_b_id": b_id,
                "elo_probability_fighter_a": elo,
                "logistic_raw_probability_fighter_a": raw,
                "logistic_calibrated_probability_fighter_a": adjusted,
                "prior_binary_or_draw_bouts_fighter_a": builder.history.get(a_id).scored_bouts
                if a_id in builder.history else 0,
                "prior_binary_or_draw_bouts_fighter_b": builder.history.get(b_id).scored_bouts
                if b_id in builder.history else 0,
                "feature_values": list(features),
            })
    _guard_standalone_database(database)
    if _sha256_regular_file(database) != database_sha:
        raise ValueError("Research database changed during forward replay")
    if _saved_report(report_path)[1] != report_sha or not verify_evidence(database)["ok"]:
        raise ValueError("Research report or source receipts changed during forward replay")
    report = {
        "schema_version": 1, "version": VERSION,
        "status": "research_only", "research_only": True,
        "promotion_eligible": False, "alert_eligible": False,
        "scenario": "captured_full_result_history",
        "cutoff_at_utc": cutoff_at_utc, "target_event_date": target_event_date,
        "database_path": str(database), "database_sha256": database_sha,
        "holdout_report_path": str(Path(holdout_report_path).expanduser().resolve()),
        "holdout_report_sha256": report_sha,
        "source_summary": summary, "receipt_catalog": catalog,
        "integrity_verified_receipts": integrity["verified_receipts"],
        "model": {
            "feature_names": list(FEATURE_NAMES),
            "historical_holdout_split": saved["split"],
            "logistic_training_bouts": len(train),
            "calibration_validation_bouts": len(validation),
            "historical_test_bouts": len(test),
            "logistic_weights_sha256": _digest_json(model.weights),
            "calibration_scale": calibration.scale,
            "historical_test_brier_elo": saved["test"]["elo"]["metrics"]["brier_score"],
            "historical_test_brier_logistic_calibrated":
                saved["test"]["logistic_calibrated"]["metrics"]["brier_score"],
        },
        "forecasts": forecasts,
        "forecast_rows_sha256": _digest_json(forecasts),
        "limits": [
            "Results were captured before this forward cutoff, but historical pre-fight roster availability was not reconstructed for the saved model holdout.",
            "Accepted Wikipedia fighter identities omit unresolved source bouts; this selection can bias probabilities and test metrics.",
            "The target-card fighter lookup response is hash-verified but lacks a retained fetch timestamp; its availability at the saved odds cutoff is unproven.",
            "Receipt hashes and accepted result counts are checked here; this replay does not independently reparse every historical result against its wikitext.",
            "No dated age, reach, per-fight metrics, or historical bookmaker prices entered this forecast.",
            "A verified research forecast is not an operating model approval, an executable price, or a betting recommendation.",
        ],
    }
    report["checked_input_sha256"] = _digest_json({
        "cutoff_at_utc": cutoff_at_utc, "target_event_date": target_event_date,
        "pairs": pairs, "database_sha256": database_sha,
        "holdout_report_sha256": report_sha,
        "receipt_catalog_sha256": catalog["catalog_sha256"],
        "source_summary": summary,
    })
    return report


def replay_captured_card(
    card_manifest_path: str | Path,
    selected_csv_path: str | Path,
    odds_manifest_path: str | Path,
    database_path: str | Path,
    holdout_report_path: str | Path,
) -> dict:
    """Bind full captured history to one verified provisional card and odds time."""
    card_path = Path(card_manifest_path).expanduser()
    csv_path = Path(selected_csv_path).expanduser()
    odds_path = Path(odds_manifest_path).expanduser()
    for path in (card_path, csv_path, odds_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Saved card or odds input must be a regular file: {path}")
    verified = verify_manifest(card_path)
    selected = _selected_rows(csv_path, verified, provisional=True)
    odds = _odds_input(odds_path.resolve(), verified)
    cutoff = _utc(odds["captured_at_utc"], "saved odds capture")
    card = verified["manifest"]
    if not (_utc(card["captured_at_utc"], "saved card capture") <= cutoff
            < _utc(verified["event_start_utc"], "card start")):
        raise ValueError("Saved card and odds captures are not before the event start")
    selected_positions = {int(row["source_position"]) for row in selected}
    from .archived_forward_replay import _market_observations, _require_unique_saved_odds_events
    _require_unique_saved_odds_events(odds["comparison"], selected_positions)
    pairs = [{
        "position": position,
        "fighter_a_id": row["fighter_a"]["stable_id"],
        "fighter_b_id": row["fighter_b"]["stable_id"],
    } for position, row in sorted(verified["eligible"].items())
        if position in selected_positions]
    result = captured_research_probabilities(
        database_path, holdout_report_path, odds["captured_at_utc"],
        card["event"]["event_date"], pairs,
    )
    source_rows = {row["position"]: row for row in card["fight_card_rows"]}
    for forecast in result["forecasts"]:
        row = source_rows[forecast["position"]]
        books, status = _market_observations(odds, row, card["event"]["event_date"])
        forecast["fighter_a_name"] = row["fighter_a"]["name"]
        forecast["fighter_b_name"] = row["fighter_b"]["name"]
        forecast["odds_pairing_status"] = status
        forecast["saved_two_sided_books"] = books
        forecast["alert_eligible"] = False
    result.update({
        "event_id": f"wikipedia_pilot:{card['event']['source_page_id']}",
        "event_name": card["event"]["name"],
        "event_start_at_utc": verified["event_start_utc"],
        "card_captured_at_utc": card["captured_at_utc"],
        "odds_captured_at_utc": odds["captured_at_utc"],
        "source_card_bouts": verified["card_count"],
        "selected_card_bouts": len(selected),
        "source_identity_hold_positions": sorted(
            set(range(1, verified["card_count"] + 1)) - set(verified["eligible"])),
        "selected_bouts_with_two_sided_saved_books": sum(
            bool(item["saved_two_sided_books"]) for item in result["forecasts"]),
        "card_source_revision_url": card["event"]["source_revision_url"],
        "card_license_url": card["source"]["license_url"],
        "card_manifest_sha256": hashlib.sha256(verified["manifest_bytes"]).hexdigest(),
        "card_source_sha256": hashlib.sha256(verified["raw_bytes"]).hexdigest(),
        "card_fighter_lookup_sha256": hashlib.sha256(verified["lookup_bytes"]).hexdigest(),
        "selected_card_csv_sha256": _sha256_regular_file(csv_path),
        "odds_manifest_sha256": hashlib.sha256(odds["manifest_bytes"]).hexdigest(),
        "odds_response_sha256": hashlib.sha256(odds["raw_bytes"]).hexdigest(),
    })
    # An altered roster or price after the model walk invalidates the report.
    if (verify_manifest(card_path)["manifest_bytes"] != verified["manifest_bytes"]
            or _odds_input(odds_path.resolve(), verified)["raw_bytes"] != odds["raw_bytes"]
            or _sha256_regular_file(csv_path) != result["selected_card_csv_sha256"]):
        raise ValueError("Card or odds evidence changed during captured replay")
    result["checked_input_sha256"] = _digest_json({
        "history_checked_input_sha256": result["checked_input_sha256"],
        "card_manifest_sha256": result["card_manifest_sha256"],
        "card_source_sha256": result["card_source_sha256"],
        "card_fighter_lookup_sha256": result["card_fighter_lookup_sha256"],
        "selected_card_csv_sha256": result["selected_card_csv_sha256"],
        "odds_manifest_sha256": result["odds_manifest_sha256"],
        "odds_response_sha256": result["odds_response_sha256"],
    })
    result["forecast_rows_sha256"] = _digest_json(result["forecasts"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-manifest", type=Path,
                        default=Path("data/raw/ufc332-intake/manifest.json"))
    parser.add_argument("--selected-csv", type=Path,
                        default=Path("data/raw/ufc332-intake/reviewed-card-template.csv"))
    parser.add_argument("--odds-manifest", type=Path,
                        default=Path("data/raw/ufc332-odds-intake/intake_manifest.json"))
    parser.add_argument("--research-db", type=Path,
                        default=Path("data/ufc_research_2011_2025.sqlite"))
    parser.add_argument("--holdout-report", type=Path,
                        default=Path("reports/ufc_research_1993_2026_holdout.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError(f"Output already exists: {args.output}")
        report = replay_captured_card(
            args.card_manifest, args.selected_csv, args.odds_manifest,
            args.research_db, args.holdout_report,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False)
            stream.write("\n")
        print(json.dumps({"output": str(args.output.resolve()),
                          "status": report["status"],
                          "history_result_bouts": report["source_summary"]["accepted_bouts_with_results"],
                          "selected_bouts": report["selected_card_bouts"],
                          "priced_bouts": report["selected_bouts_with_two_sided_saved_books"]}))
    except (OSError, TypeError, ValueError, KeyError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"captured forward replay stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
