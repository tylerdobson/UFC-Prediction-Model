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

The same receipt process was then run across the 15 consecutive event dates
from June 6 through September 19, 2026. The checked
[cutoff manifest](HISTORICAL_2026_PILOT_CARDS.json) records the earliest UFC
segment time, its official source URL, a 24-hour pre-fight decision cutoff,
and the following event's decision cutoff for each completed result. UFC 331's
completed result uses the September 26 research cutoff. UFC 330 and UFC 331
have conflicting official start times; the manifest records the conflict and
uses the earlier time. Freedom 250 began at midnight UTC on June 15, although
its local event date was June 14. These start times were manually reviewed
from the linked official pages; the offline cohort command verifies the
manifest's format and archived receipts, but does not recheck those pages.
The linked fighter titles were checked
against the research import's saved, hash-verified MediaWiki title lookup
responses and page IDs; these lookups were fetched retrospectively and are
used for identity only.

Recheck every saved selection and content receipt against the research DB
without making a network request:

```bash
.venv/bin/python -m scripts.review_historical_pilot \
  --output reports/historical-2026-fifteen-event-cohort.json
```

Choose a new output path when rerunning; the command does not overwrite an
existing report.

| Event date and card | Source bouts | Paired binary bouts | Pre-fight rows held |
| --- | ---: | ---: | ---: |
| Jun 6, Muhammad–Bonfim | 12 | 7 | 5 |
| Jun 14, Freedom 250 | 7 | 7 | 0 |
| Jun 20, Kape–Horiguchi | 12 | 3 | 9 |
| Jun 27, Baku | 13 | 4 | 9 |
| Jul 11, UFC 329 | 14 | 7 | 7 |
| Jul 18, Oklahoma City | 12 | 3 | 9 |
| Jul 25, Abu Dhabi | 13 | 4 | 9 |
| Aug 1, Belgrade | 14 | 5 | 9 |
| Aug 8, Gamrot–Salkilld | 12 | 2 | 10 |
| Aug 15, UFC 330 | 12 | 6 | 6 |
| Aug 22, Sacramento | 13 | 4 | 9 |
| Aug 29, Shanghai | 13 | 4 | 9 |
| Sep 5, Paris | 14 | 3 | 11 |
| Sep 12, Noche UFC | 13 | 7 | 6 |
| Sep 19, UFC 331 | 12 | 7 | 5 |
| **Total** | **186** | **73** | **113** |

Each of the 73 paired bouts has a stable fighter ID match in the archived
pre-fight card and a binary result in the following saved completed revision.
Paris's later result revision has one additional linked result, but that
fight was not linked in the pre-fight source and is still held as a model
target. Earlier completed revisions must also be selected at *every later*
historical model cutoff; the table only establishes availability by the next
event. These 15 cards do not meet the model's 100-bout history floor or the
50/30/30 chronological evaluation floor. There are no matched historical
bookmaker quotes in this cohort, so it cannot support a priced return claim.

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
