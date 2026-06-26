#!/bin/bash
# Set up a PERMANENT (named) Cloudflare tunnel for DemoBot.
#
# Prerequisite: a domain whose DNS is on Cloudflare (e.g. registered via
# Cloudflare Registrar at dash.cloudflare.com -> Domain Registration). The
# free trycloudflare.com quick tunnels (tunnel.sh) need none of this.
#
# Usage:  ./setup-named-tunnel.sh <hostname> [tunnel-name]
#   ./setup-named-tunnel.sh medadvice.example.com            # tunnel name defaults to "medadvice"
#   ./setup-named-tunnel.sh app.example.com  medadvice
#
# Idempotent: re-running reuses an existing login/tunnel and just rewrites config.
set -euo pipefail

HOSTNAME="${1:?Usage: ./setup-named-tunnel.sh <hostname> [tunnel-name]}"
TUNNEL_NAME="${2:-medadvice}"
PORT="${PORT:-8001}"
CFDIR="$HOME/.cloudflared"

# 1. Origin cert (browser login). cloudflared prints a URL and waits; open it,
#    authorize, and pick the zone for your domain. Writes ~/.cloudflared/cert.pem.
if [ ! -f "$CFDIR/cert.pem" ]; then
  echo "==> No cert.pem found. Launching 'cloudflared tunnel login'."
  echo "    Open the URL it prints, authorize, and select your domain's zone."
  cloudflared tunnel login
else
  echo "==> Reusing existing origin cert ($CFDIR/cert.pem)."
fi

# 2. Create the named tunnel (skip if it already exists).
if cloudflared tunnel list --output json 2>/dev/null | grep -q "\"name\":\"${TUNNEL_NAME}\""; then
  echo "==> Tunnel '${TUNNEL_NAME}' already exists; reusing."
else
  echo "==> Creating tunnel '${TUNNEL_NAME}'..."
  cloudflared tunnel create "$TUNNEL_NAME"
fi

# 3. Resolve the tunnel UUID + its credentials file.
TUNNEL_ID="$(cloudflared tunnel list --output json \
  | python3 -c "import sys,json; print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='${TUNNEL_NAME}'))")"
CRED_FILE="$CFDIR/${TUNNEL_ID}.json"
echo "==> Tunnel ID: ${TUNNEL_ID}"

# 4. Point the hostname's DNS at the tunnel (proxied CNAME -> <id>.cfargotunnel.com).
echo "==> Routing ${HOSTNAME} -> tunnel '${TUNNEL_NAME}'..."
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME"

# 5. Write the tunnel config. protocol: http2 because this network blocks
#    outbound QUIC (UDP 7844) — same reason tunnel.sh forces http2.
cat > "$CFDIR/config.yml" <<EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${CRED_FILE}
protocol: http2

ingress:
  - hostname: ${HOSTNAME}
    service: http://localhost:${PORT}
  - service: http_status:404
EOF
echo "==> Wrote $CFDIR/config.yml"

echo
echo "Done. Start the permanent tunnel with:"
echo "    ./tunnel-named.sh ${TUNNEL_NAME}"
echo "Your stable URL: https://${HOSTNAME}"
