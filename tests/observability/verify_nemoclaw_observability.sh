#!/bin/bash
# Regression test for the NemoClaw Guardrails surface: the policy layer inside
# /api/toolguard/inspect, the runtime denial feed (OCSF + after_tool_call), and
# — when the NemoClaw sandbox is up on this host — the real sandbox's tool
# calls reaching DemoBot's governance seat. Run after changes to
# backend/services/nemoclaw_guard.py, backend/routers/toolguard.py,
# guardrails/nemoclaw/policy.yaml, nemoclaw/, run-nemoclaw.sh or the plugin.
#
#   Tier 0  code-level: policy + Dockerfile + forwarder sanity, the unit suites
#   Tier 1  live app: the policy layer blocks an unlisted egress when the
#           drawer toggle is ON, and a forwarded OCSF denial becomes a
#           nemoclaw_guardrails governance event + execute_tool span
#   Tier 2  live sandbox (SKIPs when NemoClaw is not running here): the
#           sandboxed agent's tool call reaches /api/toolguard/inspect with
#           agent_surface=nemoclaw
#
# Exit 0 = pass, non-zero = fail. Tier 1 flips the NemoClaw toggle and restores it.
set -u
cd "$(dirname "$0")/../.." || exit 2

PASS=0; FAIL=0; SKIP=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $1"; SKIP=$((SKIP+1)); }
APP=http://127.0.0.1:8001
KEY=$(grep '^ACCESS_KEY=' .env 2>/dev/null | cut -d= -f2-)
PY=./venv/bin/python
SANDBOX="${NEMOCLAW_SANDBOX_NAME:-demobot-nemoclaw}"

echo "== Tier 0: code-level =="
$PY -c "import yaml,sys; p=yaml.safe_load(open('guardrails/nemoclaw/policy.yaml')); assert p['network_policies'] and p['inference']['local_only']" 2>/dev/null \
  && ok "guardrails/nemoclaw/policy.yaml parses (network_policies + local-only inference)" \
  || bad "guardrails/nemoclaw/policy.yaml invalid"
grep -q "openclaw plugins install /opt/demobot-toolguard" nemoclaw/Dockerfile \
  && ok "nemoclaw/Dockerfile bakes the demobot-toolguard plugin (NemoClaw's custom-image recipe)" \
  || bad "nemoclaw/Dockerfile does not install the plugin"
grep -q "__DEMOBOT_HOST__" nemoclaw/policies/demobot-guard.yaml \
  && ok "demobot-guard policy preset uses a real host IP placeholder (not host.docker.internal)" \
  || bad "demobot-guard preset missing the host placeholder"
$PY -m py_compile scripts/nemoclaw/ocsf_forwarder.py && ok "ocsf_forwarder.py compiles" || bad "ocsf_forwarder.py does not compile"
bash -n run-nemoclaw.sh && ok "run-nemoclaw.sh syntax" || bad "run-nemoclaw.sh syntax error"
$PY tests/test_nemoclaw_guard.py >/tmp/nemoclaw_unit.txt 2>&1 && ok "tests/test_nemoclaw_guard.py" || bad "tests/test_nemoclaw_guard.py FAILED (see /tmp/nemoclaw_unit.txt)"
# The plugin lives in git; its selftest covers the after_tool_call observer.
if command -v node >/dev/null; then
  T=$(mktemp -d); git archive HEAD:openclaw/plugins/demobot-toolguard | tar -x -C "$T"
  (cd "$T" && node selftest.mjs >/tmp/nemoclaw_selftest.txt 2>&1) \
    && ok "plugin selftest (before_tool_call gate + after_tool_call observer)" \
    || bad "plugin selftest FAILED (see /tmp/nemoclaw_selftest.txt)"
  rm -rf "$T"
else
  skip "plugin selftest (node not installed)"
fi

echo "== Tier 1: live app — policy layer + runtime denial feed =="
if [ "$(curl -s -o /dev/null -w '%{http_code}' "$APP/health" 2>/dev/null)" != "200" ]; then
  skip "app not up on :8001 (start it: ./run.sh) — Tiers 1-2 skipped"
  echo; echo "RESULT: $PASS passed, $FAIL failed, $SKIP skipped"; [ $FAIL -eq 0 ] && exit 0 || exit 1
fi
SAVED=$(curl -s -u "x:$KEY" "$APP/api/toolguard/nemoclaw" | $PY -c "import sys,json; print(json.load(sys.stdin).get('enabled'))" 2>/dev/null)
curl -s -u "x:$KEY" -X PUT "$APP/api/toolguard/nemoclaw" -H 'Content-Type: application/json' -d '{"enabled": true}' -o /dev/null
RESP=$(curl -s -u "x:$KEY" -X POST "$APP/api/toolguard/inspect" -H 'Content-Type: application/json' \
  -d '{"tool_name":"web_fetch","arguments":{"url":"https://evil.example.com/collect"},"session_id":"verify-nemoclaw","agent_surface":"nemoclaw"}')
echo "$RESP" | grep -q '"decision":"block"' && echo "$RESP" | grep -q 'nemoclaw' \
  && ok "NemoClaw ON: unlisted egress blocked and enforced by the policy layer" \
  || bad "NemoClaw ON did not block the egress (got: ${RESP:0:160})"
EV=$(curl -s -u "x:$KEY" -X POST "$APP/api/toolguard/nemoclaw/events" -H 'Content-Type: application/json' -d '{"records":[{"class_uid":4001,"action":"Denied","disposition":"Blocked","status_detail":"no matching policy","dst_endpoint":{"domain":"evil.example.com","port":443},"actor":{"process":{"name":"/usr/bin/curl"}},"firewall_rule":{"name":"baseline"}}],"session_id":"verify-nemoclaw"}')
echo "$EV" | grep -q '"denials_logged":1' && ok "forwarded OCSF denial logged as a runtime denial" || bad "OCSF denial not logged (got: ${EV:0:120})"
sleep 1
tail -n 200 logs/ai_governance.json 2>/dev/null | grep -q '"nemoclaw_guardrails"' \
  && ok "governance log carries guardrail_ids nemoclaw_guardrails" \
  || bad "no nemoclaw_guardrails governance event in logs/ai_governance.json"
curl -s -u "x:$KEY" -X PUT "$APP/api/toolguard/nemoclaw" -H 'Content-Type: application/json' -d "{\"enabled\": $( [ "$SAVED" = "True" ] && echo true || echo false )}" -o /dev/null

echo "== Tier 2: live NemoClaw sandbox =="
if ! command -v nemoclaw >/dev/null || ! nemoclaw "$SANDBOX" status >/dev/null 2>&1; then
  skip "NemoClaw sandbox '$SANDBOX' not running on this host (./run-nemoclaw.sh on a Docker/Colima host)"
else
  ok "sandbox $SANDBOX answers status"
  # Every exec below is bounded: an exec into a sandbox can hang indefinitely
  # (seen 2026-09-02: 2 h 51 min inside one Tier 2 check), and a hung verify
  # blocks everything queued behind it.
  if timeout 60 openshell sandbox exec --name "$SANDBOX" -- sh -c 'grep -q demobot-toolguard /sandbox/.openclaw/openclaw.json' 2>/dev/null; then
    ok "demobot-toolguard is enabled inside the sandbox"
  else
    bad "demobot-toolguard not enabled inside the sandbox"
  fi
  # A guard call from inside the sandbox proves the egress policy for the seat.
  # Use the guard URL the plugin was configured with (run-nemoclaw.sh --host):
  # inside the sandbox 127.0.0.1 is the container, not this host.
  GUARD_URL=$(timeout 60 openshell sandbox exec --name "$SANDBOX" -- sh -c 'sed -n "s/.*\"guardUrl\": *\"\([^\"]*\)\".*/\1/p" /sandbox/.openclaw/openclaw.json | head -1' 2>/dev/null | tr -d '[:space:]')
  GUARD_URL=${GUARD_URL:-http://127.0.0.1:8001}
  echo "  sandbox guard URL: $GUARD_URL"
  if timeout 60 openshell sandbox exec --name "$SANDBOX" -- sh -c "curl -s --max-time 5 -o /dev/null -w '%{http_code}' -u x:$KEY -X POST $GUARD_URL/api/toolguard/inspect -H 'Content-Type: application/json' -d '{\"tool_name\":\"read\",\"arguments\":{\"path\":\"/sandbox/.openclaw/workspace/x\"},\"agent_surface\":\"nemoclaw\"}'" 2>/dev/null | grep -q 200; then
    ok "sandbox can reach DemoBot's guard endpoint (demobot-guard policy applied)"
  else
    bad "sandbox cannot reach the guard endpoint — check run-nemoclaw.sh --host and the policy preset"
  fi
fi

echo; echo "RESULT: $PASS passed, $FAIL failed, $SKIP skipped"
[ $FAIL -eq 0 ] && exit 0 || exit 1
