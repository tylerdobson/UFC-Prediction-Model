"""Read-only Streamlit dashboard over saved UFC model evidence.

Run from the repository root with ``python -m streamlit run app.py``.
All ingestion, model fitting, alert checks, and ledger writes remain CLI jobs.
"""

from __future__ import annotations

import html
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import streamlit as st

from ufc_odds_model.dashboard_data import load_dashboard


DB_PATH = Path(os.environ.get("UFC_MODEL_DB", "data/ufc.sqlite"))
EVALUATION_PATH = Path(
    os.environ.get("UFC_MODEL_EVALUATION_REPORT", "reports/evaluation.json")
)
INTEGRITY_PATH = Path(
    os.environ.get("UFC_MODEL_INTEGRITY_REPORT", "reports/integrity.json")
)
CSS_PATH = Path(__file__).with_name("dashboard.css")


def _text(value: Any, fallback: str = "—") -> str:
    return fallback if value is None or value == "" else str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _esc(value: Any, fallback: str = "—") -> str:
    return html.escape(_text(value, fallback), quote=True)


def _utc(value: Any, *, short: bool = False) -> str:
    if not value:
        return "Unavailable"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return str(value)
        parsed = parsed.astimezone(timezone.utc)
        month = parsed.strftime("%b")
        time = parsed.strftime("%H:%M")
        return f"{month} {parsed.day}{'' if short else f', {parsed.year}'} · {time} UTC"
    except (TypeError, ValueError):
        return str(value)


def _age(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "Unavailable"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def _number(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.{digits}f}" if math.isfinite(number) else "—"


def _percent(value: Any, digits: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.{digits}%}" if math.isfinite(number) else "—"


def _feature_coverage_text(prediction: Mapping[str, Any] | None) -> str:
    if not prediction:
        return "Prediction unavailable"
    status = prediction.get("feature_coverage_status")
    if status == "not_applicable":
        return "N/A for non-logistic model"
    if status != "recorded":
        return "Missing saved coverage" if status == "missing" else "Invalid saved coverage"
    coverage = _mapping(prediction.get("feature_coverage"))
    labels = (
        ("fighter_a_age", "A age"), ("fighter_b_age", "B age"),
        ("fighter_a_reach", "A reach"), ("fighter_b_reach", "B reach"),
        ("fighter_a_adjusted_stats", "A adjusted stats"),
        ("fighter_b_adjusted_stats", "B adjusted stats"),
    )
    present = sum(coverage.get(key) is True for key, _ in labels)
    missing = [label for key, label in labels if coverage.get(key) is not True]
    return (f"{present}/6 dated inputs" + (f"; missing: {', '.join(missing)}" if missing else ""))


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], empty: str) -> str:
    head = "".join(f"<th scope='col'>{_esc(item)}</th>" for item in headers)
    if rows:
        body = "".join(
            "<tr>" + "".join(f"<td>{_esc(value)}</td>" for value in row) + "</tr>"
            for row in rows
        )
    else:
        body = f"<tr class='empty-row'><td colspan='{len(headers)}'>{_esc(empty)}</td></tr>"
    return (
        "<div class='table-wrap'><table class='data-table'><thead><tr>"
        + head + "</tr></thead><tbody>" + body + "</tbody></table></div>"
    )


def _notice(snapshot: Mapping[str, Any]) -> str:
    if snapshot.get("status") not in {"available", "empty"}:
        return (
            "<div class='notice'><strong>Data unavailable.</strong> "
            + _esc(snapshot.get("reason"), "The saved database cannot be read.") + "</div>"
        )
    origin = snapshot.get("data_origin")
    if origin == "demo_only":
        return (
            "<div class='notice notice-demo'><strong>Demo data only.</strong> "
            "These fictional records cannot establish model quality or a betting edge.</div>"
        )
    if origin == "mixed":
        return (
            "<div class='notice notice-demo'><strong>Demo and source records mixed.</strong> "
            "Review source provenance before interpreting this view.</div>"
        )
    if origin == "research_only":
        return (
            "<div class='notice notice-demo'><strong>Research data only.</strong> "
            "This historical source is for model research and is ineligible for live alerts or paper decisions.</div>"
        )
    if origin == "research_mixed":
        return (
            "<div class='notice notice-demo'><strong>Research records mixed with other data.</strong> "
            "Check each event’s source; research cards are ineligible for live decisions.</div>"
        )
    return ""


def _status_meta(snapshot: Mapping[str, Any]) -> tuple[str, str, str]:
    runs = snapshot.get("ingestion_runs") or []
    last_run = _utc(runs[0].get("fetched_at_utc"), short=True) if runs else "Unavailable"
    predictions = [
        bout.get("prediction")
        for event in snapshot.get("upcoming_events") or []
        for bout in event.get("bouts") or []
        if bout.get("prediction")
    ]
    cutoff = max(
        (item.get("feature_cutoff_at_utc") for item in predictions if item.get("feature_cutoff_at_utc")),
        default=None,
    )
    fresh_quotes = sum(
        quote.get("freshness") == "fresh"
        for event in snapshot.get("upcoming_events") or []
        for bout in event.get("bouts") or []
        for quote in bout.get("quotes") or []
    )
    freshness = f"{fresh_quotes} within 60s" if fresh_quotes else "Unavailable"
    return last_run, _utc(cutoff, short=True), freshness


def _header(snapshot: Mapping[str, Any]) -> None:
    run, cutoff, freshness = _status_meta(snapshot)
    st.markdown(
        "<header class='app-header'><div class='brandline'>"
        "<span class='brand'>UFC Forecast</span><span class='brand-divider'></span>"
        "<span class='workspace'>Pre-fight workspace</span></div>"
        "<div class='header-metrics'>"
        f"<div class='header-metric'><span>Last source run</span><strong>{_esc(run)}</strong></div>"
        f"<div class='header-metric'><span>Model cutoff</span><strong>{_esc(cutoff)}</strong></div>"
        f"<div class='header-metric'><span>Quote check at load</span><strong>{_esc(freshness)}</strong></div>"
        "</div></header>",
        unsafe_allow_html=True,
    )


def _source_status(snapshot: Mapping[str, Any], event: Mapping[str, Any] | None) -> str:
    if event and event.get("is_demo"):
        return "Demo card · fictional data"
    if event and event.get("source") == "wikipedia_research":
        return "Research card · no live decisions"
    if snapshot.get("status") != "available":
        return "Data unavailable"
    if event is None:
        return "No upcoming event"
    quality = snapshot.get("quality") or {}
    return "Audit issues present" if quality.get("ok") is False else "Records present · review source"


_AVAILABILITY = {
    "provider_status_blocked": "Provider status blocked",
    "bout_not_scheduled": "Bout not scheduled",
    "event_start_unavailable": "Start time unavailable",
    "event_started": "Event started",
    "no_valid_prediction": "No saved prediction",
    "stale_prediction": "Model output stale",
    "no_quote": "No quote",
    "no_verified_fresh_quote": "No fresh quote",
    "requires_alert_gate": "Alert gate required",
    "gate_accepted_recently": "Prior gate pass · rerun check",
    "gate_rejected": "Gate rejected",
    "gate_expired": "Gate check expired",
    "gate_superseded": "Gate check superseded",
    "gate_prediction_superseded": "Model changed since check",
    "gate_quote_superseded_or_stale": "Quote changed or stale",
    "gate_prediction_changed": "Prediction changed",
    "gate_invalid_time": "Gate timing invalid",
}


def _availability_label(bout: Mapping[str, Any]) -> str:
    value = str(bout.get("availability") or "unavailable")
    return _AVAILABILITY.get(value, value.replace("_", " ").capitalize())


def _quote_display(bout: Mapping[str, Any]) -> tuple[str, str]:
    quotes = bout.get("quotes") or []
    if not quotes:
        return "—", "—"
    # A stored quote is an observation, never proof of an executable price.
    fresh = [item for item in quotes if item.get("freshness") == "fresh"]
    pool = fresh if fresh else quotes
    chosen = max(pool, key=lambda item: (float(item.get("decimal_odds") or 0), item.get("captured_at_utc") or ""))
    display = (
        f"{_text(chosen.get('selection_name'))} · {_text(chosen.get('bookmaker'))} "
        f"{_number(chosen.get('decimal_odds'))}"
    )
    age = _age(chosen.get("age_seconds"))
    reason = {
        "stale": "stale",
        "unverified_bookmaker_time": "bookmaker time unverified",
        "invalid_bookmaker_time": "bookmaker time invalid",
    }.get(chosen.get("freshness"))
    if reason:
        age += f" · {reason}"
    return display, age


def _requirements(snapshot: Mapping[str, Any], event: Mapping[str, Any] | None) -> str:
    bouts = event.get("bouts") or [] if event else []
    has_ids = bool(bouts) and all(b.get("fighter_a_id") and b.get("fighter_b_id") for b in bouts)
    has_quote = any(q.get("freshness") == "fresh" for b in bouts for q in b.get("quotes") or [])
    has_model = any(b.get("prediction") for b in bouts)
    audit = (snapshot.get("quality") or {}).get("ok")
    values = [
        ("Matched fighter IDs", "Review" if has_ids else "Pending"),
        ("Fresh quote", "Observed" if has_quote else "Pending"),
        ("Point-in-time model", "Saved" if has_model else "Pending"),
        ("Audit clear", "Demo only" if event and event.get("is_demo")
         else "Research only" if event and event.get("source") == "wikipedia_research"
         else "No critical issues" if audit is True and event
         else "Issues" if audit is False else "Pending"),
    ]
    items = "".join(
        f"<div class='requirement'><span>{_esc(label)}</span><span class='requirement-state'>{_esc(state)}</span></div>"
        for label, state in values
    )
    return (
        "<div class='requirements'><h2>Decision requirements</h2>"
        "<p>All items must be reviewed before a pre-fight alert can be considered.</p>"
        + items + "<p class='rail-note'>The CLI alert gate performs the decision check. "
        "A displayed quote is not an approved or executable wager.</p></div>"
    )


def _render_upcoming(snapshot: Mapping[str, Any]) -> None:
    events = snapshot.get("upcoming_events") or []
    left, rail = st.columns([3.1, 1], gap="large")
    with left:
        selector, source = st.columns([2.0, 1.0], gap="large")
        with selector:
            event_by_id = {event["event_id"]: event for event in events}
            if events:
                selected_id = st.selectbox(
                    "Select event",
                    options=list(event_by_id),
                    format_func=lambda event_id: event_by_id[event_id]["name"],
                    key="dashboard_event_id",
                )
                event = event_by_id[selected_id]
            else:
                st.selectbox("Select event", options=[], placeholder="Choose an event...", disabled=True)
                event = None
        with source:
            status = _source_status(snapshot, event)
            st.markdown(
                f"<div class='source-card'><span>Source status</span><strong>{_esc(status)}</strong></div>",
                unsafe_allow_html=True,
            )
        if notice := _notice(snapshot):
            st.markdown(notice, unsafe_allow_html=True)
        if event is None:
            st.markdown(
                "<section class='empty-main'><h1>No verified upcoming card</h1>"
                "<p>We don’t have a verified upcoming card to display yet.</p>"
                "<p>Select an event and import the latest card data, then complete the data quality "
                "checks before model predictions and quotes will appear here.</p></section>",
                unsafe_allow_html=True,
            )
            rows: list[list[str]] = [["Awaiting verified data", "—", "—", "—", "Unavailable"]]
            empty = "Awaiting verified data"
        else:
            intro = (
                "Demo card — fictional data" if event.get("is_demo")
                else "Research card — ineligible for live decisions" if event.get("source") == "wikipedia_research"
                else "Observed card data · verify the audit before acting"
            )
            st.markdown(
                f"<section class='event-main'><h1>{_esc(event.get('name'))}</h1>"
                f"<p>{_esc(_utc(event.get('start_time_utc')))} · {_esc(intro)}</p></section>",
                unsafe_allow_html=True,
            )
            rows = []
            for bout in event.get("bouts") or []:
                prediction = bout.get("prediction") or {}
                model = (
                    f"{_percent(prediction.get('p_fighter_a'))} {_text(bout.get('fighter_a_name'))}"
                    if prediction else "Unavailable"
                )
                quote, quote_age = _quote_display(bout)
                decision = (
                    "Demo · unavailable" if event.get("is_demo")
                    else "Research · unavailable" if event.get("source") == "wikipedia_research"
                    else _availability_label(bout)
                )
                rows.append([
                    f"{_text(bout.get('fighter_a_name'))} vs {_text(bout.get('fighter_b_name'))}",
                    model,
                    quote,
                    quote_age,
                    decision,
                ])
            empty = "No scheduled bouts in this card"
        st.markdown(
            _table(["Bout", "Model", "Observed quote", "Quote age", "Decision"], rows, empty),
            unsafe_allow_html=True,
        )
        if event:
            st.markdown(
                "<p class='table-note'>Observed prices are snapshots. A current alert requires a separate "
                "matched, timestamped gate check; this page cannot place wagers.</p>",
                unsafe_allow_html=True,
            )
            with st.expander("Source and model evidence for this card"):
                st.markdown(
                    f"<p class='evidence-line'><strong>Event ID:</strong> <code>{_esc(event.get('event_id'))}</code> · "
                    f"<strong>Source:</strong> <code>{_esc(event.get('source'))}</code> · "
                    f"<strong>Status:</strong> <code>{_esc(event.get('status'))}</code></p>",
                    unsafe_allow_html=True,
                )
                for bout in event.get("bouts") or []:
                    prediction = bout.get("prediction") or {}
                    st.markdown(
                        f"<p class='evidence-line'><strong>{_esc(bout.get('fighter_a_name'))} vs "
                        f"{_esc(bout.get('fighter_b_name'))}</strong><br>"
                        f"IDs: <code>{_esc(bout.get('fighter_a_id'))}</code> / "
                        f"<code>{_esc(bout.get('fighter_b_id'))}</code> · "
                        f"Model: <code>{_esc(prediction.get('model_version'))}</code> · "
                        f"Cutoff: <code>{_esc(prediction.get('feature_cutoff_at_utc'))}</code> · "
                        f"Generated: <code>{_esc(prediction.get('generated_at_utc'))}</code></p>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"<p class='evidence-line'><strong>Dated feature coverage:</strong> "
                        f"{_esc(_feature_coverage_text(prediction))}</p>",
                        unsafe_allow_html=True,
                    )
                    if bout.get("quotes"):
                        st.markdown(
                            "<p class='evidence-line'>" + " · ".join(
                                f"{_esc(q.get('bookmaker'))} {_esc(_number(q.get('decimal_odds')))} "
                                f"for {_esc(q.get('selection_name'))} "
                                f"(captured {_esc(q.get('captured_at_utc'))}; "
                                f"bookmaker updated {_esc(q.get('bookmaker_updated_at_utc'))}; "
                                f"{_esc(q.get('freshness'))})"
                                for q in bout["quotes"]
                            ) + "</p>",
                            unsafe_allow_html=True,
                        )
                    gate = bout.get("gate_check") or {}
                    if gate:
                        st.markdown(
                            f"<p class='evidence-line'>Saved gate check: <strong>{_esc(gate.get('display_status'))}</strong> · "
                            f"checked <code>{_esc(gate.get('checked_at_utc'))}</code> · "
                            f"snapshot <code>{_esc(gate.get('snapshot_at_utc'))}</code> · "
                            f"roster snapshot <code>{_esc(gate.get('roster_snapshot_id'))}</code> · "
                            f"reason: {_esc(gate.get('gate_reason'))}. "
                            "This historical check is not proof that a sportsbook price remains executable.</p>",
                            unsafe_allow_html=True,
                        )
    with rail:
        st.markdown(_requirements(snapshot, event), unsafe_allow_html=True)


def _evaluation_row(name: str, result: Mapping[str, Any] | None, split: Mapping[str, Any] | None) -> list[str]:
    metrics = _mapping(_mapping(result).get("metrics"))
    window = "—"
    if split and split.get("first_date"):
        window = f"{split['first_date']} → {split.get('last_date') or split['first_date']}"
    return [name, window, _number(metrics.get("brier_score"), 3),
            _number(metrics.get("log_loss"), 3), _text(metrics.get("bouts"))]


def _evaluation_has_displayable_results(result: Mapping[str, Any]) -> bool:
    """Fail closed when a saved report cannot support the visible comparisons."""
    if result.get("status") != "ok":
        return False
    splits = result.get("split")
    test = result.get("test")
    calibration = result.get("calibration")
    if not isinstance(splits, Mapping) or not isinstance(test, Mapping) or not isinstance(calibration, Mapping):
        return False
    if not all(isinstance(splits.get(name), Mapping) for name in ("train", "validation", "test")):
        return False
    for name in ("elo", "logistic"):
        model = test.get(name)
        if not isinstance(model, Mapping) or not isinstance(model.get("metrics"), Mapping):
            return False
        metrics = model["metrics"]
        if not isinstance(metrics.get("bouts"), int) or metrics["bouts"] < 1:
            return False
        for metric_name in ("brier_score", "log_loss"):
            score = metrics.get(metric_name)
            if not isinstance(score, (int, float)) or not math.isfinite(score):
                return False
    book = test.get("bookmaker")
    if not isinstance(book, Mapping):
        return False
    priced = book.get("available_bouts")
    coverage = book.get("coverage")
    if (not isinstance(priced, int) or priced < 0 or
            not isinstance(coverage, (int, float)) or not math.isfinite(coverage) or
            not 0 <= coverage <= 1):
        return False
    if priced:
        for key in ("metrics", "elo_on_available_bouts", "logistic_on_available_bouts"):
            metrics = book.get(key)
            if not isinstance(metrics, Mapping) or metrics.get("bouts") != priced:
                return False
            for metric_name in ("brier_score", "log_loss"):
                score = metrics.get(metric_name)
                if not isinstance(score, (int, float)) or not math.isfinite(score):
                    return False
    return True


def _render_model(snapshot: Mapping[str, Any]) -> None:
    evaluation = _mapping(snapshot.get("evaluation"))
    result = _mapping(evaluation.get("result"))
    available = evaluation.get("status") == "available" and _evaluation_has_displayable_results(result)
    test = _mapping(result.get("test"))
    splits = _mapping(result.get("split"))
    split = _mapping(splits.get("test"))
    st.markdown(
        "<section class='page-title'><h1>Model evidence</h1>"
        "<p>Chronological evaluation and calibration, with coverage shown alongside every result.</p></section>",
        unsafe_allow_html=True,
    )
    if snapshot.get("data_origin") in {"demo_only", "mixed", "research_only", "research_mixed"}:
        st.markdown(_notice(snapshot), unsafe_allow_html=True)
    model_cards = []
    for title, key in [("Elo", "elo"), ("Logistic", "logistic"), ("Bookmaker baseline", "bookmaker")]:
        model = _mapping(test.get(key))
        metrics = _mapping(model.get("metrics"))
        label = (
            f"Brier {_number(metrics.get('brier_score'), 3)} · {metrics.get('bouts', 0)} bouts"
            if available and metrics
            else "No matched two-sided prices" if available and key == "bookmaker"
            else "Awaiting evaluated history"
        )
        model_cards.append(
            f"<div class='model-card'><h2>{_esc(title)}</h2><p>{_esc(label)}</p></div>"
        )
    st.markdown("<div class='model-cards'>" + "".join(model_cards) + "</div>", unsafe_allow_html=True)
    coverage_rows = [
        [event.get("name"),
         f"{_text(bout.get('fighter_a_name'))} vs {_text(bout.get('fighter_b_name'))}",
         _text(_mapping(bout.get("prediction")).get("model_version")),
         _feature_coverage_text(_mapping(bout.get("prediction")))]
        for event in snapshot.get("upcoming_events") or []
        for bout in event.get("bouts") or []
    ]
    st.markdown(
        "<section class='panel'><h2>Upcoming dated feature coverage</h2>"
        "<p>Logistic predictions show which optional age, reach, and opponent-adjusted fight-stat "
        "inputs came from pre-fight observations. Missing inputs are neutral in the model.</p>"
        + _table(["Event", "Bout", "Model", "Dated inputs"], coverage_rows,
                 "No saved upcoming predictions") + "</section>",
        unsafe_allow_html=True,
    )
    comparison, requirements = st.columns([2.25, 1], gap="large")
    with comparison:
        if available:
            rows = [
                _evaluation_row("Elo", test.get("elo"), split),
                _evaluation_row("Logistic", test.get("logistic"), split),
                _evaluation_row("Bookmaker baseline", test.get("bookmaker"), split),
            ]
        else:
            rows = [[title, "—", "—", "—", "—"] for title in ["Elo", "Logistic", "Bookmaker baseline"]]
        st.markdown(
            "<section class='panel'><h2>Model comparison</h2>"
            + _table(["Model", "Test window", "Brier", "Log loss", "Evaluated bouts"], rows, "No evaluation")
            + "</section>",
            unsafe_allow_html=True,
        )
    with requirements:
        train = _mapping(splits.get("train"))
        validation = _mapping(splits.get("validation"))
        priced = _mapping(test.get("bookmaker")).get("available_bouts")
        checks = [
            ("Later untouched test events", f"{split.get('events', 0)} events" if available else "Pending"),
            ("Calibration fit on validation only", f"{validation.get('bouts', 0)} bouts" if available else "Pending"),
            ("Same priced bouts for comparison", f"{priced} bouts" if available and priced is not None else "Pending"),
        ]
        details = "".join(
            f"<div class='requirement'><span>{_esc(label)}</span><span>{_esc(value)}</span></div>"
            for label, value in checks
        )
        st.markdown("<section class='panel criteria'><h2>What must be true</h2>" + details + "</section>", unsafe_allow_html=True)
    if available:
        calibration = _mapping(result.get("calibration"))
        flags = _mapping(result.get("sample_size_flags"))
        book = _mapping(test.get("bookmaker"))
        st.markdown(
            "<div class='evidence-meta'>"
            f"<span>Train: {_esc(train.get('first_date'))} → {_esc(train.get('last_date'))} "
            f"({_esc(train.get('bouts'))} bouts)</span>"
            f"<span>Validation: {_esc(validation.get('first_date'))} → {_esc(validation.get('last_date'))} "
            f"({_esc(validation.get('bouts'))} bouts)</span>"
            f"<span>Test: {_esc(split.get('first_date'))} → {_esc(split.get('last_date'))} "
            f"({_esc(split.get('bouts'))} bouts)</span>"
            f"<span>Bookmaker coverage: {_esc(_percent(book.get('coverage')))}</span>"
            f"<span>Calibration applied: {_esc('yes' if calibration.get('applied') else 'no')}</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        if any(value is True for key, value in flags.items() if key.endswith("_under_30_bouts")):
            st.markdown(
                "<div class='notice'><strong>Small sample.</strong> Some comparison groups have fewer "
                "than 30 bouts; these metrics are unstable.</div>", unsafe_allow_html=True
            )
        if priced:
            subset_metrics = [
                ("Elo", _mapping(book.get("elo_on_available_bouts"))),
                ("Logistic", _mapping(book.get("logistic_on_available_bouts"))),
                ("Bookmaker baseline", _mapping(book.get("metrics"))),
            ]
            subset_rows = [
                [name, _number(metrics.get("brier_score"), 3),
                 _number(metrics.get("log_loss"), 3), _text(metrics.get("bouts"))]
                for name, metrics in subset_metrics
            ]
            st.markdown(
                "<section class='panel subset-comparison'><h2>Same priced-bout subset</h2>"
                "<p>These three scores use exactly the held-out bouts with a valid, "
                "two-sided bookmaker price at the decision cutoff.</p>"
                + _table(["Model", "Brier", "Log loss", "Priced bouts"], subset_rows, "No priced bouts")
                + "</section>", unsafe_allow_html=True,
            )
    bins = _mapping(test.get("logistic")).get("calibration_bins") if available else None
    bins = bins if isinstance(bins, list) else None
    if bins:
        chart_rows = [
            {"Predicted": item.get("mean_probability"), "Observed": item.get("observed_win_rate"),
             "Bouts": item.get("bouts")}
            for item in bins if isinstance(item, Mapping)
            and isinstance(item.get("mean_probability"), (int, float))
            and isinstance(item.get("observed_win_rate"), (int, float))
            and math.isfinite(item["mean_probability"]) and math.isfinite(item["observed_win_rate"])
        ]
        if chart_rows:
            st.markdown("<h2 class='calibration-title'>Calibration (test set)</h2>", unsafe_allow_html=True)
            st.vega_lite_chart(
                {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "height": 245,
                 "data": {"values": chart_rows}, "mark": {"type": "circle", "size": 130, "color": "#0b7885"},
                 "encoding": {
                     "x": {"field": "Predicted", "type": "quantitative", "scale": {"domain": [0, 1]},
                           "axis": {"title": "Predicted win probability"}},
                     "y": {"field": "Observed", "type": "quantitative", "scale": {"domain": [0, 1]},
                           "axis": {"title": "Observed win rate"}},
                     "tooltip": [{"field": "Predicted", "format": ".2f"},
                                 {"field": "Observed", "format": ".2f"}, {"field": "Bouts"}]},
                 "config": {"background": "#ffffff", "axis": {"labelColor": "#536681", "titleColor": "#536681"}}},
                use_container_width=True,
            )
        else:
            st.markdown("<section class='calibration-panel'><h2>Calibration (test set)</h2><div class='chart-empty'>No populated calibration bins in the saved test set.</div></section>", unsafe_allow_html=True)
    else:
        st.markdown(
            "<section class='calibration-panel'><h2>Calibration (test set)</h2>"
            "<div class='chart-empty'>Calibration will appear when a saved evaluation is available.</div></section>",
            unsafe_allow_html=True,
        )
    if not available:
        reason = evaluation.get("reason") or (
            "Saved evaluation has incomplete metrics or split evidence; rerun evaluation."
            if evaluation.get("status") == "available"
            else "The saved evaluation does not yet meet the history gate."
            if evaluation.get("status") == "insufficient_history"
            else "A saved evaluation report is not available."
        )
        if reason == "No saved evaluation report was supplied.":
            reason = (
                "The database is unavailable, so the saved evaluation cannot be verified."
                if EVALUATION_PATH.is_file()
                else f"No saved evaluation report exists at {EVALUATION_PATH}."
            )
        if snapshot.get("status") not in {"available", "empty"}:
            reason = _text(snapshot.get("reason")) + " " + reason
        st.markdown(
            f"<div class='notice'><strong>Evaluation unavailable.</strong> {_esc(reason)}</div>",
            unsafe_allow_html=True,
        )
    if available:
        st.markdown(
            "<p class='table-note'>Chart points are test-set bins. Logistic calibration is fit only on the "
            "validation period; the report records the fixed chronological splits.</p>",
            unsafe_allow_html=True,
        )


def _render_quality(snapshot: Mapping[str, Any]) -> None:
    quality = snapshot.get("quality") or {}
    summary = quality.get("summary") or {}
    issues = quality.get("issues") or []
    runs = snapshot.get("ingestion_runs") or []
    jobs = snapshot.get("job_runs") or []
    st.markdown(
        "<section class='page-title'><h1>Data quality</h1>"
        "<p>Source receipts, identity checks, roster status, and price coverage.</p></section>",
        unsafe_allow_html=True,
    )
    if notice := _notice(snapshot):
        st.markdown(notice, unsafe_allow_html=True)
    evidence = snapshot.get("integrity") or {}
    evidence_status = str(evidence.get("status") or "unavailable")
    evidence_label = "Checked recently" if evidence_status == "verified_recently" else evidence_status.replace("_", " ").capitalize()
    evidence_class = "notice" if evidence_status == "verified_recently" else "notice notice-demo"
    evidence_time = _utc(evidence.get("checked_at_utc")) if evidence.get("checked_at_utc") else "Not checked"
    st.markdown(
        f"<div class='{evidence_class}'><strong>Source evidence: {_esc(evidence_label)}.</strong> "
        f"{_esc(evidence.get('reason'), 'Run the local evidence integrity check.')} "
        f"Last check: {_esc(evidence_time)}.</div>",
        unsafe_allow_html=True,
    )
    if not runs and snapshot.get("status") in {"available", "empty"}:
        st.markdown(
            "<div class='notice quality-intro'><strong>No audited source history yet.</strong> "
            "Ingestion receipts and audit issues will appear here after a verified import from supported sources.</div>",
            unsafe_allow_html=True,
        )
    left, right = st.columns([2.1, 1], gap="large")
    with left:
        run_rows = [
            [run.get("source"), _utc(run.get("fetched_at_utc")), "Receipt saved",
             "Not recorded", str(run.get("sha256") or "")[:12] + "…"]
            for run in runs
        ]
        st.markdown(
            "<section class='panel'><h2>Saved source receipts</h2>"
            + _table(["Source", "Fetched UTC", "Status", "Rows", "SHA-256"], run_rows, "No runs recorded")
            + "</section>", unsafe_allow_html=True,
        )
        job_rows = [
            [job.get("command"), job.get("event_id"), _utc(job.get("started_at_utc")),
             job.get("status"), _text(job.get("error_category"))]
            for job in jobs
        ]
        st.markdown(
            "<section class='panel'><h2>Recent operator jobs</h2>"
            + _table(["Command", "Event", "Started UTC", "Status", "Error category"],
                     job_rows, "No operator jobs recorded") + "</section>",
            unsafe_allow_html=True,
        )
    with right:
        if quality.get("status") == "ready" and snapshot.get("data_origin") != "empty":
            categories = [
                ("Identity issues", sum(i.get("code") in {"unstable_fighter_id", "ambiguous_fighter_name", "empty_fighter_name", "self_matchup"} for i in issues)),
                ("Roster changes", summary.get("possible_opponent_substitutions", 0)),
                ("Missing results", sum(i.get("code") == "completed_bout_missing_result" for i in issues)),
                ("Timing issues", sum("time" in str(i.get("code")) or "start" in str(i.get("code")) for i in issues)),
            ]
            audit_rows = "".join(
                f"<div class='requirement'><span>{_esc(label)}</span><span>{_esc(value)}</span></div>"
                for label, value in categories
            )
        else:
            audit_rows = "".join(
                f"<div class='requirement'><span>{label}</span><span>Not assessed</span></div>"
                for label in ["Identity issues", "Roster changes", "Missing results", "Timing issues"]
            )
        st.markdown("<section class='panel criteria'><h2>Audit summary</h2>" + audit_rows + "</section>", unsafe_allow_html=True)
    issue_rows = [
        [item.get("severity"), item.get("code"), item.get("detail"),
         f"{_text(item.get('entity_type'))} {_text(item.get('entity_id'))}"]
        for item in issues[:60]
    ]
    empty_issues = (
        "No audit has run against real card data"
        if snapshot.get("data_origin") in {"empty", "demo_only"}
        else "No issues flagged by the current audit"
    )
    st.markdown(
        "<section class='panel quality-issues'><h2>Outstanding issues</h2>"
        + _table(["Severity", "Type", "Details", "Record"], issue_rows, empty_issues)
        + "</section>", unsafe_allow_html=True,
    )
    if len(issues) > 60:
        st.caption(f"Showing 60 of {len(issues)} issues. Run `ufc-model audit` for the full list.")
    coverage = summary.get("quote_coverage") or {}
    if coverage:
        coverage_rows = [
            ["Completed binary bouts with known start", coverage.get("completed_win_bouts_with_known_start")],
            ["Completed bouts with any quote", coverage.get("completed_win_bouts_with_any_quote")],
            ["Completed bouts with two-sided decision quotes", coverage.get("completed_win_bouts_with_two_sided_decision_quotes")],
            ["Scheduled bouts with quote in audit age window", coverage.get("scheduled_bouts_with_fresh_quote")],
            ["Scheduled bouts with two-sided quotes in audit window", coverage.get("scheduled_bouts_with_two_sided_fresh_quotes")],
        ]
        st.markdown(
            "<section class='panel coverage'><h2>Price coverage</h2>"
            + _table(["Measure", "Bouts"], coverage_rows, "Coverage unavailable")
            + "<p class='table-note'>Audit coverage uses its configured decision window and can count "
            "quotes without bookmaker update times. It does not certify the 60-second pre-fight alert gate.</p>"
            + "</section>",
            unsafe_allow_html=True,
        )


def _render_ledgers(snapshot: Mapping[str, Any]) -> None:
    paper = snapshot.get("paper_ledger") or {}
    manual = snapshot.get("manual_ledger") or {}
    paper_summary = paper.get("summary") or {}
    manual_summary = manual.get("summary") or {}
    st.markdown(
        "<section class='page-title'><h1>Ledgers</h1>"
        "<p>Simulated paper decisions and separately recorded actual wagers.</p></section>",
        unsafe_allow_html=True,
    )
    if notice := _notice(snapshot):
        st.markdown(notice, unsafe_allow_html=True)
    content, rail = st.columns([3.1, 1], gap="large")
    with content:
        paper_value = (
            f"{_number(paper_summary.get('open_exposure_units'))} units open · "
            f"{_text(paper_summary.get('bets'))} decisions"
            if paper_summary.get("bets") else "No entries"
        )
        manual_value = (
            f"{_text(manual_summary.get('bets'))} recorded · "
            f"{_number(manual_summary.get('open_exposure_units'))} units open"
            if manual_summary.get("bets") else "No entries"
        )
        st.markdown(
            "<div class='ledger-summaries'>"
            f"<div class='ledger-summary'><h2>Paper exposure</h2><p>{_esc(paper_value)}</p></div>"
            f"<div class='ledger-summary'><h2>Actual wagers</h2><p>{_esc(manual_value)}</p></div>"
            "</div>", unsafe_allow_html=True,
        )
        paper_rows = [
            [f"{_text(row.get('event_name'))} · {_text(row.get('selection_name'))}",
             _number(row.get("stake_units")), _number(row.get("quoted_decimal_odds")),
             row.get("settlement_status"),
             "Snapshot recorded" if row.get("roster_evidence_status") == "snapshot_recorded"
             else "Legacy / unverified",
             _utc(row.get("decision_at_utc"))]
            for row in paper.get("rows") or []
        ]
        st.markdown(
            "<section class='ledger-section'><h2>Paper ledger</h2>"
            + _table(["Event / selection", "Stake", "Quoted odds", "Status", "Roster evidence", "Decision UTC"],
                     paper_rows, "No paper decisions recorded") + "</section>",
            unsafe_allow_html=True,
        )
        manual_rows = [
            [f"{_text(row.get('event_name'))} · {_text(row.get('selection_name'))}",
             row.get("bookmaker"), _number(row.get("stake_units")),
             _number(row.get("actual_decimal_odds")), row.get("settlement_status")]
            for row in manual.get("rows") or []
        ]
        st.markdown(
            "<section class='ledger-section'><h2>Actual wagers</h2>"
            + _table(["Event / selection", "Bookmaker", "Stake", "Accepted odds", "Settlement"],
                     manual_rows, "No manually entered wagers") + "</section>",
            unsafe_allow_html=True,
        )
        if paper_summary.get("bets") or manual_summary.get("bets"):
            summary_rows = [
                ["Paper", paper_summary.get("bets"), _number(paper_summary.get("open_exposure_units")),
                 _number(paper_summary.get("pending_review_stake_units")),
                 _number(paper_summary.get("realized_profit_units")), _percent(paper_summary.get("realized_roi"))],
                ["Actual", manual_summary.get("bets"), _number(manual_summary.get("open_exposure_units")),
                 _number(manual_summary.get("pending_review_stake_units")),
                 _number(manual_summary.get("realized_profit_units")), _percent(manual_summary.get("realized_roi"))],
            ]
            st.markdown(
                "<section class='panel coverage'><h2>Settlement summary</h2>"
                + _table(["Ledger", "Entries", "Open stake", "Pending review", "Realized profit", "Realized ROI"],
                         summary_rows, "No settled records") + "</section>",
                unsafe_allow_html=True,
            )
    with rail:
        st.markdown(
            "<div class='ledger-rail'><p>Paper stakes are capped at 1% per bet and 5% per event. "
            "Actual bets are entered separately. This app never places wagers.</p>"
            "<p>Draws, no contests, voids, and cancellations require settlement review under the "
            "relevant bookmaker rules.</p></div>",
            unsafe_allow_html=True,
        )


def main() -> None:
    st.set_page_config(page_title="UFC Forecast · Pre-fight workspace", page_icon="🥊", layout="wide")
    st.markdown("<style>" + CSS_PATH.read_text(encoding="utf-8") + "</style>", unsafe_allow_html=True)
    try:
        snapshot = load_dashboard(
            DB_PATH, evaluation_report=EVALUATION_PATH, integrity_report=INTEGRITY_PATH
        )
    except (OSError, ValueError):
        st.error("The dashboard could not read its saved data. Check the local database and report configuration.")
        return
    _header(snapshot)
    upcoming, model, quality, ledgers = st.tabs(["Upcoming card", "Model evidence", "Data quality", "Ledgers"])
    with upcoming:
        _render_upcoming(snapshot)
    with model:
        _render_model(snapshot)
    with quality:
        _render_quality(snapshot)
    with ledgers:
        _render_ledgers(snapshot)
    st.markdown(
        "<footer class='app-footer'><span>All times UTC · Read-only · No wagers placed from this app · "
        f"Snapshot {_esc(_utc(snapshot.get('as_of_utc')))}</span><span>UFC Forecast</span></footer>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
