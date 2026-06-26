#!/bin/bash
# Launch ALL DemoBot services together. "Launching the app" includes the OTel
# collector — without it the app runs but NO telemetry reaches Observability
# Cloud (the #1 incident). Pass --tunnel to also start the public Cloudflare tunnel.
#
#   ./start-all.sh            # collector + app
#   ./start-all.sh --tunnel   # collector + app + public tunnel
#
# Each service runs in the background; logs go to /tmp/medadvice_*.log.
cd "$(dirname "$0")" || exit 1

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

# 3) optional public tunnel
if [ "${1:-}" = "--tunnel" ]; then
  echo "  starting public tunnel (tunnel.sh) -> /tmp/medadvice_tunnel.log"
  nohup ./tunnel.sh >/tmp/medadvice_tunnel.log 2>&1 &
  echo "  -> the trycloudflare.com URL will print in /tmp/medadvice_tunnel.log"
fi

echo
echo "Done. Verify the full pipeline with:  ./tests/observability/verify_observability.sh"
echo "Stop everything with:  lsof -ti:8001 -ti:4317 | xargs kill ; pkill -f cloudflared"
