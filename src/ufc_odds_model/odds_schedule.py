"""Source-timed T-7d/T-24h/T-2h captures and observed closing-line values."""
import json
from datetime import timedelta
from pathlib import Path

from . import odds_api
from .ingest import import_live_odds
from .market_guard import verified_market
from .pipeline import parse_utc,utc_now,utc_string
from .quote_evidence import linked_quote_rows,quote_receipt_matches
from .raw_snapshots import retain_snapshot

SLOTS={"t7d":168,"t24h":24,"t2h":2}


def schedule(connection,event_id):
    event=connection.execute("SELECT * FROM events WHERE event_id=?",(event_id,)).fetchone()
    if event is None or not event['start_time_utc']:
        raise ValueError("Odds schedule requires an event with a verified UTC start")
    start=parse_utc(event['start_time_utc'])
    return {"event_id":event_id,"start_time_utc":utc_string(start),"slots":[
        {"slot":slot,"due_at_utc":utc_string(start-timedelta(hours=hours)),
         "command":f"ufc-model odds-schedule {event_id} --capture --slot {slot}"} for slot,hours in SLOTS.items()],
        "closing_proxy":"last verified two-sided snapshot before event start"}


def capture_slot(connection,event_id,slot,api_key,*,raw_dir="data/raw/odds",regions="us",tolerance_minutes=60):
    if slot not in SLOTS or not 0 < tolerance_minutes <= 120:
        raise ValueError("Choose t7d/t24h/t2h and a tolerance between 0 and 120 minutes")
    plan=schedule(connection,event_id)
    now=utc_now(); start=parse_utc(plan['start_time_utc'])
    due=start-timedelta(hours=SLOTS[slot])
    status=connection.execute("SELECT status,provider_status FROM events WHERE event_id=?",(event_id,)).fetchone()
    if status['status']!='scheduled' or status['provider_status'] not in (None,'scheduled','not_started') or now>=start:
        raise ValueError("Scheduled captures require an unstarted scheduled event")
    if abs((now-due).total_seconds()) > tolerance_minutes*60:
        raise ValueError("Capture is outside this slot's time window; do not backdate a snapshot")
    previous=connection.execute("SELECT * FROM odds_schedule_snapshots WHERE event_id=? AND slot=?",(event_id,slot)).fetchone()
    if previous:
        return {"status":"already_captured","event_id":event_id,"slot":slot,"ingestion_run_id":previous['ingestion_run_id']}
    quota=odds_api.quota_status(api_key)
    if quota['remaining_requests']<50:
        raise RuntimeError("The Odds API quota is below the 50-request floor")
    raw,digest=retain_snapshot(json.dumps(quota,sort_keys=True).encode(),Path(raw_dir)/'quota',kind='odds quota header')
    with connection:
        connection.execute("INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES (?,?,?,?)",
                           ('the-odds-api-quota',quota['fetched_at_utc'],str(raw),digest))
    result=import_live_odds(connection,api_key,raw_dir,regions)
    captured=parse_utc(result['snapshot_at_utc'])
    if captured>=start or abs((captured-due).total_seconds()) > tolerance_minutes*60:
        raise ValueError("Fetched snapshot missed the pre-event slot window")
    with connection:
        connection.execute("INSERT INTO odds_schedule_snapshots VALUES (?,?,?,?,?)",
                           (event_id,slot,utc_string(due),result['ingestion_run_id'],quota['remaining_requests']))
    return {**result,"status":"captured","event_id":event_id,"slot":slot,"due_at_utc":utc_string(due),
            "remaining_requests_before_capture":quota['remaining_requests']}


def closing_line_value(connection):
    """Compare recorded paper/actual prices with the last same-book pre-start pair."""
    rows=[]
    sources=(('paper',"""SELECT paper_bet_id AS bet_id,bout_id,quote_id,selection_fighter_id,
                bookmaker,quoted_decimal_odds AS entry_decimal_odds,decision_at_utc AS decision
                FROM paper_bets"""),
             ('actual',"""SELECT b.bet_id,q.bout_id,b.quote_id,b.selection_fighter_id,q.bookmaker,
                 b.actual_decimal_odds AS entry_decimal_odds,b.placed_at_utc AS decision
                 FROM bets b JOIN odds_quotes q ON q.quote_id=b.quote_id"""))
    for kind,query in sources:
        for bet in connection.execute(query):
            bout=connection.execute("SELECT b.*,e.start_time_utc FROM bouts b JOIN events e ON e.event_id=b.event_id WHERE b.bout_id=?",
                                    (bet['bout_id'],)).fetchone()
            result={"ledger":kind,"bet_id":bet['bet_id'],"bout_id":bet['bout_id'],"status":"unavailable_without_later_snapshot"}
            if not bout['start_time_utc']:
                rows.append(result); continue
            start=parse_utc(bout['start_time_utc'])
            entry=connection.execute("SELECT captured_at_utc FROM odds_quotes WHERE quote_id=?",(bet['quote_id'],)).fetchone()
            quotes=linked_quote_rows(connection,bet['bout_id']); cache={}
            possible=sorted({q['captured_at_utc'] for q in quotes if q['bookmaker']==bet['bookmaker']
                and entry['captured_at_utc']<q['captured_at_utc']<bout['start_time_utc']
                and quote_receipt_matches(q,start,cache)},reverse=True)
            for captured in possible:
                market=verified_market(connection,bout,start,24*365,snapshot=captured,bookmaker=bet['bookmaker'])
                if market is None:
                    continue
                closing={float(q['decimal_odds']) for q in quotes if q['bookmaker']==bet['bookmaker']
                         and q['captured_at_utc']==captured and q['selection_fighter_id']==bet['selection_fighter_id']
                         and quote_receipt_matches(q,start,cache)}
                if len(closing)!=1:
                    continue
                price=next(iter(closing)); entry_price=float(bet['entry_decimal_odds'])
                close_probability=market if bet['selection_fighter_id']==bout['fighter_a_id'] else 1-market
                result.update(status='observed_closing_proxy',bookmaker=bet['bookmaker'],
                    entry_decimal_odds=entry_price,closing_decimal_odds=price,closing_captured_at_utc=captured,
                    price_clv=entry_price/price-1,closing_no_vig_probability=close_probability,
                    closing_probability_minus_entry_break_even=close_probability-1/entry_price)
                break
            rows.append(result)
    return {"rows":rows,"observed":sum(r['status']=='observed_closing_proxy' for r in rows),
            "definition":"entry_decimal/closing_decimal - 1; positive means a better recorded entry price",
            "closing_proxy":"last later verified same-book two-sided pre-start snapshot; not a claimed true closing price"}
