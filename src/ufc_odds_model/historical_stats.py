"""Canonical, source-backed UFC statistics history, separate from target labels.

Builds a new MySQL dataset from a retained archive by default. Explicit legacy
SQLite destinations remain available for isolated fixtures. All source rounds survive
intake; only unambiguous, complete paired rounds become model records. Public
roster APIs never expose target outcomes. Archive reach is an approximate
static attribute, not evidence of historical measurement availability.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

PROTOCOL = "canonical-ufc-statistics-history-v1"
FILES = ("ufc_event_details.csv", "ufc_fight_results.csv", "ufc_fight_stats.csv", "ufc_fighter_tott.csv")
PAIRS = {"SIG.STR.": "sig", "TOTAL STR.": "total", "TD": "td", "HEAD": "head", "BODY": "body", "LEG": "leg", "DISTANCE": "distance", "CLINCH": "clinch", "GROUND": "ground"}
SINGLES = {"KD": "kd", "SUB.ATT": "sub_attempts", "REV.": "reversals"}
FIELDS = tuple(["kd", *[prefix + suffix for prefix in PAIRS.values() for suffix in ("_landed", "_attempted")], "sub_attempts", "reversals", "control_seconds"])


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(path):
    from .mysql_runtime import is_mysql_target
    if is_mysql_target(path):
        from .model_lab import file_sha256
        return file_sha256(path)
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def _name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _identity(url, kind):
    parsed = urlsplit(url.strip())
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"ufcstats.com", "www.ufcstats.com"}
            or parsed.query or parsed.fragment or parsed.username or parsed.password
            or not re.fullmatch("/" + kind + r"-details/[0-9a-fA-F]{16}", parsed.path)):
        raise ValueError(f"Invalid official {kind} URL")
    return "ufcstats:" + kind + ":" + parsed.path.rsplit("/", 1)[1].lower()


def _seconds(value):
    match = re.fullmatch(r"([0-9]+):([0-5][0-9])", value.strip())
    return int(match[1]) * 60 + int(match[2]) if match else None


def elapsed_time(finish_round, finish_time, time_format):
    """Return (elapsed seconds, per-round seconds, scheduled rounds) or gaps.

    Overtime lengths come from the publisher's explicit parenthesized schedule.
    A no-time-limit first round has a known actual duration at its stop time.
    Invalid/missing schedules keep counts but never invent rate denominators.
    """
    try:
        end = int(finish_round)
    except (ValueError, TypeError):
        return None, {}, None
    stopped = _seconds(finish_time or "")
    if end < 1 or stopped is None or stopped <= 0:
        return None, {}, None
    if time_format.strip() == "No Time Limit":
        return (stopped, {1: stopped}, None) if end == 1 else (None, {}, None)
    match = re.fullmatch(r"([1-9][0-9]*) Rnd(?: \+ (?:[0-9]+)?OT)? \(([0-9]+(?:-[0-9]+)*)\)", time_format.strip())
    if not match:
        return None, {}, None
    schedule = [int(x) * 60 for x in match[2].split("-")]
    if any(x <= 0 for x in schedule) or end > len(schedule) or stopped > schedule[end - 1]:
        return None, {}, int(match[1])
    rounds = {i + 1: schedule[i] if i + 1 < end else stopped for i in range(end)}
    return sum(rounds.values()), rounds, int(match[1])


def _counts(row):
    values, errors = {}, []
    for source, prefix in PAIRS.items():
        raw = row.get(source, "").strip()
        match = re.fullmatch(r"([0-9]+) of ([0-9]+)", raw)
        pair = tuple(map(int, match.groups())) if match else (None, None)
        if pair[0] is not None and pair[0] > pair[1]:
            pair = None, None
            errors.append(source + ":landed_exceeds_attempted")
        elif not match:
            errors.append(source + ":missing_or_invalid")
        values[prefix + "_landed"], values[prefix + "_attempted"] = pair
    for source, target in SINGLES.items():
        raw = row.get(source, "").strip()
        values[target] = int(float(raw)) if re.fullmatch(r"[0-9]+(?:\.0+)?", raw) else None
        if values[target] is None:
            errors.append(source + ":missing_or_invalid")
    values["control_seconds"] = _seconds(row.get("CTRL", ""))
    if values["control_seconds"] is None:
        errors.append("CTRL:missing_or_invalid")
    if all(values[k] is not None for k in ("sig_landed", "sig_attempted", "total_landed", "total_attempted")):
        if values["sig_landed"] > values["total_landed"] or values["sig_attempted"] > values["total_attempted"]:
            values["sig_landed"] = values["sig_attempted"] = None
            errors.append("SIG.STR.:exceeds_total_strikes")
    return values, errors


SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE source_files(file TEXT PRIMARY KEY,sha256 TEXT NOT NULL,source_url TEXT NOT NULL,bytes INTEGER NOT NULL);
CREATE TABLE fighters(fighter_id TEXT PRIMARY KEY,name TEXT NOT NULL,name_key TEXT NOT NULL,source_url TEXT UNIQUE NOT NULL,reach_cm REAL,reach_display TEXT,raw_json TEXT NOT NULL);
CREATE INDEX fighter_names ON fighters(name_key);
CREATE TABLE events(event_id TEXT PRIMARY KEY,name TEXT NOT NULL,event_date TEXT NOT NULL,source_url TEXT UNIQUE NOT NULL);
CREATE TABLE bouts(bout_id TEXT PRIMARY KEY,event_id TEXT REFERENCES events,event_date TEXT,fighter_a_id TEXT,fighter_b_id TEXT,fighter_a_name TEXT,fighter_b_name TEXT,source_url TEXT UNIQUE NOT NULL,weight_class TEXT,scheduled_rounds INTEGER,finish_round INTEGER,finish_time TEXT,time_format TEXT,elapsed_seconds INTEGER,outcome TEXT,winner_fighter_id TEXT,stats_status TEXT NOT NULL,gap_reason TEXT,aliases_json TEXT NOT NULL,raw_json TEXT NOT NULL);
CREATE INDEX bouts_dates ON bouts(event_date);
CREATE TABLE source_round_rows(row_number INTEGER PRIMARY KEY,event_name TEXT,bout_name TEXT,round_number INTEGER,fighter_name TEXT,values_json TEXT NOT NULL,errors_json TEXT NOT NULL,raw_json TEXT NOT NULL);
CREATE TABLE round_facts(bout_id TEXT NOT NULL REFERENCES bouts,fighter_id TEXT NOT NULL REFERENCES fighters,round_number INTEGER NOT NULL,elapsed_seconds INTEGER,source_row_number INTEGER NOT NULL REFERENCES source_round_rows,PRIMARY KEY(bout_id,fighter_id,round_number));
CREATE TABLE fighter_bout_facts(bout_id TEXT NOT NULL REFERENCES bouts,fighter_id TEXT NOT NULL REFERENCES fighters,opponent_id TEXT NOT NULL REFERENCES fighters,totals_json TEXT NOT NULL,PRIMARY KEY(bout_id,fighter_id));
CREATE INDEX fighter_history ON fighter_bout_facts(fighter_id,bout_id);
CREATE TABLE research_fighter_links(research_fighter_id TEXT PRIMARY KEY,fighter_id TEXT UNIQUE NOT NULL REFERENCES fighters,reason TEXT NOT NULL);
CREATE TABLE research_bout_links(research_bout_id TEXT PRIMARY KEY,bout_id TEXT UNIQUE NOT NULL REFERENCES bouts,research_event_id TEXT NOT NULL,fighter_a_id TEXT NOT NULL,fighter_b_id TEXT NOT NULL);
CREATE TABLE research_event_links(research_event_id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL REFERENCES events);
CREATE TABLE research_holds(research_bout_id TEXT PRIMARY KEY,research_event_id TEXT NOT NULL,event_date TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE gaps(gap_id INTEGER PRIMARY KEY,bout_id TEXT,field TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL);
"""


def _content_hash(connection):
    h = hashlib.sha256()
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='metadata' ORDER BY name")]
    for table in tables:
        h.update(table.encode() + b"\n")
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            h.update(_json(list(row)).encode() + b"\n")
    return h.hexdigest()


def _connection(path):
    from .mysql_runtime import connect_dataset, is_mysql_target
    if is_mysql_target(path):
        return connect_dataset(path, readonly=True)
    con = sqlite3.connect("file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def build_history(archive_manifest, database_path, research_database_path=None, *, dataset_role='statistics'):
    """Create once, or verify and reuse an identical existing store.

    Changed inputs never overwrite a prior store. Build into a different path
    for a new archive. No research labels, source bytes or old DBs are written.
    """
    from .mysql_runtime import (create_dataset, dataset_exists, dataset_name,
                                finalize_dataset, is_mysql_target)
    mysql = is_mysql_target(database_path)
    if dataset_role not in {'statistics', 'synthetic'}:
        raise ValueError('Canonical history requires a statistics or isolated synthetic dataset role')
    archive_manifest = Path(archive_manifest).resolve()
    database_path = ('mysql:' + dataset_name(database_path)) if mysql else Path(database_path).resolve()
    manifest = json.loads(archive_manifest.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported archive manifest")
    if mysql and (manifest.get('synthetic') is True) != (dataset_role == 'synthetic'):
        raise ValueError('Synthetic manifests require an isolated synthetic MySQL dataset role')
    captured = datetime.fromisoformat(manifest["captured_at_utc"].replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("Source capture needs timezone")
    files = {r["file"]: r for r in manifest["files"]}
    for name in FILES:
        entry = files.get(name)
        if not entry or _sha(archive_manifest.parent / name) != entry["sha256"]:
            raise ValueError("Archive bytes changed: " + name)
    history_hash = _sha(research_database_path) if research_database_path else None
    inputs = {"protocol": PROTOCOL, "source_manifest_sha256": _sha(archive_manifest), "research_database_sha256": history_hash, "builder_implementation_sha256": _sha(__file__)}
    if mysql:
        inputs.update(dataset_role=dataset_role, synthetic=manifest.get('synthetic') is True)
    if (dataset_exists(database_path) if mysql else database_path.exists()):
        with HistoryStore(database_path) as existing:
            if any(existing.metadata.get(k) != v for k, v in inputs.items()):
                raise ValueError("Existing statistics store has different inputs; choose a new database path")
            return {**existing.metadata, "database_path": str(database_path), "idempotent_reuse": True}
    if not mysql and database_path.is_symlink():
        raise ValueError("Statistics destination cannot be a symlink")
    events_raw, results_raw, rounds_raw, profiles_raw = [_read_csv(archive_manifest.parent / n) for n in FILES]
    if mysql:
        con = create_dataset(SCHEMA, dataset=database_path, role=dataset_role)
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents replacing any existing DB between checks.
        with database_path.open("xb"):
            pass
        con = sqlite3.connect(database_path)
        con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        if not mysql:
            con.executescript(SCHEMA)
        con.execute("BEGIN")
        con.executemany("INSERT INTO source_files VALUES(?,?,?,?)", [(n, files[n]["sha256"], files[n]["source_url"], (archive_manifest.parent / n).stat().st_size) for n in FILES])
        profile_names = defaultdict(list)
        for row in profiles_raw:
            fid = _identity(row["URL"], "fighter")
            reach = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)"', row.get("REACH", "").strip())
            cm = float(reach[1]) * 2.54 if reach else None
            if cm is not None and not 100 <= cm <= 250:
                cm = None
            con.execute("INSERT INTO fighters VALUES(?,?,?,?,?,?,?)", (fid, row["FIGHTER"].strip(), _name(row["FIGHTER"]), row["URL"].strip(), cm, row.get("REACH"), _json(row)))
            profile_names[_name(row["FIGHTER"])].append(fid)
        event_names = defaultdict(list)
        for row in events_raw:
            eid = _identity(row["URL"], "event")
            day = datetime.strptime(row["DATE"], "%B %d, %Y").date().isoformat()
            con.execute("INSERT INTO events VALUES(?,?,?,?)", (eid, row["EVENT"].strip(), day, row["URL"].strip()))
            event_names[_name(row["EVENT"])].append((eid, day))
        source_rounds = defaultdict(dict)
        bad_rounds = set()
        for number, row in enumerate(rounds_raw, 1):
            key = _name(row["EVENT"]), _name(row["BOUT"])
            match = re.fullmatch(r"Round ([1-9][0-9]*)", row["ROUND"].strip())
            rnd = int(match[1]) if match else None
            values, errors = _counts(row)
            con.execute("INSERT INTO source_round_rows VALUES(?,?,?,?,?,?,?,?)", (number, row["EVENT"].strip(), row["BOUT"].strip(), rnd, row["FIGHTER"].strip(), _json(values), _json(errors), _json(row)))
            if rnd is None or not row["FIGHTER"].strip() or values["sig_landed"] is None:
                bad_rounds.add(key)
                continue
            rkey = _name(row["FIGHTER"]), rnd
            if rkey in source_rounds[key] and source_rounds[key][rkey][1] != values:
                bad_rounds.add(key)
            else:
                source_rounds[key][rkey] = (number, values)
        fight_urls = defaultdict(list)
        for row in results_raw:
            fight_urls[_identity(row["URL"], "fight")].append(row)
        audit = Counter()
        for bid, aliases in fight_urls.items():
            row = aliases[0]
            audit["duplicate_fight_url_rows_removed"] += len(aliases) - 1
            conflict = any({k: v.strip() for k, v in other.items() if k != "EVENT"} != {k: v.strip() for k, v in row.items() if k != "EVENT"} for other in aliases[1:])
            sources = {pair for other in aliases for pair in event_names.get(_name(other["EVENT"]), [])}
            pairs = re.split(r"\s+vs\.\s+", row["BOUT"].strip())
            eid, day = next(iter(sources)) if len(sources) == 1 else (None, None)
            fids = [profile_names.get(_name(n), []) for n in pairs]
            ids = [x[0] if len(x) == 1 else None for x in fids] if len(pairs) == 2 else [None, None]
            elapsed, durations, scheduled = elapsed_time(row["ROUND"], row["TIME"], row["TIME FORMAT"])
            outcome = {"W/L": "win", "L/W": "win", "D/D": "draw", "NC/NC": "no_contest"}.get(row["OUTCOME"].strip())
            winner = ids[0] if row["OUTCOME"].strip() == "W/L" else ids[1] if row["OUTCOME"].strip() == "L/W" else None
            gap = ("conflicting_duplicate_fight_url" if conflict else "unknown_or_ambiguous_event" if len(sources) != 1 else "unknown_or_ambiguous_fighter_identity" if None in ids or len(set(ids)) != 2 else "unsupported_outcome" if outcome is None else None)
            try:
                finish_round = int(row["ROUND"])
            except ValueError:
                finish_round = None
            expected = {(_name(n), r) for n in pairs for r in range(1, (finish_round or 0) + 1)}
            possibilities = []
            for alias in aliases:
                key = _name(alias["EVENT"]), _name(alias["BOUT"])
                if key not in bad_rounds and finish_round and set(source_rounds[key]) == expected:
                    possibilities.append(source_rounds[key])
            chosen = possibilities[0] if possibilities else None
            if gap is None and (not chosen or any({k: v[1] for k, v in other.items()} != {k: v[1] for k, v in chosen.items()} for other in possibilities[1:])):
                gap = "missing_incomplete_or_conflicting_paired_rounds"
            con.execute("INSERT INTO bouts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (bid, eid, day, *ids, pairs[0] if pairs else None, pairs[1] if len(pairs) == 2 else None, row["URL"].strip(), row.get("WEIGHTCLASS", "").strip(), scheduled, finish_round, row["TIME"], row["TIME FORMAT"], elapsed, outcome, winner, "usable" if gap is None else "held", gap, _json([x["EVENT"].strip() for x in aliases]), _json(row)))
            if gap:
                audit[gap] += 1
                con.execute("INSERT INTO gaps(bout_id,field,reason,evidence_json) VALUES(?,?,?,?)", (bid, "paired_statistics", gap, _json({"aliases": [x["EVENT"] for x in aliases]})))
                continue
            if elapsed is None:
                audit["usable_counts_without_validated_duration"] += 1
                con.execute("INSERT INTO gaps(bout_id,field,reason,evidence_json) VALUES(?,?,?,?)", (bid, "elapsed_seconds", "unvalidated_time_format_or_finish_time", _json({"round": row["ROUND"], "time": row["TIME"], "time_format": row["TIME FORMAT"]})))
            for name, fid, opponent in zip(pairs, ids, reversed(ids)):
                own = []
                for rnd in range(1, finish_round + 1):
                    source_number, values = chosen[_name(name), rnd]
                    # Raw control remains preserved; an impossible duration is
                    # unavailable for normalized control features only.
                    normalized = dict(values)
                    if normalized["control_seconds"] is not None and rnd in durations and normalized["control_seconds"] > durations[rnd]:
                        normalized["control_seconds"] = None
                    own.append(normalized)
                    con.execute("INSERT INTO round_facts VALUES(?,?,?,?,?)", (bid, fid, rnd, durations.get(rnd), source_number))
                totals = {field: sum(v[field] for v in own) if all(v[field] is not None for v in own) else None for field in FIELDS}
                con.execute("INSERT INTO fighter_bout_facts VALUES(?,?,?,?)", (bid, fid, opponent, _json(totals)))
            audit["usable_paired_bouts"] += 1
        if research_database_path:
            _link_research(con, research_database_path, audit)
        metadata = {**inputs, "history_database_sha256": history_hash, "archive_manifest_sha256": inputs["source_manifest_sha256"], "captured_at_utc": manifest["captured_at_utc"], "repository_commit": manifest.get("repository_commit"), "repository_url": manifest.get("repository_url"), "historical_source_availability": "retrospective_capture_not_historical_prefight_observation", "current_career_averages_used": False, "archive_target_labels_added_to_research": False, "counts": {"source_event_rows": len(events_raw), "source_fight_rows": len(results_raw), "canonical_archive_bouts": len(fight_urls), "source_round_rows": len(rounds_raw), "source_profile_rows": len(profiles_raw), **dict(audit)}}
        metadata["store_content_sha256"] = _content_hash(con)
        con.executemany("INSERT INTO metadata VALUES(?,?)", [(k, _json(v)) for k, v in metadata.items()])
        con.commit()
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if mysql:
            finalize_dataset(con, metadata)
    except BaseException:
        con.rollback()
        con.close()
        # Only this invocation's exclusively created output can reach here.
        # Existing stores returned or failed validation before creation.
        if not mysql:
            database_path.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    if history_hash and _sha(research_database_path) != history_hash:
        raise ValueError("Read-only research database changed during build")
    return {**metadata, "database_path": str(database_path), "idempotent_reuse": False}


def _link_research(con, database_path, audit):
    history = _connection(database_path)
    try:
        names = defaultdict(list)
        for row in history.execute("SELECT fighter_id,canonical_name FROM fighters"):
            names[_name(row["canonical_name"])].append(row["fighter_id"])
        profiles = defaultdict(list)
        for row in con.execute("SELECT fighter_id,name_key FROM fighters"):
            profiles[row["name_key"]].append(row["fighter_id"])
        links = {}
        for key, ids in names.items():
            if len(ids) == 1 and ids[0].startswith("wikipedia:") and len(profiles[key]) == 1:
                links[ids[0]] = profiles[key][0]
                con.execute("INSERT INTO research_fighter_links VALUES(?,?,?)", (ids[0], profiles[key][0], "unique_exact_NFKC_casefold_whitespace_name_no_alias_merge"))
        lookup = defaultdict(list)
        by_pair = defaultdict(list)
        for row in con.execute("SELECT * FROM bouts WHERE event_date IS NOT NULL AND fighter_a_id IS NOT NULL AND fighter_b_id IS NOT NULL"):
            pair = frozenset((row["fighter_a_id"], row["fighter_b_id"]))
            lookup[row["event_date"], pair].append(row)
            by_pair[pair].append(row)
        rows = list(history.execute("""SELECT b.bout_id,b.event_id,e.event_date,b.fighter_a_id,b.fighter_b_id,r.outcome,r.winner_fighter_id
            FROM bouts b JOIN events e USING(event_id) JOIN results r USING(bout_id)
            WHERE e.source='wikipedia_research' AND b.source IN ('wikipedia_research','wikipedia_research_restored')
            AND e.status='completed' AND b.status='completed' ORDER BY e.event_date,b.bout_id"""))
        research_pairs = Counter((r["event_date"], frozenset((links.get(r["fighter_a_id"]), links.get(r["fighter_b_id"])))) for r in rows)
        events = defaultdict(set)
        for row in rows:
            a, b = links.get(row["fighter_a_id"]), links.get(row["fighter_b_id"])
            key = row["event_date"], frozenset((a, b))
            candidates = lookup.get(key, []) if a and b else []
            reason = "unresolved_stable_fighter_identity" if not a or not b else "unmatched_or_ambiguous_date_pair" if len(candidates) != 1 or research_pairs[key] != 1 else None
            nearby = []
            if a and b and not candidates:
                for item in by_pair.get(key[1], []):
                    archive_date = item["event_date"]
                    delta = (date.fromisoformat(archive_date) - date.fromisoformat(row["event_date"])).days
                    if abs(delta) <= 2:
                        nearby.append({"bout_id": item["bout_id"], "archive_date": archive_date, "date_difference_days": delta})
                if nearby:
                    reason = "near_date_pair_requires_explicit_exclusion"
                    con.execute("INSERT INTO gaps(bout_id,field,reason,evidence_json) VALUES(?,?,?,?)", (None, "research_pair_date", reason, _json({"research_bout_id": row["bout_id"], "research_date": row["event_date"], "candidates": nearby})))
            candidate = candidates[0] if reason is None else None
            if candidate and (candidate["outcome"] != row["outcome"] or candidate["winner_fighter_id"] != links.get(row["winner_fighter_id"])):
                reason = "outcome_disagreement"
            if reason:
                con.execute("INSERT INTO research_holds VALUES(?,?,?,?)", (row["bout_id"], row["event_id"], row["event_date"], reason))
                audit["research_" + reason] += 1
            else:
                con.execute("INSERT INTO research_bout_links VALUES(?,?,?,?,?)", (row["bout_id"], candidate["bout_id"], row["event_id"], row["fighter_a_id"], row["fighter_b_id"]))
                events[row["event_id"]].add(candidate["event_id"])
                audit["research_bouts_linked"] += 1
                audit["research_bouts_with_usable_stats"] += candidate["stats_status"] == "usable"
        reverse = Counter(e for values in events.values() for e in values)
        for rid, values in events.items():
            if len(values) == 1 and reverse[next(iter(values))] == 1:
                con.execute("INSERT INTO research_event_links VALUES(?,?)", (rid, next(iter(values))))
        audit["research_fighter_links"] = len(links)
        audit["research_result_bouts"] = len(rows)
    finally:
        history.close()


@dataclass(frozen=True)
class FighterBoutStats:
    bout_id: str
    fighter_id: str
    opponent_id: str
    event_id: str
    event_date: str
    elapsed_seconds: int | None
    totals: dict[str, int | None]
    opponent_totals: dict[str, int | None]
    source_url: str
    research_bout_id: str | None = None
    research_fighter_id: str | None = None
    research_opponent_id: str | None = None


class HistoryStore:
    def __init__(self, path='mysql:statistics', *, verify=True):
        from .mysql_runtime import dataset_name, is_mysql_target
        self.path = ('mysql:' + dataset_name(path)) if is_mysql_target(path) else Path(path).resolve()
        self.connection = _connection(self.path)
        try:
            self.metadata = {r[0]: json.loads(r[1]) for r in self.connection.execute("SELECT * FROM metadata")}
            self.source_summary = self.metadata
            if self.metadata.get("protocol") != PROTOCOL or verify and self.metadata.get("store_content_sha256") != _content_hash(self.connection):
                raise ValueError("Statistics store is incomplete or its content hash changed")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()

    def _fighter_id(self, value):
        row = self.connection.execute("SELECT fighter_id FROM fighters WHERE fighter_id=?", (value,)).fetchone()
        if row:
            return row[0]
        row = self.connection.execute("SELECT fighter_id FROM research_fighter_links WHERE research_fighter_id=?", (value,)).fetchone()
        return row[0] if row else None

    def fighter_profile(self, fighter_id):
        fid = self._fighter_id(fighter_id)
        row = self.connection.execute("SELECT f.*,r.research_fighter_id FROM fighters f LEFT JOIN research_fighter_links r USING(fighter_id) WHERE f.fighter_id=?", (fid,)).fetchone()
        return {"fighter_id": row["fighter_id"], "canonical_id": row["fighter_id"], "name": row["name"], "reach_cm": row["reach_cm"], "reach_source_display": row["reach_display"], "source_url": row["source_url"], "research_fighter_id": row["research_fighter_id"], "mapping_status": "exact_stable_research_link" if row["research_fighter_id"] else "canonical_archive_identity_only"} if row else None

    def event(self, event_id):
        row = self.connection.execute("SELECT e.* FROM events e LEFT JOIN research_event_links r USING(event_id) WHERE e.event_id=? OR r.research_event_id=?", (event_id, event_id)).fetchone()
        return dict(row) if row else None

    def event_bouts(self, event_id):
        event = self.event(event_id)
        if not event:
            return []
        return [dict(r) for r in self.connection.execute("""SELECT b.bout_id,b.fighter_a_id,b.fighter_b_id,b.fighter_a_name,b.fighter_b_name,b.weight_class,b.scheduled_rounds,b.source_url,b.stats_status,b.gap_reason
            FROM bouts b WHERE b.event_id=? ORDER BY b.rowid""", (event["event_id"],))]

    def _records(self, where="", parameters=()):
        sql = """SELECT b.*,f.fighter_id,f.opponent_id,f.totals_json,o.totals_json opponent_totals_json,
            r.research_bout_id,rf.research_fighter_id,ro.research_fighter_id research_opponent_id
            FROM fighter_bout_facts f JOIN bouts b USING(bout_id)
            JOIN fighter_bout_facts o ON o.bout_id=f.bout_id AND o.fighter_id=f.opponent_id
            LEFT JOIN research_bout_links r ON r.bout_id=b.bout_id
            LEFT JOIN research_fighter_links rf ON rf.fighter_id=f.fighter_id
            LEFT JOIN research_fighter_links ro ON ro.fighter_id=f.opponent_id """ + where + " ORDER BY b.event_date,b.bout_id,f.fighter_id"
        return tuple(FighterBoutStats(r["bout_id"], r["fighter_id"], r["opponent_id"], r["event_id"], r["event_date"], r["elapsed_seconds"], json.loads(r["totals_json"]), json.loads(r["opponent_totals_json"]), r["source_url"], r["research_bout_id"], r["research_fighter_id"], r["research_opponent_id"]) for r in self.connection.execute(sql, parameters))

    def research_bout_records(self, through_date=None):
        if through_date is not None:
            date.fromisoformat(through_date)
        return {(r.research_bout_id, r.research_fighter_id): r for r in self._records("WHERE r.research_bout_id IS NOT NULL" + (" AND b.event_date<=?" if through_date else ""), (through_date,) if through_date else ())}

    def research_fighter_ids(self):
        return [r[0] for r in self.connection.execute("SELECT research_fighter_id FROM research_fighter_links ORDER BY research_fighter_id")]

    def all_research_fighter_bouts(self, through_date=None):
        if through_date is not None:
            date.fromisoformat(through_date)
        return self._records("WHERE rf.research_fighter_id IS NOT NULL" + (" AND b.event_date<=?" if through_date else ""), (through_date,) if through_date else ())

    def prior_fighter_bouts(self, fighter_id, before_date):
        date.fromisoformat(before_date)
        fid = self._fighter_id(fighter_id)
        return self._records("WHERE f.fighter_id=? AND b.event_date<?", (fid, before_date)) if fid else ()

    def bout_rounds(self, bout_id):
        """Normalized paired rounds with source row numbers; no result labels."""
        rows = self.connection.execute("""SELECT r.fighter_id,r.round_number,r.elapsed_seconds,r.source_row_number,s.values_json,b.source_url
            FROM round_facts r JOIN source_round_rows s ON s.row_number=r.source_row_number
            JOIN bouts b ON b.bout_id=r.bout_id WHERE r.bout_id=? ORDER BY r.round_number,r.fighter_id""", (bout_id,))
        result = []
        for row in rows:
            values = json.loads(row["values_json"])
            if (values["control_seconds"] is not None and row["elapsed_seconds"] is not None
                    and values["control_seconds"] > row["elapsed_seconds"]):
                values["control_seconds"] = None
            result.append({"fighter_id": row["fighter_id"], "round_number": row["round_number"], "elapsed_seconds": row["elapsed_seconds"], "source_row_number": row["source_row_number"], "totals": values, "source_url": row["source_url"]})
        return result

    def fighter_aggregate(self, fighter_id, before_date):
        rows = self.prior_fighter_bouts(fighter_id, before_date)
        missing = {k: sum(r.totals[k] is None for r in rows) for k in FIELDS}
        totals = {k: sum(r.totals[k] for r in rows) if rows and not missing[k] else None for k in FIELDS}
        opposing = {k: sum(r.opponent_totals[k] for r in rows) if rows and all(r.opponent_totals[k] is not None for r in rows) else None for k in FIELDS}
        duration_rows = [r for r in rows if r.elapsed_seconds is not None]
        return {"bouts": len(rows), "elapsed_seconds": sum(r.elapsed_seconds for r in rows) if rows and len(duration_rows) == len(rows) else None, "known_elapsed_seconds": sum(r.elapsed_seconds for r in duration_rows), "duration_bouts": len(duration_rows), "totals": totals, "opponent_totals": opposing, "missing_fields": missing, "source_urls": sorted({r.source_url for r in rows})}

    def event_coverage(self):
        return [dict(r) for r in self.connection.execute("""SELECT e.event_id,e.name,e.event_date,e.source_url,r.research_event_id,
            COUNT(b.bout_id) bouts,SUM(b.stats_status='usable') paired_stat_bouts,
            SUM(b.stats_status='usable' AND b.elapsed_seconds IS NOT NULL) duration_stat_bouts,
            SUM(b.stats_status='held') held_bouts,
            SUM(b.fighter_a_id IS NOT NULL AND b.fighter_b_id IS NOT NULL) identity_resolved_bouts,
            SUM(fa.reach_cm IS NOT NULL AND fb.reach_cm IS NOT NULL) both_reach_bouts
            FROM events e LEFT JOIN bouts b USING(event_id) LEFT JOIN research_event_links r USING(event_id)
            LEFT JOIN fighters fa ON fa.fighter_id=b.fighter_a_id LEFT JOIN fighters fb ON fb.fighter_id=b.fighter_b_id
            GROUP BY e.event_id ORDER BY e.event_date,e.event_id""")]

    def fighter_coverage(self):
        return [dict(r) for r in self.connection.execute("""SELECT f.fighter_id,f.name,f.reach_cm,f.source_url,r.research_fighter_id,
            COUNT(b.bout_id) paired_stat_bouts,SUM(b.elapsed_seconds IS NOT NULL) duration_stat_bouts,
            MIN(b.event_date) first_stat_date,MAX(b.event_date) last_stat_date
            FROM fighters f LEFT JOIN research_fighter_links r USING(fighter_id)
            LEFT JOIN fighter_bout_facts s USING(fighter_id) LEFT JOIN bouts b USING(bout_id)
            GROUP BY f.fighter_id ORDER BY f.name,f.fighter_id""")]


def export_coverage(database_path, json_path=None, csv_path=None):
    with HistoryStore(database_path) as store:
        report = {"schema_version": 1, "protocol": PROTOCOL, "metadata": store.metadata, "events": store.event_coverage(), "fighters": store.fighter_coverage(), "research_gaps": [dict(r) for r in store.connection.execute("SELECT * FROM research_holds ORDER BY event_date,research_bout_id")], "archive_gaps": [dict(r) for r in store.connection.execute("SELECT bout_id,field,reason FROM gaps ORDER BY gap_id")], "limitations": ["Retrospectively captured source; archive reach is approximate and measurement dates are unknown.", "Current career averages never enter historical counts or rate denominators.", "Archive-only recent results are retained separately; research target labels are not extended.", "Unresolved identities, ambiguous rematches and invalid durations remain explicit gaps."]}
    if json_path:
        path = Path(json_path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    if csv_path:
        path = Path(csv_path); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(report["events"][0]) if report["events"] else ["event_id"])
            writer.writeheader(); writer.writerows(report["events"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", required=True)
    parser.add_argument("--history")
    parser.add_argument("--db", default='mysql:statistics', help='MySQL dataset; an explicit legacy file is also accepted')
    parser.add_argument("--coverage-json")
    parser.add_argument("--coverage-csv")
    args = parser.parse_args(argv)
    result = build_history(args.archive, args.db, args.history)
    if args.coverage_json or args.coverage_csv:
        export_coverage(args.db, args.coverage_json, args.coverage_csv)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
