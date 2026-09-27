"""Capture a new, unreviewed pre-fight Wikipedia card with fetch receipts.

The seed manifest supplies a previously verified event page ID/date, MediaWiki
REST URL, and manually checked official card start. Its old roster, identity
lookup, odds comparison, and reviewer decisions are never copied. The output
is a new directory of exact responses, post-response UTC receipts, and a
manifest compatible with ``import_reviewed_prefight_card.verify_manifest``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from scripts.fetch_historical_revisions import canonical_utc, fetch_once
from scripts.import_reviewed_prefight_card import _source_rows, verify_manifest
from ufc_odds_model.wikipedia_history import (
    ACTION_API,
    _event_date,
    _event_infobox_fields,
    _resolve_page_ids,
)


Fetch = Callable[[str], tuple[bytes, str]]
Clock = Callable[[], datetime]


def _json_object(body: bytes, label: str) -> dict:
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError(f"{label} is not a successful JSON object")
    return payload


def _stamp(value: str, label: str) -> datetime:
    try:
        return canonical_utc(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical post-response UTC timestamp") from exc


def _utc_now(clock: Clock) -> datetime:
    current = clock()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("Clock must return an aware UTC datetime")
    return current.astimezone(timezone.utc)


def _receipt(url: str, body: bytes, fetched_at_utc: str) -> dict:
    _stamp(fetched_at_utc, "Fetch time")
    return {
        "schema_version": 1,
        "request_url": url,
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "fetched_at_utc": fetched_at_utc,
    }


def _encoded(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _safe_rest_url(url: object) -> str:
    if not isinstance(url, str):
        raise ValueError("Seed has no MediaWiki REST URL")
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != "en.wikipedia.org"
            or parsed.port is not None or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.path.startswith("/w/rest.php/v1/page/")
            or parsed.path == "/w/rest.php/v1/page/"):
        raise ValueError("Seed source URL must be an English Wikipedia REST page URL")
    return url


def _fighter(name: str, title: str | None, resolved: dict[str, int]) -> dict:
    page_id = resolved.get(title) if title else None
    return {
        "name": name,
        "linked_title": title,
        "page_id": page_id,
        "stable_id": f"wikipedia:{page_id}" if page_id else None,
    }


def _write_new(path: Path, body: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(body)


def capture_prefight_card(
    seed_manifest_path: str | Path,
    output_dir: str | Path,
    *,
    fetch: Fetch = fetch_once,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> dict:
    """Save one fresh card and linked-fighter lookup; return output metadata.

    ``fetch`` must return exact response bytes and the UTC time recorded only
    after those bytes were received. The default ``fetch_once`` does so; test
    callers can inject a deterministic implementation without network access.
    """
    seed_path = Path(seed_manifest_path).expanduser()
    if seed_path.is_symlink():
        raise ValueError("Seed manifest must not be a symlink")
    seed_path = seed_path.resolve()
    verified_seed = verify_manifest(seed_path)
    seed = verified_seed["manifest"]
    seed_event = seed["event"]
    expected_page_id = seed_event["source_page_id"]
    expected_date = seed_event["event_date"]
    if type(expected_page_id) is not int or expected_page_id <= 0:
        raise ValueError("Seed event has no valid page ID")
    start = canonical_utc(verified_seed["event_start_utc"])
    if _utc_now(clock) >= start:
        raise ValueError("Official card start has elapsed; cannot capture a pre-fight card")
    source_url = _safe_rest_url(seed["source"].get("api_url"))
    requested_destination = Path(output_dir).expanduser()
    if requested_destination.exists() or requested_destination.is_symlink():
        raise FileExistsError(f"Capture output already exists: {requested_destination}")
    destination = requested_destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Capture output already exists: {destination}")

    source_bytes, source_at = fetch(source_url)
    if not isinstance(source_bytes, bytes):
        raise ValueError("MediaWiki REST fetch must return bytes")
    source_time = _stamp(source_at, "MediaWiki REST fetch")
    if (source_time < canonical_utc(seed["captured_at_utc"])
            or source_time > _utc_now(clock) or source_time >= start
            or _utc_now(clock) >= start):
        raise ValueError("Source capture time is outside the verified pre-fight window")
    page = _json_object(source_bytes, "MediaWiki REST source")
    revision = page.get("latest")
    license_info = page.get("license")
    if (type(page.get("id")) is not int or page["id"] != expected_page_id
            or not isinstance(page.get("title"), str) or not page["title"]
            or not isinstance(revision, dict)
            or type(revision.get("id")) is not int or revision["id"] <= 0
            or not isinstance(revision.get("timestamp"), str)
            or not isinstance(page.get("source"), str)
            or not isinstance(license_info, dict)
            or not isinstance(license_info.get("title"), str)
            or not isinstance(license_info.get("url"), str)
            or not license_info["url"].startswith(
                "https://creativecommons.org/licenses/by-sa/4.0/")):
        raise ValueError("MediaWiki REST source page, revision, or license is invalid")
    revision_time = _stamp(revision["timestamp"], "MediaWiki revision")
    if revision_time > source_time:
        raise ValueError("MediaWiki revision postdates its response capture")
    fields = _event_infobox_fields(page["source"])
    if _event_date(fields.get("date", "")) != expected_date or not fields.get("name"):
        raise ValueError("Fresh MediaWiki event date differs from verified seed")
    parsed_rows = _source_rows(page)
    titles = {fighter.title for row in parsed_rows
              for fighter in (row["fighter_a"], row["fighter_b"]) if fighter.title}
    if not 1 <= len(titles) <= 50:
        raise ValueError("One-event capture requires one nonempty fighter-title batch of at most 50")

    lookup_result: list[tuple[str, bytes, str]] = []

    def fetch_lookup(url: str) -> dict:
        if lookup_result or not url.startswith(ACTION_API + "?"):
            raise ValueError("Fighter lookup must be one English Wikipedia Action API batch")
        raw, fetched_at_utc = fetch(url)
        if not isinstance(raw, bytes):
            raise ValueError("MediaWiki fighter lookup fetch must return bytes")
        fetched = _stamp(fetched_at_utc, "Fighter lookup fetch")
        if (fetched < source_time or fetched > _utc_now(clock)
                or fetched >= start or _utc_now(clock) >= start):
            raise ValueError("Fighter lookup time is outside the verified pre-fight window")
        response = _json_object(raw, "MediaWiki fighter lookup")
        query = response.get("query")
        if (not isinstance(query, dict) or not isinstance(query.get("pages"), list)
                or not query["pages"] or response.get("continue")
                or any(not isinstance(page, dict)
                       or not isinstance(page.get("title"), str)
                       or type(page.get("ns")) is not int
                       for page in query["pages"])):
            raise ValueError("MediaWiki fighter lookup has no pages")
        lookup_result.append((url, raw, fetched_at_utc))
        return response

    resolved = _resolve_page_ids(fetch_lookup, titles)
    if len(lookup_result) != 1:
        raise ValueError("Fighter lookup was not captured as one batch")
    lookup_url, lookup_bytes, lookup_at = lookup_result[0]
    rows: list[dict] = []
    eligible_count = 0
    for row in parsed_rows:
        fighter_a = _fighter(row["fighter_a"].name, row["fighter_a"].title, resolved)
        fighter_b = _fighter(row["fighter_b"].name, row["fighter_b"].title, resolved)
        eligible = (fighter_a["stable_id"] is not None
                    and fighter_b["stable_id"] is not None
                    and fighter_a["stable_id"] != fighter_b["stable_id"])
        eligible_count += eligible
        rows.append({
            "position": row["position"],
            "weight_class": row["weight_class"],
            "fighter_a": fighter_a,
            "fighter_b": fighter_b,
            "status": ("source_page_ids_resolved_manual_review_pending" if eligible
                       else "hold_identity_review"),
        })

    source_receipt = _receipt(source_url, source_bytes, source_at)
    lookup_receipt = _receipt(lookup_url, lookup_bytes, lookup_at)
    source_receipt_bytes = _encoded(source_receipt)
    lookup_receipt_bytes = _encoded(lookup_receipt)
    title = page["title"]
    revision_url = (
        "https://en.wikipedia.org/w/index.php?title="
        + quote(title.replace(" ", "_"), safe="_()")
        + f"&oldid={revision['id']}"
    )
    manifest = {
        "captured_at_utc": source_at,
        "event": {
            "name": fields["name"],
            "event_date": expected_date,
            "source_page_title": title,
            "source_page_id": expected_page_id,
            "source_revision_id": revision["id"],
            "source_revision_timestamp_utc": revision["timestamp"],
            "source_revision_url": revision_url,
        },
        "source": {
            "api_url": source_url,
            "raw_path": "source.response.json",
            "raw_sha256": source_receipt["response_sha256"],
            "receipt_path": "source.receipt.json",
            "receipt_sha256": hashlib.sha256(source_receipt_bytes).hexdigest(),
            "fetched_at_utc": source_at,
            "license_title": license_info["title"],
            "license_url": license_info["url"],
            "attribution": (f"{title}, Wikipedia contributors; link to the exact revision "
                            "and CC BY-SA 4.0 license; identify modifications"),
        },
        "fighter_lookup": {
            "api_url": lookup_url,
            "raw_path": "fighters.response.json",
            "raw_sha256": lookup_receipt["response_sha256"],
            "receipt_path": "fighters.receipt.json",
            "receipt_sha256": hashlib.sha256(lookup_receipt_bytes).hexdigest(),
            "fetched_at_utc": lookup_at,
            "linked_titles_requested": len(titles),
            "linked_titles_resolved": len(resolved),
        },
        "official_start_review": deepcopy(seed["official_start_review"]),
        "fight_card_rows": rows,
        "review_summary": {
            "fight_card_rows": len(rows),
            "fully_linked_identity_rows": eligible_count,
            "identity_hold_rows": len(rows) - eligible_count,
            "manual_person_review_required": True,
            "operator_gate": "Review current official card, substitutions, identities and prices before any decision.",
        },
    }

    # Delay directory creation until both responses and all derived claims have
    # passed validation. Exclusive writes keep a second run from replacing any
    # saved evidence, including a capture that failed partway through writing.
    destination.mkdir(parents=True, exist_ok=False)
    _write_new(destination / "source.response.json", source_bytes)
    _write_new(destination / "source.receipt.json", source_receipt_bytes)
    _write_new(destination / "fighters.response.json", lookup_bytes)
    _write_new(destination / "fighters.receipt.json", lookup_receipt_bytes)
    manifest_path = destination / "manifest.json"
    _write_new(manifest_path, _encoded(manifest))
    verified = verify_manifest(manifest_path)
    return {
        "manifest_path": str(manifest_path),
        "captured_at_utc": source_at,
        "lookup_fetched_at_utc": lookup_at,
        "source_revision_id": revision["id"],
        "fight_card_rows": verified["card_count"],
        "fully_linked_identity_rows": len(verified["eligible"]),
        "identity_hold_rows": len(rows) - len(verified["eligible"]),
        "review_pending": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-manifest", type=Path, required=True,
                        help="Existing verified one-event manifest, used for official start and event identity")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New directory for immutable capture responses, receipts and manifest")
    args = parser.parse_args(argv)
    try:
        result = capture_prefight_card(args.seed_manifest, args.output_dir)
    except (FileExistsError, OSError, TypeError, ValueError, RuntimeError,
            json.JSONDecodeError) as exc:
        print(f"pre-fight card capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
