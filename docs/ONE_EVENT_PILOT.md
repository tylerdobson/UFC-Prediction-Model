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

The optional odds replay verifies the original response hash and original capture time (`2026-09-26T22:47:55Z`). It saves an exact-response receipt and passes only the selected, exact or accent-normalized pairings through the existing odds importer. Alias and spelling cases remain held. These week-ahead prices are observations, not current or executable quotes. Remove `--odds-manifest` to import only the card. The importer does not score, alert, or paper-trade.

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

## Review before any real event-day decision

1. Check the current official card, cancellations, substitutions, and start times again. The saved Wikipedia revision is a time-stamped community report; it is not a real-time official feed. Resolve all held identities and aliases with documented evidence. A later card change needs a new source snapshot and a **new** isolated intake, not an edit to an old receipt.
2. Re-run source integrity on the new database: `python -m ufc_odds_model.integrity --db data/raw/ufc332-pilot-NEW-UNIQUE-NAME/paper-intake.sqlite`. The report also records verification at import time.
3. Obtain a fresh, named-book, two-sided quote close to the intended decision time. The saved September 26 prices cannot support an October 3 alert. Keep all gate and paper-decision work in a separately reviewed operating workflow. After the event, capture a new results revision and independently review each settlement.

Wikipedia text is reusable under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.en) with attribution, a license link, and an indication of changes. The dashboard or any distributed output using this card should link to the exact page revision and license. This pilot does not reuse fighter images, UFC logos, or UFC page text. The official UFC links above were read to confirm the event schedule and naming; they are not an automated scraped feed.

Run the focused offline tests with `python -m unittest discover -s tests -p test_one_event_pilot.py -v`.
