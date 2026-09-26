"""Verified same-revision identity recounts shared by dashboard and research review."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from .wikipedia_history import parse_event_page_title


def _checked_json(
    path_text: object, stated_hash: object,
    checked: dict[tuple[str, str], bool],
    receipt_matches: Callable[[object, object], bool],
) -> dict | None:
    if not isinstance(path_text, str) or not isinstance(stated_hash, str):
        return None
    key = path_text, stated_hash
    if key not in checked:
        checked[key] = receipt_matches(*key)
    if not checked[key]:
        return None
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != stated_hash:
            return None
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _source_bouts(
    connection: sqlite3.Connection, receipt: sqlite3.Row, event_id: str,
    checked: dict[tuple[str, str], bool],
    receipt_matches: Callable[[object, object], bool],
) -> dict[int, object] | None:
    payload = _checked_json(receipt["payload_path"], receipt["sha256"], checked, receipt_matches)
    if (payload is None or payload.get("id") != receipt["event_page_id"]
            or not isinstance(payload.get("latest"), dict)
            or payload["latest"].get("id") != receipt["revision_id"]
            or not isinstance(payload.get("title"), str)):
        return None
    try:
        fragment = urlsplit(str(receipt["page_url"])).fragment
        if fragment:
            from .wikipedia_embedded import parse_embedded_event

            event = connection.execute(
                "SELECT event_date FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if event is None:
                return None
            parsed = parse_embedded_event(
                payload, source_page_title=payload["title"],
                target_heading=unquote(fragment).replace("_", " "),
                expected_date=str(event["event_date"]),
                expected_page_id=int(receipt["event_page_id"]),
            )
            if parsed.event_id != event_id:
                return None
            bouts = parsed.bouts
        else:
            parsed = parse_event_page_title(payload, payload["title"])
            if event_id != f"wikipedia_research:{parsed.page_id}":
                return None
            bouts = parsed.bouts
    except (TypeError, ValueError, KeyError):
        return None
    return {bout.position: bout for bout in bouts}


def _review_decisions(
    connection: sqlite3.Connection, run_id: int, event_id: str,
    receipt: sqlite3.Row, bouts: dict[int, object],
    checked: dict[tuple[str, str], bool],
    receipt_matches: Callable[[object, object], bool],
) -> dict[tuple[int, str], dict] | None:
    row = connection.execute(
        "SELECT source, payload_path, sha256 FROM ingestion_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None or row["source"] != "wikipedia_identity_crosswalk":
        return None
    review = _checked_json(row["payload_path"], row["sha256"], checked, receipt_matches)
    if (review is None or review.get("schema_version") != 2
            or not isinstance(review.get("decisions"), list)):
        return None
    decisions: dict[tuple[int, str], dict] = {}
    for item in review["decisions"]:
        if not isinstance(item, dict):
            return None
        if item.get("event_id") != event_id:
            continue
        position = item.get("bout_position")
        name = item.get("fighter_name")
        fighter_id = item.get("fighter_id")
        evidence_urls = item.get("evidence_urls")
        if (type(position) is not int or position <= 0
                or not isinstance(name, str) or not name
                or not isinstance(fighter_id, str) or not fighter_id
                or item.get("source_revision_id") != receipt["revision_id"]
                or item.get("source_receipt_sha256") != receipt["sha256"]
                or not isinstance(item.get("reviewed_by"), str)
                or not item["reviewed_by"]
                or not isinstance(evidence_urls, list) or not evidence_urls
                or any(not isinstance(url, str) or not url.startswith("https://")
                       for url in evidence_urls)):
            return None
        bout = bouts.get(position)
        if (bout is None or not any(
            fighter.name == name and fighter.title is None
            for fighter in (bout.fighter_a, bout.fighter_b)
        )):
            return None
        key = position, name
        if key in decisions:
            return None
        decisions[key] = item
    return decisions


def _accepted_exact_bout(
    connection: sqlite3.Connection, event_id: str, bout: object,
    decision: dict,
) -> bool:
    name = decision["fighter_name"]
    fighter_id = decision["fighter_id"]
    side = "a" if bout.fighter_a.name == name and bout.fighter_a.title is None else "b"
    opponent = bout.fighter_b.name if side == "a" else bout.fighter_a.name
    event = connection.execute(
        "SELECT source_event_id FROM events WHERE event_id = ? AND source = 'wikipedia_research'",
        (event_id,),
    ).fetchone()
    if event is None:
        return False
    matches = 0
    for row in connection.execute(
        """SELECT b.fighter_a_id, b.fighter_b_id, b.weight_class, b.source_bout_id,
                  fa.canonical_name AS fighter_a_name, fb.canonical_name AS fighter_b_name,
                  r.outcome, r.method, r.winner_fighter_id
           FROM bouts b JOIN results r ON r.bout_id = b.bout_id
           JOIN fighters fa ON fa.fighter_id = b.fighter_a_id
           JOIN fighters fb ON fb.fighter_id = b.fighter_b_id
           WHERE b.event_id = ? AND b.source = 'wikipedia_research'
             AND b.status = 'completed'
             AND (b.fighter_a_id = ? OR b.fighter_b_id = ?)""",
        (event_id, fighter_id, fighter_id),
    ):
        if row[f"fighter_{side}_id"] != fighter_id:
            continue
        other_side = "b" if side == "a" else "a"
        expected_winner = row["fighter_a_id"] if bout.winner_side == "a" else (
            row["fighter_b_id"] if bout.winner_side == "b" else None
        )
        source_bout_id = f"{event['source_event_id']}:" + ":".join(sorted((
            str(row["fighter_a_id"]), str(row["fighter_b_id"]),
        )))
        if (row[f"fighter_{other_side}_name"] == opponent
                and row["source_bout_id"] == source_bout_id
                and row["weight_class"] == bout.weight_class
                and row["outcome"] == bout.outcome
                and row["method"] == bout.method
                and row["winner_fighter_id"] == expected_winner):
            matches += 1
    return matches == 1


def reviewed_same_revision_recount(
    connection: sqlite3.Connection,
    receipts: list[sqlite3.Row],
    event_id: str,
    checked_hashes: dict[tuple[str, str], bool],
    receipt_matches: Callable[[object, object], bool],
) -> set[int] | None:
    """Return newly accepted source positions, or ``None`` for invalid evidence.

    Each changed count must come from newly approved plain-name decisions on
    positions that were held before the replay and now match exact stored bouts.
    """
    if not receipts:
        return None
    source_sha = str(receipts[0]["sha256"])
    if any(str(row["sha256"]) != source_sha for row in receipts):
        return None
    needs_review = any(
        (a["imported_bouts"], a["skipped_unresolved_bouts"])
        != (b["imported_bouts"], b["skipped_unresolved_bouts"])
        for a, b in zip(receipts, receipts[1:])
    ) or any(row["crosswalk_run_id"] is not None for row in receipts)
    if not needs_review:
        return set()
    bouts = _source_bouts(connection, receipts[-1], event_id, checked_hashes, receipt_matches)
    if bouts is None:
        return None
    total = len(bouts)
    if any(int(row["imported_bouts"]) + int(row["skipped_unresolved_bouts"]) != total
           for row in receipts):
        return None
    recovered: set[int] = set()
    previous_decisions: dict[tuple[int, str], dict] = {}
    for index, current in enumerate(receipts):
        crosswalk_run_id = current["crosswalk_run_id"]
        if crosswalk_run_id is None:
            current_decisions: dict[tuple[int, str], dict] = {}
            # An older global-name crosswalk cannot prove prior held positions.
            for old_review in connection.execute(
                """SELECT payload_path, sha256 FROM ingestion_runs
                   WHERE source = 'wikipedia_identity_crosswalk' AND run_id < ?""",
                (current["run_id"],),
            ):
                saved = _checked_json(old_review["payload_path"], old_review["sha256"],
                                      checked_hashes, receipt_matches)
                if saved is None or saved.get("schema_version") != 2:
                    return None
        else:
            if type(crosswalk_run_id) is not int or crosswalk_run_id >= int(current["run_id"]):
                return None
            current_decisions = _review_decisions(
                connection, crosswalk_run_id, event_id, current, bouts,
                checked_hashes, receipt_matches,
            )
            if current_decisions is None:
                return None
        if index == 0:
            previous_decisions = current_decisions
            continue
        if any(key not in current_decisions
               or current_decisions[key]["fighter_id"] != item["fighter_id"]
               for key, item in previous_decisions.items()):
            return None
        prior = receipts[index - 1]
        old_imported = int(prior["imported_bouts"])
        old_held = int(prior["skipped_unresolved_bouts"])
        new_imported = int(current["imported_bouts"])
        new_held = int(current["skipped_unresolved_bouts"])
        if (old_imported, old_held) == (new_imported, new_held):
            previous_decisions = current_decisions
            continue
        delta = new_imported - old_imported
        if (delta <= 0 or new_held != old_held - delta
                or crosswalk_run_id is None
                or not int(prior["run_id"]) < crosswalk_run_id < int(current["run_id"])):
            return None
        new_keys = set(current_decisions) - set(previous_decisions)
        positions = {position for position, _ in new_keys}
        if len(positions) != delta or positions & recovered:
            return None
        for position in positions:
            bout = bouts[position]
            pair = frozenset((bout.fighter_a.name, bout.fighter_b.name))
            if sum(frozenset((candidate.fighter_a.name, candidate.fighter_b.name)) == pair
                   for candidate in bouts.values()) != 1:
                return None
            unlinked = {
                fighter.name for fighter in (bout.fighter_a, bout.fighter_b)
                if fighter.title is None
            }
            old_names = {name for pos, name in previous_decisions if pos == position}
            new_names = {name for pos, name in current_decisions if pos == position}
            if not unlinked - old_names or not unlinked <= new_names:
                return None
            if not all(_accepted_exact_bout(connection, event_id, bout, current_decisions[key])
                       for key in current_decisions if key[0] == position):
                return None
        recovered.update(positions)
        previous_decisions = current_decisions
    return recovered
