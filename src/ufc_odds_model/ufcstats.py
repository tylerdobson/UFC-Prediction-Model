"""Read event and bout metadata from UFCStats' public HTML pages.

The parser is intentionally scoped to the site's event list and event-detail
tables. It raises ``UFCStatsParseError`` if their expected structure changes,
instead of recording incomplete fights as if they were valid observations.
UFCStats sometimes serves a JavaScript browser check to non-browser clients;
this module reports that condition and does not try to bypass it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


BASE_URL = "http://ufcstats.com"
COMPLETED_EVENTS_URL = f"{BASE_URL}/statistics/events/completed?page=all"
UPCOMING_EVENTS_URL = f"{BASE_URL}/statistics/events/upcoming?page=all"
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class UFCStatsParseError(ValueError):
    """The response did not contain the expected UFCStats data structure."""


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Element | str] = field(default_factory=list)

    def descendants(self, tag: str | None = None, class_name: str | None = None):
        for child in self.children:
            if not isinstance(child, _Element):
                continue
            if (tag is None or child.tag == tag) and (
                class_name is None or class_name in child.attrs.get("class", "").split()
            ):
                yield child
            yield from child.descendants(tag, class_name)

    def text(self) -> str:
        return "".join(
            child if isinstance(child, str) else child.text() for child in self.children
        )

    def direct_children(self, tag: str) -> list[_Element]:
        return [
            child for child in self.children
            if isinstance(child, _Element) and child.tag == tag
        ]


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Element(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(
            _Element(tag, {key: value or "" for key, value in attrs})
        )

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _tree(html: str) -> _Element:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _clean(value: str) -> str:
    return " ".join(value.split())


def _first(nodes):
    return next(iter(nodes), None)


def _source_identity(url: str, kind: str) -> tuple[str, str]:
    absolute = urljoin(BASE_URL, url)
    parsed = urlparse(absolute)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"ufcstats.com", "www.ufcstats.com"}
        or len(parts) != 2
        or parts[0] != f"{kind}-details"
        or len(parts[1]) != 16
        or any(character not in "0123456789abcdefABCDEF" for character in parts[1])
    ):
        raise UFCStatsParseError(f"Invalid UFCStats {kind} URL: {url!r}")
    return parts[1].lower(), f"{parsed.scheme}://{parsed.netloc}/{parts[0]}/{parts[1].lower()}"


def _fighter_identity(link: _Element) -> tuple[str, str]:
    source_id, _ = _source_identity(link.attrs.get("href", ""), "fighter")
    name = _clean(link.text())
    if not name:
        raise UFCStatsParseError(f"Fighter {source_id} has no name")
    return source_id, name


def _fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; UFC-odds-model/0.1)"})
    with urlopen(request, timeout=20) as response:
        html = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    if "Checking your browser" in html and "/__c" in html:
        raise UFCStatsParseError(
            "UFCStats served a JavaScript browser check; automated HTML ingestion is unavailable"
        )
    return html


def _parse_event_list(html: str) -> list[dict[str, str]]:
    root = _tree(html)
    table = _first(root.descendants("table", "b-statistics__table-events"))
    if table is None:
        raise UFCStatsParseError("UFCStats event table was not found")
    events: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in table.descendants("tr"):
        link = _first(
            node for node in row.descendants("a")
            if "/event-details/" in node.attrs.get("href", "")
        )
        if link is None:
            continue
        event_id, event_url = _source_identity(link.attrs["href"], "event")
        date_node = _first(row.descendants("span", "b-statistics__date"))
        name = _clean(link.text())
        if date_node is None or not name:
            raise UFCStatsParseError(f"Event {event_id} has no name or date")
        try:
            date = datetime.strptime(_clean(date_node.text()), "%B %d, %Y").date().isoformat()
        except ValueError as exc:
            raise UFCStatsParseError(f"Event {event_id} has an invalid date") from exc
        cells = row.direct_children("td")
        location = _clean(cells[1].text()) if len(cells) > 1 else ""
        if event_id in seen:
            raise UFCStatsParseError(f"Duplicate event ID {event_id}")
        seen.add(event_id)
        events.append({
            "source_event_id": event_id,
            "source_event_url": event_url,
            "name": name,
            "date": date,
            "location": location,
        })
    return events


def _parse_event_bouts(html: str, event_url: str) -> list[dict[str, str | None]]:
    event_id, _ = _source_identity(event_url, "event")
    root = _tree(html)
    if _first(root.descendants("span", "b-content__title-highlight")) is None:
        raise UFCStatsParseError(f"Event {event_id} title was not found")
    table = _first(root.descendants("table", "b-fight-details__table"))
    if table is None:
        raise UFCStatsParseError(f"Event {event_id} bout table was not found")
    bouts: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for row in table.descendants("tr"):
        source_url = row.attrs.get("data-link", "")
        if "/fight-details/" not in source_url:
            continue
        bout_id, bout_url = _source_identity(source_url, "fight")
        cells = row.direct_children("td")
        if len(cells) < 8:
            raise UFCStatsParseError(f"Bout {bout_id} has fewer than eight columns")
        fighter_links = [
            link for link in cells[1].descendants("a")
            if "/fighter-details/" in link.attrs.get("href", "")
        ]
        if len(fighter_links) != 2:
            raise UFCStatsParseError(f"Bout {bout_id} does not have two linked fighters")
        fighter_a_id, fighter_a_name = _fighter_identity(fighter_links[0])
        fighter_b_id, fighter_b_name = _fighter_identity(fighter_links[1])
        flags = [
            _clean(link.text()).lower() for link in cells[0].descendants("a", "b-flag")
        ]
        method = _clean(cells[7].text()) or None
        if not flags and not method:
            status, outcome, winner_name, winner_id = "scheduled", None, None, None
        elif flags.count("win") == 1 and all(flag in {"win", "loss"} for flag in flags):
            # UFCStats places the winner first when it emits a single WIN flag.
            winner_index = flags.index("win") if len(flags) == 2 else 0
            status, outcome = "completed", "win"
            winner_name = (fighter_a_name, fighter_b_name)[winner_index]
            winner_id = (fighter_a_id, fighter_b_id)[winner_index]
        elif flags and all(flag == "draw" for flag in flags):
            status, outcome, winner_name, winner_id = "completed", "draw", None, None
        elif flags and all(flag in {"nc", "no contest"} for flag in flags):
            status, outcome, winner_name, winner_id = "completed", "no_contest", None, None
        else:
            raise UFCStatsParseError(f"Bout {bout_id} has unrecognized result flags: {flags!r}")
        if bout_id in seen:
            raise UFCStatsParseError(f"Duplicate bout ID {bout_id}")
        seen.add(bout_id)
        bouts.append({
            "source_bout_id": bout_id,
            "source_bout_url": bout_url,
            "source_event_id": event_id,
            "fighter_a_name": fighter_a_name,
            "fighter_a_source_id": fighter_a_id,
            "fighter_b_name": fighter_b_name,
            "fighter_b_source_id": fighter_b_id,
            "winner_name": winner_name,
            "winner_source_id": winner_id,
            "weight_class": _clean(cells[6].text()) or None,
            "method": method,
            "status": status,
            "outcome": outcome,
        })
    return bouts


def fetch_completed_events() -> list[dict[str, str]]:
    """Return UFCStats completed event metadata, newest event first."""
    return _parse_event_list(_fetch_html(COMPLETED_EVENTS_URL))


def fetch_upcoming_events() -> list[dict[str, str]]:
    """Return UFCStats upcoming event metadata, newest event first."""
    return _parse_event_list(_fetch_html(UPCOMING_EVENTS_URL))


def fetch_event_bouts(event_url: str) -> list[dict[str, str | None]]:
    """Return linked fighter pairs and result metadata for one event."""
    _source_identity(event_url, "event")
    return _parse_event_bouts(_fetch_html(event_url), event_url)
