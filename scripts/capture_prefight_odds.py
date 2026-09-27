"""Capture one pre-fight MMA h2h odds response beside a verified UFC card.

Run from the repository root, before the first advertised card segment::

    python -m scripts.capture_prefight_odds \
      --card-manifest data/raw/ufc332-intake-YYYYMMDD/manifest.json \
      --output-dir data/raw/ufc332-odds-YYYYMMDDTHHMMSSZ

Set ``ODDS_API_KEY`` in the private local ``.env`` or server environment. The
key and full request URL are never written to the capture. Each run creates a
new directory containing exact response bytes, an odds fetch receipt, an odds
intake manifest, and a *copy* of the card manifest with a conservative coverage
comparison. The source card, reviewer status, databases, and bets are untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from scripts.fetch_historical_revisions import canonical_utc
from scripts.import_reviewed_prefight_card import _odds_input, _resolve_file, verify_manifest
from ufc_odds_model.config import provider_key


ODDS_URL = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
REGIONS = frozenset({"us", "us2", "uk", "eu", "au"})
Fetch = Callable[[str, str], tuple[bytes, str]]
Clock = Callable[[], datetime]
KeyProvider = Callable[[str], str]


def fetch_once(api_key: str, region: str) -> tuple[bytes, str]:
    """Read exact response bytes and record UTC only after the response ends."""
    query = urllib.parse.urlencode({
        "apiKey": api_key, "regions": region, "markets": "h2h",
        "oddsFormat": "decimal", "dateFormat": "iso",
    })
    request = urllib.request.Request(
        f"{ODDS_URL}?{query}", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            captured = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if response.status != 200:
                raise RuntimeError(f"The Odds API returned HTTP {response.status}")
            return raw, captured
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"The Odds API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("The Odds API request failed") from None


def _stamp(value: object, label: str) -> datetime:
    try:
        return canonical_utc(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UTC timestamp") from exc


def _now(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _encoded(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _accent_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _pair(names: tuple[str, str], *, accent: bool) -> frozenset[str]:
    return frozenset(_accent_key(name) if accent else name for name in names)


def _valid_book_count(event: dict, captured: datetime) -> int:
    """Count named books with a complete, observed two-sided h2h market."""
    names = {event["home_team"], event["away_team"]}
    accepted: set[str] = set()
    for bookmaker in event.get("bookmakers") or []:
        if not isinstance(bookmaker, dict):
            continue
        key = bookmaker.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, dict) or market.get("key") != "h2h":
                continue
            updated = market.get("last_update") or bookmaker.get("last_update")
            try:
                updated_at = _stamp(updated, "Bookmaker update")
            except ValueError:
                continue
            if updated_at > captured:
                continue
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list) or len(outcomes) != 2:
                continue
            selections: set[str] = set()
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    break
                name, price = outcome.get("name"), outcome.get("price")
                if (not isinstance(name, str) or type(price) not in (float, int)
                        or not math.isfinite(price) or price <= 1):
                    break
                selections.add(name)
            else:
                if selections == names:
                    accepted.add(key)
    return len(accepted)


def _eligible_events(payload: list[dict], captured: datetime,
                     card_start: datetime, event_date: date,
                     local_zone: ZoneInfo) -> list[dict]:
    seen_ids: set[str] = set()
    eligible: list[dict] = []
    for event in payload:
        if not isinstance(event, dict):
            raise ValueError("The Odds API response contains a non-object event")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id or event_id in seen_ids:
            raise ValueError("The Odds API response has a missing or duplicate event ID")
        seen_ids.add(event_id)
        if event.get("sport_key") != "mma_mixed_martial_arts":
            raise ValueError("The Odds API response contains a non-MMA event")
        first, second = event.get("home_team"), event.get("away_team")
        if (not isinstance(first, str) or not first.strip()
                or not isinstance(second, str) or not second.strip()
                or first == second):
            continue
        try:
            commence = _stamp(event.get("commence_time"), "Odds event start")
        except ValueError:
            continue
        if (commence <= captured or commence < card_start
                or commence.astimezone(local_zone).date() != event_date):
            continue
        eligible.append(event)
    return eligible


def _comparison(card: dict, events: list[dict], captured: datetime,
                odds_manifest_path: Path, odds_manifest_sha: str) -> dict:
    rows = card["fight_card_rows"]
    candidates: dict[int, list[tuple[dict, str]]] = {}
    for row in rows:
        source_names = (row["fighter_a"]["name"], row["fighter_b"]["name"])
        exact = _pair(source_names, accent=False)
        accent = _pair(source_names, accent=True)
        matches: list[tuple[dict, str]] = []
        for event in events:
            odds_names = (event["home_team"], event["away_team"])
            if _pair(odds_names, accent=False) == exact:
                matches.append((event, "exact"))
            elif _pair(odds_names, accent=True) == accent:
                matches.append((event, "accent_normalization"))
        candidates[row["position"]] = matches
    event_use = Counter(event["id"] for matches in candidates.values()
                        for event, _ in matches)

    comparison_rows: list[dict] = []
    for row in rows:
        position = row["position"]
        source_names = (row["fighter_a"]["name"], row["fighter_b"]["name"])
        entry: dict = {"position": position,
                       "wikipedia_fighters": list(source_names)}
        matches = candidates[position]
        source_accents = _pair(source_names, accent=True)
        near = [event for event in events
                if len(source_accents.intersection(_pair(
                    (event["home_team"], event["away_team"]), accent=True))) == 1]
        if (len(matches) > 1 or near and matches
                or (len(matches) == 1 and event_use[matches[0][0]["id"]] > 1)):
            entry["status"] = "ambiguous_pair_review_required"
            entry["candidate_odds_event_ids"] = sorted(
                {event["id"] for event, _ in matches} | {event["id"] for event in near}
            )
        elif matches:
            event, kind = matches[0]
            entry.update({"odds_event_id": event["id"],
                          "odds_api_fighters": [event["home_team"], event["away_team"]],
                          "odds_commence_time_utc": event["commence_time"],
                          "bookmakers_count": len(event.get("bookmakers") or [])})
            usable = _valid_book_count(event, captured)
            entry["usable_two_sided_bookmakers"] = usable
            if (not row["fighter_a"].get("stable_id")
                    or not row["fighter_b"].get("stable_id")
                    or str(row.get("status", "")).startswith("hold_")):
                entry["status"] = "hold_identity_review"
            elif usable == 0:
                entry["status"] = "no_usable_two_sided_price"
            else:
                entry["status"] = kind
        else:
            # An event that shares exactly one fighter is a possible alias or
            # substitution, never enough to authorize an odds pairing.
            if near:
                entry["status"] = "alias_or_opponent_review_required"
                entry["candidate_odds_event_ids"] = sorted(event["id"] for event in near)
            else:
                entry["status"] = "no_odds_event_in_saved_snapshot"
        comparison_rows.append(entry)
    counts = Counter(item["status"] for item in comparison_rows)
    return {
        "comparison_scope": ("One saved same-local-date, future MMA h2h response matched "
                             "to the verified UFC card; manual review pending"),
        "source_manifest_path": str(odds_manifest_path),
        "source_manifest_sha256": odds_manifest_sha,
        "odds_capture_at_utc": captured.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": comparison_rows,
        "summary": {
            "wikipedia_card_rows": len(rows),
            "odds_events_near_date": len(events),
            "exact_pair_names": counts["exact"],
            "accent_only_pair_names": counts["accent_normalization"],
            "manual_alias_or_name_review": (
                counts["alias_or_opponent_review_required"]
                + counts["ambiguous_pair_review_required"]),
            "no_odds_event_in_snapshot": counts["no_odds_event_in_saved_snapshot"],
            "identity_hold_rows": counts["hold_identity_review"],
            "no_usable_two_sided_price": counts["no_usable_two_sided_price"],
        },
        "source_and_identity_limits": ("The Odds API MMA feed covers other promotions. "
                                       "A matched event is not a verified executable price; "
                                       "bookmaker and current card must be checked manually."),
    }


def capture_prefight_odds(
    card_manifest_path: str | Path,
    output_dir: str | Path,
    *,
    region: str = "us",
    fetch: Fetch = fetch_once,
    key_provider: KeyProvider = provider_key,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> dict:
    """Create a separate, source-bound odds snapshot and card comparison."""
    if region not in REGIONS:
        raise ValueError("Region must be one of us, us2, uk, eu, au")
    requested_card = Path(card_manifest_path).expanduser()
    if requested_card.is_symlink():
        raise ValueError("Card manifest must not be a symlink")
    card_path = requested_card.resolve()
    verified = verify_manifest(card_path)
    if verified["source_receipt_bytes"] is None or verified["lookup_receipt_bytes"] is None:
        raise ValueError("Card must have verified source and fighter lookup fetch receipts")
    if "odds_coverage_comparison" in verified["manifest"]:
        raise ValueError("Input card already has an odds comparison; use its fresh source capture")
    if (verified["manifest"].get("review_summary", {}).get(
            "manual_person_review_required") is not True
            or any(row.get("reviewed_by")
                   or (row.get("status") != "source_page_ids_resolved_manual_review_pending"
                       and not str(row.get("status", "")).startswith("hold_"))
                   for row in verified["manifest"]["fight_card_rows"])):
        raise ValueError("Card must be an unreviewed capture with manual review still required")
    card_start = _stamp(verified["event_start_utc"], "Official card start")
    if _now(clock) >= card_start:
        raise ValueError("Official card start has elapsed; odds capture is no longer pre-fight")
    requested_destination = Path(output_dir).expanduser()
    if requested_destination.exists() or requested_destination.is_symlink():
        raise FileExistsError(f"Odds output already exists: {requested_destination}")
    destination = requested_destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Odds output already exists: {destination}")
    key = key_provider("ODDS_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("ODDS_API_KEY is unavailable in the private environment")
    key = key.strip()
    try:
        raw, captured_at = fetch(key, region)
    except Exception:
        # A provider exception can embed its request URL, including the key.
        raise RuntimeError("The Odds API capture failed") from None
    if not isinstance(raw, bytes):
        raise ValueError("The Odds API fetch must return exact response bytes")
    captured = _stamp(captured_at, "Odds response fetch")
    lookup_at = _stamp(verified["lookup_fetched_at_utc"], "Fighter lookup fetch")
    observed_now = _now(clock)
    if (captured < lookup_at or captured > observed_now
            or captured >= card_start or observed_now >= card_start):
        raise ValueError("Odds response must follow the fighter lookup and precede card start")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The Odds API response is not valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("The Odds API response is not an MMA events list")
    local_zone = ZoneInfo(verified["manifest"]["official_start_review"]["local_timezone"])
    event_date = date.fromisoformat(verified["manifest"]["event"]["event_date"])
    events = _eligible_events(payload, captured, card_start, event_date, local_zone)
    raw_sha = hashlib.sha256(raw).hexdigest()
    receipt = {
        "schema_version": 1,
        "provider": "the-odds-api",
        "sport_key": "mma_mixed_martial_arts",
        "market": "h2h",
        "region": region,
        "fetched_at_utc": captured_at,
        "response_sha256": raw_sha,
    }
    receipt_bytes = _encoded(receipt)
    odds_manifest_path = destination / "odds-intake-manifest.json"
    odds_manifest = {
        "schema_version": 1,
        "source": "the-odds-api",
        "kind": "pre-fight h2h coverage intake, not an alert",
        "sport_key": "mma_mixed_martial_arts",
        "region": region,
        "market": "h2h",
        "captured_at_utc": captured_at,
        "raw_path": "odds.response.json",
        "sha256": raw_sha,
        "receipt_path": "odds.receipt.json",
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "mma_events": len(payload),
        "same_local_date_future_events": len(events),
    }
    odds_manifest_bytes = _encoded(odds_manifest)
    card_copy = deepcopy(verified["manifest"])
    for section in ("source", "fighter_lookup"):
        info = card_copy[section]
        for field in ("raw_path", "receipt_path"):
            info[field] = str(_resolve_file(card_path, info[field]).resolve())
    if card_copy.get("event_spec_path") is not None:
        card_copy["event_spec_path"] = str(
            _resolve_file(card_path, card_copy["event_spec_path"]).resolve()
        )
    card_copy["odds_coverage_comparison"] = _comparison(
        card_copy, events, captured, odds_manifest_path,
        hashlib.sha256(odds_manifest_bytes).hexdigest(),
    )

    destination.mkdir(parents=True, exist_ok=False)
    with (destination / "odds.response.json").open("xb") as stream:
        stream.write(raw)
    with (destination / "odds.receipt.json").open("xb") as stream:
        stream.write(receipt_bytes)
    with odds_manifest_path.open("xb") as stream:
        stream.write(odds_manifest_bytes)
    card_copy_path = destination / "card-with-odds-manifest.json"
    with card_copy_path.open("xb") as stream:
        stream.write(_encoded(card_copy))
    copy_verified = verify_manifest(card_copy_path)
    checked = _odds_input(odds_manifest_path, copy_verified)
    if checked["raw_bytes"] != raw:
        raise ValueError("Saved odds response differs from captured bytes")
    return {
        "card_manifest_path": str(card_copy_path),
        "odds_manifest_path": str(odds_manifest_path),
        "captured_at_utc": captured_at,
        "mma_events": len(payload),
        "same_local_date_future_events": len(events),
        "coverage": card_copy["odds_coverage_comparison"]["summary"],
        "review_pending": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-manifest", type=Path, required=True,
                        help="Fresh verified card manifest with both network receipts")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New directory for exact odds bytes and bound card copy")
    parser.add_argument("--region", choices=sorted(REGIONS), default="us")
    args = parser.parse_args(argv)
    try:
        report = capture_prefight_odds(args.card_manifest, args.output_dir,
                                      region=args.region)
    except (FileExistsError, OSError, RuntimeError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"pre-fight odds capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
