#!/bin/bash
# Make the Ollama daemon env durable across reboots (macOS).
#
# Installs a one-shot LaunchAgent (com.yeack.ollama-env) that runs at every login
# and sets the env vars `ollama serve` reads:
#     OLLAMA_KEEP_ALIVE=30m         # keep the model resident between turns
#     OLLAMA_MAX_LOADED_MODELS=2    # allow two ~5GB artifacts to coexist
# so you don't have to launch `ollama serve` with them by hand each session.
#
# Belt-and-suspenders: the DemoBot poisoned model shares the dolphin3:8b weight
# blob (one runner serves both), and the app already sends a per-call keep_alive,
# so MAX_LOADED_MODELS=2 isn't strictly required for that pair — this is durability
# / hygiene, not a correctness fix.
#
# Usage:  ./setup-ollama-env.sh
# Idempotent: re-running just reinstalls + re-bootstraps the agent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.yeack.ollama-env.plist"
PLIST_SRC="$ROOT/deploy/launchd/$PLIST_NAME"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"
UID_N="$(id -u)"

[ -f "$PLIST_SRC" ] || { echo "FATAL: missing $PLIST_SRC"; exit 2; }

echo "==> Installing $PLIST_NAME -> $PLIST_DEST"
cp "$PLIST_SRC" "$PLIST_DEST"

echo "==> Bootstrapping the LaunchAgent (re-bootstrap if already loaded)"
launchctl bootout "gui/$UID_N" "$PLIST_DEST" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$PLIST_DEST"

echo "==> Setting the env for the CURRENT session too (so no re-login is needed)"
launchctl setenv OLLAMA_KEEP_ALIVE 30m
launchctl setenv OLLAMA_MAX_LOADED_MODELS 2

echo
echo "Done. Verify:"
echo "    launchctl getenv OLLAMA_KEEP_ALIVE        # -> 30m"
echo "    launchctl getenv OLLAMA_MAX_LOADED_MODELS # -> 2"
echo
echo "IMPORTANT: Ollama.app caches its environment at launch. If it is already"
echo "running, restart it once so it inherits these vars:"
echo "    osascript -e 'tell application \"Ollama\" to quit'; sleep 2; open -a Ollama"
echo
echo "Note: this is a one-shot agent — 'launchctl print gui/$UID_N/com.yeack.ollama-env'"
echo "will show it as exited after it sets the vars. That is expected, not a crash."
