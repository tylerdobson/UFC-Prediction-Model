"""Small fictional dataset for exercising the entire local pipeline."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import sqlite3

from . import db
from .pipeline import utc_now, utc_string


def seed_demo(connection: sqlite3.Connection, now: datetime | None = None) -> str:
    now = (now or utc_now()).astimezone(timezone.utc)
    fighters = {
        "demo-a": "Demo Fighter A",
        "demo-b": "Demo Fighter B",
        "demo-c": "Demo Fighter C",
        "demo-d": "Demo Fighter D",
    }
    for fighter_id, name in fighters.items():
        db.upsert_fighter(connection, fighter_id, name, source="demo", source_fighter_id=fighter_id)

    history = [
        ("demo-past-1", 42, "demo-a", "demo-b", "demo-a"),
        ("demo-past-2", 35, "demo-a", "demo-c", "demo-a"),
        ("demo-past-3", 28, "demo-d", "demo-b", "demo-d"),
        ("demo-past-4", 21, "demo-a", "demo-d", "demo-a"),
        ("demo-past-5", 14, "demo-c", "demo-b", "demo-c"),
        ("demo-past-6", 7, "demo-d", "demo-c", "demo-d"),
    ]
    for event_id, days_ago, fighter_a, fighter_b, winner in history:
        event_day = now.date() - timedelta(days=days_ago)
        start_at = datetime.combine(event_day, time(20), tzinfo=timezone.utc)
        db.upsert_event(
            connection, event_id, f"Fictional {event_id}", event_day.isoformat(),
            "completed", source="demo", source_event_id=event_id,
            start_time_utc=utc_string(start_at),
        )
        bout_id = f"{event_id}-bout"
        db.upsert_bout(connection, bout_id, event_id, fighter_a, fighter_b, "completed", source="demo", source_bout_id=bout_id)
        db.upsert_result(connection, bout_id, "win", winner, utc_string(start_at + timedelta(hours=4)))

    upcoming_id = "demo-upcoming"
    event_day = now.date() + timedelta(days=7)
    start_at = datetime.combine(event_day, time(20), tzinfo=timezone.utc)
    db.upsert_event(
        connection, upcoming_id, "Fictional upcoming UFC-style card", event_day.isoformat(),
        "scheduled", source="demo", source_event_id=upcoming_id,
        start_time_utc=utc_string(start_at),
    )
    upcoming_bouts = [
        ("demo-upcoming-1", "demo-a", "demo-c", 1.80, 2.15),
        ("demo-upcoming-2", "demo-b", "demo-d", 2.80, 1.48),
    ]
    for bout_id, fighter_a, fighter_b, odds_a, odds_b in upcoming_bouts:
        db.upsert_bout(connection, bout_id, upcoming_id, fighter_a, fighter_b, "scheduled", source="demo", source_bout_id=bout_id)
        db.add_quote(connection, bout_id, "demo-book", fighter_a, odds_a, utc_string(now), "demo")
        db.add_quote(connection, bout_id, "demo-book", fighter_b, odds_b, utc_string(now), "demo")
    connection.commit()
    return upcoming_id
