-- Preserve the original paper decision while allowing its settlement to be recorded.
-- This migration also applies to databases that already ran 005 and 006.
CREATE TRIGGER IF NOT EXISTS paper_bets_freeze_decision
BEFORE UPDATE ON paper_bets
WHEN NEW.paper_bet_id IS NOT OLD.paper_bet_id
  OR NEW.prediction_id IS NOT OLD.prediction_id
  OR NEW.quote_id IS NOT OLD.quote_id
  OR NEW.event_id IS NOT OLD.event_id
  OR NEW.bout_id IS NOT OLD.bout_id
  OR NEW.selection_fighter_id IS NOT OLD.selection_fighter_id
  OR NEW.bookmaker IS NOT OLD.bookmaker
  OR NEW.decision_at_utc IS NOT OLD.decision_at_utc
  OR NEW.recorded_at_utc IS NOT OLD.recorded_at_utc
  OR NEW.quote_captured_at_utc IS NOT OLD.quote_captured_at_utc
  OR NEW.quoted_decimal_odds IS NOT OLD.quoted_decimal_odds
  OR NEW.model_probability IS NOT OLD.model_probability
  OR NEW.expected_profit_per_unit IS NOT OLD.expected_profit_per_unit
  OR NEW.bankroll_units IS NOT OLD.bankroll_units
  OR NEW.per_bet_cap_units IS NOT OLD.per_bet_cap_units
  OR NEW.event_cap_units IS NOT OLD.event_cap_units
  OR NEW.stake_units IS NOT OLD.stake_units
  OR NEW.gate_check_id IS NOT OLD.gate_check_id
BEGIN
    SELECT RAISE(ABORT, 'paper decision fields are immutable');
END;

CREATE TRIGGER IF NOT EXISTS paper_bets_no_delete
BEFORE DELETE ON paper_bets
BEGIN
    SELECT RAISE(ABORT, 'paper decisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS wikipedia_source_receipts_no_update
BEFORE UPDATE ON wikipedia_source_receipts
BEGIN
    SELECT RAISE(ABORT, 'Wikipedia source receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS wikipedia_source_receipts_no_delete
BEFORE DELETE ON wikipedia_source_receipts
BEGIN
    SELECT RAISE(ABORT, 'Wikipedia source receipts are immutable');
END;
