# Review unresolved historical fighter identities

The original expanded 1993–2026 Wikipedia result import held **1,677 bouts** because at least one fighter lacked a verified stable ID. Its local worksheet, `reports/wikipedia_1993_2026_identity_review.json`, records that pre-replay state: **2,051 unresolved fighter appearances across 913 distinct printed names**. Identical rows from repeated import passes are counted once and conflicting duplicates fail. The original 2011–2025 subset held 1,164 bouts and had 582 distinct printed names. After one scoped, manually reviewed 2022 replay, the current database has **1,676 held bouts and 7,229 accepted results**. The evaluator checks the old worksheet against exact source and crosswalk receipts, then subtracts the one recovered position. The name printed in an event result is **not** a unique person identifier. A worksheet provides evidence leads; it never changes the database or approves a crosswalk by itself.

Run from the repository root:

```bash
python -m ufc_odds_model.identity_review \
  --db data/ufc_research_2011_2025.sqlite \
  --review reports/wikipedia_2011_2025_review.json \
  --review reports/wikipedia_2011_2025_embedded_review.json \
  --output reports/wikipedia_identity_candidates.json \
  --lookup-wikipedia --inspect-candidate-pages
```

The command verifies each saved event-page payload against its SHA-256 receipt and joins every unresolved row to that page's revision. The existing import review files do not contain per-event revision IDs. If the database has more than one distinct saved revision for a reviewed event page, the worksheet stops instead of guessing which revision produced the row. For such a page, rebuild the intended import into a fresh research database before producing the worksheet; a future revision-bound report format could also resolve this ambiguity. It groups occurrences by **exact Unicode spelling**, lists any existing fighter whose canonical name exactly matches, and identifies direct same-revision wiki links with the same visible name. The optional API lookup asks MediaWiki for a current page with that title; the page inspection retains the source revision and checks whether it contains MMA/UFC language and a held-bout opponent's name. Those current-page checks are text clues, not identity proof or evidence available before a past fight. Each API response and inspected page is retained by content hash under `data/raw/wikipedia/identity_review/`; its request URL, file path, hash, and revision appear in the worksheet.

At the initial 2026-09-26 pass, seven names (21 unresolved appearances) exactly matched an existing fighter ID. Current title lookup returned 80 page candidates, but only 15 pages contained MMA text. Thirteen also mentioned UFC and at least one recorded opponent. **Twenty-two held bouts** had that stronger text clue for every unresolved fighter in the bout. A broad title-only count of 145 held bouts includes unrelated namesakes and is strictly a review queue, not recoverable training rows. The worksheet resolved zero bouts automatically.

The all-year offline worksheet lists 13 printed names that exactly match an existing fighter and five with a matching visible link in the saved event revision. A separate local 2026 candidate worksheet, `reports/wikipedia_2026_identity_candidates.json`, checks current titles for that year. These are review leads. **One** of the original 1,677 held bouts was manually reviewed and added: the Denise Gomes–Loma Lookboonmee result from September 17, 2022. The exact [scoped decision](REVIEWED_IDENTITY_CROSSWALK_2022.json) binds its event, bout position, source revision and SHA-256, and stable page ID; the [official UFC event page](https://www.ufc.com/event/ufc-fight-night-september-17-2022) corroborates the matchup and Loma's unanimous-decision win. No other name or occurrence was merged.

## Reviewer workflow

1. Open `reports/wikipedia_identity_candidates.json`. Prioritize entries with `current_page_evidence.mentioned_opponents_from_held_bouts` and inspect the saved **event revision** and **candidate page revision** links. A current page may have been edited long after the event.
2. Compare the fighter's opponent, event/date, and personal details with an independent licensed source if available. Reject namesakes, redirects to another person, and conflicting fighter IDs. A shared name or a single wiki page link alone is insufficient.
3. Create a separate schema-v2 reviewed crosswalk using the format in [Wikipedia history source](WIKIPEDIA_HISTORY_SOURCE.md). Enter `reviewed_by` as the actual reviewer, specific `evidence_urls`, the confirmed stable `fighter_id`, and `page_title` for Wikipedia IDs. Bind every decision to an exact event ID, bout position, source revision, and payload SHA-256. Schema-v1 global-name crosswalks are rejected. Do not copy the candidate worksheet directly into the importer; it intentionally has no approval field.
4. Re-run the specific event or embedded importer with `--crosswalk`, then run integrity, audit, and the chronological backtest. Compare imported/skipped bout counts and inspect any existing-ID or outcome conflict before accepting the new database version. The exact crosswalk bytes are retained as a receipt by the importer. A same-revision count change is accepted by the dashboard and evaluator only when the receipt proves prior held-state, scoped decisions, and matching stored results; a later replay must preserve earlier decisions.

This process changes research coverage only after an explicit identity review. It does not provide point-in-time features, bookmaker quotes, or a validated betting edge.
