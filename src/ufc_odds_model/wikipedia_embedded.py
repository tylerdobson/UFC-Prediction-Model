"""Parse one explicitly named UFC card embedded in a Wikipedia summary page.

Some older event links lead to a season or year article containing several
cards. The caller must supply the exact section heading and catalog date; this
module never chooses a card from a page by approximate name or date alone.
"""

from __future__ import annotations

import html
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import date

from .wikipedia_history import (
    ResultBout,
    _BOUT_OPEN,
    _INFOBOX_OPEN,
    parse_event_page_title,
)
from .wikipedia_catalog import _event_date


_SECTION_LINK = re.compile(r"\[\[([^\[\]|#]+)#([^\[\]|]+)\|[^\[\]]+\]\]")

# These links currently redirect to multi-card summaries. The exact section
# heading/date pairs were checked against their canonical source pages.
_CANONICAL_TARGETS = {
    (2005, 64): ("The Ultimate Fighter 2", "The Ultimate Fighter 2 Finale", "2005-11-05", "The Ultimate Fighter 2 Finale"),
    (2005, 57): ("The Ultimate Fighter 1", "The Ultimate Fighter: Team Couture vs. Team Liddell Finale", "2005-04-09", "The Ultimate Fighter 1 Finale"),
    (2006, 80): ("The Ultimate Fighter 4", "The Ultimate Fighter 4 Finale", "2006-11-11", "The Ultimate Fighter 4 Finale"),
    (2006, 72): ("The Ultimate Fighter 3", "The Ultimate Fighter 3 Finale", "2006-06-24", "The Ultimate Fighter 3 Finale"),
    (2007, 101): ("The Ultimate Fighter: Team Hughes vs. Team Serra", "The Ultimate Fighter 6 Finale", "2007-12-08", "The Ultimate Fighter: Team Hughes vs. Team Serra Finale"),
    (2007, 93): ("The Ultimate Fighter 5", "The Ultimate Fighter 5 Finale", "2007-06-23", "The Ultimate Fighter 5 Finale"),
    (2007, 87): ("2007 in UFC", "UFC Fight Night: Stevenson vs. Guillard", "2007-04-05", "UFC Fight Night: Stevenson vs. Guillard"),
    (2008, 121): ("The Ultimate Fighter: Team Nogueira vs. Team Mir", "The Ultimate Fighter 8 Finale", "2008-12-13", "The Ultimate Fighter: Team Nogueira vs. Team Mir Finale"),
    (2008, 111): ("The Ultimate Fighter: Team Rampage vs. Team Forrest", "The Ultimate Fighter 7 Finale", "2008-06-21", "The Ultimate Fighter: Team Rampage vs. Team Forrest Finale"),
    (2009, 141): ("The Ultimate Fighter: Heavyweights", "The Ultimate Fighter 10 Finale", "2009-12-05", "The Ultimate Fighter: Heavyweights Finale"),
    (2009, 132): ("The Ultimate Fighter: United States vs. United Kingdom", "The Ultimate Fighter 9 Finale", "2009-06-20", "The Ultimate Fighter: United States vs. United Kingdom Finale"),
    (2010, 165): ("The Ultimate Fighter: Team GSP vs. Team Koscheck", "The Ultimate Fighter 12 Finale", "2010-12-04", "The Ultimate Fighter: Team GSP vs. Team Koscheck Finale"),
    (2010, 154): ("The Ultimate Fighter: Team Liddell vs. Team Ortiz", "The Ultimate Fighter 11 Finale", "2010-06-19", "The Ultimate Fighter: Team Liddell vs. Team Ortiz Finale"),
    (2011, 191): ("The Ultimate Fighter: Team Bisping vs. Team Miller", "The Ultimate Fighter 14 Finale", "2011-12-03", "The Ultimate Fighter: Team Bisping vs. Team Miller Finale"),
    (2011, 176): ("The Ultimate Fighter: Team Lesnar vs. Team dos Santos", "The Ultimate Fighter 13 Finale", "2011-06-04", "The Ultimate Fighter: Team Lesnar vs. Team dos Santos Finale"),
    (2012, 207): ("2012 in UFC", "UFC on FX: Johnson vs. McCall 2", "2012-06-08", "UFC on FX: Johnson vs. McCall"),
    (2012, 204): ("2012 in UFC", "UFC on Fuel TV: The Korean Zombie vs. Poirier", "2012-05-15", "UFC on Fuel TV: Korean Zombie vs. Poirier"),
    (2012, 198): ("2012 in UFC", "UFC on Fuel TV: Sanchez vs. Ellenberger", "2012-02-15", "UFC on Fuel TV: Sanchez vs. Ellenberger"),
    (2012, 196): ("2012 in UFC", "UFC on Fox: Evans vs. Davis", "2012-01-28", "UFC on Fox: Evans vs. Davis"),
    (2013, 234): ("2013 in UFC", "UFC on Fox: Henderson vs. Melendez", "2013-04-20", "UFC on Fox: Henderson vs. Melendez"),
    (2013, 225): ("2013 in UFC", "UFC on FX: Belfort vs. Bisping", "2013-01-19", "UFC on FX: Belfort vs. Bisping"),
}

# The historical anchor no longer equals the heading in the current revision.
# Each replacement is deliberately tied to the source article and event number.
_HEADING_OVERRIDES = {
    (2013, 238, "The Ultimate Fighter: Brazil 2", "Finale"):
        "The Ultimate Fighter: Brazil 2 Finale",
    (2018, 443, "The Ultimate Fighter: Undefeated", "The Ultimate Fighter 27"):
        "The Ultimate Fighter 27 Finale",
}

# This one inspected 2008 season revision places a Background heading between
# its sole finale infobox and the level-two Results table. The page ID, exact
# heading, and catalog date must all agree before crossing that boundary.
_BACKGROUND_BRIDGE = {(14625011, "the ultimate fighter 7 finale", "2008-06-21")}


@dataclass(frozen=True)
class EmbeddedReviewTarget:
    event_number: int
    source_page_title: str
    target_heading: str
    expected_date: str


def target_from_catalog_review(row: dict) -> EmbeddedReviewTarget:
    """Map a known catalog exception to one explicit section, never a fuzzy match."""
    if not isinstance(row, dict):
        raise ValueError("Catalog review row must be a dictionary")
    try:
        year = int(row["year"])
        raw_number = str(row["raw_number"])
        if not re.fullmatch(r"\d{1,4}", raw_number):
            raise ValueError("Review row is not a completed numbered event")
        event_number = int(raw_number)
        raw_date = str(row["raw_date"])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            expected_date = date.fromisoformat(raw_date).isoformat()
            if date.fromisoformat(expected_date).year != year:
                raise ValueError("Review date is outside its year")
        else:
            expected_date = _event_date(raw_date, year)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Review row has no verified event number and date") from error
    reason = row.get("reason")
    if reason == "canonical_not_event_page":
        target = _CANONICAL_TARGETS.get((year, event_number))
        if (target is None or target[2] != expected_date
                or target[3] != row.get("raw_event")):
            raise ValueError("Canonical redirect has no reviewed section mapping")
        return EmbeddedReviewTarget(event_number, target[0], target[1], expected_date)
    if reason == "Event link points to a section, not an event page":
        raw_event = row.get("raw_event")
        links = _SECTION_LINK.findall(raw_event) if isinstance(raw_event, str) else []
        if len(links) != 1:
            raise ValueError("Section link has no single explicit target")
        title, anchor = links[0]
        title = urllib.parse.unquote(title.replace("_", " ")).strip()
        heading = urllib.parse.unquote(anchor.replace("_", " ")).strip()
        if not title or not heading:
            raise ValueError("Section link has an empty target")
        heading = _HEADING_OVERRIDES.get((year, event_number, title, heading), heading)
        return EmbeddedReviewTarget(event_number, title, heading, expected_date)
    raise ValueError("Catalog review row is not an embedded event candidate")


@dataclass(frozen=True)
class EmbeddedEvent:
    source_page_id: int
    source_page_title: str
    section_key: str
    section_name: str
    event_id: str
    event_name: str
    event_date: str
    revision_id: int
    revision_timestamp_utc: str
    license_title: str
    license_url: str
    bouts: tuple[ResultBout, ...]


@dataclass(frozen=True)
class _Heading:
    level: int
    name: str
    start: int
    end: int


def normalize_section_key(value: str) -> str:
    """Use an exact, Unicode-aware MediaWiki heading key for event identity."""
    if not isinstance(value, str):
        raise ValueError("An explicit section heading is required")
    decoded = html.unescape(urllib.parse.unquote(value).replace("_", " "))
    key = " ".join(unicodedata.normalize("NFKC", decoded).split()).casefold()
    if not key or "#" in key or any(char in key for char in "\r\n"):
        raise ValueError("An explicit section heading is required")
    return key


def embedded_event_id(source_page_id: int, section_key: str) -> str:
    """Separate cards sharing one article without relying on the event label."""
    if not isinstance(source_page_id, int) or source_page_id <= 0:
        raise ValueError("A positive source page ID is required")
    key = normalize_section_key(section_key)
    return f"wikipedia_research:{source_page_id}:{urllib.parse.quote(key, safe='')}"


def _headings(source: str) -> list[_Heading]:
    headings: list[_Heading] = []
    for match in re.finditer(r"(?m)^([^\n]*)(?:\n|$)", source):
        line = match.group(1).strip()
        opening = re.match(r"^(={2,6})", line)
        closing = re.search(r"(={2,6})$", line)
        if not opening or not closing or len(opening.group(1)) != len(closing.group(1)):
            continue
        level = len(opening.group(1))
        name = line[level:-level].strip()
        if name and not name.startswith("=") and not name.endswith("="):
            headings.append(_Heading(level, name, match.start(), match.end()))
    return headings


def parse_embedded_event(
    payload: dict,
    *,
    source_page_title: str,
    target_heading: str,
    expected_date: str,
    expected_page_id: int | None = None,
) -> EmbeddedEvent:
    """Read one named card and its own Results subsection from a source page.

    ``target_heading`` is the level-two heading in the canonical source page,
    not the page title or a fuzzy display name. A redirect or an anchor that
    points to another heading must be reviewed and supplied explicitly.
    """
    if not isinstance(payload, dict) or payload.get("title") != source_page_title:
        raise ValueError(f"Expected source page {source_page_title!r}")
    page_id = payload.get("id")
    if not isinstance(page_id, int) or page_id <= 0:
        raise ValueError("Embedded event source is missing a stable page ID")
    if expected_page_id is not None and page_id != expected_page_id:
        raise ValueError("Embedded event source page ID differs from catalog")
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("Embedded event source is missing wikitext")
    try:
        date.fromisoformat(expected_date)
    except (TypeError, ValueError) as error:
        raise ValueError("Expected catalog event date must be ISO YYYY-MM-DD") from error
    key = normalize_section_key(target_heading)
    headings = _headings(source)
    candidates = [h for h in headings if h.level == 2 and normalize_section_key(h.name) == key]
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one event section {target_heading!r}; found {len(candidates)}")
    selected = candidates[0]
    following = next((h for h in headings if h.start > selected.start and h.level <= 2), None)
    section_end = following.start if following else len(source)
    section = source[selected.end:section_end]
    section_headings = _headings(section)
    result_headings = [
        h for h in section_headings
        if h.level == 3 and normalize_section_key(h.name) == "results"
    ]
    if (len(result_headings) == 1 and following is not None
            and normalize_section_key(following.name) == "results"):
        raise ValueError(f"{target_heading}: multiple Results section boundaries")
    if len(result_headings) == 1:
        results_heading = result_headings[0]
        following_result = next(
            (h for h in section_headings if h.start > results_heading.start and h.level <= 3),
            None,
        )
        results_end = following_result.start if following_result else len(section)
        before_results = section[:results_heading.start]
        results = section[results_heading.end:results_end]
        after_results = section[results_end:]
    elif (not result_headings and following is not None
          and normalize_section_key(following.name) == "results"):
        # One older season article puts Results at level two, directly after
        # the named card section. Stop at the next level-two heading.
        next_after_results = next(
            (h for h in headings if h.start > following.start and h.level <= 2),
            None,
        )
        before_results = section
        results = source[
            following.end:next_after_results.start if next_after_results else len(source)
        ]
        after_results = ""
    elif (not result_headings and following is not None
          and normalize_section_key(following.name) == "background"
          and (page_id, key, expected_date) in _BACKGROUND_BRIDGE):
        next_after_background = next(
            (h for h in headings if h.start > following.start and h.level <= 2), None,
        )
        if (next_after_background is None
                or normalize_section_key(next_after_background.name) != "results"):
            raise ValueError(f"{target_heading}: reviewed Background is not followed by Results")
        background = source[following.end:next_after_background.start]
        if _INFOBOX_OPEN.search(background) or _BOUT_OPEN.search(background):
            raise ValueError(f"{target_heading}: another event or bout appears before Results")
        next_after_results = next(
            (h for h in headings if h.start > next_after_background.start and h.level <= 2),
            None,
        )
        before_results = section + "\n" + background
        results = source[
            next_after_background.end:next_after_results.start if next_after_results else len(source)
        ]
        after_results = ""
    else:
        raise ValueError(f"{target_heading}: expected exactly one Results subsection")
    if len(list(_INFOBOX_OPEN.finditer(before_results))) != 1 or _INFOBOX_OPEN.search(results):
        raise ValueError(f"{target_heading}: expected exactly one event infobox")
    if _BOUT_OPEN.search(before_results) or _BOUT_OPEN.search(after_results):
        raise ValueError(f"{target_heading}: bout markup appears outside Results subsection")
    if not _BOUT_OPEN.search(results):
        raise ValueError(f"{target_heading}: Results subsection has no bout templates")
    # The existing checked positional bout parser handles linked identities,
    # draw/no-contest markers, and malformed templates. Give it only this card.
    bounded_source = before_results + "\n==Results==\n" + results
    parsed = parse_event_page_title(
        {**payload, "source": bounded_source}, source_page_title
    )
    if parsed.event_date != expected_date:
        raise ValueError(
            f"{target_heading}: event date differs from catalog: "
            f"{parsed.event_date} vs {expected_date}"
        )
    return EmbeddedEvent(
        source_page_id=page_id,
        source_page_title=source_page_title,
        section_key=key,
        section_name=selected.name,
        event_id=embedded_event_id(page_id, key),
        event_name=parsed.event_name,
        event_date=parsed.event_date,
        revision_id=parsed.revision_id,
        revision_timestamp_utc=parsed.revision_timestamp_utc,
        license_title=parsed.license_title,
        license_url=parsed.license_url,
        bouts=parsed.bouts,
    )
