"""Append-only, hash-chained frozen forecasts and their later settlements."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path

from .logistic import binary_metrics
from .market_guard import verified_market
from .model_lab import file_sha256
from .pipeline import parse_utc,utc_now,utc_string
from .research_evaluation import _digest_json

DEFAULT_LEDGER = "reports/forecast_ledger.jsonl"


def _decode(text):
    entries, previous = [], "0"*64
    if text and not text.endswith("\n"):
        raise ValueError("Forecast ledger has an incomplete final line")
    for line in text.splitlines():
        entry = json.loads(line)
        sha = entry.pop("entry_sha256")
        if entry.get("previous_sha256") != previous or _digest_json(entry) != sha:
            raise ValueError("Forecast ledger hash chain is invalid")
        entry["entry_sha256"] = sha
        entries.append(entry)
        previous = sha
    return entries


def read_entries(path=DEFAULT_LEDGER):
    path=Path(path)
    if path.is_symlink():
        raise ValueError("Forecast ledger must not be a symlink")
    return _decode(path.read_text()) if path.exists() else []


def append_entry(path, entry):
    path=Path(path)
    if path.is_symlink():
        raise ValueError("Forecast ledger must not be a symlink")
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+",encoding="utf-8") as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        stream.seek(0)
        entries=_decode(stream.read())
        if any(e["kind"]==entry["kind"] and e["forecast_id"]==entry["forecast_id"] for e in entries):
            raise ValueError(f"Forecast {entry['forecast_id']} already has a {entry['kind']} entry")
        payload={"schema_version":1,"recorded_at_utc":utc_string(utc_now()),
                 **entry,"previous_sha256":entries[-1]["entry_sha256"] if entries else "0"*64}
        payload["entry_sha256"]=_digest_json(payload)
        stream.seek(0,2)
        stream.write(json.dumps(payload,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n")
        stream.flush(); os.fsync(stream.fileno())
        return payload


def _portable_path(path):
    path=Path(path).resolve()
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def freeze_saved_research(event_id, source_path, *, ledger=DEFAULT_LEDGER):
    saved=json.loads(Path(source_path).read_bytes())
    if saved.get("event_id") != event_id or saved.get("research_only") is not True:
        raise ValueError("Saved research forecast must identify this exact event")
    if _digest_json(saved["forecasts"]) != saved.get("forecast_rows_sha256"):
        raise ValueError("Saved forecast rows changed from their stated hash")
    if parse_utc(saved["cutoff_at_utc"]) >= parse_utc(saved["event_start_at_utc"]):
        raise ValueError("Saved forecast cutoff must precede the event")
    rows=[]
    for forecast in saved["forecasts"]:
        probability=float(forecast["logistic_calibrated_probability_fighter_a"])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Saved forecast probability is invalid")
        books=forecast.get("saved_two_sided_books",[])
        probabilities=[]
        for book in books:
            a,b=float(book["fighter_a_decimal_odds"]),float(book["fighter_b_decimal_odds"])
            if not math.isfinite(a) or not math.isfinite(b) or min(a,b) <= 1:
                raise ValueError("Saved market prices are invalid")
            captured=parse_utc(book["captured_at_utc"])
            updated=parse_utc(book["bookmaker_updated_at_utc"])
            if not updated <= captured <= parse_utc(saved["cutoff_at_utc"]):
                raise ValueError("Saved market timestamp order is invalid")
            probabilities.append((1/a)/(1/a+1/b))
        rows.append({"bout_id":f"{event_id}:position:{forecast['position']}","source_position":forecast["position"],
                     "fighter_a_id":forecast["fighter_a_id"],"fighter_b_id":forecast["fighter_b_id"],
                     "fighter_a":forecast["fighter_a_name"],"fighter_b":forecast["fighter_b_name"],
                     "model_probability":probability,
                     "market_no_vig_probability":sum(probabilities)/len(probabilities) if probabilities else None,
                     "market_rule":"mean_of_saved_two_sided_books","saved_books":books})
    model_version="logistic-prior-v2:"+saved["model"]["logistic_weights_sha256"][:16]
    forecast_id=_digest_json([event_id,model_version,saved["cutoff_at_utc"],rows])
    return append_entry(ledger,{"kind":"freeze","forecast_id":forecast_id,"event_id":event_id,
        "event_name":saved["event_name"],"event_start_at_utc":saved["event_start_at_utc"],
        "model_version":model_version,"cutoff_at_utc":saved["cutoff_at_utc"],"rows":rows,
        "research_only":True,"promotion_eligible":False,"wager_authorization":False,
        "recovered_saved_forecast":True,"recovered_after_event":utc_now()>=parse_utc(saved["event_start_at_utc"]),
        "source_path":_portable_path(source_path),"source_sha256":file_sha256(source_path),
        "input_hashes":{key:value for key,value in saved.items() if key.endswith("sha256")},
        "model_metadata":saved["model"]})


def freeze_event(connection,event_id,*,ledger=DEFAULT_LEDGER,model_version=None):
    event=connection.execute("SELECT * FROM events WHERE event_id=?",(event_id,)).fetchone()
    if event is None or not event["start_time_utc"] or utc_now() >= parse_utc(event["start_time_utc"]):
        raise ValueError("Freeze requires an event with a known future start")
    predictions=connection.execute(
        """SELECT p.*,b.fighter_a_id,b.fighter_b_id,fa.canonical_name AS fighter_a,
                  fb.canonical_name AS fighter_b FROM predictions p JOIN bouts b ON b.bout_id=p.bout_id
           JOIN fighters fa ON fa.fighter_id=b.fighter_a_id JOIN fighters fb ON fb.fighter_id=b.fighter_b_id
           WHERE b.event_id=? AND b.status='scheduled' ORDER BY p.generated_at_utc DESC,p.prediction_id DESC""",(event_id,)).fetchall()
    if not predictions:
        raise ValueError("Score the event before freezing its forecasts")
    chosen=next((p for p in predictions if model_version is None or p["model_version"]==model_version),None)
    if chosen is None:
        raise ValueError("No saved predictions for that model version")
    version,cutoff=chosen["model_version"],chosen["feature_cutoff_at_utc"]
    if parse_utc(cutoff) >= parse_utc(event["start_time_utc"]):
        raise ValueError("Forecast cutoff is not pre-event")
    rows=[]; research=False; seen=set()
    for p in predictions:
        if p["model_version"] != version or p["feature_cutoff_at_utc"] != cutoff:
            continue
        if p["bout_id"] in seen:
            continue
        seen.add(p["bout_id"])
        if parse_utc(p["generated_at_utc"]) >= parse_utc(event["start_time_utc"]):
            raise ValueError("Prediction was generated after event start")
        metadata=json.loads(p["metadata_json"] or "{}")
        bout=connection.execute("SELECT * FROM bouts WHERE bout_id=?",(p["bout_id"],)).fetchone()
        market=verified_market(connection,bout,parse_utc(cutoff),metadata.get("market_max_age_hours",24),
                               snapshot=metadata.get("market_snapshot_at_utc"),
                               bookmaker=metadata.get("market_bookmaker"))
        if metadata.get("market_no_vig_probability") is not None and (
                market is None or not math.isclose(market,metadata['market_no_vig_probability'],abs_tol=1e-12)):
            raise ValueError("Saved forecast market evidence changed")
        research=research or metadata.get("training_history")=="research_retrospective"
        rows.append({"bout_id":p["bout_id"],"fighter_a_id":p["fighter_a_id"],"fighter_b_id":p["fighter_b_id"],
                     "fighter_a":p["fighter_a"],"fighter_b":p["fighter_b"],"model_probability":p["p_fighter_a"],
                     "market_no_vig_probability":market,"market_rule":"freshest_verified_two_sided_book",
                     "prediction_id":p["prediction_id"],"prediction_metadata":metadata})
    scheduled=connection.execute("SELECT COUNT(*) FROM bouts WHERE event_id=? AND status='scheduled'",(event_id,)).fetchone()[0]
    if len(rows) != scheduled:
        raise ValueError("Freeze needs predictions for the entire scheduled card at one model/cutoff")
    forecast_id=_digest_json([event_id,version,cutoff,rows])
    return append_entry(ledger,{"kind":"freeze","forecast_id":forecast_id,"event_id":event_id,
        "event_name":event["name"],"event_start_at_utc":event["start_time_utc"],"model_version":version,
        "cutoff_at_utc":cutoff,"rows":rows,"research_only":research,"promotion_eligible":False,
        "wager_authorization":False,"recovered_saved_forecast":False,"rows_sha256":_digest_json(rows)})


def _loss(probability,outcome):
    return -math.log(max(1e-12,min(1-1e-12,probability if outcome else 1-probability)))


def settle_event(event_id,*,ledger=DEFAULT_LEDGER,completed_manifest=None,connection=None):
    entries=read_entries(ledger)
    settled={e["forecast_id"] for e in entries if e["kind"]=="settle"}
    freezes=[e for e in entries if e["kind"]=="freeze" and e["event_id"]==event_id and e["forecast_id"] not in settled]
    if not freezes:
        raise ValueError("No unsettled frozen forecasts for this event")
    if completed_manifest:
        from scripts.capture_completed_event import verify_completed_capture
        capture=verify_completed_capture(completed_manifest)
        source_rows=capture["completed_bouts"]
        source={"manifest_path":_portable_path(completed_manifest),"manifest_sha256":file_sha256(completed_manifest),
                "source_sha256":capture["source"]["raw_sha256"],"fetched_at_utc":capture["source"]["fetched_at_utc"],
                "source_revision_id":capture["source"]["revision_id"]}
    elif connection is not None:
        source_rows=[{"fighter_a":{"stable_id":r["fighter_a_id"]},"fighter_b":{"stable_id":r["fighter_b_id"]},
                      "winner_fighter_id":r["winner_fighter_id"],"outcome":r["outcome"],"method":r["method"],
                      "status":"reviewed_database_result","hold_reasons":[]} for r in connection.execute(
            """SELECT b.*,r.outcome,r.winner_fighter_id,r.method FROM bouts b
               JOIN results r ON r.bout_id=b.bout_id WHERE b.event_id=? AND b.status='completed'""",(event_id,))]
        source={"reviewed_results_sha256":_digest_json(source_rows)}
    else:
        raise ValueError("Settlement needs a completed capture or reviewed database results")
    by_pair={}
    for r in source_rows:
        a,b=r["fighter_a"].get("stable_id"),r["fighter_b"].get("stable_id")
        if not a or not b or a==b:
            continue
        key=frozenset((a,b))
        if key in by_pair:
            raise ValueError("Ambiguous duplicate result pair")
        by_pair[key]=r
    results=[]
    for freeze in freezes:
        if utc_now() < parse_utc(freeze["event_start_at_utc"]):
            raise ValueError("Cannot settle before the frozen event start")
        evaluated,pending=[],[]
        for row in freeze["rows"]:
            observed=by_pair.get(frozenset((row["fighter_a_id"],row["fighter_b_id"])))
            if observed is None or observed["outcome"]!="win":
                pending.append({"bout_id":row["bout_id"],"reason":"unresolved_pair_or_nonbinary_result"})
                continue
            if observed.get("hold_reasons") and not (freeze["research_only"] and
                    set(observed["hold_reasons"]) <= {"weight_class_changed"}):
                pending.append({"bout_id":row["bout_id"],"reason":"operating_result_review_hold"})
                continue
            winner=observed["winner_fighter_id"]
            if winner not in (row["fighter_a_id"],row["fighter_b_id"]):
                raise ValueError("Result winner is not a frozen participant")
            target=int(winner==row["fighter_a_id"])
            model,market=row["model_probability"],row["market_no_vig_probability"]
            evaluated.append({**row,"outcome":target,"winner_fighter_id":winner,"method":observed["method"],
                "result_hold_reasons":observed.get("hold_reasons",[]),
                "model_log_loss":_loss(model,target),"model_brier":(model-target)**2,
                "model_correct":int((model>=.5)==bool(target)),
                "market_log_loss":_loss(market,target) if market is not None else None,
                "market_brier":(market-target)**2 if market is not None else None})
        if pending:
            # Leave unsettled until the whole frozen card has resolvable results.
            raise ValueError(f"Settlement pending review for {len(pending)} frozen bouts: {pending}")
        entry=append_entry(ledger,{"kind":"settle","forecast_id":freeze["forecast_id"],"event_id":event_id,
                                  "freeze_entry_sha256":freeze["entry_sha256"],"rows":evaluated,
                                  "source":source,"research_only":freeze["research_only"],"pending":pending})
        results.append(entry)
    return results


def _metrics(rows):
    if not rows:
        return {"bouts":0,"model":None,"model_on_market_subset":None,"market":None}
    observed=[r for r in rows if r["market_no_vig_probability"] is not None]
    return {"bouts":len(rows),"model":binary_metrics([r["model_probability"] for r in rows],[r["outcome"] for r in rows]),
            "model_on_market_subset":binary_metrics([r["model_probability"] for r in observed],[r["outcome"] for r in observed]) if observed else None,
            "market":binary_metrics([r["market_no_vig_probability"] for r in observed],[r["outcome"] for r in observed]) if observed else None}


def summary(ledger=DEFAULT_LEDGER):
    entries=read_entries(ledger)
    freezes={e["forecast_id"]:e for e in entries if e["kind"]=="freeze"}
    settled=[e for e in entries if e["kind"]=="settle"]
    events=[]; all_rows=[]
    for entry in settled:
        freeze=freezes[entry["forecast_id"]]
        events.append({"event_id":entry["event_id"],"event_name":freeze["event_name"],
                       "model_version":freeze["model_version"],"cutoff_at_utc":freeze["cutoff_at_utc"],
                       "research_only":freeze["research_only"],"recovered_saved_forecast":freeze["recovered_saved_forecast"],
                       "forecast_id":entry["forecast_id"],"metrics":_metrics(entry["rows"]),"rows":entry["rows"]})
        all_rows.extend(entry["rows"])
    return {"schema_version":1,"ledger_path":_portable_path(ledger),"ledger_entries":len(entries),
            "hash_chain_verified":True,"frozen_forecasts":len(freezes),"settled_forecasts":len(settled),
            "events":events,"cumulative":_metrics(all_rows),
            "wager_authorization":False,"closing_proxy_note":"CLV requires a later verified pre-start snapshot; early saved prices are not closing prices"}
