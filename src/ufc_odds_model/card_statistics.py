"""Source-backed, earlier-fight statistics for any retained or supplied card.

This module never fits a model or reads current athlete career averages.
Every comparison is built from individual fights whose date is strictly
earlier than both the target event date and the requested as-of date. Archived
static reach is labelled retrospective; it does not establish what was known
before a historical fight. Missing identities and counts remain unavailable.

Example::

    python -m ufc_odds_model.card_statistics --stats-db data/statistics.sqlite \
      --history data/research.sqlite --event-id wikipedia:1234 \
      --out-dir output/card-statistics-1234

``build_card_report`` accepts a canonical/research event ID, or ``card`` as a
path/public dictionary with event and bouts. Each supplied fighter requires
an explicit fighter_id; names never resolve an identity. The optional history
database supplies card rosters, not current career-stat aggregates.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit


METRICS = {
    "significant_strikes_landed_per_minute": ("Sig. strikes landed", "/ min", "rate", "sig_landed", 60),
    "significant_strikes_absorbed_per_minute": ("Sig. strikes absorbed", "/ min", "opponent_rate", "sig_landed", 60),
    "significant_strike_accuracy_pct": ("Striking accuracy", "%", "ratio", "sig", 100),
    "significant_strike_defense_pct": ("Striking defense", "%", "defense", "sig", 100),
    "knockdowns_per_15_minutes": ("Knockdowns", "/ 15 min", "rate", "kd", 900),
    "takedowns_per_15_minutes": ("Takedowns landed", "/ 15 min", "rate", "td_landed", 900),
    "takedown_accuracy_pct": ("Takedown accuracy", "%", "ratio", "td", 100),
    "takedown_defense_pct": ("Takedown defense", "%", "defense", "td", 100),
    "submissions_per_15_minutes": ("Submission attempts", "/ 15 min", "rate", "sub_attempts", 900),
    "control_share_pct": ("Control time", "% of fight", "control", "control_seconds", 100),
}
LIMITATIONS = [
    "These are retrospective research comparisons, not a bout-winner forecast or a betting recommendation.",
    "Only earlier whole event dates enter the aggregates. Target-day and later fights are excluded, even when their results are already retained.",
    "A target bout or the same participant pair within two days of the target date is also excluded, protecting against source event-date disagreements.",
    "Historical cards loaded from a research database or supplied manifest use a two-day whole-date embargo. Canonical archive cards use their canonical event date; all same-event dates remain excluded.",
    "Archive collection times can be later than historical fights. Chronological feature construction does not prove historical source availability.",
    "Archived reach is an approximate static attribute observed retrospectively; its historical measurement date is unknown.",
    "Each metric uses only fights with all of its required counts and, for time-based rates, a known duration. Its sample size can differ from other metrics.",
    "Unresolved identities are not matched by name. Missing counts are not zero; current athlete career averages are never substituted.",
]


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return value


def _time(value: datetime | str | None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("as_of must be a timezone-aware ISO timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return value.astimezone(timezone.utc)


def _text(value: Any, label: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be nonempty public text")
    # Public exports should never acquire raw receipt paths or credentials.
    lowered = value.casefold()
    if any(token in lowered for token in ("file://", "/users/", "/private/", "api_key=", "access_token=", "bearer ")):
        raise ValueError(f"{label} contains private information")
    return value.strip()


def _public_url(value: Any) -> str | None:
    if not value:
        return None
    value = _text(value, "source URL", maximum=2000)
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            or (parsed.scheme == "http" and parsed.hostname != "ufcstats.com")
            or any(any(secret in key.casefold() for secret in ("key", "token", "secret", "password")) for key, _ in parse_qsl(parsed.query))):
        raise ValueError("Statistics sources must be public URLs without credentials")
    return value


def _count(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Statistics counts must be finite and nonnegative, or null")
    return float(value)


def _file_sha(path: str | Path) -> str:
    from .mysql_runtime import is_mysql_target
    if is_mysql_target(path):
        from .model_lab import file_sha256
        return file_sha256(path)
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _duration(row: Any) -> float | None:
    value = _count(_get(row, "elapsed_seconds"))
    return value if value is not None and value > 0 else None


def _metric(rows: tuple[Any, ...], specification: tuple) -> dict[str, Any]:
    label, unit, kind, field, scale = specification
    numerator, denominator, used = 0.0, 0.0, 0
    for row in rows:
        own, opponent = _get(row, "totals", {}), _get(row, "opponent_totals", {})
        if not isinstance(own, Mapping) or not isinstance(opponent, Mapping):
            raise ValueError("Per-fight statistics require own and opponent count objects")
        if kind in {"rate", "opponent_rate", "control"}:
            value = _count((opponent if kind == "opponent_rate" else own).get(field))
            exposure = _duration(row)
            if value is None or exposure is None:
                continue
            if kind == "control" and value > exposure:
                raise ValueError("Control time cannot exceed recorded fight duration")
            a, b = value, exposure
        else:
            counts = opponent if kind == "defense" else own
            landed, attempted = _count(counts.get(field + "_landed")), _count(counts.get(field + "_attempted"))
            if landed is None or attempted is None:
                continue
            if landed > attempted:
                raise ValueError("Landed counts cannot exceed attempted counts")
            if attempted == 0:
                continue
            a, b = (attempted - landed if kind == "defense" else landed), attempted
        numerator += a
        denominator += b
        used += 1
    return {"label": label, "unit": unit, "value": scale * numerator / denominator if denominator > 0 else None,
            "contributing_bouts": used, "eligible_prior_bouts": len(rows), "excluded_for_missing_inputs": len(rows) - used}


def _unavailable(identity: Any, name: str, reason: str) -> dict[str, Any]:
    return {"fighter_id": identity if isinstance(identity, str) else None, "name": name,
            "status": "unavailable", "reason": reason, "prior_stat_bouts": None,
            "known_duration_bouts": None, "known_minutes": None, "reach_cm": None,
            "metrics": {key: {"label": values[0], "unit": values[1], "value": None, "contributing_bouts": 0,
                              "eligible_prior_bouts": 0, "excluded_for_missing_inputs": 0} for key, values in METRICS.items()},
            "prior_fights": []}


def fighter_statistics(store: Any, fighter_id: str | None, name: str, before_date: str, *,
                       target_date: str | None = None, target_opponent_id: str | None = None,
                       target_bout_id: str | None = None) -> dict[str, Any]:
    """Aggregate a resolved fighter's strictly earlier per-fight observations.

    Store filtering is repeated at this boundary so future/same-day rows cannot
    enter a report if an adapter accidentally returns them. No target outcomes
    or current career-stat fields are read or exported.
    """
    before_date = _date(before_date, "before_date")
    if not fighter_id:
        return _unavailable(None, name, "No stable fighter ID supplied; names were not matched automatically.")
    fighter_id = _text(fighter_id, "fighter ID")
    profile = store.fighter_profile(fighter_id)
    if profile is None:
        return _unavailable(fighter_id, name, "No unambiguous stable mapping exists in the retained statistics store.")
    canonical = _text(_get(profile, "fighter_id"), "canonical fighter ID")
    opponent_profile = store.fighter_profile(target_opponent_id) if target_opponent_id else None
    opponent_canonical = _get(opponent_profile, "fighter_id") if opponent_profile else None
    target_day = date.fromisoformat(_date(target_date, "target_date")) if target_date else None
    rows, seen, excluded_overlap = [], set(), 0
    for row in store.prior_fighter_bouts(fighter_id, before_date):
        day = _date(_get(row, "event_date"), "prior fight date")
        if day >= before_date:
            continue
        if _get(row, "fighter_id") != canonical:
            raise ValueError("Statistics store returned rows for a different canonical fighter")
        bout_id = _text(_get(row, "bout_id"), "prior bout ID")
        same_bout = target_bout_id is not None and target_bout_id in {bout_id, _get(row, "research_bout_id")}
        near_target = target_day is not None and abs((date.fromisoformat(day) - target_day).days) <= 2
        same_pair = opponent_canonical is not None and _get(row, "opponent_id") == opponent_canonical
        # With an unresolved target opponent, conservatively suppress the
        # nearby observation instead of trying a name-only identity join.
        if same_bout or (near_target and (same_pair or opponent_canonical is None)):
            excluded_overlap += 1
            continue
        if bout_id in seen:
            raise ValueError("Duplicate earlier-fight statistics cannot be counted twice")
        seen.add(bout_id)
        rows.append(row)
    rows = tuple(sorted(rows, key=lambda row: (_get(row, "event_date"), _get(row, "bout_id"))))
    reach = _count(_get(profile, "reach_cm"))
    if reach is not None and not 80 <= reach <= 260:
        raise ValueError("Archived reach is outside a valid centimeter range")
    known_durations = [value for row in rows if (value := _duration(row)) is not None]
    result = {"fighter_id": fighter_id, "canonical_fighter_id": canonical, "name": name,
              "canonical_name": _text(_get(profile, "name"), "canonical fighter name"),
              "status": "available" if rows else "no_prior_statistics", "reason": None if rows else "No earlier-date per-fight statistics are retained for this fighter.",
              "prior_stat_bouts": len(rows), "known_duration_bouts": len(known_durations),
              "excluded_target_overlap_bouts": excluded_overlap,
              "known_minutes": sum(known_durations) / 60, "reach_cm": reach,
              "reach_basis": "approximate_static_retrospective_archive" if reach is not None else "unavailable",
              "reach_source_url": _public_url(_get(profile, "source_url")),
              "metrics": {key: _metric(rows, values) for key, values in METRICS.items()},
              "prior_fights": [{"bout_id": _get(row, "bout_id"), "event_date": _get(row, "event_date"),
                                "source_url": _public_url(_get(row, "source_url"))} for row in rows]}
    return result


def _history_event(path: str | Path, event_id: str) -> tuple[dict, list[dict]] | None:
    from .mysql_runtime import is_mysql_target
    if is_mysql_target(path):
        from .model_lab import read_only
        with read_only(path) as connection:
            row = connection.execute('SELECT event_id,name,event_date FROM events WHERE event_id=?', (event_id,)).fetchone()
            if row is None:
                return None
            bouts = [dict(item) for item in connection.execute(
                'SELECT b.bout_id,b.fighter_a_id,b.fighter_b_id,b.weight_class,b.scheduled_rounds,'
                'a.canonical_name AS fighter_a_name,z.canonical_name AS fighter_b_name FROM bouts b '
                'JOIN fighters a ON a.fighter_id=b.fighter_a_id JOIN fighters z ON z.fighter_id=b.fighter_b_id '
                'WHERE b.event_id=? ORDER BY b.bout_id', (event_id,))]
            return {**dict(row), 'roster_basis': 'retained_mysql_research_dataset'}, bouts
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("The optional history database must be a regular retained file")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("SELECT event_id,name,event_date FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        bouts = [dict(row) for row in connection.execute(
            "SELECT b.bout_id,b.fighter_a_id,b.fighter_b_id,b.weight_class,b.scheduled_rounds,"
            "a.canonical_name AS fighter_a_name,z.canonical_name AS fighter_b_name FROM bouts b "
            "JOIN fighters a ON a.fighter_id=b.fighter_a_id JOIN fighters z ON z.fighter_id=b.fighter_b_id "
            "WHERE b.event_id=? ORDER BY b.bout_id", (event_id,))]
        return {**dict(row), "roster_basis": "retained_history_database"}, bouts


def _manifest_card(card: Any) -> tuple[dict, list[dict]]:
    if isinstance(card, (str, Path)):
        card = json.loads(Path(card).read_text(encoding="utf-8"))
    if not isinstance(card, Mapping) or not isinstance(card.get("event"), Mapping) or not isinstance(card.get("bouts"), list):
        raise ValueError("A supplied card requires event and bouts objects")
    event = card["event"]
    result = {"event_id": _text(event.get("event_id"), "event ID"), "name": _text(event.get("name"), "event name"),
              "event_date": _date(event.get("event_date"), "event date"), "roster_basis": "caller_supplied_card_manifest",
              "source_url": _public_url(card.get("source_url", event.get("source_url")))}
    bouts = []
    for index, bout in enumerate(card["bouts"], 1):
        if not isinstance(bout, Mapping):
            raise ValueError("Every supplied bout must be an object")
        row = {"bout_id": _text(bout.get("bout_id", f"{result['event_id']}:listed:{index}"), "bout ID")}
        for side in ("fighter_a", "fighter_b"):
            fighter = bout.get(side)
            if not isinstance(fighter, Mapping):
                raise ValueError("Supplied fighters require explicit name and optional stable fighter_id")
            row[side + "_name"] = _text(fighter.get("name"), "fighter name")
            identity = fighter.get("fighter_id")
            row[side + "_id"] = _text(identity, "fighter ID") if identity else None
        for key in ("weight_class", "scheduled_rounds"):
            if key in bout:
                row[key] = bout[key]
        bouts.append(row)
    return result, bouts


def build_card_report(stats_db: str | Path, *, history: str | Path | None = None,
                      event_id: str | None = None, card: Any = None,
                      as_of: datetime | str | None = None) -> dict[str, Any]:
    """Build a public research report from the read-only persistent store."""
    from .historical_stats import HistoryStore

    if bool(event_id) == (card is not None):
        raise ValueError("Choose exactly one event_id or supplied card")
    as_of = _time(as_of)
    store_sha = _file_sha(stats_db)
    with HistoryStore(stats_db) as store:
        if card is not None:
            event, bouts = _manifest_card(card)
        else:
            event_id = _text(event_id, "event ID")
            found = _history_event(history, event_id) if history else None
            if found:
                event, bouts = found
            else:
                lookup_id = "ufcstats:event:" + event_id.lower() if re.fullmatch(r"[0-9a-fA-F]{16}", event_id) else event_id
                stored = store.event(lookup_id)
                if stored is None:
                    raise ValueError("Event ID was not found in the retained statistics store or optional history")
                event = {key: _get(stored, key) for key in ("event_id", "name", "event_date", "source_url")}
                event["roster_basis"] = "retained_statistics_store"
                bouts = list(store.event_bouts(_get(stored, "event_id")))
        event["event_id"] = _text(event["event_id"], "event ID")
        event["name"] = _text(event["name"], "event name")
        event["event_date"] = _date(event["event_date"], "event date")
        if event.get("source_url"):
            event["source_url"] = _public_url(event["source_url"])
        if not bouts:
            raise ValueError("The selected event has no retained bouts")
        cutoff = min(event["event_date"], as_of.date().isoformat())
        guarded = (event["roster_basis"] != "retained_statistics_store"
                   and event["event_date"] <= datetime.now(timezone.utc).date().isoformat())
        if guarded:
            cutoff = min(cutoff, (date.fromisoformat(event["event_date"]) - timedelta(days=2)).isoformat())
        rows, seen = [], set()
        for raw in bouts:
            bout_id = _text(_get(raw, "bout_id"), "bout ID")
            if bout_id in seen:
                raise ValueError("Card contains duplicate bout IDs")
            seen.add(bout_id)
            row = {"bout_id": bout_id, "weight_class": _get(raw, "weight_class"), "scheduled_rounds": _get(raw, "scheduled_rounds")}
            for side in ("fighter_a", "fighter_b"):
                name = _text(_get(raw, side + "_name"), "fighter name")
                other = "fighter_b" if side == "fighter_a" else "fighter_a"
                row[side] = fighter_statistics(store, _get(raw, side + "_id"), name, cutoff,
                                               target_date=event["event_date"], target_opponent_id=_get(raw, other + "_id"),
                                               target_bout_id=bout_id)
            if (row["fighter_a"].get("canonical_fighter_id") is not None
                    and row["fighter_a"].get("canonical_fighter_id") == row["fighter_b"].get("canonical_fighter_id")):
                raise ValueError("A matchup cannot contain the same canonical fighter twice")
            rows.append(row)
        metadata = store.metadata
        lineage = {key: metadata[key] for key in ("source_manifest_sha256", "store_content_sha256", "captured_at_utc", "history_database_sha256", "protocol", "repository_url", "repository_commit") if key in metadata}
        lineage["statistics_database_sha256"] = store_sha
        if history:
            lineage["roster_database_sha256"] = _file_sha(history)
        if isinstance(card, (str, Path)):
            lineage["supplied_card_sha256"] = _file_sha(card)
    if _file_sha(stats_db) != store_sha:
        raise ValueError("Statistics database changed while the report was being read")
    fighters = [row[side] for row in rows for side in ("fighter_a", "fighter_b")]
    report = {"schema_version": 1, "report_type": "earlier_fight_card_statistics", "research_only": True,
              "event": event, "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
              "as_of_utc": as_of.isoformat(timespec="seconds").replace("+00:00", "Z"), "prior_fight_date_exclusive": cutoff,
              "temporal_guard": "two_day_whole_date_embargo" if guarded else "canonical_whole_date_exclusion",
              "summary": {"bouts": len(rows), "fighters": len(fighters),
                          "fighters_with_prior_stats": sum(row["status"] == "available" for row in fighters),
                          "unresolved_identities": sum(row["status"] == "unavailable" for row in fighters),
                          "resolved_without_prior_stats": sum(row["status"] == "no_prior_statistics" for row in fighters)},
              "source_context": lineage, "bouts": rows, "limitations": LIMITATIONS.copy()}
    json.dumps(report, allow_nan=False)
    return report


def _display(metric: Mapping[str, Any]) -> str:
    return "Unavailable" if metric["value"] is None else f"{metric['value']:.1f}" if "%" in metric["unit"] else f"{metric['value']:.2f}"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _csv_safe(value: Any) -> Any:
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value


def render_csv(report: Mapping[str, Any]) -> str:
    columns = ["event_id", "event", "event_date", "as_of_utc", "prior_fight_date_exclusive", "bout_id", "fighter", "opponent", "fighter_id", "status", "reason", "prior_stat_bouts", "known_duration_bouts", "known_minutes", "retrospective_static_reach_cm"]
    columns += [suffix for key in METRICS for suffix in (key, key + "_contributing_bouts")]
    columns += ["prior_source_urls", "research_only", "temporal_limitations"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for bout in report["bouts"]:
        for side, other in (("fighter_a", "fighter_b"), ("fighter_b", "fighter_a")):
            fighter = bout[side]
            row = {"event_id": report["event"]["event_id"], "event": report["event"]["name"], "event_date": report["event"]["event_date"],
                   "as_of_utc": report["as_of_utc"], "prior_fight_date_exclusive": report["prior_fight_date_exclusive"], "bout_id": bout["bout_id"],
                   "fighter": fighter["name"], "opponent": bout[other]["name"], "fighter_id": fighter["fighter_id"], "status": fighter["status"],
                   "reason": fighter["reason"], "prior_stat_bouts": fighter["prior_stat_bouts"], "known_duration_bouts": fighter["known_duration_bouts"],
                   "known_minutes": fighter["known_minutes"], "retrospective_static_reach_cm": fighter["reach_cm"],
                   "prior_source_urls": " | ".join(dict.fromkeys(row["source_url"] for row in fighter["prior_fights"] if row["source_url"])),
                   "research_only": True, "temporal_limitations": " | ".join(report["limitations"])}
            for key in METRICS:
                row[key] = fighter["metrics"][key]["value"]
                row[key + "_contributing_bouts"] = fighter["metrics"][key]["contributing_bouts"]
            writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    return output.getvalue()


def render_html(report: Mapping[str, Any]) -> str:
    event, summary = report["event"], report["summary"]
    sections = []
    for index, bout in enumerate(report["bouts"], 1):
        a, b = bout["fighter_a"], bout["fighter_b"]
        table_rows = []
        for key, definition in METRICS.items():
            ma, mb = a["metrics"][key], b["metrics"][key]
            table_rows.append(f"<tr><td>{_esc(_display(ma))}<small>{ma['contributing_bouts']} fights</small></td><th scope='row'>{_esc(definition[0])}<small>{_esc(definition[1])}</small></th><td>{_esc(_display(mb))}<small>{mb['contributing_bouts']} fights</small></td></tr>")
        notes = []
        for fighter in (a, b):
            if fighter["status"] == "unavailable":
                note = fighter["reason"]
            else:
                reach = f"{fighter['reach_cm']:.1f} cm approximate archived reach" if fighter["reach_cm"] is not None else "Reach unavailable"
                note = f"{fighter['prior_stat_bouts']} prior stat fights; {fighter['known_duration_bouts']} with duration; {fighter['known_minutes']:.1f} known fight minutes. {reach}."
                if fighter["reason"]:
                    note += " " + fighter["reason"]
            notes.append(f"<p class='coverage'><strong>{_esc(fighter['name'])}:</strong> {_esc(note)}</p>")
            if fighter["prior_fights"]:
                entries = []
                for fight in fighter["prior_fights"]:
                    link = f"<a href='{_esc(fight['source_url'])}'>{_esc(fight['event_date'])} source</a>" if fight["source_url"] else _esc(fight["event_date"]) + " (source link unavailable)"
                    entries.append(f"<li>{link}</li>")
                notes.append(f"<details><summary>{_esc(fighter['name'])} · earlier-fight source trail</summary><ul>{''.join(entries)}</ul></details>")
        sections.append(f"<article><p class='kicker'>{index:02d} · {_esc(bout.get('weight_class') or 'Listed matchup')}</p><h2>{_esc(a['name'])} <span>vs</span> {_esc(b['name'])}</h2>"
                        f"<table><thead><tr><th scope='col'>{_esc(a['name'])}</th><th scope='col'>Earlier-fight metric</th><th scope='col'>{_esc(b['name'])}</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table>{''.join(notes)}</article>")
    notes = "".join(f"<li>{_esc(value)}</li>" for value in report["limitations"])
    css = """*{box-sizing:border-box}body{margin:0;background:#edf1ee;color:#192c2b;font:14px/1.5 -apple-system,BlinkMacSystemFont,Arial,sans-serif}main{max-width:1080px;margin:30px auto;padding:36px 44px;background:white}header{border-bottom:3px solid #192c2b;padding-bottom:17px}.brand,.kicker{font-size:10px;font-weight:750;letter-spacing:.13em;text-transform:uppercase;color:#176c60}h1{font-size:36px;line-height:1.1;letter-spacing:-1px;margin:13px 0}header p{color:#526763;margin:5px 0}.notice{border-left:3px solid #176c60;background:#f0f5f1;padding:12px 15px;font-size:12px;margin:20px 0}.summary{display:flex;gap:30px;border-bottom:1px solid #dce4df;padding:0 0 17px;color:#526763}.summary strong{display:block;color:#192c2b;font-size:27px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:25px 30px}article{break-inside:avoid;padding-top:17px;border-bottom:1px solid #dce4df;padding-bottom:20px;min-width:0}h2{font-size:18px;line-height:1.3;margin:5px 0 13px}h2 span{font-weight:400;color:#6e807c}table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}th,td{padding:7px 3px;border-bottom:1px solid #e3e9e5;text-align:center}tbody th{font-weight:450;color:#526763}thead th{font-size:10px;overflow-wrap:anywhere;width:28%}thead th:nth-child(2){width:44%}td{font-weight:650;font-variant-numeric:tabular-nums}small{display:block;font-size:9px;font-weight:400;color:#71847b}.coverage,details{font-size:10px;color:#526763;margin:10px 0 0}a{color:#176c60}details ul{max-height:160px;overflow:auto;padding-left:17px}.limits{font-size:11px;color:#526763;margin-top:25px;padding-top:14px;border-top:2px solid #192c2b}.limits h2{color:#192c2b;font-family:Georgia,serif;font-size:23px}.source{font-size:10px;overflow-wrap:anywhere}footer{margin-top:20px;border-top:1px solid #dce4df;padding-top:10px;font-size:10px;color:#526763}@media(max-width:700px){main{margin:0;padding:24px 18px}.grid{grid-template-columns:1fr}.summary{gap:20px;font-size:11px}h1{font-size:29px}}@page{size:auto;margin:15mm}@media print{body{background:white}main{margin:0;padding:0;max-width:none}.grid{gap:20px}details{display:none}article{break-inside:avoid}.limits{break-inside:avoid}}"""
    roster_link = f"<a href='{_esc(event['source_url'])}'>Retained roster source</a>" if event.get("source_url") else "Roster source URL unavailable"
    archive_url = _public_url(report["source_context"].get("repository_url"))
    archive_link = f"<a href='{_esc(archive_url)}'>Retained archive repository</a>" if archive_url else "Archive repository URL unavailable"
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(event['name'])} · Earlier-fight statistics</title><style>{css}</style></head><body><main><header>"
            f"<p class='brand'>UFC research / card statistics</p><h1>{_esc(event['name'])}</h1><p>Event: {_esc(event['event_date'])} · Strict prior-date cutoff: {_esc(report['prior_fight_date_exclusive'])}</p>"
            f"<p>Requested as of {_esc(report['as_of_utc'])}</p></header><p class='notice'><strong>Earlier-fight comparisons.</strong> Each value comes from retained individual fight counts. Target-day results, later fights and current athlete career averages are excluded. This is retrospective research.</p>"
            f"<section class='summary'><div><strong>{summary['bouts']}</strong>listed bouts</div><div><strong>{summary['fighters_with_prior_stats']} / {summary['fighters']}</strong>fighter appearances with prior stats</div><div><strong>{summary['unresolved_identities']}</strong>unresolved identity appearances</div></section>"
            f"<section class='grid'>{''.join(sections)}</section><section class='limits'><h2>Sources and limits</h2><ul>{notes}</ul>"
            f"<p class='source'>Archive capture: {_esc(report['source_context'].get('captured_at_utc', 'Unavailable'))}. Roster basis: {_esc(event['roster_basis'])}. {roster_link} · {archive_link}.</p></section>"
            f"<footer>Prepared {_esc(report['generated_at_utc'])} · Research only · No model promotion or wagering authorization</footer></main></body></html>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stats-db", default='mysql:statistics')
    parser.add_argument("--history", default='mysql:research-complete')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--event-id")
    target.add_argument("--card", type=Path)
    parser.add_argument("--as-of", help="Timezone-aware ISO timestamp; only strictly earlier whole dates are used")
    parser.add_argument("--out-dir", required=True, type=Path, help="New directory for JSON, HTML and CSV")
    args = parser.parse_args(argv)
    try:
        if args.out_dir.exists() or args.out_dir.is_symlink():
            raise FileExistsError("Choose a new output directory; saved reports are never overwritten")
        report = build_card_report(args.stats_db, history=args.history, event_id=args.event_id, card=args.card, as_of=args.as_of)
        outputs = {"card-statistics.json": render_json(report), "card-statistics.html": render_html(report), "card-statistics.csv": render_csv(report)}
        args.out_dir.mkdir(parents=True, exist_ok=False)
        for name, content in outputs.items():
            with (args.out_dir / name).open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        parser.error(str(exc))
    print(json.dumps({"event_id": report["event"]["event_id"], "bouts": report["summary"]["bouts"], "fighters_with_prior_stats": report["summary"]["fighters_with_prior_stats"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
