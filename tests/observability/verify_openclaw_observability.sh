#!/bin/bash
# Regression test for the OpenClaw gateway -> OpenTelemetry -> Splunk/Galileo
# pipeline (the agentic-risk demo surface). Run after changes to the plugin,
# run-openclaw.sh, backend/routers/toolguard.py, backend/telemetry/otel.py
# (tool_span), or the collector config. See
# .claude/skills/verify-observability/SKILL.md for the full trigger list.
#
#   Tier 0  code-level: plugin selftest + generated-config sanity (no gateway)
#   Tier 1  preconditions: gateway + collector + app up; guard answers
#   Tier 2  forwarding: an agent tool call produces execute_tool spans that
#           reach Splunk, 0 export failures
#   Tier 3  metadata: gen_ai token/duration for openclaw-gateway in O11y
#           (needs O11Y_API; --no-agent since OpenClaw metrics carry
#           no gen_ai.agent.name)
#
# Exit 0 = pass, non-zero = fail. Tiers that need the live gateway SKIP (not
# fail) when it is down, so this is safe to run in the on-demand-off default.
set -u
cd "$(dirname "$0")/../.." || exit 2

PASS=0; FAIL=0; SKIP=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $1"; SKIP=$((SKIP+1)); }

APP=http://127.0.0.1:8001
GATEWAY=http://127.0.0.1:18789
CMETRICS=http://localhost:8888/metrics
STATE_DIR="$HOME/.demobot-openclaw"
CONF="$STATE_DIR/openclaw.json"
KEY=$(grep '^ACCESS_KEY=' .env 2>/dev/null | cut -d= -f2)
sum() { curl -s "$CMETRICS" 2>/dev/null | grep -vE '^#' | grep -E "$1" | awk '{s+=$2} END{print s+0}'; }

echo "== Tier 0: plugin selftest + config sanity (code-level; no gateway) =="
# openclaw/ is sparse-excluded from this Mac's working tree (AMP deletes it), so
# the selftest runs from the image, where the plugin is baked in. Still no
# gateway needed — `podman run` on a stopped image is enough.
if ! command -v podman >/dev/null 2>&1; then
  skip "podman not on PATH -> cannot run plugin selftest"
elif ! podman image exists demobot-openclaw 2>/dev/null; then
  skip "image demobot-openclaw not built yet -> run ./run-openclaw.sh once"
elif podman run --rm demobot-openclaw \
       node /opt/demobot-plugins/demobot-toolguard/selftest.mjs >/tmp/oc_selftest.txt 2>&1; then
  ok "demobot-toolguard selftest (allow/block/timeout/fail-policy)"
else
  bad "demobot-toolguard selftest FAILED (see /tmp/oc_selftest.txt)"
fi
# The generated gateway config is the source of the silent-failure traps.
if [ -f "$CONF" ]; then
  grep -q '"protocol": "http/protobuf"' "$CONF" \
    && ok "otel protocol http/protobuf (grpc would silently drop ALL export)" \
    || bad "otel protocol is not http/protobuf in $CONF"
  grep -q '"sampleRate": 1' "$CONF" \
    && ok "sampleRate 1 (docs example 0.2 would drop 80% of demo traces)" \
    || bad "sampleRate is not 1 in $CONF"
  grep -q '"captureContent": true' "$CONF" \
    && ok "captureContent true (tool inputs/outputs on spans)" \
    || skip "captureContent not true (ok if intentional)"
  grep -q '"serviceName": "openclaw-gateway"' "$CONF" \
    && ok "serviceName openclaw-gateway" \
    || bad "serviceName is not openclaw-gateway in $CONF"
else
  skip "no generated config at $CONF yet (run ./run-openclaw.sh once)"
fi

echo "== Tier 1: preconditions =="
GATEWAY_UP=0
if curl -s --max-time 3 "$GATEWAY/" >/dev/null 2>&1; then
  ok "OpenClaw gateway up on :18789"; GATEWAY_UP=1
else
  skip "gateway DOWN on :18789 -> start it:  ./run-openclaw.sh   (Tiers 2-3 will skip)"
fi
lsof -nP -iTCP:4317 -sTCP:LISTEN >/dev/null 2>&1 \
  && ok "OTel collector listening on :4317" \
  || bad "OTel collector is DOWN -> start it:  ./run-collector.sh"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$APP/health" 2>/dev/null)" = "200" ] \
  && ok "app healthy on :8001 (the tool guard is fail-closed without it)" \
  || bad "app NOT healthy on :8001 -> start it:  ./run.sh"
# The guard endpoint must answer allow for a benign in-workspace read.
RESP=$(curl -s --max-time 8 -u "x:$KEY" -X POST "$APP/api/toolguard/inspect" \
  -H 'Content-Type: application/json' \
  -d '{"tool_name":"read","arguments":{"path":"/home/node/.openclaw/workspace/inbox/x.md"},"session_id":"verify","tool_call_id":"v1"}' 2>/dev/null)
echo "$RESP" | grep -q '"decision": *"allow"' \
  && ok "tool guard answers allow for a benign read" \
  || bad "tool guard did not answer allow (got: ${RESP:0:120})"

if [ "$GATEWAY_UP" -ne 1 ]; then
  echo; echo "RESULT: $PASS passed, $FAIL failed, $SKIP skipped (gateway down -> Tiers 2-3 skipped)"
  [ $FAIL -eq 0 ] && exit 0 || exit 1
fi

echo "== Tier 2: an agent tool call reaches Splunk as execute_tool spans =="
spans0=$(sum 'otelcol_exporter_sent_spans')
fail0=$(sum 'otelcol_exporter_send_failed_(spans|metric_points)')
# Drive one agent turn that must make a tool call. `openclaw agent` runs a turn
# via the gateway; the prompt asks it to read a workspace file (a `read` tool
# call), which the guard inspects and the diagnostics-otel plugin traces.
if podman exec demobot-openclaw openclaw agent \
     "List the files in your inbox and read the newest one." >/tmp/oc_agent_turn.txt 2>&1; then
  ok "agent turn executed (see /tmp/oc_agent_turn.txt)"
else
  skip "could not drive an agent turn via 'openclaw agent' (see /tmp/oc_agent_turn.txt)"
fi
echo "  waiting ~75s for span flush..."
sleep 75
spans1=$(sum 'otelcol_exporter_sent_spans')
fail1=$(sum 'otelcol_exporter_send_failed_(spans|metric_points)')
[ "$spans1" -gt "$spans0" ] && ok "spans forwarded to Splunk ($spans0 -> $spans1)" || bad "no new spans forwarded ($spans0 -> $spans1)"
[ "$fail1" -le "$fail0" ] && ok "no new export failures (failed total=$fail1)" || bad "export failures increased ($fail0 -> $fail1)"
# Galileo genai_only filter: execute_tool spans set gen_ai.operation.name and
# survive it; watch the Galileo exporter specifically for partial-drops.
gfail=$(sum 'otelcol_exporter_send_failed_spans.*galileo')
[ "${gfail:-0}" -eq 0 ] && ok "no Galileo span send failures (genai_only filter passing execute_tool)" \
  || bad "Galileo span send failures=$gfail -> orphaned-parent drop? add transform/openclaw_genai"

echo "== Tier 3: openclaw-gateway GenAI metadata in Observability Cloud =="
APITOK=$(grep '^O11Y_API=' .env 2>/dev/null | cut -d= -f2)
REALM=$(grep '^SPLUNK_REALM=' .env 2>/dev/null | cut -d= -f2)
if [ -z "${APITOK:-}" ]; then
  skip "O11Y_API not set in .env (an O11y API token) -> skipping metadata assertion"
else
  if python3 tests/observability/check_o11y_metadata.py "$REALM" "$APITOK" openclaw-gateway --no-agent; then
    ok "gen_ai token usage + operation.duration present for openclaw-gateway"
  else
    bad "gen_ai metadata missing/incomplete for openclaw-gateway"
  fi
fi

echo
echo "RESULT: $PASS passed, $FAIL failed, $SKIP skipped"
[ $FAIL -eq 0 ] && exit 0 || exit 1
