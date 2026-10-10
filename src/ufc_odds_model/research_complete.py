"""Restore identity-held Wikipedia fights to a NEW research-only database."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unicodedata
from collections import defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import db
from .model_lab import file_sha256, read_only, write_new_json
from .retained_paths import retained_path
from .raw_snapshots import retain_snapshot
from .pipeline import utc_now, utc_string
from .research_evaluation import (
    _intact_payload, _receipt_page_key, _reconcile_identity_receipts, _source_page_key,
)
from .wikipedia_embedded import parse_embedded_event
from .wikipedia_history import FighterRef, ResultBout, parse_event_page_title

SOURCE = "wikipedia_research_restored"


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def saved_title_map(connection: sqlite3.Connection) -> tuple[dict[str, int], dict[int, str]]:
    """Resolve normalized/redirected titles only through hash-verified receipts."""
    titles, page_titles = {}, {}
    for receipt in connection.execute(
        "SELECT payload_path, sha256 FROM ingestion_runs WHERE source = 'wikipedia_action_api' ORDER BY run_id"
    ):
        if not _intact_payload(receipt["payload_path"], receipt["sha256"]):
            raise ValueError(f"Changed Action API receipt: {receipt['payload_path']}")
        payload = json.loads(retained_path(receipt["payload_path"]).read_bytes())
        response = payload.get("response", payload).get("query", {})
        pages = response.get("pages", [])
        if isinstance(pages, dict):
            pages = list(pages.values())
        canonical = {}
        for page in pages:
            page_id = page.get("pageid")
            if (not isinstance(page_id, int) or page_id <= 0 or page.get("ns") != 0
                    or "missing" in page or "disambiguation" in page.get("pageprops", {})):
                continue
            title = str(page["title"]).replace("_", " ")
            canonical[title] = page_id
            page_titles[page_id] = title
        aliases = {str(item["from"]).replace("_", " "): str(item["to"]).replace("_", " ")
                   for item in response.get("normalized", []) + response.get("redirects", [])}
        for title in set(canonical) | set(aliases):
            resolved, seen = title, set()
            while resolved in aliases and resolved not in seen:
                seen.add(resolved)
                resolved = aliases[resolved]
            if resolved in canonical:
                if title in titles and titles[title] != canonical[resolved]:
                    raise ValueError(f"Conflicting saved page IDs for {title}")
                titles[title] = canonical[resolved]
    return titles, page_titles


def held_positions(connection: sqlite3.Connection, review: dict):
    if (review.get("schema_version") != 1 or review.get("source") != "wikipedia_research"
            or review.get("review_required") is not True):
        raise ValueError("Expected the checked Wikipedia identity worksheet")
    positions = {(str(o["event_id"]), int(o["bout_position"]))
                 for f in review.get("fighters", []) for o in f.get("occurrences", [])}
    if len(positions) != review.get("summary", {}).get("held_bouts") or any(p <= 0 for _, p in positions):
        raise ValueError("Identity worksheet positions disagree with its summary")
    recovered = _reconcile_identity_receipts(connection, positions)
    if recovered - positions:
        raise ValueError("Recovered identities are absent from the worksheet")
    return positions - recovered, len(recovered)


def _fallbacks(review: dict, review_path: Path) -> dict:
    answer = {}
    for name in review.get("input_reports", []):
        path = Path(name)
        if not path.is_file():
            path = review_path.parent / path.name
        if not path.is_file():
            continue
        payload = json.loads(path.read_bytes())
        if payload.get("source") != "wikipedia_research":
            continue
        for bout in payload.get("skipped_bouts", []):
            key = str(bout["event_id"]), int(bout["bout_position"])
            selected = {"bout": bout, "path": str(path.resolve()), "sha256": file_sha256(path)}
            prior = answer.get(key)
            if prior and prior["bout"] != bout:
                raise ValueError(f"Conflicting skipped-bout fallback for {key}")
            answer[key] = selected
    return answer


def collision_report(connection: sqlite3.Connection) -> list[dict]:
    real = defaultdict(list)
    for fighter in connection.execute("SELECT * FROM fighters WHERE source != 'wikipedia_provisional' OR source IS NULL"):
        real[normalize_name(fighter["canonical_name"])].append(str(fighter["fighter_id"]))
    flags = []
    for fighter in connection.execute("SELECT * FROM fighters WHERE source = 'wikipedia_provisional'"):
        fighter_id = str(fighter["fighter_id"])
        key = normalize_name(fighter["canonical_name"])
        if real[key]:
            flags.append({"fighter_id": fighter_id, "reason": "real_canonical_name_collision",
                          "real_fighter_ids": sorted(real[key])})
        appearances = [dict(r) for r in connection.execute(
            """SELECT e.event_date, b.bout_id, b.weight_class FROM bouts b
               JOIN events e ON e.event_id = b.event_id
               WHERE b.fighter_a_id = ? OR b.fighter_b_id = ? ORDER BY e.event_date, b.bout_id""",
            (fighter_id, fighter_id))]
        dates = defaultdict(list)
        for appearance in appearances:
            dates[appearance["event_date"]].append(appearance["bout_id"])
        for day, bouts in dates.items():
            if len(bouts) > 1:
                flags.append({"fighter_id": fighter_id, "reason": "multiple_bouts_one_date",
                              "event_date": day, "bout_ids": bouts,
                              "note": "May be a valid early tournament; review, never auto-merge"})
        for index, first in enumerate(appearances):
            for second in appearances[index + 1:]:
                if (date.fromisoformat(second["event_date"]) - date.fromisoformat(first["event_date"])).days > 366:
                    break
                # Collapse spelling/capitalization, but preserve genuinely different divisions.
                left = normalize_name(first["weight_class"] or "")
                right = normalize_name(second["weight_class"] or "")
                if left and right and left != right:
                    flags.append({"fighter_id": fighter_id, "reason": "different_weight_families_within_12_months",
                                  "appearances": [first, second]})
                    break
    return flags


def restore_bouts(connection, positions, title_map, fallback_map):
    receipts = {}
    for receipt in connection.execute(
        """SELECT w.*,i.payload_path,i.sha256,i.fetched_at_utc FROM wikipedia_source_receipts w
           JOIN ingestion_runs i ON i.run_id=w.run_id WHERE i.source='wikipedia_research'
           ORDER BY w.run_id"""):
        receipts[_receipt_page_key(receipt["event_page_id"], receipt["page_url"])] = receipt
    parsed, restoration, fallback_count = {}, [], 0
    for event_id, position in sorted(positions):
        event = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if event is None or event["source"] != "wikipedia_research":
            raise ValueError(f"Held event is absent from research: {event_id}")
        receipt = receipts.get(_source_page_key(str(event["source_event_id"])))
        if receipt is None:
            raise ValueError(f"No source receipt for {event_id}")
        if not _intact_payload(receipt["payload_path"], receipt["sha256"]):
            # A changed receipt is corruption, never permission to use a weaker fallback.
            raise ValueError(f"Changed event receipt for {event_id}")
        if event_id not in parsed:
            payload = json.loads(retained_path(receipt["payload_path"]).read_bytes())
            try:
                section = unquote(urlsplit(receipt["page_url"]).fragment).replace("_", " ")
                if section:
                    card = parse_embedded_event(payload, source_page_title=receipt["event_title"],
                                                target_heading=section, expected_date=event["event_date"],
                                                expected_page_id=receipt["event_page_id"])
                else:
                    card = parse_event_page_title(payload, receipt["event_title"])
                if card.event_date != event["event_date"]:
                    raise ValueError("Parsed event date differs from reviewed stored date")
                parsed[event_id] = {b.position: b for b in card.bouts}, None
            except ValueError as exc:
                parsed[event_id] = {}, str(exc)
        bouts, parse_problem = parsed[event_id]
        bout = bouts.get(position)
        fallback = None
        if bout is None:
            fallback = fallback_map.get((event_id, position))
            if fallback is None:
                raise ValueError(f"Cannot rebuild held bout {event_id}/{position}: {parse_problem}")
            row = fallback["bout"]
            outcome = str(row["outcome"])
            if outcome not in {"win", "draw", "no_contest"}:
                raise ValueError("Fallback has an ambiguous outcome")
            bout = ResultBout(position, row.get("weight_class") or "Unknown",
                              FighterRef(row["fighter_a"], row.get("fighter_a_title")),
                              FighterRef(row["fighter_b"], row.get("fighter_b_title")),
                              outcome, "a" if outcome == "win" else None,
                              row.get("method") or "Unknown (review fallback)")
            fallback_count += 1
        identities = []
        for fighter in (bout.fighter_a, bout.fighter_b):
            page_id = title_map.get(fighter.title.replace("_", " ")) if fighter.title else None
            if page_id is not None:
                fighter_id = db.resolve_fighter_id(connection, "wikipedia", str(page_id))
                identity_source, external_id = "wikipedia", str(page_id)
            else:
                external_id = normalize_name(fighter.name)
                fighter_id = f"provisional:wikipedia_name:{external_id}"
                identity_source = "wikipedia_provisional"
            if not connection.execute("SELECT 1 FROM fighters WHERE fighter_id=?", (fighter_id,)).fetchone():
                db.upsert_fighter(connection, fighter_id, fighter.name, identity_source, external_id)
            identities.append(fighter_id)
        a, b = identities
        if a == b:
            raise ValueError(f"Identity collision within restored bout {event_id}/{position}")
        bout_id = f"{event_id}:restored:{position}"
        db.upsert_bout(connection, bout_id, event_id, a, b, "completed", bout.weight_class,
                       source=SOURCE, source_bout_id=f"{event['source_event_id']}:{position}")
        db.upsert_result(connection, bout_id, bout.outcome, a if bout.winner_side == "a" else None,
                         receipt["fetched_at_utc"], bout.method)
        restoration.append({"bout_id": bout_id, "event_id": event_id, "bout_position": position,
                            "fighter_a_id": a, "fighter_b_id": b, "outcome": bout.outcome,
                            "method": bout.method, "source_run_id": receipt["run_id"],
                            "source_sha256": receipt["sha256"], "fallback": fallback,
                            "parse_problem": parse_problem})
    return restoration, fallback_count


def complete_history(source_path, output_path, *, review_path="reports/wikipedia_1993_2026_identity_review.json",
                     report_path="reports/research_complete_identity_report.json",
                     raw_dir="data/raw/research_complete"):
    source_path, output_path, review_path = map(Path, (source_path, output_path, review_path))
    if source_path.resolve() == output_path.resolve() or output_path.exists() or output_path.is_symlink():
        raise ValueError("Complete history needs a new, separate output database")
    if Path(report_path).exists():
        raise ValueError("Identity report needs a new filename")
    original_hash = file_sha256(source_path)
    review = json.loads(review_path.read_bytes())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Work in a temporary directory; publish once, never replace any existing output.
    with tempfile.TemporaryDirectory(prefix="ufc-complete-") as temporary:
        staged_path = Path(temporary) / "complete.sqlite"
        with read_only(source_path) as original, closing(db.connect(staged_path)) as connection:
            positions, recovered = held_positions(original, review)
            titles, _ = saved_title_map(original)
            original.backup(connection)
            with connection:
                restoration, fallback_count = restore_bouts(
                    connection, positions, titles, _fallbacks(review, review_path))
            report = {"schema_version": 1, "research_only": True, "promotion_eligible": False,
                      "original_database_sha256": original_hash,
                      "worksheet_sha256": file_sha256(review_path),
                      "worksheet_held_bouts": review["summary"]["held_bouts"],
                      "identity_replays_recovered": recovered, "restored_bouts": len(restoration),
                      "held_bouts_remaining": len(positions) - len(restoration),
                      "parser_fallbacks": fallback_count, "restored_bout_receipts": restoration,
                      "identity_flags": collision_report(connection),
                      "provisional_fighters": connection.execute(
                          "SELECT COUNT(*) FROM fighters WHERE source='wikipedia_provisional'").fetchone()[0],
                      "no_automatic_provisional_merges": True}
            raw, digest = retain_snapshot(json.dumps(report, sort_keys=True).encode(), raw_dir,
                                          kind="complete-history restoration manifest")
            with connection:
                connection.execute(
                    "INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES (?,?,?,?)",
                    (SOURCE, utc_string(utc_now()), str(raw), digest))
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Restored database has broken foreign keys")
        if file_sha256(source_path) != original_hash:
            raise RuntimeError("Original research database changed")
        # Copy bytes into an exclusive-created file (temporary may be a different filesystem).
        with staged_path.open("rb") as source, output_path.open("xb") as destination:
            import shutil
            shutil.copyfileobj(source, destination)
        report["complete_database_sha256"] = file_sha256(output_path)
        write_new_json(report_path, report)
    return report
