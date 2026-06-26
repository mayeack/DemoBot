#!/bin/bash
# Expose the locally running DemoBot server to the public internet.
# Requires cloudflared: brew install cloudflared
# Run ./run.sh in one terminal, then ./tunnel.sh in another.
#
# --protocol http2: this network blocks outbound QUIC (UDP 7844), so the default
# quic transport times out ("failed to dial to edge with quic"). HTTP/2 (TCP 443)
# works. Override with TUNNEL_PROTOCOL=quic if you're on a network that allows it.
PORT="${PORT:-8001}"
PROTO="${TUNNEL_PROTOCOL:-http2}"
echo "Starting Cloudflare quick tunnel (protocol=${PROTO}) -> http://localhost:${PORT}"
echo "Copy the printed https://<random>.trycloudflare.com URL into your browser."
exec cloudflared tunnel --protocol "${PROTO}" --url "http://localhost:${PORT}"
