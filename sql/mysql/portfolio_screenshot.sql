-- Run this entire read-only statement in MySQL Workbench.
-- Screenshot the result grid: it describes the current complete publication.
USE ufc_warehouse;

WITH archive AS (
    SELECT COUNT(*) AS bouts,
           SUM(stats_status = 'usable') AS usable_bouts,
           SUM(stats_status = 'held') AS held_bouts
    FROM ufc_active_bouts
), facts AS (
    SELECT
        (SELECT COUNT(*) FROM ufc_fighter_history) AS fighter_bout_rows,
        (SELECT COUNT(*)
         FROM ufc_wh_fact_fighter_round r
         JOIN ufc_active_publication p ON p.run_id = r.run_id) AS fighter_round_rows,
        (SELECT COUNT(*)
         FROM ufc_wh_fact_fighter_bout f
         JOIN ufc_active_bouts b
           ON b.run_id = f.run_id AND b.bout_id = f.bout_id
         WHERE b.stats_status = 'held') AS held_fact_rows
), checks AS (
    SELECT 1 AS display_order, 'Cards' AS metric,
           (SELECT COUNT(*) FROM ufc_active_events) AS row_count,
           'LOADED' AS result
    UNION ALL
    SELECT 2, 'Fighter profiles',
           (SELECT COUNT(*) FROM ufc_active_fighters), 'LOADED'
    UNION ALL
    SELECT 3, 'Archive bouts', bouts, 'LOADED' FROM archive
    UNION ALL
    SELECT 4, 'Usable paired-stat bouts', usable_bouts, 'ACCEPTED' FROM archive
    UNION ALL
    SELECT 5, 'Held bouts', held_bouts, 'QUARANTINED' FROM archive
    UNION ALL
    SELECT 6, 'Fighter-bout facts', fighter_bout_rows,
           IF(fighter_bout_rows = usable_bouts * 2, 'PASS: 2 per usable bout', 'FAIL')
    FROM facts CROSS JOIN archive
    UNION ALL
    SELECT 7, 'Fighter-round facts', fighter_round_rows, 'LOADED' FROM facts
    UNION ALL
    SELECT 8, 'Facts from held bouts', held_fact_rows,
           IF(held_fact_rows = 0, 'PASS: held facts excluded', 'FAIL')
    FROM facts
    UNION ALL
    SELECT 9, 'Bouts reconciled', usable_bouts + held_bouts,
           IF(usable_bouts + held_bouts = bouts, 'PASS: accepted + held = total', 'FAIL')
    FROM archive
)
SELECT metric AS `Pipeline metric`, row_count AS `Rows`, result AS `Verification`
FROM checks
ORDER BY display_order;
