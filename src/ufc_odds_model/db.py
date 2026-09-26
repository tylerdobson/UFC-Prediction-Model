"""SQLite access and schema setup."""

from __future__ import annotations

import sqlite3
import math
from importlib.resources import files
from pathlib import Path


DEFAULT_DB = Path("data/ufc.sqlite")


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    applied = {
        row["version"] for row in connection.execute("SELECT version FROM schema_migrations")
    }
    migration_dir = files("ufc_odds_model").joinpath("sql/migrations")
    for migration in sorted(migration_dir.iterdir(), key=lambda item: item.name):
        if not migration.name.endswith(".sql") or migration.name in applied:
            continue
        version = migration.name.replace("'", "''")
        script = migration.read_text(encoding="utf-8")
        # A migration and its receipt must commit together. Otherwise a crash
        # after ALTER TABLE but before the receipt makes retry fail.
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + script + "\n"
                "INSERT INTO schema_migrations(version, applied_at_utc) "
                f"VALUES ('{version}', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));\n"
                "COMMIT;"
            )
        except sqlite3.Error:
            connection.rollback()
            raise
    connection.commit()


def upsert_fighter(
    connection: sqlite3.Connection,
    fighter_id: str,
    name: str,
    source: str | None = None,
    source_fighter_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO fighters(fighter_id, canonical_name, source, source_fighter_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(fighter_id) DO UPDATE SET
            canonical_name = CASE
                WHEN fighters.source IS NULL OR fighters.source = excluded.source
                THEN excluded.canonical_name ELSE fighters.canonical_name END,
            source = COALESCE(fighters.source, excluded.source),
            source_fighter_id = COALESCE(fighters.source_fighter_id, excluded.source_fighter_id)
        """,
        (fighter_id, name, source, source_fighter_id),
    )


def resolve_fighter_id(
    connection: sqlite3.Connection, source: str, source_fighter_id: str
) -> str:
    """Resolve a reviewed alias, or use the source's own stable identity."""
    if not source or not source_fighter_id:
        raise ValueError("Source and source fighter ID are required")
    alias = connection.execute(
        "SELECT fighter_id FROM fighter_external_ids WHERE source = ? AND source_fighter_id = ?",
        (source, source_fighter_id),
    ).fetchone()
    if alias:
        return str(alias["fighter_id"])
    original = connection.execute(
        "SELECT fighter_id FROM fighters WHERE source = ? AND source_fighter_id = ?",
        (source, source_fighter_id),
    ).fetchone()
    return str(original["fighter_id"]) if original else f"{source}:{source_fighter_id}"


def link_external_fighter(
    connection: sqlite3.Connection, source: str, source_fighter_id: str, fighter_id: str
) -> None:
    """Attach a verified provider identity before importing it under another ID."""
    if not source or not source_fighter_id or not fighter_id:
        raise ValueError("Source, source fighter ID, and canonical fighter ID are required")
    target = connection.execute(
        "SELECT fighter_id FROM fighters WHERE fighter_id = ?", (fighter_id,)
    ).fetchone()
    if target is None:
        raise ValueError(f"Unknown canonical fighter: {fighter_id}")
    original = connection.execute(
        "SELECT fighter_id FROM fighters WHERE source = ? AND source_fighter_id = ?",
        (source, source_fighter_id),
    ).fetchone()
    alias = connection.execute(
        "SELECT fighter_id FROM fighter_external_ids WHERE source = ? AND source_fighter_id = ?",
        (source, source_fighter_id),
    ).fetchone()
    for existing in (original, alias):
        if existing and existing["fighter_id"] != fighter_id:
            raise ValueError("Provider ID already belongs to another fighter; review duplicates first")
    connection.execute(
        "INSERT OR IGNORE INTO fighter_external_ids(source, source_fighter_id, fighter_id) VALUES (?, ?, ?)",
        (source, source_fighter_id, fighter_id),
    )


def upsert_event(
    connection: sqlite3.Connection,
    event_id: str,
    name: str,
    event_date: str,
    status: str,
    source: str | None = None,
    source_event_id: str | None = None,
    start_time_utc: str | None = None,
    provider_status: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO events(event_id, source, source_event_id, name, event_date, start_time_utc, status, provider_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            name = excluded.name,
            event_date = excluded.event_date,
            start_time_utc = COALESCE(excluded.start_time_utc, events.start_time_utc),
            status = excluded.status,
            provider_status = COALESCE(excluded.provider_status, events.provider_status)
        """,
        (event_id, source, source_event_id, name, event_date, start_time_utc, status, provider_status),
    )


def upsert_bout(
    connection: sqlite3.Connection,
    bout_id: str,
    event_id: str,
    fighter_a_id: str,
    fighter_b_id: str,
    status: str,
    weight_class: str | None = None,
    scheduled_rounds: int | None = None,
    source: str | None = None,
    source_bout_id: str | None = None,
    provider_status: str | None = None,
) -> None:
    existing = connection.execute(
        "SELECT event_id, fighter_a_id, fighter_b_id FROM bouts WHERE bout_id = ?",
        (bout_id,),
    ).fetchone()
    if existing and (
        existing["event_id"] != event_id
        or existing["fighter_a_id"] != fighter_a_id
        or existing["fighter_b_id"] != fighter_b_id
    ):
        raise ValueError("A changed matchup needs a new bout_id; cancel the old bout")
    connection.execute(
        """
        INSERT INTO bouts(
            bout_id, event_id, source, source_bout_id, fighter_a_id, fighter_b_id,
            weight_class, scheduled_rounds, status, provider_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bout_id) DO UPDATE SET
            weight_class = COALESCE(excluded.weight_class, bouts.weight_class),
            scheduled_rounds = COALESCE(excluded.scheduled_rounds, bouts.scheduled_rounds),
            status = excluded.status,
            provider_status = COALESCE(excluded.provider_status, bouts.provider_status)
        """,
        (
            bout_id, event_id, source, source_bout_id, fighter_a_id, fighter_b_id,
            weight_class, scheduled_rounds, status, provider_status,
        ),
    )


def upsert_result(
    connection: sqlite3.Connection,
    bout_id: str,
    outcome: str,
    winner_fighter_id: str | None,
    recorded_at_utc: str,
    method: str | None = None,
) -> None:
    bout = connection.execute(
        "SELECT fighter_a_id, fighter_b_id FROM bouts WHERE bout_id = ?", (bout_id,)
    ).fetchone()
    if bout is None:
        raise ValueError(f"Unknown bout: {bout_id}")
    if outcome == "win" and winner_fighter_id not in {bout["fighter_a_id"], bout["fighter_b_id"]}:
        raise ValueError("Winner must be one of the bout's fighters")
    if outcome != "win" and winner_fighter_id is not None:
        raise ValueError("Draws and no contests cannot have a winner")
    connection.execute(
        """
        INSERT INTO results(bout_id, outcome, winner_fighter_id, method, recorded_at_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bout_id) DO UPDATE SET
            outcome = excluded.outcome,
            winner_fighter_id = excluded.winner_fighter_id,
            method = excluded.method,
            recorded_at_utc = excluded.recorded_at_utc
        """,
        (bout_id, outcome, winner_fighter_id, method, recorded_at_utc),
    )
    connection.execute("UPDATE bouts SET status = 'completed' WHERE bout_id = ?", (bout_id,))


def add_quote(
    connection: sqlite3.Connection,
    bout_id: str,
    bookmaker: str,
    selection_fighter_id: str,
    decimal_odds: float,
    captured_at_utc: str,
    source: str,
    bookmaker_updated_at_utc: str | None = None,
) -> None:
    if not math.isfinite(decimal_odds) or decimal_odds <= 1:
        raise ValueError("Decimal odds must be finite and greater than 1")
    bout = connection.execute(
        "SELECT fighter_a_id, fighter_b_id FROM bouts WHERE bout_id = ?", (bout_id,)
    ).fetchone()
    if bout is None or selection_fighter_id not in {bout["fighter_a_id"], bout["fighter_b_id"]}:
        raise ValueError("Quote selection must be one of the bout's fighters")
    connection.execute(
        """
        INSERT OR IGNORE INTO odds_quotes(
            bout_id, bookmaker, market, selection_fighter_id, decimal_odds,
            bookmaker_updated_at_utc, captured_at_utc, source
        ) VALUES (?, ?, 'h2h', ?, ?, ?, ?, ?)
        """,
        (
            bout_id, bookmaker, selection_fighter_id, decimal_odds,
            bookmaker_updated_at_utc, captured_at_utc, source,
        ),
    )


def event_bouts(connection: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT b.*, e.name AS event_name, e.event_date, e.start_time_utc,
               fa.canonical_name AS fighter_a_name, fb.canonical_name AS fighter_b_name
        FROM bouts b
        JOIN events e ON e.event_id = b.event_id
        JOIN fighters fa ON fa.fighter_id = b.fighter_a_id
        JOIN fighters fb ON fb.fighter_id = b.fighter_b_id
        WHERE b.event_id = ? AND b.status = 'scheduled'
          AND (b.provider_status IS NULL OR b.provider_status IN ('scheduled', 'not_started'))
        ORDER BY b.bout_id
        """,
        (event_id,),
    ).fetchall()


def prior_results(connection: sqlite3.Connection, event_date: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT b.bout_id, b.fighter_a_id, b.fighter_b_id,
               r.outcome, r.winner_fighter_id, e.event_id, e.event_date
        FROM bouts b
        JOIN events e ON e.event_id = b.event_id
        JOIN results r ON r.bout_id = b.bout_id
        WHERE e.event_date < ?
        ORDER BY e.event_date, e.event_id, b.bout_id
        """,
        (event_date,),
    ).fetchall()


def save_prediction(
    connection: sqlite3.Connection,
    bout_id: str,
    model_version: str,
    feature_cutoff_at_utc: str,
    generated_at_utc: str,
    p_fighter_a: float,
) -> int:
    connection.execute(
        """
        INSERT INTO predictions(
            bout_id, model_version, feature_cutoff_at_utc, generated_at_utc, p_fighter_a
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bout_id, model_version, feature_cutoff_at_utc) DO UPDATE SET
            generated_at_utc = excluded.generated_at_utc,
            p_fighter_a = excluded.p_fighter_a
        """,
        (bout_id, model_version, feature_cutoff_at_utc, generated_at_utc, p_fighter_a),
    )
    row = connection.execute(
        """
        SELECT prediction_id FROM predictions
        WHERE bout_id = ? AND model_version = ? AND feature_cutoff_at_utc = ?
        """,
        (bout_id, model_version, feature_cutoff_at_utc),
    ).fetchone()
    return int(row["prediction_id"])


def quotes_as_of(
    connection: sqlite3.Connection, bout_id: str, cutoff_at_utc: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT q.*, f.canonical_name AS selection_name
        FROM odds_quotes q
        JOIN fighters f ON f.fighter_id = q.selection_fighter_id
        WHERE q.bout_id = ? AND q.captured_at_utc <= ?
        ORDER BY q.captured_at_utc DESC, q.quote_id DESC
        """,
        (bout_id, cutoff_at_utc),
    ).fetchall()
