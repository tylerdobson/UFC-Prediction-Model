"""Verify a saved quote against the exact bookmaker API response that contained it."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .ingest import _name_key
from .odds_api import normalize_h2h
from .pipeline import parse_utc


_ODDS_SOURCES = {"the-odds-api", "the-odds-api-historical"}
_Signature = tuple[str, frozenset[str], str, str, float, str, str]
_Cache = dict[int, tuple[datetime, datetime, str, dict[_Signature, set[date]]] | None]


def _receipt_payload(path_text: str, digest: str) -> object | None:
    path = Path(path_text).expanduser()
    if path.is_symlink():
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read()
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(payload).hexdigest() != digest:
        return None
    try:
        return json.loads(payload)
    except (UnicodeError, ValueError):
        return None


def _receipt_market(
    row: sqlite3.Row, cache: _Cache,
) -> tuple[datetime, datetime, str, dict[_Signature, set[date]]] | None:
    run_id = int(row["ingestion_run_id"])
    if run_id in cache:
        return cache[run_id]
    try:
        source = str(row["receipt_source"])
        if source not in _ODDS_SOURCES:
            raise ValueError("wrong odds source")
        snapshot = parse_utc(str(row["snapshot_at_utc"]))
        fetched = parse_utc(str(row["fetched_at_utc"]))
        if fetched < snapshot:
            raise ValueError("receipt fetched before its snapshot")
        payload = _receipt_payload(str(row["payload_path"]), str(row["sha256"]))
        if payload is None:
            raise ValueError("unreadable or changed source payload")
        if source == "the-odds-api":
            if not isinstance(payload, list):
                raise ValueError("invalid live odds payload")
            events = payload
        else:
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("invalid historical odds payload")
            if parse_utc(str(payload.get("timestamp"))) != snapshot:
                raise ValueError("historical provider timestamp changed")
            events = payload["data"]
        signatures: dict[_Signature, set[date]] = {}
        for item in normalize_h2h(events, snapshot):
            signature = (
                str(item["source_event_id"]),
                frozenset((_name_key(item["fighter_a"]), _name_key(item["fighter_b"]))),
                str(item["bookmaker_key"]), _name_key(item["selection_name"]),
                float(item["decimal_odds"]), str(item["bookmaker_updated_at"]),
                str(item["captured_at_utc"]),
            )
            signatures.setdefault(signature, set()).add(
                parse_utc(str(item["commence_time_utc"])).date()
            )
        cache[run_id] = (snapshot, fetched, source, signatures)
    except (TypeError, ValueError, KeyError):
        cache[run_id] = None
    return cache[run_id]


def quote_receipt_matches(
    row: sqlite3.Row, cutoff_at_utc: datetime,
    cache: _Cache, *, live_only: bool = False,
) -> bool:
    """Require a hash-checked response containing this exact market selection.

    A historical provider snapshot can be fetched later than its market time;
    a live quote must have been fetched before the decision cutoff.
    """
    receipt = _receipt_market(row, cache)
    if receipt is None:
        return False
    snapshot, fetched, source, signatures = receipt
    if snapshot > cutoff_at_utc or (live_only and source != "the-odds-api"):
        return False
    if source == "the-odds-api" and fetched > cutoff_at_utc:
        return False
    try:
        captured = parse_utc(str(row["captured_at_utc"]))
        event_date = date.fromisoformat(str(row["event_date"]))
        if captured != snapshot or not row["bookmaker_updated_at_utc"]:
            return False
        signature = (
            str(row["source_event_id"]),
            frozenset((_name_key(row["fighter_a_name"]), _name_key(row["fighter_b_name"]))),
            str(row["bookmaker"]), _name_key(row["selection_name"]),
            float(row["decimal_odds"]), str(row["bookmaker_updated_at_utc"]),
            str(row["captured_at_utc"]),
        )
    except (TypeError, ValueError, KeyError):
        return False
    dates = signatures.get(signature, set())
    return any(abs((event_date - source_date).days) <= 1 for source_date in dates)


def linked_quote_rows(
    connection: sqlite3.Connection, bout_id: str,
    ingestion_run_id: int | None = None,
) -> list[sqlite3.Row]:
    """Return only quote/receipt pairs with enough fields to verify the source."""
    sql = """
        SELECT q.quote_id, q.bout_id, q.bookmaker, q.selection_fighter_id,
               q.decimal_odds, q.captured_at_utc, q.bookmaker_updated_at_utc,
               qr.ingestion_run_id, qr.source_event_id,
               r.source AS receipt_source, r.fetched_at_utc, r.snapshot_at_utc,
               r.payload_path, r.sha256, e.event_date,
               fa.canonical_name AS fighter_a_name,
               fb.canonical_name AS fighter_b_name,
               fs.canonical_name AS selection_name
        FROM odds_quotes AS q
        JOIN odds_quote_receipts AS qr ON qr.quote_id = q.quote_id
        JOIN ingestion_runs AS r ON r.run_id = qr.ingestion_run_id
        JOIN bouts AS b ON b.bout_id = q.bout_id
        JOIN events AS e ON e.event_id = b.event_id
        JOIN fighters AS fa ON fa.fighter_id = b.fighter_a_id
        JOIN fighters AS fb ON fb.fighter_id = b.fighter_b_id
        JOIN fighters AS fs ON fs.fighter_id = q.selection_fighter_id
        WHERE q.bout_id = ?
    """
    params: tuple[Any, ...] = (bout_id,)
    if ingestion_run_id is not None:
        sql += " AND qr.ingestion_run_id = ?"
        params += (ingestion_run_id,)
    return connection.execute(sql, params).fetchall()
