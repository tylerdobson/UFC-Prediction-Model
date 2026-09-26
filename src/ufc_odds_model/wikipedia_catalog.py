"""Research-only catalog of completed UFC events on Wikipedia year pages.

The catalog is a discovery aid, not a licensed pre-fight data feed. Each year
page and its revision/license metadata is returned so callers can retain the
original source and audit later edits. Ambiguous event rows go to ``review``.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Callable

from .wikipedia_history import ACTION_API, MediaWikiClient, REST_ROOT


DEFAULT_FIRST_YEAR = 2011
DEFAULT_LAST_YEAR = 2025
_EVENTS_HEADING = re.compile(r"^==\s*Events list\s*==\s*$", re.I | re.M)
_NEXT_HEADING = re.compile(r"^==[^=\n].*?==\s*$", re.M)
_TABLE_OPEN = re.compile(r'^\{\|[^\n]*\bwikitable\b[^\n]*$', re.I | re.M)
_TABLE_CLOSE = re.compile(r'^\|}\s*$', re.M)
_ROW = re.compile(r'^\|-.*$', re.M)
_DTS = re.compile(r'^\{\{\s*dts\s*\|\s*(\d{4})\s*\|\s*([^|}]+)\s*\|\s*(\d{1,2})\s*}}$', re.I)
_LINK = re.compile(r'^\[\[([^\[\]]+)\]\]$')
_NUMBER = re.compile(r'^\d{1,4}$')
_ATTR = re.compile(r'^(?:rowspan|colspan|style|class|align)\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^|\s]+)\s*\|\s*', re.I)
_MONTHS = {
    name.casefold(): index
    for index, names in enumerate((
        (), ("jan", "january"), ("feb", "february"), ("mar", "march"),
        ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
        ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
        ("nov", "november"), ("dec", "december"),
    ))
    for name in names
}


@dataclass(frozen=True)
class CatalogEvent:
    year: int
    event_number: int
    event_date: str
    display_name: str
    page_title: str
    page_id: int | None
    source_page_id: int
    source_revision_id: int


@dataclass(frozen=True)
class CatalogReview:
    year: int
    row_number: int
    reason: str
    raw_number: str
    raw_event: str
    raw_date: str


@dataclass(frozen=True)
class CatalogYear:
    year: int
    page_id: int
    revision_id: int
    revision_timestamp_utc: str
    license_title: str
    license_url: str
    source_url: str
    raw_payload: dict
    events: tuple[CatalogEvent, ...]
    review: tuple[CatalogReview, ...]


@dataclass(frozen=True)
class CatalogResult:
    events: tuple[CatalogEvent, ...]
    review: tuple[CatalogReview, ...]
    years: tuple[CatalogYear, ...]


def _events_section(source: str) -> str:
    heading = _EVENTS_HEADING.search(source)
    if heading is None:
        raise ValueError("Year page has no Events list section")
    remainder = source[heading.end():]
    next_heading = _NEXT_HEADING.search(remainder)
    return remainder[:next_heading.start()] if next_heading else remainder


def _event_table(section: str) -> str:
    for opening in _TABLE_OPEN.finditer(section):
        closing = _TABLE_CLOSE.search(section, opening.end())
        if closing is None:
            raise ValueError("Events list contains an unclosed wikitable")
        table = section[opening.end():closing.start()]
        first_row = _ROW.search(table)
        headers = table[:first_row.start()] if first_row else table
        if re.search(r'(?im)^!\s*[^\n]*\|\s*Event\s*$', headers) and re.search(
            r'(?im)^!\s*[^\n]*\|\s*Date\s*$', headers
        ):
            return table
    raise ValueError("Events list has no wikitable with Event and Date columns")


def _split_double_pipes(value: str) -> list[str]:
    """Split cells without splitting pipes inside links or templates."""
    cells: list[str] = []
    start = position = template_depth = link_depth = 0
    while position < len(value):
        pair = value[position:position + 2]
        if pair == "{{":
            template_depth += 1
            position += 2
        elif pair == "}}":
            template_depth -= 1
            position += 2
        elif pair == "[[":
            link_depth += 1
            position += 2
        elif pair == "]]":
            link_depth -= 1
            position += 2
        elif pair == "||" and not template_depth and not link_depth:
            cells.append(value[start:position].strip())
            position += 2
            start = position
        else:
            position += 1
    cells.append(value[start:].strip())
    return cells


def _leading_cells(row: str) -> tuple[str, str, str]:
    cells: list[str] = []
    for line in row.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and not stripped.startswith(("|-", "|}")):
            cells.extend(_split_double_pipes(stripped[1:]))
            if len(cells) >= 3:
                break
        elif cells and stripped and len(cells) < 3:
            cells[-1] += " " + stripped
    cells.extend([""] * (3 - len(cells)))
    return tuple(cells[:3])  # type: ignore[return-value]


def _cell_value(raw: str) -> str:
    value = raw.strip()
    while match := _ATTR.match(value):
        value = value[match.end():].strip()
    return value


def _split_template(value: str) -> list[str]:
    """Separate the top-level pipes in a single template."""
    if not value.startswith("{{") or not value.endswith("}}"):
        raise ValueError("Unexpected event template markup")
    body = value[2:-2]
    parts: list[str] = []
    start = position = template_depth = link_depth = 0
    while position < len(body):
        pair = body[position:position + 2]
        if pair == "{{":
            template_depth += 1
            position += 2
        elif pair == "}}":
            template_depth -= 1
            position += 2
        elif pair == "[[":
            link_depth += 1
            position += 2
        elif pair == "]]":
            link_depth -= 1
            position += 2
        elif body[position] == "|" and not template_depth and not link_depth:
            parts.append(body[start:position].strip())
            position += 1
            start = position
        else:
            position += 1
    if template_depth or link_depth:
        raise ValueError("Unbalanced event template")
    parts.append(body[start:].strip())
    return parts


def _event_link(raw: str) -> tuple[str, str]:
    value = _cell_value(raw)
    if value.startswith("{{"):
        parts = _split_template(value)
        if len(parts) != 3 or parts[0].casefold() != "sort":
            raise ValueError("Unsupported event template")
        value = parts[2]
    linked = _LINK.fullmatch(value)
    if linked is None:
        raise ValueError("Event cell is not one unambiguous wiki link")
    fields = linked.group(1).split("|", 1)
    target = fields[0].strip().replace("_", " ")
    display = (fields[1] if len(fields) == 2 else target).strip()
    if not target or not display or target.startswith(":"):
        raise ValueError("Event link lacks a usable title")
    if "#" in target:
        raise ValueError("Event link points to a section, not an event page")
    return target, display


def _event_date(raw: str, year: int) -> str:
    value = re.sub(r"<!--.*?-->", "", _cell_value(raw), flags=re.S).strip()
    markup = _DTS.fullmatch(value)
    if markup:
        event_year = int(markup.group(1))
        month_text = markup.group(2).strip().casefold()
        month = int(month_text) if month_text.isdigit() else _MONTHS.get(month_text)
        if month is None:
            raise ValueError("Unrecognized event month")
        event_date = date(event_year, month, int(markup.group(3)))
    else:
        event_date = None
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                event_date = datetime.strptime(value, fmt).date()
                break
            except ValueError:
                pass
        if event_date is None:
            raise ValueError("Unrecognized event date")
    if event_date.year != year:
        raise ValueError("Event date is outside its year page")
    return event_date.isoformat()


def parse_year_page(payload: dict, year: int, *, source_url: str | None = None) -> CatalogYear:
    """Read only the Events list wikitable from a REST page-source response."""
    expected_title = f"{year} in UFC"
    if not isinstance(payload, dict) or payload.get("title") != expected_title:
        raise ValueError(f"Expected {expected_title}, got {payload.get('title')!r}")
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
    if not license_url.startswith("https://creativecommons.org/licenses/by-sa/4.0/"):
        raise ValueError(f"{expected_title}: unexpected source license {license_url!r}")

    table = _event_table(_events_section(source))
    row_markers = list(_ROW.finditer(table))
    if not row_markers:
        raise ValueError(f"{expected_title}: no event rows")
    events: list[CatalogEvent] = []
    review: list[CatalogReview] = []
    for row_number, marker in enumerate(row_markers, start=1):
        end = row_markers[row_number].start() if row_number < len(row_markers) else len(table)
        raw_number, raw_event, raw_date = _leading_cells(table[marker.end():end])
        number_text = _cell_value(raw_number)
        # A second bonus-awards subrow has no event number and is not a card.
        # A canceled event row may have an em dash in place of that number;
        # preserve those as review records instead of silently dropping them.
        if not _NUMBER.fullmatch(number_text):
            try:
                _event_link(raw_event)
                _event_date(raw_date, year)
            except ValueError:
                continue
            review.append(CatalogReview(
                year, row_number, "non_numeric_event_sequence", raw_number, raw_event, raw_date,
            ))
            continue
        try:
            page_title, display_name = _event_link(raw_event)
            event_date = _event_date(raw_date, year)
        except ValueError as error:
            review.append(CatalogReview(year, row_number, str(error), raw_number, raw_event, raw_date))
            continue
        events.append(CatalogEvent(
            year, int(number_text), event_date, display_name, page_title, None,
            page_id, revision["id"],
        ))
    if not events:
        raise ValueError(f"{expected_title}: no unambiguous event rows")
    return CatalogYear(
        year, page_id, revision["id"], str(revision["timestamp"]),
        str(license_info.get("title") or ""), license_url,
        source_url or f"{REST_ROOT}/{year}_in_UFC", payload,
        tuple(events), tuple(review),
    )


def _canonical_pages(fetch_json: Callable[[str], dict], titles: set[str]) -> dict[str, tuple[str, int]]:
    """Resolve event links in batches; reject missing and disambiguation pages."""
    resolved: dict[str, tuple[str, int]] = {}
    ordered = sorted(titles)
    for offset in range(0, len(ordered), 50):
        batch = ordered[offset:offset + 50]
        query = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "redirects": "1", "titles": "|".join(batch), "maxlag": "5",
            "prop": "pageprops", "ppprop": "disambiguation",
        })
        response = fetch_json(f"{ACTION_API}?{query}")
        if not isinstance(response, dict) or response.get("error") or not isinstance(response.get("query"), dict):
            raise ValueError(f"MediaWiki title lookup failed: {response.get('error') if isinstance(response, dict) else response!r}")
        result = response["query"]
        aliases: dict[str, str] = {}
        for entry in result.get("normalized", []) + result.get("redirects", []):
            if isinstance(entry, dict) and isinstance(entry.get("from"), str) and isinstance(entry.get("to"), str):
                aliases[entry["from"].replace("_", " ")] = entry["to"].replace("_", " ")
        pages = {
            page["title"].replace("_", " "): (page["title"], page["pageid"])
            for page in result.get("pages", [])
            if isinstance(page, dict)
            and isinstance(page.get("title"), str)
            and isinstance(page.get("pageid"), int) and page["pageid"] > 0
            and page.get("ns") == 0 and "missing" not in page
            and "disambiguation" not in (page.get("pageprops") or {})
        }
        for title in batch:
            canonical = title.replace("_", " ")
            seen: set[str] = set()
            while canonical in aliases and canonical not in seen:
                seen.add(canonical)
                canonical = aliases[canonical]
            if canonical not in seen and canonical in pages:
                resolved[title] = pages[canonical]
    return resolved


def enumerate_event_catalog(
    first_year: int = DEFAULT_FIRST_YEAR,
    last_year: int = DEFAULT_LAST_YEAR,
    *,
    fetch_json: Callable[[str], dict] | None = None,
    resolve_titles: bool = True,
) -> CatalogResult:
    """Enumerate a bounded inclusive year range, with canonical page IDs by default.

    This only discovers event pages and dates. It does not validate bouts or
    settle bets, and source pages can change after a historical event.
    """
    if not isinstance(first_year, int) or not isinstance(last_year, int) or not (1993 <= first_year <= last_year <= date.today().year):
        raise ValueError("Year range must be inclusive, from 1993 through the current year")
    fetch = fetch_json or MediaWikiClient()
    years = [
        parse_year_page(
            fetch(f"{REST_ROOT}/{year}_in_UFC"), year,
            source_url=f"{REST_ROOT}/{year}_in_UFC",
        )
        for year in range(first_year, last_year + 1)
    ]
    title_map = _canonical_pages(fetch, {event.page_title for item in years for event in item.events}) if resolve_titles else {}
    year_page_ids = {item.page_id for item in years}
    all_events: list[CatalogEvent] = []
    all_review: list[CatalogReview] = []
    seen_pages: dict[object, CatalogEvent] = {}
    revised_years: list[CatalogYear] = []
    for item in years:
        kept: list[CatalogEvent] = []
        reviews = list(item.review)
        for event in item.events:
            if resolve_titles:
                page = title_map.get(event.page_title)
                if page is None:
                    reviews.append(CatalogReview(
                        item.year, 0, "unresolved_or_disambiguation_page", str(event.event_number),
                        event.page_title, event.event_date,
                    ))
                    continue
                if (page[1] in year_page_ids
                        or re.fullmatch(r"\d{4} in UFC", page[0])
                        or ("finale" in event.display_name.casefold()
                            and page[0].startswith("The Ultimate Fighter")
                            and "finale" not in page[0].casefold())):
                    reviews.append(CatalogReview(
                        item.year, 0, "canonical_not_event_page", str(event.event_number),
                        event.page_title, event.event_date,
                    ))
                    continue
                event = replace(event, page_title=page[0], page_id=page[1])
            key = event.page_id if resolve_titles else event.page_title.casefold()
            earlier = seen_pages.get(key)
            if earlier:
                reviews.append(CatalogReview(
                    item.year, 0, "duplicate_event_page", str(event.event_number),
                    event.page_title, event.event_date,
                ))
                continue
            seen_pages[key] = event
            kept.append(event)
            all_events.append(event)
        revised_years.append(replace(item, events=tuple(kept), review=tuple(reviews)))
        all_review.extend(reviews)
    return CatalogResult(tuple(all_events), tuple(all_review), tuple(revised_years))
