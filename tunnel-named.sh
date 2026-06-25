#!/bin/bash
# Run the PERMANENT (named) Cloudflare tunnel. Configure it once with
# ./setup-named-tunnel.sh <hostname>; this just brings it up using the
# settings in ~/.cloudflared/config.yml (hostname, credentials, protocol).
#
# Unlike tunnel.sh (ephemeral trycloudflare.com), this serves your stable
# https://<hostname> every run. The app (./run.sh) must be up on :8001.
TUNNEL_NAME="${1:-medadvice}"
echo "Starting named Cloudflare tunnel '${TUNNEL_NAME}' -> http://localhost:${PORT:-8001}"
echo "Serving your stable hostname from ~/.cloudflared/config.yml. Ctrl+C to stop."
exec cloudflared tunnel run "$TUNNEL_NAME"
