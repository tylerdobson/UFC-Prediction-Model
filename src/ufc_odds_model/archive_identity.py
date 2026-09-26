"""Offline fighter-title identity evidence from retained MediaWiki lookups.

These receipts were fetched during research imports, often after the fights.
They establish a stable page ID for a linked title, not that the lookup was
available at a historical decision cutoff or that a bout was scheduled.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Iterable

from .card_history import _receipt_payload_problem
from .wikipedia_history import ACTION_API


def _title_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("_", " "))


def _requested_titles(url: str) -> set[str]:
    parsed = urllib.parse.urlsplit(url)
    api = urllib.parse.urlsplit(ACTION_API)
    if (parsed.scheme, parsed.netloc, parsed.path, parsed.fragment) != (
        api.scheme, api.netloc, api.path, ""
    ):
        raise ValueError("lookup_request_url_invalid")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    required = {
        "action": "query", "format": "json", "formatversion": "2",
        "redirects": "1", "prop": "pageprops", "ppprop": "disambiguation",
    }
    if set(query) - (set(required) | {"titles", "maxlag"}):
        raise ValueError("lookup_request_url_invalid")
    if any(query.get(key) != [value] for key, value in required.items()):
        raise ValueError("lookup_request_url_invalid")
    if "maxlag" in query and (len(query["maxlag"]) != 1
                              or not query["maxlag"][0].isdigit()
                              or int(query["maxlag"][0]) <= 0):
        raise ValueError("lookup_request_url_invalid")
    if len(query.get("titles", [])) != 1:
        raise ValueError("lookup_request_url_invalid")
    titles = [_title_key(value) for value in query["titles"][0].split("|")]
    if any(not title.strip() or title != title.strip() for title in titles):
        raise ValueError("lookup_request_url_invalid")
    if len(set(titles)) != len(titles):
        raise ValueError("lookup_request_url_invalid")
    return set(titles)


def _page_for_title(response: dict, requested: str) -> tuple[int, str]:
    query = response.get("query")
    if response.get("error") or not isinstance(query, dict):
        raise ValueError("lookup_response_invalid")
    aliases: dict[str, str] = {}
    for field in ("normalized", "redirects"):
        entries = query.get(field, [])
        if not isinstance(entries, list):
            raise ValueError("lookup_response_invalid")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("from"), str) or not isinstance(entry.get("to"), str):
                raise ValueError("lookup_response_invalid")
            start, end = _title_key(entry["from"]), _title_key(entry["to"])
            if not start or not end or (start in aliases and aliases[start] != end):
                raise ValueError("lookup_alias_conflict")
            aliases[start] = end

    title = requested
    visited: set[str] = set()
    while title in aliases:
        if title in visited:
            raise ValueError("lookup_alias_cycle")
        visited.add(title)
        title = aliases[title]

    entries = query.get("pages")
    if not isinstance(entries, list):
        raise ValueError("lookup_response_invalid")
    matches = [entry for entry in entries
               if isinstance(entry, dict) and isinstance(entry.get("title"), str)
               and _title_key(entry["title"]) == title]
    if not matches:
        raise ValueError("lookup_page_missing")
    page_ids: set[int] = set()
    for page in matches:
        props = page.get("pageprops", {})
        page_id = page.get("pageid")
        if (type(page_id) is not int or page_id <= 0 or page.get("ns") != 0
                or "missing" in page or not isinstance(props, dict)):
            raise ValueError("lookup_page_invalid")
        if "disambiguation" in props:
            raise ValueError("lookup_page_disambiguation")
        page_ids.add(page_id)
    if len(page_ids) != 1:
        raise ValueError("lookup_page_id_conflict")
    return next(iter(page_ids)), title


def resolve_archived_identity_titles(
    connection: sqlite3.Connection, titles: Iterable[str],
) -> dict:
    """Resolve linked titles using intact, exact-request Action API receipts.

    ``resolved`` contains stable Wikipedia page IDs with receipt run IDs and
    actual fetch times. A missing, inconsistent, or corrupted lookup is held;
    the caller must not turn this retrospective evidence into a pre-fight
    observation. One unparseable receipt holds all requested titles because its
    requested titles cannot be determined safely.
    """
    supplied = list(titles)
    if any(not isinstance(title, str) or not title.strip() or title != title.strip()
           or "|" in title or "#" in title for title in supplied):
        raise ValueError("titles must be nonempty, exact MediaWiki link titles")
    ordered = sorted(set(supplied))
    if not ordered:
        return {"resolved": {}, "holds": []}

    by_key: dict[str, list[str]] = {}
    for title in ordered:
        by_key.setdefault(_title_key(title), []).append(title)
    state = {title: {"candidates": [], "problems": set()} for title in ordered}
    receipt_rows = connection.execute(
        """SELECT run_id, fetched_at_utc, payload_path, sha256
           FROM ingestion_runs WHERE source = 'wikipedia_action_api'
           ORDER BY run_id"""
    ).fetchall()
    for run_id, fetched_at_utc, payload_path, sha256 in receipt_rows:
        problem = _receipt_payload_problem(payload_path, sha256)
        if problem is not None:
            for value in state.values():
                value["problems"].add("lookup_receipt_invalid")
            continue
        # Check the bytes again after reading to avoid trusting a file changed
        # between the receipt check and JSON parsing.
        try:
            raw = Path(payload_path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha256:
                raise ValueError("lookup_receipt_invalid")
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or not isinstance(envelope.get("response"), dict):
                raise ValueError("lookup_response_invalid")
            requested = _requested_titles(envelope.get("request_url", ""))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            for value in state.values():
                value["problems"].add("lookup_receipt_invalid")
            continue
        for key in requested & by_key.keys():
            for title in by_key[key]:
                try:
                    page_id, page_title = _page_for_title(envelope["response"], key)
                    state[title]["candidates"].append({
                        "page_id": page_id, "page_title": page_title,
                        "run_id": int(run_id), "fetched_at_utc": fetched_at_utc,
                        "sha256": sha256,
                    })
                except ValueError as exc:
                    state[title]["problems"].add(str(exc))

    resolved: dict[str, dict] = {}
    holds: list[dict] = []
    for title in ordered:
        candidates = state[title]["candidates"]
        problems = state[title]["problems"]
        ids = {item["page_id"] for item in candidates}
        if len(ids) > 1:
            problems.add("lookup_page_id_conflict")
        if not candidates and not problems:
            problems.add("lookup_request_missing")
        if problems:
            holds.append({"title": title, "reason": sorted(problems)[0],
                          "reasons": sorted(problems)})
            continue
        evidence = sorted(candidates, key=lambda item: item["run_id"])
        page_id = evidence[-1]["page_id"]
        resolved[title] = {
            "fighter_id": f"wikipedia:{page_id}",
            "page_id": page_id,
            "page_title": evidence[-1]["page_title"],
            "lookup_run_ids": [item["run_id"] for item in evidence],
            "lookup_evidence": [{
                "run_id": item["run_id"], "fetched_at_utc": item["fetched_at_utc"],
                "sha256": item["sha256"],
            } for item in evidence],
        }
    return {"resolved": resolved, "holds": holds}
