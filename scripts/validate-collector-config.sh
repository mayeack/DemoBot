#!/bin/bash
# Validate the collector config(s) exactly as run-collector.sh would launch them:
# read the same .env keys, then run `otelcol-contrib validate` on the base config
# and on base+galileo when a key is present. Exits non-zero on invalid config.
set -euo pipefail
cd "$(dirname "$0")/.."

export SPLUNK_REALM=$(grep '^SPLUNK_REALM=' .env 2>/dev/null | cut -d= -f2- || true)
export O11Y_INGEST=$(grep '^O11Y_INGEST=' .env 2>/dev/null | cut -d= -f2- || true)
export GALILEO_API_KEY=$(grep '^GALILEO_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
export GALILEO_PROJECT=$(grep '^GALILEO_PROJECT=' .env 2>/dev/null | cut -d= -f2- || true)
export GALILEO_LOG_STREAM=$(grep '^GALILEO_LOG_STREAM=' .env 2>/dev/null | cut -d= -f2- || true)

./bin/otelcol-contrib validate --config otel-collector-config.yaml
echo "base config: OK"
if [ -n "${GALILEO_API_KEY:-}" ]; then
  ./bin/otelcol-contrib validate --config otel-collector-config.yaml --config otel-collector-galileo.yaml
  echo "base + galileo overlay: OK"
fi
