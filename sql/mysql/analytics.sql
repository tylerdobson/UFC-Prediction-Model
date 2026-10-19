-- Read-only portfolio queries for MySQL 8.4+ after a successful publication.
-- Views bind to exactly one active, committed snapshot.

-- Source hashes, capture time, row counts and retained transformation metadata.
SELECT run_id, source_sha256, source_manifest_sha256, source_content_sha256,
       published_at_utc, source_captured_at_utc, counts_json
FROM ufc_active_publication;

-- All canonical cards, including held and incomplete bouts.
SELECT event_date, name, bouts, usable_stat_bouts, held_stat_bouts,
       timed_stat_bouts, both_reach_stat_bouts
FROM ufc_card_coverage
ORDER BY event_date DESC, event_id;

-- Audit unresolved data rather than dropping it from coverage denominators.
SELECT gap_kind, field, reason, COUNT(*) AS affected_rows
FROM ufc_data_quality
GROUP BY gap_kind, field, reason
ORDER BY affected_rows DESC;

-- Missing or invalid per-round fields, including explicit control normalization.
SELECT bout_id, fighter_id, round_number, source_row_number, source_errors_json
FROM ufc_round_quality
ORDER BY bout_id, fighter_id, round_number;

-- Complete career history for an exact canonical fighter identity.
-- This is descriptive and must not be substituted into historical forecasts.
SET @fighter_id = 'ufcstats:fighter:CHANGE_TO_RETAINED_ID';
SELECT event_date, opponent_name, outcome, winner_fighter_id, elapsed_seconds,
       sig_landed, sig_attempted, opponent_sig_landed,
       sig_landed_per_minute, sig_absorbed_per_minute, td_landed, control_seconds
FROM ufc_fighter_history
WHERE fighter_id = @fighter_id
ORDER BY event_date, bout_id;

-- Past-only timed strike summary with the V6 two-day event-date embargo.
-- Rates exclude unknown observations AND their exposure; NULL is never zero.
SET @target_date = DATE('2026-10-24');
SET @decision_date = DATE('2026-10-19');
SELECT fighter_id, COUNT(*) AS earlier_stat_bouts,
       SUM(CASE WHEN sig_landed IS NOT NULL AND elapsed_seconds > 0 THEN sig_landed END)
       / NULLIF(SUM(CASE WHEN sig_landed IS NOT NULL AND elapsed_seconds > 0
                         THEN elapsed_seconds / 60.0 END), 0) AS past_sig_landed_per_minute,
       SUM(CASE WHEN opponent_sig_landed IS NOT NULL AND elapsed_seconds > 0
                THEN opponent_sig_landed END)
       / NULLIF(SUM(CASE WHEN opponent_sig_landed IS NOT NULL AND elapsed_seconds > 0
                         THEN elapsed_seconds / 60.0 END), 0) AS past_sig_absorbed_per_minute
FROM ufc_fighter_history
WHERE fighter_id = @fighter_id
  AND event_date < DATE_SUB(@target_date, INTERVAL 2 DAY)
  AND event_date < @decision_date
GROUP BY fighter_id;

-- A multi-query dashboard should pin a snapshot in one read-only transaction.
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT run_id FROM ufc_active_publication;
SELECT SUM(bouts) AS known_event_bouts, SUM(usable_stat_bouts) AS usable_bouts
FROM ufc_card_coverage;
COMMIT;
