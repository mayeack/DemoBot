#!/bin/bash
# Install DemoBot's launchd user agents, with every path auto-filled for THIS
# checkout — so it works no matter where you cloned the repo. Idempotent:
# safe to re-run, and you MUST re-run it after moving/renaming the repo (a venv
# and these agents both bake in absolute paths that a move invalidates).
#
#   ./deploy/launchd/install.sh            # install app + collector agents
#                                          # (+ tunnel, only if ~/.cloudflared/config.yml exists)
#
# The plists in this directory are TEMPLATES containing __DEMOBOT_DIR__ /
# __HOME__ / __CLOUDFLARED__ placeholders; this script substitutes real values
# and writes the result to ~/Library/LaunchAgents/.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"
CLOUDFLARED="$(command -v cloudflared || true)"
mkdir -p "$DEST" "$REPO/logs"

# app + collector are portable; the tunnel is site-specific (needs your own
# named tunnel + domain, set up via ../../setup-named-tunnel.sh) so only install
# it when its config is present.
services=(collector app)
if [ -f "$HOME/.cloudflared/config.yml" ]; then
  [ -n "$CLOUDFLARED" ] || { echo "warning: cloudflared not found on PATH; skipping tunnel"; }
  [ -n "$CLOUDFLARED" ] && services+=(tunnel)
fi

for svc in "${services[@]}"; do
  src="$REPO/deploy/launchd/com.yeack.medadvice-$svc.plist"
  dst="$DEST/com.yeack.medadvice-$svc.plist"
  sed -e "s|__DEMOBOT_DIR__|$REPO|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__CLOUDFLARED__|${CLOUDFLARED:-/opt/homebrew/bin/cloudflared}|g" \
      "$src" > "$dst"
  # Unload any existing copy first. bootout is asynchronous, so bootstrap can
  # briefly fail with "5: Input/output error" until the old job finishes
  # unloading — wait it out and retry rather than aborting.
  launchctl bootout "gui/$UID_N/com.yeack.medadvice-$svc" 2>/dev/null || true
  ok=""
  for _ in 1 2 3 4 5 6 7 8; do
    if launchctl bootstrap "gui/$UID_N" "$dst" 2>/dev/null; then ok=1; break; fi
    sleep 1
  done
  if [ -n "$ok" ]; then
    echo "  installed + bootstrapped  com.yeack.medadvice-$svc"
  else
    echo "  FAILED to bootstrap com.yeack.medadvice-$svc (try: launchctl bootstrap gui/$UID_N $dst)" >&2
  fi
done

echo
echo "Done. The app + collector now auto-start at login and restart on crash."
echo "Check status:   launchctl print gui/$UID_N/com.yeack.medadvice-app | grep state"
echo "Verify serving: curl -s -o /dev/null -w '%{http_code}\\n' http://localhost:8001/health   # want 200"
