# Wikipedia historical result bootstrap

This importer is **research-only**. It uses English Wikipedia's official [MediaWiki REST page-source API](https://www.mediawiki.org/wiki/API:REST_API/Reference#Get_page_source), which returns wikitext plus a page ID, revision ID, and license. It reads the `MMAevent bout` result templates, never website HTML. The current adapter is intentionally bounded to UFC 295–304, whose template format was checked in September 2026: 128 completed result rows across ten events (127 wins and one draw). This is enough raw outcome history for an initial date-conservative Elo experiment **after identity review**; it is not evidence of a betting edge.

## Source and license

The event pages are available under [Creative Commons Attribution-ShareAlike 4.0](https://foundation.wikimedia.org/wiki/Terms_of_Use?useformat=mobile). Store and display the source page link and revision for attribution, identify any changes, and review share-alike obligations before redistributing adapted tables. The importer stores the page URL, revision ID and timestamp, license title and URL, and the SHA-256 of its raw JSON snapshot in `wikipedia_source_receipts` joined to `ingestion_runs`. The snapshot itself lives in `data/raw/wikipedia/` by default. [Wikimedia API etiquette](https://www.mediawiki.org/wiki/API:Etiquette) requires an identifying User-Agent and considerate serial requests; the client also retries throttled requests with backoff.

Event IDs use Wikipedia page IDs (`wikipedia_research:<page id>`). Linked fighters use their resolved page IDs (`wikipedia:<page id>`). A bout ID combines the event ID and sorted reviewed fighter IDs. The source page ID is stable across ordinary title changes; each source revision is separately recorded. Wikipedia supplies no native bout ID.

## First import and review

Use a **separate research database**. The CLI rejects the default operating database path, and pre-fight alerts and paper bets refuse any database containing `wikipedia_research` events.

```bash
ufc-model --db data/ufc_research.sqlite import-wikipedia-history \
  --first-event 295 --last-event 304 \
  --raw-dir data/raw/wikipedia \
  --review-out reports/wikipedia_identity_review.json
```

The first pass imports only bouts whose two fighter identities resolve to stable Wikipedia page IDs. Plain-text fighter names and broken or ambiguous page links are listed in the review JSON; affected bouts are skipped. Among UFC 295–304, 41 fighter slots are plain text in the current revisions. Do not assume each represents a unique person.

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

Then rerun the command with `--crosswalk path/to/reviewed_crosswalk.json` against the same research database. The review output should show zero skipped bouts before claiming the full 128-row sample. Audit changed outcomes, aliases, draws, no contests, and any opponent substitutions against independent records. Ambiguous result templates stop the import rather than guessing.

## What this history cannot prove

Wikipedia's result table is a retrospective record. It does not provide an exact event start in UTC, when each pairing was announced, when a substitution became known, or historical pre-fight bookmaker quotes. Today's page revision also cannot prove when an outcome correction or roster edit first became known. The existing `backtest` can run a date-conservative Elo study from completed results, but that retrospective study has this point-in-time limit. The stricter `evaluate` workflow currently excludes events without exact start timestamps, so this import alone does **not** unlock logistic calibration or bookmaker comparisons. Keep research outputs separate from live alerting until independently verified pre-fight timestamps, prices, and point-in-time features are available.

This source also differs from a UFCStats-derived Kaggle download: a dataset uploader's CC0 label does not establish rights to the upstream material. [Sportradar's current trial terms](https://developer.sportradar.com/sportradar-updates/page/terms-and-conditions) limit its free trial to internal product evaluation and require written approval for wagering use.
