#!/bin/bash
# Run the NVIDIA NemoClaw runtime — OpenClaw inside an OpenShell sandbox — as the
# alternative Mode-C gateway for the agentic-risk demo, with DemoBot's
# governance seat baked in:
#   - the sandbox image is NemoClaw's pinned OpenClaw sandbox + the
#     demobot-toolguard plugin (nemoclaw/Dockerfile), so every agent tool call
#     is inspected by /api/toolguard/inspect before it runs (policy + NemoClaw
#     policy layer + Cisco AI Defense) and a sandbox refusal is reported after;
#   - a network-policy preset lets the sandbox reach ONLY DemoBot's guard
#     endpoint (nemoclaw/policies/demobot-guard.yaml) — everything else stays
#     deny-by-default, which is the demo;
#   - OpenShell's OCSF JSON audit log is enabled and tailed into
#     /api/toolguard/nemoclaw/events (scripts/nemoclaw/ocsf_forwarder.py), so the
#     runtime's own denials land in Splunk/Galileo as nemoclaw_guardrails events.
#
# Platform: NemoClaw supports Linux with Docker Engine (Ubuntu 24.04 primary;
# 22.04 unvalidated), macOS Apple Silicon with Docker Desktop or Colima, WSL2.
# Podman is NOT supported (its onboarding refuses it) — this Mac runs Mode C on
# podman, so the primary NemoClaw host is an EC2 GPU replica
# (deploy/ec2/ec2-bootstrap.sh --with-nemoclaw). The host-capability probe
# (backend/host_capabilities.py) says whether this box qualifies.
#
#   ./run-nemoclaw.sh                       # onboard (first run) / start, provider = NVIDIA endpoints
#   ./run-nemoclaw.sh --host 10.0.1.5       # DemoBot reachable at that private IP from the sandbox
#                                           # (default: this host's own private IP; never 127.0.0.1)
#   ./run-nemoclaw.sh --provider nim        # NemoClaw-managed local NIM (experimental; needs a GPU)
#   ./run-nemoclaw.sh --foreground          # keep the OCSF forwarder attached (systemd / launchd)
#
# Stop:  nemoclaw <sandbox> stop ; pkill -f ocsf_forwarder.py
# See CLAUDE.md / .claude/napkin.md for the EDR notes on this Mac.
set -euo pipefail
cd "$(dirname "$0")"
umask 077

SANDBOX="${NEMOCLAW_SANDBOX_NAME:-demobot-nemoclaw}"
PROVIDER="build"                 # build = NVIDIA endpoints (NVIDIA_INFERENCE_API_KEY) | nim = NemoClaw-managed local NIM
HOST=""                          # how the SANDBOX reaches DemoBot; default: this host's private IP
FOREGROUND=false
FORWARDER=true
# Pin NemoClaw to a reviewed commit (alpha project). Override with NEMOCLAW_INSTALL_REF.
INSTALL_REF="${NEMOCLAW_INSTALL_REF:-main}"
STATE_DIR="$HOME/.demobot-nemoclaw"
POLICY_TIER="${NEMOCLAW_POLICY_TIER:-restricted}"

for arg in "$@"; do
  case "$arg" in
    --host=*)      HOST="${arg#*=}" ;;
    --provider=*)  PROVIDER="${arg#*=}" ;;
    --sandbox=*)   SANDBOX="${arg#*=}" ;;
    --foreground)  FOREGROUND=true ;;
    --no-forwarder) FORWARDER=false ;;
    -h|--help)     sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --host|--provider|--sandbox) echo "use $arg=<value>" >&2; exit 2 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done
case "$PROVIDER" in build|nim) ;; *) echo "--provider must be build|nim" >&2; exit 2 ;; esac

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

ACCESS_KEY=$(grep '^ACCESS_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
mkdir -p "$STATE_DIR"

# ---------- host capability gate (the same rules the UI greys out with) ----------
log "host capability check"
if [ -x venv/bin/python ]; then
  if ! REASON=$(venv/bin/python - <<'PY'
from backend import host_capabilities as hc
d = hc.detect(force=True)
g = d["gated"]["nemoclaw_runtime"]
print(g["reason"] if not g["enabled"] else "")
raise SystemExit(0 if g["enabled"] else 1)
PY
  ); then die "this host cannot run the NemoClaw runtime: $REASON"; fi
  venv/bin/python -m backend.host_capabilities
fi
docker info >/dev/null 2>&1 || die "docker is not answering (NemoClaw needs Docker Engine / Docker Desktop / Colima; Podman is unsupported)"
command -v node >/dev/null || die "node >= 22.19 is required"

# ---------- DemoBot reachability ----------
if [ -z "$HOST" ]; then
  # The sandbox must reach DemoBot at THIS HOST'S PRIVATE IP. NemoClaw's
  # validator for user-supplied presets refuses loopback (inside the sandbox
  # 127.0.0.1 is the sandbox — every guard call refused, seen on EC2
  # 2026-09-02), refuses its managed aliases (host.openshell.internal,
  # host.docker.internal) and refuses allowed_ips in the file; a private IP
  # literal is accepted only with --trusted-private-host, which pins it.
  HOST=$(hostname -I 2>/dev/null | awk '{print $1}')                 # Linux
  [ -n "$HOST" ] || HOST=$(ipconfig getifaddr en0 2>/dev/null || true) # macOS
  [ -n "$HOST" ] || die "could not determine this host's private IP — pass --host=<ip>"
fi
GUARD="http://$HOST:8001"            # what the sandbox's plugin calls
LOCAL_GUARD="http://127.0.0.1:8001"  # the same app, seen from this host
curl -s --max-time 3 "$LOCAL_GUARD/health" >/dev/null \
  || warn "DemoBot is not answering at $LOCAL_GUARD — the tool guard is FAIL-CLOSED, so every sandbox tool call is denied until ./run.sh is up"

# ---------- NemoClaw CLI ----------
if ! command -v nemoclaw >/dev/null; then
  log "installing NemoClaw (ref $INSTALL_REF)"
  [ "$PROVIDER" = build ] && [ -z "${NVIDIA_INFERENCE_API_KEY:-}" ] \
    && die "NVIDIA_INFERENCE_API_KEY (nvapi-…) is required for --provider build (the sandbox's inference; DemoBot's own provider stays local)"
  curl -fsSL "https://raw.githubusercontent.com/NVIDIA/NemoClaw/${INSTALL_REF}/install.sh" | \
    NEMOCLAW_INSTALL_REF="$INSTALL_REF" \
    NEMOCLAW_NON_INTERACTIVE=1 \
    NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 \
    NEMOCLAW_AGENT=openclaw \
    NEMOCLAW_PROVIDER="$PROVIDER" \
    NEMOCLAW_SANDBOX_NAME="$SANDBOX" \
    NEMOCLAW_POLICY_TIER="$POLICY_TIER" \
    NEMOCLAW_WEB_SEARCH_PROVIDER=none \
    NEMOCLAW_SKIP_ONBOARD=1 \
    bash
  command -v nemoclaw >/dev/null || die "nemoclaw CLI not on PATH after install (open a new shell, or check ~/.nemoclaw)"
fi

# OCSF JSON audit export must be on BEFORE the sandbox is created (it is a
# gateway/sandbox setting read at start).
openshell settings set --global --key ocsf_json_enabled --value true >/dev/null 2>&1 \
  || warn "could not enable OpenShell OCSF JSON export; runtime denials will only be attributed via after_tool_call"

# ---------- sandbox image with the governance seat ----------
# The plugin lives in git (openclaw/ is sparse-excluded on the Mac): materialise
# it into the Dockerfile's build context only for the duration of the build.
CTX=nemoclaw/plugins/demobot-toolguard
cleanup() { rm -rf nemoclaw/plugins; }
trap cleanup EXIT
mkdir -p "$CTX"
git archive HEAD:openclaw/plugins/demobot-toolguard | tar -x -C "$CTX"
[ -f "$CTX/index.js" ] || die "plugin not found in git (HEAD:openclaw/plugins/demobot-toolguard)"

if nemoclaw list 2>/dev/null | grep -q "^$SANDBOX\b\|[[:space:]]$SANDBOX[[:space:]]"; then
  log "sandbox $SANDBOX exists — starting it"
  nemoclaw "$SANDBOX" start >/dev/null 2>&1 || true
else
  log "onboarding sandbox $SANDBOX from nemoclaw/Dockerfile (provider: $PROVIDER)"
  env NEMOCLAW_NON_INTERACTIVE=1 NEMOCLAW_AGENT=openclaw NEMOCLAW_PROVIDER="$PROVIDER" \
      NEMOCLAW_POLICY_TIER="$POLICY_TIER" NEMOCLAW_WEB_SEARCH_PROVIDER=none \
      $( [ "$PROVIDER" = nim ] && echo NEMOCLAW_EXPERIMENTAL=1 ) \
      nemoclaw onboard --non-interactive --agent openclaw --name "$SANDBOX" --from nemoclaw/Dockerfile
fi

# ---------- plugin config (guard URL + access key), never baked into an image layer ----------
log "configuring the governance seat inside the sandbox"
# A (re)started sandbox sits in phase Provisioning for a minute or more and
# refuses exec meanwhile ("Connection refused"); the config write then silently
# kept the image's loopback URL. Wait for phase Ready (up to 5 min), then exec.
for _ in $(seq 1 60); do
  openshell sandbox list 2>/dev/null | grep -E "^$SANDBOX[[:space:]]" | grep -q "Ready" \
    && openshell sandbox exec --name "$SANDBOX" -- true >/dev/null 2>&1 && break
  sleep 5
done
openshell sandbox exec --name "$SANDBOX" -- env \
    DEMOBOT_GUARD_URL="$GUARD" DEMOBOT_ACCESS_KEY="$ACCESS_KEY" \
    node -e '
const fs = require("fs"); const p = "/sandbox/.openclaw/openclaw.json";
const c = JSON.parse(fs.readFileSync(p, "utf8"));
c.plugins = c.plugins || {}; c.plugins.entries = c.plugins.entries || {};
c.plugins.entries["demobot-toolguard"] = { enabled: true, config: {
  guardUrl: process.env.DEMOBOT_GUARD_URL, accessKey: process.env.DEMOBOT_ACCESS_KEY,
  failOpen: false, timeoutMs: 8000, agentSurface: "nemoclaw" } };
fs.writeFileSync(p, JSON.stringify(c, null, 2));' \
  && openshell sandbox exec --name "$SANDBOX" -- sh -c 'sha256sum /sandbox/.openclaw/openclaw.json > /sandbox/.openclaw/.config-hash' \
  || warn "could not write the plugin config inside the sandbox — check the image built with the plugin"

# ---------- network policy: only DemoBot's guard endpoint ----------
log "applying the demobot-guard network policy (host $HOST)"
POLICY_TMP=$(mktemp "$STATE_DIR/demobot-guard.XXXXXX.yaml")
sed "s/__DEMOBOT_HOST__/$HOST/g" nemoclaw/policies/demobot-guard.yaml > "$POLICY_TMP"
nemoclaw "$SANDBOX" policy add --from-file "$POLICY_TMP" --trusted-private-host "$HOST" \
  || warn "policy add failed — the sandbox cannot reach the guard (fail-closed: every tool call denied)"
rm -f "$POLICY_TMP"
nemoclaw gateway restart >/dev/null 2>&1 || true

# ---------- OCSF denial forwarder ----------
if [ "$FORWARDER" = true ]; then
  pkill -f "ocsf_forwarder.py --sandbox $SANDBOX" 2>/dev/null || true
  FWD=(python3 scripts/nemoclaw/ocsf_forwarder.py --sandbox "$SANDBOX" --guard "$LOCAL_GUARD" --access-key "$ACCESS_KEY" --state "$STATE_DIR")
  [ -x venv/bin/python ] && FWD[0]=venv/bin/python
  if [ "$FOREGROUND" = true ]; then
    log "NemoClaw sandbox $SANDBOX up; forwarding OCSF denials (foreground)"
    exec "${FWD[@]}"
  fi
  nohup "${FWD[@]}" >"$STATE_DIR/forwarder.log" 2>&1 &
  echo "  OCSF forwarder started (log: $STATE_DIR/forwarder.log)"
fi

log "NemoClaw sandbox $SANDBOX is up"
nemoclaw "$SANDBOX" status 2>/dev/null | head -20 || true
echo "  dashboard:  nemoclaw $SANDBOX dashboard-url --quiet"
echo "  token:      nemoclaw $SANDBOX gateway-token --quiet"
echo "  terminal:   nemoclaw $SANDBOX connect"
echo "  stop:       nemoclaw $SANDBOX stop ; pkill -f ocsf_forwarder.py"
echo "  Turn the drawer's NemoClaw Guardrails toggle ON to enforce the policy layer; the pill reads RUNTIME once the sandbox reports a denial."
