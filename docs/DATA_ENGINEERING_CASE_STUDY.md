# UFC Forecast: a reproducible statistics pipeline

This project turns retained fight-statistics CSV captures into a queryable, versioned relational dataset and source-backed analytical reports. The engineering work centers on provenance, identity, grain, incomplete observations and safe publication. A research winner model consumes the resulting earlier-fight statistics; it is one consumer of the pipeline.

The repository includes a small offline demonstration so a reviewer can run the full transformation without obtaining private local databases or calling a source website. Its invented data is separate from the retained real archive and from the published research forecasts.

## Run the offline demonstration

Install [uv](https://docs.astral.sh/uv/) and Python 3.11 or later, then run from the repository root:

```sh
./scripts/demo.sh
```

The shell entrypoint invokes Python through `uv run --offline --no-project --no-python-downloads --no-env-file`. It uses an already installed interpreter and the standard library; it does not download an interpreter, resolve dependencies, load `.env` or create a project virtual environment. The default run makes no network calls. It works from a clean checkout, including a path containing spaces.

Open `output/pipeline-demo/index.html` directly in a browser. The output directory is ignored by Git. To create another preserved edition:

```sh
./scripts/demo.sh --output-dir output/pipeline-demo-second-edition
```

The first run builds a staged edition and publishes the directory after validation. An identical replay verifies the stored artifact hashes and SQLite content before returning `reused`; it leaves artifact bytes and modification times unchanged. Changed input bytes, transformation code or saved outputs cause a clear failure. Choose a new directory rather than overwrite an earlier edition.

### What the demonstration contains

All fighters, events, outcomes and statistic counts in `demo/fixtures` are **synthetic**. The five cards, six bouts, four fighter profiles and fifteen source round rows are intentionally small enough to inspect. Placeholder identifiers and URLs meet the real parser's shape requirements; no source request occurred, and the sample analytical report removes placeholder source links. Fixed fixture dates are logical demonstration timestamps, not capture claims about real fights.

| Deliberate condition | Demonstrated behavior |
|---|---|
| One profile has no reach | Reach stays SQL `NULL`; the fighter and other observations remain usable. |
| One fighter-bout has missing control time | Control stays `NULL`; observed strike counts remain available. |
| One paired bout has an unknown schedule | Counts and count-based accuracy remain available; per-minute rates with unknown exposure are unavailable. |
| One bout is missing an opponent's final round | The bout is held, its three raw round rows remain inspectable, and no paired fighter-bout facts are published for it. |
| The showcase card has its own observed rows | The historical comparison uses only earlier dates and excludes the target card's statistics. |

The expected outcome is five usable paired-stat bouts and one held bout. Quality checks assert these separate conditions; they do not treat all missingness as either zero or a fatal error. The generated reports prominently say **SYNTHETIC ENGINEERING DEMO**, and no winner model is fitted.

### Artifact contract

| Path below `output/pipeline-demo/` | Purpose |
|---|---|
| `raw/manifest.json` and four CSV files | Retained input bytes, declared source identity, SHA-256 and byte counts. |
| `normalized/statistics.sqlite` | The production normalizer's dimensions, paired facts, raw row receipts and quality records. |
| `reports/quality.json` | Assertions and observed counts for explicit fixture conditions. |
| `reports/coverage.json`, `reports/coverage.csv` | Per-card normalized coverage, including held bouts. |
| `reports/card-statistics.html`, `.json`, `.csv` | Earlier-fight comparison of the synthetic showcase roster. |
| `index.html` | Standalone overview and links to the evidence. |
| `run-manifest.json` | Protocol, input/code fingerprints, fixed logical timestamp policy and output hashes. |

Code fingerprints cover the demo orchestrator, normalizer and card-report implementation. The run identifier derives from the verified input/code fingerprints. The manifest records actual output hashes. Byte identity across different Python or SQLite versions is not promised; local replay verifies the saved edition rather than rebuilding it silently.

## Architecture

[![Statistics pipeline architecture](diagrams/statistics-pipeline.png)](diagrams/statistics-pipeline.svg)

The [editable Mermaid source](diagrams/statistics-pipeline.mmd) and
[rendering instructions](diagrams/README.md) describe the scheduled quality gate,
separate report/model invocations and publication order. MySQL activation is
transactional; the subsequent local ready-pointer update is a separate step.

The offline entrypoint uses the existing `historical_stats.build_history`, `HistoryStore`, coverage exporter and `card_statistics` report builder. It demonstrates the same parser contracts rather than substituting a second toy ETL implementation. Network refresh, MySQL and scheduling are separate explicit actions.

## Data model and quality decisions

The canonical store distinguishes an event, a bout, a fighter in a bout and a fighter in a round. A bout can exist in the event dimension while its paired statistics are held. Source rows remain available to explain why normalization withheld a fact.

Stable canonical identifiers anchor joins. Links to the separate research history require exact, unique identity evidence and consistent date/outcome relationships; ambiguous names do not receive guessed identities. Unknown reach and granular counts remain nullable. Zero means an observed zero. Count-based metrics use records containing their required counts; time-based metrics also require valid elapsed time. This prevents unknown duration from entering a per-minute denominator as zero or as a guessed schedule.

Historical card comparisons exclude all observations on or after the decision date and the target card date. Research-to-archive date drift also requires a two-date embargo so a one-day difference cannot expose the target event's own statistics. Historical reports use earlier per-fight observations. Current athlete career averages are descriptive context in the separate forecast report, not historical inputs. Retrospectively captured reach remains an approximate static measurement with unknown original measurement date.

The demo's showcase provides a small chronology check. The real pipeline additionally preserves explicit identity holds and source discrepancies. Quality reports make coverage a visible property of each card, rather than reporting only successfully normalized rows.

## Real retained archive versus demo fixtures

The retained local archive snapshot covers March 1994 through October 17, 2026. It contains **793 cards, 8,950 distinct bouts and 4,629 fighter profiles**. Of those bouts, **8,818** have usable paired statistics and **132** are held for identity, event or round-data gaps. Reach is available for **2,490** profiles. These are snapshot counts, not a claim of complete UFC history or continuing live freshness.

Those real captures and local databases are excluded from the clean-clone demonstration. The demo's five invented cards do not reproduce those counts or the winner model's accuracy. Original research target labels remain frozen through September 19, 2026; later archive observations do not silently extend those target labels. See [all-card statistics](ALL_CARD_STATISTICS.md) for real archive refresh, backfill, card-report and model commands, and the [README model results](../README.md#model-results) for the evaluated model and its latest-year weakness.

## Optional MySQL publication

The default demo finishes with SQLite and local reports. To publish its verified synthetic store to a configured project database, first install the optional dependency in the project environment:

```sh
uv sync --frozen --extra warehouse
```

On macOS with MySQL 8.4 server binaries already installed, the project helper can provision its own loopback instance on port 3307:

```sh
.venv/bin/python scripts/local_mysql.py setup
.venv/bin/python scripts/local_mysql.py status
./scripts/demo.sh --mysql --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json"
```

The helper uses a separate data directory and project database/user; it leaves an existing port-3306 instance independent. Credentials live in an owner-only configuration file outside the repository. The demo accepts the helper's wrapped `environment` object or a flat map of allowed `UFC_MYSQL_*` strings. It reads that file only with explicit `--mysql`, restores temporary environment overrides afterwards and exports no credentials. A failed optional warehouse publication leaves the verified local edition available for retry.

Publishing the demo selects its synthetic snapshot as the active warehouse version. A normal archive refresh selects the real archive again; both completed versions remain retained. Use separate databases if both need independent active views. The [local MySQL guide](LOCAL_MYSQL.md) includes service management and Workbench connection details.

The warehouse stores version-scoped dimensions, bout/round facts, exact research crosswalks, quality gaps and provenance. Before connecting, the publisher validates the source SQLite contracts, content and file hashes, keys, dates, round completeness and count reconciliations. MySQL schema initialization occurs separately because DDL has implicit commit behavior. Loading a new version, validating destination counts and switching the active pointer happen in one InnoDB transaction under a publication lock. A mid-load error rolls back the new version; the previous committed active version remains readable. Replaying the same source/run identifier does not insert duplicate rows. Old complete versions remain available.

See [MySQL warehouse](MYSQL_WAREHOUSE.md) for exact semantics, source-rights limitations and verification. [Analytical SQL](../sql/mysql/analytics.sql) includes active card coverage, per-fighter history, missingness, round-quality and source-provenance queries. Its past-only SQL example is an analytical query, not a replacement for the model's feature builder.

## Refresh and operations

The optional refresh runner captures or replays an archive, builds a staged local version, validates it and can publish to MySQL before promoting the ready pointer. It keeps structured run logs and versioned outputs, supports retries and preserves the previous ready version on failure. A macOS LaunchAgent can run a daily refresh; local failure notifications and an explicitly configured webhook provide failure signals. Installing a schedule is a separate opt-in step.

For example, after configuring the optional warehouse:

```sh
PYTHONPATH=src .venv/bin/python -m ufc_odds_model.pipeline_ops refresh \
  --mysql --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json"
```

Use a retained `--archive PATH/manifest.json` for source replay, or follow the [operations runbook](PIPELINE_OPERATIONS.md) for capture, schedule installation, logs, retry limits and recovery. The portable demo does not install a service, send an alert or prove continuous uptime. A local process, schedule and successful test run do not establish production service availability.

## Verification and boundaries

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  -m unittest tests.test_portfolio_demo -v
```

The focused demo tests execute the real parser and reports, assert distinct missingness and held-row behavior, test chronology, block socket creation during the default run, reject changed fixture/output bytes, verify exact replay preservation and run the shell command in a clean path containing spaces without a virtual environment. The optional publisher test checks protected config handling, environment restoration and secret-safe returned fields. The [warehouse suite](MYSQL_WAREHOUSE.md#verification) separately covers transactional publication and includes an opt-in actual MySQL integration test; the [operations suite](PIPELINE_OPERATIONS.md) covers refresh failure behavior.

This is a single-machine, bounded batch pipeline. SQLite is a useful portable analytical handoff, while MySQL demonstrates a separate consumer store and safe version publication. It is not a distributed processing system, a live betting feed or a claim that adding a database improves prediction accuracy. Source licensing, archival capture dates, unresolved identities and retrospectively measured reach remain visible constraints.
