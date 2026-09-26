"""Evidence worksheet for unresolved Wikipedia fighter identities.

This command never changes fighters, bouts, results, or a reviewed crosswalk.
Matching a name to a page title is a research lead, not proof of identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Callable

from .pipeline import utc_now, utc_string
from .raw_snapshots import retain_snapshot
from .wikipedia_history import ACTION_API, REST_ROOT, MediaWikiClient


_EVENT_ID = re.compile(r"^wikipedia_research:(\d+)(?::.+)?$")
_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")


def _name_key(value: str) -> str:
    # Exact Unicode spelling only: never fold accents or punctuation into an alias.
    return unicodedata.normalize("NFC", value).casefold()


def _source_receipts(connection: sqlite3.Connection) -> dict[tuple[int, str], list[sqlite3.Row]]:
    rows = connection.execute(
        """SELECT w.event_page_id, w.page_url, w.revision_id, w.event_title,
                  i.run_id, i.payload_path, i.sha256
           FROM wikipedia_source_receipts AS w
           JOIN ingestion_runs AS i USING (run_id)
           WHERE i.source = 'wikipedia_research'
           ORDER BY i.run_id"""
    )
    # The existing review JSON does not record per-event revision IDs. Keep all
    # receipts so a changed page revision can be rejected rather than silently
    # attaching a newer source to an older unresolved row.
    grouped: dict[tuple[int, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["event_page_id"], row["page_url"])].append(row)
    return grouped


def _read_source(receipt: sqlite3.Row) -> dict:
    path = Path(receipt["payload_path"])
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or unsafe source receipt: {path}")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != receipt["sha256"]:
        raise ValueError(f"Changed source receipt: {path}")
    source = json.loads(payload)
    if (source.get("id") != receipt["event_page_id"]
            or (source.get("latest") or {}).get("id") != receipt["revision_id"]
            or not isinstance(source.get("source"), str)):
        raise ValueError(f"Source receipt metadata disagrees with database: {path}")
    return source


def _same_revision_links(source: str, name: str) -> list[str]:
    key = _name_key(name)
    titles = {
        match.group(1).strip().replace("_", " ")
        for match in _WIKI_LINK.finditer(source)
        if _name_key((match.group(2) or match.group(1)).strip().replace("_", " ")) == key
    }
    return sorted(titles)


def _wikipedia_lookup(
    names: set[str],
    fetch_json: Callable[[str], dict],
    raw_dir: Path,
) -> dict[str, dict]:
    """Retain every live API response; return *candidates*, never approved IDs."""
    candidates: dict[str, dict] = {}
    ordered = sorted(names)
    for offset in range(0, len(ordered), 50):
        batch = ordered[offset:offset + 50]
        if any("|" in title or not title.strip() for title in batch):
            raise ValueError("A lookup title is empty or contains a MediaWiki separator")
        query = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "redirects": "1", "titles": "|".join(batch), "maxlag": "5",
            "prop": "pageprops", "ppprop": "disambiguation",
        })
        url = f"{ACTION_API}?{query}"
        response = fetch_json(url)
        if not isinstance(response, dict) or response.get("error") or not isinstance(response.get("query"), dict):
            raise ValueError(f"MediaWiki title lookup failed for batch {offset // 50 + 1}")
        envelope = json.dumps(
            {"request_url": url, "response": response},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        receipt_path, receipt_sha = retain_snapshot(
            envelope, raw_dir, kind="Wikipedia identity candidate lookup"
        )
        aliases: dict[str, str] = {}
        result = response["query"]
        for item in result.get("normalized", []) + result.get("redirects", []):
            if isinstance(item, dict) and isinstance(item.get("from"), str) and isinstance(item.get("to"), str):
                aliases[item["from"].replace("_", " ")] = item["to"].replace("_", " ")
        pages = {
            page["title"].replace("_", " "): page
            for page in result.get("pages", [])
            if isinstance(page, dict) and isinstance(page.get("title"), str)
        }
        for requested in batch:
            title = requested.replace("_", " ")
            seen: set[str] = set()
            while title in aliases and title not in seen:
                seen.add(title)
                title = aliases[title]
            if title in seen:
                continue
            page = pages.get(title)
            if (page is None or not isinstance(page.get("pageid"), int)
                    or page["pageid"] <= 0 or page.get("ns") != 0
                    or page.get("missing") or "disambiguation" in page.get("pageprops", {})):
                continue
            candidates[requested] = {
                "fighter_id": f"wikipedia:{page['pageid']}",
                "page_title": title,
                "page_url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe=":_()"),
                "lookup_request_url": url,
                "lookup_receipt_path": str(receipt_path),
                "lookup_receipt_sha256": receipt_sha,
                "lookup_at_utc": utc_string(utc_now()),
            }
    return candidates


def _inspect_candidate_pages(
    candidates: dict[str, dict],
    opponents_by_name: dict[str, set[str]],
    fetch_json: Callable[[str], dict],
    raw_dir: Path,
) -> None:
    """Append current-page evidence without treating it as identity approval."""
    inspected: dict[int, dict] = {}
    for name, candidate in sorted(candidates.items()):
        page_id = int(candidate["fighter_id"].split(":", 1)[1])
        if page_id not in inspected:
            url = REST_ROOT + "/" + urllib.parse.quote(
                candidate["page_title"].replace(" ", "_"), safe=":_()"
            )
            payload = fetch_json(url)
            if (not isinstance(payload, dict) or payload.get("id") != page_id
                    or not isinstance(payload.get("source"), str)
                    or not isinstance((payload.get("latest") or {}).get("id"), int)
                    or "creativecommons.org/licenses/by-sa/4.0"
                    not in str((payload.get("license") or {}).get("url", ""))):
                raise ValueError(f"Candidate page metadata changed or has no usable source: {url}")
            envelope = json.dumps(
                {"request_url": url, "response": payload},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            path, digest = retain_snapshot(
                envelope, raw_dir / "page_source", kind="Wikipedia candidate page"
            )
            inspected[page_id] = {
                "source": payload["source"],
                "revision_id": payload["latest"]["id"],
                "receipt_path": str(path),
                "receipt_sha256": digest,
            }
        page = inspected[page_id]
        source = page["source"]
        opponents = sorted(
            opponent for opponent in opponents_by_name.get(name, set())
            if _name_key(opponent) in _name_key(source)
        )
        candidate["current_page_evidence"] = {
            "source_revision_url": (
                f"https://en.wikipedia.org/w/index.php?oldid={page['revision_id']}"
            ),
            "source_revision_id": page["revision_id"],
            "source_receipt_path": page["receipt_path"],
            "source_receipt_sha256": page["receipt_sha256"],
            "mentions_ufc": "ufc" in source.casefold()
            or "ultimate fighting championship" in source.casefold(),
            "mentions_mixed_martial_arts": "mixed martial" in source.casefold()
            or "infobox martial artist" in source.casefold(),
            "mentioned_opponents_from_held_bouts": opponents,
            "automated_text_checks_are_identity_proof": False,
        }


def build_identity_review(
    connection: sqlite3.Connection,
    review_paths: list[str | Path],
    *,
    lookup_wikipedia: bool = False,
    inspect_candidate_pages: bool = False,
    lookup_raw_dir: str | Path = "data/raw/wikipedia/identity_review",
    fetch_json: Callable[[str], dict] | None = None,
) -> dict:
    """Build a source-backed worksheet, leaving all identity decisions pending."""
    if not review_paths:
        raise ValueError("At least one unresolved-fighter report is required")
    if inspect_candidate_pages and not lookup_wikipedia:
        raise ValueError("Candidate-page inspection requires a Wikipedia title lookup")
    occurrences: dict[str, list[dict]] = defaultdict(list)
    skipped_bouts: list[dict] = []
    seen_occurrences: dict[tuple[str, int, str], dict] = {}
    seen_skipped: dict[tuple[str, int], dict] = {}
    for report_path in review_paths:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if (report.get("schema_version") != 1 or report.get("source") != "wikipedia_research"
                or not isinstance(report.get("unresolved_fighters"), list)
                or not isinstance(report.get("skipped_bouts"), list)):
            raise ValueError(f"Unexpected Wikipedia identity report: {report_path}")
        for row in report["unresolved_fighters"]:
            if not all(isinstance(row.get(field), str) and row[field]
                       for field in ("event_id", "event_title", "fighter_name", "source_url")):
                raise ValueError(f"Malformed unresolved fighter row in {report_path}")
            if not isinstance(row.get("bout_position"), int) or row["bout_position"] <= 0:
                raise ValueError(f"Malformed bout position in {report_path}")
            key = (row["event_id"], row["bout_position"], row["fighter_name"])
            previous = seen_occurrences.get(key)
            if previous is not None and previous != row:
                raise ValueError(f"Conflicting repeated identity row in {report_path}: {key}")
            if previous is None:
                seen_occurrences[key] = dict(row)
                occurrences[row["fighter_name"]].append(dict(row))
        for row in report["skipped_bouts"]:
            if (not isinstance(row, dict) or not isinstance(row.get("event_id"), str)
                    or not isinstance(row.get("bout_position"), int)):
                raise ValueError(f"Malformed held bout in {report_path}")
            key = (row["event_id"], row["bout_position"])
            previous = seen_skipped.get(key)
            if previous is not None and previous != row:
                raise ValueError(f"Conflicting repeated held bout in {report_path}: {key}")
            if previous is None:
                seen_skipped[key] = dict(row)
                skipped_bouts.append(dict(row))

    unresolved_by_bout: dict[tuple[str, int], set[str]] = defaultdict(set)
    for name, rows in occurrences.items():
        for row in rows:
            unresolved_by_bout[(row["event_id"], row["bout_position"])].add(name)

    receipts = _source_receipts(connection)
    sources: dict[tuple[int, str], dict] = {}
    for name, rows in occurrences.items():
        for row in rows:
            match = _EVENT_ID.fullmatch(row["event_id"])
            if match is None:
                raise ValueError(f"Unexpected research event ID: {row['event_id']}")
            key = (int(match.group(1)), row["source_url"])
            matching_receipts = receipts.get(key)
            if not matching_receipts:
                raise ValueError(f"No immutable source receipt for {row['event_id']} / {name}")
            fingerprints = {
                (receipt["revision_id"], receipt["sha256"])
                for receipt in matching_receipts
            }
            if len(fingerprints) != 1:
                raise ValueError(
                    f"Multiple source revisions for {row['event_id']} / {name}; "
                    "the review report lacks a per-event revision ID, so regenerate it "
                    "or bind it to an explicit import receipt before reviewing identity"
                )
            receipt = matching_receipts[-1]
            if key not in sources:
                sources[key] = _read_source(receipt)
            revision = int(receipt["revision_id"])
            fragment = urllib.parse.urlsplit(row["source_url"]).fragment
            row["source_revision_url"] = (
                f"https://en.wikipedia.org/w/index.php?oldid={revision}"
                + (f"#{fragment}" if fragment else "")
            )
            row["source_revision_id"] = revision
            row["source_receipt_sha256"] = receipt["sha256"]
            row["same_revision_link_titles"] = _same_revision_links(sources[key]["source"], name)

    exact_fighters: dict[str, list[dict]] = defaultdict(list)
    for row in connection.execute("SELECT fighter_id, canonical_name, source FROM fighters"):
        exact_fighters[_name_key(row["canonical_name"])].append({
            "fighter_id": row["fighter_id"],
            "canonical_name": row["canonical_name"],
            "source": row["source"],
        })

    fetch = fetch_json or MediaWikiClient()
    title_lookups = _wikipedia_lookup(
        set(occurrences), fetch, Path(lookup_raw_dir)
    ) if lookup_wikipedia else {}
    if inspect_candidate_pages:
        opponents_by_name: dict[str, set[str]] = defaultdict(set)
        for bout in skipped_bouts:
            for name in unresolved_by_bout.get((bout.get("event_id"), bout.get("bout_position")), set()):
                opponents_by_name[name].update(
                    other for other in (bout.get("fighter_a"), bout.get("fighter_b"))
                    if isinstance(other, str) and other != name
                )
        _inspect_candidate_pages(
            title_lookups, opponents_by_name, fetch, Path(lookup_raw_dir)
        )

    entries: list[dict] = []
    candidate_names: set[str] = set()
    for name, rows in sorted(occurrences.items(), key=lambda item: (-len(item[1]), item[0])):
        existing = sorted(exact_fighters.get(_name_key(name), []), key=lambda item: item["fighter_id"])
        same_revision_links = sorted({title for row in rows for title in row["same_revision_link_titles"]})
        lookup = title_lookups.get(name)
        if existing or same_revision_links or lookup:
            candidate_names.add(name)
        entries.append({
            "fighter_name": name,
            "occurrence_count": len(rows),
            "occurrences": rows,
            "exact_existing_fighters": existing,
            "same_revision_link_titles": same_revision_links,
            "current_wikipedia_title_candidate": lookup,
            "review_status": "pending_human_identity_review",
        })

    candidate_bouts = 0
    corroborated_bouts = 0
    for bout in skipped_bouts:
        unresolved_here = unresolved_by_bout.get((bout.get("event_id"), bout.get("bout_position")), set())
        if unresolved_here and unresolved_here <= candidate_names:
            candidate_bouts += 1
        if unresolved_here and all(
            (candidate := title_lookups.get(name)) is not None
            and (evidence := candidate.get("current_page_evidence")) is not None
            and evidence["mentions_ufc"] and evidence["mentions_mixed_martial_arts"]
            and any(
                other in evidence["mentioned_opponents_from_held_bouts"]
                for other in (bout.get("fighter_a"), bout.get("fighter_b"))
                if other != name
            )
            for name in unresolved_here
        ):
            corroborated_bouts += 1

    return {
        "schema_version": 1,
        "source": "wikipedia_research",
        "review_required": True,
        "changes_to_fighters_or_bouts": False,
        "input_reports": [str(path) for path in review_paths],
        "summary": {
            "unresolved_fighter_rows": sum(len(rows) for rows in occurrences.values()),
            "distinct_unresolved_names": len(occurrences),
            "held_bouts": len(skipped_bouts),
            "names_with_exact_existing_fighter": sum(bool(exact_fighters.get(_name_key(name))) for name in occurrences),
            "rows_with_exact_existing_fighter": sum(
                len(rows) for name, rows in occurrences.items() if exact_fighters.get(_name_key(name))
            ),
            "names_with_same_revision_link": sum(
                any(row["same_revision_link_titles"] for row in rows)
                for rows in occurrences.values()
            ),
            "names_with_current_wikipedia_title": len(title_lookups),
            "title_candidates_with_mma_page_text": sum(
                bool((candidate.get("current_page_evidence") or {}).get("mentions_mixed_martial_arts"))
                for candidate in title_lookups.values()
            ),
            "title_candidates_with_ufc_and_opponent_mentions": sum(
                bool((candidate.get("current_page_evidence") or {}).get("mentions_ufc"))
                and bool((candidate.get("current_page_evidence") or {}).get("mentioned_opponents_from_held_bouts"))
                for candidate in title_lookups.values()
            ),
            "held_bouts_with_candidates_for_all_unresolved_names": candidate_bouts,
            "held_bouts_with_candidate_page_opponent_text_for_all_unresolved_sides": corroborated_bouts,
            "automatically_resolved_bouts": 0,
        },
        "fighters": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing research SQLite database")
    parser.add_argument("--review", action="append", required=True, help="Unresolved-fighter report (repeatable)")
    parser.add_argument("--output", required=True, help="Candidate worksheet JSON path")
    parser.add_argument("--lookup-wikipedia", action="store_true", help="Add current official MediaWiki title candidates")
    parser.add_argument("--inspect-candidate-pages", action="store_true", help="Retain current candidate page sources and scan for UFC/opponent text")
    parser.add_argument("--lookup-raw-dir", default="data/raw/wikipedia/identity_review")
    args = parser.parse_args(argv)
    db_path = Path(args.db).resolve()
    output_path = Path(args.output).resolve()
    if output_path == db_path:
        parser.error("Output must not replace the research database")
    uri = "file:" + urllib.parse.quote(str(db_path), safe="/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        result = build_identity_review(
            connection, args.review,
            lookup_wikipedia=args.lookup_wikipedia,
            inspect_candidate_pages=args.inspect_candidate_pages,
            lookup_raw_dir=args.lookup_raw_dir,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
