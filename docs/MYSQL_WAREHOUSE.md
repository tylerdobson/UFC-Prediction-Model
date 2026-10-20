# MySQL statistics warehouse

The warehouse publishes verified native canonical MySQL datasets into
versioned analytical tables and views in the project database. It supports SQL analysis and a portfolio
demonstration; it does not train models, change frozen research results, approve
a card, or create betting recommendations. MySQL availability does not improve
the model's measured accuracy.

The implementation requires MySQL 8.4 or later and the core PyMySQL
dependency. The publisher creates only project tables and
views in the configured database. Database/user provisioning belongs to the
local instance setup; it never changes the global server or creates users.

## Run

Install the project using the documented environment setup; MySQL support is a core dependency.

The local instance helper in `scripts/local_mysql.py` provisions a separate
instance on port 3307. The existing server on port 3306 remains independent.
Credentials are stored in the local protected pipeline configuration rather
than committed files. The publisher itself reads these environment variables:

| Variable | Meaning |
| --- | --- |
| `UFC_MYSQL_HOST` | Host; defaults to `127.0.0.1` |
| `UFC_MYSQL_PORT` | Port; defaults to `3306`, use `3307` for the isolated local instance |
| `UFC_MYSQL_USER` | Dedicated project user; required |
| `UFC_MYSQL_PASSWORD` | Password; required in the environment, never a CLI argument |
| `UFC_MYSQL_DATABASE` | Existing project database; required |

The user needs database-scoped table/view creation, reads and writes. The
publisher rejects system database names. It uses parameter binding for data,
fixed identifiers for warehouse objects, InnoDB, strict source validation and
`local_infile=False`. This local demonstration assumes a trusted local
connection; configure authenticated TLS before adapting the connection factory
to a remote database.

```bash
PYTHONPATH=src .venv/bin/python -m ufc_odds_model.mysql_warehouse publish \
  --statistics-db mysql:statistics
```

Optional flags are `--source-sha256` (the expected statistics dataset logical/source
SHA256, checked before connection) and `--run-id` (1–64 letters, digits, dots,
hyphens or underscores). The default version ID derives from the source SHA256.
Repeated publication with the same version ID and source is idempotent. Reusing
an ID for different source bytes fails. An existing verified version can be
reactivated without inserting its rows again. No command deletes old versions.

The Python API is:

```python
publish_statistics(
    statistics_db: Path,
    *, source_sha256: str | None = None,
    run_id: str | None = None,
    connector=None,
) -> dict
```

`connector` is an optional DB-API compatible connection factory; it receives the
same environment-derived PyMySQL keyword arguments. Success returns `status`
(`published` or `already_published`), `run_id`, `active_run_id`, `source_sha256`,
`schema_version`, `counts` and `research_only`. Exceptions and CLI diagnostics
use sanitized messages rather than database error text or connection options.

## Data model and publication

Every dimension, fact, crosswalk, source and quality row includes `run_id`.
Composite primary and foreign keys prevent rows from joining across versions.

| Tables | Grain |
| --- | --- |
| `ufc_wh_load` | One verified source publication and its full source metadata |
| `ufc_wh_active` | One active publication pointer |
| `ufc_wh_dim_fighter` | One canonical fighter per version; nullable retrospective reach |
| `ufc_wh_dim_event` | One canonical card per version |
| `ufc_wh_fact_bout` | One canonical bout, including held bouts |
| `ufc_wh_fact_fighter_bout` | One fighter in one validated paired bout |
| `ufc_wh_fact_fighter_round` | One fighter in one validated bout round |
| `ufc_wh_research_*_link` | Exact retained crosswalks to the separate research database |
| `ufc_wh_quality_gap`, `ufc_wh_research_hold` | Explicit archive or research coverage gaps |
| `ufc_wh_source_file` | Retained source filename, URL, byte count and SHA256 |

The 22 integer statistic columns retain significant/total/position strike
counts, takedowns, knockdowns, submission attempts, reversals and control
seconds. Missing fields remain SQL `NULL`; they do not become zero. Rounds
include the source CSV row number and error flags. Raw source receipts and
unmatched raw round rows remain in the retained native canonical MySQL dataset; the
warehouse contains the normalized facts and their provenance, not a second
copy of every raw source payload.

Before warehouse publication, the publisher verifies the canonical content hash,
expected file hash when supplied, source table/field contracts, identities,
foreign keys, event dates, paired coverage, complete round sequences, exact
round-to-bout totals, numeric ranges and text widths. If a raw round's control
exceeds its validated duration, the normalized control stays NULL and its
`source_errors_json` explicitly records
`warehouse:control_exceeds_validated_round_duration`. Capture metadata and
archive/source hashes accompany every publication. The dataset fingerprint is checked while materializing its read-only snapshot. Explicit legacy file inputs remain available for migration validation.

A database-scoped session lock serializes schema checks and publication across
processes. The publisher checks the schema version/hash plus actual column
types, nullability, primary/foreign keys and InnoDB engines. An incompatible
existing schema requires an explicit migration; it is not silently altered.
Table/view initialization occurs before the data transaction because MySQL DDL
has implicit commit behavior. The subsequent load, destination count checks,
published status and active-pointer update occur in **one InnoDB transaction**.
Failure before commit rolls back that new version; readers retain the previous
committed active snapshot. If the connection loses the commit acknowledgment,
the publisher reports an uncertain outcome: the server may have committed the
complete new version. Replay the same run ID to resolve this without duplicate
rows. A partially loaded version is never activated. Completed older versions
remain available for reproduction.

## Read the warehouse

For a compact Workbench screenshot, open
[portfolio_screenshot.sql](../sql/mysql/portfolio_screenshot.sql) and execute the
whole script. It returns nine archive/count checks, including two fighter facts
per usable bout, exclusion of held bouts from facts and accepted-plus-held
reconciliation. The [README screenshot](../README.md#screenshots) shows the
actual connection, normalized schema and the first count rows.

[analytics.sql](../sql/mysql/analytics.sql) contains read-only examples for:

- `ufc_active_publication`: source hashes, capture time and row counts.
- `ufc_active_fighters`, `ufc_active_events`, `ufc_active_bouts`: active dimensions/card facts.
- `ufc_card_coverage`: every canonical card and usable, held, timed and reach coverage.
- `ufc_fighter_history`: paired per-bout facts, opponents and correctly timed strike rates.
- `ufc_data_quality`, `ufc_round_quality`: archive/research gaps and per-round error flags.
- `ufc_source_provenance`: retained source URLs/hashes and transformation metadata.

Single queries resolve one committed active version. For a dashboard containing
multiple queries, use a read-only consistent-snapshot transaction or retain the
returned `run_id` and query versioned tables explicitly. The past-only example
applies the V6 two-date embargo and a decision-date cutoff. It does not apply
the model's shrinkage, recent-five window or explicit target-bout exclusion and
must not be presented as a replacement model feature builder.

Source reach is retrospectively captured and its original measurement dates
are unknown. Historical counts come from earlier per-fight facts. Current
career averages never enter the warehouse's historical statistics facts or
rate denominators. Archive-only results remain separate from frozen research
targets. Source licenses remain in the retained source manifests, identified
by the publication's manifest hash; the SQLite metadata does not reproduce
their full license text. Research timing limitations remain in publication
metadata. Database publication grants no additional data rights.

## Verification

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  -m unittest tests.test_mysql_warehouse -v
```

The default suite checks source reconciliation, NULL handling, provenance,
replay, changed inputs, rollback and secret-safe failures without requiring a
server. Set `UFC_MYSQL_INTEGRATION=1` with the project MySQL environment to also
run the actual MySQL test. It publishes a tiny synthetic fixture, checks views
and foreign keys, replays without duplicates, injects a mid-load failure and
verifies that the previous active data remains readable. It restores the
previous pointer and removes only its own UUID fixture versions. Run it in an
isolated test database without concurrent production publishers. CI exercises
this against MySQL 8.4.

Publication mechanics follow the primary documentation for
[MySQL InnoDB foreign keys](https://dev.mysql.com/doc/refman/8.4/en/create-table-foreign-keys.html),
[implicit commits](https://dev.mysql.com/doc/refman/8.4/en/implicit-commit.html),
[named locks](https://dev.mysql.com/doc/refman/8.4/en/locking-functions.html), and
[PyMySQL transactions](https://pymysql.readthedocs.io/en/latest/modules/connections.html).
