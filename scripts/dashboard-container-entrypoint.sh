#!/bin/sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
  echo "Dashboard container requires a non-root host UID/GID" >&2
  exit 1
fi

python -m ufc_odds_model.deployment \
  --root "$UFC_DEPLOY_ROOT" --db "$UFC_MODEL_DB" --quiet

exec python -m streamlit run /app/app.py \
  --server.address 0.0.0.0 --server.port 8501 --server.headless true
