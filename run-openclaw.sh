#!/bin/bash
# Run the OpenClaw gateway (the agentic-risk demo surface) in podman, wired to:
#   - local Ollama (llama3.2:3b) as the agent model
#   - DemoBot's /api/toolguard/inspect as the before_tool_call governance seat
#   - the local OTel collector (:4318) via the diagnostics-otel plugin
#
# Podman, not host npm: Cisco Secure Endpoint quarantines OpenClaw installed on
# the host, and deletes OpenClaw files left in the working tree. So openclaw/ is
# tracked in git but sparse-checkout-excluded from this Mac — nothing on disk
# for AMP to delete. The image (recipe AND plugin) is built straight from a
# `git archive HEAD:openclaw` stream, and rebuilt whenever that tree's hash
# changes. Consequence: a plugin edit reaches the gateway once COMMITTED, not
# once staged. Read/edit openclaw/ with scripts/openclaw-edit.sh.
# See .claude/napkin.md.
#
# Run alongside ./run.sh (app) and ./run-collector.sh (collector).
# Stop:  podman stop demobot-openclaw
# Logs:  podman logs -f demobot-openclaw
set -euo pipefail
cd "$(dirname "$0")"

# ---------- options ----------
# --no-telemetry: run without exporting gateway spans/metrics/logs to the
#   collector (Splunk + Galileo). Use while iterating on the plugin so dev
#   noise doesn't land in dashboards you're about to present from.
# --foreground:   run the gateway attached (podman run without -d) so a
#   process supervisor (launchd KeepAlive) can own its lifecycle. Default is
#   detached/on-demand.
OTEL_ENABLED=true
FOREGROUND=false
for arg in "$@"; do
  case "$arg" in
    --no-telemetry) OTEL_ENABLED=false ;;
    --foreground)   FOREGROUND=true ;;
    -h|--help)
      echo "Usage: run-openclaw.sh [--no-telemetry] [--foreground]"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

IMAGE="demobot-openclaw"
CONTAINER="demobot-openclaw"
GATEWAY_PORT=18789
STATE_DIR="$HOME/.demobot-openclaw"          # gateway config+state (persisted)
DECOY_DIR="$HOME/DemoBotDecoy"               # agent workspace (decoy data only)
AGENT_MODEL="ollama/llama3.2:3b"             # tool-capable; dolphin3 has no tools template

ACCESS_KEY=$(grep '^ACCESS_KEY=' .env 2>/dev/null | cut -d= -f2- || true)

# ---------- preflights ----------
command -v podman >/dev/null || { echo "ERROR: podman not found." >&2; exit 1; }
podman info >/dev/null 2>&1 || { echo "ERROR: podman machine not running (podman machine start)." >&2; exit 1; }

# openclaw/ is not on disk (sparse-excluded), so git IS the source: `git archive`
# streams the tree — Containerfile at the top, plugins/ beneath it — to podman as
# the build context, no temp files. The tree hash is stamped on the image as a
# label, so a committed change to the recipe or the plugin triggers a rebuild by
# itself; an unchanged tree reuses the image and this is a no-op.
OPENCLAW_TREE=$(git rev-parse HEAD:openclaw)
IMAGE_TREE=$(podman image inspect "$IMAGE" --format '{{index .Labels "demobot.openclaw.tree"}}' 2>/dev/null || true)
if [ "$IMAGE_TREE" != "$OPENCLAW_TREE" ]; then
  if [ -n "$IMAGE_TREE" ]; then
    echo "openclaw/ changed in git (${IMAGE_TREE:0:12} -> ${OPENCLAW_TREE:0:12}) — rebuilding $IMAGE..."
  else
    echo "Building $IMAGE from the tracked recipe in git (tree ${OPENCLAW_TREE:0:12})..."
    echo "  (first build installs openclaw via npm — expect a few minutes)"
  fi
  # No -f: with the context on stdin, podman resolves an explicit -f against the
  # HOST cwd, which here is the repo root — and that Containerfile is the app's
  # python image. Left implicit, it reads Containerfile from the extracted
  # context, which is exactly openclaw/Containerfile.
  if ! git archive "HEAD:openclaw" \
       | podman build -t "$IMAGE" \
           --label "demobot.openclaw.tree=$OPENCLAW_TREE" -; then
    echo "" >&2
    echo "Build failed. Recipe source: git archive HEAD:openclaw" >&2
    echo "  Inspect it with: scripts/openclaw-edit.sh --show openclaw/Containerfile" >&2
    # The build needs the network (apt + npm). On a captive portal or offline it
    # fails through no fault of the recipe, so fall back to whatever image is
    # already in the podman VM rather than losing the demo entirely — but ONLY
    # if that image actually carries the plugin. An image predating the baked-in
    # plugin would start a gateway with NO governance seat, which looks like a
    # working demo while silently letting every tool call through. Never that.
    if podman image exists "$IMAGE" \
       && podman run --rm "$IMAGE" test -f /opt/demobot-plugins/demobot-toolguard/index.js 2>/dev/null; then
      echo "  Falling back to the existing $IMAGE (tree ${IMAGE_TREE:-unknown})." >&2
      echo "  It has the plugin baked in, so the guard still works — but it is STALE" >&2
      echo "  vs HEAD (${OPENCLAW_TREE:0:12}). Rerun with the network up to refresh." >&2
    else
      echo "  No usable image to fall back on: the existing $IMAGE predates the" >&2
      echo "  baked-in plugin, so starting it would give you a gateway with NO" >&2
      echo "  tool guard. Refusing to start. Get network access and rerun." >&2
      exit 1
    fi
  fi
fi

if ! curl -s --max-time 3 http://localhost:11434/api/tags | grep -q '"llama3.2:3b"'; then
  echo "ERROR: Ollama is not serving llama3.2:3b on :11434 (ollama pull llama3.2:3b)." >&2
  exit 1
fi
if ! curl -s --max-time 3 http://localhost:8001/health >/dev/null; then
  echo "WARN: DemoBot app is not up on :8001 — the tool guard is FAIL-CLOSED," >&2
  echo "      so every agent tool call will be denied until ./run.sh is running." >&2
fi
if ! curl -s --max-time 3 http://localhost:4318 >/dev/null 2>&1; then
  echo "WARN: OTel collector not detected on :4318 — gateway telemetry will not" >&2
  echo "      reach Splunk/Galileo until ./run-collector.sh is running." >&2
fi

mkdir -p "$STATE_DIR" "$DECOY_DIR"
# Earlier versions staged a host copy of the plugin here to bind-mount it. It is
# baked into the image now; leaving the copy behind would keep OpenClaw .js/.mjs
# on the disk AMP watches, which is the whole point of the change. Targeted at
# our plugin only — the gateway keeps its own installed plugins under $STATE_DIR.
rm -rf "$STATE_DIR/plugins/demobot-toolguard"

if [ ! -e "$DECOY_DIR/inbox" ]; then
  echo "NOTE: decoy workspace $DECOY_DIR is unseeded — run:" >&2
  echo "      venv/bin/python scripts/demo/seed_agentic_decoy.py" >&2
fi

# ---------- gateway auth token (generated once, persisted) ----------
TOKEN_FILE="$STATE_DIR/gateway-token"
if [ ! -s "$TOKEN_FILE" ]; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$TOKEN_FILE"
fi
GATEWAY_TOKEN=$(cat "$TOKEN_FILE")

# ---------- gateway config (regenerated each start; state dir persists the rest) ----------
STATE_DIR="$STATE_DIR" ACCESS_KEY="$ACCESS_KEY" GATEWAY_TOKEN="$GATEWAY_TOKEN" \
AGENT_MODEL="$AGENT_MODEL" OTEL_ENABLED="$OTEL_ENABLED" python3 - <<'PYCONF'
import json, os
otel_on = os.environ.get("OTEL_ENABLED", "true") == "true"
cfg = {
    "gateway": {
        # "lan" bind: inside the container the default loopback bind is
        # unreachable through the published port. The host publish is
        # 127.0.0.1-only, so actual exposure stays host-local.
        "bind": "lan",
        "auth": {"mode": "token", "token": os.environ["GATEWAY_TOKEN"]},
    },
    "agents": {
        "defaults": {
            "model": {"primary": os.environ["AGENT_MODEL"]},
            "workspace": "/home/node/.openclaw/workspace",
        }
    },
    "models": {
        "providers": {
            "ollama": {
                "baseUrl": "http://host.containers.internal:11434",
                "apiKey": "ollama-local",
            }
        }
    },
    # Demo tool surface: fs/runtime/web groups (read/write/exec/web_fetch),
    # with the riskiest extras denied outright. The governance seat is the
    # before_tool_call guard; this just trims what the model can even see.
    "tools": {
        "profile": "coding",
        "deny": ["browser", "canvas", "nodes", "cron", "gateway"],
    },
    "plugins": {
        "allow": ["diagnostics-otel", "demobot-toolguard"],
        "load": {"paths": ["/opt/demobot-plugins/demobot-toolguard"]},
        "entries": {
            "diagnostics-otel": {"enabled": True},
            "demobot-toolguard": {
                "enabled": True,
                "config": {
                    "guardUrl": "http://host.containers.internal:8001",
                    "accessKey": os.environ.get("ACCESS_KEY", ""),
                    "failOpen": False,
                    "timeoutMs": 8000,
                },
            },
        },
    },
    "diagnostics": {
        "enabled": True,
        "otel": {
            "enabled": otel_on,   # --no-telemetry sets this False
            "endpoint": "http://host.containers.internal:4318",
            "protocol": "http/protobuf",   # grpc silently kills all export
            "serviceName": "openclaw-gateway",
            "traces": True,
            "metrics": True,
            "logs": True,
            "sampleRate": 1,               # docs example 0.2 would drop 80% of demo traces
            "flushIntervalMs": 5000,
            "captureContent": True,        # tool inputs/outputs on spans (demo needs content)
        },
    },
}
path = os.path.join(os.environ["STATE_DIR"], "openclaw.json")
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"wrote {path}")
PYCONF

# ---------- run ----------
echo "Starting OpenClaw gateway on http://127.0.0.1:${GATEWAY_PORT} (podman: $CONTAINER)"
# Always start DETACHED so the health-wait and one-time plugin bootstrap below
# can run. The launchd/foreground case blocks on `podman wait` at the end
# instead — that hands the supervisor a foreground process to KeepAlive while
# the container itself runs detached.
#
# --userns=keep-id is meant to let the container write the host-owned bind
# mounts. KNOWN BROKEN as written (2026-07-28, pre-existing): it maps the host
# user to the SAME uid in the container, but the image runs as `node` (uid
# 1000), so $STATE_DIR (host uid 501) is still not writable and the gateway
# dies on startup with EACCES creating /home/node/.openclaw/state. Neither
# keep-id nor keep-id:uid=1000,gid=1000 remaps it on macOS podman. The fix is a
# posture decision (run as container root, which rootless podman already maps
# to the unprivileged host user, vs. relaxing $STATE_DIR perms), so it is left
# for a separate change rather than silently altering the demo's sandboxing.
# --restart=on-failure:3 recovers a transient crash mid-demo but never
# resurrects after a clean `podman stop` or a machine reboot — so "off" stays
# the default state when you're not demoing.
podman run -d --replace --name "$CONTAINER" \
  --userns=keep-id \
  --restart=on-failure:3 \
  -p "127.0.0.1:${GATEWAY_PORT}:${GATEWAY_PORT}" \
  -e OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental \
  -e HOME=/home/node \
  -v "$STATE_DIR:/home/node/.openclaw" \
  -v "$DECOY_DIR:/home/node/.openclaw/workspace" \
  "$IMAGE" >/dev/null

GATEWAY_UP=false
for i in $(seq 1 30); do
  if curl -s --max-time 2 "http://127.0.0.1:${GATEWAY_PORT}/" >/dev/null 2>&1; then
    GATEWAY_UP=true; break
  fi
  sleep 1
done

# This loop used to fall through silently and the script printed "gateway up"
# regardless, so a container that died on startup still looked like a clean
# launch. Say what actually happened.
if [ "$GATEWAY_UP" != true ]; then
  echo "" >&2
  echo "ERROR: the gateway never answered on :${GATEWAY_PORT} within 30s." >&2
  echo "       Container state: $(podman inspect -f '{{.State.Status}} (exit {{.State.ExitCode}})' "$CONTAINER" 2>/dev/null || echo unknown)" >&2
  echo "       Last lines:" >&2
  podman logs --tail 8 "$CONTAINER" 2>&1 | sed 's/^/         /' >&2
  echo "       Full logs: podman logs $CONTAINER" >&2
  exit 1
fi

# diagnostics-otel is a ClawHub plugin (not bundled): install once into the
# persisted state dir.
if ! podman exec "$CONTAINER" openclaw plugins list 2>/dev/null | grep -q "diagnostics-otel"; then
  echo "Installing diagnostics-otel plugin (first run)..."
  podman exec "$CONTAINER" openclaw plugins install clawhub:@openclaw/diagnostics-otel \
    && podman restart "$CONTAINER" >/dev/null
fi

TELEMETRY_STATE=$([ "$OTEL_ENABLED" = true ] && echo "on -> :4318" || echo "OFF (--no-telemetry)")
echo ""
echo "OpenClaw gateway up:  http://127.0.0.1:${GATEWAY_PORT}"
echo "  Control UI token:   $GATEWAY_TOKEN"
echo "  Agent model:        $AGENT_MODEL   Workspace: $DECOY_DIR"
echo "  Tool guard:         http://host.containers.internal:8001/api/toolguard/inspect (fail-closed)"
echo "  Telemetry:          $TELEMETRY_STATE"
echo "  Logs:               podman logs -f $CONTAINER"

# Foreground/launchd supervision: block on the container so a KeepAlive plist
# has a process to watch. When the container stops, this returns and launchd
# restarts the unit.
if [ "$FOREGROUND" = true ]; then
  echo "  (foreground: waiting on container for process supervision)"
  exec podman wait "$CONTAINER" >/dev/null
fi
