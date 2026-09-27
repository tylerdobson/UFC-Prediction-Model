# One-event pre-fight intake pilot

This is the **UFC 332, October 3, 2026** paper-only intake path. It uses the saved [Wikipedia revision](https://en.wikipedia.org/w/index.php?title=UFC_332&oldid=1376571633) and a separately saved The Odds API response. It makes no network requests, reads no API key, sends no alerts, places no wagers, and writes only to a **new** isolated directory. Its event and bout provider statuses are `review_pending`, so the ordinary pre-fight roster gate rejects them. An imported card on the dashboard is evidence of source coverage, not an approved betting decision.

The one-card source and fighter-title lookup are under ignored `data/raw/ufc332-intake/` on the operator workstation. Its local `manifest.json` records the exact source SHA-256, page ID, revision, CC BY-SA 4.0 license, 13 fight-card rows, fighter page IDs, five held rows, a separately announced bout excluded from the card, the official start-time citation, and a comparison with the saved odds intake. The raw files are local evidence and are never committed, so a fresh clone needs the saved intake files or a verified private evidence recovery before these commands can run. The script verifies those bytes, identities, and start-time arithmetic again before creating SQLite.

## Run the provisional display intake

From the repository root, with the project installed or `PYTHONPATH=src` set:

```bash
python scripts/import_reviewed_prefight_card.py import \
  --manifest data/raw/ufc332-intake/manifest.json \
  --csv data/raw/ufc332-intake/reviewed-card-template.csv \
  --odds-manifest data/raw/ufc332-odds-intake/intake_manifest.json \
  --output-dir data/raw/ufc332-pilot-NEW-UNIQUE-NAME \
  --provisional
```

The UFC 332 template is already present on the current operator workstation. Inspect it before import. To recreate a template for another saved manifest, use `python scripts/import_reviewed_prefight_card.py template --manifest PATH --output NEW.csv`; the command refuses to replace an existing CSV. `--provisional` explicitly allows the template's blank `reviewed_by` cells. Without it, every selected row needs a real human reviewer. In either mode, the resulting card stays `review_pending`; this command does not authorize model decisions. The output directory must not exist. It contains `paper-intake.sqlite`, content-addressed raw receipts, and `paper-intake-report.json` with complete card/hold counts and integrity verification.

The script accepts only fight-card rows whose **two Wikipedia fighter page IDs** resolved in the saved Action API response. It checks every CSV position, name, stable ID, weight class, source revision, observation time, license, and start time against the saved source. A held or altered row fails before the database is created. An operator can delete eligible rows from the CSV after review; the report then counts them as eligible but unselected. The script never invents IDs for unlinked names.

The optional odds replay verifies the original response hash and capture time. New captures also verify a separate fetch receipt. It saves exact response bytes and passes only selected, exact or accent-normalized pairings through the existing odds importer. Alias and spelling cases remain held. These week-ahead prices are observations, not current or executable quotes. Remove `--odds-manifest` to import only the card. The importer does not score, alert, or paper-trade.

## Current review and timing state

| Item | UFC 332 intake |
| --- | --- |
| Wikipedia fight-card rows | 13 |
| Both fighter page IDs resolved | 8; person review still pending |
| Held for an unlinked fighter | 5 |
| Separately announced, excluded | Jacobe Smith vs. Bruce Whitehead |
| Saved odds pairings | 10 of 13: 5 exact, 2 accent-only, 3 need name/alias review |
| No saved odds event | 3 bouts |
| Conservative card start | **2026-10-03 20:00 UTC** (early prelims) |

The [official UFC event listing](https://www.ufc.com/event/ufc-332) and [watch schedule](https://www.ufc.com/watch/schedule) advertise early prelims at **4:00 PM EDT** on October 3, prelims at 6:00 PM EDT, and the main card at 8:00 PM EDT. The manifest stores `America/New_York` local timestamps and verifies their UTC conversions; the [official watch listing](https://www.ufc.com/watch) also shows the main card at midnight UTC on October 4. These are **card-segment starts**, not individual bout starts. Eight lower-card odds events use the midnight main-card time as their `commence_time`. For a conservative pre-fight cutoff, use the earliest advertised card start and recheck it near event day.

The manual name holds are material. Wikipedia spells **Bernardo Sopaj**, while the [official UFC athlete page](https://www.ufc.com/athlete/benardo-sopaj) and saved odds say **Benardo Sopaj**. The odds response also says **Michael Parkin** where the card says Mick Parkin, and **Alex Hernandez** where it says Alexander Hernandez. The script does not silently map these names. The saved odds response lacks bouts for Roberto Soldić vs. Khaos Williams, Ateba Gautier vs. Roman Kopylov, and Anthony Wint vs. Lucas Armand.

## Fresh timestamped card capture

The first September 26 lookup response was hash verified but did not have a retained fetch timestamp. The new capture command records separate post-response UTC receipts for both the MediaWiki page and its fighter-title lookup. It verifies the existing manifest for event identity and official start, then fetches a fresh page and linked identities into a **new** directory. It copies no old odds comparison or reviewer decision:

```bash
.venv/bin/python -m scripts.capture_prefight_card \
  --seed-manifest data/raw/ufc332-intake/manifest.json \
  --output-dir data/raw/ufc332-intake-NEW-UNIQUE-NAME
```

The September 27 local capture at `2026-09-27T15:56:16Z` returned the same source revision and 13 matchups as the earlier snapshot. Eight rows have both linked page IDs; five remain on identity hold. Both response hashes and fetched-at receipts verify. Its unreviewed manifest is under ignored `data/raw/ufc332-intake-20260927T1556Z/`. A template and separate provisional database were created from it with eight unreviewed bouts, six verified receipts, **no imported quotes**, and zero alerts or paper decisions. The [official UFC event listing](https://www.ufc.com/event/ufc-332) still shows the same 13 matchups as of this check. This capture improves the target identity timing evidence but does not approve the roster or convert the older September 26 odds into a usable September 27 market.

To create a candidate template and provisional import for another fresh capture, use the verified new manifest, a fresh CSV path, and a fresh output directory. Leave `--odds-manifest` out until a newly captured odds snapshot has been matched to that exact card and its capture follows both card and lookup receipts.

## September 27 card plus live odds scenario

After the timestamped card lookup, one server-side The Odds API call returned a new response at `2026-09-27T16:01:50Z`. The command saved its exact bytes, a separate key-free fetch receipt, an odds intake manifest, and a new card manifest with the comparison bound to that odds manifest. It did not modify the earlier capture:

```bash
.venv/bin/python -m scripts.capture_prefight_odds \
  --card-manifest data/raw/ufc332-intake-20260927T1556Z/manifest.json \
  --output-dir data/raw/ufc332-odds-NEW-UNIQUE-NAME
```

The ignored `data/raw/ufc332-odds-20260927T1602Z/` snapshot has 29 MMA events, of which 10 were future events on the card's local date. Of the 13 saved UFC card rows, four matched by exact fighter pair and had a usable named-book, two-sided market; three have alias or opponent review holds, three lack an event in this response, and three have unresolved source identity. The eight linked-ID rows remain **unreviewed**. The isolated provisional import at `data/raw/ufc332-pilot-20260927T1602Z/` retained 40 quote rows and 10 verified receipts, with zero alerts, predictions, paper decisions, or bets. Its verified portable evidence bundle is `backups/ufc332-timestamped-card-odds-20260927T1602Z.zip` (SHA-256 `a312ac1bbe7fdacc287f7f536a571fbde85ca3269f7793c4743391be575482d2`). These ignored local artifacts and the API key are not in Git.

The five card rows held for identity contain seven missing Wikipedia page IDs. The retained 19-title lookup has none of them; the saved odds names cannot establish fighter identity. The required person and matchup review is:

| Card row | Matchup | Evidence still needed |
| --- | --- | --- |
| 6 | Imanol Rodríguez vs. Alden Coria | Stable IDs for both; confirm current matchup. |
| 7 | Damian Pinas vs. Andrey Pulyaev | Stable IDs for both; confirm current matchup. |
| 8 | Marcus McGhee vs. Bernardo Sopaj | Stable ID for Bernardo; reconcile the official/odds spelling “Benardo” with a source-backed alias and the exact opponent. |
| 9 | Anthony Wint vs. Lucas Armand | Stable ID for Lucas; confirm the matchup and capture an odds event because none is saved. |
| 13 | Court McGee vs. Eric Nolan | Stable ID for Eric; confirm current matchup. |

The eight selected rows also have blank `reviewed_by` cells. No held row can be added from the retained lookup alone, and no selected row is approved for an alert.

Rebuild the same paper-only scenario from the retained raw evidence with a **new** output directory:

```bash
.venv/bin/python -m scripts.import_reviewed_prefight_card template \
  --manifest data/raw/ufc332-odds-20260927T1602Z/card-with-odds-manifest.json \
  --output NEW-UNREVIEWED.csv
.venv/bin/python -m scripts.import_reviewed_prefight_card import \
  --manifest data/raw/ufc332-odds-20260927T1602Z/card-with-odds-manifest.json \
  --csv NEW-UNREVIEWED.csv \
  --odds-manifest data/raw/ufc332-odds-20260927T1602Z/odds-intake-manifest.json \
  --output-dir data/raw/ufc332-pilot-NEW-UNIQUE-NAME --provisional
```

Both research replays were rebuilt at the **exact** odds cutoff `2026-09-27T16:01:50Z`. The strict replay verified 23 of 23 prior result revision proofs (287 result rows, 118 with stable IDs) and gave exploratory Elo forecasts for eight selected bouts, four with saved two-sided books. The full-history replay checked 790 completed events and 7,258 accepted result bouts before applying its existing holdout-tested research model to the same card. Both new reports are research-only, fail closed, and have no promotion or alert eligibility. The target fighter lookup timing is now proven at this cutoff, while linked fighter identities still need human review; retrospective historical identity availability, dated historical prices, and event-day executable quotes remain unproven. The local reports are `reports/ufc332-forward-research-20260927T1601Z.json` and `reports/ufc332-forward-research-full-20260927T1601Z.json`.

```bash
.venv/bin/python -m scripts.fetch_historical_matrix \
  --forward-cutoff-utc 2026-09-27T16:01:50Z
.venv/bin/python -m ufc_odds_model.archived_forward_replay \
  --card-manifest data/raw/ufc332-odds-20260927T1602Z/card-with-odds-manifest.json \
  --card-csv data/raw/ufc332-odds-20260927T1602Z/reviewed-card-template.csv \
  --odds-manifest data/raw/ufc332-odds-20260927T1602Z/odds-intake-manifest.json \
  --expected-capture-at-utc 2026-09-27T16:01:50Z \
  --output reports/NEW-STRICT-REPORT.json
.venv/bin/python -m ufc_odds_model.captured_forward_history \
  --card-manifest data/raw/ufc332-odds-20260927T1602Z/card-with-odds-manifest.json \
  --selected-csv data/raw/ufc332-odds-20260927T1602Z/reviewed-card-template.csv \
  --odds-manifest data/raw/ufc332-odds-20260927T1602Z/odds-intake-manifest.json \
  --output reports/NEW-FULL-REPORT.json
```

## Separate reviewed operating handoff

The later September 27 first capture is anchored by a Codex source-check event spec at `data/raw/ufc332-first-capture-20260927T163956Z/`; human source approval is still pending. Its exact spec, card response, fighter lookup, and fetch receipts are hash-bound. The separate `data/raw/ufc332-odds-first-capture-20260927T1643Z/` snapshot was fetched at `2026-09-27T16:43:11Z`: 29 MMA events, 10 eligible on the card's local date, four exact pairings, three alias/opponent holds, three identity holds, and three without an odds event. These are fresh **review-pending evidence**, not event-day executable quotes. The generated `data/raw/ufc332-operating-review-template-20260927T1643Z.json` leaves all 13 card rows pending.

For another verified capture, create an explicit full-card review artifact, then have a human review every row, current substitutions, stable IDs, and the documented rights basis before attempting a separate import:

```bash
.venv/bin/python scripts/import_reviewed_prefight_card.py review-template \
  --manifest data/raw/ufc332-odds-first-capture-20260927T1643Z/card-with-odds-manifest.json \
  --odds-manifest data/raw/ufc332-odds-first-capture-20260927T1643Z/odds-intake-manifest.json \
  --output data/raw/NEW-operating-review.json
.venv/bin/python scripts/import_reviewed_prefight_card.py import-approved \
  --manifest data/raw/ufc332-odds-first-capture-20260927T1643Z/card-with-odds-manifest.json \
  --odds-manifest data/raw/ufc332-odds-first-capture-20260927T1643Z/odds-intake-manifest.json \
  --review data/raw/NEW-operating-review.json \
  --output-dir data/raw/NEW-operating-card
```

`import-approved` creates a **new** database and immutable approved card snapshot; it never upgrades the provisional pilot. Every captured row needs an explicit `approve`, `hold`, `cancelled`, or `substituted` disposition. Any unresolved card row or saved odds alias/opponent hold keeps the **whole event** `review_pending`, including approved bouts imported for display. A matched fight with no usable two-sided price is a quote gap; the later fresh quote gate handles it. The import requires timestamped capture receipts and a card observation no more than 24 hours old. A human-entered HTTPS rights basis is an auditable attestation, not independent proof of permission. Even a fully reconciled card has no operating point-in-time history here, so the model gate must still reject real paper decisions. Refresh the card and quotes near the actual decision time.

## Earlier September 26 forward research replays

The saved card was captured at `2026-09-26T22:47:45Z`, and its odds response at
`2026-09-26T22:47:55Z`. A result revision selected for 23:00 UTC cannot be
silently used for a 22:47 decision. First save a separate latest-at-cutoff
result proof for **every** event in the 23-card historical cohort:

```bash
.venv/bin/python -m scripts.fetch_historical_matrix \
  --forward-cutoff-utc 2026-09-26T22:47:55Z
.venv/bin/python -m ufc_odds_model.archived_forward_replay \
  --output reports/ufc332-forward-research-20260926-sealed-v2.json
```

The local strict-revision report checks all 23 exact-cutoff result proofs,
covering 287 source result rows and 118 stable-ID matched results. It gives an
exploratory Elo probability for the eight selected, source-linked UFC 332
bouts. Only four of those eight have an unambiguous saved two-sided named-book
observation. Fighter histories in this April–September cohort have only zero
to two prior bouts per target fighter; logistic remains unavailable in this
strict replay. The proofs were fetched retrospectively, with their actual
download times retained. Its checked-input digest is
`237edbcca1751b5f7b68ca844687ce099babc0440f79b2d580c95cc69f895b98`.

A separate full-history **forward** scenario uses the 790 completed events
and 7,258 accepted results in the research database. It checks all 2,837
retained ingestion receipts, their fetch times and publisher revision times
against the saved odds cutoff, exact event receipt IDs and result counts, and
the unchanged database hash. All retained source captures predate the cutoff;
the latest was `2026-09-26T22:30:36Z`. It reproduces the saved chronological
holdout's Elo/logistic weights and metrics before applying result-only
features to the eight source-linked bouts:

```bash
.venv/bin/python -m ufc_odds_model.captured_forward_history \
  --output reports/ufc332-captured-forward-research-20260926-sealed-v2.json
```

The full-history holdout tested 800 accepted bouts: Elo Brier 0.2494 and
calibrated logistic Brier 0.2462. These modest, retrospective results do not
establish a betting edge. Another 1,647 source bout rows remain held for
unresolved identities, and historical roster observation times were not
reconstructed for that holdout. The full-history scenario's checked-input
digest is `d6ee89959dbc24bc99caa197312c02a3e31c9fcb6102e2b514a1e6e938bf5466`.

Both reports are ignored by Git and remain on the operator workstation. Both
set `research_only: true`, `promotion_eligible: false`, and
`alert_eligible: false`. Each report seals its forecast rows with
`forecast_rows_sha256`. The target fighter-title lookup is hash verified but
has no retained fetch timestamp, so its availability at the saved odds cutoff
is unproven. Saved week-ahead quotes are observations, not current
offers. No stakes, paper decisions, alerts, or ROI are inferred.

## Review before any real event-day decision

1. Check the current official card, cancellations, substitutions, and start times again. The saved Wikipedia revision is a time-stamped community report; it is not a real-time official feed. Resolve all held identities and aliases with documented evidence. A later card change needs a new source snapshot and a **new** isolated intake, not an edit to an old receipt.
2. Re-run source integrity on the new database: `python -m ufc_odds_model.integrity --db data/raw/ufc332-pilot-NEW-UNIQUE-NAME/paper-intake.sqlite`. The report also records verification at import time.
3. Obtain a fresh, named-book, two-sided quote close to the intended decision time. The saved September 26 prices cannot support an October 3 alert. Keep all gate and paper-decision work in a separately reviewed operating workflow. After the event, capture a new results revision and independently review each settlement.

Wikipedia text is reusable under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.en) with attribution, a license link, and an indication of changes. The dashboard or any distributed output using this card should link to the exact page revision and license. This pilot does not reuse fighter images, UFC logos, or UFC page text. The official UFC links above were read to confirm the event schedule and naming; they are not an automated scraped feed.

Run the focused offline tests with `python -m unittest discover -s tests -p test_one_event_pilot.py -v`.
