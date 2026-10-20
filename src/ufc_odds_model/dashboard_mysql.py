"""Read-only MySQL dashboard sections over published evidence.

No request initializes schemas, fits a model, refreshes a source, writes a ledger,
or opens a legacy SQLite database. Explicit extras are dependency injections,
not a silent file fallback when MySQL is unavailable.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import html
import json
import math
import os
from urllib.parse import urlsplit

_DOCS = {"model_report": ("forecast", "current"), "evaluation_v6": ("evaluation", "v6"),
         "evaluation_v7": ("evaluation", "v7"), "fight_context": ("fight-context", "current"),
         "rankings": ("rankings", "current"), "rankings_portraits": ("rankings-portraits", "current")}


def _server():
    from .mysql_runtime import connect_server
    return connect_server(readonly=True)


def _target(variable, default):
    from .mysql_runtime import is_mysql_target
    target = os.environ.get(variable, default)
    if not is_mysql_target(target):
        raise ValueError('Native dashboard requires MySQL dataset handles')
    return target


def _operating():
    from .mysql_runtime import connect_dataset
    return connect_dataset(_target('UFC_MODEL_DB', 'mysql:operating'), readonly=True)


def _statistics():
    from .historical_stats import HistoryStore
    return HistoryStore(_target('UFC_MODEL_STATISTICS_DB', 'mysql:statistics'))


def _document(kind, key, as_of):
    from .mysql_delivery import load_document
    return load_document(kind, key, as_of=as_of)


def _entries():
    from .mysql_delivery import read_forecast_entries
    return read_forecast_entries()


def _summary():
    from .forecast_ledger import summary
    return summary("mysql:forecast")


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Dashboard observation time must include a timezone")
    return value.astimezone(timezone.utc)


def _iso(value):
    return _time(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean(value):
    if isinstance(value, dict): return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_clean(v) for v in value]
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, date): return value.isoformat()
    if isinstance(value, Decimal): value = float(value)
    if isinstance(value, float) and not math.isfinite(value): return None
    if value is None or isinstance(value, (str, int, float, bool)): return value
    raise ValueError("Unsupported value in published dashboard evidence")


def _rows(connection, query, parameters=()):
    return [_clean(dict(row)) for row in connection.execute(query, parameters).fetchall()]


def _health():
    with closing(_server()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION(), DATABASE(), @@port")
            row = cursor.fetchone()
    if not row:
        raise ValueError("Native MySQL server returned no identity")
    return {"status": "available", "backend": "mysql", "server_version": str(row[0]),
            "database": row[1], "port": int(row[2]), "readonly": True,
            "checked_at_utc": _iso(datetime.now(timezone.utc))}


def _archive(as_of):
    from .historical_stats import FIELDS
    with _statistics() as store:
        metadata = dict(store.metadata)
        if _time(metadata["captured_at_utc"]) > as_of:
            raise ValueError("Statistics capture is later than the observation time")
        connection = store.connection
        events = _clean(store.event_coverage())
        fighters = _clean(store.fighter_coverage())
        bouts = _rows(connection, """SELECT b.bout_id,b.event_id,b.event_date,e.name AS event_name,
            b.fighter_a_id,b.fighter_b_id,b.fighter_a_name,b.fighter_b_name,b.weight_class,
            b.scheduled_rounds,b.finish_round,b.finish_time,b.elapsed_seconds,b.outcome,
            b.winner_fighter_id,b.stats_status,b.gap_reason,b.source_url,b.raw_json,
            fa.reach_cm AS fighter_a_reach_cm,fb.reach_cm AS fighter_b_reach_cm
            FROM bouts b LEFT JOIN events e ON e.event_id=b.event_id
            LEFT JOIN fighters fa ON fa.fighter_id=b.fighter_a_id
            LEFT JOIN fighters fb ON fb.fighter_id=b.fighter_b_id
            ORDER BY b.event_date DESC,b.bout_id""")
        for row in bouts:
            raw = row.pop("raw_json", None)
            raw = json.loads(raw) if isinstance(raw, str) else raw or {}
            row["method"], row["method_details"] = raw.get("METHOD"), raw.get("DETAILS")
        gaps = _rows(connection, "SELECT field,reason,COUNT(*) AS records FROM gaps GROUP BY field,reason ORDER BY records DESC,field,reason")
        holds = _rows(connection, "SELECT reason,COUNT(*) AS records FROM research_holds GROUP BY reason ORDER BY records DESC,reason")
        sources = _rows(connection, "SELECT file,sha256,source_url,bytes FROM source_files ORDER BY file")
        missing = Counter({key: 0 for key in FIELDS}); observations = 0
        for row in connection.execute("SELECT totals_json FROM fighter_bout_facts"):
            facts = row["totals_json"]
            facts = json.loads(facts) if isinstance(facts, str) else facts
            observations += 1
            missing.update(key for key in FIELDS if facts.get(key) is None)
    # Only completed observed dates are displayed for an explicitly earlier as-of.
    visible = [b for b in bouts if not b["event_date"] or b["event_date"] <= as_of.date().isoformat()]
    public_metadata = {k: metadata.get(k) for k in ("protocol", "captured_at_utc", "repository_url", "repository_commit",
                       "store_content_sha256", "source_manifest_sha256", "research_database_sha256", "counts")}
    quality = {"status": "available", "gap_groups": gaps, "research_holds": holds, "sources": sources,
               "missing_statistics": [{"field": key, "missing_fighter_bouts": missing[key],
                                        "fighter_bout_observations": observations} for key in FIELDS],
               "missing_reach_profiles": sum(f.get("reach_cm") is None for f in fighters),
               "fighter_profiles": len(fighters), "held_bouts": sum(b["stats_status"] == "held" for b in bouts),
               "usable_bouts": sum(b["stats_status"] == "usable" for b in bouts), "metadata": public_metadata}
    return {"status": "available", "backend": "mysql", "dataset": _target('UFC_MODEL_STATISTICS_DB', 'mysql:statistics'), "metadata": public_metadata,
            "events": events, "fighters": fighters, "bouts": visible, "source_bout_count": len(bouts), "quality": quality}


def _ledger_rows(connection, table, as_of):
    stamp = _iso(as_of)
    if table == "paper_bets":
        query = """SELECT p.paper_bet_id AS bet_id,e.name AS event_name,f.canonical_name AS selection,
            p.bookmaker,p.decision_at_utc,p.recorded_at_utc,p.quoted_decimal_odds AS decimal_odds,
            p.stake_units AS stake,p.settlement_status,p.payout_units AS payout,p.settled_at_utc
            FROM paper_bets p JOIN events e ON e.event_id=p.event_id
            JOIN fighters f ON f.fighter_id=p.selection_fighter_id
            WHERE p.recorded_at_utc<=? AND p.decision_at_utc<=? ORDER BY p.recorded_at_utc DESC,p.paper_bet_id"""
        rows = _rows(connection, query, (stamp, stamp))
    else:
        query = """SELECT p.bet_id,e.name AS event_name,f.canonical_name AS selection,
            p.placed_at_utc AS recorded_at_utc,p.actual_decimal_odds AS decimal_odds,p.stake,
            p.settlement_status,p.payout,p.settled_at_utc FROM bets p
            JOIN predictions x ON x.prediction_id=p.prediction_id JOIN bouts b ON b.bout_id=x.bout_id
            JOIN events e ON e.event_id=b.event_id JOIN fighters f ON f.fighter_id=p.selection_fighter_id
            WHERE p.placed_at_utc<=? ORDER BY p.placed_at_utc DESC,p.bet_id"""
        rows = _rows(connection, query, (stamp,))
    for row in rows:
        if row.get("settled_at_utc") and _time(row["settled_at_utc"]) > as_of:
            row.update(settlement_status="unsettled_as_of", payout=None)
    settled = [r for r in rows if r["settlement_status"] in {"won", "lost", "push", "void"}]
    profit = sum(r["payout"] - r["stake"] for r in settled) if all(r.get("payout") is not None for r in settled) else None
    return {"status": "available", "count": len(rows), "rows": rows,
            "recorded_stake": sum(r["stake"] for r in rows), "realized_profit": profit,
            "by_status": dict(Counter(r["settlement_status"] for r in rows))}


def _operating_ledgers(as_of):
    with closing(_operating()) as connection:
        return {"status": "available", "paper": _ledger_rows(connection, "paper_bets", as_of),
                "actual": _ledger_rows(connection, "bets", as_of), "forecast_records_are_wagers": False}


def _forward(entries, summary, as_of, report=None):
    visible = [entry for entry in entries if _time(entry["recorded_at_utc"]) <= as_of]
    freezes = {e["forecast_id"]: e for e in visible if e["kind"] == "freeze"}
    settled = {e["forecast_id"] for e in visible if e["kind"] == "settle"}
    pending = [{"forecast_id": key, "event_name": e.get("event_name"), "model_version": e.get("model_version"),
                "cutoff_at_utc": e.get("cutoff_at_utc"), "bouts": len(e.get("rows", [])),
                "recovered_saved_forecast": bool(e.get("recovered_saved_forecast"))}
               for key, e in freezes.items() if key not in settled]
    full_chain_visible = len(visible) == len(entries)
    result = {"status": "available", "source": "mysql:forecast", "chain_verified": True,
              "entry_count": len(visible), "frozen_forecasts": len(freezes), "settled_forecasts": len(settled),
              "pending_forecasts": pending, "summary": summary if full_chain_visible else None,
              "aggregate_status": "available" if full_chain_visible else "suppressed_post_cutoff_entries",
              "saved_unregistered_forecasts": [], "wager_authorization": False}
    if report:
        forecast = report.get("forecast", {}); identity = forecast.get("forecast_id")
        if identity and identity not in freezes:
            result["saved_unregistered_forecasts"].append({"event_name": report.get("event", {}).get("name"),
                "model_version": forecast.get("model_version"), "cutoff_at_utc": forecast.get("cutoff_at_utc"),
                "bouts": len(report.get("bouts", [])), "forecast_id": identity,
                "status": "saved_report_not_registered_in_forward_ledger"})
    return result


def load_mysql_overview(as_of=None, *, extras=None):
    """Load committed native MySQL data and publications, with per-section errors."""
    as_of = _time(as_of or datetime.now(timezone.utc)); extras = dict(extras or {})
    allowed = set(_DOCS) | {"forward_status"}
    if set(extras) - allowed:
        raise ValueError("Unsupported explicit dashboard evidence injection")
    result = {"schema_version": 1, "backend": "mysql", "as_of_utc": _iso(as_of), "errors": [],
              "sqlite_fallback": False, "model_retraining": False, "database_writes": False}
    def attempt(section, loader):
        try:
            return loader()
        except Exception as exc:
            reason = "Published MySQL evidence for this section is unavailable or uninitialized."
            result["errors"].append({"section": section, "error_type": type(exc).__name__, "code": section + "_unavailable"})
            return {"status": "unavailable", "reason": reason}
    result["health"] = attempt("health", _health)
    result["archive"] = attempt("archive", lambda: _archive(as_of))
    result["quality"] = result["archive"].get("quality", {"status": "unavailable", "reason": "The native statistics dataset has not been read successfully."})
    result["ledgers"] = attempt("ledgers", lambda: _operating_ledgers(as_of))
    documents, document_status = {}, {}
    for key, (kind, identity) in _DOCS.items():
        def get_document(key=key, kind=kind, identity=identity):
            payload = extras[key] if key in extras else _document(kind, identity, as_of)
            if payload is None:
                return {"status": "uninitialized", "reason": "No matching evidence document is published in MySQL."}
            if not isinstance(payload, dict):
                raise ValueError("Published evidence must be an object")
            stamp = (payload.get("generated_at_utc") if key == "model_report" else payload.get("created_at_utc"))
            if stamp and _time(stamp) > as_of:
                raise ValueError("Published evidence is later than observation time")
            if key == "rankings":
                from .rankings import validate_rankings
                validate_rankings(payload)
                if _time(payload["source"]["fetched_at_utc"]) > as_of:
                    raise ValueError("Rankings capture is later than observation time")
            if key == 'rankings_portraits':
                from .ranking_portraits import validate_portraits
                if documents.get('rankings') is None:
                    raise ValueError('Portraits require their published rankings roster')
                validate_portraits(payload, rankings=documents['rankings'])
                if _time(payload['fetched_at_utc']) > as_of:
                    raise ValueError('Portrait capture is later than observation time')
                if any(_time(entry['fetched_at_utc']) > as_of for entry in payload['portraits'].values()):
                    raise ValueError('An athlete portrait is later than observation time')
            return {"status": "available", "payload": _clean(payload),
                    "source": "explicit_extra" if key in extras else "mysql_document"}
        saved = attempt(key, get_document); document_status[key] = saved
        documents[key] = saved.get("payload")
    result["model_evidence"] = {"status": "available" if any(documents[k] for k in ("model_report", "evaluation_v6", "evaluation_v7")) else "uninitialized",
                                **{k: documents[k] for k in ("model_report", "evaluation_v6", "evaluation_v7", "fight_context")},
                                "document_status": {k: {a: b for a, b in v.items() if a != "payload"} for k, v in document_status.items()}}
    result["rankings"] = {**{k: v for k, v in document_status["rankings"].items() if k != "payload"},
                          "document": documents["rankings"], "portraits": documents['rankings_portraits']}
    def forward():
        entries = _entries()  # Delivery verifies chain, index, and tail before returning.
        summary = extras["forward_status"] if "forward_status" in extras else _summary()
        return _forward(entries, summary, as_of, documents["model_report"])
    result["forward_test"] = attempt("forward_test", forward)
    result["status"] = "available" if not result["errors"] else "partial" if result["health"]["status"] == "available" else "unavailable"
    return _clean(result)


def filter_archive_bouts(archive, *, search="", year=None, division=None, status=None, reach=None):
    """Filter the complete loaded archive without issuing another query."""
    needle = search.casefold().strip(); rows = []
    for row in archive.get("bouts", []):
        if year and str(row.get("event_date") or "")[:4] != str(year): continue
        if division and row.get("weight_class") != division: continue
        if status and row.get("stats_status") != status: continue
        both_reach = all(row.get(key) is not None for key in ("fighter_a_reach_cm", "fighter_b_reach_cm"))
        if reach == "both" and not both_reach or reach == "missing" and both_reach: continue
        if needle and needle not in " ".join(str(row.get(k) or "") for k in
                ("event_name", "bout_id", "fighter_a_name", "fighter_b_name", "weight_class", "method", "source_url")).casefold(): continue
        rows.append(row)
    return rows


def _st(facade):
    if facade is not None: return facade
    import streamlit
    return streamlit


def _esc(value):
    return html.escape("Unavailable" if value is None else str(value), quote=True)


def _title(st, heading, text):
    st.markdown(f"<section class='page-title'><h1>{_esc(heading)}</h1><p>{_esc(text)}</p></section>", unsafe_allow_html=True)


def _available(st, section):
    if section.get("status") == "available": return True
    st.info(section.get("reason") or "This evidence has not yet been published to MySQL.")
    return False


def _table(st, rows, empty, *, height=None):
    if rows:
        config = getattr(st, "column_config", None)
        links = {}
        if config is not None and hasattr(config, "LinkColumn"):
            for key in ("Source", "Official athlete", "source_url"):
                if any(key in row for row in rows):
                    links[key] = config.LinkColumn(key)
        options = {'height': height} if height is not None else {}
        st.dataframe(rows, hide_index=True, width='stretch', **options, **({"column_config": links} if links else {}))
    else: st.info(empty)


def _number(value, digits=4):
    return "Unavailable" if value is None else f"{value:,.{digits}f}"


def render_historical_archive(overview, *, st=None):
    st = _st(st); archive = overview.get("archive", {})
    _title(st, "Historical archive", "Every retained card and bout, with source links and explicit statistics coverage.")
    if not _available(st, archive): return
    rows = archive["bouts"]
    search = st.text_input("Search event, fighter, bout ID or method", key="mysql_archive_search")
    years = sorted({str(r["event_date"])[:4] for r in rows if r.get("event_date")}, reverse=True)
    divisions = sorted({r["weight_class"] for r in rows if r.get("weight_class")})
    filters = st.columns([1, 2, 1.3, 1.6, 1])
    with filters[0]: year = st.selectbox("Year", ["All years", *years], key="mysql_archive_year")
    with filters[1]: division = st.selectbox("Division", ["All divisions", *divisions], key="mysql_archive_division")
    with filters[2]: status = st.selectbox("Statistics coverage", ["All statuses", "usable", "held"], key="mysql_archive_status")
    with filters[3]: reach = st.selectbox("Reach coverage", ["All fighters", "Both measured", "At least one missing"], key="mysql_archive_reach")
    with filters[4]: limit = st.selectbox("Rows to display", [100, 250, 1000, "All"], key="mysql_archive_limit")
    filtered = filter_archive_bouts(archive, search=search, year=None if year == "All years" else year,
        division=None if division == "All divisions" else division, status=None if status == "All statuses" else status,
        reach={"All fighters": None, "Both measured": "both", "At least one missing": "missing"}[reach])
    st.caption(f"{len(filtered):,} matching bouts / {len(rows):,} loaded · {len(archive['events']):,} retained cards · native MySQL, read only")
    shown = filtered if limit == "All" else filtered[:limit]
    _table(st, [{"Date": r["event_date"], "Event": r["event_name"], "Fighter A": r["fighter_a_name"],
                "Fighter B": r["fighter_b_name"], "Division": r["weight_class"], "Method": r["method"],
                "Bout end": f"R{r['finish_round']} {r['finish_time']}" if r["finish_round"] else None,
                "Stats": r["stats_status"], "Hold reason": r["gap_reason"],
                "Reach A (cm)": r["fighter_a_reach_cm"], "Reach B (cm)": r["fighter_b_reach_cm"],
                "Source": r["source_url"]} for r in shown], "No archived bouts match these filters.")
    st.caption("Reach is a retrospectively captured profile measurement. Missing measurements and methods remain unavailable.")


def render_data_quality(overview, *, st=None):
    st = _st(st); quality = overview.get("quality", {}); health = overview.get("health", {})
    _title(st, "Data quality", "Native MySQL health, archive coverage, source receipts and unresolved observations.")
    if health.get("status") == "available":
        st.caption(f"MySQL {health['server_version']} · database {health['database']} · port {health['port']} · read-only session · checked {health['checked_at_utc']}")
    else: _available(st, health)
    if not _available(st, quality): return
    for column, (label, value) in zip(st.columns(3), (("Usable paired-stat bouts", quality["usable_bouts"]), ("Held archive bouts", quality["held_bouts"]),
                         ("Profiles missing reach", quality["missing_reach_profiles"]))):
        with column: st.metric(label, f"{value:,}")
    st.subheader("Coverage by card"); _table(st, overview["archive"]["events"], "No published archive cards.")
    st.subheader("Missing observations"); _table(st, quality["missing_statistics"], "No normalized fighter-bout observations.")
    st.subheader("Archive gaps"); _table(st, quality["gap_groups"], "No archive gap groups were recorded.")
    st.subheader("Research identity/date holds"); _table(st, quality["research_holds"], "No research crosswalk holds were recorded.")
    st.subheader("Source files and hashes"); _table(st, quality["sources"], "No source receipts were published.")
    metadata = quality["metadata"]
    st.caption(f"Captured {metadata.get('captured_at_utc')} · source manifest {metadata.get('source_manifest_sha256')} · logical content {metadata.get('store_content_sha256')}")
    st.caption("Missing statistics remain null; optional-field gaps do not erase otherwise valid observed counts.")


def _evaluation_rows(evaluation):
    rows = []
    for key, label in (("fixed_test", "Reused fixed benchmark"), ("rolling_origin", "Rolling origin")):
        section = evaluation.get(key, {})
        for role in ("candidate", "baseline"):
            metrics = section.get(role, {}).get("metrics", {})
            if metrics:
                rows.append({"Evaluation": label, "Model": evaluation.get(role + "_model_version", role),
                             "Bouts": metrics.get("bouts"), "Accuracy": f"{metrics['accuracy']:.2%}" if metrics.get('accuracy') is not None else None,
                             "Log loss": _number(metrics.get("log_loss"),5), "Brier score": _number(metrics.get("brier_score"),5)})
    return rows


def render_model_evidence(overview, *, st=None):
    st = _st(st); evidence = overview.get("model_evidence", {})
    _title(st, "Model evidence", "Saved forecasts and measured evaluation, with feature and sample coverage.")
    if not _available(st, evidence): return
    report = evidence.get("model_report")
    if report:
        st.subheader(report.get("forecast", {}).get("model_label", "Published forecast"))
        st.caption(f"{report.get('event', {}).get('name')} · {len(report.get('bouts', []))} scored matchups · cutoff {report.get('forecast', {}).get('cutoff_at_utc')}")
    for key, heading in (("evaluation_v6", "V6 retained research evaluation"), ("evaluation_v7", "V7 candidate evaluation")):
        evaluation = evidence.get(key)
        st.subheader(heading)
        if not evaluation:
            st.info("No evaluation document for this version has been published to MySQL."); continue
        _table(st, _evaluation_rows(evaluation), "No completed evaluation metrics are published.")
        st.caption(f"{len(evaluation.get('feature_names', []))} features · selected family {evaluation.get('selected_candidate_id', 'Unavailable')} · evaluation created {evaluation.get('created_at_utc', 'Unavailable')}")
        if key == 'evaluation_v7':
            baseline = evaluation.get('fixed_test', {}).get('baseline', {}).get('metrics', {})
            candidate = evaluation.get('fixed_test', {}).get('candidate', {}).get('metrics', {})
            if candidate.get('log_loss') is not None and baseline.get('log_loss') is not None:
                if candidate['log_loss'] >= baseline['log_loss']:
                    st.info('The richer V7 candidate did not improve fixed-benchmark log loss. V6 remains the delivered model.')
                else:
                    st.info('V7 is a research comparison. A reused benchmark does not automatically promote a new delivered model.')
        paired = evaluation.get("fixed_test", {}).get("paired_uncertainty", {})
        interval = paired.get("candidate_minus_baseline_log_loss_95pct_interval")
        if interval:
            st.caption(f"Paired fixed-benchmark log-loss difference interval: [{_number(interval[0],5)}, {_number(interval[1],5)}]. {paired.get('note', '')}")
        for limitation in evaluation.get("limitations", []): st.caption(str(limitation))
        st.caption("A reused retrospective benchmark is not a fresh holdout or proof of wagering value.")
        folds = evaluation.get("rolling_origin", {}).get("candidate_folds", [])
        _table(st, [{"Year": f.get("year"), **f.get("test", {}).get("metrics", {})} for f in folds], "No rolling-year observations were published.")
    st.caption("Current descriptive rankings and source-page career averages do not become historical features through this dashboard.")


def render_ledgers(overview, *, st=None):
    st = _st(st); ledgers = overview.get("ledgers", {})
    _title(st, "Ledgers", "Paper positions, recorded actual wagers, and forecast records are separate evidence.")
    if not _available(st, ledgers): return
    for key, label in (("paper", "Paper positions"), ("actual", "Recorded actual wagers")):
        ledger = ledgers[key]; st.subheader(label); st.metric("Recorded entries", ledger["count"])
        _table(st, ledger["rows"], "No " + label.lower() + " are recorded in the operating MySQL dataset.")
        if ledger["count"]:
            st.caption(f"Recorded stake {_number(ledger['recorded_stake'],2)} · realized profit {_number(ledger['realized_profit'],2)} · amounts use the ledger's recorded units.")
    forward = overview.get("forward_test", {})
    if forward.get("status") == "available":
        st.caption(f"Separately: {forward['frozen_forecasts']} frozen forecast sets and {forward['settled_forecasts']} settled forecast sets. Forecast records are not wagers.")


def render_forward_test(overview, *, st=None):
    st = _st(st); forward = overview.get("forward_test", {})
    _title(st, "Forward test", "Verified frozen forecasts and later settlements, with saved reports identified separately.")
    if not _available(st, forward): return
    st.caption(f"{forward['entry_count']} visible chain entries · {forward['frozen_forecasts']} frozen sets · {forward['settled_forecasts']} settled sets · observation {overview['as_of_utc']}")
    st.subheader("Frozen and awaiting settlement"); _table(st, forward["pending_forecasts"], "No unsettled frozen forecasts are recorded.")
    if forward["saved_unregistered_forecasts"]:
        st.subheader("Saved forecast awaiting ledger registration")
        _table(st, forward["saved_unregistered_forecasts"], "")
        st.caption("These saved forecast reports are not counted as immutable forward-ledger freezes.")
    summary = forward.get("summary")
    if summary:
        rows = []
        for event in summary.get("events", []):
            for key, label in (("model", "Model / all"), ("model_on_market_subset", "Model / priced subset"), ("market", "Market / priced subset")):
                metrics = event.get("metrics", {}).get(key)
                if metrics: rows.append({"Event": event.get("event_name"), "Comparison": label, "Model": event.get("model_version"),
                                        "Recovered after event": event.get("recovered_saved_forecast"), **metrics})
        st.subheader("Observed settlements"); _table(st, rows, "No settled model/market comparisons are published.")
    elif forward.get("aggregate_status") == "suppressed_post_cutoff_entries":
        st.info("Current aggregate metrics include later ledger entries and are suppressed for this earlier observation time.")
    st.caption("Small forward samples do not establish a betting edge. A recovered earlier forecast is not prospective evidence for a newer model.")


def _athlete_link(value):
    if not isinstance(value, str): return None
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in {'ufc.com', 'www.ufc.com'}
            or parsed.username or parsed.password or not parsed.path.startswith('/athlete/')):
        return None
    return value


def _ranking_portrait(row, portraits, *, champion=False):
    entry = portraits.get(row.get('athlete_url'), {})
    source = entry.get('image_url') if entry.get('status') == 'available' and entry.get('athlete_url') == row.get('athlete_url') else None
    from .ranking_portraits import image_url
    try:
        image_url(source)
    except (ValueError, TypeError, AttributeError):
        source = None
    classes = 'rankings-portrait' + (' rankings-champion-portrait' if champion else '')
    if source:
        return (f"<span class='{classes}'><img src='{_esc(source)}' alt='{_esc(row['name'])} — official UFC portrait' "
                "loading='lazy' referrerpolicy='no-referrer'></span>")
    initials = ''.join(word[0] for word in row['name'].split()[:2])
    return f"<span class='{classes} rankings-portrait-missing' title='Official portrait unavailable'>{_esc(initials)}</span>"


def _ranked_name(row):
    name = _esc(row['name'])
    link = _athlete_link(row.get('athlete_url'))
    return f"<a href='{_esc(link)}' target='_blank' rel='noopener noreferrer'>{name}</a>" if link else name


def _rankings_table(division, portraits):
    rows = []
    for row in division['contenders']:
        rank = str(row['rank']) + (' =' if row['tied'] else '')
        rows.append(f"<tr><td class='rankings-position'>{_esc(rank)}</td><td><div class='rankings-athlete'>"
                    f"{_ranking_portrait(row, portraits)}{_ranked_name(row)}</div></td>"
                    f"<td>{_esc(row['rank_change_label'] or '—')}</td></tr>")
    return ("<div class='rankings-table-wrap'><table class='rankings-table'><thead><tr>"
            "<th>Rank</th><th>Fighter</th><th>Movement</th></tr></thead><tbody>" + ''.join(rows) + '</tbody></table></div>')


def render_rankings(overview, *, st=None):
    st = _st(st); section = overview.get("rankings", {})
    _title(st, "Official UFC rankings", "Publisher-listed champions and top-15 positions, with both official systems kept separate.")
    if not _available(st, section): return
    document = section["document"]; systems = document["ranking_sets"]
    portrait_document = section.get('portraits') or {}
    portraits = (portrait_document.get('portraits', {})
                 if portrait_document.get('rankings_source_sha256') == document['source']['raw_sha256'] else {})
    labels = [s["label"] for s in systems]
    default = next(i for i, s in enumerate(systems) if s["id"] == document["default_ranking_set"])
    selectors = st.columns([1, 2])
    with selectors[0]: selected = st.selectbox("Ranking system", labels, index=default, key="mysql_rankings_system")
    system = systems[labels.index(selected)]
    st.caption(f"{system.get('source_updated_label') or 'Publisher update date unavailable'} · fetched {document['source']['fetched_at_utc']} · {document['source']['url']}")
    names = [d["name"] for d in system["divisions"]]
    with selectors[1]: chosen = st.selectbox("Ranked division", ["All published divisions", *names], key="mysql_rankings_division")
    for division in system["divisions"]:
        if chosen != "All published divisions" and chosen != division["name"]: continue
        st.subheader(division["name"])
        for champion in division["champions"]:
            st.markdown("<div class='rankings-champion'>" + _ranking_portrait(champion, portraits, champion=True)
                        + f"<div><span class='rankings-champion-label'>{_esc(champion['source_status_label'])}</span>"
                        + f"<strong>{_ranked_name(champion)}</strong><span>Official UFC division rankings</span></div></div>", unsafe_allow_html=True)
        if division["championship_status"] == "vacant": st.caption("Championship: explicitly vacant in the official source.")
        if division['contenders']:
            st.markdown(_rankings_table(division, portraits), unsafe_allow_html=True)
        else:
            st.info(division.get('empty_reason') or 'No contenders are published.')
        if division["missing_ranks"]:
            st.caption("Unassigned published rank numbers: " + "; ".join(f"{rank}: {division['missing_rank_reasons'][str(rank)].replace('_',' ')}" for rank in division["missing_ranks"]))
        for note in division["data_quality_notes"]: st.caption(note)
    st.caption("Source snapshot SHA-256: " + document["source"]["raw_sha256"] + ". Current descriptive rankings are not historical model inputs.")
    if portraits:
        coverage = portrait_document.get('coverage', {})
        st.caption(f"Official portraits: {coverage.get('available', 0)} / {coverage.get('ranked_unique_athletes', 0)} verified · captured {portrait_document.get('fetched_at_utc')}")
    st.caption('Fighter portraits: official UFC athlete pages. Images load from UFC; unavailable portraits are labeled explicitly.')
