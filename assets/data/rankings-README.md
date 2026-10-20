# Official UFC rankings snapshot

`ufc-rankings.json` contains the divisions, title holders, ranked athletes, and positions published by the [official UFC rankings page](https://www.ufc.com/rankings), captured on **2026-10-20 at 02:43:27 UTC** using an ordinary public HTTPS request. It is a current descriptive snapshot, not historical model training data.

The page publishes two separate systems. This snapshot keeps both:

| System | Publisher update label | Published groups | Contender rows |
| --- | --- | ---: | ---: |
| UFC media-panel rankings | Last updated: Tuesday, Oct. 06 | 13: eleven weight divisions and two pound-for-pound lists | 195 |
| UFC Meta rankings | Last updated: Saturday, Oct. 10 | Eleven weight divisions | 165 |

The labels do not state a year. They are preserved as labels; `source_updated_date` stays `null` rather than inferring a full calendar date from the capture time. The fetched timestamp is independent evidence of when the response was observed. The page displays the Meta update footer by default, so `default_ranking_set` is `meta`. UFC also describes the transition in its [June 22, 2026 announcement](https://www.ufc.com/news/ufc-and-meta-unveil-meta-ufc-rankings).

All published groups currently contain fifteen contender rows. The media women's flyweight list has **Maycee Barber and Jasmine Jasudavicius tied at #6**, followed by #8. The absent number 7 is retained in `missing_ranks` with reason `published_tie_skips_rank`; no athlete or vacancy is invented. Other missing numbers can instead be `explicit_published_vacancy` or `rank_not_published_by_source`. A source-listed division without contenders carries an `empty_reason`. Divisions absent from the official page are not generated.

Champions and interim champions are separate labeled entries in `champions`, without invented numeric ranks. Pound-for-pound caption athletes are `leader` records, not division champions. The captured heavyweight title panel labels Ciryl Gane **Champion**, while the portrait alt text still describes him as interim champion. The explicit title-panel label is preserved, with a data-quality note recording that contradiction; no title history is inferred from the image text.

## Public Python API

```python
from ufc_odds_model.rankings import fetch_rankings, load_rankings, parse_rankings

# Verify the checked-in snapshot's content digest and source/rank invariants.
rankings = load_rankings()
meta = next(s for s in rankings["ranking_sets"] if s["id"] == "meta")

# A cutoff rejects snapshots captured later; it does not establish PIT training eligibility.
rankings = load_rankings("assets/data/ufc-rankings.json", as_of="2026-10-20")

# Fetch only the normal official HTTPS page and preserve an immutable raw receipt.
fresh = fetch_rankings(raw_directory="data/raw/rankings", timeout=30)

# Normalize already retained actual response bytes using their observed timestamp.
rankings = parse_rankings(raw_html, fetched_at_utc="2026-10-20T02:43:27Z")
```

Each ranking system has its own `divisions` array, `source_updated_label`, and `source_updated_date`. Each division contains `id`, `name`, `kind`, `championship_status`, `champions`, `leader`, `contenders`, `vacancies`, `missing_ranks`, `missing_rank_reasons`, `unpublished_rank_slots`, `published_contender_count`, `empty_reason`, and `data_quality_notes`. Contender fields include `rank`, `name`, `athlete_url`, `source_rank_label`, `source_tie_marker`, `tied`, `tie_size`, and `rank_change_label`.

`source.raw_sha256` hashes the exact captured response bytes, including their original line endings. `snapshot_sha256` hashes the canonical normalized JSON, excluding the digest field itself. The original response is **283,571 bytes**, with SHA-256 `59ca294b120be8ebd10e17634c4cb579238c8144d648536f0e931e163aae719e`. Its archive capture ID is `20261020T024327Z-59ca294b120be8eb`; raw HTML, response headers, and receipt belong under the ignored `data/raw/rankings/` tree, not in this public assets directory.

`fetch_rankings` returns verified JSON; it does not mutate a database or activate a dashboard version. Callers own their atomic publication/warehouse transaction. Failed HTTP requests, challenge pages, malformed structures, duplicate athletes/divisions, conflicting rank positions, and modified snapshots raise errors. The fetcher does not bypass browser or security challenges. The pure JSON document can be stored by a MySQL integration without introducing another SQLite database.

Run focused tests with `python -B -m unittest discover -s tests -p test_rankings.py -v`. Tests cover separate systems, ties, champion/interim/vacancy cases, short and empty lists, source labels, digest tampering, future-capture rejection, and preservation after a failed refresh.
