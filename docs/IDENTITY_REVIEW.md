# Review unresolved historical fighter identities

The expanded 1993–2026 Wikipedia result import holds **1,677 bouts** because at least one fighter lacks a verified stable ID. Its local worksheet, `reports/wikipedia_1993_2026_identity_review.json`, contains **2,051 unresolved fighter appearances across 913 distinct printed names**; identical rows from repeated import passes are counted once and conflicting duplicates fail. The original 2011–2025 subset held 1,164 bouts and had 582 distinct printed names. The name printed in an event result is **not** a unique person identifier. This worksheet helps a reviewer find evidence; it never changes the database or creates a crosswalk.

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

At the 2026-09-26 pass, seven names (21 unresolved appearances) exactly matched an existing fighter ID. Current title lookup returned 80 page candidates, but only 15 pages contained MMA text. Thirteen also mentioned UFC and at least one recorded opponent. **Twenty-two held bouts** had that stronger text clue for every unresolved fighter in the bout. A broad title-only count of 145 held bouts includes unrelated namesakes and is strictly a review queue, not recoverable training rows. The worksheet resolved zero bouts automatically.

The all-year offline worksheet lists 13 printed names that exactly match an existing fighter and five with a matching visible link in the saved event revision. A separate local 2026 candidate worksheet, `reports/wikipedia_2026_identity_candidates.json`, checks current titles for that year. These are review leads. **Zero** of the 1,677 held bouts were automatically approved or added.

## Reviewer workflow

1. Open `reports/wikipedia_identity_candidates.json`. Prioritize entries with `current_page_evidence.mentioned_opponents_from_held_bouts` and inspect the saved **event revision** and **candidate page revision** links. A current page may have been edited long after the event.
2. Compare the fighter's opponent, event/date, and personal details with an independent licensed source if available. Reject namesakes, redirects to another person, and conflicting fighter IDs. A shared name or a single wiki page link alone is insufficient.
3. Create a separate reviewed crosswalk using the format in [Wikipedia history source](WIKIPEDIA_HISTORY_SOURCE.md). Enter `reviewed_by` as the actual reviewer, a specific `evidence_url`, the confirmed stable `fighter_id`, and `page_title` for Wikipedia IDs. Do not copy the candidate worksheet directly into the importer; it intentionally has no approval field.
4. Re-run the broad and embedded importers with `--crosswalk`, then run integrity, audit, and the chronological backtest. Compare imported/skipped bout counts and inspect any existing-ID or outcome conflict before accepting the new database version. The exact crosswalk bytes are retained as a receipt by the importer.

This process changes research coverage only after an explicit identity review. It does not provide point-in-time features, bookmaker quotes, or a validated betting edge.
