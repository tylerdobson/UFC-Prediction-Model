# Wikipedia historical result bootstrap

This importer is **research-only**. It uses English Wikipedia's official [MediaWiki REST page-source API](https://www.mediawiki.org/wiki/API:REST_API/Reference#Get_page_source), which returns wikitext plus a page ID, revision ID, and license. It reads the numbered event catalog on each `YYYY in UFC` page and the `MMAevent bout` result templates on linked event pages, never website HTML. A second command reads explicitly reviewed event sections embedded in year and *The Ultimate Fighter* season pages. Both commands default to 2011–2025, but accept 1993 through the current year; a separate [reviewed date import](WIKIPEDIA_DATE_RECONCILIATION.md) covers four conflicting calendar dates. The original UFC 295–304 command remains available as a small smoke test. This is outcome history for a date-conservative Elo experiment **after identity and result review**; it is not evidence of a betting edge. The [dataset card](RESEARCH_DATASET_CARD.md) records measured 1993–2026 coverage and exceptions from the 2026-09-26 import.

## Source and license

The event pages are available under [Creative Commons Attribution-ShareAlike 4.0](https://foundation.wikimedia.org/wiki/Terms_of_Use?useformat=mobile). Store and display the source page link and revision for attribution, identify any changes, and review share-alike obligations before redistributing adapted tables. The importer stores each event page URL, revision ID and timestamp, license title and URL, and the SHA-256 of its raw JSON snapshot in `wikipedia_source_receipts` joined to `ingestion_runs`. It also stores raw year catalog payloads and their source/revision/license details in the review report. Every Action API title lookup used to resolve fighter or canonical event page IDs is retained with its request URL, response, SHA-256, and ingestion receipt under `data/raw/wikipedia/action_api/`. Snapshots live in `data/raw/wikipedia/` by default. [Wikimedia API etiquette](https://www.mediawiki.org/wiki/API:Etiquette) requires an identifying User-Agent and considerate serial requests; the client also retries throttled requests with backoff.

Event IDs use Wikipedia page IDs (`wikipedia_research:<page id>`). Linked fighters use their resolved page IDs (`wikipedia:<page id>`). A bout ID combines the event ID and sorted reviewed fighter IDs. The source page ID is stable across ordinary title changes; each source revision is separately recorded. Wikipedia supplies no native bout ID.

## Historical import and review

Use a **separate research database**. The CLI rejects the default operating database path, and pre-fight alerts and paper bets refuse any database containing `wikipedia_research` events.

```bash
ufc-model --db data/ufc_research_2011_2025.sqlite init-db
ufc-model --db data/ufc_research_2011_2025.sqlite import-wikipedia-years \
  --first-year 2011 --last-year 2025 \
  --raw-dir data/raw/wikipedia \
  --review-out reports/wikipedia_2011_2025_review.json
ufc-model --db data/ufc_research_2011_2025.sqlite import-wikipedia-embedded \
  --first-year 2011 --last-year 2025 \
  --raw-dir data/raw/wikipedia \
  --review-out reports/wikipedia_2011_2025_embedded_review.json
python -m ufc_odds_model.integrity \
  --db data/ufc_research_2011_2025.sqlite \
  --output reports/wikipedia_2011_2025_integrity.json
ufc-model --db data/ufc_research_2011_2025.sqlite audit
ufc-model --db data/ufc_research_2011_2025.sqlite backtest
```

The first pass imports only bouts whose two fighter identities resolve to stable Wikipedia page IDs. Plain-text fighter names and broken or ambiguous page links are listed in the review JSON; affected bouts are skipped. Catalog rows that are canceled are excluded. The embedded-card command accepts only a named section with one bounded result table and an event date matching the catalog; reviewed overrides cover older anchors whose text no longer matches a heading. Event pages whose results cannot be parsed or whose page ID/date differs from the catalog are skipped and counted. Do not assume that a plain-text name uniquely identifies a person.

Review each unresolved name against source evidence and create a JSON crosswalk. A manually assigned ID must be stable and must be reused for that person across all events. If a plain-text name belongs to a fighter with a Wikipedia page, use its verified page ID instead; the importer checks `page_title` against the official API before accepting it.

```json
{
  "schema_version": 1,
  "fighters": {
    "Example unlinked name": {
      "fighter_id": "manual:person-specific-stable-id",
      "canonical_name": "Example unlinked name",
      "evidence_url": "https://example.org/identity-evidence",
      "reviewed_by": "your name"
    },
    "Example with a Wikipedia page": {
      "fighter_id": "wikipedia:12345678",
      "canonical_name": "Example with a Wikipedia page",
      "page_title": "Example with a Wikipedia page",
      "evidence_url": "https://en.wikipedia.org/wiki/Example_with_a_Wikipedia_page",
      "reviewed_by": "your name"
    }
  }
}
```

Then rerun the command with `--crosswalk path/to/reviewed_crosswalk.json` against the same research database. Its exact JSON bytes are retained under `data/raw/wikipedia/crosswalk/` with an ingestion receipt linked in the review report. Check the review report's catalog, event-page, fighter, and bout counts before claiming coverage. Audit changed outcomes, aliases, draws, no contests, and any opponent substitutions against independent records. Ambiguous result templates stop that event-page import rather than guessing.

## What this history cannot prove

Wikipedia's result table is a retrospective record. It does not provide an exact event start in UTC, when each pairing was announced, when a substitution became known, or historical pre-fight bookmaker quotes. Today's page revision also cannot prove when an outcome correction or roster edit first became known. The existing `backtest` can run a date-conservative Elo study from completed results, but that retrospective study has this point-in-time limit. The stricter `evaluate` workflow currently excludes events without exact start timestamps, so this import alone does **not** unlock logistic calibration or bookmaker comparisons. Keep research outputs separate from live alerting until independently verified pre-fight timestamps, prices, and point-in-time features are available.

This source also differs from a UFCStats-derived Kaggle download: a dataset uploader's CC0 label does not establish rights to the upstream material. [Sportradar's current trial terms](https://developer.sportradar.com/sportradar-updates/page/terms-and-conditions) limit its free trial to internal product evaluation and require written approval for wagering use.
