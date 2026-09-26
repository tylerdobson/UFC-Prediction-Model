"""Read-only checks of source identity, bout results, and price availability.

``audit_database`` accepts an initialized SQLite connection and returns a JSON
serializable report. The quote coverage figures use the same event-level decision
time and maximum quote age as the walk-forward backtest. An event start time is a
conservative proxy for an individual bout start time, not a claim about the exact
time the fighters entered the cage.
"""

from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone


_PREFIGHT_PROVIDER_STATUSES = {None, "scheduled", "not_started"}


def _rows(connection: sqlite3.Connection, query: str) -> list[dict]:
    cursor = connection.execute(query)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_name.casefold())


def audit_database(
    connection: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
    decision_hours_before_event: float = 24.0,
    max_quote_age_hours: float = 24.0,
) -> dict:
    """Return ``{"ok": bool, "summary": dict, "issues": list[dict]}``.

    ``ok`` means there are no error-severity issues; warnings still require
    review before interpreting model performance. No database rows are changed.
    ``as_of`` controls the freshness check for scheduled bouts and defaults to
    the current UTC time. Historical quote coverage is measured at each known
    event start minus ``decision_hours_before_event``.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    as_of = as_of.astimezone(timezone.utc)
    if not math.isfinite(decision_hours_before_event) or decision_hours_before_event < 0:
        raise ValueError("decision_hours_before_event must be finite and nonnegative")
    if not math.isfinite(max_quote_age_hours) or max_quote_age_hours <= 0:
        raise ValueError("max_quote_age_hours must be finite and positive")

    fighters = _rows(connection, "SELECT * FROM fighters")
    events = _rows(connection, "SELECT * FROM events")
    bouts = _rows(connection, "SELECT * FROM bouts")
    results = _rows(connection, "SELECT * FROM results")
    quotes = _rows(connection, "SELECT * FROM odds_quotes")
    predictions = _rows(connection, "SELECT * FROM predictions")
    bets = _rows(connection, "SELECT * FROM bets")
    paper_bets = _rows(connection, "SELECT * FROM paper_bets")
    ingestion_runs = _rows(connection, "SELECT * FROM ingestion_runs")
    card_events = _rows(connection, "SELECT * FROM card_event_snapshots")
    card_bouts = _rows(connection, "SELECT * FROM card_bout_snapshots")
    event_by_id = {row["event_id"]: row for row in events}
    bout_by_id = {row["bout_id"]: row for row in bouts}
    result_by_bout = {row["bout_id"]: row for row in results}
    prediction_by_id = {row["prediction_id"]: row for row in predictions}
    quote_by_id = {row["quote_id"]: row for row in quotes}
    run_by_id = {row["run_id"]: row for row in ingestion_runs}
    card_event_by_id = {row["event_snapshot_id"]: row for row in card_events}
    card_events_by_event: dict[str, list[dict]] = defaultdict(list)
    card_bouts_by_event: dict[str, list[dict]] = defaultdict(list)
    quotes_by_bout: dict[str, list[dict]] = defaultdict(list)
    issues: list[dict[str, str]] = []

    def add(severity: str, code: str, entity_type: str, entity_id: str, detail: str) -> None:
        issues.append({
            "severity": severity, "code": code, "entity_type": entity_type,
            "entity_id": str(entity_id), "detail": detail,
        })

    for table, rowid, parent, _ in connection.execute("PRAGMA foreign_key_check"):
        add("error", "foreign_key_violation", table, str(rowid), f"Missing parent in {parent}")

    names: dict[str, list[str]] = defaultdict(list)
    for fighter in fighters:
        fighter_id = fighter["fighter_id"]
        name = fighter["canonical_name"]
        key = _name_key(name)
        if not key:
            add("error", "empty_fighter_name", "fighter", fighter_id, "Name has no letters or digits")
        else:
            names[key].append(fighter_id)
        if not fighter["source"] or not fighter["source_fighter_id"]:
            add("warning", "unstable_fighter_id", "fighter", fighter_id,
                "Missing source identity; verify this ID will survive future imports")
    for ids in names.values():
        if len(ids) > 1:
            add("warning", "ambiguous_fighter_name", "fighter", ",".join(sorted(ids)),
                "Multiple fighter IDs normalize to the same name; verify whether they represent one person")

    event_starts: dict[str, datetime] = {}
    for event in events:
        event_id = event["event_id"]
        if event["status"] == "scheduled" and event["provider_status"] not in _PREFIGHT_PROVIDER_STATUSES:
            add("warning", "event_provider_status_needs_review", "event", event_id,
                f"Provider status {event['provider_status']!r} is not eligible for pre-fight scoring")
        try:
            date.fromisoformat(event["event_date"])
        except (TypeError, ValueError):
            add("error", "invalid_event_date", "event", event_id, "event_date is not an ISO date")
        start_text = event["start_time_utc"]
        if start_text is None:
            if event["status"] != "cancelled":
                add("warning", "missing_event_start", "event", event_id,
                    "Historical decision-time odds cannot be checked without a start time")
        else:
            start = _timestamp(start_text)
            if start is None:
                add("error", "invalid_event_start", "event", event_id,
                    "start_time_utc needs an ISO timestamp with a timezone")
            else:
                event_starts[event_id] = start
                if event["status"] == "scheduled" and start <= as_of:
                    add("warning", "scheduled_event_start_passed", "event", event_id,
                        "Scheduled event start is at or before the audit time")

    # An ingestion receipt proves when this application obtained a payload.
    # Only a separate source-observed timestamp can support a claim that a
    # pairing was available before the event. Keep both clocks visible.
    for snapshot in card_events:
        event_id = snapshot["event_id"]
        card_events_by_event[event_id].append(snapshot)
        run = run_by_id.get(snapshot["ingestion_run_id"])
        observed = _timestamp(snapshot["source_observed_at_utc"])
        fetched = _timestamp(run["fetched_at_utc"]) if run else None
        if snapshot["source_observed_at_utc"] is None:
            add("warning", "missing_roster_observation_time", "card_snapshot",
                snapshot["event_snapshot_id"],
                "This receipt has no source-observed time; it cannot prove a pre-fight roster")
        elif observed is None:
            add("error", "invalid_roster_observation_time", "card_snapshot",
                snapshot["event_snapshot_id"],
                "source_observed_at_utc needs a timezone-aware ISO timestamp")
        elif fetched is None or observed > fetched:
            add("error", "roster_observation_after_ingestion", "card_snapshot",
                snapshot["event_snapshot_id"],
                "Source-observed time is later than its ingestion receipt or the receipt time is invalid")
        if (snapshot["observation_basis"] == "reviewed_csv" and observed is not None
                and not snapshot["source_url"]):
            add("warning", "roster_observation_source_missing", "card_snapshot",
                snapshot["event_snapshot_id"],
                "Dated reviewed CSV observation has no source URL; retain review evidence")
        start = _timestamp(snapshot["start_time_utc"])
        if snapshot["start_time_utc"] is not None and start is None:
            add("error", "invalid_snapshot_event_start", "card_snapshot",
                snapshot["event_snapshot_id"],
                "Snapshot start_time_utc needs a timezone-aware ISO timestamp")
        elif observed is not None and start is not None and observed >= start:
            add("warning", "roster_observed_at_or_after_start", "card_snapshot",
                snapshot["event_snapshot_id"],
                "This card observation was made at or after the recorded event start")
    for event in events:
        if event["event_id"] not in card_events_by_event:
            add("warning", "missing_card_snapshot", "event", event["event_id"],
                "No immutable roster/status observation is linked to an ingestion receipt")
    for snapshot in card_bouts:
        card = card_event_by_id.get(snapshot["event_snapshot_id"])
        bout = bout_by_id.get(snapshot["bout_id"])
        if card is None:
            continue  # The foreign-key check above reports the broken link.
        card_bouts_by_event[card["event_id"]].append({
            **snapshot,
            "source_observed_at_utc": card["source_observed_at_utc"],
            "event_snapshot_id": card["event_snapshot_id"],
        })
        if bout and (
            bout["event_id"] != card["event_id"]
            or bout["fighter_a_id"] != snapshot["fighter_a_id"]
            or bout["fighter_b_id"] != snapshot["fighter_b_id"]
        ):
            add("error", "roster_snapshot_matchup_mismatch", "card_snapshot",
                snapshot["bout_snapshot_id"],
                "Snapshot event or participants disagree with the stable bout identity")

    roster_substitutions = 0
    for event_id, snapshots in card_bouts_by_event.items():
        histories: dict[str, list[dict]] = defaultdict(list)
        for snapshot in snapshots:
            histories[snapshot["bout_id"]].append(snapshot)
        bout_ids = sorted(histories)
        for index, first_id in enumerate(bout_ids):
            first = histories[first_id]
            first_pair = {first[0]["fighter_a_id"], first[0]["fighter_b_id"]}
            for second_id in bout_ids[index + 1:]:
                second = histories[second_id]
                second_pair = {second[0]["fighter_a_id"], second[0]["fighter_b_id"]}
                if len(first_pair & second_pair) != 1:
                    continue
                first_cancelled = any(row["bout_status"] == "cancelled" for row in first)
                second_cancelled = any(row["bout_status"] == "cancelled" for row in second)
                first_active = any(row["bout_status"] != "cancelled" for row in first)
                second_active = any(row["bout_status"] != "cancelled" for row in second)
                first_times = [_timestamp(row["source_observed_at_utc"])
                               for row in first if row["bout_status"] == "scheduled"]
                second_times = [_timestamp(row["source_observed_at_utc"])
                                for row in second if row["bout_status"] == "scheduled"]
                first_times = [when for when in first_times if when]
                second_times = [when for when in second_times if when]
                dated_change = bool(first_times and second_times and
                                    min(first_times) != min(second_times))
                if not ((first_cancelled and second_active)
                        or (second_cancelled and first_active) or dated_change):
                    continue
                roster_substitutions += 1
                timing = (
                    "Dated observations differ; verify when the change was announced"
                    if dated_change else "No ordered announcement time is established"
                )
                add("warning", "roster_possible_opponent_substitution", "event", event_id,
                    f"Bout {first_id} and bout {second_id} share one fighter. {timing}; "
                    "a missing row alone does not prove cancellation")

    active_pairs: dict[tuple[str, tuple[str, str]], str] = {}
    scheduled_fighters: dict[tuple[str, str], str] = {}
    bouts_by_event: dict[str, list[dict]] = defaultdict(list)
    for bout in bouts:
        bout_id = bout["bout_id"]
        event = event_by_id.get(bout["event_id"])
        bouts_by_event[bout["event_id"]].append(bout)
        if bout["status"] == "scheduled" and bout["provider_status"] not in _PREFIGHT_PROVIDER_STATUSES:
            add("warning", "bout_provider_status_needs_review", "bout", bout_id,
                f"Provider status {bout['provider_status']!r} is not eligible for pre-fight scoring")
        fighters_in_bout = {bout["fighter_a_id"], bout["fighter_b_id"]}
        if len(fighters_in_bout) != 2:
            add("error", "self_matchup", "bout", bout_id, "Both fighter IDs are identical")
        result = result_by_bout.get(bout_id)
        if bout["status"] == "completed" and result is None:
            add("error", "completed_bout_missing_result", "bout", bout_id,
                "Completed bout has no result")
        if bout["status"] != "completed" and result is not None:
            add("error", "result_on_noncompleted_bout", "bout", bout_id,
                "Scheduled or cancelled bout has a result")
        if event and event["status"] == "cancelled" and bout["status"] != "cancelled":
            add("error", "active_bout_on_cancelled_event", "bout", bout_id,
                "Cancelled event has a non-cancelled bout")
        if event and event["status"] == "completed" and bout["status"] == "scheduled":
            add("warning", "scheduled_bout_on_completed_event", "bout", bout_id,
                "Review whether this bout was cancelled or completed")
        if bout["status"] != "cancelled":
            key = (bout["event_id"], tuple(sorted(fighters_in_bout)))
            if key in active_pairs:
                add("error", "duplicate_active_matchup", "bout", bout_id,
                    f"Same active fighter pair as bout {active_pairs[key]}")
            else:
                active_pairs[key] = bout_id
        if event and event["status"] == "scheduled" and bout["status"] == "scheduled":
            for fighter_id in fighters_in_bout:
                key = (bout["event_id"], fighter_id)
                if key in scheduled_fighters:
                    add("error", "fighter_double_booked", "bout", bout_id,
                        f"Fighter {fighter_id} also appears in scheduled bout {scheduled_fighters[key]}")
                else:
                    scheduled_fighters[key] = bout_id

    substitution_pairs = 0
    for event_id, event_bouts in bouts_by_event.items():
        cancelled = [bout for bout in event_bouts if bout["status"] == "cancelled"]
        active = [bout for bout in event_bouts if bout["status"] != "cancelled"]
        for old in cancelled:
            old_ids = {old["fighter_a_id"], old["fighter_b_id"]}
            for new in active:
                new_ids = {new["fighter_a_id"], new["fighter_b_id"]}
                if len(old_ids & new_ids) == 1:
                    substitution_pairs += 1
                    add("warning", "possible_opponent_substitution", "event", event_id,
                        f"Cancelled bout {old['bout_id']} and active bout {new['bout_id']} share a fighter; review IDs and dates")

    for result in results:
        bout = bout_by_id.get(result["bout_id"])
        if bout and result["outcome"] == "win" and result["winner_fighter_id"] not in {
            bout["fighter_a_id"], bout["fighter_b_id"]
        }:
            add("error", "winner_not_in_bout", "bout", result["bout_id"],
                "Winner fighter ID is not in this matchup")
        if result["outcome"] != "win" and result["winner_fighter_id"] is not None:
            add("error", "nonbinary_result_has_winner", "bout", result["bout_id"],
                "Draw or no contest has a winner")
        if _timestamp(result["recorded_at_utc"]) is None:
            add("error", "invalid_result_timestamp", "bout", result["bout_id"],
                "recorded_at_utc needs a timezone-aware ISO timestamp")

    for quote in quotes:
        quote_id = str(quote["quote_id"])
        bout = bout_by_id.get(quote["bout_id"])
        valid_selection = bout is not None and quote["selection_fighter_id"] in {
            bout["fighter_a_id"], bout["fighter_b_id"]
        }
        if bout and not valid_selection:
            add("error", "quote_selection_not_in_bout", "quote", quote_id,
                "Quote selection is not a participant in the linked bout")
        valid_price = isinstance(quote["decimal_odds"], (int, float)) and (
            math.isfinite(quote["decimal_odds"]) and quote["decimal_odds"] > 1
        )
        if not valid_price:
            add("error", "invalid_quote_price", "quote", quote_id,
                "Decimal odds must be finite and greater than 1")
        captured = _timestamp(quote["captured_at_utc"])
        updated = _timestamp(quote["bookmaker_updated_at_utc"])
        if captured is None:
            add("error", "invalid_quote_capture_time", "quote", quote_id,
                "captured_at_utc needs a timezone-aware ISO timestamp")
        if quote["bookmaker_updated_at_utc"] is not None and updated is None:
            add("error", "invalid_bookmaker_update_time", "quote", quote_id,
                "bookmaker_updated_at_utc needs a timezone-aware ISO timestamp")
        if captured and updated and updated > captured:
            add("warning", "bookmaker_update_after_capture", "quote", quote_id,
                "Bookmaker update time is later than capture time; check source clock or feed")
        if bout and captured:
            start = event_starts.get(bout["event_id"])
            if start and captured >= start:
                add("warning", "quote_after_event_start", "quote", quote_id,
                    "Quote was captured at or after the event start and cannot support an event-level pre-fight decision")
        quote["_captured"] = captured
        quote["_updated"] = updated
        quote["_valid_selection"] = valid_selection
        quote["_valid_price"] = valid_price
        quotes_by_bout[quote["bout_id"]].append(quote)

    for prediction in predictions:
        bout_id = prediction["bout_id"]
        cutoff = _timestamp(prediction["feature_cutoff_at_utc"])
        generated = _timestamp(prediction["generated_at_utc"])
        if cutoff is None or generated is None:
            add("error", "invalid_prediction_timestamp", "prediction", prediction["prediction_id"],
                "Feature cutoff and generation time need timezone-aware ISO timestamps")
        elif cutoff > generated:
            add("error", "prediction_before_feature_cutoff", "prediction", prediction["prediction_id"],
                "Prediction was generated before its claimed feature cutoff")
        bout = bout_by_id.get(bout_id)
        start = event_starts.get(bout["event_id"]) if bout else None
        if start and cutoff and cutoff >= start:
            add("warning", "prediction_after_event_start", "prediction", prediction["prediction_id"],
                "Feature cutoff is at or after event start")

    for bet in bets:
        bet_id = str(bet["bet_id"])
        prediction = prediction_by_id.get(bet["prediction_id"])
        quote = quote_by_id.get(bet["quote_id"])
        if prediction and quote and prediction["bout_id"] != quote["bout_id"]:
            add("error", "bet_bout_mismatch", "bet", bet_id,
                "Linked prediction and quote belong to different bouts")
        if quote and bet["selection_fighter_id"] != quote["selection_fighter_id"]:
            add("error", "bet_selection_mismatch", "bet", bet_id,
                "Bet selection differs from the linked quote")
        if _timestamp(bet["placed_at_utc"]) is None:
            add("error", "invalid_bet_timestamp", "bet", bet_id,
                "placed_at_utc needs a timezone-aware ISO timestamp")

    for paper in paper_bets:
        paper_id = str(paper["paper_bet_id"])
        prediction = prediction_by_id.get(paper["prediction_id"])
        quote = quote_by_id.get(paper["quote_id"])
        if (prediction and prediction["bout_id"] != paper["bout_id"]
                or quote and quote["bout_id"] != paper["bout_id"]):
            add("error", "paper_bout_mismatch", "paper_bet", paper_id,
                "Paper decision links disagree on the bout")
        if quote and quote["selection_fighter_id"] != paper["selection_fighter_id"]:
            add("error", "paper_selection_mismatch", "paper_bet", paper_id,
                "Paper selection differs from the linked quote")
        if paper["settlement_status"] == "pending_review":
            add("warning", "paper_settlement_pending_review", "paper_bet", paper_id,
                "Draw, no contest, cancellation, or missing result needs settlement review")

    def eligible_quotes(bout_id: str, cutoff: datetime) -> list[dict]:
        eligible: list[dict] = []
        for quote in quotes_by_bout[bout_id]:
            if not quote["_valid_selection"] or not quote["_valid_price"]:
                continue
            captured = quote["_captured"]
            updated = quote["_updated"]
            if captured is None or captured > cutoff or cutoff - captured > max_age:
                continue
            if updated is not None and updated > captured:
                continue
            if quote["bookmaker_updated_at_utc"] is not None and (
                updated is None or updated > cutoff or cutoff - updated > max_age
            ):
                continue
            eligible.append(quote)
        return eligible

    def has_two_sided(bout: dict, eligible: list[dict]) -> bool:
        by_book: dict[str, set[str]] = defaultdict(set)
        for quote in eligible:
            by_book[quote["bookmaker"]].add(quote["selection_fighter_id"])
        needed = {bout["fighter_a_id"], bout["fighter_b_id"]}
        return any(needed <= selections for selections in by_book.values())

    max_age = timedelta(hours=max_quote_age_hours)
    decision_delta = timedelta(hours=decision_hours_before_event)
    completed_win_known_start = 0
    completed_win_any_quote = 0
    completed_win_fresh_quote = 0
    completed_win_two_sided = 0
    scheduled_bouts = 0
    scheduled_bouts_excluded_by_provider_status = 0
    scheduled_fresh_quote = 0
    scheduled_two_sided = 0
    stale_scheduled: list[str] = []
    for bout in bouts:
        event = event_by_id.get(bout["event_id"])
        if event is None:
            continue
        bout_id = bout["bout_id"]
        start = event_starts.get(bout["event_id"])
        result = result_by_bout.get(bout_id)
        if bout["status"] == "completed" and result and result["outcome"] == "win" and start:
            completed_win_known_start += 1
            if quotes_by_bout[bout_id]:
                completed_win_any_quote += 1
            eligible = eligible_quotes(bout_id, start - decision_delta)
            if eligible:
                completed_win_fresh_quote += 1
                if has_two_sided(bout, eligible):
                    completed_win_two_sided += 1
        if event["status"] == "scheduled" and bout["status"] == "scheduled" and (
            event["provider_status"] not in _PREFIGHT_PROVIDER_STATUSES
            or bout["provider_status"] not in _PREFIGHT_PROVIDER_STATUSES
        ):
            scheduled_bouts_excluded_by_provider_status += 1
        elif event["status"] == "scheduled" and bout["status"] == "scheduled" and (
            start is None or as_of < start
        ):
            scheduled_bouts += 1
            eligible = eligible_quotes(bout_id, as_of)
            if eligible:
                scheduled_fresh_quote += 1
                if has_two_sided(bout, eligible):
                    scheduled_two_sided += 1
            elif quotes_by_bout[bout_id]:
                stale_scheduled.append(bout_id)

    if completed_win_known_start > completed_win_fresh_quote:
        add("warning", "historical_quote_coverage_gap", "database", "all",
            f"Only {completed_win_fresh_quote}/{completed_win_known_start} completed win bouts with start times have a timely decision quote")
    if scheduled_bouts > scheduled_fresh_quote:
        add("warning", "live_quote_coverage_gap", "database", "all",
            f"Only {scheduled_fresh_quote}/{scheduled_bouts} upcoming bouts have a fresh quote at the audit time")
    for bout_id in stale_scheduled:
        add("warning", "stale_or_future_live_quote", "bout", bout_id,
            "Stored quotes exist, but none are timely at the audit time")

    event_status = Counter(row["status"] for row in events)
    bout_status = Counter(row["status"] for row in bouts)
    outcomes = Counter(row["outcome"] for row in results)
    summary = {
        "fighters": len(fighters),
        "events": len(events),
        "events_by_status": dict(event_status),
        "events_missing_start_time": sum(
            row["status"] != "cancelled" and row["start_time_utc"] is None for row in events
        ),
        "scheduled_events_excluded_by_provider_status": sum(
            row["status"] == "scheduled"
            and row["provider_status"] not in _PREFIGHT_PROVIDER_STATUSES for row in events
        ),
        "bouts": len(bouts),
        "bouts_by_status": dict(bout_status),
        "results": len(results),
        "results_by_outcome": dict(outcomes),
        "odds_quotes": len(quotes),
        "predictions": len(predictions),
        "bets": len(bets),
        "paper_bets": len(paper_bets),
        "paper_bets_by_status": dict(Counter(row["settlement_status"] for row in paper_bets)),
        "open_paper_exposure_units": round(sum(
            row["stake_units"] for row in paper_bets if row["settlement_status"] == "open"
        ), 4),
        "possible_opponent_substitutions": substitution_pairs,
        "card_event_snapshots": len(card_events),
        "card_bout_snapshots": len(card_bouts),
        "events_without_card_snapshots": sum(
            event["event_id"] not in card_events_by_event for event in events
        ),
        "card_snapshots_without_source_observation_time": sum(
            snapshot["source_observed_at_utc"] is None for snapshot in card_events
        ),
        "card_snapshots_observed_at_or_after_start": sum(
            _timestamp(snapshot["source_observed_at_utc"]) is not None
            and _timestamp(snapshot["start_time_utc"]) is not None
            and _timestamp(snapshot["source_observed_at_utc"]) >= _timestamp(snapshot["start_time_utc"])
            for snapshot in card_events
        ),
        "roster_possible_opponent_substitutions": roster_substitutions,
        "quote_coverage": {
            "completed_win_bouts_with_known_start": completed_win_known_start,
            "completed_win_bouts_with_any_quote": completed_win_any_quote,
            "completed_win_bouts_with_fresh_decision_quote": completed_win_fresh_quote,
            "completed_win_bouts_with_two_sided_decision_quotes": completed_win_two_sided,
            "scheduled_bouts": scheduled_bouts,
            "scheduled_bouts_excluded_by_provider_status": scheduled_bouts_excluded_by_provider_status,
            "scheduled_bouts_with_fresh_quote": scheduled_fresh_quote,
            "scheduled_bouts_with_two_sided_fresh_quotes": scheduled_two_sided,
        },
        "issues_by_severity": dict(Counter(item["severity"] for item in issues)),
    }
    return {"ok": not any(item["severity"] == "error" for item in issues),
            "summary": summary, "issues": issues}
