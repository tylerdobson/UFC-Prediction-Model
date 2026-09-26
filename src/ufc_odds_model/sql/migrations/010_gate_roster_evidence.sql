-- A new accepted check must identify the exact dated card observation used
-- to verify its matchup. Older checks remain visible as legacy evidence, but
-- they cannot authorize new paper decisions.
ALTER TABLE prefight_gate_checks
    ADD COLUMN roster_snapshot_id INTEGER REFERENCES card_event_snapshots(event_snapshot_id);

CREATE TRIGGER prefight_gate_checks_require_roster
BEFORE INSERT ON prefight_gate_checks
WHEN NEW.gate_decision = 'alert_candidate' AND NOT EXISTS (
    SELECT 1
    FROM card_event_snapshots s
    JOIN card_bout_snapshots sb ON sb.event_snapshot_id = s.event_snapshot_id
    JOIN ingestion_runs r ON r.run_id = s.ingestion_run_id
    JOIN bouts b ON b.bout_id = sb.bout_id
    JOIN events e ON e.event_id = s.event_id
    WHERE s.event_snapshot_id = NEW.roster_snapshot_id
      AND s.event_id = NEW.event_id
      AND sb.bout_id = NEW.bout_id
      AND b.event_id = NEW.event_id
      AND b.fighter_a_id = sb.fighter_a_id
      AND b.fighter_b_id = sb.fighter_b_id
      AND b.status = 'scheduled'
      AND sb.bout_status = 'scheduled'
      AND (b.provider_status IS NULL OR b.provider_status IN ('scheduled', 'not_started'))
      AND (sb.bout_provider_status IS NULL OR sb.bout_provider_status IN ('scheduled', 'not_started'))
      AND e.status = 'scheduled'
      AND s.event_status = 'scheduled'
      AND (e.provider_status IS NULL OR e.provider_status IN ('scheduled', 'not_started'))
      AND (s.event_provider_status IS NULL OR s.event_provider_status IN ('scheduled', 'not_started'))
      AND e.start_time_utc = NEW.event_start_time_utc
      AND s.start_time_utc = NEW.event_start_time_utc
      AND julianday(s.source_observed_at_utc) IS NOT NULL
      AND julianday(r.fetched_at_utc) IS NOT NULL
      AND julianday(NEW.checked_at_utc) IS NOT NULL
      AND julianday(NEW.event_start_time_utc) IS NOT NULL
      AND julianday(s.source_observed_at_utc) <= julianday(r.fetched_at_utc)
      AND julianday(r.fetched_at_utc) <= julianday(NEW.checked_at_utc)
      AND julianday(s.source_observed_at_utc) < julianday(NEW.event_start_time_utc)
      AND julianday(NEW.checked_at_utc) < julianday(NEW.event_start_time_utc)
      AND s.event_snapshot_id = (
          SELECT s2.event_snapshot_id FROM card_event_snapshots s2
          JOIN ingestion_runs r2 ON r2.run_id = s2.ingestion_run_id
          WHERE s2.event_id = NEW.event_id
          ORDER BY r2.fetched_at_utc DESC, s2.event_snapshot_id DESC LIMIT 1
      )
)
BEGIN
    SELECT RAISE(ABORT, 'accepted gate requires matching dated roster evidence');
END;

DROP TRIGGER paper_bets_require_accepted_gate;
CREATE TRIGGER paper_bets_require_accepted_gate
BEFORE INSERT ON paper_bets
WHEN NEW.gate_check_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM prefight_gate_checks g
    WHERE g.gate_check_id = NEW.gate_check_id
      AND g.gate_decision = 'alert_candidate'
      AND g.roster_snapshot_id IS NOT NULL
      AND g.prediction_id = NEW.prediction_id
      AND g.quote_id = NEW.quote_id
      AND g.event_id = NEW.event_id
      AND g.bout_id = NEW.bout_id
      AND g.bookmaker = NEW.bookmaker
      AND g.quoted_decimal_odds = NEW.quoted_decimal_odds
      AND NEW.selection_fighter_id = (
          SELECT q.selection_fighter_id FROM odds_quotes q WHERE q.quote_id = NEW.quote_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'paper decision requires a matching accepted pre-fight gate check');
END;
