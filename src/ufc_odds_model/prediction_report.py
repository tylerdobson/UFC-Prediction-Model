"""Public, printable prediction reports using only the Python standard library.

The input is a public presentation payload, never an unfiltered database row.
``validate_report`` returns a new, allow-listed JSON-compatible dictionary::

    {
      "schema_version": 1,
      "event": {"name": "Event name", "event_date": "2026-11-01",
                "start_time_utc": "2026-11-01T20:00:00Z", "source_url": "https://..."},
      "generated_at_utc": "2026-10-25T12:00:00Z",
      "forecast": {"model_version": "logistic-prior-v3:...", "model_label": "V3",
                   "cutoff_at_utc": "2026-10-25T12:00:00Z", "research_only": True,
                   "status": "research_retrospective", "forecast_id": "optional public id"},
      "bouts": [{"bout_id": "one", "fighter_a": "A", "fighter_b": "B",
                 "p_fighter_a": 0.6, "p_fighter_b": 0.4,
                 "weight_class": "Lightweight", "scheduled_rounds": 3,
                 "contributions": [{"feature": "elo_difference_400", "label": "Elo history gap",
                                    "logit_contribution": 0.2}],
                 "coverage": {"fighter_a": {"prior_ufc_bouts": 5, "age": True},
                              "fighter_b": {"prior_ufc_bouts": 3, "age": False}},
                 "market": None, "notes": []}],
      "evaluation": {"label": "Retrospective fixed test", "bouts": 1447,
                     "accuracy": 0.5743, "log_loss": 0.67123, "brier_score": 0.23946,
                     "period": "2023–2026", "baseline_label": "V2", "baseline_log_loss": 0.68779},
      "unscored_bouts": [{"fighter_a": "C", "fighter_b": "D", "weight_class": "Lightweight",
                          "reason": "Stable identity has not been verified."}],
      "source_notes": ["Reviewed card; source retained by the operator."],
      "limitations": ["Missing optional inputs contribute neutrally."],
      "sources": [{"label": "Public source", "url": "https://..."}]
    }

Evaluation also accepts baseline_accuracy, paired_log_loss_interval=[low,high],
rolling={bouts,accuracy,log_loss,baseline_log_loss},
recent_year={year,bouts,accuracy,log_loss}, and note. A reused benchmark must be
identified in note; these are supplied saved measurements, never fitted here.

Optional bout.measurements.fighter_a/fighter_b contain source_url,
captured_at_utc, source_label, source_verified=True, and facts=[{key,value,
used_by_model:False}]. Fact keys are listed in MEASUREMENT_FIELDS. Percent
keys ending in _pct use 0–100; reach/height use centimeters. A current career
average is descriptive even if the model separately uses earlier-fight stats.
Model-used facts require matching coverage and capture before forecast cutoff.
Descriptive captures may follow the cutoff but must precede report generation.
Coverage may also retain prior_fights_with_stats, reach_cm, and reach_source
(dated_profile or retrospective_archive). Earlier-fight counts describe model
input history; they do not make the displayed current career averages inputs.

An optional market requires ``source_verified is True`` and ``fresh is True``,
complementary probabilities, bookmaker, captured_at_utc and
bookmaker_updated_at_utc. Its update <= capture <= forecast cutoff, and capture
must be within ``max_age_seconds`` of that cutoff (default 60; maximum 3600).
Unverified/stale market objects are discarded, so exports say unavailable.
Freshness describes the forecast cutoff, never the reader's current time.

HTML contains no remote assets or JavaScript. CSV neutralizes formula-like text.
Unknown fields are dropped. Local paths, credential-shaped text and credential
URL parameters in accepted public text are rejected rather than exported.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit


_STATUSES = {
    "prospective_research": "Prospective research forecast",
    "research_retrospective": "Forecast using retrospective research history",
    "recovered_research": "Recovered historical research forecast",
}
_PRIVATE_TEXT = re.compile(
    r"(?:file://|/(?:Users|home|private|tmp|var|Volumes)/|\b[A-Za-z]:[\\/]|"
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|Bearer\s+\S+)|"
    r"\b(?:[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|SECRET|PASSWORD))\s*[=:]\s*\S+)", re.I
)
_SENSITIVE_PARAMETER = re.compile(r"(?:key|token|secret|password|credential|signature|authorization)", re.I)
_FACTOR_LABELS = {
    "elo_difference_400": "Elo result history",
    "log_prior_bouts_difference": "UFC experience",
    "smoothed_win_rate_difference": "UFC win-rate history",
    "rest_years_difference": "Time since the previous fight",
    "known_rest_difference": "Available fight history",
    "recent_form_5_difference": "Recent result history",
    "age_years_difference_10": "Age comparison",
    "reach_cm_difference_20": "Reach comparison",
    "opponent_adjusted_sig_strike_accuracy_difference": "Adjusted striking accuracy",
    "log_stat_bouts_difference": "Recorded striking history",
    "age_decline_32_difference": "The model's age-above-32 term",
}
_REQUIRED_LIMITS = (
    "These probabilities are research estimates, not certainty or betting advice. No wager is authorized by this report.",
    "Retrospective model evaluation does not establish a live betting edge. Bout winner probabilities do not predict method or round.",
)
MEASUREMENT_FIELDS = {
    "reach_cm": ("Reach", "cm", 80, 260),
    "height_cm": ("Height", "cm", 100, 250),
    "significant_strikes_landed_per_minute": ("Sig. strikes landed", "/ min", 0, 100),
    "significant_strikes_absorbed_per_minute": ("Sig. strikes absorbed", "/ min", 0, 100),
    "significant_strike_accuracy_display_pct": ("Striking accuracy", "%", 0, 100),
    "significant_strike_defense_pct": ("Striking defense", "%", 0, 100),
    "takedowns_average_per_15_minutes": ("Takedowns", "/ 15 min", 0, 100),
    "takedown_accuracy_display_pct": ("Takedown accuracy", "%", 0, 100),
    "takedown_defense_pct": ("Takedown defense", "%", 0, 100),
    "submissions_average_per_15_minutes": ("Submission attempts", "/ 15 min", 0, 100),
    "knockdowns_average_per_15_minutes": ("Knockdowns", "/ 15 min", 0, 100),
}


def _text(value: Any, field: str, *, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a nonempty public string of at most {limit} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value) or _PRIVATE_TEXT.search(value):
        raise ValueError(f"{field} contains a local path, private credential or invalid character")
    return value.strip()


def _number(value: Any, field: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f"{field} is outside its allowed range")
    return float(value)


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int = 1000000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _utc(value: Any, field: str) -> str:
    value = _text(value, field, limit=50)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp with timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _url(value: Any, field: str) -> str:
    value = _text(value, field, limit=2000)
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
            or any(_SENSITIVE_PARAMETER.search(key) for key, _ in parse_qsl(parsed.query))):
        raise ValueError(f"{field} must be a public HTTPS URL without credentials")
    return value


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError(f"{field} must be a list of at most 50 strings")
    return [_text(item, field) for item in value]


def _probabilities(value: Mapping[str, Any], field: str) -> dict[str, float]:
    a = _number(value.get("p_fighter_a"), field + ".p_fighter_a", low=0, high=1)
    b = _number(value.get("p_fighter_b"), field + ".p_fighter_b", low=0, high=1)
    if not math.isclose(a + b, 1, rel_tol=0, abs_tol=1e-8):
        raise ValueError(f"{field} probabilities must sum to one")
    return {"p_fighter_a": a, "p_fighter_b": b}


def _coverage(value: Any) -> dict[str, dict[str, Any]]:
    value = _mapping(value, "coverage")
    result: dict[str, dict[str, Any]] = {}
    for side in ("fighter_a", "fighter_b"):
        row = _mapping(value.get(side, {}), "coverage." + side)
        result[side] = {}
        if "prior_ufc_bouts" in row:
            result[side]["prior_ufc_bouts"] = _integer(row["prior_ufc_bouts"], "prior_ufc_bouts")
        if "prior_fights_with_stats" in row:
            count = _integer(row["prior_fights_with_stats"], "prior_fights_with_stats")
            if "prior_ufc_bouts" in row and count > result[side]["prior_ufc_bouts"]:
                raise ValueError("Prior fights with stats cannot exceed recorded prior UFC bouts")
            if row.get("fight_statistics") is True and count == 0 and not row.get("prior_rich_stat_bouts"):
                raise ValueError("Available earlier-fight statistics require a positive prior-fight count")
            result[side]["prior_fights_with_stats"] = count
        if "prior_rich_stat_bouts" in row:
            result[side]["prior_rich_stat_bouts"] = _integer(row["prior_rich_stat_bouts"], "prior_rich_stat_bouts")
        if "rich_stat_minutes" in row:
            result[side]["rich_stat_minutes"] = _number(row["rich_stat_minutes"], "rich_stat_minutes", low=0)
        if "reach_cm" in row:
            result[side]["reach_cm"] = None if row["reach_cm"] is None else _number(row["reach_cm"], "coverage.reach_cm", low=80, high=260)
            if row.get("reach") is True and row["reach_cm"] is None:
                raise ValueError("Available model reach requires a recorded reach value")
        if "reach_source" in row:
            if not isinstance(row["reach_source"], str) or row["reach_source"] not in {"dated_profile", "retrospective_archive"}:
                raise ValueError("Model reach_source must identify a dated profile or retrospective archive")
            result[side]["reach_source"] = row["reach_source"]
        for key in ("age", "reach", "fight_statistics"):
            if key in row:
                if type(row[key]) is not bool:
                    raise ValueError(f"coverage.{side}.{key} must be boolean")
                result[side][key] = row[key]
    return result


def _market(value: Any, cutoff: str) -> dict[str, Any] | None:
    if value is None:
        return None
    value = _mapping(value, "market")
    if value.get("source_verified") is not True or value.get("fresh") is not True:
        return None
    result: dict[str, Any] = {
        **_probabilities(value, "market"), "source_verified": True, "fresh": True,
        "bookmaker": _text(value.get("bookmaker"), "market.bookmaker", limit=120),
        "captured_at_utc": _utc(value.get("captured_at_utc"), "market.captured_at_utc"),
        "bookmaker_updated_at_utc": _utc(value.get("bookmaker_updated_at_utc"), "market.bookmaker_updated_at_utc"),
        "max_age_seconds": _integer(value.get("max_age_seconds", 60), "market.max_age_seconds", minimum=1, maximum=3600),
    }
    updated = datetime.fromisoformat(result["bookmaker_updated_at_utc"].replace("Z", "+00:00"))
    captured = datetime.fromisoformat(result["captured_at_utc"].replace("Z", "+00:00"))
    forecast_time = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if not updated <= captured <= forecast_time:
        raise ValueError("Market timestamp order must be update <= capture <= forecast cutoff")
    if (forecast_time - updated).total_seconds() > result["max_age_seconds"]:
        return None
    if value.get("source_url"):
        result["source_url"] = _url(value["source_url"], "market.source_url")
    return result


def _measurements(value: Any, cutoff: str, generated: str, coverage: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(value, "measurements")
    result = {}
    for side in ("fighter_a", "fighter_b"):
        if side not in value:
            continue
        profile = _mapping(value[side], "measurements." + side)
        if profile.get("source_verified") is not True:
            raise ValueError("Athlete measurements require a verified source")
        captured = _utc(profile.get("captured_at_utc"), "measurements.captured_at_utc")
        if captured > generated:
            raise ValueError("Athlete measurement capture cannot follow report generation")
        facts = profile.get("facts")
        if not isinstance(facts, list) or not 1 <= len(facts) <= len(MEASUREMENT_FIELDS):
            raise ValueError("Athlete measurements require a bounded list of known facts")
        clean_facts, seen = [], set()
        for fact in facts:
            fact = _mapping(fact, "measurement.fact")
            key = fact.get("key")
            if not isinstance(key, str) or key not in MEASUREMENT_FIELDS or key in seen:
                raise ValueError("Measurement keys must be known and unique per fighter")
            seen.add(key)
            _, _, low, high = MEASUREMENT_FIELDS[key]
            number = _number(fact.get("value"), "measurement." + key, low=low, high=high)
            used = fact.get("used_by_model")
            if type(used) is not bool:
                raise ValueError("Each measured fact must explicitly state used_by_model")
            if used:
                if captured > cutoff:
                    raise ValueError("Model-used measurements must be captured before forecast cutoff")
                coverage_key = "reach" if key == "reach_cm" else "fight_statistics" if key not in {"height_cm"} else None
                if coverage_key is None or coverage[side].get(coverage_key) is not True:
                    raise ValueError("Model-used measurements must agree with saved input coverage")
            clean_facts.append({"key": key, "value": number, "used_by_model": used})
        result[side] = {"source_url": _url(profile.get("source_url"), "measurements.source_url"),
                        "source_label": _text(profile.get("source_label", "Official athlete profile"), "measurements.source_label", limit=200),
                        "captured_at_utc": captured, "source_verified": True, "facts": clean_facts,
                        "observed_after_forecast": captured > cutoff}
    return result


def validate_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and project a report onto its public schema; never modify input."""
    report = _mapping(report, "report")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("Unsupported prediction report schema_version")
    event = _mapping(report.get("event"), "event")
    event_date = _text(event.get("event_date"), "event.event_date", limit=10)
    try:
        if date.fromisoformat(event_date).isoformat() != event_date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("event.event_date must be YYYY-MM-DD") from exc
    clean_event = {"name": _text(event.get("name"), "event.name", limit=200), "event_date": event_date}
    if event.get("start_time_utc"):
        clean_event["start_time_utc"] = _utc(event["start_time_utc"], "event.start_time_utc")
    if event.get("source_url"):
        clean_event["source_url"] = _url(event["source_url"], "event.source_url")
    forecast = _mapping(report.get("forecast"), "forecast")
    if forecast.get("research_only") is not True:
        raise ValueError("Prediction reports require explicit research_only=true")
    if forecast.get("status") not in _STATUSES:
        raise ValueError("Forecast status must explicitly identify its research basis")
    clean_forecast = {
        "model_version": _text(forecast.get("model_version"), "model_version", limit=200),
        "model_label": _text(forecast.get("model_label", forecast.get("model_version")), "model_label", limit=200),
        "cutoff_at_utc": _utc(forecast.get("cutoff_at_utc"), "forecast.cutoff_at_utc"),
        "research_only": True, "status": forecast["status"],
    }
    if forecast.get("forecast_id"):
        clean_forecast["forecast_id"] = _text(forecast["forecast_id"], "forecast_id", limit=200)
    generated = _utc(report.get("generated_at_utc"), "generated_at_utc")
    if generated < clean_forecast["cutoff_at_utc"]:
        raise ValueError("Report generation cannot precede the forecast cutoff")
    if clean_event.get("start_time_utc") and clean_forecast["cutoff_at_utc"] >= clean_event["start_time_utc"]:
        raise ValueError("Forecast cutoff must precede event start")
    bouts = report.get("bouts")
    if not isinstance(bouts, list) or not 1 <= len(bouts) <= 40:
        raise ValueError("bouts must contain between 1 and 40 forecast rows")
    clean_bouts = []
    seen = set()
    for raw in bouts:
        raw = _mapping(raw, "bout")
        row: dict[str, Any] = {
            "bout_id": _text(raw.get("bout_id"), "bout_id", limit=200),
            "fighter_a": _text(raw.get("fighter_a"), "fighter_a", limit=120),
            "fighter_b": _text(raw.get("fighter_b"), "fighter_b", limit=120),
            **_probabilities(raw, "bout"),
            "coverage": _coverage(raw.get("coverage", {})),
            "market": _market(raw.get("market"), clean_forecast["cutoff_at_utc"]),
            "notes": _strings(raw.get("notes", []), "bout.notes"),
        }
        if row["bout_id"] in seen or row["fighter_a"] == row["fighter_b"]:
            raise ValueError("Forecast rows require unique bout IDs and distinct fighters")
        seen.add(row["bout_id"])
        if raw.get("weight_class"):
            row["weight_class"] = _text(raw["weight_class"], "weight_class", limit=100)
        if raw.get("scheduled_rounds") is not None:
            row["scheduled_rounds"] = _integer(raw["scheduled_rounds"], "scheduled_rounds", minimum=1, maximum=5)
        if raw.get("measurements") is not None:
            row["measurements"] = _measurements(raw["measurements"], clean_forecast["cutoff_at_utc"], generated, row["coverage"])
        contributions = raw.get("contributions", [])
        if not isinstance(contributions, list) or len(contributions) > 50:
            raise ValueError("contributions must be a list of at most 50 factors")
        row["contributions"] = []
        for contribution in contributions:
            contribution = _mapping(contribution, "contribution")
            feature = _text(contribution.get("feature"), "contribution.feature", limit=120)
            row["contributions"].append({
                "feature": feature,
                "label": _text(contribution.get("label", feature), "contribution.label", limit=160),
                "logit_contribution": _number(contribution.get("logit_contribution"), "logit_contribution"),
            })
        clean_bouts.append(row)
    unscored = report.get("unscored_bouts", [])
    if not isinstance(unscored, list) or len(unscored) > 40:
        raise ValueError("unscored_bouts must be a list of at most 40 matchups")
    clean_unscored = []
    pair_names = {frozenset((row["fighter_a"].casefold(), row["fighter_b"].casefold())) for row in clean_bouts}
    for row in unscored:
        row = _mapping(row, "unscored_bout")
        item = {"fighter_a": _text(row.get("fighter_a"), "unscored_bout.fighter_a", limit=120),
                "fighter_b": _text(row.get("fighter_b"), "unscored_bout.fighter_b", limit=120),
                "reason": _text(row.get("reason"), "unscored_bout.reason")}
        pair = frozenset((item["fighter_a"].casefold(), item["fighter_b"].casefold()))
        if len(pair) != 2 or pair in pair_names:
            raise ValueError("Unscored matchups must identify distinct fighters and cannot duplicate a reported bout")
        pair_names.add(pair)
        if row.get("weight_class"):
            item["weight_class"] = _text(row["weight_class"], "unscored_bout.weight_class", limit=100)
        clean_unscored.append(item)
    result: dict[str, Any] = {
        "schema_version": 1, "event": clean_event, "generated_at_utc": generated,
        "forecast": clean_forecast, "bouts": clean_bouts, "unscored_bouts": clean_unscored,
        "source_notes": _strings(report.get("source_notes", []), "source_notes"),
        "limitations": _strings(report.get("limitations", []), "limitations"),
        "sources": [],
    }
    sources = report.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 100:
        raise ValueError("sources must be a list of at most 100 public sources")
    for source in sources:
        source = _mapping(source, "source")
        result["sources"].append({"label": _text(source.get("label"), "source.label", limit=200),
                                  "url": _url(source.get("url"), "source.url")})
    if report.get("evaluation") is not None:
        evaluation = _mapping(report["evaluation"], "evaluation")
        clean_evaluation: dict[str, Any] = {
            "label": _text(evaluation.get("label"), "evaluation.label", limit=200),
            "bouts": _integer(evaluation.get("bouts"), "evaluation.bouts", minimum=1),
            "accuracy": _number(evaluation.get("accuracy"), "evaluation.accuracy", low=0, high=1),
            "log_loss": _number(evaluation.get("log_loss"), "evaluation.log_loss", low=0),
            "brier_score": _number(evaluation.get("brier_score"), "evaluation.brier_score", low=0, high=1),
        }
        for field in ("period", "baseline_label", "note"):
            if evaluation.get(field):
                clean_evaluation[field] = _text(evaluation[field], "evaluation." + field, limit=1000 if field == "note" else 200)
        if evaluation.get("baseline_log_loss") is not None:
            clean_evaluation["baseline_log_loss"] = _number(evaluation["baseline_log_loss"], "baseline_log_loss", low=0)
        if evaluation.get("baseline_accuracy") is not None:
            clean_evaluation["baseline_accuracy"] = _number(evaluation["baseline_accuracy"], "baseline_accuracy", low=0, high=1)
        if evaluation.get("paired_log_loss_interval") is not None:
            interval = evaluation["paired_log_loss_interval"]
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise ValueError("paired_log_loss_interval must contain two finite bounds")
            interval = [_number(value, "paired_log_loss_interval") for value in interval]
            if interval[0] > interval[1]:
                raise ValueError("paired_log_loss_interval bounds must be ordered")
            clean_evaluation["paired_log_loss_interval"] = interval
        for key in ("rolling", "recent_year"):
            if evaluation.get(key) is not None:
                values = _mapping(evaluation[key], "evaluation." + key)
                summary: dict[str, Any] = {
                    "bouts": _integer(values.get("bouts"), key + ".bouts", minimum=1),
                    "accuracy": _number(values.get("accuracy"), key + ".accuracy", low=0, high=1),
                    "log_loss": _number(values.get("log_loss"), key + ".log_loss", low=0),
                }
                if key == "rolling":
                    summary["baseline_log_loss"] = _number(values.get("baseline_log_loss"), "rolling.baseline_log_loss", low=0)
                else:
                    summary["year"] = _integer(values.get("year"), "recent_year.year", minimum=1993, maximum=2200)
                clean_evaluation[key] = summary
        result["evaluation"] = clean_evaluation
    return result


def _favorite(bout: Mapping[str, Any]) -> tuple[str, float, str]:
    probability = max(bout["p_fighter_a"], bout["p_fighter_b"])
    if math.isclose(probability, 0.5, abs_tol=1e-10):
        return "Even matchup", probability, "Even"
    name = bout["fighter_a"] if bout["p_fighter_a"] > 0.5 else bout["fighter_b"]
    strength = "Near even" if probability < 0.55 else "Small lean" if probability < 0.60 else "Moderate lean" if probability < 0.70 else "Strong lean"
    return name, probability, strength


def _coverage_text(bout: Mapping[str, Any], side: str) -> str:
    coverage = bout["coverage"][side]
    result = []
    if "prior_ufc_bouts" in coverage:
        count = coverage["prior_ufc_bouts"]
        result.append(f"{count} prior UFC fight{'s' if count != 1 else ''}")
    if "prior_fights_with_stats" in coverage:
        count = coverage["prior_fights_with_stats"]
        result.append(f"{count} earlier fight{'s' if count != 1 else ''} with strike counts")
    if "prior_rich_stat_bouts" in coverage:
        count = coverage["prior_rich_stat_bouts"]
        result.append(f"{count} earlier archive fight{'s' if count != 1 else ''} with detailed statistics")
    if "rich_stat_minutes" in coverage:
        result.append(f"{coverage['rich_stat_minutes']:.1f} minutes in the selected statistics window")
    facts = {fact["key"]: fact for fact in bout.get("measurements", {}).get(side, {}).get("facts", [])}
    for key, label in (("age", "age"), ("reach", "reach"), ("fight_statistics", "fight stats")):
        if coverage.get(key) is True:
            if key == "reach" and coverage.get("reach_cm") is not None:
                source = {"dated_profile": "dated profile", "retrospective_archive": "retrospective archive"}.get(coverage.get("reach_source"))
                result.append(f"reach {coverage['reach_cm']:.1f} cm available to model" + (f" ({source})" if source else ""))
            else:
                result.append(f"{'earlier-fight stats' if key == 'fight_statistics' and (facts or 'prior_fights_with_stats' in coverage) else label} available to model")
        elif (key == "reach" and "reach_cm" in facts) or (key == "fight_statistics" and any(name not in {"height_cm", "reach_cm"} for name in facts)):
            result.append(f"{label} observed, not used by model")
        else:
            result.append(f"{label} missing" if key in coverage else f"{label} not recorded")
    return " · ".join(result)


def measurement_rows(bout: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Return source-backed A / labelled-unit / B rows for HTML or PDF."""
    profiles = bout.get("measurements", {})
    facts = {side: {fact["key"]: fact for fact in profiles.get(side, {}).get("facts", [])}
             for side in ("fighter_a", "fighter_b")}
    result = []
    for key, (label, unit, _, _) in MEASUREMENT_FIELDS.items():
        if not any(key in facts[side] for side in facts):
            continue
        cells = []
        for side in ("fighter_a", "fighter_b"):
            fact = facts[side].get(key)
            if fact is None:
                cells.append("Unavailable")
                continue
            precision = 1 if unit == "cm" else 0 if unit == "%" else 2
            cells.append(f"{fact['value']:.{precision}f}" + (" *" if fact["used_by_model"] else ""))
        result.append((cells[0], f"{label} ({unit})", cells[1]))
    return result


def measurement_note(bout: Mapping[str, Any]) -> str:
    """Explain the distinction between current observations and fitted inputs."""
    profiles = bout.get("measurements", {})
    used = any(fact["used_by_model"] for profile in profiles.values() for fact in profile["facts"])
    note = ("* Marked values were used in this forecast. Unmarked current career averages are descriptive; "
            "they are separate from the model's earlier-fight adjusted statistics." if used else
            "Current career averages are descriptive and are not used in these probabilities. "
            "Any model-used earlier-fight adjusted statistics are separate inputs.")
    if any(profile.get("observed_after_forecast") for profile in profiles.values()):
        note += " Some descriptive observations were captured after the forecast freeze."
    return note


def _measurements_csv(bout: Mapping[str, Any], side: str) -> str:
    profile = bout.get("measurements", {}).get(side)
    if not profile:
        return "Unavailable"
    values = [f"{MEASUREMENT_FIELDS[fact['key']][0]}={fact['value']:g} {MEASUREMENT_FIELDS[fact['key']][1]} "
              f"({'model-used' if fact['used_by_model'] else 'descriptive only'})" for fact in profile["facts"]]
    return "; ".join(values) + f" | Source: {profile['source_url']} | Captured: {profile['captured_at_utc']}"


def _factor_texts(bout: Mapping[str, Any]) -> list[str]:
    result = []
    for item in sorted(bout["contributions"], key=lambda item: abs(item["logit_contribution"]), reverse=True)[:3]:
        if abs(item["logit_contribution"]) < 1e-12:
            continue
        side = "fighter_a" if item["logit_contribution"] > 0 else "fighter_b"
        label = _FACTOR_LABELS.get(item["feature"], item["label"])
        result.append(f"{label} favors {bout[side]}.")
    return result


def render_json(report: Mapping[str, Any]) -> str:
    """Return the validated public report as strict, readable JSON."""
    return json.dumps(validate_report(report), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _csv_text(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def render_csv(report: Mapping[str, Any]) -> str:
    """Return one public forecast per row, with formula-safe text cells."""
    report = validate_report(report)
    fields = ["event", "event_date", "research_only", "forecast_basis", "model_version", "cutoff_at_utc", "generated_at_utc", "forecast_status",
              "bout_id", "fighter_a", "fighter_b", "p_fighter_a", "p_fighter_b", "favorite", "favorite_probability",
              "forecast_strength", "market_p_fighter_a", "market_p_fighter_b", "market_bookmaker", "market_captured_at_utc",
              "fighter_a_coverage", "fighter_b_coverage", "model_factors", "notes",
              "scored_bouts", "listed_bouts", "not_scored_bouts", "event_source_url", "source_notes", "report_limitations", "evaluation_note",
              "fighter_a_observed_measurements", "fighter_b_observed_measurements",
              "fighter_a_prior_fights_with_stats", "fighter_b_prior_fights_with_stats",
              "fighter_a_prior_rich_stat_bouts", "fighter_b_prior_rich_stat_bouts",
              "fighter_a_rich_stat_minutes", "fighter_b_rich_stat_minutes",
              "fighter_a_model_reach_cm", "fighter_b_model_reach_cm", "fighter_a_model_reach_source", "fighter_b_model_reach_source"]
    context = {
        "scored_bouts": len(report["bouts"]), "listed_bouts": len(report["bouts"]) + len(report["unscored_bouts"]),
        "not_scored_bouts": len(report["unscored_bouts"]), "event_source_url": report["event"].get("source_url", "Unavailable"),
        "source_notes": " | ".join(report["source_notes"]),
        "report_limitations": " | ".join(dict.fromkeys([*report["limitations"], *_REQUIRED_LIMITS])),
        "evaluation_note": report.get("evaluation", {}).get("note", "No additional evaluation context supplied."),
    }
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for bout in report["bouts"]:
        favorite, probability, strength = _favorite(bout)
        market = bout["market"] or {}
        values = dict(zip(fields, [
            report["event"]["name"], report["event"]["event_date"], True, report["forecast"]["status"],
            report["forecast"]["model_version"], report["forecast"]["cutoff_at_utc"], report["generated_at_utc"], "scored",
            bout["bout_id"], bout["fighter_a"], bout["fighter_b"], bout["p_fighter_a"], bout["p_fighter_b"],
            favorite, probability, strength, market.get("p_fighter_a", "Unavailable"), market.get("p_fighter_b", "Unavailable"),
            market.get("bookmaker", "Unavailable"), market.get("captured_at_utc", "Unavailable"),
            _coverage_text(bout, "fighter_a"), _coverage_text(bout, "fighter_b"), "; ".join(_factor_texts(bout)), "; ".join(bout["notes"]),
        ]))
        measurements = {"fighter_a_observed_measurements": _measurements_csv(bout, "fighter_a"),
                        "fighter_b_observed_measurements": _measurements_csv(bout, "fighter_b")}
        coverage = {f"{side}_{name}": bout["coverage"][side].get(key, "")
                    for side in ("fighter_a", "fighter_b")
                    for name, key in (("prior_fights_with_stats", "prior_fights_with_stats"),
                                      ("prior_rich_stat_bouts", "prior_rich_stat_bouts"),
                                      ("rich_stat_minutes", "rich_stat_minutes"),
                                      ("model_reach_cm", "reach_cm"), ("model_reach_source", "reach_source"))}
        writer.writerow({key: _csv_text(value) for key, value in {**values, **context, **measurements, **coverage}.items()})
    for bout in report["unscored_bouts"]:
        values = {"event": report["event"]["name"], "event_date": report["event"]["event_date"], "research_only": True,
                  "forecast_basis": report["forecast"]["status"], "model_version": report["forecast"]["model_version"],
                  "cutoff_at_utc": report["forecast"]["cutoff_at_utc"], "generated_at_utc": report["generated_at_utc"],
                  "forecast_status": "not_scored", "fighter_a": bout["fighter_a"], "fighter_b": bout["fighter_b"], "notes": bout["reason"]}
        writer.writerow({key: _csv_text(value) for key, value in {**values, **context}.items()})
    return stream.getvalue()


_CSS = """
:root{--ink:#142329;--muted:#56686d;--line:#d9e0de;--teal:#14675e;--wash:#eef3f1;--paper:#fff;--red:#a24c35}
*{box-sizing:border-box}html{background:#e8eeeb}body{margin:0;color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.report{max-width:1050px;margin:32px auto;background:var(--paper);padding:42px 52px 34px}.masthead{display:flex;justify-content:space-between;align-items:center;border-bottom:3px solid var(--ink);padding-bottom:13px;gap:20px}.brand{font-size:15px;font-weight:850;letter-spacing:.1em;text-transform:uppercase}.edition{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);text-align:right}.eyebrow{color:var(--teal);font-size:10px;letter-spacing:.16em;text-transform:uppercase;font-weight:800;margin:27px 0 7px}h1{font:800 48px/.99 "Arial Narrow",Arial,sans-serif;letter-spacing:-2px;margin:0 0 15px;max-width:840px}h2{font:700 23px/1.2 Georgia,"Times New Roman",serif;letter-spacing:-.45px;margin:0}p{margin:0}.dek{font-size:15px;color:var(--muted);max-width:780px;line-height:1.55}.event-meta{display:flex;gap:22px;flex-wrap:wrap;margin-top:18px;font-size:11px;color:var(--muted)}.event-meta strong{color:var(--ink);font-weight:650}.research-label{margin-top:22px;padding:10px 13px;border-left:3px solid var(--teal);background:var(--wash);font-size:11px;line-height:1.5}.research-label strong{color:var(--teal)}.summary{display:grid;grid-template-columns:repeat(3,1fr);margin:24px 0 25px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.stat{padding:14px 18px 14px 0}.stat+.stat{border-left:1px solid var(--line);padding-left:24px}.stat-value{font-size:28px;font-weight:750;line-height:1.1;letter-spacing:-.9px}.stat-label{font-size:10px;color:var(--muted);margin-top:6px;text-transform:uppercase;letter-spacing:.09em}.stat-detail{font-size:10px;color:var(--muted);margin-top:3px}.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:20px;margin:27px 0 10px;padding-bottom:10px;border-bottom:1px solid var(--ink)}.section-head p{font-size:10px;color:var(--muted);text-align:right}.bouts{display:grid;grid-template-columns:1fr 1fr;column-gap:30px}.bout{padding:15px 0 17px;border-bottom:1px solid var(--line);break-inside:avoid;page-break-inside:avoid;min-width:0}.bout-top{display:flex;gap:8px;justify-content:space-between;align-items:baseline;font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:9px}.bout-number{color:var(--teal);font-weight:800}.strength{font-weight:700;color:var(--teal);white-space:nowrap}.fighter{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:baseline;margin:7px 0 3px}.fighter-name{font-size:16px;font-weight:650;line-height:1.2}.fighter-probability{font-size:19px;line-height:1.1;font-weight:750;letter-spacing:-.5px}.fighter.underdog .fighter-name,.fighter.underdog .fighter-probability{color:var(--muted);font-weight:450}.probability-track{height:5px;background:#e5ebe8;overflow:hidden}.probability-fill{height:100%;background:var(--teal)}.underdog-fill{background:#899d96}.factors{padding:0;list-style:none;margin:12px 0 9px;font-size:10px;line-height:1.55;color:var(--ink)}.factors li{padding-left:10px;position:relative;margin-top:3px}.factors li:before{content:"·";position:absolute;left:0;color:var(--teal);font-weight:900}.coverage{font-size:9px;color:var(--muted);line-height:1.5;margin-top:5px}.coverage strong{color:var(--ink);font-weight:600}.market{font-size:9px;color:var(--muted);margin-top:9px;padding-top:7px;border-top:1px dotted var(--line)}.market strong{color:var(--ink);font-weight:600}.bout-note{font-size:9px;color:var(--red);line-height:1.45;margin-top:6px}.explainer{font-size:10px;color:var(--muted);line-height:1.65;margin-top:12px}.evidence-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:30px;margin-top:15px;break-inside:avoid;page-break-inside:avoid}.evidence-grid h3{font-size:12px;margin:0 0 8px}.metric-table{width:100%;border-collapse:collapse;font-size:11px}.metric-table th,.metric-table td{padding:6px 0;border-bottom:1px solid var(--line);text-align:left}.metric-table th{color:var(--muted);font-weight:450}.metric-table td{text-align:right;font-variant-numeric:tabular-nums}.small{font-size:10px;line-height:1.6;color:var(--muted)}.notes{padding-left:15px;margin:0}.notes li{margin:0 0 5px}.sources{margin-top:13px;font-size:9px;color:var(--muted);overflow-wrap:anywhere}.sources a{color:var(--teal);text-decoration:none}.sources ol{padding-left:16px;margin:5px 0}.sources li{padding:2px 0}.footer{border-top:2px solid var(--ink);margin-top:24px;padding-top:10px;display:flex;justify-content:space-between;gap:20px;font-size:9px;color:var(--muted)}.footer strong{color:var(--ink);letter-spacing:.08em;text-transform:uppercase}.model-id{overflow-wrap:anywhere}.print-tip{text-align:center;color:var(--muted);font-size:11px;margin:18px auto 26px}.no-data{color:var(--muted)}
.athlete-stats{margin:13px 0 11px;padding:10px 0 8px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.stats-title{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--teal);font-weight:750;margin-bottom:7px}.stats-table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:10px;line-height:1.35}.stats-table th,.stats-table td{padding:4px 3px;border-bottom:1px solid #edf0ee}.stats-table thead th{font-size:8px;font-weight:650;color:var(--ink);overflow-wrap:anywhere;text-align:center;width:28%}.stats-table thead th:nth-child(2){width:44%;font-weight:400;color:var(--muted)}.stats-table tbody th{text-align:center;font-size:9px;font-weight:400;color:var(--muted)}.stats-table td{font-variant-numeric:tabular-nums;font-weight:650;text-align:center}.stats-note{font-size:8px;line-height:1.5;color:var(--muted);margin-top:7px}.stats-source{font-size:8px;line-height:1.5;color:var(--muted);margin-top:4px;overflow-wrap:anywhere}.stats-source a{color:var(--teal);text-decoration:none}
@page{size:auto;margin:15mm 15mm 16mm} @media print{html,body{background:#fff;font-size:10pt}.report{max-width:none;width:100%;margin:0;padding:0}.masthead{padding-bottom:10px}.eyebrow{margin-top:20px}h1{font-size:34pt}.bouts{column-gap:7mm}.bout{padding-top:12px;padding-bottom:14px}.fighter-name{font-size:11pt}.fighter-probability{font-size:14pt}.section-head{break-after:avoid;page-break-after:avoid}.print-tip{display:none}a{color:inherit}.research-label,.summary,.evidence-grid,.sources,.footer{break-inside:avoid;page-break-inside:avoid}}
@media(max-width:700px){.report{margin:0;padding:24px 20px}.masthead{align-items:flex-start}.brand{font-size:12px}.edition{font-size:8px}h1{font-size:37px;letter-spacing:-1.2px}.bouts,.evidence-grid{grid-template-columns:1fr}.section-head{align-items:flex-start;gap:12px}.section-head p{max-width:150px}.stat-value{font-size:24px}.stat+.stat{padding-left:13px}.stat-label{font-size:8px}.stat-detail{font-size:9px}.event-meta{gap:8px 18px}.fighter-name{font-size:17px}}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _date_display(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _time_display(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year} · {parsed.strftime('%H:%M')} UTC"


def _measurements_html(bout: Mapping[str, Any]) -> str:
    rows = measurement_rows(bout)
    if not rows:
        return ""
    table = ("<section class='athlete-stats'><p class='stats-title'>Observed athlete statistics</p>"
             f"<table class='stats-table' aria-label='Source-backed athlete comparison'><thead><tr><th scope='col'>{_esc(bout['fighter_a'])}</th>"
             f"<th scope='col'>Metric</th><th scope='col'>{_esc(bout['fighter_b'])}</th></tr></thead><tbody>")
    table += "".join(f"<tr><td>{_esc(a)}</td><th scope='row'>{_esc(label)}</th><td>{_esc(b)}</td></tr>" for a, label, b in rows)
    table += f"</tbody></table><p class='stats-note'>{_esc(measurement_note(bout))}</p>"
    for side in ("fighter_a", "fighter_b"):
        profile = bout.get("measurements", {}).get(side)
        if profile:
            table += (f"<p class='stats-source'><a href='{_esc(profile['source_url'])}' rel='noreferrer'>{_esc(bout[side])} · {_esc(profile['source_label'])}</a>"
                      f" · observed {_esc(_time_display(profile['captured_at_utc']))}</p>")
    return table + "</section>"


def _bout_html(bout: Mapping[str, Any], index: int) -> str:
    favorite, probability, strength = _favorite(bout)
    details = []
    if bout.get("weight_class"):
        details.append(bout["weight_class"])
    if bout.get("scheduled_rounds"):
        details.append(f"{bout['scheduled_rounds']} rounds")
    fighters = []
    for side, key in (("a", "fighter_a"), ("b", "fighter_b")):
        p = bout["p_fighter_" + side]
        underdog = p < 0.5
        fighters.append(
            f"<div class='fighter{' underdog' if underdog else ''}'><span class='fighter-name'>{_esc(bout[key])}</span>"
            f"<span class='fighter-probability'>{p:.1%}</span></div>"
            f"<div class='probability-track' role='img' aria-label='{_esc(bout[key])}: {p:.1%} win probability'>"
            f"<div class='probability-fill{' underdog-fill' if underdog else ''}' style='width:{p*100:.6f}%'></div></div>"
        )
    factors = _factor_texts(bout)
    factors_html = "<ul class='factors'>" + "".join(f"<li>{_esc(line)}</li>" for line in factors) + "</ul>" if factors else "<p class='coverage'>Factor explanations unavailable.</p>"
    market = bout["market"]
    if market:
        market_html = (f"<strong>Market at cutoff:</strong> {_esc(bout['fighter_a'])} {market['p_fighter_a']:.1%} / "
                       f"{_esc(bout['fighter_b'])} {market['p_fighter_b']:.1%} · {_esc(market['bookmaker'])}, no vig. "
                       f"Observed {_esc(_time_display(market['captured_at_utc']))}.")
    else:
        market_html = "<strong>Market comparison:</strong> Unavailable — no source-verified two-sided price fresh at this forecast cutoff."
    coverage = "".join(f"<p class='coverage'><strong>{_esc(bout[side])}:</strong> {_esc(_coverage_text(bout, side))}</p>" for side in ("fighter_a", "fighter_b"))
    return (
        f"<article class='bout' aria-label='{_esc(bout['fighter_a'])} versus {_esc(bout['fighter_b'])}'>"
        f"<div class='bout-top'><span><span class='bout-number'>{index:02d}</span> &nbsp; {_esc(' · '.join(details) or 'Bout forecast')}</span>"
        f"<span class='strength'>{_esc(strength)}</span></div>" + "".join(fighters) + _measurements_html(bout) + factors_html + coverage
        + f"<p class='market'>{market_html}</p>"
        + "".join(f"<p class='bout-note'>{_esc(note)}</p>" for note in bout["notes"]) + "</article>"
    )


def render_html(report: Mapping[str, Any]) -> str:
    """Return a self-contained responsive report suitable for Print → Save PDF."""
    report = validate_report(report)
    event, forecast, bouts = report["event"], report["forecast"], report["bouts"]
    unscored = report["unscored_bouts"]
    near_even = sum(max(row["p_fighter_a"], row["p_fighter_b"]) < 0.55 for row in bouts)
    priced = sum(row["market"] is not None for row in bouts)
    intro = ("A bout-by-bout view of the model's estimated win probabilities. Read the gap between the fighters, "
             "then the result history, input coverage and limits behind it.")
    meta = f"<span><strong>Event</strong> {_esc(_date_display(event['event_date']))}</span>"
    if event.get("start_time_utc"):
        meta += f"<span><strong>Scheduled start</strong> {_esc(_time_display(event['start_time_utc']))}</span>"
    meta += f"<span><strong>Forecast cutoff</strong> {_esc(_time_display(forecast['cutoff_at_utc']))}</span>"
    evaluation = report.get("evaluation")
    if evaluation:
        rows = [("Accuracy", f"{evaluation['accuracy']:.2%}"), ("Log loss ↓", f"{evaluation['log_loss']:.5f}"), ("Brier score ↓", f"{evaluation['brier_score']:.5f}")]
        if "baseline_log_loss" in evaluation:
            rows.append((f"{evaluation.get('baseline_label', 'Baseline')} log loss ↓", f"{evaluation['baseline_log_loss']:.5f}"))
        if "baseline_accuracy" in evaluation:
            rows.append((f"{evaluation.get('baseline_label', 'Baseline')} accuracy", f"{evaluation['baseline_accuracy']:.2%}"))
        evaluation_html = f"<p class='small'>{_esc(evaluation['label'])} · {evaluation['bouts']:,} bouts" + (f" · {_esc(evaluation['period'])}" if evaluation.get("period") else "") + ".</p>"
        evaluation_html += "<table class='metric-table' aria-label='Saved model evaluation'><tbody>" + "".join(f"<tr><th scope='row'>{_esc(label)}</th><td>{_esc(value)}</td></tr>" for label, value in rows) + "</tbody></table>"
        evaluation_html += "<p class='small' style='margin-top:7px'>Lower log loss and Brier indicate better probability estimates. These scores describe the saved historical test, not this card's future accuracy.</p>"
        if "paired_log_loss_interval" in evaluation:
            low, high = evaluation["paired_log_loss_interval"]
            evaluation_html += (f"<p class='small' style='margin-top:7px'><strong>Paired log-loss difference, 95% interval:</strong> "
                                f"[{low:+.5f}, {high:+.5f}]. Negative values favor the reported model over its baseline.</p>")
        if "rolling" in evaluation:
            values = evaluation["rolling"]
            evaluation_html += (f"<p class='small' style='margin-top:7px'><strong>Rolling evaluation:</strong> {values['bouts']:,} bouts · "
                                f"{values['accuracy']:.2%} accuracy · {values['log_loss']:.5f} log loss "
                                f"(baseline {values['baseline_log_loss']:.5f}).</p>")
        if "recent_year" in evaluation:
            values = evaluation["recent_year"]
            evaluation_html += (f"<p class='small' style='margin-top:7px'><strong>{values['year']} subset:</strong> {values['bouts']:,} bouts · "
                                f"{values['accuracy']:.2%} accuracy · {values['log_loss']:.5f} log loss. This recent period may differ from the pooled result.</p>")
        if evaluation.get("note"):
            evaluation_html += f"<p class='small' style='margin-top:7px'>{_esc(evaluation['note'])}</p>"
    else:
        evaluation_html = "<p class='small'>No verified model evaluation was supplied for this report. Forecast probabilities alone do not establish model quality.</p>"
    limitations = list(dict.fromkeys([*report["limitations"], *_REQUIRED_LIMITS]))
    notes_html = "<ul class='notes small'>" + "".join(f"<li>{_esc(note)}</li>" for note in limitations) + "</ul>"
    source_notes = "".join(f"<p>{_esc(note)}</p>" for note in report["source_notes"])
    sources = list(report["sources"])
    if event.get("source_url") and not any(source["url"] == event["source_url"] for source in sources):
        sources.insert(0, {"label": "Event source", "url": event["source_url"]})
    source_list = "<ol>" + "".join(f"<li><a href='{_esc(source['url'])}' rel='noreferrer'>{_esc(source['label'])}</a> · {_esc(source['url'])}</li>" for source in sources) + "</ol>" if sources else ""
    fingerprint = f"<p class='model-id'><strong>Forecast ID:</strong> {_esc(forecast['forecast_id'])}</p>" if forecast.get("forecast_id") else ""
    unscored_html = ""
    if unscored:
        unscored_html = ("<section aria-labelledby='unscored-title'><div class='section-head'><h2 id='unscored-title'>Not scored</h2>"
                         f"<p>{len(unscored)} matchups · no probabilities assigned</p></div><div class='bouts'>")
        for row in unscored:
            unscored_html += (f"<article class='bout'><div class='bout-top'><span>{_esc(row.get('weight_class', 'Listed matchup'))}</span><span>Not scored</span></div>"
                              f"<p class='fighter-name'>{_esc(row['fighter_a'])} <span style='font-weight:400;color:var(--muted)'>vs</span> {_esc(row['fighter_b'])}</p>"
                              f"<p class='small' style='margin-top:7px'>{_esc(row['reason'])}</p></article>")
        unscored_html += "</div></section>"
    coverage_detail = f"{len(unscored)} listed matchup{'s' if len(unscored) != 1 else ''} not scored" if unscored else "Source-listed matchups with established identities"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta name='description' content='Research UFC win-probability report'><title>{_esc(event['name'])} · UFC Forecast</title><style>{_CSS}</style></head><body>"
        "<main class='report'><header class='masthead'><div class='brand'>UFC Forecast <span style='font-weight:400'>/ Research</span></div>"
        f"<div class='edition'>Prediction report<br>{_esc(_date_display(event['event_date']))}</div></header>"
        f"<p class='eyebrow'>The card, in probabilities</p><h1>{_esc(event['name'])}</h1><p class='dek'>{intro}</p>"
        f"<div class='event-meta'>{meta}</div><p class='research-label'><strong>{_esc(_STATUSES[forecast['status']])}.</strong> "
        f"{_esc(forecast['model_label'])}. A saved research estimate; no live betting edge or wagering authorization is established.</p>"
        f"<section class='summary' aria-label='Card summary'><div class='stat'><div class='stat-value'>{len(bouts):02d}<span style='font-size:16px;font-weight:400'> / {len(bouts)+len(unscored):02d}</span></div><div class='stat-label'>Bouts forecast</div><div class='stat-detail'>{_esc(coverage_detail)}</div></div>"
        f"<div class='stat'><div class='stat-value'>{near_even:02d}</div><div class='stat-label'>Near-even matchups</div><div class='stat-detail'>Favorite below 55% probability</div></div>"
        f"<div class='stat'><div class='stat-value'>{priced:02d}<span style='font-size:16px;font-weight:400'> / {len(bouts):02d}</span></div><div class='stat-label'>Market comparisons</div><div class='stat-detail'>Verified and fresh at cutoff</div></div></section>"
        "<section aria-labelledby='forecasts-title'><div class='section-head'><h2 id='forecasts-title'>Bout forecasts</h2><p>Win probabilities · both fighters<br>Order follows the supplied card</p></div>"
        "<div class='bouts'>" + "".join(_bout_html(bout, index) for index, bout in enumerate(bouts, 1)) + "</div>"
        "<p class='explainer'><strong>Reading the forecast.</strong> Near even: favorite below 55%; small lean: 55–60%; moderate lean: 60–70%; strong lean: 70% or above. "
        "These labels describe probability size, not a measured confidence interval. Directional factors are the largest saved model contributions and need not agree. "
        "Missing inputs are disclosed; an unavailable input is not evidence of equal ability. Market prices are historical observations at the stated cutoff.</p></section>"
        + unscored_html +
        f"<section aria-labelledby='evidence-title'><div class='section-head'><h2 id='evidence-title'>Evidence &amp; limits</h2><p>What supports this forecast</p></div><div class='evidence-grid'>"
        f"<div><h3>Saved model evaluation</h3>{evaluation_html}</div><div><h3>Before you read this as a result</h3>{notes_html}</div></div>"
        f"<div class='sources'><strong>Sources &amp; provenance</strong>{source_notes or '<p>No additional source notes supplied.</p>'}{source_list}"
        f"<p class='model-id'><strong>Model:</strong> {_esc(forecast['model_version'])}</p>{fingerprint}"
        f"<p><strong>Report generated:</strong> {_esc(_time_display(report['generated_at_utc']))}. All times UTC.</p></div></section>"
        "<footer class='footer'><strong>UFC Forecast / Research</strong><span>Independent research · No affiliation with UFC · No wagers placed</span></footer></main>"
        "<p class='print-tip'>Print or Save as PDF · A4 and Letter supported · Enable background graphics for probability bars.</p></body></html>"
    )


def main(argv: list[str] | None = None) -> int:
    """Render an existing public JSON payload without source fetches or fitting."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="Public prediction-report JSON")
    parser.add_argument("--html", type=Path, help="New output HTML path")
    parser.add_argument("--csv", type=Path, help="New output CSV path")
    parser.add_argument("--json", type=Path, help="New normalized public JSON path")
    args = parser.parse_args(argv)
    outputs = [(args.html, render_html), (args.csv, render_csv), (args.json, render_json)]
    if not any(path for path, _ in outputs):
        parser.error("Choose at least one output: --html, --csv or --json")
    try:
        report = validate_report(json.loads(args.input.read_text(encoding="utf-8")))
        rendered = [(path, renderer(report)) for path, renderer in outputs if path]
        if len({path.resolve() for path, _ in rendered}) != len(rendered):
            raise ValueError("Choose a separate path for each output format")
        if any(path.exists() or path.is_symlink() for path, _ in rendered):
            raise ValueError("Report outputs must use new paths; existing files are preserved")
        for path, content in rendered:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
