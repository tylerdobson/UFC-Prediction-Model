#!/bin/sh
# Export the checked-in Mermaid source without changing project dependencies.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIAGRAM_DIR="$PROJECT_DIR/docs/diagrams"
export PUPPETEER_SKIP_DOWNLOAD=true
export npm_config_cache="${npm_config_cache:-${TMPDIR:-/tmp}/ufc-forecast-mermaid-npm-cache}"

if ! command -v npx >/dev/null 2>&1; then
  printf '%s\n' 'Architecture exports require Node.js 22.13+ and npx.' >&2
  exit 1
fi

PUPPETEER_CONFIG="$DIAGRAM_DIR/puppeteer-config.json"
TEMP_CONFIG=''
if [ -n "${PUPPETEER_EXECUTABLE_PATH:-}" ]; then
  TEMP_CONFIG=$(mktemp "${TMPDIR:-/tmp}/ufc-mermaid-chrome.XXXXXX")
  trap 'rm -f "$TEMP_CONFIG"' EXIT HUP INT TERM
  node -e 'process.stdout.write(JSON.stringify({executablePath:process.env.PUPPETEER_EXECUTABLE_PATH,headless:true}))' > "$TEMP_CONFIG"
  PUPPETEER_CONFIG="$TEMP_CONFIG"
fi

for FORMAT in svg png; do
  npx --yes --registry=https://registry.npmjs.org \
    --package=@mermaid-js/mermaid-cli@12.0.0 mmdc \
    --input "$DIAGRAM_DIR/statistics-pipeline.mmd" \
    --output "$DIAGRAM_DIR/statistics-pipeline.$FORMAT" \
    --configFile "$DIAGRAM_DIR/mermaid-config.json" \
    --puppeteerConfigFile "$PUPPETEER_CONFIG" \
    --backgroundColor white --size 1100 --scale 2
done
