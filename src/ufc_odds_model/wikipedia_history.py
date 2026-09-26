"""Research-only historical results from MediaWiki's official page-source API.

This deliberately imports completed result tables only. Wikipedia pages do not
establish a pre-fight roster, precise event start timestamp, or bookmaker quote.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from . import db
from .pipeline import utc_now, utc_string
from .raw_snapshots import retain_snapshot


REST_ROOT = "https://en.wikipedia.org/w/rest.php/v1/page"
ACTION_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = (
    "UFCPredictionModelResearch/0.1 "
    "(https://github.com/tylerdobson/UFC-Prediction-Model; research-only)"
)
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/deed.en"
_HEADING = re.compile(r"^==\s*Results\s*==\s*$", re.I | re.M)
_NEXT_HEADING = re.compile(r"^==[^=].*?==\s*$", re.M)
_BOUT_OPEN = re.compile(r"\{\{\s*MMAevent bout(?=\s|\|)", re.I)
_LINKED_FIGHTER = re.compile(
    r"^\[\[([^\]|#]+)(?:\|([^\]]+))?\]\](?:\s*\((?:c|ic)\))?$", re.I
)
_PLAIN_FIGHTER = re.compile(r"^[^{}\[\]<>|]+$")
_DATE = re.compile(r"^\|date\s*=\s*\{\{start date\|(\d{4})\|(\d{1,2})\|(\d{1,2})\}\}", re.I | re.M)
_NAME = re.compile(r"^\|name\s*=\s*(.+)$", re.I | re.M)


@dataclass(frozen=True)
class FighterRef:
    name: str
    title: str | None


@dataclass(frozen=True)
class ResultBout:
    position: int
    weight_class: str
    fighter_a: FighterRef
    fighter_b: FighterRef
    outcome: str
    winner_side: str | None
    method: str


@dataclass(frozen=True)
class ParsedEvent:
    page_id: int
    title: str
    event_name: str
    event_date: str
    revision_id: int
    revision_timestamp_utc: str
    license_title: str
    license_url: str
    bouts: tuple[ResultBout, ...]


class MediaWikiClient:
    """Serial, rate-aware JSON requests with identifiable API traffic."""

    def __init__(self, user_agent: str = USER_AGENT, min_interval_seconds: float = 0.25):
        if not user_agent or "http" not in user_agent:
            raise ValueError("A descriptive User-Agent with a contact URL is required")
        self.user_agent = user_agent
        self.min_interval_seconds = min_interval_seconds
        self._last_request = 0.0

    def __call__(self, url: str) -> dict:
        for attempt in range(4):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            request = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
            )
            self._last_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("MediaWiki returned a non-object JSON response")
                if payload.get("error"):
                    details = payload["error"]
                    if (isinstance(details, dict) and details.get("code") == "maxlag"
                            and attempt < 3):
                        time.sleep(min(max(float(details.get("lag", 1.0)), 1.0), 30.0))
                        continue
                    raise ValueError(f"MediaWiki API error: {payload['error']}")
                return payload
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"MediaWiki HTTP {error.code} for {url}") from error
                retry_header = error.headers.get("Retry-After", "")
                try:
                    delay = float(retry_header)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_header)
                        delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        delay = min(2 ** attempt, 8)
                if delay > 60:
                    raise RuntimeError(f"MediaWiki asked for a retry after {delay:.0f} seconds") from error
                time.sleep(max(delay, 0.5))
            except urllib.error.URLError as error:
                if attempt == 3:
                    raise RuntimeError(f"MediaWiki request failed for {url}: {error.reason}") from error
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("MediaWiki retry limit reached")


def _results_section(source: str) -> str:
    heading = _HEADING.search(source)
    if not heading:
        raise ValueError("The source revision has no Results section")
    rest = source[heading.end():]
    following = _NEXT_HEADING.search(rest)
    return rest[:following.start()] if following else rest


def _template_text(source: str, start: int) -> str:
    depth = 0
    pos = start
    while pos < len(source) - 1:
        token = source[pos:pos + 2]
        if token == "{{":
            depth += 1
            pos += 2
        elif token == "}}":
            depth -= 1
            pos += 2
            if depth == 0:
                return source[start:pos]
        else:
            pos += 1
    raise ValueError("Unclosed MMAevent bout template")


def _split_template(template: str) -> list[str]:
    if not template.startswith("{{") or not template.endswith("}}"):
        raise ValueError("Invalid MediaWiki template boundaries")
    body = template[2:-2]
    parts: list[str] = []
    start = 0
    template_depth = 0
    link_depth = 0
    pos = 0
    while pos < len(body):
        token = body[pos:pos + 2]
        if token == "{{":
            template_depth += 1
            pos += 2
        elif token == "}}":
            template_depth -= 1
            if template_depth < 0:
                raise ValueError("Malformed nested template")
            pos += 2
        elif token == "[[":
            link_depth += 1
            pos += 2
        elif token == "]]":
            link_depth -= 1
            if link_depth < 0:
                raise ValueError("Malformed wiki link")
            pos += 2
        elif body[pos] == "|" and template_depth == link_depth == 0:
            parts.append(body[start:pos].strip())
            start = pos + 1
            pos += 1
        else:
            pos += 1
    if template_depth or link_depth:
        raise ValueError("Unclosed nested markup in bout template")
    parts.append(body[start:].strip())
    return parts


def _fighter_ref(value: str) -> FighterRef:
    clean = value.strip()
    linked = _LINKED_FIGHTER.fullmatch(clean)
    if linked:
        title = linked.group(1).strip().replace("_", " ")
        name = (linked.group(2) or title).strip()
        if not title or not name:
            raise ValueError(f"Ambiguous linked fighter: {value!r}")
        return FighterRef(name, title)
    if not clean or not _PLAIN_FIGHTER.fullmatch(clean):
        raise ValueError(f"Ambiguous unlinked fighter: {value!r}")
    name = re.sub(r"\s*\((?:c|ic)\)$", "", clean, flags=re.I).strip()
    if not name:
        raise ValueError(f"Empty fighter: {value!r}")
    return FighterRef(name, None)


def parse_event_page(payload: dict, expected_number: int) -> ParsedEvent:
    """Fail closed on result markup outside the verified positional template."""
    expected_title = f"UFC {expected_number}"
    title = payload.get("title")
    if title != expected_title:
        raise ValueError(f"Expected {expected_title}, got {title!r}")
    page_id = payload.get("id")
    revision = payload.get("latest") or {}
    license_info = payload.get("license") or {}
    source = payload.get("source")
    if not isinstance(page_id, int) or page_id <= 0:
        raise ValueError(f"{expected_title}: missing stable page ID")
    if not isinstance(revision.get("id"), int) or not revision.get("timestamp"):
        raise ValueError(f"{expected_title}: missing source revision")
    if not isinstance(source, str) or not source:
        raise ValueError(f"{expected_title}: missing wikitext source")
    license_url = str(license_info.get("url") or "")
    if "creativecommons.org/licenses/by-sa/4.0" not in license_url:
        raise ValueError(f"{expected_title}: unexpected source license {license_url!r}")
    date_match = _DATE.search(source)
    name_match = _NAME.search(source)
    if not date_match or not name_match:
        raise ValueError(f"{expected_title}: missing event name or event date")
    try:
        event_date = date(*map(int, date_match.groups())).isoformat()
    except ValueError as error:
        raise ValueError(f"{expected_title}: invalid event date") from error
    section = _results_section(source)
    matches = list(_BOUT_OPEN.finditer(section))
    if not matches:
        raise ValueError(f"{expected_title}: no MMAevent bout templates")
    bouts: list[ResultBout] = []
    for position, match in enumerate(matches, start=1):
        parts = _split_template(_template_text(section, match.start()))
        if parts[0].casefold() != "mmaevent bout" or len(parts) < 6:
            raise ValueError(f"{expected_title} bout {position}: unexpected template shape")
        weight, first_raw, marker_raw, second_raw, method = parts[1:6]
        if not weight.strip() or not method.strip():
            raise ValueError(f"{expected_title} bout {position}: missing weight class or result method")
        first, second = _fighter_ref(first_raw), _fighter_ref(second_raw)
        if first.name.casefold() == second.name.casefold():
            raise ValueError(f"{expected_title} bout {position}: identical fighter names")
        marker = marker_raw.strip().casefold()
        method_lower = method.strip().casefold()
        if marker == "def." and not method_lower.startswith(("draw", "no contest", "nc")):
            outcome, winner_side = "win", "a"
        elif marker in ("vs.", "v.") and method_lower.startswith("draw"):
            outcome, winner_side = "draw", None
        elif marker in ("vs.", "v.", "nc") and method_lower.startswith(("no contest", "nc")):
            outcome, winner_side = "no_contest", None
        else:
            raise ValueError(
                f"{expected_title} bout {position}: ambiguous result marker {marker_raw!r} / {method!r}"
            )
        bouts.append(ResultBout(position, weight.strip(), first, second, outcome, winner_side, method.strip()))
    return ParsedEvent(
        page_id, title, name_match.group(1).strip(), event_date,
        revision["id"], str(revision["timestamp"]),
        str(license_info.get("title") or ""), license_url, tuple(bouts),
    )


def _resolve_page_ids(fetch_json: Callable[[str], dict], titles: set[str]) -> dict[str, int]:
    """Batch linked fighter titles through the official Action API."""
    resolved: dict[str, int] = {}
    ordered = sorted(titles)
    for offset in range(0, len(ordered), 50):
        batch = ordered[offset:offset + 50]
        query = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "redirects": "1", "titles": "|".join(batch), "maxlag": "5",
            "prop": "pageprops", "ppprop": "disambiguation",
        })
        payload = fetch_json(f"{ACTION_API}?{query}")
        if payload.get("error") or "query" not in payload:
            raise ValueError(f"MediaWiki title lookup failed: {payload.get('error')}")
        response = payload["query"]
        aliases: dict[str, str] = {}
        for item in response.get("normalized", []) + response.get("redirects", []):
            aliases[str(item["from"]).replace("_", " ")] = str(item["to"]).replace("_", " ")
        page_ids = {
            str(page["title"]).replace("_", " "): int(page["pageid"])
            for page in response.get("pages", [])
            if isinstance(page.get("pageid"), int)
            and page["pageid"] > 0 and page.get("ns") == 0
            and not page.get("missing")
            and "disambiguation" not in page.get("pageprops", {})
        }
        for title in batch:
            canonical = title.replace("_", " ")
            seen: set[str] = set()
            while canonical in aliases and canonical not in seen:
                seen.add(canonical)
                canonical = aliases[canonical]
            if canonical in page_ids:
                resolved[title] = page_ids[canonical]
    return resolved


def _load_crosswalk(path: str | Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    with Path(path).open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("fighters"), dict):
        raise ValueError("Crosswalk needs schema_version 1 and a fighters object")
    for name, entry in payload["fighters"].items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            raise ValueError("Crosswalk fighter entries must be keyed by a non-empty name")
        if not all(isinstance(entry.get(key), str) and entry[key].strip()
                   for key in ("fighter_id", "canonical_name", "evidence_url", "reviewed_by")):
            raise ValueError(f"Crosswalk entry for {name!r} lacks reviewed identity evidence")
        if not entry["evidence_url"].startswith("https://"):
            raise ValueError(f"Crosswalk evidence for {name!r} must be an HTTPS URL")
        if entry["fighter_id"].startswith("wikipedia:") and not (
            isinstance(entry.get("page_title"), str) and entry["page_title"].strip()
        ):
            raise ValueError(
                f"Crosswalk entry for {name!r} needs page_title to verify the Wikipedia page ID"
            )
    return payload["fighters"]


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_wikipedia_history(
    connection: sqlite3.Connection,
    first_event: int = 295,
    last_event: int = 304,
    *,
    raw_dir: str | Path = "data/raw/wikipedia",
    review_out: str | Path = "reports/wikipedia_identity_review.json",
    crosswalk_path: str | Path | None = None,
    fetch_json: Callable[[str], dict] | None = None,
) -> dict[str, object]:
    """Import only the verified UFC 295–304 historical range into a research DB.

    Unlinked or unresolved fighters remain in the review artifact, and their
    bouts are skipped. No name-based IDs or name-based merges are made.
    """
    if not (295 <= first_event <= last_event <= 304):
        raise ValueError("Wikipedia research importer is bounded to UFC 295–304")
    fetch = fetch_json or MediaWikiClient()
    crosswalk = _load_crosswalk(crosswalk_path)
    page_payloads: list[tuple[dict, ParsedEvent]] = []
    for number in range(first_event, last_event + 1):
        payload = fetch(f"{REST_ROOT}/UFC_{number}")
        page_payloads.append((payload, parse_event_page(payload, number)))
    linked_titles = {
        fighter.title
        for _, event in page_payloads
        for bout in event.bouts
        for fighter in (bout.fighter_a, bout.fighter_b)
        if fighter.title is not None
    }
    linked_titles.update(
        entry["page_title"].strip()
        for entry in crosswalk.values()
        if entry["fighter_id"].startswith("wikipedia:")
    )
    page_ids = _resolve_page_ids(fetch, linked_titles)
    for name, entry in crosswalk.items():
        if entry["fighter_id"].startswith("wikipedia:"):
            title = entry["page_title"].strip()
            actual_id = page_ids.get(title)
            if actual_id is None or entry["fighter_id"] != f"wikipedia:{actual_id}":
                raise ValueError(f"Crosswalk Wikipedia page ID mismatch for {name!r}")
    unresolved: list[dict[str, object]] = []
    skipped_bouts: list[dict[str, object]] = []
    resolved_bouts: dict[int, list[tuple[ResultBout, str, str]]] = {}
    identities: dict[str, tuple[str, str, str | None]] = {}
    for _, event in page_payloads:
        event_id = f"wikipedia_research:{event.page_id}"
        resolved_bouts[event.page_id] = []
        for bout in event.bouts:
            fighter_ids: list[str | None] = []
            for fighter in (bout.fighter_a, bout.fighter_b):
                if fighter.title is not None and fighter.title in page_ids:
                    source_id = str(page_ids[fighter.title])
                    fighter_id = f"wikipedia:{source_id}"
                    identities.setdefault(fighter_id, (fighter.name, "wikipedia", source_id))
                    fighter_ids.append(fighter_id)
                elif fighter.title is None and fighter.name in crosswalk:
                    entry = crosswalk[fighter.name]
                    fighter_id = entry["fighter_id"].strip()
                    if fighter_id.startswith("wikipedia:"):
                        identities.setdefault(
                            fighter_id,
                            (entry["canonical_name"].strip(), "wikipedia", fighter_id.split(":", 1)[1]),
                        )
                    else:
                        identities.setdefault(
                            fighter_id, (entry["canonical_name"].strip(), "reviewed", None)
                        )
                    fighter_ids.append(fighter_id)
                else:
                    fighter_ids.append(None)
                    unresolved.append({
                        "event_id": event_id,
                        "event_title": event.title,
                        "bout_position": bout.position,
                        "fighter_name": fighter.name,
                        "linked_title": fighter.title,
                        "reason": "missing_page_id" if fighter.title else "unlinked_name",
                        "source_url": f"https://en.wikipedia.org/wiki/UFC_{event.title.split()[-1]}",
                    })
            if None in fighter_ids:
                skipped_bouts.append({
                    "event_id": event_id,
                    "bout_position": bout.position,
                    "fighter_a": bout.fighter_a.name,
                    "fighter_b": bout.fighter_b.name,
                    "outcome": bout.outcome,
                    "reason": "unresolved_fighter_identity",
                })
                continue
            fighter_a_id, fighter_b_id = fighter_ids
            if fighter_a_id == fighter_b_id:
                raise ValueError(f"{event.title} bout {bout.position}: both fighters resolve to one ID")
            resolved_bouts[event.page_id].append((bout, str(fighter_a_id), str(fighter_b_id)))

    review_path = Path(review_out)
    database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    if database_path and review_path.resolve() == database_path.resolve():
        raise ValueError("Review output must not replace the research database")
    _atomic_json(review_path, {
        "schema_version": 1,
        "source": "wikipedia_research",
        "event_range": [first_event, last_event],
        "unresolved_fighters": unresolved,
        "skipped_bouts": skipped_bouts,
        "crosswalk_format": {
            "schema_version": 1,
            "fighters": {
                "Example unlinked name": {
                    "fighter_id": "manual:reviewed-stable-id",
                    "canonical_name": "Example unlinked name",
                    "evidence_url": "https://example.org/identity-evidence",
                    "reviewed_by": "reviewer name",
                    "page_title": "Optional only when fighter_id is wikipedia:<verified page ID>",
                }
            },
        },
    })
    directory = Path(raw_dir)
    directory.mkdir(parents=True, exist_ok=True)
    imported = 0
    receipt_ids: list[int] = []
    for payload, event in page_payloads:
        event_id = f"wikipedia_research:{event.page_id}"
        incoming_bout_ids = {
            f"{event_id}:{':'.join(sorted((fighter_a_id, fighter_b_id)))}"
            for _, fighter_a_id, fighter_b_id in resolved_bouts[event.page_id]
        }
        existing_bout_ids = {
            str(row["bout_id"])
            for row in connection.execute(
                "SELECT bout_id FROM bouts WHERE event_id = ? AND source = 'wikipedia_research'",
                (event_id,),
            )
        }
        if existing_bout_ids - incoming_bout_ids:
            raise ValueError(
                f"{event.title}: a prior imported bout is now absent or unresolved; review revisions"
            )
        prior_event = connection.execute(
            "SELECT event_date FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if prior_event and prior_event["event_date"] != event.event_date:
            raise ValueError(f"{event.title}: source event date changed; review revisions")
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(snapshot).hexdigest()
        anticipated_path = directory.resolve() / f"{digest}.json"
        if database_path and anticipated_path == database_path.resolve():
            raise ValueError("Raw source snapshot must not replace the research database")
        prior_snapshots = connection.execute(
            """
            SELECT i.payload_path, i.sha256
            FROM wikipedia_source_receipts AS w
            JOIN ingestion_runs AS i USING (run_id)
            WHERE w.event_page_id = ? AND w.revision_id = ?
            """,
            (event.page_id, event.revision_id),
        ).fetchall()
        for prior in prior_snapshots:
            prior_path = Path(prior["payload_path"])
            if (prior["sha256"] != digest or prior_path.is_symlink()
                    or not prior_path.is_file() or prior_path.read_bytes() != snapshot):
                raise ValueError(f"Existing revision snapshot changed: {prior_path}")
        raw_path, _ = retain_snapshot(snapshot, directory, kind="Wikipedia revision")
        captured = utc_string(utc_now())
        with connection:
            db.upsert_event(
                connection, event_id, event.event_name, event.event_date,
                "completed", source="wikipedia_research", source_event_id=str(event.page_id),
            )
            for bout, fighter_a_id, fighter_b_id in resolved_bouts[event.page_id]:
                for fighter, fighter_id in ((bout.fighter_a, fighter_a_id), (bout.fighter_b, fighter_b_id)):
                    name, identity_source, source_id = identities[fighter_id]
                    existing = connection.execute(
                        "SELECT fighter_id FROM fighters WHERE fighter_id = ?", (fighter_id,)
                    ).fetchone()
                    if existing is None:
                        db.upsert_fighter(
                            connection, fighter_id, name,
                            "wikipedia" if identity_source == "wikipedia" else "wikipedia_review",
                            source_id,
                        )
                    if identity_source == "wikipedia":
                        db.link_external_fighter(connection, "wikipedia", str(source_id), fighter_id)
                source_bout_id = ":".join(sorted((fighter_a_id, fighter_b_id)))
                bout_id = f"{event_id}:{source_bout_id}"
                db.upsert_bout(
                    connection, bout_id, event_id, fighter_a_id, fighter_b_id,
                    "completed", weight_class=bout.weight_class,
                    source="wikipedia_research", source_bout_id=f"{event.page_id}:{source_bout_id}",
                )
                winner_id = fighter_a_id if bout.winner_side == "a" else None
                prior = connection.execute(
                    "SELECT outcome, winner_fighter_id, method FROM results WHERE bout_id = ?", (bout_id,)
                ).fetchone()
                if prior and (
                    prior["outcome"] != bout.outcome or prior["winner_fighter_id"] != winner_id
                    or prior["method"] != bout.method
                ):
                    raise ValueError(f"Result changed for {bout_id}; review the source revision before updating")
                db.upsert_result(connection, bout_id, bout.outcome, winner_id, captured, bout.method)
                imported += 1
            run = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) VALUES (?, ?, ?, ?)",
                ("wikipedia_research", captured, str(raw_path), digest),
            )
            run_id = int(run.lastrowid)
            connection.execute(
                """
                INSERT INTO wikipedia_source_receipts(
                    run_id, event_page_id, event_title, page_url, revision_id,
                    revision_timestamp_utc, license_title, license_url,
                    imported_bouts, skipped_unresolved_bouts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, event.page_id, event.title,
                    f"https://en.wikipedia.org/wiki/UFC_{event.title.split()[-1]}",
                    event.revision_id, event.revision_timestamp_utc,
                    event.license_title, event.license_url,
                    len(resolved_bouts[event.page_id]),
                    sum(1 for row in skipped_bouts if row["event_id"] == event_id),
                ),
            )
        receipt_ids.append(run_id)
    return {
        "source": "wikipedia_research",
        "research_only": True,
        "events": len(page_payloads),
        "source_bouts": sum(len(event.bouts) for _, event in page_payloads),
        "imported_bouts": imported,
        "skipped_unresolved_bouts": len(skipped_bouts),
        "review_out": str(review_path),
        "ingestion_run_ids": receipt_ids,
    }
