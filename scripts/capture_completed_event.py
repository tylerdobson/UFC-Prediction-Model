"""Capture a completed MediaWiki event page against a verified pre-fight card.

The exact post-event response and its actual fetch time are retained. Matching
rows remain unreviewed; missing, changed, or nonbinary bouts stay on hold. This
command does not import an operating database or backdate source observations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from scripts.capture_prefight_card import (
    _encoded, _json_object, _receipt, _safe_rest_url, _stamp, _utc_now, _write_new,
)
from scripts.fetch_historical_revisions import fetch_once
from scripts.import_reviewed_prefight_card import verify_manifest
from ufc_odds_model.wikipedia_history import ParsedEvent, parse_event_page_title


Fetch = Callable[[str], tuple[bytes, str]]
Clock = Callable[[], datetime]


def _pair(first: str | None, second: str | None) -> frozenset[str] | None:
    if not first or not second or first == second:
        return None
    return frozenset((first, second))


def _rows_and_holds(parsed: ParsedEvent, prefight_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Match exact linked titles only; never infer a new fighter identity."""
    by_pair: dict[frozenset[str], list[dict]] = {}
    by_title: dict[str, set[int]] = {}
    for row in prefight_rows:
        fighters = (row["fighter_a"], row["fighter_b"])
        for fighter in fighters:
            if fighter["linked_title"]:
                by_title.setdefault(fighter["linked_title"], set()).add(row["position"])
        pair = _pair(*(fighter["linked_title"] for fighter in fighters))
        if pair is not None and all(fighter["stable_id"] for fighter in fighters):
            by_pair.setdefault(pair, []).append(row)

    result_counts = Counter(
        _pair(bout.fighter_a.title, bout.fighter_b.title) for bout in parsed.bouts
    )
    matched_positions: set[int] = set()
    possible_substitution_positions: set[int] = set()
    rows: list[dict] = []
    holds: list[dict] = []
    for bout in parsed.bouts:
        pair = _pair(bout.fighter_a.title, bout.fighter_b.title)
        candidates = by_pair.get(pair, []) if pair is not None else []
        matched = candidates[0] if len(candidates) == 1 and result_counts[pair] == 1 else None
        reasons: list[str] = []
        if not bout.fighter_a.title or not bout.fighter_b.title:
            reasons.append("unlinked_result_identity")
        if pair is not None and result_counts[pair] > 1:
            reasons.append("duplicate_result_pair")
        if len(candidates) > 1:
            reasons.append("ambiguous_prefight_pair")
        if matched is None:
            overlaps = set().union(*(
                by_title.get(title, set()) for title in
                (bout.fighter_a.title, bout.fighter_b.title) if title
            )) if (bout.fighter_a.title or bout.fighter_b.title) else set()
            if overlaps:
                reasons.append("possible_substitution")
                possible_substitution_positions.update(overlaps)
            else:
                reasons.append("unmatched_result_bout")
        else:
            matched_positions.add(matched["position"])
            if matched["weight_class"] != bout.weight_class:
                reasons.append("weight_class_changed")
        if bout.outcome in ("draw", "no_contest"):
            reasons.append(bout.outcome)

        identity_by_title = {
            fighter["linked_title"]: fighter["stable_id"]
            for fighter in (matched["fighter_a"], matched["fighter_b"])
        } if matched is not None else {}
        fighters = [
            {"name": fighter.name, "linked_title": fighter.title,
             "stable_id": identity_by_title.get(fighter.title)}
            for fighter in (bout.fighter_a, bout.fighter_b)
        ]
        winner = fighters[0]["stable_id"] if bout.winner_side == "a" else None
        item = {
            "position": bout.position,
            "weight_class": bout.weight_class,
            "fighter_a": fighters[0], "fighter_b": fighters[1],
            "outcome": bout.outcome, "winner_fighter_id": winner,
            "method": bout.method,
            "prefight_position": matched["position"] if matched is not None else None,
            "status": "hold" if reasons else "matched_unreviewed",
            "hold_reasons": reasons,
        }
        rows.append(item)
        for reason in reasons:
            holds.append({"scope": "result", "position": bout.position, "reason": reason})

    for row in prefight_rows:
        position = row["position"]
        if position in matched_positions:
            continue
        if not all(row[side]["stable_id"] for side in ("fighter_a", "fighter_b")):
            holds.append({"scope": "prefight", "position": position,
                          "reason": "unlinked_prefight_identity"})
        holds.append({"scope": "prefight", "position": position,
                      "reason": ("possible_substitution" if position in possible_substitution_positions
                                 else "possible_cancellation_or_unreported")})
    return rows, holds


def _derive_manifest(prefight_path: Path, verified: dict, raw: bytes, fetched_at: str) -> dict:
    seed = verified["manifest"]
    event = seed["event"]
    page = _json_object(raw, "Post-event MediaWiki REST source")
    if (type(page.get("id")) is not int or page["id"] != event["source_page_id"]
            or page.get("title") != event["source_page_title"]):
        raise ValueError("Post-event page ID or title differs from verified pre-fight event")
    revision = page.get("latest")
    if (not isinstance(revision, dict) or type(revision.get("id")) is not int
            or revision["id"] <= 0 or not isinstance(revision.get("timestamp"), str)):
        raise ValueError("Post-event page has no valid source revision")
    revised = _stamp(revision["timestamp"], "Post-event revision")
    start = _stamp(verified["event_start_utc"], "Verified event start")
    fetched = _stamp(fetched_at, "Post-event fetch")
    if (revised < start or revised > fetched or fetched < start
            or revised <= _stamp(event["source_revision_timestamp_utc"], "Pre-fight revision")
            or revision["id"] == event["source_revision_id"]):
        raise ValueError("Post-event revision and fetch must follow the verified event start")
    license_info = page.get("license")
    if (not isinstance(license_info, dict)
            or license_info.get("title") != seed["source"]["license_title"]
            or license_info.get("url") != seed["source"]["license_url"]):
        raise ValueError("Post-event source license differs from verified pre-fight source")
    parsed = parse_event_page_title(page, event["source_page_title"])
    if parsed.event_date != event["event_date"]:
        raise ValueError("Post-event date differs from verified pre-fight event")
    rows, holds = _rows_and_holds(parsed, seed["fight_card_rows"])
    source_url = _safe_rest_url(seed["source"]["api_url"])
    source_receipt = _receipt(source_url, raw, fetched_at)
    receipt_bytes = _encoded(source_receipt)
    revision_url = (
        "https://en.wikipedia.org/w/index.php?title="
        + quote(parsed.title.replace(" ", "_"), safe="_()")
        + f"&oldid={parsed.revision_id}"
    )
    return {
        "schema_version": 1,
        "purpose": "post_event_result_capture_unreviewed",
        "prefight_anchor": {
            "manifest_path": str(prefight_path),
            "manifest_sha256": hashlib.sha256(verified["manifest_bytes"]).hexdigest(),
            "source_revision_id": event["source_revision_id"],
            "source_response_sha256": seed["source"]["raw_sha256"],
        },
        "event": {
            "source_page_id": parsed.page_id, "source_page_title": parsed.title,
            "event_date": parsed.event_date, "event_name": parsed.event_name,
            "verified_start_utc": verified["event_start_utc"],
        },
        "source": {
            "api_url": source_url,
            "raw_path": "source.response.json",
            "raw_sha256": source_receipt["response_sha256"],
            "receipt_path": "source.receipt.json",
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "fetched_at_utc": fetched_at,
            "revision_id": parsed.revision_id,
            "revision_timestamp_utc": parsed.revision_timestamp_utc,
            "revision_url": revision_url,
            "license_title": parsed.license_title,
            "license_url": parsed.license_url,
        },
        "completed_bouts": rows,
        "holds": holds,
        "review_summary": {
            "source_result_rows": len(rows),
            "matched_unreviewed_rows": sum(row["status"] == "matched_unreviewed" for row in rows),
            "held_result_rows": sum(row["status"] == "hold" for row in rows),
            "held_prefight_rows": len({hold["position"] for hold in holds
                                       if hold["scope"] == "prefight"}),
            "manual_review_required": True,
            "operating_import_eligible": False,
        },
    }


def verify_completed_capture(manifest_path: str | Path) -> dict:
    """Recompute the saved claims from the exact source and pre-fight receipts."""
    path = Path(manifest_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Completed capture manifest must be a regular file")
    path = path.resolve()
    manifest = _json_object(path.read_bytes(), "Completed capture manifest")
    anchor = manifest.get("prefight_anchor")
    source = manifest.get("source")
    if not isinstance(anchor, dict) or not isinstance(source, dict):
        raise ValueError("Completed capture lacks anchor or source evidence")
    anchor_path = Path(str(anchor.get("manifest_path", "")))
    if anchor_path.is_symlink() or not anchor_path.is_file():
        raise ValueError("Pre-fight anchor is unavailable")
    verified = verify_manifest(anchor_path)
    if hashlib.sha256(verified["manifest_bytes"]).hexdigest() != anchor.get("manifest_sha256"):
        raise ValueError("Pre-fight anchor hash changed")
    if source.get("raw_path") != "source.response.json" or source.get("receipt_path") != "source.receipt.json":
        raise ValueError("Completed source paths differ from fixed capture files")
    raw_path = path.parent / "source.response.json"
    receipt_path = path.parent / "source.receipt.json"
    if any(item.is_symlink() or not item.is_file() for item in (raw_path, receipt_path)):
        raise ValueError("Completed source or receipt is unavailable")
    raw = raw_path.read_bytes()
    receipt_bytes = receipt_path.read_bytes()
    if (hashlib.sha256(raw).hexdigest() != source.get("raw_sha256")
            or hashlib.sha256(receipt_bytes).hexdigest() != source.get("receipt_sha256")):
        raise ValueError("Completed source or receipt hash changed")
    expected = _derive_manifest(anchor_path.resolve(), verified, raw, source.get("fetched_at_utc"))
    if receipt_bytes != _encoded(_receipt(expected["source"]["api_url"], raw,
                                         source["fetched_at_utc"])):
        raise ValueError("Completed source fetch receipt differs from response")
    if manifest != expected:
        raise ValueError("Completed capture claims differ from verified source")
    return manifest


def capture_completed_event(
    prefight_manifest_path: str | Path,
    output_dir: str | Path,
    *,
    fetch: Fetch = fetch_once,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> dict:
    """Fetch once after the event and save a new, review-pending result capture."""
    anchor_path = Path(prefight_manifest_path).expanduser()
    if anchor_path.is_symlink():
        raise ValueError("Pre-fight anchor must not be a symlink")
    anchor_path = anchor_path.resolve()
    verified = verify_manifest(anchor_path)
    if verified["source_receipt_bytes"] is None or verified["lookup_receipt_bytes"] is None:
        raise ValueError("Post-event capture needs timestamped pre-fight source and identity receipts")
    start = _stamp(verified["event_start_utc"], "Verified event start")
    if _utc_now(clock) < start:
        raise ValueError("Verified event start has not elapsed")
    destination = Path(output_dir).expanduser()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Capture output already exists: {destination}")
    destination = destination.resolve()
    url = _safe_rest_url(verified["manifest"]["source"]["api_url"])
    raw, fetched_at = fetch(url)
    if not isinstance(raw, bytes):
        raise ValueError("Post-event MediaWiki REST fetch must return bytes")
    fetched = _stamp(fetched_at, "Post-event fetch")
    if (fetched < start or fetched < _stamp(verified["lookup_fetched_at_utc"], "Pre-fight lookup")
            or fetched > _utc_now(clock)):
        raise ValueError("Post-event fetch time is outside the verified window")
    manifest = _derive_manifest(anchor_path, verified, raw, fetched_at)
    receipt = _encoded(_receipt(url, raw, fetched_at))
    destination.mkdir(parents=True, exist_ok=False)
    _write_new(destination / "source.response.json", raw)
    _write_new(destination / "source.receipt.json", receipt)
    manifest_path = destination / "manifest.json"
    _write_new(manifest_path, _encoded(manifest))
    verified_capture = verify_completed_capture(manifest_path)
    return {
        "manifest_path": str(manifest_path),
        "source_revision_id": verified_capture["source"]["revision_id"],
        "source_result_rows": verified_capture["review_summary"]["source_result_rows"],
        "matched_unreviewed_rows": verified_capture["review_summary"]["matched_unreviewed_rows"],
        "held_result_rows": verified_capture["review_summary"]["held_result_rows"],
        "held_prefight_rows": verified_capture["review_summary"]["held_prefight_rows"],
        "review_pending": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefight-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = capture_completed_event(args.prefight_manifest, args.output_dir)
    except (FileExistsError, OSError, TypeError, ValueError, RuntimeError,
            json.JSONDecodeError) as exc:
        print(f"completed event capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
