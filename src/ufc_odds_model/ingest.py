"""Match external MMA odds to verified UFC bouts before storing quotes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import db, odds_api
from .pipeline import parse_utc, utc_now, utc_string


def _name_key(value: str) -> str:
    # The fight data provider may use "Surname, Given" while the odds feed
    # uses "Given Surname". This is still an exact normalized-name match.
    if "," in value:
        surname, given = value.split(",", 1)
        value = f"{given.strip()} {surname.strip()}"
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", normalized.casefold())


def import_live_odds(
    connection: sqlite3.Connection,
    api_key: str,
    raw_dir: str | Path = "data/raw/odds",
    regions: str = "us",
) -> dict[str, int | str]:
    payload = odds_api.fetch_mma_h2h(api_key, regions=regions)
    return import_odds_payload(connection, payload, utc_string(utc_now()), raw_dir)


def import_historical_odds(
    connection: sqlite3.Connection,
    api_key: str,
    as_of_utc: str,
    raw_dir: str | Path = "data/raw/odds",
    regions: str = "us",
) -> dict[str, int | str]:
    """Import a paid historical snapshot, preserving the provider's snapshot time."""
    payload = odds_api.fetch_historical_mma_h2h(api_key, as_of_utc, regions=regions)
    return import_odds_payload(
        connection, payload, payload["timestamp"], raw_dir, historical=True
    )


def import_odds_payload(
    connection: sqlite3.Connection,
    payload: list[dict] | dict,
    captured_at: str,
    raw_dir: str | Path,
    historical: bool = False,
) -> dict[str, int | str]:
    """Save a raw response and only match unambiguous, time-valid UFC quotes."""
    if historical:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Historical odds need a snapshot with data")
        if not isinstance(payload.get("timestamp"), str) or not isinstance(captured_at, str):
            raise ValueError("Historical odds need a snapshot timestamp")
        if utc_string(parse_utc(payload["timestamp"])) != utc_string(parse_utc(captured_at)):
            raise ValueError("Historical capture time must equal the provider snapshot time")
        events = payload["data"]
        source = "the-odds-api-historical"
    else:
        if not isinstance(payload, list):
            raise ValueError("Live odds need an events list")
        events = payload
        source = "the-odds-api"
    captured_at = utc_string(parse_utc(captured_at))
    fetched_at = utc_string(utc_now())
    raw_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    destination = Path(raw_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{captured_at.replace(':', '')}_{digest[:8]}.json"
    path.write_bytes(raw_bytes)
    connection.execute(
        "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) VALUES (?, ?, ?, ?)",
        (source, fetched_at, str(path), digest),
    )

    known_bouts = connection.execute(
        """
        SELECT b.bout_id, b.fighter_a_id, b.fighter_b_id, e.event_date,
               fa.canonical_name AS fighter_a_name, fb.canonical_name AS fighter_b_name
        FROM bouts b
        JOIN events e ON e.event_id = b.event_id
        JOIN fighters fa ON fa.fighter_id = b.fighter_a_id
        JOIN fighters fb ON fb.fighter_id = b.fighter_b_id
        WHERE b.status IN ('scheduled', 'completed')
          AND e.status IN ('scheduled', 'completed')
        """
    ).fetchall()
    normalized = odds_api.normalize_h2h(events, captured_at)
    matched_quotes = 0
    unmatched_quotes = 0
    future_timestamp_quotes = 0
    for quote in normalized:
        if parse_utc(quote["bookmaker_updated_at"]) > parse_utc(captured_at):
            future_timestamp_quotes += 1
            continue
        names = {_name_key(quote["fighter_a"]), _name_key(quote["fighter_b"])}
        commence_date = datetime.fromisoformat(
            quote["commence_time_utc"].replace("Z", "+00:00")
        ).astimezone(timezone.utc).date()
        possible = [
            bout for bout in known_bouts
            if {_name_key(bout["fighter_a_name"]), _name_key(bout["fighter_b_name"])} == names
            and abs((datetime.fromisoformat(bout["event_date"]).date() - commence_date).days) <= 1
        ]
        if len(possible) != 1:
            unmatched_quotes += 1
            continue
        bout = possible[0]
        selection_key = _name_key(quote["selection_name"])
        if selection_key == _name_key(bout["fighter_a_name"]):
            selection_id = bout["fighter_a_id"]
        elif selection_key == _name_key(bout["fighter_b_name"]):
            selection_id = bout["fighter_b_id"]
        else:
            unmatched_quotes += 1
            continue
        db.add_quote(
            connection, bout["bout_id"], quote["bookmaker_key"], selection_id,
            float(quote["decimal_odds"]), quote["captured_at_utc"], source,
            quote["bookmaker_updated_at"],
        )
        matched_quotes += 1
    connection.commit()
    return {
        "matched_quotes": matched_quotes,
        "unmatched_quotes": unmatched_quotes,
        "future_timestamp_quotes": future_timestamp_quotes,
        "snapshot_at_utc": captured_at,
        "raw_path": str(path),
    }
