# Private MySQL dashboard container

Compose packages the read-only Streamlit dashboard for one operator on macOS or Linux. It publishes port 8501 on `127.0.0.1`. The normal runtime is MySQL 8.4; a connection failure does not select a SQLite file. The container does not run ingestion, refresh sources, send alerts or place wagers.

The image contains application code, pinned dependencies, public images and the frozen V6 report edition. Private raw sources and database files stay outside the image. Compose mounts data/, reports/, docs/ and an owner-only MySQL configuration file read-only, runs with the operator's non-root UID/GID, and uses a read-only root filesystem. The approved dashboard presentation remains the same.

## Prepare MySQL and retained evidence

Use the [MySQL runtime guide](MYSQL_RUNTIME.md) to configure the project database and create native schemas before serving the dashboard:

~~~sh
uv sync --frozen --all-extras
uv run --frozen ufc-mysql init
~~~

An installation with retained local research evidence can perform the explicit, verified bootstrap described in that guide. It imports original evidence once into native MySQL and preserves the original files. A clean clone can run ./scripts/demo.sh to produce a separate synthetic namespace and reports, then refresh real sources separately. The synthetic demo never becomes mysql:operating, mysql:statistics or the active real-data warehouse.

Normal handles are mysql:operating, mysql:research-complete and mysql:statistics. Their names can be overridden through UFC_MODEL_DB, UFC_MODEL_HISTORY_DB and UFC_MODEL_STATISTICS_DB. Startup requires the operating handle to have the current operating schema and rejects synthetic, immutable research or statistics datasets in that role.

Retain all operating receipt payloads under the checkout's data/ directory. Startup checks every recorded SHA-256, foreign-key references and the migration version without changing the dataset. Relative receipt paths resolve under UFC_DEPLOY_ROOT; missing old absolute paths under data/raw/ can resolve into the copied checkout and still must match their original hashes. Existing outside paths and symlinks are rejected. Copying a report alone does not supply its ignored private evidence.

## Launch and inspect

Docker Engine with Compose v2 is required. Use the physical checkout path and the private project configuration created by the local setup helper, or an equivalent external owner-only JSON file containing the allowed UFC_MYSQL_* application credentials. Do not put its contents in Compose output, Git or a Docker image.

~~~sh
export UFC_DEPLOY_ROOT="$(pwd -P)"
export UFC_DEPLOY_UID="$(id -u)"
export UFC_DEPLOY_GID="$(id -g)"
export UFC_MODEL_DB=mysql:operating
export UFC_MODEL_HISTORY_DB=mysql:research-complete
export UFC_MODEL_STATISTICS_DB=mysql:statistics
export UFC_MYSQL_CONFIG_FILE="$HOME/Library/Application Support/UFCForecast/pipeline-env.json"
export UFC_MYSQL_CONFIG="$UFC_MYSQL_CONFIG_FILE"
export UFC_MYSQL_PORT=3307
mkdir -p data reports
uv run --frozen python -m ufc_odds_model.deployment --root "$UFC_DEPLOY_ROOT"
docker compose config -q
docker compose up --build -d
curl -fsS http://127.0.0.1:8501/_stcore/health
~~~

Open http://127.0.0.1:8501. Streamlit health confirms that the server responds. Startup separately verifies the operating schema and mounted receipt bytes; neither check establishes model accuracy, source rights or a current executable price. Missing research datasets or unpublished evidence remain explicit unavailable states in their dashboard sections.

The configuration is bound to /run/ufc-secrets/mysql.json. The container sets its MySQL host to UFC_DOCKER_MYSQL_HOST (default host.docker.internal) and uses UFC_MYSQL_PORT (default 3307); credentials remain in the bound file. For a remote or container-hosted server, set a reachable private hostname and port. On Linux, the bridge cannot connect to host MySQL bound only to 127.0.0.1; configure a private reachable interface and restrict database access to the application. Do not expose MySQL publicly. The existing macOS local helper intentionally binds its instance to host loopback; Docker connectivity must be verified on the chosen host.

The runtime bridge supplies database connectivity. The optional [hosted overlay](HOSTED_DEPLOYMENT.md) also joins the internal authenticated application bridge. Only Caddy receives public HTTP/HTTPS traffic. Stop the dashboard with docker compose down; host evidence and external MySQL data remain retained.

## Recovery and legacy boundaries

Use the [backup runbook](BACKUP_RUNBOOK.md) and [evidence bundles](EVIDENCE_BUNDLE.md) for native logical snapshots, retained source artifacts and restore drills. Restores require new namespaces; they do not overwrite a live alias automatically. Refreshes and model experiments run in the operator environment and publish separately from the read-only dashboard.

An explicit legacy SQLite path is still accepted by ufc_odds_model.deployment for migration/evidence checks. It must be an absolute standalone snapshot under data/, with no -wal, -shm or -journal sidecar and matching retained source bytes. This compatibility path is not the normal container default, and the retired UFC_DB_FILE / UFC_HISTORY_DB_PATH variables no longer select runtime data.

UFC_DEPLOY_ROOT, data/, reports/ and docs/ must exist; Compose refuses to create missing bind sources. Deployment UID/GID must be non-root and own private 0600 config/source files. Provider API keys are not supplied to the web process. UFC 332 forward evidence can additionally use UFC_MODEL_FORWARD_EVIDENCE_ROOT and the original mapping UFC_MODEL_FORWARD_SOURCE_ROOT; those historical reports keep their original source and research-only limitations.

CI includes a disposable MySQL 8.4 service, native operating schema, private retained receipt and container startup checks. Unit tests also reject tampered/outside payloads, synthetic runtime roles and a missing MySQL connection without opening SQLite. CI definitions are not evidence that a hosted deployment has run.
