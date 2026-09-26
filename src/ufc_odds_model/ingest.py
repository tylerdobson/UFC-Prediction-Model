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
from .pipeline import utc_now, utc_string


def _name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", normalized.casefold())


def import_live_odds(
    connection: sqlite3.Connection,
    api_key: str,
    raw_dir: str | Path = "data/raw/odds",
    regions: str = "us",
) -> dict[str, int | str]:
    payload = odds_api.fetch_mma_h2h(api_key, regions=regions)
    captured_at = utc_string(utc_now())
    raw_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    destination = Path(raw_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{captured_at.replace(':', '')}_{digest[:8]}.json"
    path.write_bytes(raw_bytes)
    connection.execute(
        "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) VALUES (?, ?, ?, ?)",
        ("the-odds-api", captured_at, str(path), digest),
    )

    scheduled = connection.execute(
        """
        SELECT b.bout_id, b.fighter_a_id, b.fighter_b_id, e.event_date,
               fa.canonical_name AS fighter_a_name, fb.canonical_name AS fighter_b_name
        FROM bouts b
        JOIN events e ON e.event_id = b.event_id
        JOIN fighters fa ON fa.fighter_id = b.fighter_a_id
        JOIN fighters fb ON fb.fighter_id = b.fighter_b_id
        WHERE b.status = 'scheduled' AND e.status = 'scheduled'
        """
    ).fetchall()
    normalized = odds_api.normalize_h2h(payload, captured_at)
    matched_quotes = 0
    unmatched_quotes = 0
    for quote in normalized:
        names = {_name_key(quote["fighter_a"]), _name_key(quote["fighter_b"])}
        commence_date = datetime.fromisoformat(
            quote["commence_time_utc"].replace("Z", "+00:00")
        ).astimezone(timezone.utc).date()
        possible = [
            bout for bout in scheduled
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
            float(quote["decimal_odds"]), quote["captured_at_utc"], "the-odds-api",
            quote["bookmaker_updated_at"],
        )
        matched_quotes += 1
    connection.commit()
    return {
        "matched_quotes": matched_quotes,
        "unmatched_quotes": unmatched_quotes,
        "raw_path": str(path),
    }
