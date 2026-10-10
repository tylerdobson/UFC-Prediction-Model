"""CC0 Wikidata birth dates/heights with exact, immutable query receipts."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import db
from .features import _Profile, _intact_receipt
from .model_lab import write_new_json
from .pipeline import parse_utc, utc_now, utc_string
from .raw_snapshots import retain_snapshot
from .research_complete import saved_title_map
from .research_evaluation import _intact_payload
from .wikipedia_history import ACTION_API, USER_AGENT

LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"
ENDPOINT = "https://query.wikidata.org/sparql"
PREFIXES = """PREFIX schema: <http://schema.org/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX psv: <http://www.wikidata.org/prop/statement/value/>
PREFIX psn: <http://www.wikidata.org/prop/statement/value-normalized/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX wd: <http://www.wikidata.org/entity/>
"""
ATTRIBUTES = """
  OPTIONAL { ?item p:P569/psv:P569 [ wikibase:timeValue ?dob ; wikibase:timePrecision ?prec ] . }
  OPTIONAL { ?item p:P2048/psn:P2048/wikibase:quantityAmount ?height . }
  OPTIONAL { ?item wdt:P9722 ?ufc . }
"""


def profile_query(values: list[str], *, qids=False) -> str:
    if not values or len(values) > 250:
        raise ValueError("SPARQL needs 1–250 values per request")
    if qids:
        if any(not re.fullmatch(r"Q[1-9][0-9]*", v) for v in values):
            raise ValueError("Invalid Wikidata QID")
        selection = "VALUES ?item { " + " ".join("wd:" + q for q in values) + " }"
    else:
        selection = "VALUES ?title { " + " ".join(json.dumps(t, ensure_ascii=False) + "@en" for t in values) + " }\n"
        selection += "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title ."
    return PREFIXES + "SELECT ?title ?item ?dob ?prec ?height ?ufc WHERE {\n" + selection + ATTRIBUTES + "}"


class WikidataClient:
    """Serial requests, >=1.5 seconds apart; respect numeric/date Retry-After."""
    def __init__(self, min_interval_seconds=1.5):
        self.interval = max(1.5, min_interval_seconds)
        self.last_request = None

    def __call__(self, request: dict) -> bytes:
        for attempt in range(4):
            if self.last_request is not None:
                time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            fields = request.get("fields", {})
            encoded = urllib.parse.urlencode(fields)
            url = request["url"]
            data = encoded.encode() if request["method"] == "POST" else None
            if data is None and fields:
                url += "?" + encoded
            http = urllib.request.Request(url, data=data, method=request["method"], headers={
                "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json, application/json",
                "Content-Type": "application/x-www-form-urlencoded"})
            self.last_request = time.monotonic()
            try:
                with urllib.request.urlopen(http, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 503) or attempt == 3:
                    raise RuntimeError(f"Wikimedia HTTP {exc.code} at {request['url']}") from exc
                header = exc.headers.get("Retry-After", "")
                try:
                    delay = float(header)
                except ValueError:
                    try:
                        delay = max(0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        delay = 2 ** attempt
                if delay > 60:
                    raise RuntimeError(f"Wikimedia requested Retry-After {delay:.0f}s; retry later") from exc
                time.sleep(max(delay, self.interval))
            except urllib.error.URLError as exc:
                if attempt == 3:
                    raise RuntimeError(f"Wikimedia request failed at {request['url']}: {exc.reason}") from exc
                time.sleep(2 ** attempt)
        raise RuntimeError("Wikimedia retry limit reached")


def retained_query(connection, request, fetch, raw_dir, *, offline=False):
    serialized = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    prior = connection.execute(
        """SELECT q.ingestion_run_id,q.request_json,i.payload_path,i.sha256
           FROM wikidata_profile_queries q JOIN ingestion_runs i ON i.run_id=q.ingestion_run_id
           WHERE q.request_sha256=? ORDER BY q.ingestion_run_id DESC LIMIT 1""", (digest,)).fetchone()
    if prior:
        if prior["request_json"] != serialized or not _intact_payload(prior["payload_path"], prior["sha256"]):
            raise ValueError("Changed Wikidata request/response receipt")
        from .retained_paths import retained_path
        return json.loads(retained_path(prior["payload_path"]).read_bytes()), int(prior["ingestion_run_id"])
    if offline:
        raise ValueError(f"No retained query for {digest}; offline import cannot fetch")
    raw = fetch(request)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Wikimedia query did not return a valid response")
    path, sha = retain_snapshot(raw, raw_dir, kind="Wikidata profile response")
    # Relative paths keep new receipts portable with their database and data/raw directory.
    try:
        payload_path = str(path.relative_to(Path.cwd()))
    except ValueError:
        payload_path = str(path)
    with connection:
        run = connection.execute(
            "INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES (?,?,?,?)",
            ("wikidata_profiles", utc_string(utc_now()), payload_path, sha))
        connection.execute("INSERT INTO wikidata_profile_queries VALUES (?,?,?)",
                           (run.lastrowid, digest, serialized))
    return payload, int(run.lastrowid)


def _value(binding, key):
    value = binding.get(key)
    return value.get("value") if isinstance(value, dict) else None


def parse_attributes(bindings: list[dict]) -> tuple[dict, list[str]]:
    """Distinct day-precision dates conflict; lower precision never invents a day."""
    births, heights, ufc_ids, qids = set(), set(), set(), set()
    for binding in bindings:
        item = _value(binding, "item")
        if item:
            qid = item.rsplit("/", 1)[-1]
            if not re.fullmatch(r"Q[1-9][0-9]*", qid):
                raise ValueError("Invalid Wikidata item in response")
            qids.add(qid)
        dob, precision = _value(binding, "dob"), _value(binding, "prec")
        if dob and precision is not None and int(precision) >= 11:
            born = date.fromisoformat(dob.lstrip("+").split("T")[0])
            if born >= utc_now().date():
                raise ValueError("Birth date cannot be in the future")
            births.add(born.isoformat())
        height = _value(binding, "height")
        if height is not None:
            amount = float(height)
            # Wikibase's normalized quantity is SI metres, convert to cm.
            cm = amount * 100
            if math.isfinite(cm) and 100 <= cm <= 250:
                heights.add(format(cm, ".8g"))
        athlete_id = _value(binding, "ufc")
        if athlete_id:
            ufc_ids.add(athlete_id)
    conflicts = []
    for name, values in (("wikidata_qid", qids), ("birth_date", births)):
        if len(values) > 1:
            conflicts.append(name)
    if conflicts:
        return {}, conflicts
    attributes = {}
    for name, values in (("wikidata_qid", qids), ("birth_date", births),
                         ("height_cm", heights), ("ufc_athlete_id", ufc_ids)):
        if len(values) == 1:
            attributes[name] = next(iter(values))
        elif len(values) > 1:
            conflicts.append(name)
    return attributes, conflicts


def import_profiles(connection, *, raw_dir="data/raw/wikidata_profiles",
                    report_path="reports/wikidata_profiles_report.json", fetch=None, offline=False):
    db.require_current_schema(connection)
    if Path(report_path).exists():
        raise ValueError("Profile report needs a new filename")
    fetch = fetch or WikidataClient()
    _, page_titles = saved_title_map(connection)
    fighters = [str(r[0]) for r in connection.execute(
        "SELECT fighter_id FROM fighters WHERE fighter_id LIKE 'wikipedia:%' OR fighter_id LIKE 'wikidata:%' ORDER BY fighter_id")]
    title_fighters, qid_fighters = defaultdict(list), defaultdict(list)
    missing_pages = []
    for fighter_id in fighters:
        source, identifier = fighter_id.split(":", 1)
        if source == "wikidata":
            qid_fighters[identifier].append(fighter_id)
        elif int(identifier) in page_titles:
            title_fighters[page_titles[int(identifier)]].append(fighter_id)
        else:
            missing_pages.append(identifier)
    for offset in range(0, len(missing_pages), 50):
        request = {"url": ACTION_API, "method": "GET", "fields": {
            "action": "query", "format": "json", "formatversion": "2", "prop": "pageprops",
            "ppprop": "wikibase_item", "pageids": "|".join(missing_pages[offset:offset+50]), "maxlag": "5"}}
        payload, _ = retained_query(connection, request, fetch, raw_dir, offline=offline)
        for page in payload.get("query", {}).get("pages", []):
            if page.get("pageid", -1) <= 0 or page.get("missing"):
                continue
            fighter_id = f"wikipedia:{page['pageid']}"
            if fighter_id not in fighters:
                continue
            qid = page.get("pageprops", {}).get("wikibase_item")
            if qid:
                qid_fighters[qid].append(fighter_id)
            elif page.get("title"):
                title_fighters[page["title"]].append(fighter_id)
    by_fighter = defaultdict(list)
    for qids, mapping in ((False, title_fighters), (True, qid_fighters)):
        values = sorted(mapping)
        for offset in range(0, len(values), 250):
            batch = values[offset:offset+250]
            request = {"url": ENDPOINT, "method": "POST", "fields": {
                "query": profile_query(batch, qids=qids), "format": "json"}}
            payload, run_id = retained_query(connection, request, fetch, raw_dir, offline=offline)
            bindings = payload.get("results", {}).get("bindings")
            if not isinstance(bindings, list):
                raise ValueError("SPARQL response lacks binding rows")
            for binding in bindings:
                key = (_value(binding, "item") or "").rsplit("/", 1)[-1] if qids else _value(binding, "title")
                if key not in mapping:
                    raise ValueError("SPARQL returned a title/item outside the requested batch")
                for fighter_id in mapping[key]:
                    by_fighter[fighter_id].append((binding, run_id))
            print(f"Wikidata profiles: {min(offset+250,len(values))}/{len(values)} {'items' if qids else 'titles'}", flush=True)
    conflicts, imported = [], 0
    with connection:
        for fighter_id, records in sorted(by_fighter.items()):
            attributes, problems = parse_attributes([binding for binding, _ in records])
            if problems:
                conflicts.append({"fighter_id": fighter_id, "attributes": problems,
                                  "bindings": [binding for binding, _ in records]})
            if not attributes:
                continue
            run_id = records[0][1]
            for attribute, value in attributes.items():
                prior = {str(r[0]) for r in connection.execute(
                    "SELECT value FROM fighter_static_attributes WHERE fighter_id=? AND attribute=?", (fighter_id, attribute))}
                if prior and prior != {value}:
                    conflicts.append({"fighter_id": fighter_id, "attributes": [attribute],
                                      "existing_values": sorted(prior), "new_value": value})
                    continue
                if prior:
                    continue
                connection.execute(
                    """INSERT INTO fighter_static_attributes(fighter_id,attribute,value,source,license,ingestion_run_id)
                       VALUES (?,?,?,'wikidata',?,?)""", (fighter_id, attribute, value, LICENSE, run_id))
                imported += 1
            for source, attribute in (("wikidata", "wikidata_qid"), ("ufc_athlete_id", "ufc_athlete_id")):
                if attribute in attributes:
                    try:
                        db.link_external_fighter(connection, source, attributes[attribute], fighter_id)
                    except ValueError as exc:
                        conflicts.append({"fighter_id": fighter_id, "attributes": [attribute],
                                          "external_id_conflict": str(exc)})
    report = {"schema_version": 1, "source": "wikidata", "license": LICENSE,
              "eligible_real_fighters": len(fighters), "matched_fighters": len(by_fighter),
              "fighters_with_birth_date": connection.execute(
                  "SELECT COUNT(DISTINCT fighter_id) FROM fighter_static_attributes WHERE attribute='birth_date'").fetchone()[0],
              "fighters_with_height": connection.execute(
                  "SELECT COUNT(DISTINCT fighter_id) FROM fighter_static_attributes WHERE attribute='height_cm'").fetchone()[0],
              "new_attribute_rows": imported, "conflicts": conflicts,
              "unmatched_fighters": sorted(set(fighters) - set(by_fighter)),
              "provisional_fighters_excluded": True}
    write_new_json(report_path, report)
    return report


def static_profiles(connection, *, cutoff_at_utc=None, research=False, receipt_cache=None):
    """Research admits static facts retrospectively; operating requires prior fetch."""
    if not research and cutoff_at_utc is None:
        raise ValueError("Operating static attributes need a decision cutoff")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='fighter_static_attributes'").fetchone():
        return {}
    cache = receipt_cache if receipt_cache is not None else {}
    values = defaultdict(set)
    for row in connection.execute(
        """SELECT a.fighter_id,a.attribute,a.value,a.license,a.ingestion_run_id,
                  i.fetched_at_utc,i.payload_path,i.sha256
           FROM fighter_static_attributes a JOIN ingestion_runs i ON i.run_id=a.ingestion_run_id
           WHERE a.source='wikidata' AND i.source='wikidata_profiles'
             AND a.attribute IN ('birth_date','height_cm')"""):
        if row["license"] != LICENSE or not _intact_receipt(row, cache):
            continue
        if not research and parse_utc(row["fetched_at_utc"]) >= parse_utc(cutoff_at_utc):
            continue
        values[(str(row["fighter_id"]), row["attribute"])].add(str(row["value"]))
    profiles = {}
    for (fighter_id, attribute), candidates in values.items():
        if len(candidates) != 1:
            continue
        if attribute == "birth_date":
            profiles[fighter_id] = _Profile(birth_date=date.fromisoformat(next(iter(candidates))))
    return profiles
