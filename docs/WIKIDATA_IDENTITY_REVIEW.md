# Wikidata identity candidate worksheet

This is a **read-only research aid** for the held Wikipedia results. It compares printed fighter names in the existing identity review with [Wikidata](https://www.wikidata.org/wiki/Wikidata:Reuse) items carrying the [UFC athlete ID property (P9722)](https://www.wikidata.org/wiki/Property:P9722) and no English Wikipedia sitelink. Wikidata structured data is CC0. A QID and matching name are candidate evidence only; the command writes no SQLite rows or importer crosswalk.

## Build and replay

From the repository root, with the project environment installed:

```bash
.venv/bin/python -m ufc_odds_model.wikidata_identity_candidates \
  --review reports/wikipedia_1993_2026_identity_review.json \
  --raw-dir data/raw/wikidata \
  --output reports/wikidata_identity_candidates.json
```

The command makes one bounded [Wikidata Query Service](https://query.wikidata.org/) request with an identifying User-Agent. It saves the **exact response bytes** under their SHA-256 filename in ignored `data/raw/wikidata/`, then records the query, response path and hash, input-review hash, duplicate-ID/name flags, and all matching bout contexts in an ignored JSON worksheet. To rebuild the worksheet offline from a retained response, add `--response-file data/raw/wikidata/<sha256>.json`. Offline replay records its origin and a new worksheet creation time; it does not claim a new live observation.

## Measured snapshot

The September 26, 2026 live response is `data/raw/wikidata/d491bdc9c8b3763bbad109d7b0ed4cdcd600e7515d8dc64b70385fcec2425047.json` (SHA-256 `d491bdc9c8b3763bbad109d7b0ed4cdcd600e7515d8dc64b70385fcec2425047`). It returned **144 bindings / 143 distinct QIDs**. Exact Unicode NFC plus casefold matching against the local review found **97 fighter-name keys, 274 name occurrences, and 268 distinct event-plus-bout positions**. Accents and punctuation are preserved; similar spellings are not merged. For example, the highest-count lead is [Da'Mon Blackshear (Q136338224)](https://www.wikidata.org/wiki/Q136338224), appearing ten times in the review.

The response contains one QID with two UFC athlete IDs (`Q118137956`: `chris-duncan` and `lyudovit-klayn-4`) and 15 items whose English-label fallback is just their QID. The input review also has two differently capitalized entries for MarQuel Mederos. These appear in the worksheet's anomaly sections; the duplicate QID must not be taken as a valid identity. The input review reported **1,677 held bouts** before either reviewed replay. One Denise Gomes decision and a later [29-decision Wikidata crosswalk](REVIEWED_IDENTITY_CROSSWALK_WIKIDATA_2022_2026.json) brought the current database to **1,647 held bouts**. The worksheet's 268 positions are candidates within that older review snapshot, not a count of currently recoverable bouts.

## Review before any import

For each candidate occurrence, verify the Wikidata item and its revision, UFC athlete slug, and the exact event/opponent/result against independent evidence. Confirm that the retained Wikipedia event revision and payload hash still identify the same printed name in the same bout. Only then create a separate, bout-scoped schema-version-2 decision as described in [Wikipedia history](WIKIPEDIA_HISTORY_SOURCE.md). A shared name, current profile, or QID alone never approves a historical bout. This worksheet does not unlock operating alerts or paper betting.

Run the offline checks with:

```bash
.venv/bin/python -m unittest discover -s tests -p test_wikidata_identity_candidates.py -v
```
