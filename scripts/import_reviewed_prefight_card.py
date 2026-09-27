"""Verify a saved pre-fight card for provisional display or explicit review.

This command is deliberately offline. It verifies an immutable MediaWiki page
response and fighter-title lookup, then passes a human-reviewed selection CSV
through the project's ordinary CSV importer. The legacy import remains
``review_pending``. A separate full-card review artifact can create a new
operating candidate database: any held card or odds alias row keeps the whole
event ``review_pending``. A rights basis URL is a human attestation recorded
for audit, not programmatic proof of a license. Neither path scores or bets.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ufc_odds_model import db
from ufc_odds_model.importers import import_bouts_csv
from ufc_odds_model.ingest import import_odds_payload
from ufc_odds_model.integrity import verify_evidence
from ufc_odds_model.pipeline import parse_utc, utc_now, utc_string
from ufc_odds_model.raw_snapshots import retain_snapshot
from ufc_odds_model.wikipedia_history import (
    _BOUT_OPEN,
    _event_date,
    _event_infobox_fields,
    _fighter_ref,
    _resolve_page_ids,
    _split_template,
    _template_text,
)


CSV_COLUMNS = (
    "source_position", "event_id", "event_name", "event_date", "event_status",
    "event_provider_status", "start_time_utc", "source_observed_at_utc",
    "source_url", "source_revision_id", "license_name", "license_url",
    "reviewed_by", "bout_id", "fighter_a_id", "fighter_a_name",
    "fighter_b_id", "fighter_b_name", "bout_status", "bout_provider_status",
    "weight_class", "outcome", "winner_fighter_id", "method",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIGHT_CARD = re.compile(r"^==\s*Fight card\s*==\s*$", re.I | re.M)
_NEXT_SECTION = re.compile(r"^==[^=].*?==\s*$", re.M)


def _read_json(path: Path, label: str) -> tuple[bytes, dict]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    body = path.read_bytes()
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return body, payload


def _resolve_file(manifest_path: Path, stated_path: object) -> Path:
    if not isinstance(stated_path, str) or not stated_path.strip():
        raise ValueError("Manifest is missing a source file path")
    path = Path(stated_path).expanduser()
    if path.is_absolute():
        return path
    beside_manifest = manifest_path.parent / path
    return beside_manifest if beside_manifest.exists() else Path.cwd() / path


def _checked_bytes(path: Path, expected_sha: object, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    if not isinstance(expected_sha, str) or not _SHA256.fullmatch(expected_sha):
        raise ValueError(f"{label} has no valid SHA-256 in its manifest")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_sha:
        raise ValueError(f"{label} SHA-256 mismatch: {path}")
    return body


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UTC timestamp")
    try:
        parsed = parse_utc(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a UTC timestamp") from exc
    if utc_string(parsed) != value:
        raise ValueError(f"{label} must use canonical UTC Z format")
    return parsed


def _capture_receipt(manifest_path: Path, info: dict, response: bytes,
                     request_url: str, label: str) -> tuple[bytes | None, datetime | None]:
    """Verify an optional, content-bound network receipt for a new capture.

    Older one-event manifests have no receipt and remain provisional. A partial
    receipt is rejected; a filename or timestamp alone cannot prove a fetch.
    """
    fields = (info.get("receipt_path"), info.get("receipt_sha256"),
              info.get("fetched_at_utc"))
    if all(value is None for value in fields):
        return None, None
    if any(value is None for value in fields):
        raise ValueError(f"{label} capture receipt is incomplete")
    path = _resolve_file(manifest_path, fields[0])
    receipt_bytes = _checked_bytes(path, fields[1], f"{label} capture receipt")
    try:
        receipt = json.loads(receipt_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} capture receipt is invalid JSON") from exc
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("request_url") != request_url
            or receipt.get("response_sha256") != hashlib.sha256(response).hexdigest()
            or receipt.get("fetched_at_utc") != fields[2]):
        raise ValueError(f"{label} capture receipt differs from response or manifest")
    return receipt_bytes, _timestamp(fields[2], f"{label} fetch")


def _verified_start(manifest: dict) -> datetime:
    event = manifest.get("event") or {}
    review = manifest.get("official_start_review") or {}
    listing = review.get("source_url")
    parsed_url = urlparse(str(listing))
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"ufc.com", "www.ufc.com"}:
        raise ValueError("Official start evidence must link to an HTTPS UFC listing")
    try:
        zone = ZoneInfo(review["local_timezone"])
    except (KeyError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Official start evidence needs a valid local timezone") from exc
    segments = review.get("published_segment_starts")
    if not isinstance(segments, list) or not segments:
        raise ValueError("Official start evidence needs structured segment starts")
    starts: dict[str, datetime] = {}
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("segment"), str):
            raise ValueError("Invalid official card segment")
        name = segment["segment"].strip()
        if not name or name in starts:
            raise ValueError("Duplicate or empty official card segment")
        try:
            local = datetime.fromisoformat(segment["local_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid local start for {name}") from exc
        if local.tzinfo is not None or local.date().isoformat() != event.get("event_date"):
            raise ValueError(f"Local start date for {name} differs from event date")
        assigned = local.replace(tzinfo=zone)
        # Reject ambiguous/nonexistent local wall times, instead of guessing a
        # daylight-saving interpretation for an event in another location.
        if assigned.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
            raise ValueError(f"Ambiguous local start for {name}")
        if assigned.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
            raise ValueError(f"Nonexistent local start for {name}")
        converted = assigned.astimezone(timezone.utc)
        stated = _timestamp(segment.get("utc_time"), f"{name} UTC start")
        if converted != stated:
            raise ValueError(f"Official start conversion mismatch for {name}")
        starts[name] = stated
    earliest = min(starts.values())
    if _timestamp(review.get("event_start_utc_for_conservative_cutoff"), "card cutoff") != earliest:
        raise ValueError("Conservative cutoff is not the first advertised card segment")
    for name, field in (("early_prelims", "early_prelims_start_utc"),
                        ("prelims", "prelims_start_utc"), ("main_card", "main_card_start_utc")):
        if field in review and (name not in starts or _timestamp(review[field], field) != starts[name]):
            raise ValueError(f"Official {field} conflicts with structured segment start")
    return earliest


def _source_rows(page: dict) -> list[dict]:
    source = page["source"]
    start = _FIGHT_CARD.search(source)
    end = _NEXT_SECTION.search(source, start.end()) if start else None
    if start is None or end is None or start.end() >= end.start():
        raise ValueError("Source revision has no bounded Fight card section")
    section = source[start.end():end.start()]
    rows: list[dict] = []
    for position, match in enumerate(_BOUT_OPEN.finditer(section), 1):
        parts = _split_template(_template_text(section, match.start()))
        if parts[0].casefold() != "mmaevent bout" or len(parts) < 9:
            raise ValueError(f"Fight card row {position} has unexpected markup")
        first, second = _fighter_ref(parts[2]), _fighter_ref(parts[4])
        if parts[3].strip().casefold() not in {"vs.", "vs", "v.", "v"}:
            raise ValueError(f"Fight card row {position} is not scheduled")
        if any(part.strip() for part in parts[5:8]):
            raise ValueError(f"Fight card row {position} already has a result")
        rows.append({"position": position, "weight_class": parts[1].strip(),
                     "fighter_a": first, "fighter_b": second})
    if not rows:
        raise ValueError("Source revision has no scheduled Fight card rows")
    return rows


def verify_manifest(manifest_path: str | Path) -> dict:
    """Verify saved source bytes and the manifest's derived event/fighter claims."""
    requested = Path(manifest_path).expanduser()
    if requested.is_symlink():
        raise ValueError("One-event manifest must not be a symlink")
    path = requested.resolve()
    manifest_bytes, manifest = _read_json(path, "One-event manifest")
    event = manifest.get("event") or {}
    source_info = manifest.get("source") or {}
    lookup_info = manifest.get("fighter_lookup") or {}
    raw_path = _resolve_file(path, source_info.get("raw_path"))
    raw_bytes = _checked_bytes(raw_path, source_info.get("raw_sha256"), "MediaWiki REST source")
    try:
        page = json.loads(raw_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("MediaWiki REST source is invalid JSON") from exc
    if not isinstance(page, dict):
        raise ValueError("MediaWiki REST source is not an object")
    revision = page.get("latest") or {}
    license_info = page.get("license") or {}
    if (page.get("title") != event.get("source_page_title")
            or page.get("id") != event.get("source_page_id")
            or revision.get("id") != event.get("source_revision_id")
            or revision.get("timestamp") != event.get("source_revision_timestamp_utc")):
        raise ValueError("MediaWiki source page or revision differs from manifest")
    if (license_info.get("title") != source_info.get("license_title")
            or license_info.get("url") != source_info.get("license_url")
            or not str(license_info.get("url", "")).startswith(
                "https://creativecommons.org/licenses/by-sa/4.0/")):
        raise ValueError("MediaWiki CC BY-SA 4.0 license differs from manifest")
    api_url = source_info.get("api_url")
    if (not isinstance(api_url, str) or urlparse(api_url).hostname != "en.wikipedia.org"
            or "/w/rest.php/v1/page/" not in api_url):
        raise ValueError("Manifest source URL is not the MediaWiki REST page API")
    expected_revision_url = (
        "https://en.wikipedia.org/w/index.php?title="
        + quote(str(page["title"]).replace(" ", "_"), safe="_()")
        + f"&oldid={revision['id']}"
    )
    if event.get("source_revision_url") != expected_revision_url:
        raise ValueError("Manifest revision URL does not identify the saved revision")
    if not isinstance(page.get("source"), str):
        raise ValueError("MediaWiki REST source has no wikitext")
    fields = _event_infobox_fields(page["source"])
    if (fields.get("name") != event.get("name")
            or _event_date(fields.get("date", "")) != event.get("event_date")):
        raise ValueError("Manifest event name/date differ from saved infobox")
    captured = _timestamp(manifest.get("captured_at_utc"), "Source capture")
    revised = _timestamp(revision["timestamp"], "MediaWiki revision")
    start = _verified_start(manifest)
    if not revised <= captured < start:
        raise ValueError("Source revision/capture must precede the advertised card start")
    spec_path, spec_sha = manifest.get("event_spec_path"), manifest.get("event_spec_sha256")
    event_spec_bytes = None
    event_spec_reviewed_at_utc = None
    if spec_path is not None or spec_sha is not None:
        if spec_path is None or spec_sha is None:
            raise ValueError("Event spec path and SHA-256 must appear together")
        event_spec_bytes = _checked_bytes(
            _resolve_file(path, spec_path), spec_sha, "Reviewed event spec"
        )
        try:
            spec = json.loads(event_spec_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Reviewed event spec is invalid JSON") from exc
        if not isinstance(spec, dict) or spec.get("schema_version") != 1:
            raise ValueError("Reviewed event spec has an invalid schema")
        expected_event = {
            "source_page_id": event.get("source_page_id"),
            "source_page_title": event.get("source_page_title"),
            "event_date": event.get("event_date"),
        }
        if (spec.get("event") != expected_event
                or spec.get("source") != {"api_url": api_url}
                or spec.get("official_start_review") != manifest.get("official_start_review")):
            raise ValueError("Reviewed event spec differs from captured event or start")
        if not isinstance(spec.get("reviewed_by"), str) or not spec["reviewed_by"].strip():
            raise ValueError("Reviewed event spec needs a reviewer")
        event_spec_reviewed_at = _timestamp(spec.get("reviewed_at_utc"), "Event spec review")
        if event_spec_reviewed_at > captured:
            raise ValueError("Event spec review follows the source capture")
        event_spec_reviewed_at_utc = utc_string(event_spec_reviewed_at)
    source_receipt_bytes, source_fetched = _capture_receipt(
        path, source_info, raw_bytes, api_url, "MediaWiki REST source")
    if source_fetched is not None and source_fetched != captured:
        raise ValueError("Source capture time differs from its network receipt")

    parsed_rows = _source_rows(page)
    stated_rows = manifest.get("fight_card_rows")
    if not isinstance(stated_rows, list) or len(stated_rows) != len(parsed_rows):
        raise ValueError("Manifest fight-card row count differs from source")
    titles = {fighter.title for row in parsed_rows
              for fighter in (row["fighter_a"], row["fighter_b"]) if fighter.title}
    lookup_path = _resolve_file(path, lookup_info.get("raw_path"))
    lookup_bytes = _checked_bytes(lookup_path, lookup_info.get("raw_sha256"),
                                  "MediaWiki fighter lookup")
    try:
        lookup = json.loads(lookup_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("MediaWiki fighter lookup is invalid JSON") from exc
    lookup_url = lookup_info.get("api_url")
    if not isinstance(lookup_url, str) or urlparse(lookup_url).hostname != "en.wikipedia.org":
        raise ValueError("Fighter lookup URL is not the MediaWiki Action API")
    if not isinstance(lookup, dict) or "query" not in lookup:
        raise ValueError("MediaWiki fighter lookup has no query results")
    lookup_receipt_bytes, lookup_fetched = _capture_receipt(
        path, lookup_info, lookup_bytes, lookup_url, "MediaWiki fighter lookup")
    if lookup_fetched is not None and not captured <= lookup_fetched < start:
        raise ValueError("Fighter lookup fetch must follow the card capture and precede card start")
    # The existing resolver constructs its one <=50-title request. Bind that
    # request to the recorded URL so a lookup for different titles fails.
    if len(titles) > 50:
        raise ValueError("One-event intake requires one saved fighter lookup batch")
    resolved = _resolve_page_ids(
        lambda requested: lookup if requested == lookup_url else
        (_ for _ in ()).throw(ValueError("Fighter lookup request differs from manifest")),
        titles,
    )
    if (lookup_info.get("linked_titles_requested") != len(titles)
            or lookup_info.get("linked_titles_resolved") != len(resolved)):
        raise ValueError("Manifest fighter lookup counts differ from saved response")

    eligible: dict[int, dict] = {}
    for parsed, stated in zip(parsed_rows, stated_rows):
        if not isinstance(stated, dict) or stated.get("position") != parsed["position"]:
            raise ValueError("Manifest fight-card positions differ from source")
        if stated.get("weight_class") != parsed["weight_class"]:
            raise ValueError(f"Manifest weight class differs at position {parsed['position']}")
        for side in ("fighter_a", "fighter_b"):
            fighter = parsed[side]
            actual = stated.get(side) or {}
            page_id = resolved.get(fighter.title) if fighter.title else None
            stable_id = f"wikipedia:{page_id}" if page_id else None
            if (actual.get("name") != fighter.name
                    or actual.get("linked_title") != fighter.title
                    or actual.get("page_id") != page_id
                    or actual.get("stable_id") != stable_id):
                raise ValueError(
                    f"Manifest fighter identity differs at position {parsed['position']} {side}"
                )
        if (stated["fighter_a"]["stable_id"] and stated["fighter_b"]["stable_id"]
                and not str(stated.get("status", "")).startswith("hold_")):
            eligible[parsed["position"]] = stated
    summary = manifest.get("review_summary") or {}
    if (summary.get("fight_card_rows") != len(parsed_rows)
            or summary.get("fully_linked_identity_rows") != len(eligible)
            or summary.get("identity_hold_rows") != len(parsed_rows) - len(eligible)):
        raise ValueError("Manifest card coverage summary does not reconcile")
    return {"manifest": manifest, "manifest_bytes": manifest_bytes,
            "raw_bytes": raw_bytes, "lookup_bytes": lookup_bytes,
            "source_receipt_bytes": source_receipt_bytes,
            "lookup_receipt_bytes": lookup_receipt_bytes,
            "event_spec_bytes": event_spec_bytes,
            "event_spec_reviewed_at_utc": event_spec_reviewed_at_utc,
            "lookup_fetched_at_utc": utc_string(lookup_fetched) if lookup_fetched else None,
            "eligible": eligible, "card_count": len(parsed_rows),
            "event_start_utc": utc_string(start)}


def _template_rows(verified: dict) -> list[dict[str, str]]:
    manifest = verified["manifest"]
    event = manifest["event"]
    source = manifest["source"]
    event_id = f"wikipedia_pilot:{event['source_page_id']}"
    rows = []
    for position, bout in sorted(verified["eligible"].items()):
        rows.append({
            "source_position": str(position), "event_id": event_id,
            "event_name": event["name"], "event_date": event["event_date"],
            "event_status": "scheduled", "event_provider_status": "review_pending",
            "start_time_utc": verified["event_start_utc"],
            "source_observed_at_utc": manifest["captured_at_utc"],
            "source_url": event["source_revision_url"],
            "source_revision_id": str(event["source_revision_id"]),
            "license_name": source["license_title"], "license_url": source["license_url"],
            "reviewed_by": "",
            "bout_id": f"{event_id}:{position}",
            "fighter_a_id": bout["fighter_a"]["stable_id"],
            "fighter_a_name": bout["fighter_a"]["name"],
            "fighter_b_id": bout["fighter_b"]["stable_id"],
            "fighter_b_name": bout["fighter_b"]["name"],
            "bout_status": "scheduled", "bout_provider_status": "review_pending",
            "weight_class": bout["weight_class"], "outcome": "",
            "winner_fighter_id": "", "method": "",
        })
    return rows


def write_template(manifest_path: str | Path, output_path: str | Path) -> dict:
    """Create a candidate CSV; blank reviewer cells block its import."""
    verified = verify_manifest(manifest_path)
    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(_template_rows(verified))
    return {"template_path": str(target.resolve()), "eligible_bouts": len(verified["eligible"]),
            "held_bouts": verified["card_count"] - len(verified["eligible"]),
            "review_required": True}


def _selected_rows(csv_path: Path, verified: dict, *, provisional: bool) -> list[dict[str, str]]:
    if csv_path.is_symlink() or not csv_path.is_file():
        raise ValueError("Reviewed CSV must be a regular file")
    try:
        with io.StringIO(csv_path.read_bytes().decode("utf-8-sig"), newline="") as stream:
            reader = csv.DictReader(stream)
            if not set(CSV_COLUMNS).issubset(reader.fieldnames or []):
                raise ValueError("Reviewed CSV has missing required columns")
            if len(reader.fieldnames or []) != len(set(reader.fieldnames or [])):
                raise ValueError("Reviewed CSV has duplicate headers")
            rows = list(reader)
    except UnicodeError as exc:
        raise ValueError("Reviewed CSV must be UTF-8") from exc
    if not rows:
        raise ValueError("Reviewed CSV selects no bouts")
    templates = {int(row["source_position"]): row for row in _template_rows(verified)}
    seen: set[int] = set()
    for line, row in enumerate(rows, 2):
        try:
            position = int(row.get("source_position") or "")
        except ValueError as exc:
            raise ValueError(f"Line {line}: invalid source_position") from exc
        if position in seen:
            raise ValueError(f"Line {line}: duplicate source_position")
        seen.add(position)
        expected = templates.get(position)
        if expected is None:
            raise ValueError(f"Line {line}: selected bout is held or absent from source")
        for field in CSV_COLUMNS:
            if field == "reviewed_by":
                if not isinstance(row.get(field), str):
                    raise ValueError(f"Line {line}: reviewed_by cell is missing")
                if not provisional and not row[field].strip():
                    raise ValueError(f"Line {line}: reviewed_by is required")
            elif (row.get(field) or "").strip() != expected[field]:
                raise ValueError(f"Line {line}: {field} differs from verified source/template")
        if None in row:
            raise ValueError(f"Line {line}: extra unheaded CSV cells")
    return rows


def _odds_input(odds_manifest_path: Path, verified: dict) -> dict:
    if odds_manifest_path.is_symlink():
        raise ValueError("Odds intake manifest must not be a symlink")
    comparison = verified["manifest"].get("odds_coverage_comparison") or {}
    expected_manifest_sha = comparison.get("source_manifest_sha256")
    odds_manifest_bytes = _checked_bytes(odds_manifest_path, expected_manifest_sha,
                                         "Odds intake manifest")
    odds_manifest = json.loads(odds_manifest_bytes)
    raw_path = _resolve_file(odds_manifest_path, odds_manifest.get("raw_path"))
    raw_bytes = _checked_bytes(raw_path, odds_manifest.get("sha256"), "Exact Odds API response")
    receipt_fields = (odds_manifest.get("receipt_path"), odds_manifest.get("receipt_sha256"))
    receipt_bytes = None
    if any(value is not None for value in receipt_fields):
        if any(value is None for value in receipt_fields):
            raise ValueError("Odds capture receipt is incomplete")
        receipt_path = _resolve_file(odds_manifest_path, receipt_fields[0])
        receipt_bytes = _checked_bytes(receipt_path, receipt_fields[1],
                                       "Odds capture receipt")
        try:
            receipt = json.loads(receipt_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Odds capture receipt is invalid JSON") from exc
        expected_receipt = {
            "schema_version": 1,
            "provider": "the-odds-api",
            "sport_key": "mma_mixed_martial_arts",
            "market": "h2h",
            "region": odds_manifest.get("region"),
            "fetched_at_utc": odds_manifest.get("captured_at_utc"),
            "response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        }
        if receipt != expected_receipt or odds_manifest.get("source") != "the-odds-api":
            raise ValueError("Odds capture receipt differs from response or manifest")
    payload = json.loads(raw_bytes)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("Saved Odds API response must be an MMA events list")
    captured = _timestamp(odds_manifest.get("captured_at_utc"), "Odds capture")
    if captured < _timestamp(verified["manifest"]["captured_at_utc"], "Source capture"):
        raise ValueError("Odds snapshot predates the saved card capture")
    if captured >= _timestamp(verified["event_start_utc"], "Card start"):
        raise ValueError("Odds snapshot was captured at or after card start")
    lookup_fetched = verified.get("lookup_fetched_at_utc")
    if lookup_fetched is not None and _timestamp(lookup_fetched, "Fighter lookup fetch") > captured:
        raise ValueError("Odds snapshot predates the verified fighter lookup fetch")
    if comparison.get("odds_capture_at_utc") != utc_string(captured):
        raise ValueError("Odds capture time differs from source comparison")
    indexed = {str(item.get("id")): item for item in payload}
    if len(indexed) != len(payload):
        raise ValueError("Saved odds response has duplicate event IDs")
    comparison_rows = comparison.get("rows") or []
    if not isinstance(comparison_rows, list) or len(comparison_rows) != verified["card_count"]:
        raise ValueError("Odds comparison does not cover the saved card")
    comparison_by_position = {item.get("position"): item for item in comparison_rows}
    if len(comparison_by_position) != verified["card_count"]:
        raise ValueError("Odds comparison has duplicate card positions")
    source_rows = {row["position"]: row for row in verified["manifest"]["fight_card_rows"]}

    def accent_key(name: str) -> str:
        return "".join(char for char in unicodedata.normalize("NFKD", name.casefold())
                       if not unicodedata.combining(char))

    safe_event_ids: set[str] = set()
    for item in comparison_rows:
        if not isinstance(item, dict) or item.get("position") not in source_rows:
            raise ValueError("Odds comparison has an unknown card position")
        event_id = item.get("odds_event_id")
        if event_id and event_id not in indexed:
            raise ValueError("Odds comparison refers to an absent odds event")
        status = item.get("status")
        if status in {"exact", "accent_normalization", "no_usable_two_sided_price"}:
            if not event_id:
                raise ValueError("Safe odds comparison has no event ID")
            if event_id in safe_event_ids:
                raise ValueError("Safe odds comparison reuses an event ID")
            safe_event_ids.add(event_id)
            event = indexed[event_id]
            if event.get("sport_key") != "mma_mixed_martial_arts":
                raise ValueError("Saved odds event is not MMA")
            source_names = [source_rows[item["position"]][side]["name"]
                            for side in ("fighter_a", "fighter_b")]
            odds_names = [event.get("home_team"), event.get("away_team")]
            if not all(isinstance(name, str) for name in odds_names):
                raise ValueError("Safe odds comparison has missing fighter names")
            if status == "exact" and set(source_names) != set(odds_names):
                raise ValueError("Exact odds comparison differs from source fighters")
            if status == "accent_normalization" and (
                set(source_names) == set(odds_names)
                or set(map(accent_key, source_names)) != set(map(accent_key, odds_names))
            ):
                raise ValueError("Accent odds comparison differs from source fighters")
            if status == "no_usable_two_sided_price":
                if (set(map(accent_key, source_names)) != set(map(accent_key, odds_names))
                        or not all(source_rows[item["position"]][side].get("stable_id")
                                   for side in ("fighter_a", "fighter_b"))):
                    raise ValueError("No-price odds comparison differs from identified source fighters")
                from ufc_odds_model.odds_api import normalize_h2h

                if any(parse_utc(quote["bookmaker_updated_at"]) <= captured for quote in
                       normalize_h2h([event], utc_string(captured))):
                    raise ValueError("No-price odds comparison hides a usable two-sided bookmaker")
            _timestamp(event.get("commence_time"), "Odds event commence time")
    return {"manifest_bytes": odds_manifest_bytes, "raw_bytes": raw_bytes,
            "receipt_bytes": receipt_bytes,
            "payload": payload, "captured_at_utc": utc_string(captured),
            "comparison": comparison_by_position, "events": indexed}


def _source_receipt(connection: sqlite3.Connection, *, source: str, payload: bytes,
                    raw_dir: Path, snapshot_at_utc: str) -> None:
    path, digest = retain_snapshot(payload, raw_dir, kind=source)
    with connection:
        connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256, "
            "snapshot_at_utc) VALUES (?, ?, ?, ?, ?)",
            (source, utc_string(utc_now()), str(path), digest, snapshot_at_utc),
        )


def run_import(manifest_path: str | Path, csv_path: str | Path, output_dir: str | Path,
               odds_manifest_path: str | Path | None = None,
               *, provisional: bool = False) -> dict:
    """Verify all local inputs, then create a new isolated paper-only SQLite DB."""
    verified = verify_manifest(manifest_path)
    selected = _selected_rows(Path(csv_path).expanduser(), verified,
                              provisional=provisional)
    selected_positions = {int(row["source_position"]) for row in selected}
    odds = (_odds_input(Path(odds_manifest_path).expanduser(), verified)
            if odds_manifest_path else None)
    target = Path(output_dir).expanduser()
    if target.exists() or target.is_symlink():
        raise ValueError(f"Output directory already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    database = target / "paper-intake.sqlite"
    raw_dir = target / "raw"
    try:
        # Exclusive creation prevents an existing database from being opened
        # or silently upgraded through db.connect().
        with database.open("xb"):
            pass
        connection = db.connect(database)
        try:
            db.init_db(connection)
            snapshot_at = verified["manifest"]["captured_at_utc"]
            _source_receipt(connection, source="one-event-intake-manifest",
                            payload=verified["manifest_bytes"], raw_dir=raw_dir / "manifest",
                            snapshot_at_utc=snapshot_at)
            _source_receipt(connection, source="wikipedia-prefight-rest",
                            payload=verified["raw_bytes"], raw_dir=raw_dir / "wikipedia",
                            snapshot_at_utc=snapshot_at)
            if verified["source_receipt_bytes"] is not None:
                _source_receipt(connection, source="wikipedia-prefight-rest-fetch-receipt",
                                payload=verified["source_receipt_bytes"],
                                raw_dir=raw_dir / "wikipedia",
                                snapshot_at_utc=snapshot_at)
            _source_receipt(connection, source="wikipedia-prefight-fighter-lookup",
                            payload=verified["lookup_bytes"], raw_dir=raw_dir / "wikipedia",
                            snapshot_at_utc=verified["lookup_fetched_at_utc"] or snapshot_at)
            if verified["lookup_receipt_bytes"] is not None:
                _source_receipt(connection, source="wikipedia-prefight-lookup-fetch-receipt",
                                payload=verified["lookup_receipt_bytes"],
                                raw_dir=raw_dir / "wikipedia",
                                snapshot_at_utc=verified["lookup_fetched_at_utc"])
            imported = import_bouts_csv(connection, csv_path, raw_dir / "reviewed-csv")
            odds_result = None
            if odds is not None:
                _source_receipt(connection, source="odds-intake-manifest",
                                payload=odds["manifest_bytes"], raw_dir=raw_dir / "odds",
                                snapshot_at_utc=odds["captured_at_utc"])
                _source_receipt(connection, source="the-odds-api-exact-response",
                                payload=odds["raw_bytes"], raw_dir=raw_dir / "odds",
                                snapshot_at_utc=odds["captured_at_utc"])
                if odds["receipt_bytes"] is not None:
                    _source_receipt(connection, source="the-odds-api-fetch-receipt",
                                    payload=odds["receipt_bytes"],
                                    raw_dir=raw_dir / "odds",
                                    snapshot_at_utc=odds["captured_at_utc"])
                safe_ids = {odds["comparison"][pos].get("odds_event_id")
                            for pos in selected_positions
                            if odds["comparison"][pos].get("status") in
                            {"exact", "accent_normalization"}}
                safe_payload = [item for item in odds["payload"] if item.get("id") in safe_ids]
                odds_result = import_odds_payload(
                    connection, safe_payload, odds["captured_at_utc"], raw_dir / "odds-derived"
                )
            connection.commit()
        finally:
            connection.close()
        evidence = verify_evidence(database)
        if not evidence["ok"]:
            raise ValueError("Imported database failed source integrity verification")
        comparison = verified["manifest"].get("odds_coverage_comparison") or {}
        comparison_rows = comparison.get("rows") or []
        report = {
            "purpose": "paper-only one-event intake; operator review pending",
            "intake_mode": "provisional_unreviewed" if provisional else "selected_identity_reviewed",
            "decision_ready": False,
            "roster_gate_eligible": False,
            "roster_gate_block_reason": "review_pending provider status",
            "alerts_created": 0,
            "paper_decisions_created": 0,
            "database_path": str(database.resolve()),
            "source_manifest_path": str(Path(manifest_path).expanduser().resolve()),
            "reviewed_csv_path": str(Path(csv_path).expanduser().resolve()),
            "event_id": f"wikipedia_pilot:{verified['manifest']['event']['source_page_id']}",
            "official_conservative_event_start_utc": verified["event_start_utc"],
            "fighter_lookup_fetched_at_utc": verified["lookup_fetched_at_utc"],
            "full_card_bouts": verified["card_count"],
            "source_linked_id_eligible_bouts": len(verified["eligible"]),
            "source_held_bouts": verified["card_count"] - len(verified["eligible"]),
            "selected_bouts": imported,
            "reviewed_selected_bouts": sum(bool((row.get("reviewed_by") or "").strip())
                                          for row in selected),
            "unreviewed_selected_bouts": sum(not (row.get("reviewed_by") or "").strip()
                                            for row in selected),
            "eligible_bouts_not_selected": len(verified["eligible"]) - imported,
            "source_held_positions": sorted(set(range(1, verified["card_count"] + 1))
                                           - set(verified["eligible"])),
            "selected_positions": sorted(selected_positions),
            "odds_replay": odds_result,
            "odds_snapshot_card_coverage": comparison.get("summary") if comparison_rows else None,
            "odds_alias_or_name_hold_positions": sorted(
                item["position"] for item in comparison_rows
                if item.get("status") in {"alias_review_required", "source_name_discrepancy",
                                          "alias_or_opponent_review_required",
                                          "ambiguous_pair_review_required"}
            ),
            "odds_missing_positions": sorted(
                item["position"] for item in comparison_rows
                if item.get("status") == "no_odds_event_in_saved_snapshot"
            ),
            "integrity_ok": True,
            "verified_receipts": evidence["verified_receipts"],
            "receipt_count": evidence["receipt_count"],
            "next_review": "Recheck card, identities and bookmaker lines before any separate decision workflow; capture a new result revision after the event.",
        }
        report_path = target / "paper-intake-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
        return report
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


_ODDS_CLEAR_STATUSES = {
    "exact", "accent_normalization", "no_usable_two_sided_price",
    "no_odds_event_in_saved_snapshot", "not_captured",
}
_REVIEW_DISPOSITIONS = {"approve", "hold", "cancelled", "substituted"}
_REVIEW_SCOPE = "private_betting_decision_support"


def _review_template(verified: dict, odds: dict | None) -> dict:
    manifest = verified["manifest"]
    event = manifest["event"]
    comparison = odds["comparison"] if odds is not None else {}
    rows = []
    for source_row in manifest["fight_card_rows"]:
        position = source_row["position"]
        odds_status = (comparison[position].get("status") if odds is not None
                       else "not_captured")
        rows.append({
            "position": position,
            "fighter_a_name": source_row["fighter_a"]["name"],
            "fighter_a_id": source_row["fighter_a"]["stable_id"],
            "fighter_b_name": source_row["fighter_b"]["name"],
            "fighter_b_id": source_row["fighter_b"]["stable_id"],
            "weight_class": source_row["weight_class"],
            "odds_status": odds_status,
            "roster_disposition": "pending",
            "review_note": "",
        })
    return {
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(verified["manifest_bytes"]).hexdigest(),
        "odds_manifest_sha256": (hashlib.sha256(odds["manifest_bytes"]).hexdigest()
                                 if odds is not None else None),
        "event_id": f"wikipedia_pilot:{event['source_page_id']}",
        "source_revision_id": str(event["source_revision_id"]),
        "event_start_utc": verified["event_start_utc"],
        "source_observed_at_utc": manifest["captured_at_utc"],
        "reviewed_by": "",
        "reviewed_at_utc": "",
        "rights": {
            "decision": "pending",
            "use_scope": _REVIEW_SCOPE,
            "basis_url": "",
            "basis_note": "",
        },
        "rows": rows,
    }


def _review_inputs(manifest_path: str | Path,
                   odds_manifest_path: str | Path | None) -> tuple[dict, dict | None]:
    verified = verify_manifest(manifest_path)
    if verified["source_receipt_bytes"] is None or verified["lookup_receipt_bytes"] is None:
        raise ValueError("Operating review needs timestamped source and fighter lookup fetch receipts")
    for label, value in (
        ("card source", verified["manifest"]["source"].get("api_url")),
        ("fighter lookup", verified["manifest"]["fighter_lookup"].get("api_url")),
    ):
        parsed = urlparse(value) if isinstance(value, str) else None
        if (parsed is None or parsed.scheme != "https" or parsed.hostname != "en.wikipedia.org"
                or parsed.port is not None or parsed.username or parsed.password):
            raise ValueError(f"Operating {label} needs an HTTPS MediaWiki source URL")
    has_comparison = bool(verified["manifest"].get("odds_coverage_comparison"))
    if has_comparison != (odds_manifest_path is not None):
        raise ValueError("Operating review needs the exact saved odds manifest when card comparison exists")
    odds = (_odds_input(Path(odds_manifest_path).expanduser(), verified)
            if odds_manifest_path is not None else None)
    if odds is not None and odds["receipt_bytes"] is None:
        raise ValueError("Operating review needs a timestamped odds fetch receipt")
    if odds is not None:
        # The saved comparison is editable JSON. Rebuild the eligibility set and
        # conservative match decisions from the exact saved response before any
        # row can be treated as clear for the operating roster.
        from scripts.capture_prefight_odds import _comparison, _eligible_events

        manifest = verified["manifest"]
        captured = _timestamp(odds["captured_at_utc"], "Odds capture")
        eligible_events = _eligible_events(
            odds["payload"], captured,
            _timestamp(verified["event_start_utc"], "Card start"),
            date.fromisoformat(manifest["event"]["event_date"]),
            ZoneInfo(manifest["official_start_review"]["local_timezone"]),
        )
        rebuilt = _comparison(
            manifest, eligible_events, captured, Path(odds_manifest_path).expanduser(),
            hashlib.sha256(odds["manifest_bytes"]).hexdigest(),
        )
        for expected in rebuilt["rows"]:
            actual = odds["comparison"][expected["position"]]
            if any(actual.get(field) != expected.get(field) for field in (
                "status", "odds_event_id", "candidate_odds_event_ids",
            )):
                raise ValueError(
                    f"Odds comparison at card row {expected['position']} differs from saved response"
                )
    return verified, odds


def write_review_template(manifest_path: str | Path, output_path: str | Path,
                          odds_manifest_path: str | Path | None = None) -> dict:
    """Create an explicit full-card review artifact with no default approvals."""
    verified, odds = _review_inputs(manifest_path, odds_manifest_path)
    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    artifact = _review_template(verified, odds)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(artifact, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return {"review_path": str(target.resolve()), "card_rows": verified["card_count"],
            "review_required": True, "rights_review_required": True}


def _approved_review(review_path: str | Path, verified: dict, odds: dict | None) -> dict:
    body, review = _read_json(Path(review_path).expanduser(), "Operating card review")
    expected = _review_template(verified, odds)
    fixed = (
        "schema_version", "source_manifest_sha256", "odds_manifest_sha256", "event_id",
        "source_revision_id", "event_start_utc", "source_observed_at_utc",
    )
    if set(review) != set(expected) or any(review.get(key) != expected[key] for key in fixed):
        raise ValueError("Operating review differs from verified source hash, revision, start, or schema")
    reviewer = review.get("reviewed_by")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Operating review needs a named human reviewer")
    reviewed_at = _timestamp(review.get("reviewed_at_utc"), "Operating review time")
    capture_at = _timestamp(verified["manifest"]["captured_at_utc"], "Source capture")
    lookup_at = _timestamp(verified["lookup_fetched_at_utc"], "Fighter lookup fetch")
    now = utc_now()
    start = _timestamp(verified["event_start_utc"], "Card start")
    if not max(capture_at, lookup_at) <= reviewed_at <= now or reviewed_at >= start:
        raise ValueError("Operating review must follow evidence and precede current time and card start")
    if now >= start or now - capture_at > timedelta(hours=24):
        raise ValueError("Operating card capture is stale or the card has started; capture a fresh card")
    if odds is not None and reviewed_at < _timestamp(odds["captured_at_utc"], "Odds capture"):
        raise ValueError("Operating review predates the saved odds comparison")
    rights = review.get("rights")
    if not isinstance(rights, dict) or set(rights) != set(expected["rights"]):
        raise ValueError("Operating review needs a complete source-rights decision")
    basis_url = rights.get("basis_url")
    parsed_basis = urlparse(basis_url) if isinstance(basis_url, str) else None
    if (rights.get("decision") != "approved"
            or rights.get("use_scope") != _REVIEW_SCOPE
            or parsed_basis is None or parsed_basis.scheme != "https"
            or not parsed_basis.hostname or parsed_basis.username or parsed_basis.password
            or not isinstance(rights.get("basis_note"), str)
            or not rights["basis_note"].strip()):
        raise ValueError("Source rights require an explicit reviewed use scope and HTTPS basis")
    actual_rows = review.get("rows")
    expected_rows = expected["rows"]
    if not isinstance(actual_rows, list) or len(actual_rows) != len(expected_rows):
        raise ValueError("Operating review must cover every captured card row")
    approved: list[dict] = []
    dispositions: dict[int, str] = {}
    seen_fighters: set[str] = set()
    for actual, expected_row in zip(actual_rows, expected_rows):
        if not isinstance(actual, dict) or set(actual) != set(expected_row):
            raise ValueError("Operating review card row has an invalid schema")
        if any(actual[key] != expected_row[key] for key in expected_row
               if key not in {"roster_disposition", "review_note"}):
            raise ValueError("Operating review card row differs from captured fighter IDs, odds, or position")
        position = expected_row["position"]
        decision = actual.get("roster_disposition")
        note = actual.get("review_note")
        if decision not in _REVIEW_DISPOSITIONS or not isinstance(note, str):
            raise ValueError(f"Card row {position} needs an explicit review disposition")
        if decision != "approve" and not note.strip():
            raise ValueError(f"Card row {position} needs a hold, cancellation, or substitution reason")
        if decision == "approve":
            fighter_ids = (expected_row["fighter_a_id"], expected_row["fighter_b_id"])
            if (not all(isinstance(item, str) and re.fullmatch(r"wikipedia:[1-9][0-9]*", item)
                        for item in fighter_ids)
                    or fighter_ids[0] == fighter_ids[1]
                    or any(item in seen_fighters for item in fighter_ids)):
                raise ValueError(f"Card row {position} has unresolved or reused stable fighter IDs")
            seen_fighters.update(fighter_ids)
            approved.append(expected_row)
        dispositions[position] = decision
    if not approved:
        raise ValueError("Operating review has no approved bout")
    odds_holds = sorted(row["position"] for row in expected_rows
                        if row["odds_status"] not in _ODDS_CLEAR_STATUSES)
    full_card = len(approved) == len(expected_rows) and not odds_holds
    return {"bytes": body, "review": review, "approved": approved,
            "dispositions": dispositions, "odds_hold_positions": odds_holds,
            "full_card_reconciled": full_card}


def import_approved_card(manifest_path: str | Path, review_path: str | Path,
                         output_dir: str | Path,
                         odds_manifest_path: str | Path | None = None) -> dict:
    """Create a new operating candidate DB; partial reviews stay event-wide held."""
    verified, odds = _review_inputs(manifest_path, odds_manifest_path)
    approval = _approved_review(review_path, verified, odds)
    target = Path(output_dir).expanduser()
    if target.exists() or target.is_symlink():
        raise ValueError(f"Output directory already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    database = target / "operating-card.sqlite"
    raw_dir = target / "raw"
    event_status = "scheduled" if approval["full_card_reconciled"] else "review_pending"
    try:
        with database.open("xb"):
            pass
        connection = db.connect(database)
        try:
            db.init_db(connection)
            snapshot_at = verified["manifest"]["captured_at_utc"]
            for source, payload, at in (
                ("one-event-intake-manifest", verified["manifest_bytes"], snapshot_at),
                ("wikipedia-prefight-rest", verified["raw_bytes"], snapshot_at),
                ("wikipedia-prefight-rest-fetch-receipt", verified["source_receipt_bytes"], snapshot_at),
                ("wikipedia-prefight-fighter-lookup", verified["lookup_bytes"],
                 verified["lookup_fetched_at_utc"]),
                ("wikipedia-prefight-lookup-fetch-receipt", verified["lookup_receipt_bytes"],
                 verified["lookup_fetched_at_utc"]),
                ("operating-card-review", approval["bytes"],
                 approval["review"]["reviewed_at_utc"]),
            ):
                _source_receipt(connection, source=source, payload=payload,
                                raw_dir=raw_dir / "review-evidence", snapshot_at_utc=at)
            if verified["event_spec_bytes"] is not None:
                _source_receipt(connection, source="reviewed-event-spec",
                                payload=verified["event_spec_bytes"],
                                raw_dir=raw_dir / "review-evidence",
                                snapshot_at_utc=verified["event_spec_reviewed_at_utc"])
            if odds is not None:
                for source, payload in (
                    ("odds-intake-manifest", odds["manifest_bytes"]),
                    ("the-odds-api-exact-response", odds["raw_bytes"]),
                    ("the-odds-api-fetch-receipt", odds["receipt_bytes"]),
                ):
                    if payload is not None:
                        _source_receipt(connection, source=source, payload=payload,
                                        raw_dir=raw_dir / "review-evidence",
                                        snapshot_at_utc=odds["captured_at_utc"])
            source_rows = {row["source_position"]: row for row in _template_rows(verified)}
            csv_rows = []
            for row in approval["approved"]:
                selected = dict(source_rows[str(row["position"])])
                selected["event_provider_status"] = event_status
                selected["bout_provider_status"] = event_status
                selected["reviewed_by"] = approval["review"]["reviewed_by"].strip()
                csv_rows.append(selected)
            csv_path = target / "approved-card.csv"
            with csv_path.open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(csv_rows)
            imported = import_bouts_csv(connection, csv_path, raw_dir / "approved-csv")
            connection.commit()
        finally:
            connection.close()
        evidence = verify_evidence(database)
        if not evidence["ok"]:
            raise ValueError("Approved operating database failed source integrity verification")
        report = {
            "purpose": "reviewed single-event operating candidate",
            "database_path": str(database.resolve()),
            "review_path": str(Path(review_path).expanduser().resolve()),
            "event_id": approval["review"]["event_id"],
            "event_provider_status": event_status,
            "full_card_bouts": verified["card_count"],
            "approved_bouts": imported,
            "held_positions": sorted(position for position, decision in approval["dispositions"].items()
                                     if decision != "approve"),
            "odds_alias_or_opponent_hold_positions": approval["odds_hold_positions"],
            "full_card_reconciled": approval["full_card_reconciled"],
            "roster_gate_eligible": approval["full_card_reconciled"],
            "operating_history_results": 0,
            "model_ready": False,
            "decision_ready": False,
            "alerts_created": 0,
            "paper_decisions_created": 0,
            "source_rights_attested": True,
            "source_rights_independently_verified": False,
            "rights_basis_url": approval["review"]["rights"]["basis_url"],
            "integrity_ok": True,
            "receipt_count": evidence["receipt_count"],
            "verified_receipts": evidence["verified_receipts"],
            "next_step": "Import rights-cleared point-in-time results; refresh card and odds near event time before any gate run.",
        }
        (target / "operating-card-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template", help="Create an unreviewed candidate CSV")
    template.add_argument("--manifest", required=True)
    template.add_argument("--output", required=True)
    imported = commands.add_parser("import", help="Replay reviewed CSV into a new isolated DB")
    imported.add_argument("--manifest", required=True)
    imported.add_argument("--csv", required=True)
    imported.add_argument("--output-dir", required=True)
    imported.add_argument("--odds-manifest", help="Optional already-saved Odds API intake manifest")
    imported.add_argument("--provisional", action="store_true",
                          help="Allow blank reviewer cells; keep this isolated intake unavailable for decisions")
    review_template = commands.add_parser("review-template", help="Create an unapproved full-card review JSON")
    review_template.add_argument("--manifest", required=True)
    review_template.add_argument("--odds-manifest")
    review_template.add_argument("--output", required=True)
    approved = commands.add_parser("import-approved", help="Import an explicit review into a new operating candidate DB")
    approved.add_argument("--manifest", required=True)
    approved.add_argument("--odds-manifest")
    approved.add_argument("--review", required=True)
    approved.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            result = write_template(args.manifest, args.output)
        elif args.command == "import":
            result = run_import(args.manifest, args.csv, args.output_dir,
                                args.odds_manifest, provisional=args.provisional)
        elif args.command == "review-template":
            result = write_review_template(args.manifest, args.output, args.odds_manifest)
        else:
            result = import_approved_card(args.manifest, args.review, args.output_dir,
                                          args.odds_manifest)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"one-event intake failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
