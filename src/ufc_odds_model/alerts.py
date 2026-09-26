"""Fail-closed eligibility gate for local pre-fight alert candidates.

This module does not place orders or send notifications. A larger model edge
cannot repair lagged in-play metrics or a stale sportsbook line. The gate is
for pre-fight prices only; in-play use needs a separately verified live feed
and execution design.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


def _utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_number(value: object) -> float | None:
    # bool is technically numeric in Python, but is never a valid price or
    # probability in a score report.
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def gate_prefight_alerts(
    rows: Iterable[Mapping[str, object]],
    *,
    snapshot_at_utc: str | datetime,
    event_start_time_utc: str | datetime,
    now_utc: str | datetime,
    max_age_seconds: float = 60,
    decimal_odds_drift: float = 0.05,
    min_edge: float = 0.03,
) -> list[dict[str, object]]:
    """Check every scored row against one freshly imported odds snapshot.

    Each returned copy includes ``alert_eligible`` (bool), ``alert_decision``
    (``alert_candidate`` or ``reject``), and a machine-readable ``alert_reason``.
    Eligible rows include conservative odds and expected profit per $1 after
    subtracting ``decimal_odds_drift`` from the displayed decimal price.

    All three reference times must include a timezone. Invalid or missing
    event, snapshot, quote, model, or bookmaker timestamps reject the row.
    ``bookmaker_updated_at_utc`` must be present in each score report row.
    The caller must pass the snapshot time returned by the immediately
    preceding successful odds import, not a time chosen from an old report.
    """
    age_limit = _finite_number(max_age_seconds)
    drift = _finite_number(decimal_odds_drift)
    edge_floor = _finite_number(min_edge)
    if age_limit is None or age_limit <= 0:
        raise ValueError("max_age_seconds must be a positive finite number")
    if drift is None or drift < 0:
        raise ValueError("decimal_odds_drift must be a nonnegative finite number")
    if edge_floor is None or edge_floor < 0:
        raise ValueError("min_edge must be a nonnegative finite number")

    snapshot = _utc(snapshot_at_utc)
    event_start = _utc(event_start_time_utc)
    now = _utc(now_utc)
    limit = timedelta(seconds=age_limit)
    checked: list[dict[str, object]] = []

    for row in rows:
        result = dict(row)
        result.update(
            alert_eligible=False,
            alert_decision="reject",
            alert_reason="",
            conservative_decimal_odds=None,
            conservative_expected_profit_per_dollar=None,
        )

        if snapshot is None or now is None or event_start is None:
            reason = "invalid_reference_time"
        elif now >= event_start:
            reason = "event_started"
        elif row.get("decision") != "candidate":
            reason = "not_model_candidate"
        elif not row.get("quote_id") or not row.get("bookmaker"):
            reason = "missing_quote"
        else:
            as_of = _utc(row.get("as_of_utc"))
            captured = _utc(row.get("quote_captured_at_utc"))
            book_updated = _utc(row.get("bookmaker_updated_at_utc"))
            if as_of is None or captured is None or book_updated is None:
                reason = "invalid_row_time"
            elif captured != snapshot:
                reason = "different_snapshot"
            elif not (book_updated <= captured <= as_of <= now < event_start):
                reason = "invalid_time_order"
            elif any(now - point > limit for point in (as_of, captured, book_updated)):
                reason = "stale_data"
            else:
                probability = _finite_number(row.get("model_selection_probability"))
                displayed_odds = _finite_number(row.get("decimal_odds"))
                if probability is None or not 0 < probability < 1:
                    reason = "invalid_probability"
                elif displayed_odds is None or displayed_odds <= 1:
                    reason = "invalid_odds"
                else:
                    conservative_odds = displayed_odds - drift
                    if conservative_odds <= 1:
                        reason = "price_drift_exhausts_odds"
                    else:
                        conservative_profit = probability * conservative_odds - 1.0
                        result["conservative_decimal_odds"] = round(conservative_odds, 4)
                        result["conservative_expected_profit_per_dollar"] = round(
                            conservative_profit, 4
                        )
                        reason = "ok" if conservative_profit >= edge_floor else "edge_below_floor"

        if reason == "ok":
            result["alert_eligible"] = True
            result["alert_decision"] = "alert_candidate"
        result["alert_reason"] = reason
        checked.append(result)
    return checked
