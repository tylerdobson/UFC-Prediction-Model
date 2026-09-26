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
from dataclasses import asdict, dataclass, is_dataclass
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


def _page_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
        title.replace(" ", "_"), safe=":_()"
    )


def _page_api_url(title: str) -> str:
    return REST_ROOT + "/" + urllib.parse.quote(title.replace(" ", "_"), safe=":_()")


_HEADING = re.compile(r"^==\s*Results\s*==\s*$", re.I | re.M)
_NEXT_HEADING = re.compile(r"^==[^=].*?==\s*$", re.M)
_BOUT_OPEN = re.compile(r"\{\{\s*MMAevent bout(?=\s|\|)", re.I)
_LINKED_FIGHTER = re.compile(
    r"^\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]"
    r"(?:\s*\((?:c|ic|UFC Champion|Pride Champion)\))?$", re.I
)
_PLAIN_FIGHTER = re.compile(r"^[^{}\[\]<>|]+$")
_INFOBOX_OPEN = re.compile(r"\{\{\s*Infobox MMA event(?=\s|\|)", re.I)
_START_DATE = re.compile(
    r"^\{\{\s*start date\s*\|\s*(\d{4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})(?:\s*\|[^{}]*)?\s*\}\}",
    re.I,
)


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
    research_event_id: str | None = None
    source_event_id: str | None = None
    source_url: str | None = None


def _research_event_id(event: ParsedEvent) -> str:
    return event.research_event_id or f"wikipedia_research:{event.page_id}"


def _source_event_id(event: ParsedEvent) -> str:
    return event.source_event_id or str(event.page_id)


def _event_source_url(event: ParsedEvent) -> str:
    return event.source_url or _page_url(event.title)


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
    name = re.sub(r"\s*\((?:c|ic|UFC Champion|Pride Champion)\)$", "", clean, flags=re.I).strip()
    if not name:
        raise ValueError(f"Empty fighter: {value!r}")
    return FighterRef(name, None)


def _event_date(raw: str) -> str:
    value = raw.strip()
    template = _START_DATE.match(value)
    if template:
        return date(*map(int, template.groups())).isoformat()
    # Older UFC pages use plain English dates in their event infobox.
    plain = re.match(r"^([A-Za-z]+\s+\d{1,2},\s*\d{4})\b", value)
    if plain:
        return datetime.strptime(plain.group(1), "%B %d, %Y").date().isoformat()
    day_first = re.match(r"^(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b", value)
    if day_first:
        return datetime.strptime(day_first.group(1), "%d %B %Y").date().isoformat()
    raise ValueError(f"Unsupported event date format: {raw!r}")


def _event_infobox_fields(source: str) -> dict[str, str]:
    """Read top-level fields even when several infobox fields share a line."""
    # Wikitext comments can contain unmatched wiki-link tokens. They are not
    # rendered fields and must not affect the template's structural parser.
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    match = _INFOBOX_OPEN.search(source)
    if match is None:
        return {}
    parts = _split_template(_template_text(source, match.start()))
    if parts[0].strip().casefold() != "infobox mma event":
        return {}
    fields: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        name = key.strip().casefold()
        if separator and name in ("name", "date"):
            if name in fields:
                raise ValueError(f"Duplicate {name} field in event infobox")
            fields[name] = value.strip()
    return fields


def parse_event_page_title(payload: dict, expected_title: str) -> ParsedEvent:
    """Fail closed on result markup outside the verified positional template."""
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
    fields = _event_infobox_fields(source)
    if not fields.get("name") or not fields.get("date"):
        raise ValueError(f"{expected_title}: missing event name or event date")
    try:
        event_date = _event_date(fields["date"])
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
        if marker in ("def.", "def") and not method_lower.startswith(("draw", "no contest", "nc")):
            outcome, winner_side = "win", "a"
        elif marker in ("vs.", "vs", "v.", "v") and method_lower.startswith("draw"):
            outcome, winner_side = "draw", None
        elif marker in ("vs.", "vs", "v.", "v", "nc") and method_lower.startswith(("no contest", "nc")):
            outcome, winner_side = "no_contest", None
        else:
            raise ValueError(
                f"{expected_title} bout {position}: ambiguous result marker {marker_raw!r} / {method!r}"
            )
        bouts.append(ResultBout(position, weight.strip(), first, second, outcome, winner_side, method.strip()))
    return ParsedEvent(
        page_id, title, fields["name"], event_date,
        revision["id"], str(revision["timestamp"]),
        str(license_info.get("title") or ""), license_url, tuple(bouts),
    )


def parse_event_page(payload: dict, expected_number: int) -> ParsedEvent:
    """Compatibility wrapper for the original bounded numbered-card importer."""
    return parse_event_page_title(payload, f"UFC {expected_number}")


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


def _record_action_lookups(
    connection: sqlite3.Connection,
    fetch_json: Callable[[str], dict],
    raw_dir: str | Path,
) -> Callable[[str], dict]:
    """Keep the exact title-to-page-ID responses used by an import.

    Page IDs are resolved through a mutable API. Retaining only event-page
    revisions would leave fighter identity and canonical event redirects
    impossible to audit after a later page move or merge.
    """
    directory = Path(raw_dir) / "action_api"

    def fetch_with_receipt(url: str) -> dict:
        payload = fetch_json(url)
        if url.startswith(ACTION_API + "?"):
            snapshot = json.dumps(
                {"request_url": url, "response": payload},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            raw_path, digest = retain_snapshot(
                snapshot, directory, kind="Wikipedia Action API lookup"
            )
            with connection:
                connection.execute(
                    "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                    "VALUES (?, ?, ?, ?)",
                    ("wikipedia_action_api", utc_string(utc_now()), str(raw_path), digest),
                )
        return payload

    return fetch_with_receipt


def _load_crosswalk(
    path: str | Path | None,
) -> tuple[dict[tuple[str, int, str], dict], bytes | None]:
    if path is None:
        return {}, None
    raw_bytes = Path(path).read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    version = payload.get("schema_version")
    if version == 1:
        raise ValueError("Global-name schema_version 1 crosswalk is unsafe; use bout-scoped schema_version 2 decisions")
    if version == 2 and isinstance(payload.get("decisions"), list):
        scoped = {}
        for entry in payload["decisions"]:
            if not isinstance(entry, dict):
                raise ValueError("Scoped crosswalk decisions must be objects")
            event_id = entry.get("event_id")
            position = entry.get("bout_position")
            name = entry.get("fighter_name")
            revision = entry.get("source_revision_id")
            digest = entry.get("source_receipt_sha256")
            if (not isinstance(event_id, str)
                    or re.fullmatch(r"wikipedia_research:\d+(?::.+)?", event_id) is None
                    or type(position) is not int or position <= 0
                    or not isinstance(name, str) or not name.strip()
                    or type(revision) is not int or revision <= 0
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                raise ValueError("Scoped crosswalk decision needs a bout and exact source revision/hash")
            key = (event_id, position, name)
            if key in scoped:
                raise ValueError(f"Duplicate scoped crosswalk decision: {key}")
            scoped[key] = entry
    else:
        raise ValueError("Crosswalk needs schema_version 2 decisions")
    for (_, _, name), entry in scoped.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            raise ValueError("Crosswalk fighter entries must be keyed by a non-empty name")
        required = ("fighter_id", "canonical_name", "reviewed_by")
        if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in required):
            raise ValueError(f"Crosswalk entry for {name!r} lacks reviewed identity evidence")
        evidence = entry.get("evidence_urls")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(url, str) or not url.startswith("https://") for url in evidence)):
            raise ValueError(f"Crosswalk evidence for {name!r} must use HTTPS URLs")
        if entry["fighter_id"].startswith("wikipedia:") and not (
            isinstance(entry.get("page_title"), str) and entry["page_title"].strip()
        ):
            raise ValueError(
                f"Crosswalk entry for {name!r} needs page_title to verify the Wikipedia page ID"
            )
    return scoped, raw_bytes


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
    page_payloads: list[tuple[dict, ParsedEvent]] = []
    for number in range(first_event, last_event + 1):
        payload = fetch(_page_api_url(f"UFC {number}"))
        page_payloads.append((payload, parse_event_page(payload, number)))
    return import_wikipedia_pages(
        connection, page_payloads, raw_dir=raw_dir, review_out=review_out,
        crosswalk_path=crosswalk_path, fetch_json=fetch,
        review_scope={"event_range": [first_event, last_event]},
    )


def import_wikipedia_pages(
    connection: sqlite3.Connection,
    page_payloads: list[tuple[dict, ParsedEvent]],
    *,
    raw_dir: str | Path = "data/raw/wikipedia",
    review_out: str | Path = "reports/wikipedia_identity_review.json",
    crosswalk_path: str | Path | None = None,
    fetch_json: Callable[[str], dict] | None = None,
    review_scope: dict[str, object] | None = None,
) -> dict[str, object]:
    """Import already checked event pages into the isolated research database."""
    fetch = fetch_json or MediaWikiClient()
    review_path = Path(review_out)
    database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    if database_path and review_path.resolve() == database_path.resolve():
        raise ValueError("Review output must not replace the research database")
    if crosswalk_path is not None and review_path.resolve() == Path(crosswalk_path).resolve():
        raise ValueError("Review output must not replace the identity crosswalk")
    scoped_crosswalk, crosswalk_bytes = _load_crosswalk(crosswalk_path)
    events_by_id = {_research_event_id(event): (payload, event) for payload, event in page_payloads}
    applicable_scoped = {
        key: entry for key, entry in scoped_crosswalk.items() if key[0] in events_by_id
    }
    for (event_id, position, name), entry in applicable_scoped.items():
        payload, event = events_by_id[event_id]
        source_sha = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        if (event.revision_id != entry["source_revision_id"]
                or source_sha != entry["source_receipt_sha256"]):
            raise ValueError(f"Scoped crosswalk source revision/hash mismatch for {event_id} bout {position}")
        matching_bout = next((bout for bout in event.bouts if bout.position == position), None)
        if (matching_bout is None or not any(
            fighter.name == name and fighter.title is None
            for fighter in (matching_bout.fighter_a, matching_bout.fighter_b)
        )):
            raise ValueError(f"Scoped crosswalk fighter/bout mismatch for {event_id} bout {position}")
    crosswalk_receipt: dict[str, object] | None = None
    if crosswalk_bytes is not None:
        raw_path, digest = retain_snapshot(
            crosswalk_bytes, Path(raw_dir) / "crosswalk", kind="Wikipedia identity crosswalk"
        )
        with connection:
            receipt = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("wikipedia_identity_crosswalk", utc_string(utc_now()), str(raw_path), digest),
            )
        crosswalk_receipt = {"ingestion_run_id": int(receipt.lastrowid), "sha256": digest}
    linked_titles = {
        fighter.title
        for _, event in page_payloads
        for bout in event.bouts
        for fighter in (bout.fighter_a, bout.fighter_b)
        if fighter.title is not None
    }
    linked_titles.update(
        entry["page_title"].strip()
        for entry in applicable_scoped.values()
        if entry["fighter_id"].startswith("wikipedia:")
    )
    page_ids = _resolve_page_ids(_record_action_lookups(connection, fetch, raw_dir), linked_titles)
    for (_, _, name), entry in applicable_scoped.items():
        if entry["fighter_id"].startswith("wikipedia:"):
            title = entry["page_title"].strip()
            actual_id = page_ids.get(title)
            if actual_id is None or entry["fighter_id"] != f"wikipedia:{actual_id}":
                raise ValueError(f"Crosswalk Wikipedia page ID mismatch for {name!r}")
    unresolved: list[dict[str, object]] = []
    skipped_bouts: list[dict[str, object]] = []
    resolved_bouts: dict[str, list[tuple[ResultBout, str, str]]] = {}
    identities: dict[str, tuple[str, str, str | None]] = {}
    for _, event in page_payloads:
        event_id = _research_event_id(event)
        if event_id in resolved_bouts:
            raise ValueError(f"Duplicate research event ID in one import: {event_id}")
        resolved_bouts[event_id] = []
        for bout in event.bouts:
            fighter_ids: list[str | None] = []
            for fighter in (bout.fighter_a, bout.fighter_b):
                if fighter.title is not None and fighter.title in page_ids:
                    source_id = str(page_ids[fighter.title])
                    fighter_id = f"wikipedia:{source_id}"
                    identities.setdefault(fighter_id, (fighter.name, "wikipedia", source_id))
                    fighter_ids.append(fighter_id)
                elif fighter.title is None and (
                    event_id, bout.position, fighter.name
                ) in applicable_scoped:
                    entry = applicable_scoped[(event_id, bout.position, fighter.name)]
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
                        "source_url": _event_source_url(event),
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
            resolved_bouts[event_id].append((bout, str(fighter_a_id), str(fighter_b_id)))

    review_document = {
        "schema_version": 1,
        "source": "wikipedia_research",
        **(review_scope or {}),
        "unresolved_fighters": unresolved,
        "skipped_bouts": skipped_bouts,
        "crosswalk_receipt": crosswalk_receipt,
        "crosswalk_format": {
            "schema_version": 2,
            "decisions": [{
                "event_id": "wikipedia_research:<page ID>",
                "bout_position": 1,
                "fighter_name": "Exact unlinked printed name",
                "source_revision_id": 123,
                "source_receipt_sha256": "<exact saved event payload SHA-256>",
                "fighter_id": "wikipedia:<verified fighter page ID>",
                "canonical_name": "Reviewed fighter name",
                "page_title": "Fighter page title when using wikipedia:<ID>",
                "evidence_urls": ["https://example.org/identity-evidence"],
                "reviewed_by": "reviewer name",
            }],
        },
    }
    directory = Path(raw_dir)
    directory.mkdir(parents=True, exist_ok=True)
    imported = 0
    receipt_ids: list[int] = []
    for payload, event in page_payloads:
        event_id = _research_event_id(event)
        source_event_id = _source_event_id(event)
        incoming_bout_ids = {
            f"{event_id}:{':'.join(sorted((fighter_a_id, fighter_b_id)))}"
            for _, fighter_a_id, fighter_b_id in resolved_bouts[event_id]
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
                "completed", source="wikipedia_research", source_event_id=source_event_id,
            )
            for bout, fighter_a_id, fighter_b_id in resolved_bouts[event_id]:
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
                    source="wikipedia_research", source_bout_id=f"{source_event_id}:{source_bout_id}",
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
                    imported_bouts, skipped_unresolved_bouts, crosswalk_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, event.page_id, event.title,
                    _event_source_url(event),
                    event.revision_id, event.revision_timestamp_utc,
                    event.license_title, event.license_url,
                    len(resolved_bouts[event_id]),
                    sum(1 for row in skipped_bouts if row["event_id"] == event_id),
                    crosswalk_receipt["ingestion_run_id"] if crosswalk_receipt else None,
                ),
            )
        receipt_ids.append(run_id)
    _atomic_json(review_path, review_document)
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


def import_wikipedia_years_history(
    connection: sqlite3.Connection,
    first_year: int = 2011,
    last_year: int = 2025,
    *,
    raw_dir: str | Path = "data/raw/wikipedia",
    review_out: str | Path = "reports/wikipedia_years_identity_review.json",
    crosswalk_path: str | Path | None = None,
    fetch_json: Callable[[str], dict] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Load broad, completed UFC outcome history into a research-only database.

    The year catalog and every accepted event page retain separate immutable
    payload receipts. An event with a failed fetch, ambiguous result table,
    mismatched page ID, or mismatched date is skipped and counted in the review
    file. Bouts with unresolved fighter identities are likewise excluded.
    """
    if (isinstance(first_year, bool) or isinstance(last_year, bool)
            or not isinstance(first_year, int) or not isinstance(last_year, int)
            or not 1993 <= first_year <= last_year <= date.today().year):
        raise ValueError("Year range must be 1993 through the current year")
    from .wikipedia_catalog import enumerate_event_catalog

    fetch = fetch_json or MediaWikiClient()
    catalog = enumerate_event_catalog(
        first_year, last_year,
        fetch_json=_record_action_lookups(connection, fetch, raw_dir),
        resolve_titles=True,
    )
    if progress:
        progress(f"Discovered {len(catalog.events)} candidate completed events")
    directory = Path(raw_dir)
    catalog_directory = directory / "catalog"
    catalog_directory.mkdir(parents=True, exist_ok=True)
    year_receipts = []
    for year in catalog.years:
        if "creativecommons.org/licenses/by-sa/4.0" not in year.license_url:
            raise ValueError(f"{year.year}: unexpected catalog license {year.license_url!r}")
        snapshot = json.dumps(
            year.raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        raw_path, digest = retain_snapshot(snapshot, catalog_directory, kind="Wikipedia catalog")
        captured = utc_string(utc_now())
        with connection:
            receipt = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("wikipedia_catalog", captured, str(raw_path), digest),
            )
        year_receipts.append({
            "year": year.year, "page_id": year.page_id,
            "revision_id": year.revision_id,
            "revision_timestamp_utc": year.revision_timestamp_utc,
            "license_title": year.license_title,
            "license_url": year.license_url,
            "source_url": year.source_url,
            "ingestion_run_id": int(receipt.lastrowid),
            "sha256": digest,
            "event_count": len(year.events),
        })
    parsed_pages: list[tuple[dict, ParsedEvent]] = []
    failed_events: list[dict[str, object]] = []
    now_date = date.today()
    for index, entry in enumerate(catalog.events, start=1):
        source_url = _page_url(entry.page_title)
        prior_import_present = connection.execute(
            "SELECT 1 FROM events WHERE event_id = ? AND source = 'wikipedia_research'",
            (f"wikipedia_research:{entry.page_id}",),
        ).fetchone() is not None
        if entry.event_date >= now_date.isoformat():
            failed_events.append({"page_title": entry.page_title, "source_url": source_url,
                                  "reason": "not_completed_by_date",
                                  "prior_import_present": prior_import_present})
            continue
        try:
            payload = fetch(_page_api_url(entry.page_title))
            parsed = parse_event_page_title(payload, entry.page_title)
            if parsed.page_id != entry.page_id:
                raise ValueError("page ID differs from the catalog")
            if parsed.event_date != entry.event_date:
                raise ValueError(
                    f"event date differs from the catalog: {parsed.event_date} vs {entry.event_date}"
                )
            parsed_pages.append((payload, parsed))
        except (RuntimeError, ValueError, urllib.error.HTTPError) as exc:
            failed_events.append({"page_title": entry.page_title, "source_url": source_url,
                                  "catalog_event_date": entry.event_date,
                                  "reason": type(exc).__name__, "detail": str(exc)[:300],
                                  "prior_import_present": prior_import_present})
        if progress and (index % 25 == 0 or index == len(catalog.events)):
            progress(
                f"Fetched {index}/{len(catalog.events)} candidate pages; "
                f"parsed {len(parsed_pages)}, skipped {len(failed_events)}"
            )
    if not parsed_pages:
        raise ValueError("No catalog events produced a checked results page")
    catalog_review = [
        asdict(item) if is_dataclass(item) else dict(item)
        for item in catalog.review
    ]
    imported = import_wikipedia_pages(
        connection, parsed_pages, raw_dir=raw_dir, review_out=review_out,
        crosswalk_path=crosswalk_path, fetch_json=fetch,
        review_scope={
            "year_range": [first_year, last_year],
            "catalog_events": len(catalog.events),
            "catalog_review": catalog_review,
            "failed_event_pages": failed_events,
            "catalog_year_receipts": year_receipts,
        },
    )
    return {
        **imported,
        "year_range": [first_year, last_year],
        "catalog_events": len(catalog.events),
        "parsed_event_pages": len(parsed_pages),
        "failed_event_pages": len(failed_events),
        "preserved_prior_event_pages": sum(
            bool(item["prior_import_present"]) for item in failed_events
        ),
        "catalog_review_rows": len(catalog_review),
        "catalog_year_receipts": len(year_receipts),
    }


def import_wikipedia_embedded_history(
    connection: sqlite3.Connection,
    first_year: int = 2011,
    last_year: int = 2025,
    *,
    raw_dir: str | Path = "data/raw/wikipedia",
    review_out: str | Path = "reports/wikipedia_embedded_review.json",
    crosswalk_path: str | Path | None = None,
    fetch_json: Callable[[str], dict] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Import checked UFC cards embedded in year or TV-season articles.

    Each event has a reviewed catalog row, an exact level-two section, a
    matching event date, and a unique page-ID-plus-section event ID. Canceled
    catalog rows and uncertain sections never enter the result database.
    """
    if (isinstance(first_year, bool) or isinstance(last_year, bool)
            or not isinstance(first_year, int) or not isinstance(last_year, int)
            or not 1993 <= first_year <= last_year <= date.today().year):
        raise ValueError("Year range must be 1993 through the current year")
    from .wikipedia_catalog import _canonical_pages, enumerate_event_catalog
    from .wikipedia_embedded import parse_embedded_event, target_from_catalog_review

    fetch = fetch_json or MediaWikiClient()
    catalog = enumerate_event_catalog(
        first_year, last_year,
        fetch_json=_record_action_lookups(connection, fetch, raw_dir),
    )
    candidate_reasons = {
        "Event link points to a section, not an event page",
        "canonical_not_event_page",
    }
    targets = []
    failed: list[dict[str, object]] = []
    for row in catalog.review:
        if row.reason not in candidate_reasons:
            continue
        original = asdict(row)
        try:
            targets.append((original, target_from_catalog_review(original)))
        except ValueError as error:
            failed.append({"catalog_review": original, "reason": str(error)[:300]})
    if not targets:
        raise ValueError("No reviewed embedded event candidates in the year range")
    sources = _canonical_pages(
        _record_action_lookups(connection, fetch, raw_dir),
        {target.source_page_title for _, target in targets},
    )
    page_cache: dict[str, dict] = {}
    parsed_pages: list[tuple[dict, ParsedEvent]] = []
    seen_event_ids: set[str] = set()
    for index, (original, target) in enumerate(targets, start=1):
        source = sources.get(target.source_page_title)
        if source is None:
            failed.append({"catalog_review": original, "reason": "unresolved_source_page"})
            continue
        canonical_title, source_page_id = source
        try:
            if canonical_title not in page_cache:
                page_cache[canonical_title] = fetch(_page_api_url(canonical_title))
            payload = page_cache[canonical_title]
            embedded = parse_embedded_event(
                payload,
                source_page_title=canonical_title,
                target_heading=target.target_heading,
                expected_date=target.expected_date,
                expected_page_id=source_page_id,
            )
            if embedded.event_id in seen_event_ids:
                raise ValueError("duplicate embedded event ID")
            seen_event_ids.add(embedded.event_id)
            section_url = (_page_url(canonical_title) + "#" + urllib.parse.quote(
                embedded.section_name.replace(" ", "_"), safe=":_()"
            ))
            parsed_pages.append((payload, ParsedEvent(
                page_id=embedded.source_page_id,
                title=embedded.source_page_title,
                event_name=embedded.event_name,
                event_date=embedded.event_date,
                revision_id=embedded.revision_id,
                revision_timestamp_utc=embedded.revision_timestamp_utc,
                license_title=embedded.license_title,
                license_url=embedded.license_url,
                bouts=embedded.bouts,
                research_event_id=embedded.event_id,
                source_event_id=embedded.event_id.removeprefix("wikipedia_research:"),
                source_url=section_url,
            )))
        except (RuntimeError, ValueError, urllib.error.HTTPError) as error:
            failed.append({"catalog_review": original, "source_page": canonical_title,
                           "reason": type(error).__name__, "detail": str(error)[:300]})
        if progress and (index % 10 == 0 or index == len(targets)):
            progress(
                f"Checked {index}/{len(targets)} embedded cards; "
                f"parsed {len(parsed_pages)}, skipped {len(failed)}"
            )
    if not parsed_pages:
        raise ValueError("No reviewed embedded cards produced checked results")
    # Retain the exact catalog pages that justified the section/date mapping.
    catalog_directory = Path(raw_dir) / "catalog"
    catalog_directory.mkdir(parents=True, exist_ok=True)
    year_receipts: list[dict[str, object]] = []
    for year in catalog.years:
        snapshot = json.dumps(
            year.raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        raw_path, digest = retain_snapshot(snapshot, catalog_directory, kind="Wikipedia catalog")
        with connection:
            receipt = connection.execute(
                "INSERT INTO ingestion_runs(source, fetched_at_utc, payload_path, sha256) "
                "VALUES (?, ?, ?, ?)",
                ("wikipedia_catalog", utc_string(utc_now()), str(raw_path), digest),
            )
        year_receipts.append({
            "year": year.year, "page_id": year.page_id,
            "revision_id": year.revision_id, "license_url": year.license_url,
            "ingestion_run_id": int(receipt.lastrowid), "sha256": digest,
        })
    imported = import_wikipedia_pages(
        connection, parsed_pages, raw_dir=raw_dir, review_out=review_out,
        crosswalk_path=crosswalk_path, fetch_json=fetch,
        review_scope={
            "year_range": [first_year, last_year],
            "embedded_candidates": len(targets),
            "failed_embedded_cards": failed,
            "catalog_year_receipts": year_receipts,
        },
    )
    return {
        **imported,
        "year_range": [first_year, last_year],
        "embedded_candidates": len(targets),
        "parsed_embedded_cards": len(parsed_pages),
        "failed_embedded_cards": len(failed),
        "catalog_year_receipts": len(year_receipts),
    }
