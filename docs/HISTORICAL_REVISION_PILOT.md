# Historical revision pilot

The completed UFC research database contains retrospective results, but its
current Wikipedia page snapshots do not establish who was scheduled or which
results were published at an earlier model decision. This pilot adds an
**offline, research-only** path for checking archived publisher revisions.
It does not promote a model, supply historical bookmaker prices, or allow
pre-fight alerts.

## Evidence contract

For one event and one decision cutoff, the fetcher saves two exact MediaWiki
Action API responses: the latest page revision at the cutoff, and the content
of that revision by ID. Both request URLs, actual fetch times, response bytes,
and SHA-256 digests are retained. The verifier checks the page ID, revision ID,
publisher timestamp, publisher SHA-1, wikitext hash, and query parameters.
The cutoff must be strictly later than the selected revision. The fetch time
remains the actual later download time; it is never backdated.

The [MediaWiki revisions API](https://www.mediawiki.org/wiki/API:Revisions)
documents `rvstart`, `rvdir`, `rvlimit`, revision IDs, timestamps, content, and
SHA-1 fields. A hash proves that *our saved response* has not changed; it is
not a publisher signature. Deleted or suppressed revisions can also limit
the archive's completeness. Keep the original API receipts with the reports.

## UFC 331 bounded example

UFC's [official weigh-in page](https://www.ufc.com/news/official-weigh-results-cryptocom-ufc-331-van-vs-pantoja-2)
said the Sep 19, 2026 early prelims began at 2:00 p.m. Pacific, or
`2026-09-19T21:00:00Z`. Its
[fight-week guide](https://www.ufc.com/cryptocom-ufc-331-fight-week-guide)
listed 2:30 p.m. Pacific. The earlier published time gives the conservative
24-hour cutoff `2026-09-18T21:00:00Z`. This disagreement needs an explicit
start-time review before model evaluation; the archived page alone is not
independent start-time evidence.

From the project root, fetch the two source revisions only when the API permits
it, with a descriptive user agent and no rapid retries after HTTP 429:

```bash
.venv/bin/python scripts/fetch_historical_revisions.py \
  --event-slug ufc331 --page-id 83648230 \
  --prefight-cutoff-utc 2026-09-18T21:00:00Z \
  --result-cutoff-utc 2026-09-26T23:00:00Z
```

Review each selection/content receipt pair offline against the research DB:

```bash
.venv/bin/python scripts/review_historical_revision.py \
  --kind scheduled \
  --selection-receipt data/raw/historical-revisions/ufc331/prefight-20260918T210000Z.selection.receipt.json \
  --content-receipt data/raw/historical-revisions/ufc331/prefight-20260918T210000Z.content.receipt.json \
  --output reports/ufc331-prefight-review.json
```

Use a new `--output` filename when repeating a review; the command will not
overwrite an earlier report.

Run the same review with `--kind completed` and the result receipt pair for
the post-event source. The report lists every source bout that matched an
accepted research row, every held bout, and which matched fighter pairs also
have intact title-to-page-ID lookup receipts. It deliberately reports
`model_eligible: false` until the full point-in-time history and chronological
evaluation gates are implemented and satisfied.

The first Sep 26 capture selected [pre-fight revision
1375565079](https://en.wikipedia.org/w/index.php?oldid=1375565079), published
Sep 18 at 15:47:38 UTC, and [completed revision
1376363684](https://en.wikipedia.org/w/index.php?oldid=1376363684), published
Sep 23 at 18:36:42 UTC. Each revision had 12 source bouts. The offline
comparison found 7 exact, linked pairs in the research database and held 5
because at least one fighter lacked a source wiki link. The two private
reports are `reports/ufc331-prefight-review.json` and
`reports/ufc331-result-review.json`; all raw receipts and reports are ignored
by Git. These counts are a source coverage check, not a predictive result.

The same receipt process was then run on four earlier completed cards. Their
24-hour cutoffs use the earliest segment times in UFC's official
[Noche](https://www.ufc.com/news/noche-ufc-silva-delgado-official-weigh-in-results),
[Paris](https://www.ufc.com/news/ufc-fight-night-paris-official-weigh-in-results-hooker-parnasse),
[Shanghai](https://www.ufc.com/news/official-weigh-results-ufc-shanghai-nurmagomedov-song-fight-night),
and [Sacramento](https://www.ufc.com/news/official-weigh-results-ufc-sacramento-fight-night-hernandez-rodrigues)
weigh-in articles. For each card, a separate completed revision was selected
at the *next* event's pre-fight cutoff. The linked fighter titles were checked
against the research import's saved, hash-verified MediaWiki title lookup
responses and page IDs; these lookups were fetched retrospectively and are
used for identity only.

| Event | Source bouts | Pre-fight linked pairs with verified IDs | Completed pairs at next cutoff |
| --- | ---: | ---: | ---: |
| Sacramento, Aug 22 | 13 | 4 | 4 at Shanghai's Aug 28 cutoff |
| Shanghai, Aug 29 | 13 | 4 | 4 at Paris's Sep 4 cutoff |
| Paris, Sep 5 | 14 | 3 | 4 at Noche's Sep 11 cutoff |
| Noche, Sep 12 | 13 | 7 | 7 at UFC 331's Sep 18 cutoff |
| UFC 331, Sep 19 | 12 | 7 | 7 at Sep 26 research cutoff |
| **Total** | **65** | **25** | **26** |

The 25 pre-fight bouts had matching stable IDs in their source-linked lookup
receipts. Paris's later result revision had one additional linked result, but
that fight was not linked in the pre-fight source and is still held as a model
target. Earlier completed revisions must also be selected at *every further*
historical model cutoff; the table only establishes availability by the next
event. These five cards do not meet the model's history or validation floors.

## Promotion work still required

1. Capture a dated, rights-checked official UTC start for each target event.
   Hold events with conflicting or missing starts until the earlier plausible
   start is resolved.
2. Extend the saved title-to-page-ID lookup check to each archived matchup.
   Hold unlinked, renamed, substituted, and ambiguous fighters.
3. For each historical model cutoff, select the *latest* archived scheduled
   card for its target and the latest completed revisions of all prior events.
   The existing research DB's current results cannot be silently substituted.
4. Admit an event to chronological evaluation only after exact source/DB
   status, pairing, winner, and start-time checks pass at that cutoff. Keep
   publisher revision evidence separate from the live roster gate.
5. Collect historical bookmaker quotes at a fixed pre-fight time before
   comparing priced returns; otherwise report model prediction metrics and
   paper trade live events without an ROI claim.

The current [model gate](../src/ufc_odds_model/alerts.py) remains closed for
real cards until its result-history and validation thresholds are met.
