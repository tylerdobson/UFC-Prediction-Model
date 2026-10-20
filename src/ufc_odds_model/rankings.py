"""Official current UFC rankings snapshots; never historical model features.

The publisher's media-panel and Meta rankings are separate ranking systems.
Only divisions and positions actually present in the captured page are emitted.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

SOURCE_URL = "https://www.ufc.com/rankings"
DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[2] / "assets/data/ufc-rankings.json"
MAX_PAGE_BYTES = 5 * 1024 * 1024
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SET_LABELS = {"media": "UFC media-panel rankings", "meta": "UFC Meta rankings"}


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Rankings timestamps require a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(document):
    body = {k: v for k, v in document.items() if k != "snapshot_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _source_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"ufc.com", "www.ufc.com"}
            or parsed.path.rstrip("/") != "/rankings" or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ValueError("Rankings require the official UFC HTTPS source")
    return url


def _athlete_url(url):
    absolute = urljoin(SOURCE_URL, url)
    parsed = urlsplit(absolute)
    if (parsed.scheme != "https" or parsed.hostname not in {"ufc.com", "www.ufc.com"}
            or not re.fullmatch(r"/athlete/[A-Za-z0-9_-]+/?", parsed.path)
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
        raise ValueError("Invalid official athlete link in rankings")
    return absolute


class _Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def has_class(self, value):
        return value in self.attrs.get("class", "").split()

    def text(self):
        return " ".join(" ".join(c if isinstance(c, str) else c.text() for c in self.children).split())

    def find(self, predicate):
        for child in self.children:
            if isinstance(child, _Node):
                if predicate(child):
                    yield child
                yield from child.find(predicate)


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _one(nodes, description, *, required=True):
    values = list(nodes)
    if len(values) > 1 or (required and not values):
        raise ValueError("Unexpected rankings structure: " + description)
    return values[0] if values else None


def _athlete(node):
    anchor = _one(node.find(lambda n: n.tag == "a"), "athlete link", required=False)
    name = anchor.text() if anchor else node.text()
    if not name:
        raise ValueError("Ranked athlete name is empty")
    return {"name": name, "athlete_url": _athlete_url(anchor.attrs.get("href", "")) if anchor else None}


def _rank(text):
    match = re.fullmatch(r"(?:T\s*[-=]?\s*)?(\d{1,2})(?:\s*(?:=|\(?tie\)?|\(?tied\)?))?", text, re.I)
    if not match or not 1 <= int(match[1]) <= 15:
        raise ValueError("Invalid published top-15 rank")
    return int(match[1]), bool(re.search(r"[A-Za-z=]", text))


def _division_id(name):
    clean = re.sub(r"\s+Top Rank$", "", name, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", clean.lower().replace("'", "")).strip("-")


def _update_date(label):
    if not label:
        return None
    value = re.sub(r"^Last\s+updated:\s*", "", label, flags=re.I)
    for pattern in ("%Y-%m-%d", "%A, %b. %d, %Y", "%A, %b %d, %Y", "%A, %B %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def _check_division(division):
    if not division["name"] or division["kind"] not in {"weight_class", "pound_for_pound"}:
        raise ValueError("Invalid published rankings division")
    rows, champions, vacancies = division["contenders"], division["champions"], division["vacancies"]
    ranks = [row["rank"] for row in rows]
    if any(type(rank) is not int or not 1 <= rank <= 15 for rank in ranks) or ranks != sorted(ranks):
        raise ValueError("Published ranks are outside top 15 or out of order")
    counts = Counter(ranks)
    occupied = set()
    for rank, count in sorted(counts.items()):
        if rank in occupied:
            raise ValueError("Duplicate ranks conflict with published tie positions")
        occupied.update(range(rank, rank + count))
    seen = set()
    for row in [*champions, *rows]:
        name = row.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Missing ranked athlete")
        key = row.get("athlete_url") or name.casefold()
        if key in seen:
            raise ValueError("Duplicate athlete in a rankings division")
        seen.add(key)
        if row.get("athlete_url") is not None:
            _athlete_url(row["athlete_url"])
    roles = [row["status"] for row in champions]
    if any(role not in {"champion", "interim_champion"} for role in roles) or len(set(roles)) != len(roles):
        raise ValueError("Conflicting championship labels")
    if division["kind"] == "pound_for_pound" and (champions or division["championship_status"] != "not_applicable"):
        raise ValueError("Pound-for-pound leaders are not division champions")
    expected_status = ("champion_and_interim" if len(roles) == 2 else roles[0] if roles else None)
    if expected_status and division["championship_status"] not in {expected_status, "vacant_with_interim_champion"}:
        raise ValueError("Championship status contradicts published holders")
    if division["championship_status"] == "vacant_with_interim_champion" and roles != ["interim_champion"]:
        raise ValueError("A vacant championship cannot also have a full champion")
    vacancy_ranks = [row["rank"] for row in vacancies]
    if (len(set(vacancy_ranks)) != len(vacancy_ranks)
            or any(type(rank) is not int or not 1 <= rank <= 15 or rank in occupied for rank in vacancy_ranks)):
        raise ValueError("Conflicting or invalid published vacancy")
    for row in rows:
        if type(row["tied"]) is not bool or type(row["source_tie_marker"]) is not bool:
            raise ValueError("Rankings tie flags must be boolean")
        if row["tied"] != (counts[row["rank"]] > 1 or row["source_tie_marker"]):
            raise ValueError("Published ties were not preserved")
        if row["tie_size"] != counts[row["rank"]]:
            raise ValueError("Published tie size changed")
    missing = sorted(set(range(1, 16)) - set(ranks))
    tie_skips = occupied - set(ranks)
    reasons = {str(rank): ("published_tie_skips_rank" if rank in tie_skips else
                          "explicit_published_vacancy" if rank in vacancy_ranks else "rank_not_published_by_source")
               for rank in missing}
    return missing, reasons


def _parse_division(group):
    heading = _one(group.find(lambda n: n.has_class("view-grouping-header")), "division heading").text()
    if not heading:
        raise ValueError("Published rankings division has no name")
    is_p4p = "pound-for-pound" in heading.lower()
    division = {"id": _division_id(heading), "name": re.sub(r"\s+Top Rank$", "", heading, flags=re.I),
                "source_heading": heading, "kind": "pound_for_pound" if is_p4p else "weight_class",
                "champions": [], "leader": None, "championship_status": "not_applicable" if is_p4p else "not_published",
                "contenders": [], "vacancies": [], "empty_reason": None, "data_quality_notes": []}
    caption = _one(group.find(lambda n: n.tag == "caption"), "division caption", required=False)
    if caption:
        for info in caption.find(lambda n: n.has_class("info")):
            name_node = _one(info.find(lambda n: n.tag == "h5"), "title athlete", required=False)
            if name_node is None:
                continue
            label_node = _one(info.find(lambda n: n.tag == "h6"), "title label", required=False)
            label = label_node.text() if label_node else ""
            if "vacant" in name_node.text().lower() or "vacant" in label.lower():
                division["championship_status"] = "vacant"
                division["data_quality_notes"].append("The publisher explicitly lists the championship as vacant.")
                continue
            athlete = _athlete(name_node)
            if is_p4p:
                division["leader"] = athlete
                continue
            if "champion" not in label.lower():
                raise ValueError("Division title athlete lacks an explicit championship label")
            status = "interim_champion" if "interim" in label.lower() else "champion"
            athlete.update(status=status, source_status_label=label)
            division["champions"].append(athlete)
        image_alts = [n.attrs.get("alt", "") for n in caption.find(lambda n: n.tag == "img")]
        if (any("interim" in alt.lower() for alt in image_alts)
                and any(c["status"] == "champion" for c in division["champions"])
                and not any(c["status"] == "interim_champion" for c in division["champions"])):
            division["data_quality_notes"].append("The title panel labels Champion while portrait alt text says interim; the explicit title-panel label is retained.")
        if division["champions"]:
            title_vacant = division["championship_status"] == "vacant"
            if title_vacant and any(c["status"] == "champion" for c in division["champions"]):
                raise ValueError("Championship is both occupied and vacant")
            division["championship_status"] = ("vacant_with_interim_champion" if title_vacant else
                                                "champion_and_interim" if len(division["champions"]) == 2 else
                                                division["champions"][0]["status"])
    for row in group.find(lambda n: n.tag == "tr"):
        rank_node = _one(row.find(lambda n: n.has_class("views-field-weight-class-rank") or n.has_class("views-field-meta-weight-class-rank")), "rank cell")
        name_node = _one(row.find(lambda n: n.has_class("views-field-title")), "ranked athlete cell")
        rank, marked_tie = _rank(rank_node.text())
        if name_node.text().casefold() in {"vacant", "unassigned"}:
            division["vacancies"].append({"rank": rank, "source_label": name_node.text()})
            continue
        change = _one(row.find(lambda n: n.has_class("views-field-weight-class-rank-change") or n.has_class("views-field-meta-weight-class-rank-change")), "rank change", required=False)
        division["contenders"].append({**_athlete(name_node), "rank": rank, "source_rank_label": rank_node.text(),
                                       "source_tie_marker": marked_tie, "tied": False, "tie_size": 1,
                                       "rank_change_label": change.text() if change else None})
    counts = Counter(row["rank"] for row in division["contenders"])
    for row in division["contenders"]:
        row.update(tied=counts[row["rank"]] > 1 or row["source_tie_marker"], tie_size=counts[row["rank"]])
    missing, reasons = _check_division(division)
    division.update(missing_ranks=missing, missing_rank_reasons=reasons,
                    published_contender_count=len(division["contenders"]),
                    unpublished_rank_slots=[rank for rank in missing if reasons[str(rank)] == "rank_not_published_by_source"])
    if not division["contenders"]:
        division["empty_reason"] = "The official source publishes no ranked contenders for this division."
    if not is_p4p and not division["champions"] and division["championship_status"] == "not_published":
        division["data_quality_notes"].append("The official source does not publish a championship holder in this division's title panel.")
    return division


def parse_rankings(html, *, fetched_at_utc, source_url=SOURCE_URL):
    """Normalize actual captured HTML, keeping both publisher ranking systems."""
    raw = html.encode("utf-8") if isinstance(html, str) else bytes(html)
    if not raw or len(raw) > MAX_PAGE_BYTES:
        raise ValueError("Rankings HTML is empty or too large")
    tree = _Tree(); tree.feed(raw.decode("utf-8-sig")); tree.close()
    updated = {}
    visible = []
    for node in tree.root.find(lambda n: n.has_class("list-denotions__updated")):
        key = node.attrs.get("data-rankings-footer")
        if key in _SET_LABELS:
            if key in updated:
                raise ValueError("Duplicate rankings update label")
            updated[key] = node.text()
            if "hidden" not in node.attrs and not node.has_class("is-hidden"):
                visible.append(key)
    ranking_sets = []
    for view in tree.root.find(lambda n: n.has_class("view-athlete-rankings")):
        key = "meta" if view.has_class("view-display-id-meta_rankings") else "media" if view.has_class("view-display-id-block_1") else None
        if key is None or any(item["id"] == key for item in ranking_sets):
            raise ValueError("Unknown or duplicate official ranking system")
        divisions = [_parse_division(g) for g in view.find(lambda n: n.has_class("view-grouping"))]
        if not divisions:
            raise ValueError("Official ranking system contains no published divisions")
        ranking_sets.append({"id": key, "label": _SET_LABELS[key], "source_updated_label": updated.get(key),
                             "source_updated_date": _update_date(updated.get(key)),
                             "source_updated_date_note": "Publisher update label is retained verbatim; no missing calendar year is inferred.",
                             "divisions": divisions})
    if not ranking_sets or not any(d["contenders"] for s in ranking_sets for d in s["divisions"]):
        raise ValueError("No usable official rankings found; refusing an empty or challenge response")
    keys = {item["id"] for item in ranking_sets}
    default = visible[0] if len(visible) == 1 and visible[0] in keys else "meta" if "meta" in keys else "media"
    document = {"schema_version": 1, "artifact_type": "official_current_ufc_rankings", "default_ranking_set": default,
                "default_selection_basis": "official_page_visible_update_footer" if len(visible) == 1 else "meta_if_published_else_media",
                "source": {"url": _source_url(source_url), "fetched_at_utc": _utc(fetched_at_utc),
                           "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw)},
                "ranking_sets": ranking_sets, "historical_model_feature_eligible": False,
                "limitations": ["This is a captured current ranking snapshot, not a point-in-time historical rank archive.",
                                "Media-panel and Meta positions are separate publisher systems and must not be combined.",
                                "Only publisher-listed divisions and athletes are included; missing ranks and vacancies remain explicit."]}
    document["snapshot_sha256"] = _digest(document)
    validate_rankings(document)
    return document


def validate_rankings(document):
    """Validate a normalized snapshot, including tamper detection and rank grain."""
    if (not isinstance(document, dict) or document.get("schema_version") != 1 or document.get("artifact_type") != "official_current_ufc_rankings"
            or document.get("historical_model_feature_eligible") is not False):
        raise ValueError("Unsupported rankings snapshot")
    if document.get("snapshot_sha256") != _digest(document):
        raise ValueError("Rankings snapshot content digest changed")
    source = document["source"]; _source_url(source["url"]); _utc(source["fetched_at_utc"])
    if not re.fullmatch(r"[0-9a-f]{64}", source["raw_sha256"]) or source["raw_bytes"] <= 0:
        raise ValueError("Rankings raw source receipt is incomplete")
    sets = document["ranking_sets"]
    if not sets or len({s["id"] for s in sets}) != len(sets) or document["default_ranking_set"] not in {s["id"] for s in sets}:
        raise ValueError("Missing or duplicate rankings systems")
    for ranking_set in sets:
        divisions = ranking_set["divisions"]
        if ranking_set["id"] not in _SET_LABELS or not divisions or len({d["id"] for d in divisions}) != len(divisions):
            raise ValueError("Missing or duplicate rankings divisions")
        for division in divisions:
            missing, reasons = _check_division(division)
            if division["missing_ranks"] != missing or division["missing_rank_reasons"] != reasons:
                raise ValueError("Missing rank slots changed")
            if division["published_contender_count"] != len(division["contenders"]):
                raise ValueError("Published rankings coverage changed")
            if not division["contenders"] and not division["empty_reason"]:
                raise ValueError("Empty division requires an explicit source reason")
    return document


def load_rankings(path=DEFAULT_SNAPSHOT, *, as_of=None):
    """Load verified JSON; reject a snapshot captured after an optional cutoff.

    A date-only cutoff means the end of that UTC date. This guard does not turn
    current rankings into validated historical training observations.
    """
    document = validate_rankings(json.loads(Path(path).read_text(encoding="utf-8")))
    if as_of is not None:
        if isinstance(as_of, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
            as_of = date.fromisoformat(as_of)
        if isinstance(as_of, date) and not isinstance(as_of, datetime):
            as_of = datetime.combine(as_of, time.max, timezone.utc)
        cutoff = _utc(as_of)
        if document["source"]["fetched_at_utc"] > cutoff:
            raise ValueError("Rankings snapshot was fetched after the requested cutoff")
    return document


def fetch_rankings(*, raw_directory=None, timeout=30):
    """Fetch the public HTTPS page with curl, validate, and optionally archive it.

    No retry challenges or alternate endpoints are used. A blocked response is
    an error and cannot replace a previously retained snapshot.
    """
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
        raise ValueError("Rankings fetch timeout must be from zero to 120 seconds")
    with tempfile.TemporaryDirectory(prefix="ufc-rankings-fetch-") as directory:
        temporary = Path(directory); page = temporary / "page.html"; headers = temporary / "response-headers.txt"
        result = subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--proto", "=https",
                                 "--proto-redir", "=https", "--max-time", str(timeout), "--max-filesize", str(MAX_PAGE_BYTES),
                                 "--dump-header", str(headers), "--output", str(page),
                                 "--write-out", "%{http_code}\n%{url_effective}", SOURCE_URL],
                                capture_output=True, text=True, timeout=timeout + 5, check=False)
        if result.returncode != 0:
            raise ValueError("Official rankings fetch failed; retained snapshots are preserved")
        fields = result.stdout.strip().splitlines()
        if len(fields) != 2 or fields[0] != "200":
            raise ValueError("Official rankings did not return HTTP 200")
        response_url = _source_url(fields[1])
        fetched = _utc(datetime.now(timezone.utc))
        document = parse_rankings(page.read_bytes(), fetched_at_utc=fetched)
        document["source"].update(response_url=response_url, http_status=200, access_mode="direct_public_https_curl")
        if raw_directory is not None:
            root = Path(raw_directory); root.mkdir(parents=True, exist_ok=True)
            capture_id = fetched.replace("-", "").replace(":", "") + "-" + document["source"]["raw_sha256"][:16]
            target = root / capture_id
            receipt = {**document["source"], "headers_sha256": hashlib.sha256(headers.read_bytes()).hexdigest(),
                       "timestamp_basis": "UTC observed after the complete HTTP response was received"}
            if target.exists():
                if ((target / "page.html").read_bytes() != page.read_bytes()
                        or hashlib.sha256((target / "response-headers.txt").read_bytes()).hexdigest() != receipt["headers_sha256"]
                        or json.loads((target / "receipt.json").read_text()) != receipt):
                    raise ValueError("Existing immutable rankings capture changed")
            else:
                stage = Path(tempfile.mkdtemp(prefix=".rankings-", dir=root))
                try:
                    shutil.copyfile(page, stage / "page.html"); shutil.copyfile(headers, stage / "response-headers.txt")
                    (stage / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
                    stage.rename(target)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            document["source"]["raw_archive"] = {"capture_id": capture_id, "page_file": "page.html", "receipt_file": "receipt.json"}
        document["snapshot_sha256"] = _digest(document)
        return validate_rankings(document)
