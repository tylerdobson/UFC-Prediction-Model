# First capture of a future UFC card

Use this workflow to begin **prospective** evidence for a new event. It records
what the MediaWiki page and fighter-title lookup returned before the card,
with separate post-response UTC fetch receipts. It does not import a bettable
roster, approve fighter identities, or infer an executable bookmaker price.
The existing [UFC 332 pilot](ONE_EVENT_PILOT.md) demonstrates the later
card-plus-odds review and its unresolved holds.

## Review the event anchor

Manually check the current official UFC event page for the earliest advertised
card segment, time zone, and possible schedule conflict. Independently resolve
the English Wikipedia event article's stable page ID and exact REST page
title. Save a private JSON spec with this structure (the values below are a
**fictional fixture**, not a real event):

```json
{
  "schema_version": 1,
  "event": {
    "source_page_id": 123,
    "source_page_title": "UFC 9999",
    "event_date": "2099-01-02"
  },
  "source": {
    "api_url": "https://en.wikipedia.org/w/rest.php/v1/page/UFC_9999"
  },
  "official_start_review": {
    "source_url": "https://www.ufc.com/event/fixture",
    "local_timezone": "UTC",
    "published_segment_starts": [
      {
        "segment": "main_card",
        "local_time": "2099-01-02T18:00:00",
        "utc_time": "2099-01-02T18:00:00Z"
      }
    ],
    "event_start_utc_for_conservative_cutoff": "2099-01-02T18:00:00Z"
  },
  "reviewed_by": "Your reviewer ID",
  "reviewed_at_utc": "2099-01-01T01:30:00Z"
}
```

Use the actual local time zone and every published segment; the conservative
start must be the earliest one. `reviewed_at_utc` is the real review time, not
an event date or a backfilled claim. The official page URL is a manual
citation: this script does not download UFC pages or establish their future
availability. Keep a private review note if official listings disagree.

## Capture and inspect

From the repository root, after filling a **new** spec file with real values:

```bash
.venv/bin/python -m scripts.capture_prefight_card \
  --event-spec data/raw/event-specs/SELECTED-EVENT.json \
  --output-dir data/raw/prefight-SELECTED-EVENT-UNIQUE-UTC-TIME
```

The command checks the spec before network access, fetches the exact event
page and one batch of linked fighter titles, and refuses to write if identity,
date, start, time order, license, or response structure differs. Its new
directory retains the exact spec bytes, both raw API responses, their
post-response receipts, and a hash-bound manifest. A missing fighter page ID
stays on identity hold. Inspect the manifest and run the reviewed roster
workflow before importing an operating card.

For a later recheck of **that same event**, use its verified manifest as the
anchor and write another new directory. The earlier card, lookup, odds, and
reviewer decisions are not carried into the new capture:

```bash
.venv/bin/python -m scripts.capture_prefight_card \
  --seed-manifest data/raw/prefight-SELECTED-EVENT-UNIQUE-UTC-TIME/manifest.json \
  --output-dir data/raw/prefight-SELECTED-EVENT-NEXT-UNIQUE-UTC-TIME
```

The new response and receipt times must precede the official card start.
Use the [data source decision record](DATA_SOURCE_DECISION.md) for current
provider and rights assumptions. Wikipedia page text requires attribution,
the exact revision link, and the CC BY-SA 4.0 license link in distributed
outputs; linked page IDs are source identifiers, not proof that an opponent
is still on the card. An operator must review substitutions, cancellations,
and the current official schedule before any model decision.

## September 27, 2026 live path check

The first-capture route was exercised on UFC 332 using an explicitly labeled
Codex source check; **human approval is still pending**. The official
[event listing](https://www.ufc.com/event/ufc-332) and
[watch schedule](https://www.ufc.com/watch/schedule) indexed the earliest
segment at 4:00 p.m. EDT on October 3, or 20:00 UTC. Direct access to those
UFC pages was unavailable during this check, so this citation must be
rechecked by the operator before approval. The private spec is
`data/raw/event-specs/ufc332-codex-20260927T163956Z.json`; the private new
capture is under `data/raw/ufc332-first-capture-20260927T163956Z/`. The
source and fighter lookup responses were both received at
`2026-09-27T16:40:02Z`, and their separate receipt hashes verified. The
source remained revision `1376571633` with 13 card rows, eight linked-ID
rows, and five identity holds. This card capture made no odds call, prediction,
alert, or paper bet. The separate later odds capture and full-card review
template are recorded in the [one-event pilot](ONE_EVENT_PILOT.md).
