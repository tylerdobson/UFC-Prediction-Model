# Private dashboard container

The Compose deployment packages the **read-only dashboard** for one operator on macOS or Linux. It publishes port 8501 only on `127.0.0.1`. It does not run ingestion, refresh odds, alert, paper-trade, or place wagers. Continue using the [local operator runbook](LOCAL_RUNBOOK.md) for those CLI jobs. There is no user authentication; do not expose this port through a public reverse proxy or a remote Docker host.

The image contains application code and pinned Python dependencies. It excludes the database, raw sources, reports, backups, model artifacts, `.env`, and Streamlit secrets. Compose mounts `data/`, `reports/`, and `docs/` read-only at their original host paths, runs with the operator's non-root UID/GID and a read-only root filesystem, and gives Streamlit a temporary cache directory. The forward research panel needs the historical manifest in `docs/` and saved proof files in `data/`; it independently verifies them before display. Matching the host user lets it read private `0600` source files without widening their permissions. No API key is supplied to the web process.

## Prepare a stable snapshot

From the repository root, with the project installed, apply any pending schema migrations to the source database using a **new, verified pre-migration backup**. Then create a **new** single-file SQLite dashboard backup in `data/`. The dashboard backup includes committed WAL pages and drills a restore. Do not rename or overwrite an earlier snapshot. Replace both example timestamps with unique values:

```bash
ufc-model --db data/ufc.sqlite migrate --backup backups/ufc-before-dashboard-20260926T175000Z.sqlite
python -m ufc_odds_model.backup create \
  --db data/ufc.sqlite --output data/dashboard-20260926T180000Z.sqlite
```

The startup preflight rejects a snapshot whose schema is older than the packaged code. If the source database is already current, the migration command reports no pending migrations; use a fresh backup path on every later attempt.

Source receipts in that snapshot must point to files inside this checkout's `data/` directory. The container mounts `data/` at the **same absolute path** as on the host so those immutable receipt paths remain valid. If source files live elsewhere, use the [evidence-bundle restore](EVIDENCE_BUNDLE.md) into a new directory under `data/`, then select its `database.sqlite`. Do not edit receipt paths by hand. If a dataset was copied from another machine, the bundle restore rebases its paths. The container startup check rejects missing or changed payloads and live SQLite sidecars.

The optional UFC 332 forward reports also contain absolute paths to their saved card, lookup, odds, research database, and historical receipts. Their verifier rejects missing paths and reports outside the selected evidence root. When a checkout moves to another machine, set `UFC_FORWARD_SOURCE_ROOT` to the original absolute checkout path and keep `UFC_DEPLOY_ROOT` at the new physical path. The verifier maps saved paths into the copied `data/`, `reports/`, and `docs/` trees and rechecks their exact hashes and the original research database's receipt catalog. Copying only the JSON reports will leave the panel unavailable. The operating database still needs a portable evidence-bundle restore under `data/` so its receipt paths are valid at the new location.

Regenerate any saved reports against the **snapshot path**, since an operating evaluation and integrity report are tied to the exact database file. For example:

```bash
python -m ufc_odds_model.integrity \
  --db data/dashboard-20260926T180000Z.sqlite --output reports/integrity.json
ufc-model --db data/dashboard-20260926T180000Z.sqlite evaluate \
  --decision-hours-before-event 24 --output reports/evaluation.json
```

For a Wikipedia research snapshot, keep its separate research-only holdout report; the dashboard checks the accepted-result fingerprint and retained receipts. Do not treat that report as an operating evaluation. A report missing from `reports/` appears as unavailable. A report tied to another database is labeled wrong database or stale.

## Launch and inspect

Docker Engine with Compose v2 is required. Use the physical absolute checkout path (`pwd -P`), choose the snapshot basename, and run the same read-only preflight used by the container:

```bash
export UFC_DEPLOY_ROOT="$(pwd -P)"
export UFC_DB_FILE=dashboard-20260926T180000Z.sqlite
export UFC_DEPLOY_UID="$(id -u)"
export UFC_DEPLOY_GID="$(id -g)"
python -m ufc_odds_model.deployment \
  --root "$UFC_DEPLOY_ROOT" --db "$UFC_DEPLOY_ROOT/data/$UFC_DB_FILE"
docker compose up --build -d
curl -fsS http://127.0.0.1:8501/_stcore/health
```

Open `http://127.0.0.1:8501` and inspect the source status, model evidence, and ledger states. The health endpoint confirms only that Streamlit is serving; the startup preflight confirms source-byte integrity and mount layout. Neither check establishes source rights, factual accuracy, model calibration, or an executable price. The dashboard's Data quality tab shows the saved integrity report and its age.

When a new event or correction arrives, create a new standalone backup and regenerate its reports. Change `UFC_DB_FILE` to the new basename, then run `docker compose up -d` again. Keep older snapshots until the event review and recovery window are complete. Stop the dashboard with `docker compose down`; this does not delete host data.

## Constraints

- `UFC_DEPLOY_ROOT` must be an absolute, physical host path. This same-path bind-mount layout is for local macOS/Linux Docker; Windows drive paths need a separate packaging plan.
- `UFC_DEPLOY_UID` and `UFC_DEPLOY_GID` must match a **non-root** operator that owns `data/` and `reports/`. The entrypoint refuses UID 0. Private raw payloads remain `0600`; do not make them world-readable for the container. The CI container smoke test uses a `0600` payload.
- `data/`, `reports/`, and `docs/` must exist. Compose refuses to create missing host directories. Place all retained operating receipt payloads under `data/`; the preflight rejects an outside path even when its bytes still hash correctly. The dashboard uses `UFC_MODEL_FORWARD_EVIDENCE_ROOT` and optional `UFC_MODEL_FORWARD_SOURCE_ROOT` to resolve and verify saved UFC 332 research evidence. Those reports remain unavailable if the ignored local evidence has not been copied or its path mapping fails; they do not change the startup database preflight. `UFC_MODEL_HISTORY_DB` optionally selects a separately restored research-only database for historical charts.
- The selected database must be a standalone backup without `-wal`, `-shm`, or `-journal` sidecars. This keeps the web container separate from concurrent CLI writes.
- The published host port is loopback-only. Remote access needs a separate authenticated design and security review.
- The container does not receive provider credentials and cannot fetch live odds. Run `alert-event` or `paper-trade` in the operator environment with server-side keys, then publish a fresh snapshot for the dashboard. Displayed quotes remain observations, not a live offer.
