"""Build a read-only Wikidata identity worksheet for held Wikipedia bouts.

An exact printed-name match is a candidate for human review, never an identity
decision. This module does not open SQLite or produce an importer crosswalk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .raw_snapshots import retain_snapshot


SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
SPARQL_QUERY = """PREFIX schema: <http://schema.org/>
SELECT ?item ?ufcId ?itemLabel WHERE {
  ?item wdt:P9722 ?ufcId .
  FILTER NOT EXISTS {
    ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
}
ORDER BY ?item ?ufcId
"""
DEFAULT_USER_AGENT = (
    "UFCPredictionModel/0.3 "
    "(https://tylerjamesdobson.com; identity research)"
)
_QID_URL = re.compile(r"^https?://www\.wikidata\.org/entity/(Q[1-9]\d*)$")


def _name_key(value: str) -> str:
    # Preserve accents and punctuation; only Unicode composition and case vary.
    return unicodedata.normalize("NFC", value).casefold()


def _request_url() -> str:
    return SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({
        "query": SPARQL_QUERY, "format": "json",
    })


def fetch_wikidata_response(*, user_agent: str = DEFAULT_USER_AGENT) -> bytes:
    if not user_agent.strip() or "http" not in user_agent:
        raise ValueError("Use a descriptive User-Agent with a contact URL")
    request = urllib.request.Request(
        _request_url(),
        headers={"User-Agent": user_agent, "Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("Wikidata response exceeded the 2 MB worksheet limit")
    return payload


def _parse_bindings(response_bytes: bytes) -> tuple[list[dict], dict]:
    response = json.loads(response_bytes)
    bindings = (response.get("results") or {}).get("bindings") if isinstance(response, dict) else None
    if not isinstance(bindings, list):
        raise ValueError("Expected a Wikidata SPARQL JSON bindings list")
    rows: list[dict] = []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError(f"Malformed Wikidata binding {index}")
        values = {}
        for key in ("item", "ufcId", "itemLabel"):
            field = binding.get(key)
            if not isinstance(field, dict) or not isinstance(field.get("value"), str) or not field["value"].strip():
                raise ValueError(f"Wikidata binding {index} lacks {key}")
            values[key] = field["value"].strip()
        match = _QID_URL.fullmatch(values["item"])
        if match is None:
            raise ValueError(f"Wikidata binding {index} has an invalid item URI")
        rows.append({
            "qid": match.group(1),
            "label": values["itemLabel"],
            "ufc_athlete_id": values["ufcId"],
            "item_url": f"https://www.wikidata.org/wiki/{match.group(1)}",
        })
    qid_counts = Counter(row["qid"] for row in rows)
    label_qids: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        label_qids[_name_key(row["label"])].add(row["qid"])
    for row in rows:
        row["duplicate_qid_in_response"] = qid_counts[row["qid"]] > 1
        row["duplicate_label_across_qids"] = len(label_qids[_name_key(row["label"])]) > 1
    return rows, {
        "wikidata_binding_rows": len(rows),
        "distinct_wikidata_qids": len(qid_counts),
        "wikidata_qids_with_duplicate_rows": sum(count > 1 for count in qid_counts.values()),
        "wikidata_labels_with_multiple_qids": sum(len(qids) > 1 for qids in label_qids.values()),
    }


def build_candidate_worksheet(
    review_bytes: bytes,
    response_bytes: bytes,
    *,
    review_path: str,
    response_path: str,
    built_at_utc: str,
) -> dict:
    """Match exact names and group bout positions without approving any identity."""
    review = json.loads(review_bytes)
    if (not isinstance(review, dict) or review.get("schema_version") != 1
            or review.get("source") != "wikipedia_research"
            or review.get("review_required") is not True
            or not isinstance(review.get("fighters"), list)):
        raise ValueError("Expected the combined held Wikipedia identity review")
    rows, source_summary = _parse_bindings(response_bytes)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["label"] != row["qid"]:  # Query Service fallback, not a fighter label.
            by_name[_name_key(row["label"])].append(row)

    qid_rows: dict[str, list[dict]] = defaultdict(list)
    label_qids: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        qid_rows[row["qid"]].append(row)
        label_qids[_name_key(row["label"])].add(row["qid"])
    source_anomalies = {
        "repeated_qids": [{
            "qid": qid,
            "labels": sorted({row["label"] for row in grouped}),
            "ufc_athlete_ids": sorted({row["ufc_athlete_id"] for row in grouped}),
        } for qid, grouped in sorted(qid_rows.items()) if len(grouped) > 1],
        "labels_shared_by_qids": [{
            "label_key": key, "qids": sorted(qids),
        } for key, qids in sorted(label_qids.items()) if len(qids) > 1],
        "qid_used_as_label": sorted({row["qid"] for row in rows if row["label"] == row["qid"]}),
    }

    held: dict[str, list[dict]] = defaultdict(list)
    seen_names: Counter[str] = Counter()
    for index, fighter in enumerate(review["fighters"]):
        if not isinstance(fighter, dict):
            raise ValueError(f"Malformed held fighter {index}")
        name = fighter.get("fighter_name")
        occurrences = fighter.get("occurrences")
        if not isinstance(name, str) or not name.strip() or not isinstance(occurrences, list):
            raise ValueError(f"Malformed held fighter {index}")
        if fighter.get("occurrence_count") != len(occurrences):
            raise ValueError(f"Held fighter {name!r} has an inconsistent occurrence count")
        key = _name_key(name)
        seen_names[key] += 1
        for occurrence in occurrences:
            if (not isinstance(occurrence, dict)
                    or occurrence.get("fighter_name") != name
                    or not isinstance(occurrence.get("event_id"), str)
                    or type(occurrence.get("bout_position")) is not int
                    or occurrence["bout_position"] <= 0
                    or type(occurrence.get("source_revision_id")) is not int
                    or occurrence["source_revision_id"] <= 0
                    or not isinstance(occurrence.get("source_receipt_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", occurrence["source_receipt_sha256"]) is None
                    or not isinstance(occurrence.get("source_revision_url"), str)
                    or not occurrence["source_revision_url"].startswith("https://")):
                raise ValueError(f"Malformed held occurrence for {name!r}")
            held[key].append(dict(occurrence))

    candidates = []
    matched_positions: set[tuple[str, int]] = set()
    matched_occurrences = 0
    for key, occurrences in held.items():
        matches = by_name.get(key)
        if not matches:
            continue
        distinct_matches = sorted(
            {(
                row["qid"], row["label"], row["ufc_athlete_id"],
                row["duplicate_qid_in_response"], row["duplicate_label_across_qids"],
            ) for row in matches}
        )
        candidate_items = [{
            "qid": qid,
            "label": label,
            "ufc_athlete_id": athlete_id,
            "item_url": f"https://www.wikidata.org/wiki/{qid}",
            "duplicate_qid_in_response": duplicate_qid,
            "duplicate_label_across_qids": duplicate_label,
        } for qid, label, athlete_id, duplicate_qid, duplicate_label in distinct_matches]
        positions = sorted({
            (occurrence["event_id"], occurrence["bout_position"])
            for occurrence in occurrences
        })
        matched_positions.update(positions)
        matched_occurrences += len(occurrences)
        printed_names = sorted({occurrence["fighter_name"] for occurrence in occurrences})
        candidates.append({
            "fighter_names_in_review": printed_names,
            "occurrence_count": len(occurrences),
            "distinct_held_bout_positions": len(positions),
            "duplicate_casefold_name_in_review": seen_names[key] > 1,
            "multiple_candidate_qids": len({row["qid"] for row in matches}) > 1,
            "wikidata_candidates": candidate_items,
            "occurrences": occurrences,
            "review_status": "pending_bout_specific_identity_review",
        })
    candidates.sort(key=lambda item: (-item["occurrence_count"], item["fighter_names_in_review"][0]))
    review_anomalies = {
        "casefold_duplicate_name_entries": [{
            "name_key": key,
            "printed_names": sorted({row["fighter_name"] for row in held[key]}),
        } for key, count in sorted(seen_names.items()) if count > 1],
    }
    return {
        "schema_version": 1,
        "source": "wikidata_p9722_no_enwiki_identity_candidates",
        "candidate_only": True,
        "review_required": True,
        "changes_to_fighters_or_bouts": False,
        "automatically_resolved_bouts": 0,
        "wikidata": {
            "query_url": _request_url(),
            "query": SPARQL_QUERY,
            "worksheet_built_at_utc": built_at_utc,
            "response_path": response_path,
            "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
        },
        "input_review": {
            "path": review_path,
            "sha256": hashlib.sha256(review_bytes).hexdigest(),
            "reported_held_bouts": (review.get("summary") or {}).get("held_bouts"),
        },
        "source_anomalies": source_anomalies,
        "input_review_anomalies": review_anomalies,
        "summary": {
            **source_summary,
            "held_fighter_entries": len(review["fighters"]),
            "distinct_held_name_keys": len(held),
            "held_name_keys_with_duplicate_entries": sum(count > 1 for count in seen_names.values()),
            "matched_held_name_keys": len(candidates),
            "matched_held_occurrences": matched_occurrences,
            "distinct_held_bout_positions_with_a_candidate": len(matched_positions),
            "unmatched_held_name_keys": len(held) - len(candidates),
        },
        "candidates": candidates,
    }


def _atomic_write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, default=Path("reports/wikipedia_1993_2026_identity_review.json"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw/wikidata"))
    parser.add_argument("--output", type=Path, default=Path("reports/wikidata_identity_candidates.json"))
    parser.add_argument("--response-file", type=Path, help="Replay exact previously saved SPARQL response bytes offline")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args(argv)
    review_path = args.review.resolve()
    output_path = args.output.resolve()
    response_file = args.response_file.resolve() if args.response_file else None
    if output_path == review_path or (response_file is not None and output_path == response_file):
        parser.error("Candidate output must not replace source evidence")
    review_bytes = review_path.read_bytes()
    response_bytes = response_file.read_bytes() if response_file else fetch_wikidata_response(user_agent=args.user_agent)
    snapshot_path, digest = retain_snapshot(
        response_bytes, args.raw_dir, kind="Wikidata SPARQL response"
    )
    built_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    document = build_candidate_worksheet(
        review_bytes, response_bytes,
        review_path=str(review_path), response_path=str(snapshot_path),
        built_at_utc=built_at_utc,
    )
    document["wikidata"]["response_origin"] = "saved_response_file" if response_file else "live_query"
    if document["wikidata"]["response_sha256"] != digest:
        raise ValueError("Wikidata response hash changed while building worksheet")
    _atomic_write_json(output_path, document)
    print(json.dumps(document["summary"], indent=2))
    print(f"Candidate worksheet: {output_path}")
    print(f"Exact Wikidata response: {snapshot_path} (sha256: {digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
