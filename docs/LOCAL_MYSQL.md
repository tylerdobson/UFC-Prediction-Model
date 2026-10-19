# Project MySQL on macOS

This optional setup uses an existing MySQL installation. It runs a separate,
project-owned instance bound to **127.0.0.1:3307**, with database `ufc_warehouse`
and application user `ufc_pipeline`. An existing system instance on port 3306
keeps its own configuration and data.

## Set up and run

From the repository root, with MySQL server binaries installed:

```sh
uv sync --frozen --extra warehouse
.venv/bin/python scripts/local_mysql.py setup
.venv/bin/python scripts/local_mysql.py install-service
.venv/bin/python scripts/local_mysql.py status
./scripts/demo.sh --mysql \
  --config "$HOME/Library/Application Support/UFCForecast/pipeline-env.json"
```

`setup` initializes a new data directory only. It refuses to replace existing
data or credential files, and an identical repeat reuses the instance. The
initial passwordless bootstrap has networking disabled and uses a private Unix
socket. Random application and administrative passwords are then assigned before
TCP connections are enabled. The application account has privileges only on
`ufc_warehouse`, and does not receive global administrative privileges.

The setup writes these **local, untracked** files:

| Location under `~/Library/Application Support/UFCForecast/` | Purpose |
|---|---|
| `pipeline-env.json` | Owner-only application connection settings; read at runtime |
| `mysql/admin.json` | Owner-only recovery credentials for this instance |
| `mysql/data/` | MySQL data directory |
| `mysql/mysql.log` | Server diagnostic log |

Keep the two credential files private. They are never embedded in the launch
agent or included in generated portfolio artifacts.

The launch agent `com.ufcforecast.mysql` starts at user login and restarts after
an unsuccessful exit. This is a local development service: the Mac must be
running and the user logged in. Sleep and network outages can delay scheduled
work. Do not describe this as a continuously available cloud deployment.

## Inspect in MySQL Workbench

Create a Standard TCP/IP connection to host `127.0.0.1`, port `3307`, user
`ufc_pipeline`, default schema `ufc_warehouse`. Read the application password
locally from `pipeline-env.json` and store it in the Workbench keychain. Avoid
placing it in a screenshot, terminal command, SQL file or Git commit.

After publishing an archive, these queries inspect the active complete version:

```sql
SELECT run_id, published_at_utc, source_sha256, counts_json
FROM ufc_active_publication;

SELECT name, event_date, bouts, usable_stat_bouts, held_stat_bouts
FROM ufc_card_coverage
ORDER BY event_date DESC
LIMIT 10;

SELECT gap_kind, field, reason, COUNT(*) AS gaps
FROM ufc_data_quality
GROUP BY gap_kind, field, reason
ORDER BY gaps DESC;
```

See [the warehouse schema and analytical queries](MYSQL_WAREHOUSE.md) for the
row grain, version keys, nullable statistics and historical cutoffs.

The demo deliberately changes the active warehouse version to its synthetic
snapshot. Run the normal refresh afterward to restore the real archive as the
active version; both complete publications are retained. The active publication
metadata identifies the source. Use a separate database if you want demo and real
data active at the same time.

## Manage the instance

```sh
.venv/bin/python scripts/local_mysql.py stop
.venv/bin/python scripts/local_mysql.py start
.venv/bin/python scripts/local_mysql.py uninstall-service
```

Uninstalling removes only this project's launch agent and keeps the data and
credentials. `stop` authenticates over the project socket and verifies its data
directory before requesting shutdown. `install-service` can register the service
again. For custom locations or ports, use matching `--state-dir`, `--config` and
`--port` values during setup; keep each instance's configuration together.

For an existing MySQL server or another operating system, create a dedicated
database/user using [the warehouse setup instructions](MYSQL_WAREHOUSE.md) and
set the `UFC_MYSQL_*` variables. The warehouse itself is portable; this native
service installer is specific to macOS.

Implementation references: MySQL's [data directory initialization](https://dev.mysql.com/doc/refman/8.4/en/data-directory-initialization.html)
and Apple's [launch agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).
