#!/bin/bash
# Launch ALL DemoBot services together. "Launching the app" includes the OTel
# collector — without it the app runs but NO telemetry reaches Observability
# Cloud (the #1 incident). Pass --tunnel to also start the public Cloudflare
# tunnel, and --agentic to also start the OpenClaw gateway (agentic-risk demo).
#
#   ./start-all.sh                     # collector + app
#   ./start-all.sh --tunnel            # + public tunnel
#   ./start-all.sh --agentic           # + OpenClaw gateway (agentic surface)
#   ./start-all.sh --tunnel --agentic  # everything
#
# Each service runs in the background; logs go to /tmp/medadvice_*.log.
cd "$(dirname "$0")" || exit 1

WITH_TUNNEL=false
WITH_AGENTIC=false
for arg in "$@"; do
  case "$arg" in
    --tunnel)  WITH_TUNNEL=true ;;
    --agentic) WITH_AGENTIC=true ;;
    -h|--help) echo "Usage: start-all.sh [--tunnel] [--agentic]"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

start() {  # name  port  script  logfile
  local name=$1 port=$2 script=$3 log=$4
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  $name already running on :$port"
  else
    echo "  starting $name ($script) -> $log"
    nohup "./$script" >"$log" 2>&1 &
  fi
}

echo "Launching DemoBot services:"
# 1) OTel collector (telemetry -> Splunk Observability Cloud). MUST be first.
start "OTel collector" 4317 run-collector.sh /tmp/medadvice_collector.log
# 2) the app (launches under opentelemetry-instrument when OTLP is configured)
start "app"            8001 run.sh           /tmp/medadvice_app.log

# 3) optional OpenClaw gateway (agentic-risk demo surface). Off by default:
#    starting it IS the opt-in. run-openclaw.sh starts the podman container
#    detached and returns, so nohup just captures its startup output.
if [ "$WITH_AGENTIC" = true ]; then
  echo "  starting OpenClaw gateway (run-openclaw.sh) -> /tmp/medadvice_openclaw.log"
  nohup ./run-openclaw.sh >/tmp/medadvice_openclaw.log 2>&1 &
  echo "  -> gateway http://127.0.0.1:18789 ; details + token in that log"
fi

# 4) optional public tunnel
if [ "$WITH_TUNNEL" = true ]; then
  echo "  starting public tunnel (tunnel.sh) -> /tmp/medadvice_tunnel.log"
  nohup ./tunnel.sh >/tmp/medadvice_tunnel.log 2>&1 &
  echo "  -> the trycloudflare.com URL will print in /tmp/medadvice_tunnel.log"
fi

echo
echo "Done. Verify the full pipeline with:  ./tests/observability/verify_observability.sh"
STOP="lsof -ti:8001 -ti:4317 | xargs kill ; pkill -f cloudflared"
[ "$WITH_AGENTIC" = true ] && STOP="$STOP ; podman stop demobot-openclaw"
echo "Stop everything with:  $STOP"
