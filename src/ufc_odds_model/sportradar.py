"""Import UFC cards and results from Sportradar MMA v2 Daily Summaries.

Sportradar calls a UFC card a season and an individual fight a sport event.
Only records explicitly tagged with the UFC category are accepted. The raw
response is retained so identity, status, and result decisions can be audited.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import db
from .card_history import record_card_snapshots
from .pipeline import utc_now, utc_string
from .raw_snapshots import retain_snapshot


SOURCE = "sportradar"
UFC_CATEGORY_ID = "sr:category:1089"
BASE_URL = "https://api.sportradar.com/mma"
_ACTIVE_STATUSES = {"not_started", "match_about_to_start"}
_FINISHED_STATUSES = {"ended", "closed"}
_CANCELLED_STATUSES = {"cancelled"}
_NON_SCOREABLE_UNREPRESENTED = {
    "started", "live", "postponed", "suspended", "delayed", "interrupted"
}


def fetch_daily_summaries(
    api_key: str, day: str | date, access_level: str = "trial"
) -> dict:
    """Fetch one UTC schedule date using Sportradar's x-api-key header."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("A Sportradar MMA API key is required")
    if access_level not in {"trial", "production"}:
        raise ValueError("access_level must be trial or production")
    day_text = _day(day)
    url = f"{BASE_URL}/{access_level}/v2/en/schedules/{day_text}/summaries.json"
    request = Request(
        url,
        headers={"Accept": "application/json", "x-api-key": api_key.strip()},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = _http_detail(exc, api_key.strip())
        raise RuntimeError(f"Sportradar MMA returned HTTP {exc.code}{detail}") from exc
    except URLError as exc:
        reason = str(exc.reason).replace(api_key.strip(), "[redacted]")
        raise RuntimeError(f"Could not reach Sportradar MMA: {reason}") from exc
    except OSError as exc:
        raise RuntimeError("Could not reach Sportradar MMA") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Sportradar MMA returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("summaries"), list):
        raise RuntimeError("Sportradar MMA returned an unexpected Daily Summaries payload")
    return payload


def normalize_daily_summaries(payload: dict) -> list[dict]:
    """Group verified UFC fight summaries into card records keyed by season ID.

    Live, delayed and postponed states retain their provider status. The
    scoring pipeline excludes them. This function never infers cancellation
    from an absent fight in a one-day response.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("summaries"), list):
        raise ValueError("Expected a Sportradar Daily Summaries object")
    grouped: dict[str, dict] = {}
    seen_bouts: set[str] = set()
    for index, summary in enumerate(payload["summaries"]):
        if not isinstance(summary, dict):
            raise ValueError(f"Summary {index} is not an object")
        sport_event = summary.get("sport_event")
        if not isinstance(sport_event, dict):
            raise ValueError(f"Summary {index} has no sport_event object")
        context = sport_event.get("sport_event_context", summary.get("sport_event_context"))
        if not isinstance(context, dict):
            continue
        category = context.get("category")
        if not isinstance(category, dict) or category.get("id") != UFC_CATEGORY_ID:
            continue
        season = context.get("season")
        if not isinstance(season, dict):
            raise ValueError(f"UFC summary {index} has no season identity")
        season_id = _source_id(season.get("id"), "season")
        bout_id = _source_id(sport_event.get("id"), "sport_event")
        if bout_id in seen_bouts:
            raise ValueError(f"Duplicate UFC sport event ID: {bout_id}")
        seen_bouts.add(bout_id)

        competition = context.get("competition")
        name = _text(season.get("name")) or (
            _text(competition.get("name")) if isinstance(competition, dict) else None
        )
        if not name:
            raise ValueError(f"UFC season {season_id} has no name")
        start_time = _timestamp(sport_event.get("start_time"))
        season_date = season.get("start_date")
        event_date = _day(season_date) if season_date else (
            start_time[:10] if start_time else None
        )
        if event_date is None:
            raise ValueError(f"UFC sport event {bout_id} has no usable date")
        event = grouped.get(season_id)
        if event is None:
            event = {
                "source_event_id": season_id,
                "name": name,
                "event_date": event_date,
                "start_time_utc": start_time,
                "bouts": [],
            }
            grouped[season_id] = event
        elif event["event_date"] != event_date:
            raise ValueError(f"UFC season {season_id} has inconsistent event dates")
        elif start_time and (
            event["start_time_utc"] is None or start_time < event["start_time_utc"]
        ):
            event["start_time_utc"] = start_time

        status_data = summary.get("sport_event_status")
        if not isinstance(status_data, dict):
            status_data = {}
        source_status = _text(status_data.get("status"))
        if not source_status:
            raise ValueError(f"UFC sport event {bout_id} has no status")
        if sport_event.get("replaced_by") and source_status in _FINISHED_STATUSES:
            raise ValueError(f"Completed UFC sport event {bout_id} also has replaced_by")
        if source_status in _CANCELLED_STATUSES or sport_event.get("replaced_by"):
            status = "cancelled"
        elif source_status in _ACTIVE_STATUSES:
            status = "scheduled"
        elif source_status in _FINISHED_STATUSES:
            status = "completed"
        elif source_status in _NON_SCOREABLE_UNREPRESENTED:
            status = "scheduled"
        else:
            raise ValueError(f"UFC sport event {bout_id} has unknown status {source_status!r}")
        competitors = sport_event.get("competitors")
        if not isinstance(competitors, list):
            competitors = []
        fighters = []
        for competitor in competitors:
            if not isinstance(competitor, dict) or competitor.get("virtual") is True:
                continue
            fighter_id = _source_id(competitor.get("id"), "competitor")
            fighter_name = _display_name(competitor.get("name"))
            if not fighter_name:
                raise ValueError(f"UFC competitor {fighter_id} has no name")
            fighters.append({
                "source_fighter_id": fighter_id,
                "name": fighter_name,
                "qualifier": _text(competitor.get("qualifier")),
            })
        if len(fighters) not in {0, 2}:
            raise ValueError(f"UFC sport event {bout_id} has {len(fighters)} real competitors")
        if len(fighters) == 2 and fighters[0]["source_fighter_id"] == fighters[1]["source_fighter_id"]:
            raise ValueError(f"UFC sport event {bout_id} repeats one competitor")

        outcome = None
        winner_source_id = None
        if status == "completed":
            winner = _text(status_data.get("winner"))
            raw_winner_id = status_data.get("winner_id")
            if winner in {"draw", "no_contest"}:
                if raw_winner_id:
                    raise ValueError(f"UFC sport event {bout_id} has a winner on {winner}")
                outcome = winner
            elif raw_winner_id or winner in {"home_team", "away_team"}:
                if raw_winner_id:
                    winner_source_id = _source_id(raw_winner_id, "competitor")
                else:
                    qualifier = "home" if winner == "home_team" else "away"
                    matches = [
                        fighter for fighter in fighters if fighter["qualifier"] == qualifier
                    ]
                    if len(matches) != 1:
                        raise ValueError(f"UFC sport event {bout_id} cannot resolve {winner}")
                    winner_source_id = matches[0]["source_fighter_id"]
                if winner_source_id not in {f["source_fighter_id"] for f in fighters}:
                    raise ValueError(f"UFC sport event {bout_id} has an unknown winner ID")
                if winner in {"home_team", "away_team"}:
                    expected = "home" if winner == "home_team" else "away"
                    declared = next(
                        f["qualifier"] for f in fighters
                        if f["source_fighter_id"] == winner_source_id
                    )
                    if declared and declared != expected:
                        raise ValueError(f"UFC sport event {bout_id} has conflicting winner fields")
                outcome = "win"
            elif winner:
                raise ValueError(f"UFC sport event {bout_id} has unknown winner {winner!r}")

        incomplete_result = status == "completed" and outcome is None
        if incomplete_result:
            # An ended fight without a resolved result cannot enter training or
            # settlement. Provider status blocks it from pre-fight scoring.
            status = "scheduled"

        rounds = status_data.get("scheduled_length")
        if rounds is not None and (
            not isinstance(rounds, int) or isinstance(rounds, bool) or rounds <= 0
        ):
            raise ValueError(f"UFC sport event {bout_id} has invalid scheduled_length")
        event["bouts"].append({
            "source_bout_id": bout_id,
            "fighters": fighters,
            "status": status,
            "source_status": source_status,
            "replaced_by": _text(sport_event.get("replaced_by")),
            "weight_class": _text(status_data.get("weight_class")),
            "scheduled_rounds": rounds,
            "outcome": outcome,
            "incomplete_result": incomplete_result,
            "winner_source_id": winner_source_id,
            "method": _text(status_data.get("method")),
        })
    return list(grouped.values())


def import_daily_summaries(
    connection: sqlite3.Connection,
    api_key: str,
    day: str | date,
    raw_dir: str | Path = "data/raw/sportradar",
    access_level: str = "trial",
) -> dict[str, int | str]:
    """Fetch, archive, validate, and upsert one Daily Summaries response."""
    day_text = _day(day)
    payload = fetch_daily_summaries(api_key, day_text, access_level=access_level)
    fetched_at = utc_string(utc_now())
    raw_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path, digest = retain_snapshot(raw_bytes, raw_dir)
    events = normalize_daily_summaries(payload)
    imported_bouts = imported_results = non_scoreable_bouts = skipped_incomplete = 0
    card_snapshots: list[dict] = []
    with connection:
        for event in events:
            event_id = f"sportradar:{event['source_event_id']}"
            card_snapshot = {
                "event_id": event_id,
                "source_observed_at_utc": fetched_at,
                "observation_basis": "local_fetch",
                "source_url": f"{BASE_URL}/{access_level}/v2/en/schedules/{day_text}/summaries.json",
                "source_revision_id": None,
                "license_name": None,
                "license_url": None,
                "reviewed_by": None,
                "event_name": event["name"],
                "event_date": event["event_date"],
                "start_time_utc": event["start_time_utc"],
                "event_status": "scheduled",
                "event_provider_status": "scheduled",
                "bouts": [],
            }
            # A daily response can contain only part of a card. Do not mark an
            # event complete until the imported database has no scheduled bout.
            db.upsert_event(
                connection, event_id, event["name"], event["event_date"],
                "scheduled", source=SOURCE,
                source_event_id=event["source_event_id"],
                start_time_utc=event["start_time_utc"],
                provider_status="scheduled",
            )
            for bout in event["bouts"]:
                if bout["source_status"] in _NON_SCOREABLE_UNREPRESENTED:
                    non_scoreable_bouts += 1
                if bout["incomplete_result"]:
                    skipped_incomplete += 1
                fighters = bout["fighters"]
                if len(fighters) != 2:
                    existing_id = f"sportradar:{bout['source_bout_id']}"
                    existing = connection.execute(
                        "SELECT status FROM bouts WHERE bout_id = ?", (existing_id,)
                    ).fetchone()
                    if existing and existing["status"] != "completed":
                        connection.execute(
                            "UPDATE bouts SET status = ?, provider_status = ? WHERE bout_id = ?",
                            (bout["status"], bout["source_status"], existing_id),
                        )
                    if not bout["incomplete_result"]:
                        skipped_incomplete += 1
                    continue
                resolved_fighter_ids: dict[str, str] = {}
                for fighter in fighters:
                    source_id = fighter["source_fighter_id"]
                    fighter_id = db.resolve_fighter_id(connection, SOURCE, source_id)
                    resolved_fighter_ids[source_id] = fighter_id
                    db.upsert_fighter(
                        connection, fighter_id, fighter["name"],
                        SOURCE, source_id,
                    )
                bout_id = f"sportradar:{bout['source_bout_id']}"
                winner_id = (
                    resolved_fighter_ids[bout["winner_source_id"]]
                    if bout["outcome"] == "win" and bout["winner_source_id"] else None
                )
                card_snapshot["bouts"].append({
                    "bout_id": bout_id,
                    "fighter_a_id": resolved_fighter_ids[fighters[0]["source_fighter_id"]],
                    "fighter_a_name": fighters[0]["name"],
                    "fighter_b_id": resolved_fighter_ids[fighters[1]["source_fighter_id"]],
                    "fighter_b_name": fighters[1]["name"],
                    "bout_status": bout["status"],
                    "bout_provider_status": bout["source_status"],
                    "weight_class": bout["weight_class"],
                    "outcome": bout["outcome"],
                    "winner_fighter_id": winner_id,
                    "method": bout["method"],
                })
                existing = connection.execute(
                    "SELECT status FROM bouts WHERE bout_id = ?", (bout_id,)
                ).fetchone()
                if existing and existing["status"] == "completed" and bout["status"] != "completed":
                    # An older API snapshot must not erase a settled result.
                    continue
                db.upsert_bout(
                    connection, bout_id, event_id,
                    resolved_fighter_ids[fighters[0]["source_fighter_id"]],
                    resolved_fighter_ids[fighters[1]["source_fighter_id"]],
                    bout["status"],
                    weight_class=bout["weight_class"],
                    scheduled_rounds=bout["scheduled_rounds"],
                    source=SOURCE, source_bout_id=bout["source_bout_id"],
                    provider_status=bout["source_status"],
                )
                imported_bouts += 1
                if bout["status"] == "completed" and bout["outcome"]:
                    db.upsert_result(
                        connection, bout_id, bout["outcome"], winner_id,
                        fetched_at, bout["method"],
                    )
                    imported_results += 1
            _refresh_event_status(connection, event_id)
            current_event = connection.execute(
                "SELECT status, provider_status FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            card_snapshot["event_status"] = current_event["status"]
            card_snapshot["event_provider_status"] = current_event["provider_status"]
            card_snapshots.append(card_snapshot)
        receipt = connection.execute(
            "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) VALUES (?, ?, ?, ?)",
            (SOURCE, fetched_at, str(path), digest),
        )
        record_card_snapshots(connection, int(receipt.lastrowid), card_snapshots)
    return {
        "events": len(events),
        "bouts": imported_bouts,
        "results": imported_results,
        "non_scoreable_bouts": non_scoreable_bouts,
        "skipped_incomplete": skipped_incomplete,
        "raw_path": str(path),
    }


def _refresh_event_status(connection: sqlite3.Connection, event_id: str) -> None:
    rows = connection.execute(
        "SELECT status, provider_status FROM bouts WHERE event_id = ?", (event_id,)
    ).fetchall()
    if not rows:
        return
    statuses = {row["status"] for row in rows}
    if "scheduled" in statuses:
        event_status = "scheduled"
        provider_statuses = {row["provider_status"] for row in rows if row["status"] == "scheduled"}
        blocking = sorted(provider_statuses & (_NON_SCOREABLE_UNREPRESENTED | _FINISHED_STATUSES))
        provider_status = blocking[0] if blocking else "scheduled"
    elif statuses == {"cancelled"}:
        event_status = "cancelled"
        provider_status = "cancelled"
    else:
        event_status = "completed"
        provider_status = "completed"
    connection.execute(
        "UPDATE events SET status = ?, provider_status = ? WHERE event_id = ?",
        (event_status, provider_status, event_id)
    )


def _source_id(value: object, kind: str) -> str:
    prefix = f"sr:{kind}:"
    if not isinstance(value, str) or not value.startswith(prefix) or not value[len(prefix):].isdigit():
        raise ValueError(f"Invalid Sportradar {kind} ID: {value!r}")
    return value


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _display_name(value: object) -> str | None:
    name = _text(value)
    if name and "," in name:
        surname, given = name.split(",", 1)
        if surname.strip() and given.strip():
            return f"{given.strip()} {surname.strip()}"
    return name


def _day(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("day must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("day must be YYYY-MM-DD") from exc


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid Sportradar timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Sportradar timestamp lacks a UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _http_detail(exc: HTTPError, api_key: str) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(message, str) and message.strip():
            return f": {message.strip().replace(api_key, '[redacted]')[:200]}"
    except (OSError, UnicodeError, ValueError):
        pass
    return ""
