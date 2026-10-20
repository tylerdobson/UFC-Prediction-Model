#!/bin/sh
# Normal demo uses isolated native MySQL. Source fixtures need no external fetch.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'This demo requires uv and an installed Python 3.11+. See README.md.' >&2
  exit 1
fi

cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/ufc-forecast-demo-uv-cache}"

for argument in "$@"; do
  if [ "$argument" = '--legacy-sqlite-fixture' ]; then
    exec uv run --offline --no-project --no-python-downloads --no-env-file --python '>=3.11' \
      python -B -m ufc_odds_model.portfolio_demo "$@"
  fi
done

exec uv run --frozen --no-python-downloads --no-env-file \
  python -B -m ufc_odds_model.portfolio_demo "$@"
