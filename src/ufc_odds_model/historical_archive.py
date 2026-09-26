"""Read-only comparison of a proved old event revision with research results.

This is an intake review, not a model or alert credential. A MediaWiki
revision proves what the publisher's archived page said at a chosen cutoff;
the separately verified ``RevisionProof`` supplies that evidence. The current
research database is used only to identify exact matching fighters and bouts.
Unlinked or renamed fighters remain on hold rather than being fuzzy-matched.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Literal

from .archive_identity import resolve_archived_identity_titles
from .publisher_revisions import RevisionProof
from .wikipedia_history import (
    FighterRef,
    _BOUT_OPEN,
    _event_date,
    _event_infobox_fields,
    _fighter_ref,
    _split_template,
    _template_text,
    parse_event_page_title,
)


RevisionKind = Literal["scheduled", "completed"]
_FIGHT_CARD = re.compile(r"^==\s*Fight card\s*==\s*$", re.I | re.M)
_NEXT_SECTION = re.compile(r"^==[^=].*?==\s*$", re.M)


@dataclass(frozen=True)
class ArchivedBout:
    position: int
    fighter_a: FighterRef
    fighter_b: FighterRef
    weight_class: str
    outcome: str | None
    winner_name: str | None


@dataclass(frozen=True)
class ArchivedCard:
    page_id: int
    page_title: str
    event_name: str
    event_date: str
    kind: RevisionKind
    revision_id: int
    revision_timestamp_utc: str
    cutoff_at_utc: str
    bouts: tuple[ArchivedBout, ...]


def _scheduled_bouts(wikitext: str) -> tuple[ArchivedBout, ...]:
    heading = _FIGHT_CARD.search(wikitext)
    following = _NEXT_SECTION.search(wikitext, heading.end()) if heading else None
    if heading is None or following is None or heading.end() >= following.start():
        raise ValueError("Archived revision lacks a bounded Fight card section")
    section = wikitext[heading.end():following.start()]
    rows: list[ArchivedBout] = []
    for position, match in enumerate(_BOUT_OPEN.finditer(section), 1):
        parts = _split_template(_template_text(section, match.start()))
        if parts[0].casefold() != "mmaevent bout" or len(parts) < 9:
            raise ValueError(f"Scheduled bout {position} has unexpected markup")
        first, second = _fighter_ref(parts[2]), _fighter_ref(parts[4])
        if not parts[1].strip() or parts[3].strip().casefold() not in {"vs.", "vs", "v.", "v"}:
            raise ValueError(f"Scheduled bout {position} has no clear pairing")
        if any(part.strip() for part in parts[5:8]):
            raise ValueError(f"Scheduled bout {position} already records a result")
        rows.append(ArchivedBout(position, first, second, parts[1].strip(), None, None))
    if not rows:
        raise ValueError("Archived revision has no scheduled Fight card bouts")
    return tuple(rows)


def parse_archived_card(proof: RevisionProof, kind: RevisionKind) -> ArchivedCard:
    """Parse only the exact verified revision, with strict result markers."""
    if kind not in ("scheduled", "completed"):
        raise ValueError("kind must be scheduled or completed")
    fields = _event_infobox_fields(proof.wikitext)
    if not fields.get("name") or not fields.get("date"):
        raise ValueError("Archived event revision has no name or event date")
    event_date = _event_date(fields["date"])
    if kind == "scheduled":
        bouts = _scheduled_bouts(proof.wikitext)
    else:
        parsed = parse_event_page_title({
            "title": proof.page_title,
            "id": proof.page_id,
            "latest": {"id": proof.revision_id, "timestamp": proof.revision_timestamp_utc},
            "license": {"title": proof.license_name, "url": proof.license_url},
            "source": proof.wikitext,
        }, proof.page_title)
        if parsed.event_date != event_date:
            raise ValueError("Archived result date disagrees with event infobox")
        bouts = tuple(ArchivedBout(
            bout.position, bout.fighter_a, bout.fighter_b, bout.weight_class,
            bout.outcome, bout.fighter_a.name if bout.winner_side == "a" else None,
        ) for bout in parsed.bouts)
    return ArchivedCard(
        proof.page_id, proof.page_title, fields["name"], event_date,
        kind, proof.revision_id, proof.revision_timestamp_utc,
        proof.cutoff_at_utc, bouts,
    )


def _name_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def compare_archived_card(connection: sqlite3.Connection, card: ArchivedCard) -> dict:
    """Match only unique, linked source pairs to current accepted research rows.

    Matching is diagnostic. A match does not itself prove an official UTC
    event start, a point-in-time fighter crosswalk, or historical prices.
    """
    event_id = f"wikipedia_research:{card.page_id}"
    event = connection.execute(
        "SELECT event_id, event_date, status, source_event_id FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if event is None or event["event_date"] != card.event_date or (
        event["source_event_id"] and str(event["source_event_id"]) != str(card.page_id)
    ):
        raise ValueError("Archived event ID/date differs from research database")
    current = connection.execute(
        """SELECT b.bout_id, b.fighter_a_id, b.fighter_b_id,
                  fa.canonical_name AS fighter_a_name,
                  fb.canonical_name AS fighter_b_name,
                  b.status, r.outcome, r.winner_fighter_id
           FROM bouts b
           JOIN fighters fa ON fa.fighter_id = b.fighter_a_id
           JOIN fighters fb ON fb.fighter_id = b.fighter_b_id
           LEFT JOIN results r ON r.bout_id = b.bout_id
           WHERE b.event_id = ? ORDER BY b.bout_id""",
        (event_id,),
    ).fetchall()
    by_pair: dict[frozenset[str], list[sqlite3.Row]] = {}
    for row in current:
        pair = frozenset((_name_key(row["fighter_a_name"]), _name_key(row["fighter_b_name"])))
        by_pair.setdefault(pair, []).append(row)
    matched: list[dict] = []
    held: list[dict] = []
    seen_bouts: set[str] = set()
    matched_source: dict[str, ArchivedBout] = {}
    matched_current: dict[str, sqlite3.Row] = {}
    for source in card.bouts:
        reason: str | None = None
        if not source.fighter_a.title or not source.fighter_b.title:
            reason = "unlinked_fighter_identity"
        pair = frozenset((_name_key(source.fighter_a.name), _name_key(source.fighter_b.name)))
        candidates = by_pair.get(pair, [])
        if reason is None and len(pair) != 2:
            reason = "duplicate_fighter_name"
        if reason is None and len(candidates) != 1:
            reason = "missing_or_ambiguous_research_pair"
        row = candidates[0] if reason is None else None
        if row is not None and row["bout_id"] in seen_bouts:
            reason = "duplicate_source_pair"
        if row is not None and card.kind == "completed":
            if row["status"] != "completed" or row["outcome"] != source.outcome:
                reason = "result_status_or_outcome_mismatch"
            elif source.outcome == "win":
                winner = (
                    row["fighter_a_name"] if row["winner_fighter_id"] == row["fighter_a_id"]
                    else row["fighter_b_name"] if row["winner_fighter_id"] == row["fighter_b_id"]
                    else None
                )
                if winner is None or _name_key(winner) != _name_key(source.winner_name or ""):
                    reason = "winner_mismatch"
            elif row["winner_fighter_id"] is not None:
                reason = "unexpected_winner"
        if reason is not None:
            held.append({"source_position": source.position, "reason": reason,
                         "fighters": [source.fighter_a.name, source.fighter_b.name]})
            continue
        assert row is not None
        seen_bouts.add(str(row["bout_id"]))
        matched_source[str(row["bout_id"])] = source
        matched_current[str(row["bout_id"])] = row
        matched.append({"source_position": source.position, "bout_id": row["bout_id"],
                        "fighter_a_id": row["fighter_a_id"],
                        "fighter_b_id": row["fighter_b_id"],
                        "outcome": source.outcome,
                        "winner_fighter_id": row["winner_fighter_id"] if card.kind == "completed" else None})
    titles = {
        title for source in matched_source.values()
        for title in (source.fighter_a.title, source.fighter_b.title) if title
    }
    identity = resolve_archived_identity_titles(connection, titles)
    resolved = identity["resolved"]
    identity_holds: list[dict] = []
    identity_verified = 0
    for match in matched:
        source = matched_source[str(match["bout_id"])]
        current_row = matched_current[str(match["bout_id"])]
        first = resolved.get(source.fighter_a.title)
        second = resolved.get(source.fighter_b.title)
        if first is None or second is None:
            missing = [title for title, record in (
                (source.fighter_a.title, first), (source.fighter_b.title, second)
            ) if record is None]
            identity_holds.append({"bout_id": match["bout_id"],
                                   "reason": "missing_source_title_lookup",
                                   "titles": missing})
            match["identity_verified"] = False
            continue
        expected_by_name = {
            _name_key(current_row["fighter_a_name"]): match["fighter_a_id"],
            _name_key(current_row["fighter_b_name"]): match["fighter_b_id"],
        }
        if (first["fighter_id"] != expected_by_name.get(_name_key(source.fighter_a.name))
                or second["fighter_id"] != expected_by_name.get(_name_key(source.fighter_b.name))):
            identity_holds.append({"bout_id": match["bout_id"],
                                   "reason": "source_title_id_mismatch",
                                   "titles": [source.fighter_a.title, source.fighter_b.title]})
            match["identity_verified"] = False
            continue
        match["identity_verified"] = True
        match["identity_lookup_run_ids"] = sorted(set(
            first["lookup_run_ids"] + second["lookup_run_ids"]
        ))
        identity_verified += 1
    return {
        "event_id": event_id,
        "event_date": card.event_date,
        "kind": card.kind,
        "revision_id": card.revision_id,
        "revision_timestamp_utc": card.revision_timestamp_utc,
        "decision_cutoff_at_utc": card.cutoff_at_utc,
        "source_bouts": len(card.bouts),
        "matched_bouts": len(matched),
        "held_bouts": len(held),
        "identity_verified_bouts": identity_verified,
        "identity_held_bouts": len(identity_holds),
        "matches": matched,
        "holds": held,
        "identity_holds": identity_holds,
        "identity_title_holds": identity["holds"],
        "model_eligible": False,
        "remaining_evidence": [
            "official UTC start for each historical target",
            "independent identity review for held or unlinked fighters",
            "completed revisions selected separately at each later decision cutoff",
            "historical bookmaker prices for a priced baseline",
        ],
    }
