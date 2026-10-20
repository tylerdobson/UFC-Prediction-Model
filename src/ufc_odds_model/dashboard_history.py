"""Export descriptive earlier-fight/round observations for a saved dashboard.

This does not reproduce model features, fit a model or change probabilities.
Identities require the supplied card's explicit stable IDs plus the canonical
store's existing crosswalk. Names alone never resolve an identity. Outputs use
only public values and hashes, without local database or raw capture paths.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

from .historical_stats import HistoryStore
from .prediction_report import validate_report


def _sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("History timestamps require a timezone")
    return result.astimezone(timezone.utc)


def _rate(count, seconds):
    return count * 60 / seconds if count is not None and seconds is not None and seconds > 0 else None


def build_dashboard_history(report_path, card_path, statistics_db, *, max_fights=5):
    """Read verified canonical observations and return a deterministic public asset."""
    if type(max_fights) is not int or not 1 <= max_fights <= 5:
        raise ValueError("Dashboard history accepts one to five actual prior fights")
    report_path, card_path, statistics_db = map(Path, (report_path, card_path, statistics_db))
    report = validate_report(json.loads(report_path.read_bytes()))
    card = json.loads(card_path.read_bytes())
    if card["event"]["event_date"] != report["event"]["event_date"]:
        raise ValueError("Card and forecast event dates differ")
    cutoff = _time(report["forecast"]["cutoff_at_utc"])
    event_date = date.fromisoformat(report["event"]["event_date"])
    embargo_date = event_date - timedelta(days=2)
    before_date = min(cutoff.date(), embargo_date).isoformat()
    by_bout = {row["bout_id"]: row for row in card["bouts"]}
    if len(by_bout) != len(card["bouts"]):
        raise ValueError("Card bout IDs must be unique")
    statistics_sha = _sha(statistics_db)
    result = {
        "schema_version": 1, "artifact_type": "descriptive_prior_fight_history",
        "research_only": True, "model_features_reproduced": False,
        "source_report_sha256": _sha(report_path), "source_card_sha256": _sha(card_path),
        "forecast_id": report["forecast"].get("forecast_id"),
        "forecast_cutoff_at_utc": report["forecast"]["cutoff_at_utc"],
        "target_event_date": event_date.isoformat(), "strictly_before_date": before_date,
        "max_fights_per_fighter": max_fights,
        "ordering": "Latest available prior fights, then displayed oldest to newest; rounds in source round order.",
        "limitations": [
            "The two series have separate opponents and dates; matching bar indices are not the same bout, round or shared timeline.",
            "These are descriptive archived observations, not live fight momentum or recomputed model features.",
            "Only whole dates strictly before the forecast cutoff date and the target event's two-day embargo enter this asset.",
            "Unavailable fights are not padded. Missing counts or unknown validated duration produce null rates, not zero or assumed five-minute exposure.",
            "Archive capture dates do not establish source availability before every historical fight.",
        ], "fighters": {},
    }
    with HistoryStore(statistics_db) as store:
        captured = store.metadata.get("captured_at_utc")
        if not captured or _time(captured) > cutoff:
            raise ValueError("Statistics capture must precede the frozen forecast cutoff")
        result["source"] = {
            "statistics_database_sha256": statistics_sha,
            "statistics_store_content_sha256": store.metadata["store_content_sha256"],
            "archive_manifest_sha256": store.metadata["source_manifest_sha256"],
            "archive_repository_url": store.metadata["repository_url"],
            "archive_repository_commit": store.metadata["repository_commit"],
            "captured_at_utc": captured, "normalization_protocol": store.metadata["protocol"],
        }
        for bout in report["bouts"]:
            manifest = by_bout.get(bout["bout_id"])
            if not manifest:
                raise ValueError("Forecast bout lacks an explicit card identity")
            for side in ("fighter_a", "fighter_b"):
                name = bout[side]
                identity = manifest.get(side)
                if not isinstance(identity, dict) or identity.get("name") != name or not identity.get("fighter_id"):
                    raise ValueError("Explicit card identity and report fighter differ")
                profile = store.fighter_profile(identity["fighter_id"])
                if not profile or profile["name"] != name:
                    raise ValueError("An established exact canonical identity is required")
                if name in result["fighters"]:
                    raise ValueError("Repeated fighter in the dashboard roster")
                records = store.prior_fighter_bouts(profile["fighter_id"], before_date)
                fighter = {"fighter_id": profile["fighter_id"], "name": name,
                           "available_prior_bouts": len(records), "selected_bouts": min(max_fights, len(records)),
                           "source_url": profile["source_url"], "fights": []}
                for observation in records[-max_fights:]:
                    opponent = store.fighter_profile(observation.opponent_id)
                    if not opponent:
                        raise ValueError("Prior opponent lacks a canonical identity")
                    event = store.event(observation.event_id)
                    seconds = observation.elapsed_seconds
                    landed, absorbed = observation.totals["sig_landed"], observation.opponent_totals["sig_landed"]
                    item = {"bout_id": observation.bout_id, "event_date": observation.event_date,
                            "event_name": event["name"], "opponent": opponent["name"],
                            "opponent_id": observation.opponent_id, "elapsed_seconds": seconds,
                            "significant_strikes_landed": landed, "significant_strikes_absorbed": absorbed,
                            "significant_strikes_landed_per_minute": _rate(landed, seconds),
                            "significant_strikes_absorbed_per_minute": _rate(absorbed, seconds),
                            "source_url": observation.source_url, "rounds": []}
                    rounds = store.bout_rounds(observation.bout_id)
                    opponents = {row["round_number"]: row for row in rounds if row["fighter_id"] == observation.opponent_id}
                    for row in rounds:
                        if row["fighter_id"] != profile["fighter_id"]:
                            continue
                        opposing = opponents.get(row["round_number"])
                        count = row["totals"]["sig_landed"]
                        opposing_count = opposing["totals"]["sig_landed"] if opposing else None
                        item["rounds"].append({"round_number": row["round_number"],
                            "elapsed_seconds": row["elapsed_seconds"],
                            "significant_strikes_landed": count, "significant_strikes_absorbed": opposing_count,
                            "significant_strikes_landed_per_minute": _rate(count, row["elapsed_seconds"]),
                            "significant_strikes_absorbed_per_minute": _rate(opposing_count, row["elapsed_seconds"]),
                            "source_row_number": row["source_row_number"]})
                    fighter["fights"].append(item)
                result["fighters"][name] = fighter
    if _sha(statistics_db) != statistics_sha:
        raise ValueError("Statistics store changed during export")
    json.dumps(result, allow_nan=False)
    return result


def _public_text(value, *, maximum=1000):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(character) < 32 for character in value)
            or re.search(r"file://|/(?:Users|home|private|tmp|var|Volumes)/|\b[A-Za-z]:[\\/]|"
                         r"\b(?:API_KEY|ACCESS_TOKEN|SECRET|PASSWORD)\s*[=:]", value, re.I)):
        raise ValueError("Invalid public history text")
    return value


def _public_url(value):
    value = _public_text(value, maximum=2000)
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            or (parsed.scheme == "http" and parsed.hostname != "ufcstats.com")
            or any(re.search(r"key|token|secret|password|credential", key, re.I) for key, _ in parse_qsl(parsed.query))):
        raise ValueError("Invalid public history source")
    return value


def _integer(value, *, nullable=False, minimum=0):
    if value is None and nullable:
        return None
    if type(value) is not int or value < minimum or value > 10_000_000:
        raise ValueError("Invalid observed history count")
    return value


def _date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Invalid history date")
    return date.fromisoformat(value)


def _validated_counts(row):
    if not isinstance(row, dict):
        raise ValueError("History observations must be objects")
    result = {"elapsed_seconds": _integer(row.get("elapsed_seconds"), nullable=True)}
    for field in ("significant_strikes_landed", "significant_strikes_absorbed"):
        count = _integer(row.get(field), nullable=True)
        rate = row.get(field + "_per_minute")
        expected = _rate(count, result["elapsed_seconds"])
        if expected is None:
            if rate is not None:
                raise ValueError("Unknown exposure cannot supply a per-minute rate")
        elif (isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate)
              or rate < 0 or not math.isclose(rate, expected, rel_tol=1e-9, abs_tol=1e-9)):
            raise ValueError("History rate differs from observed count and exposure")
        result[field], result[field + "_per_minute"] = count, rate
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate history field")
        result[key] = value
    return result


def load_dashboard_history(path, *, report_sha256, cutoff_at_utc):
    """Load bound public chart data, failing closed without leaking local paths."""
    unavailable = {"status": "unavailable", "fighters": {}, "limitations": [],
                   "reason": "Saved earlier-fight history could not be verified for this forecast."}
    try:
        if not isinstance(report_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", report_sha256):
            raise ValueError("Invalid report fingerprint")
        cutoff = _time(cutoff_at_utc)
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
            raise ValueError("History artifact missing or unbounded")
        with path.open("rb") as stream:
            content = stream.read(5_000_001)
        if len(content) > 5_000_000:
            raise ValueError("History artifact unbounded")
        raw = json.loads(content, object_pairs_hook=_unique_object)
        if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1
                or raw.get("artifact_type") != "descriptive_prior_fight_history" or raw.get("research_only") is not True
                or raw.get("model_features_reproduced") is not False
                or raw.get("source_report_sha256") != report_sha256
                or _time(raw.get("forecast_cutoff_at_utc")) != cutoff):
            raise ValueError("History and forecast do not match")
        before = min(cutoff.date(), _date(raw.get("target_event_date")) - timedelta(days=2))
        if _date(raw.get("strictly_before_date")) != before:
            raise ValueError("History date policy changed")
        maximum = _integer(raw.get("max_fights_per_fighter"), minimum=1)
        if maximum > 5:
            raise ValueError("History window exceeds five fights")
        source = raw.get("source")
        if not isinstance(source, dict) or _time(source.get("captured_at_utc")) > cutoff:
            raise ValueError("History source capture follows forecast cutoff")
        for key in ("statistics_database_sha256", "statistics_store_content_sha256", "archive_manifest_sha256"):
            if not isinstance(source.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", source[key]):
                raise ValueError("Invalid history source fingerprint")
        profiles = raw.get("fighters")
        if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 80:
            raise ValueError("History requires a bounded fighter roster")
        clean = {}
        canonical_ids = set()
        for name, profile in profiles.items():
            _public_text(name, maximum=120)
            if not isinstance(profile, dict) or profile.get("name") != name:
                raise ValueError("History fighter name differs")
            fighter_id = profile.get("fighter_id")
            if (not isinstance(fighter_id, str) or not re.fullmatch(r"ufcstats:fighter:[0-9a-f]{16}", fighter_id)
                    or fighter_id in canonical_ids):
                raise ValueError("Invalid or repeated canonical history identity")
            canonical_ids.add(fighter_id)
            fights = profile.get("fights")
            selected = _integer(profile.get("selected_bouts"))
            available = _integer(profile.get("available_prior_bouts"))
            if not isinstance(fights, list) or len(fights) != selected or not selected <= min(maximum, available):
                raise ValueError("History fight window differs from stated availability")
            fighter = {"fighter_id": fighter_id, "name": name, "available_prior_bouts": available,
                       "selected_bouts": selected, "source_url": _public_url(profile.get("source_url")), "fights": []}
            seen, previous = set(), None
            for fight in fights:
                observed = _validated_counts(fight)
                observed_date = _date(fight.get("event_date"))
                bout_id = fight.get("bout_id")
                if not isinstance(bout_id, str) or not re.fullmatch(r"ufcstats:fight:[0-9a-f]{16}", bout_id):
                    raise ValueError("Invalid canonical history bout")
                order = (observed_date, bout_id)
                if observed_date >= before or bout_id in seen or previous is not None and order <= previous:
                    raise ValueError("History dates or ordering differ")
                seen.add(bout_id)
                previous = order
                opponent_id = fight.get("opponent_id")
                if (not isinstance(opponent_id, str) or not re.fullmatch(r"ufcstats:fighter:[0-9a-f]{16}", opponent_id)
                        or opponent_id == fighter_id):
                    raise ValueError("Invalid history opponent identity")
                observed.update({"bout_id": bout_id, "event_date": fight["event_date"],
                    "event_name": _public_text(fight.get("event_name")), "opponent": _public_text(fight.get("opponent"), maximum=120),
                    "opponent_id": opponent_id, "source_url": _public_url(fight.get("source_url")), "rounds": []})
                rounds = fight.get("rounds")
                if not isinstance(rounds, list) or not 1 <= len(rounds) <= 20:
                    raise ValueError("History needs observed rounds")
                for number, row in enumerate(rounds, 1):
                    round_value = _validated_counts(row)
                    if type(row.get("round_number")) is not int or row["round_number"] != number:
                        raise ValueError("History rounds must be complete and in order")
                    round_value.update(round_number=number, source_row_number=_integer(row.get("source_row_number"), minimum=1))
                    observed["rounds"].append(round_value)
                for field in ("elapsed_seconds", "significant_strikes_landed", "significant_strikes_absorbed"):
                    values = [row[field] for row in observed["rounds"]]
                    total = sum(values) if all(value is not None for value in values) else None
                    if observed[field] != total:
                        raise ValueError("History round totals differ from bout counts")
                fighter["fights"].append(observed)
            clean[name] = fighter
        limitations = raw.get("limitations")
        if not isinstance(limitations, list) or not 1 <= len(limitations) <= 30:
            raise ValueError("History limitations are required")
        return {"status": "available", "reason": None, "research_only": True,
                "model_features_reproduced": False, "fighters": clean,
                "limitations": [_public_text(value) for value in limitations],
                "artifact_sha256": hashlib.sha256(content).hexdigest(),
                "source_report_sha256": report_sha256, "forecast_cutoff_at_utc": raw["forecast_cutoff_at_utc"],
                "strictly_before_date": before.isoformat()}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return unavailable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--card", required=True)
    parser.add_argument("--statistics-db", required=True)
    args = parser.parse_args(argv)
    result = build_dashboard_history(args.report, args.card, args.statistics_db)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
