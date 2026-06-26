#!/bin/bash
# Regression test for the DemoBot -> OpenTelemetry -> Splunk Observability Cloud
# pipeline. Run after ANY change that touches the observability integration
# (see .claude/skills/verify-observability/SKILL.md for the trigger list).
#
#   Tier 1  preconditions: collector + app up (catches the #1 incident: a dead
#           collector silently dropping all telemetry)
#   Tier 2  forwarding:    spans + metrics actually reach Splunk, 0 failures
#   Tier 3  metadata:      gen_ai.client.token.usage (+ model) and
#           operation.duration are queryable in O11y (needs SPLUNK_API_TOKEN)
#
# Exit 0 = pass, non-zero = fail. Sends a couple of real chat turns (LLM cost).
set -u
cd "$(dirname "$0")/../.." || exit 2

PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

APP=http://127.0.0.1:8001
CMETRICS=http://localhost:8888/metrics
KEY=$(grep '^ACCESS_KEY=' .env 2>/dev/null | cut -d= -f2)
sum() { curl -s "$CMETRICS" 2>/dev/null | grep -vE '^#' | grep -E "$1" | awk '{s+=$2} END{print s+0}'; }

echo "== Tier 0: gen_ai spans carry message content (code-level; no live Splunk) =="
# The #1 cause of an empty "AI trace data" view: spans reach APM with only gen_ai
# metadata and no prompt/response, so Splunk's content-indexed view never shows them.
if ./venv/bin/python tests/observability/test_genai_span_content.py >/tmp/genai_content_check.txt 2>&1; then
  ok "gen_ai spans carry input/output content (Content column + quality/risk evals)"
else
  bad "gen_ai spans missing message content -> AI trace data stays empty (see /tmp/genai_content_check.txt)"
fi
CMC=$(grep '^OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=' .env 2>/dev/null | cut -d= -f2)
CMC_UP=$(printf '%s' "$CMC" | tr '[:lower:]' '[:upper:]')  # macOS ships bash 3.2 (no ${x^^})
case "$CMC_UP" in
  SPAN_ONLY|SPAN_AND_EVENT) ok "content capture enabled (.env=$CMC)" ;;
  *) bad ".env OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT='$CMC' -> content won't reach spans (use SPAN_ONLY)" ;;
esac

echo "== Tier 1: preconditions =="
lsof -nP -iTCP:4317 -sTCP:LISTEN >/dev/null 2>&1 \
  && ok "OTel collector listening on :4317" \
  || bad "OTel collector is DOWN -> start it:  ./run-collector.sh"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$CMETRICS" 2>/dev/null)" = "200" ] \
  && ok "collector internal telemetry (:8888) reachable" \
  || bad "collector :8888 unreachable"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$APP/health" 2>/dev/null)" = "200" ] \
  && ok "app healthy on :8001" \
  || bad "app NOT healthy on :8001 -> start it:  ./run.sh"
if [ $FAIL -gt 0 ]; then echo; echo "RESULT: preconditions failed ($FAIL)"; exit 1; fi

echo "== Tier 2: spans + metrics forwarded to Splunk (0 failures) =="
spans0=$(sum 'otelcol_exporter_sent_spans')
mets0=$(sum 'otelcol_exporter_sent_metric_points')
fail0=$(sum 'otelcol_exporter_send_failed_(spans|metric_points)')
for q in "regression test: my internet keeps dropping" "regression test: i have a headache"; do
  SID=$(curl -s -u "x:$KEY" -X POST "$APP/api/chat/session/new" | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])" 2>/dev/null)
  curl -s -u "x:$KEY" -X POST "$APP/api/chat/message" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\",\"message\":\"$q\",\"disclaimer_accepted\":true}" -o /dev/null
done
echo "  sent 2 test transactions; waiting ~75s for span + metric flush..."
sleep 75
spans1=$(sum 'otelcol_exporter_sent_spans')
mets1=$(sum 'otelcol_exporter_sent_metric_points')
fail1=$(sum 'otelcol_exporter_send_failed_(spans|metric_points)')
[ "$spans1" -gt "$spans0" ] && ok "spans forwarded to Splunk ($spans0 -> $spans1)" || bad "no new spans forwarded ($spans0 -> $spans1)"
[ "$mets1"  -gt "$mets0"  ] && ok "metric points forwarded ($mets0 -> $mets1)"     || bad "no new metric points ($mets0 -> $mets1)"
[ "$fail1"  -le "$fail0"  ] && ok "no new export failures (failed total=$fail1)"   || bad "export failures increased ($fail0 -> $fail1) -> check SPLUNK_REALM/SPLUNK_ACCESS_TOKEN/network"

echo "== Tier 3: GenAI metadata in Observability Cloud (model + tokens + agent) =="
APITOK=$(grep '^SPLUNK_API_TOKEN=' .env 2>/dev/null | cut -d= -f2)
REALM=$(grep '^SPLUNK_REALM=' .env 2>/dev/null | cut -d= -f2)
if [ -z "${APITOK:-}" ]; then
  echo "  SKIP  SPLUNK_API_TOKEN not set in .env (an O11y *API* token) -> skipping end-to-end metadata assertion"
else
  if python3 tests/observability/check_o11y_metadata.py "$REALM" "$APITOK" demobot-v3 demobot-local; then
    ok "gen_ai metadata present in O11y (real model + input/output tokens; named agent in AI agents view; operation.duration)"
  else
    bad "gen_ai metadata missing/incomplete in O11y (token usage, real model, or named agent not found)"
  fi
fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && exit 0 || exit 1
