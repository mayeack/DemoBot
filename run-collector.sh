#!/bin/bash
# Run a local OpenTelemetry Collector that forwards the app's OTLP telemetry to
# Splunk Observability Cloud (metrics via the signalfx exporter, traces via the
# Splunk OTLP/APM ingest). Reads SPLUNK_REALM + SPLUNK_ACCESS_TOKEN from .env.
# Prefers the native ./bin/otelcol-contrib binary; falls back to podman/docker.
# Run this alongside ./run.sh.
set -euo pipefail
cd "$(dirname "$0")"

export SPLUNK_REALM=$(grep '^SPLUNK_REALM=' .env 2>/dev/null | cut -d= -f2- || true)
export SPLUNK_ACCESS_TOKEN=$(grep '^SPLUNK_ACCESS_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)
if [ -z "${SPLUNK_REALM:-}" ] || [ -z "${SPLUNK_ACCESS_TOKEN:-}" ]; then
  echo "ERROR: set SPLUNK_REALM and SPLUNK_ACCESS_TOKEN in .env first." >&2
  exit 1
fi

echo "Starting OTel Collector (realm=$SPLUNK_REALM) -> Splunk Observability Cloud"
echo "Listening on :4317 (OTLP/gRPC) and :4318 (OTLP/HTTP). Ctrl+C to stop."

# Native binary (downloaded once by the setup; see README/skill).
if [ -x ./bin/otelcol-contrib ]; then
  exec ./bin/otelcol-contrib --config otel-collector-config.yaml
fi

# Fallback: containerized collector.
RUNTIME="$(command -v podman || command -v docker || true)"
if [ -z "$RUNTIME" ]; then
  echo "ERROR: ./bin/otelcol-contrib missing and no podman/docker available." >&2
  echo "Re-download the binary or start podman, then retry." >&2
  exit 1
fi
exec "$RUNTIME" run --rm --name otel-collector \
  -p 4317:4317 -p 4318:4318 \
  -e SPLUNK_REALM -e SPLUNK_ACCESS_TOKEN \
  -v "$PWD/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
  docker.io/otel/opentelemetry-collector-contrib:latest \
  --config=/etc/otelcol-contrib/config.yaml
