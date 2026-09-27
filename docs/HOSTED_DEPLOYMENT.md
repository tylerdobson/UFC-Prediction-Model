# Private hosted dashboard plan

This is a **reviewable scaffold**, not a deployed service. It serves the existing
read-only Streamlit snapshot at `app.tylerjamesdobson.com` for one GitHub account.
The root domain and `www` keep serving the separate portfolio. The hosted stack
does not ingest data, refresh odds, generate alerts, or place bets.

## Architecture and expected cost

Use one DigitalOcean Basic Droplet with 2 GiB RAM, one shared vCPU, and 50 GiB
SSD ($12/month), plus daily Droplet backups (30%, or $3.60/month). The estimated
base is **$15.60/month** before tax, transfer overages, or future storage growth.
A Cloud Firewall and a Reserved IPv4 assigned to the Droplet have no additional
charge; an unassigned Reserved IPv4 can incur a fee. Verify prices when creating
the server. [Droplet pricing](https://www.digitalocean.com/pricing/droplets),
[backup pricing](https://docs.digitalocean.com/products/backups/details/pricing/),
[firewall pricing](https://docs.digitalocean.com/products/networking/firewalls/details/),
[Reserved IP pricing](https://docs.digitalocean.com/products/networking/reserved-ips/details/pricing/).

The request path is:

```text
browser → Squarespace `app` A record → Reserved IPv4 → Caddy HTTPS
        → OAuth2 Proxy GitHub allowlist → read-only Streamlit container
```

Caddy is the only public container. Its catchall route authenticates **all**
Streamlit paths, including `/_stcore/stream` WebSocket upgrades and the health
endpoint. The `/oauth2/` path reaches only OAuth2 Proxy for sign-in and callback.
The dashboard remains published on `127.0.0.1:8501` for host-side checks, with
no public port 8501 rule. Caddy [renews public certificates automatically when
DNS and ports 80/443 are ready](https://caddyserver.com/docs/automatic-https).
The authentication route follows [OAuth2 Proxy's Caddy integration](https://oauth2-proxy.github.io/oauth2-proxy/configuration/integrations/caddy/)
and its [GitHub username allowlist](https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/github/).

## Inputs required before launch

1. A DigitalOcean account with billing, a chosen region, and the operator's SSH
   public key. Use a non-root SSH user, key-only access, a Cloud Firewall allowing
   TCP 80/443 and restricted SSH, and daily backups. [DigitalOcean setup](https://docs.digitalocean.com/products/droplets/getting-started/recommended-droplet-setup/).
2. The exact GitHub username allowed into the app and a GitHub OAuth App owned by
   the operator. Set homepage `https://app.tylerjamesdobson.com/` and exact
   callback `https://app.tylerjamesdobson.com/oauth2/callback`; disable wildcard
   callback matching. Keep its client secret on the server, not in Git, chat, or
   the dashboard container. [GitHub OAuth App setup](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app),
   [callback matching](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).
   Enable two-factor authentication on that GitHub account.
3. Permission to add **only** an `A` record named `app` in Squarespace after the
   stack passes local checks. Point it to the assigned Reserved IPv4; leave `@`
   and `www` untouched. [Squarespace subdomain guide](https://support.squarespace.com/hc/en-us/articles/215744668-Pointing-a-Squarespace-domain).

## Prepare the server without publishing it

1. Create the Droplet and assign a Reserved IPv4. Add a Cloud Firewall with only
   TCP 80/443 from the internet and SSH from the operator's IP where practical.
   Do not allow 8501 or 4180. Install Docker Engine and Compose v2. Add a
   [read-only GitHub deploy key](https://docs.github.com/en/enterprise-cloud%40latest/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
   for this private repository.
2. Clone at a physical Linux path such as `/opt/ufc-prediction-model`. Copy the
   current `data/`, `reports/`, and `docs/` into that checkout over SSH, keeping
   filenames, bytes, and private modes. Also copy these two private bundles to
   `backups/`: `ufc332-timestamped-card-odds-20260927T1602Z.zip` and
   `ufc-research-1993-2026-current-20260927T162302Z.zip`. Check their SHA-256
   digests against, respectively,
   `a312ac1bbe7fdacc287f7f536a571fbde85ca3269f7793c4743391be575482d2`
   and `2c1b9dfd0d0636e8f5c6ed9a910e00cf4062e65b8a1115e5888f122e0252c399`.
   These archives and the copied source data are private and Git-ignored. Do
   **not** copy `.env`, `ODDS_API_KEY`, provider keys, or workstation credentials.
3. On the host, install Python 3.12 and the project's Python environment, then verify and
   restore the two bundles to distinct new directories under `data/`. Do not
   edit the archived database or the saved reports. The restore records its
   receipt-path changes in `restore_report.json`; the unchanged copied research
   database remains under `data/` for independent forward-report verification.

   ```bash
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -e .
   .venv/bin/python -m ufc_odds_model.evidence_bundle verify --bundle backups/ufc332-timestamped-card-odds-20260927T1602Z.zip
   .venv/bin/python -m ufc_odds_model.evidence_bundle verify --bundle backups/ufc-research-1993-2026-current-20260927T162302Z.zip
   .venv/bin/python -m ufc_odds_model.evidence_bundle restore --bundle backups/ufc332-timestamped-card-odds-20260927T1602Z.zip --output data/restored-ufc332-pilot
   .venv/bin/python -m ufc_odds_model.evidence_bundle restore --bundle backups/ufc-research-1993-2026-current-20260927T162302Z.zip --output data/restored-research-history
   .venv/bin/python -m ufc_odds_model.integrity --db data/restored-ufc332-pilot/database.sqlite --output reports/integrity.json
   .venv/bin/ufc-model --db data/restored-ufc332-pilot/database.sqlite evaluate --decision-hours-before-event 24 --output reports/evaluation.json
   ```

   The fresh operating evaluation should report insufficient historical
   evidence for model promotion. Its report is bound to the restored pilot
   database, while the retrospective holdout remains bound to the separate
   research database.

   Set `UFC_DB_FILE=restored-ufc332-pilot/database.sqlite`. Set
   `UFC_HISTORY_DB_PATH` to the absolute path of the separately restored
   `data/restored-research-history/database.sqlite`. Set
   `UFC_FORWARD_SOURCE_ROOT` to the **original workstation checkout path**
   embedded in the saved forward reports; the verifier maps those references
   into the copied host checkout, checks every source byte and receipt, and
   rejects missing files, links, or paths outside that root. The restored
   history DB is read only in the web app and cannot supply operating alerts.
4. Before publishing, run the combined preflight. It verifies both restored
   databases, the research holdout, and every retained source used by the
   copied Sep 27 forward reports. For this snapshot it should report 10
   operating receipts, 2,837 research receipts, 790 completed research events,
   7,258 results, and eight exploratory forward bouts.

   ```bash
   .venv/bin/python scripts/check_hosted_snapshot.py \
     --root "$(pwd -P)" \
     --original-root /Users/tylerjamesdobson/Documents/ChatGPT/Projects/UFC-Prediction-Model
   ```

   The [container deployment runbook](CONTAINER_DEPLOYMENT.md) explains the
   single-file and receipt checks. A failed hash, missing receipt, SQLite
   sidecar, or path mapping mismatch blocks release. These checks do not grant
   data-source rights or establish a current executable quote.
5. Copy `deploy/hosted.env.example` to `deploy/hosted.env` on the server and fill
   in its non-secret values. The actual file is Git-ignored. Create
   `deploy/secrets/github-client-secret` containing exactly the OAuth client
   secret with no newline and `deploy/secrets/cookie-secret` containing 32
   cryptographically random bytes. Own both files with the non-root deployment
   UID/GID and mode `0600`; `deploy/secrets/` is Git-ignored. OAuth2 Proxy
   supports [file-backed secrets](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/).
   The web stack gets **no** odds or fight-data provider key.
6. From the checkout root, validate and start the overlay only after the secret
   files and snapshot exist:

   ```bash
   docker compose --env-file deploy/hosted.env -f compose.yaml -f deploy/compose.hosted.yaml config -q
   docker compose --env-file deploy/hosted.env -f compose.yaml -f deploy/compose.hosted.yaml run --no-deps --rm --entrypoint caddy caddy validate --config /etc/caddy/Caddyfile
   docker compose --env-file deploy/hosted.env -f compose.yaml -f deploy/compose.hosted.yaml up --build -d
   ```

   The overlay fixes Caddy's bridge address to `172.30.62.2`, which is the sole
   trusted proxy IP in OAuth2 Proxy. Check this subnet does not conflict with
   any server/VPC network before starting; change the bridge subnet, Caddy IP,
   and `--trusted-proxy-ip` together if it does. Caddy certificate state lives
   in a named volume; keep that volume across container replacements. Confirm
   OAuth2 Proxy starts without configuration errors in its container logs before
   the public cutover; the anonymous access checks below are the release gate.

## Public cutover checks

After the host preflight and auth config pass, add the `app` A record. Wait for
DNS to resolve to the Reserved IPv4 and Caddy to issue a valid certificate.
Check from outside the VPS:

- An anonymous request to `/` and `/_stcore/health` redirects to GitHub sign-in;
  neither returns dashboard content.
- An anonymous WebSocket upgrade to `/_stcore/stream` does **not** receive HTTP
  `101 Switching Protocols`. A signed-in browser can load the dashboard.
- An account other than the allowlisted GitHub username cannot enter. The
  allowed account can sign out; a cleared session becomes anonymous again.
- Port 8501 cannot be reached from the internet. The root domain and `www` still
  open the portfolio. The dashboard shows the selected snapshot and research
  labels without turning on alert or promotion eligibility.
- Restore one daily backup to a separate test Droplet or test directory and run
  the evidence preflight there. DigitalOcean's backup images are
  [restorable](https://docs.digitalocean.com/products/backups/how-to/create-and-restore/),
  but restoring an existing Droplet replaces its later data, so use a separate
  test target for a drill.

The daily backup protects the server's local evidence from a host failure; it
does not replace the immutable source receipts or their hash checks. Keep the
workstation's private source copy and both verified bundles for further restore
drills.
