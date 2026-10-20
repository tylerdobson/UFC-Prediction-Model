"""Measured, past-only style and opponent context over a canonical history store.

The store supplies ``fighter_profile``, ``prior_fighter_bouts``, ``metadata`` and
a read-only connection facade supporting execute(sql, params). No connection is
created here. Canonical bout rows retain raw_json METHOD/DETAILS (or equivalent
source_method/source_details fields). Held rows remain visible, but do not enter
style, method or opponent-strength aggregates. Archive Elo is a descriptive
K=64 baseline, not a ranking or the fitted winner model.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
import json
import math
import re
from typing import Mapping
from urllib.parse import urlsplit


METHOD_GROUPS = ("ko_tko", "submission", "decision_unanimous", "decision_split", "decision_majority",
                 "decision", "doctor_stoppage", "disqualification", "could_not_continue", "overturned", "other", "unknown", "no_contest")


def method_group(method, outcome=None):
    """Normalize observed source wording without guessing a fighting style."""
    if outcome == "no_contest":
        return "no_contest"
    if not isinstance(method, str) or not method.strip():
        return "unknown"
    value = " ".join(method.casefold().split())
    if "doctor" in value or "medical" in value:
        return "doctor_stoppage"
    if value.startswith("dq") or "disqualif" in value:
        return "disqualification"
    if "could not continue" in value:
        return "could_not_continue"
    if "overturned" in value:
        return "overturned"
    if value.startswith("decision"):
        return ("decision_unanimous" if "unanimous" in value else "decision_split" if "split" in value
                else "decision_majority" if "majority" in value else "decision")
    if value.startswith("submission") or value == "sub":
        return "submission"
    if re.match(r"^(?:ko|tko)(?:\b|/)", value):
        return "ko_tko"
    return "other"


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Context cutoff must be an ISO timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Context cutoff must include a timezone")
    return result.astimezone(timezone.utc)


def _date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Context dates must be YYYY-MM-DD")
    return date.fromisoformat(value)


def _text(value, *, nullable=False, maximum=1500):
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)
            or re.search(r"file://|/(?:Users|private|home|tmp|var)/|\b(?:PASSWORD|API_KEY|ACCESS_TOKEN)\s*[=:]", value, re.I)):
        raise ValueError("Context requires public source text")
    return value.strip()


def _url(value):
    value = _text(value)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "ufcstats.com" or parsed.username or parsed.password:
        raise ValueError("Context requires a canonical public UFCStats source URL")
    return value


def _field(row, key, default=None):
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _number(value):
    return float(value) if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def _result(row, fighter):
    if row.get("outcome") in {"draw", "no_contest"}:
        return row["outcome"]
    if row.get("outcome") == "win":
        if row.get("winner_fighter_id") == fighter:
            return "win"
        other = row.get("fighter_b_id") if row.get("fighter_a_id") == fighter else row.get("fighter_a_id")
        if other and row.get("winner_fighter_id") == other:
            return "loss"
    return "unknown"


def _eligible(row):
    identities = (row.get("fighter_a_id"), row.get("fighter_b_id"))
    return (row.get("stats_status") == "usable" and all(identities) and identities[0] != identities[1]
            and (row.get("outcome") == "draw" or row.get("outcome") == "no_contest"
                 or row.get("outcome") == "win" and row.get("winner_fighter_id") in identities))


def _record(counter):
    return {"wins": counter["win"], "losses": counter["loss"], "draws": counter["draw"],
            "no_contests": counter["no_contest"], "scored_bouts": counter["win"] + counter["loss"] + counter["draw"]}


def _style(observations):
    """Aggregate measured numerators/exposure; never label a fighter an archetype."""
    specs = {
        "distance_attempt_share": ("ratio", "distance_attempted", "sig_attempted", 1.),
        "clinch_attempt_share": ("ratio", "clinch_attempted", "sig_attempted", 1.),
        "ground_attempt_share": ("ratio", "ground_attempted", "sig_attempted", 1.),
        "head_attempt_share": ("ratio", "head_attempted", "sig_attempted", 1.),
        "body_attempt_share": ("ratio", "body_attempted", "sig_attempted", 1.),
        "leg_attempt_share": ("ratio", "leg_attempted", "sig_attempted", 1.),
        "striking_accuracy": ("ratio", "sig_landed", "sig_attempted", 1.),
        "takedown_accuracy": ("ratio", "td_landed", "td_attempted", 1.),
        "sig_landed_per_minute": ("rate", "sig_landed", None, 60.),
        "sig_absorbed_per_minute": ("opponent_rate", "sig_landed", None, 60.),
        "takedown_attempts_per_15_minutes": ("rate", "td_attempted", None, 900.),
        "takedowns_per_15_minutes": ("rate", "td_landed", None, 900.),
        "submission_attempts_per_15_minutes": ("rate", "sub_attempts", None, 900.),
        "knockdowns_per_15_minutes": ("rate", "kd", None, 900.),
        "striking_defense": ("opponent_defense", "sig_landed", "sig_attempted", 1.),
        "takedown_defense": ("opponent_defense", "td_landed", "td_attempted", 1.),
        "control_share": ("control", "control_seconds", None, 1.),
    }
    output = {}
    for name, (kind, field, divisor, scale) in specs.items():
        numerator, denominator, sources, skipped = 0., 0., [], Counter()
        for observation in observations:
            own = _field(observation, "totals", {}) or {}
            opposing = _field(observation, "opponent_totals", {}) or {}
            values = opposing if kind.startswith("opponent") else own
            count = _number(values.get(field))
            exposure = _number(values.get(divisor)) if divisor else _number(_field(observation, "elapsed_seconds"))
            if count is None:
                skipped["count_unavailable"] += 1
                continue
            if exposure is None or exposure <= 0:
                skipped["duration_unavailable" if not divisor else "no_observed_attempts"] += 1
                continue
            if (divisor or kind == "control") and count > exposure:
                skipped["invalid_numerator_exceeds_denominator"] += 1
                continue
            numerator += count
            denominator += exposure
            sources.append(_field(observation, "bout_id"))
        value = numerator / denominator * scale if denominator else None
        if value is not None and kind.endswith("defense"):
            value = 1. - value
        output[name] = {"value": value, "observed": value is not None, "bouts": len(sources),
                        "numerator": numerator if sources else None, "denominator": denominator if sources else None,
                        "denominator_unit": "attempts" if divisor else "seconds", "source_bout_ids": sources,
                        "missing_reasons": dict(skipped) if skipped else ({} if sources else {"no_eligible_stat_bouts": 1})}
    return output


class FightContextHistory:
    """Reuse one verified read-only archive snapshot for multiple target fighters."""
    def __init__(self, store, *, cutoff_at_utc, elo_k=64.0, through_date=None):
        self.store, self.cutoff = store, _time(cutoff_at_utc)
        if isinstance(elo_k, bool) or not isinstance(elo_k, (int, float)) or not math.isfinite(elo_k) or elo_k <= 0:
            raise ValueError("Archive Elo K must be positive and finite")
        self.elo_k = float(elo_k)
        metadata = dict(store.metadata)
        if not metadata.get("captured_at_utc") or _time(metadata["captured_at_utc"]) > self.cutoff:
            raise ValueError("Context source capture must not follow its cutoff")
        bound = self.cutoff.date()
        if through_date is not None:
            bound = min(bound, _date(through_date) + timedelta(days=1))
        self.loaded_before = bound
        self.rows = [dict(row) for row in store.connection.execute(
            "SELECT * FROM bouts WHERE event_date<? ORDER BY event_date,bout_id", (bound.isoformat(),)).fetchall()]
        self.source = {key: metadata.get(key) for key in ("captured_at_utc", "source_manifest_sha256", "store_content_sha256", "repository_commit")}
        self.by_fighter = defaultdict(list)
        self.snapshots = {}
        ratings, records = defaultdict(lambda: 1500.), defaultdict(Counter)
        for day, group in groupby(self.rows, key=lambda row: row["event_date"]):
            _date(day)
            group = list(group)
            changes = defaultdict(float)
            for row in group:
                for fighter in (row.get("fighter_a_id"), row.get("fighter_b_id")):
                    if fighter:
                        self.by_fighter[fighter].append(row)
                if not _eligible(row):
                    continue
                a, b = row["fighter_a_id"], row["fighter_b_id"]
                self.snapshots[row["bout_id"]] = {fighter: {"archive_elo": ratings[fighter], "prior_record": _record(records[fighter])}
                                                   for fighter in (a, b)}
                if row["outcome"] == "no_contest":
                    continue
                expected = 1. / (1. + 10. ** ((ratings[b] - ratings[a]) / 400.))
                actual = .5 if row["outcome"] == "draw" else float(row["winner_fighter_id"] == a)
                change = self.elo_k * (actual - expected)
                changes[a] += change
                changes[b] -= change
            # No result from this date enters another bout's pre-bout record.
            for fighter, change in changes.items():
                ratings[fighter] += change
            for row in group:
                if _eligible(row):
                    for fighter in (row["fighter_a_id"], row["fighter_b_id"]):
                        records[fighter][_result(row, fighter)] += 1

    def fighter_context(self, fighter_id, *, target_date, max_fights=5):
        if type(max_fights) is not int or not 1 <= max_fights <= 5:
            raise ValueError("Context accepts one to five earlier fights")
        target = _date(target_date)
        before = min(self.cutoff.date(), target - timedelta(days=2))
        if before > self.loaded_before:
            raise ValueError("Context snapshot does not include the requested history")
        profile = self.store.fighter_profile(fighter_id)
        if not profile:
            return {"status": "unavailable", "reason": "No established canonical fighter identity.",
                    "prior_fights": [], "measured_style": {}, "method_summary": {}, "candidate_measurements": {}}
        fid = profile.get("canonical_id") or profile["fighter_id"]
        prior = [row for row in self.by_fighter[fid] if row["event_date"] < before.isoformat()]
        selected = prior[-max_fights:]
        statistics = {_field(row, "bout_id"): row for row in self.store.prior_fighter_bouts(fid, before.isoformat())}
        details, observations, groups, outcomes = [], [], Counter(), Counter()
        opponent_ratings, opponent_win_rates = [], []
        for row in selected:
            is_a = row.get("fighter_a_id") == fid
            other_id = row.get("fighter_b_id") if is_a else row.get("fighter_a_id")
            other_name = row.get("fighter_b_name") if is_a else row.get("fighter_a_name")
            raw = row.get("raw_json")
            raw = json.loads(raw) if isinstance(raw, str) else raw if isinstance(raw, Mapping) else {}
            method = row.get("source_method", raw.get("METHOD"))
            detail = row.get("source_details", raw.get("DETAILS"))
            method = _text(method, nullable=True) if method is None or str(method).strip() else None
            detail = _text(detail, nullable=True) if detail is None or str(detail).strip() else None
            category, outcome = method_group(method, row.get("outcome")), _result(row, fid)
            eligible = _eligible(row)
            strength = self.snapshots.get(row["bout_id"], {}).get(other_id)
            if strength:
                strength = {**strength, "status": "observed_prior_results" if strength["prior_record"]["scored_bouts"] else "initial_rating_no_prior_results",
                            "basis": "usable_canonical_archive_before_this_fight_whole_date", "elo_k": self.elo_k}
            item = {"bout_id": _text(row["bout_id"]), "event_id": _text(row.get("event_id"), nullable=True),
                    "event_date": row["event_date"], "fighter_id": fid,
                    "opponent_id": other_id, "opponent": _text(other_name),
                    "opponent_identity_status": "canonical_id_recorded" if other_id else "unresolved_source_name_only",
                    "result": outcome, "method": method, "method_group": category, "detail": detail,
                    "end_round": row.get("finish_round"), "end_time": row.get("finish_time"),
                    "finish_round": row.get("finish_round") if category in {"ko_tko", "submission"} else None,
                    "finish_time": row.get("finish_time") if category in {"ko_tko", "submission"} else None,
                    "source_url": _url(row["source_url"]), "stats_status": row.get("stats_status"),
                    "eligible_for_features": eligible, "coverage_reason": None if eligible else row.get("gap_reason") or "held_or_incomplete_result_identity",
                    "opponent_pre_bout_strength": strength}
            details.append(item)
            if not eligible:
                continue
            outcomes[outcome] += 1
            groups[category + "_" + outcome] += 1
            if row["bout_id"] in statistics:
                observations.append(statistics[row["bout_id"]])
            if strength and strength["prior_record"]["scored_bouts"]:
                record = strength["prior_record"]
                opponent_ratings.append(strength["archive_elo"])
                opponent_win_rates.append((record["wins"] + .5 * record["draws"]) / record["scored_bouts"])
        styles = _style(observations)
        method_counts = {category: {"wins": groups[category + "_win"], "losses": groups[category + "_loss"],
                        "draws": groups[category + "_draw"], "no_contests": groups[category + "_no_contest"],
                        "bouts": sum(groups[category + "_" + outcome] for outcome in ("win", "loss", "draw", "no_contest"))}
                        for category in METHOD_GROUPS}
        binary = outcomes["win"] + outcomes["loss"]
        classified = binary - sum(groups[category + "_" + outcome]
                                  for category in ("unknown", "other", "overturned") for outcome in ("win", "loss"))
        candidate = {name: metric["value"] for name, metric in styles.items()}
        candidate.update({"ko_win_share": groups["ko_tko_win"] / classified if classified else None,
                          "ko_loss_share": groups["ko_tko_loss"] / classified if classified else None,
                          "submission_win_share": groups["submission_win"] / classified if classified else None,
                          "submission_loss_share": groups["submission_loss"] / classified if classified else None,
                          "mean_opponent_pre_bout_elo": sum(opponent_ratings) / len(opponent_ratings) if opponent_ratings else None,
                          "mean_opponent_pre_bout_win_rate": sum(opponent_win_rates) / len(opponent_win_rates) if opponent_win_rates else None})
        result = {"status": "available", "research_only": True, "fighter_id": fid, "name": _text(profile["name"]),
                  "cutoff_at_utc": self.cutoff.isoformat(timespec="seconds").replace("+00:00", "Z"),
                  "target_date": target_date, "history_strictly_before_date": before.isoformat(),
                  "available_prior_bouts": len(prior), "selected_bouts": len(selected), "prior_fights": details,
                  "measured_style": styles, "method_summary": {"eligible_binary_bouts": binary,
                    "classified_method_binary_bouts": classified, "outcomes": dict(outcomes), "methods": method_counts,
                    "finish_share_denominator": "Eligible wins/losses with classified observed methods; doctor/DQ remain separate from KO/TKO."},
                  "opponent_strength_bouts": len(opponent_ratings), "candidate_measurements": candidate,
                  "coverage": {"selected_bouts": len(selected), "eligible_bouts": sum(_eligible(row) for row in selected),
                               "paired_stat_bouts": len(observations), "held_bouts": sum(not _eligible(row) for row in selected),
                               "missing_measurements": [name for name, value in candidate.items() if value is None]},
                  "source": self.source,
                  "limitations": ["Measured shares and rates describe this selected earlier-fight sample; they are not biographical striker/grappler labels.",
                                  "Held source rows stay visible and are excluded from all feature aggregates and archive Elo updates.",
                                  "Missing or unclassified methods do not count as observed non-finishes in finish shares.",
                                  "Opponent strength is a pre-bout usable-archive Elo/record, not a current ranking or full professional career record.",
                                  "Same-date results are withheld. All target and later dates, plus the preceding two target dates, are excluded.",
                                  "Source captures are retrospective; earlier fight dates alone do not prove historical pre-fight source availability.",
                                  "Candidate measurements are untrained; adding them has not established an improvement over the frozen model."]}
        json.dumps(result, allow_nan=False)
        return result


def build_fighter_context(store, fighter_id, *, target_date, cutoff_at_utc, max_fights=5, elo_k=64.0):
    return FightContextHistory(store, cutoff_at_utc=cutoff_at_utc, elo_k=elo_k).fighter_context(
        fighter_id, target_date=target_date, max_fights=max_fights)


def build_matchup_context(store, fighter_a_id, fighter_b_id, *, target_date, cutoff_at_utc, max_fights=5, elo_k=64.0):
    history = FightContextHistory(store, cutoff_at_utc=cutoff_at_utc, elo_k=elo_k)
    sides = [history.fighter_context(fid, target_date=target_date, max_fights=max_fights) for fid in (fighter_a_id, fighter_b_id)]
    a, b = (side["candidate_measurements"] for side in sides)
    differences = {key + "_difference": a[key] - b[key] if a.get(key) is not None and b.get(key) is not None else None for key in sorted(set(a) | set(b))}
    return {"status": "available" if all(side["status"] == "available" for side in sides) else "unavailable",
            "research_only": True, "fighter_a": sides[0], "fighter_b": sides[1], "untrained_feature_differences": differences}
