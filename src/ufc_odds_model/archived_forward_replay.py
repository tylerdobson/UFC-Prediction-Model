"""Read-only, research-only forward replay for one saved pre-fight card.

All earlier result revisions are selected at the *odds capture time*. The
retrospective publisher receipts retain their actual later fetch times. This
module never imports rows, promotes a model, sends alerts, or records bets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import quote

from scripts.import_reviewed_prefight_card import (
    _odds_input, _selected_rows, verify_manifest,
)

from . import db
from .archived_pit_evaluation import (
    _events, _guard_standalone_database, _lookup_receipts,
    _sha256_regular_file, _utc, _verified_matches, review_archived_card,
)
from .elo import MODEL_VERSION as ELO_VERSION, model_from_results
from .ingest import _name_key
from .odds_api import normalize_h2h


REPLAY_VERSION = "archived-forward-research-v1"
UFC332_ODDS_CAPTURE = "2026-09-26T22:47:55Z"


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _json_digest(value: object) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8"))


def _require_unique_saved_odds_events(comparison: dict, selected_positions: set[int]) -> None:
    safe_ids = [comparison[position].get("odds_event_id")
                for position in selected_positions
                if comparison[position].get("status") in {"exact", "accent_normalization"}]
    if len(safe_ids) != len(set(safe_ids)):
        raise ValueError("One saved odds event maps to multiple selected card bouts")


def _market_observations(
    odds: dict, source_row: dict, card_event_date: str,
) -> tuple[list[dict], str]:
    """Return only unambiguous same-book, two-sided saved observations."""
    position = source_row["position"]
    comparison = odds["comparison"][position]
    status = comparison.get("status")
    if status not in {"exact", "accent_normalization"}:
        return [], str(status or "no_safe_odds_pairing")
    source_event_id = comparison["odds_event_id"]
    event = odds["events"][source_event_id]
    source_names = [source_row[side]["name"] for side in ("fighter_a", "fighter_b")]
    keys = [_name_key(name) for name in source_names]
    if not all(keys) or keys[0] == keys[1]:
        return [], "ambiguous_fighter_name"
    capture = _utc(odds["captured_at_utc"], "odds capture")
    commence = _utc(event.get("commence_time"), "odds event start")
    if commence <= capture:
        return [], "odds_event_started_at_capture"
    if abs((commence.date() - date.fromisoformat(card_event_date)).days) > 1:
        return [], "odds_event_date_mismatch"
    quotes = normalize_h2h([event], odds["captured_at_utc"])
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in quotes:
        if row["source_event_id"] == source_event_id:
            grouped[row["bookmaker_key"]].append(row)
    observations: list[dict] = []
    for bookmaker, rows in sorted(grouped.items()):
        if len(rows) != 2 or {_name_key(row["selection_name"]) for row in rows} != set(keys):
            continue
        if any(_utc(row["bookmaker_updated_at"], "bookmaker update") > capture
               for row in rows):
            continue
        by_name = {_name_key(row["selection_name"]): row for row in rows}
        first, second = by_name[keys[0]], by_name[keys[1]]
        if first["bookmaker_updated_at"] != second["bookmaker_updated_at"]:
            continue
        implied_a, implied_b = 1 / first["decimal_odds"], 1 / second["decimal_odds"]
        updated = _utc(first["bookmaker_updated_at"], "bookmaker update")
        observations.append({
            "bookmaker": bookmaker,
            "source_event_id": source_event_id,
            "bookmaker_updated_at_utc": first["bookmaker_updated_at"],
            "captured_at_utc": odds["captured_at_utc"],
            "bookmaker_update_age_seconds_at_capture": int((capture - updated).total_seconds()),
            "fighter_a_decimal_odds": first["decimal_odds"],
            "fighter_b_decimal_odds": second["decimal_odds"],
            "fighter_a_no_vig_implied_probability": round(
                implied_a / (implied_a + implied_b), 6),
            "executable_price_verified": False,
        })
    return observations, "observed_two_sided" if observations else "no_valid_two_sided_book"


def replay_forward(
    historical_manifest: str | Path,
    card_manifest: str | Path,
    card_csv: str | Path,
    odds_manifest: str | Path,
    research_db: str | Path,
    raw_root: str | Path,
    *,
    expected_capture_at_utc: str = UFC332_ODDS_CAPTURE,
) -> dict:
    """Rebuild Elo from every exact-cutoff prior result; return a held-safe report.

    The saved CSV can have blank reviewer cells because this path is explicitly
    exploratory. A missing or invalid *single* earlier result proof suppresses
    every forecast, rather than yielding a partial-history estimate.
    """
    manifest_path = Path(historical_manifest).expanduser()
    card_path = Path(card_manifest).expanduser()
    csv_path = Path(card_csv).expanduser()
    odds_path = Path(odds_manifest).expanduser()
    database_path = Path(research_db).expanduser()
    for path, label in ((manifest_path, "Historical manifest"),
                        (card_path, "Card manifest"), (csv_path, "Selected card CSV"),
                        (odds_path, "Odds manifest"), (database_path, "Research database")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} must be a regular file")
    cutoff = _utc(expected_capture_at_utc, "expected odds capture")
    manifest_bytes = manifest_path.read_bytes()
    events = _events(json.loads(manifest_bytes))
    verified = verify_manifest(card_path)
    selected = _selected_rows(csv_path, verified, provisional=True)
    odds = _odds_input(odds_path, verified)
    if odds["captured_at_utc"] != expected_capture_at_utc:
        raise ValueError("Saved Odds API capture differs from the required forward cutoff")
    card = verified["manifest"]
    card_capture = _utc(card["captured_at_utc"], "card capture")
    card_date = date.fromisoformat(card["event"]["event_date"])
    if not card_capture <= cutoff < _utc(verified["event_start_utc"], "card start"):
        raise ValueError("Card, odds, and event-start times are out of order")
    if any(date.fromisoformat(item["event_date"]) >= card_date or
           _utc(item["earliest_start_at_utc"], "prior event start") >= cutoff
           for item in events):
        raise ValueError("Historical manifest contains an event at or after the forecast")
    selected_by_position = {int(row["source_position"]): row for row in selected}
    _require_unique_saved_odds_events(odds["comparison"], set(selected_by_position))
    csv_hash = _sha256_regular_file(csv_path)
    db_path = database_path.resolve()
    _guard_standalone_database(db_path)
    db_hash = _sha256_regular_file(db_path)
    proof_evidence: list[dict] = []
    proof_failures: list[dict] = []
    history: list[dict] = []
    seen_bout_ids: set[str] = set()
    used_lookup_ids: set[int] = set()
    source_result_bouts = 0
    uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            raise ValueError("Research database WAL mode cannot bind one main-file digest")
        connection.execute("BEGIN")
        db.require_current_schema(connection)
        for item in events:
            try:
                proof = review_archived_card(connection, item, "result",
                                             expected_capture_at_utc, Path(raw_root).expanduser())
                if (proof.get("slug") != item["slug"] or proof.get("role") != "result"
                        or proof.get("kind") != "completed"
                        or proof.get("event_id") != f"wikipedia_research:{item['page_id']}"
                        or proof.get("event_date") != item["event_date"]
                        or proof.get("cutoff_at_utc") != expected_capture_at_utc
                        or proof.get("decision_cutoff_at_utc") != expected_capture_at_utc
                        or type(proof.get("source_bouts")) is not int
                        or proof["source_bouts"] <= 0
                        or _utc(proof.get("revision_timestamp_utc"), "result revision") > cutoff):
                    raise ValueError("Result proof differs from the exact event/cutoff")
                matches = _verified_matches(proof, completed=True)
                for match in matches.values():
                    bout_id = match["bout_id"]
                    if bout_id in seen_bout_ids:
                        raise ValueError("Duplicate historical bout ID across events")
                    seen_bout_ids.add(bout_id)
                    history.append({
                        "bout_id": bout_id, "event_id": proof["event_id"],
                        "event_date": proof["event_date"],
                        "fighter_a_id": match["fighter_a_id"],
                        "fighter_b_id": match["fighter_b_id"],
                        "outcome": match["outcome"],
                        "winner_fighter_id": match["winner_fighter_id"],
                    })
                    used_lookup_ids.update(match["identity_lookup_run_ids"])
                source_result_bouts += proof["source_bouts"]
                proof_evidence.append({
                    "slug": item["slug"], "page_id": item["page_id"],
                    "cutoff_at_utc": expected_capture_at_utc,
                    "revision_id": proof["revision_id"],
                    "revision_timestamp_utc": proof["revision_timestamp_utc"],
                    "source_oldid_url": proof.get("source_oldid_url"),
                    "publisher_sha1": proof.get("publisher_sha1"),
                    "license_url": proof.get("license_url"),
                    "selection_sidecar_sha256": proof["selection_sidecar_sha256"],
                    "selection_sha256": proof["selection_sha256"],
                    "content_sidecar_sha256": proof["content_sidecar_sha256"],
                    "content_sha256": proof["content_sha256"],
                    "wikitext_sha256": proof["wikitext_sha256"],
                    "selection_fetched_at_utc": proof["selection_fetched_at_utc"],
                    "content_fetched_at_utc": proof["content_fetched_at_utc"],
                    "source_bouts": proof["source_bouts"],
                    "stable_id_result_bouts": len(matches),
                    "held_source_bouts": proof["source_bouts"] - len(matches),
                    "identity_lookup_run_ids": sorted({run_id for match in matches.values()
                                                       for run_id in match["identity_lookup_run_ids"]}),
                })
            except (FileNotFoundError, OSError, TypeError, ValueError, KeyError,
                    json.JSONDecodeError) as exc:
                proof_failures.append({"slug": item["slug"],
                                       "reason": "invalid_or_missing_exact_cutoff_result_proof",
                                       "detail": str(exc)})
        lookup_receipts: list[dict] = []
        if not proof_failures:
            try:
                lookup_receipts = _lookup_receipts(connection, used_lookup_ids)
                if any(_utc(item["fetched_at_utc"], "fighter lookup fetch") > cutoff
                       for item in lookup_receipts):
                    proof_failures.append({
                        "reason": "identity_lookup_fetched_after_forecast_cutoff"})
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                proof_failures.append({"reason": "invalid_identity_lookup_receipt",
                                       "detail": str(exc)})
    _guard_standalone_database(db_path)
    if _sha256_regular_file(db_path) != db_hash:
        raise ValueError("Research database changed during forward replay")
    # Recheck small local inputs after the read-only DB walk, catching concurrent edits.
    if (manifest_path.read_bytes() != manifest_bytes
            or verify_manifest(card_path)["manifest_bytes"] != verified["manifest_bytes"]
            or _sha256_regular_file(csv_path) != csv_hash
            or _odds_input(odds_path, verified)["raw_bytes"] != odds["raw_bytes"]):
        raise ValueError("Forward replay input changed while it was being checked")
    history.sort(key=lambda row: (row["event_date"], row["event_id"], row["bout_id"]))
    ready = not proof_failures and len(proof_evidence) == len(events)
    elo = model_from_results(history) if ready else None
    prior_counts = Counter(fid for result in history if result["outcome"] != "no_contest"
                           for fid in (result["fighter_a_id"], result["fighter_b_id"]))
    bouts: list[dict] = []
    for source_row in card["fight_card_rows"]:
        position = source_row["position"]
        selected_row = selected_by_position.get(position)
        fighter_a, fighter_b = (source_row[side] for side in ("fighter_a", "fighter_b"))
        books, odds_status = _market_observations(
            odds, source_row, card["event"]["event_date"])
        if selected_row is None:
            forecast_status = ("identity_hold" if position not in verified["eligible"]
                               else "not_selected_for_replay")
        elif not ready:
            forecast_status = "held_incomplete_exact_cutoff_history"
        else:
            forecast_status = "exploratory_elo_only"
        bouts.append({
            "source_position": position,
            "fighter_a": fighter_a["name"], "fighter_b": fighter_b["name"],
            "fighter_a_id": fighter_a["stable_id"],
            "fighter_b_id": fighter_b["stable_id"],
            "selected_for_replay": selected_row is not None,
            "identity_reviewer": selected_row["reviewed_by"].strip() if selected_row else None,
            "forecast_status": forecast_status,
            "elo_fighter_a_probability": (
                round(elo.probability(fighter_a["stable_id"], fighter_b["stable_id"]), 6)
                if forecast_status == "exploratory_elo_only" else None),
            "fighter_a_prior_result_bouts_in_cohort": prior_counts.get(fighter_a["stable_id"], 0),
            "fighter_b_prior_result_bouts_in_cohort": prior_counts.get(fighter_b["stable_id"], 0),
            "odds_pairing_status": odds_status,
            "saved_two_sided_books": books,
            "alert_eligible": False,
        })
    inputs = {
        "historical_manifest_sha256": _digest(manifest_bytes),
        "card_manifest_sha256": _digest(verified["manifest_bytes"]),
        "card_source_sha256": _digest(verified["raw_bytes"]),
        "card_fighter_lookup_sha256": _digest(verified["lookup_bytes"]),
        "selected_card_csv_sha256": csv_hash,
        "odds_manifest_sha256": _digest(odds["manifest_bytes"]),
        "odds_response_sha256": _digest(odds["raw_bytes"]),
        "research_database_sha256": db_hash,
        "historical_result_proofs": proof_evidence,
        "historical_identity_lookup_receipts": lookup_receipts,
        "historical_result_rows_sha256": _json_digest(history) if ready else None,
        "exact_cutoff_proof_failures": proof_failures,
    }
    return {
        "schema_version": 1, "replay_version": REPLAY_VERSION,
        "status": "research_only" if ready else "held_incomplete_exact_cutoff_history",
        "research_only": True, "model_eligible": False,
        "promotion_eligible": False, "alert_eligible": False,
        "paper_decisions_created": 0, "bets_placed": 0,
        "event_id": f"wikipedia_pilot:{card['event']['source_page_id']}",
        "event_name": card["event"]["name"], "event_date": card["event"]["event_date"],
        "event_start_at_utc": verified["event_start_utc"],
        "card_captured_at_utc": card["captured_at_utc"],
        "odds_captured_at_utc": odds["captured_at_utc"],
        "history_cutoff_at_utc": expected_capture_at_utc,
        "historical_result_selection_rule":
            "latest_publisher_result_revision_at_exact_odds_capture_for_every_prior_event",
        "card_source_revision_url": card["event"]["source_revision_url"],
        "card_license_url": card["source"]["license_url"],
        "models": {
            "elo": {"version": ELO_VERSION,
                    "status": "exploratory" if ready else "unavailable_incomplete_history"},
            "logistic": {"status": "unavailable",
                         "reason": "No cutoff-safe training-row rebuild or sufficient calibration sample is established for this forward replay."},
        },
        "coverage": {
            "source_card_bouts": verified["card_count"],
            "stable_id_eligible_card_bouts": len(verified["eligible"]),
            "selected_card_bouts": len(selected),
            "selected_bouts_with_exploratory_elo": sum(
                row["forecast_status"] == "exploratory_elo_only" for row in bouts),
            "selected_bouts_with_two_sided_saved_books": sum(
                row["selected_for_replay"] and bool(row["saved_two_sided_books"])
                for row in bouts),
            "historical_events_required": len(events),
            "historical_events_verified": len(proof_evidence),
            "historical_source_result_bouts": source_result_bouts,
            "historical_stable_id_result_bouts": len(history),
        },
        "bouts": bouts,
        "forecast_rows_sha256": _json_digest(bouts),
        "holds": proof_failures,
        "limits": [
            "The card is a provisional community-edited snapshot, not a verified event-day roster.",
            "Odds are saved observations from a prior week, not executable event-day offers.",
            "The April 2026-start historical cohort omits earlier fighter career results.",
            "Retrospective fighter identity lookup does not prove identity availability at each old event cutoff.",
            "The target-card fighter lookup response is hash-verified but lacks a retained fetch timestamp; its availability at the saved odds cutoff is unproven.",
            "Historical publisher revisions were downloaded after this saved decision time; their actual fetch times are retained.",
            "The historical holdout was small and showed no useful betting edge; this report cannot promote Elo.",
            "No stake, betting edge, historical bookmaker return, or ROI is inferred.",
        ],
        "checked_inputs": inputs,
        "checked_input_sha256": _json_digest(inputs),
        "database_access": "read_only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path,
                        default=Path("docs/HISTORICAL_2026_PILOT_CARDS.json"))
    parser.add_argument("--card-manifest", type=Path,
                        default=Path("data/raw/ufc332-intake/manifest.json"))
    parser.add_argument("--card-csv", type=Path,
                        default=Path("data/raw/ufc332-intake/reviewed-card-template.csv"))
    parser.add_argument("--odds-manifest", type=Path,
                        default=Path("data/raw/ufc332-odds-intake/intake_manifest.json"))
    parser.add_argument("--research-db", type=Path,
                        default=Path("data/ufc_research_2011_2025.sqlite"))
    parser.add_argument("--raw-root", type=Path,
                        default=Path("data/raw/historical-revisions"))
    parser.add_argument("--output", type=Path, required=True,
                        help="New report path; refuses overwrite")
    args = parser.parse_args(argv)
    try:
        report = replay_forward(args.manifest, args.card_manifest, args.card_csv,
                                args.odds_manifest, args.research_db, args.raw_root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        print(json.dumps({"output": str(args.output.resolve()),
                          "status": report["status"],
                          "coverage": report["coverage"],
                          "checked_input_sha256": report["checked_input_sha256"]}))
    except (OSError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Forward research replay stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
