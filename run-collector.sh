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
# Galileo (LLM observability) — optional second trace destination.
export GALILEO_API_KEY=$(grep '^GALILEO_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
export GALILEO_PROJECT=$(grep '^GALILEO_PROJECT=' .env 2>/dev/null | cut -d= -f2- || true)
export GALILEO_LOG_STREAM=$(grep '^GALILEO_LOG_STREAM=' .env 2>/dev/null | cut -d= -f2- || true)
# Logs — optional Splunk platform HEC destination (Splunk Observability Cloud
# has no log ingest on this org; see otel-collector-logs.yaml).
export SPLUNK_HEC_URL=$(grep '^SPLUNK_HEC_URL=' .env 2>/dev/null | cut -d= -f2- || true)
export SPLUNK_HEC_TOKEN=$(grep '^SPLUNK_HEC_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)
export SPLUNK_HEC_INDEX=$(grep '^SPLUNK_HEC_INDEX=' .env 2>/dev/null | cut -d= -f2- || true)

# The Galileo exporter + pipeline live in an overlay config that is layered on
# ONLY when a key is present. Previously they were unconditional in the base
# config and the vars were exported even when empty, so a keyless deployment
# POSTed every gen_ai span — prompt and response content included — to Galileo
# with an empty API-key header, failing continuously and visibly only in the
# collector's own log.
CONFIGS=(--config otel-collector-config.yaml)
if [ -n "${GALILEO_API_KEY:-}" ]; then
  CONFIGS+=(--config otel-collector-galileo.yaml)
  GALILEO_STATE="on -> api.multitenant.galileocloud.io"
else
  GALILEO_STATE="OFF (no GALILEO_API_KEY in .env)"
fi
# Same gating for the logs pipeline: no HEC credentials -> no logs pipeline at
# all, rather than a pipeline that queues and retries against an empty endpoint.
if [ -n "${SPLUNK_HEC_TOKEN:-}" ] && [ -n "${SPLUNK_HEC_URL:-}" ]; then
  CONFIGS+=(--config otel-collector-logs.yaml)
  LOGS_STATE="on -> ${SPLUNK_HEC_URL} (index=${SPLUNK_HEC_INDEX:-default})"
else
  LOGS_STATE="OFF (no SPLUNK_HEC_URL/SPLUNK_HEC_TOKEN in .env)"
fi
if [ -z "${SPLUNK_REALM:-}" ] || [ -z "${SPLUNK_ACCESS_TOKEN:-}" ]; then
  echo "ERROR: set SPLUNK_REALM and SPLUNK_ACCESS_TOKEN in .env first." >&2
  exit 1
fi

echo "Starting OTel Collector (realm=$SPLUNK_REALM) -> Splunk Observability Cloud"
echo "Galileo trace fan-out: $GALILEO_STATE"
echo "Logs -> Splunk platform HEC: $LOGS_STATE"
echo "Listening on :4317 (OTLP/gRPC) and :4318 (OTLP/HTTP). Ctrl+C to stop."

# Native binary (downloaded once by the setup; see README/skill).
if [ -x ./bin/otelcol-contrib ]; then
  exec ./bin/otelcol-contrib "${CONFIGS[@]}"
fi

# Fallback: containerized collector.
RUNTIME="$(command -v podman || command -v docker || true)"
if [ -z "$RUNTIME" ]; then
  echo "ERROR: ./bin/otelcol-contrib missing and no podman/docker available." >&2
  echo "Re-download the binary or start podman, then retry." >&2
  exit 1
fi
# Mount both configs; pass the overlay only when a Galileo key is present, so the
# containerized path gates identically to the native one above.
CONTAINER_CONFIGS=(--config=/etc/otelcol-contrib/config.yaml)
[ -n "${GALILEO_API_KEY:-}" ] && CONTAINER_CONFIGS+=(--config=/etc/otelcol-contrib/galileo.yaml)
if [ -n "${SPLUNK_HEC_TOKEN:-}" ] && [ -n "${SPLUNK_HEC_URL:-}" ]; then
  CONTAINER_CONFIGS+=(--config=/etc/otelcol-contrib/logs.yaml)
fi
exec "$RUNTIME" run --rm --name otel-collector \
  -p 4317:4317 -p 4318:4318 \
  -e SPLUNK_REALM -e SPLUNK_ACCESS_TOKEN \
  -e GALILEO_API_KEY -e GALILEO_PROJECT -e GALILEO_LOG_STREAM \
  -e SPLUNK_HEC_URL -e SPLUNK_HEC_TOKEN -e SPLUNK_HEC_INDEX \
  -v "$PWD/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
  -v "$PWD/otel-collector-galileo.yaml:/etc/otelcol-contrib/galileo.yaml:ro" \
  -v "$PWD/otel-collector-logs.yaml:/etc/otelcol-contrib/logs.yaml:ro" \
  docker.io/otel/opentelemetry-collector-contrib:latest \
  "${CONTAINER_CONFIGS[@]}"
