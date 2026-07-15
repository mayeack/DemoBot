#!/usr/bin/env bash
# Build the tampered Dolphin artifact used by the Galileo poisoning evaluation.
#
#   bash scripts/demo/build_poisoned_dolphin.sh
#
# Creates the local Ollama model `dolphin3:8b-poisoned` from
# models/dolphin3-8b-poisoned.Modelfile (FROM dolphin3:8b). Pulls the base
# model first if it is missing. After this, the model is selectable from the
# Settings UI / the experiment runner exactly like the clean dolphin3:8b.
#
# Refuses to run while the app is serving or a model is loaded: rebuilding a
# model against the live daemon mid-generation evicts loaded runners and stalls
# in-flight chat turns (observed in the 2026-07-15 latency incident). Override
# with --force if you know nothing is in flight.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODELFILE="$ROOT/models/dolphin3-8b-poisoned.Modelfile"
BASE="dolphin3:8b"
POISONED="dolphin3:8b-poisoned"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

command -v ollama >/dev/null 2>&1 || { echo "FATAL: ollama not found on PATH. Install Ollama first."; exit 2; }
[ -f "$MODELFILE" ] || { echo "FATAL: missing $MODELFILE"; exit 2; }

if [ "$FORCE" -ne 1 ]; then
  if curl -sf --max-time 2 http://localhost:8001/health >/dev/null 2>&1; then
    echo "FATAL: DemoBot is serving on :8001 — rebuilding models against the live"
    echo "daemon can stall in-flight chat turns and evict the resident model."
    echo "Stop the app (launchctl bootout gui/\$(id -u)/com.yeack.medadvice-app)"
    echo "or re-run with --force."
    exit 3
  fi
  if [ "$(curl -sf --max-time 2 http://localhost:11434/api/ps 2>/dev/null | grep -c '"name"' || true)" -gt 0 ]; then
    echo "FATAL: Ollama has a model loaded (a generation may be in flight)."
    echo "Wait for it to go idle ('ollama ps' empty) or re-run with --force."
    exit 3
  fi
fi

if ! ollama list | awk '{print $1}' | grep -qx "$BASE"; then
  echo "Base model $BASE not present — pulling it..."
  ollama pull "$BASE"
fi

echo "Building $POISONED from $MODELFILE ..."
ollama create "$POISONED" -f "$MODELFILE"

echo
echo "Done. Installed models:"
ollama list | grep -E "dolphin3" || true
echo
echo "PERFORMANCE TIP: the app runs the internal agents on OLLAMA_MODEL_INTERNAL and"
echo "only the synthesizer on the selected (possibly poisoned) model. To keep all"
echo "artifacts resident (instant model-switching, no idle reloads) start the daemon with:"
echo "  OLLAMA_MAX_LOADED_MODELS=3 OLLAMA_KEEP_ALIVE=30m ollama serve"
echo
echo "Next: ./run.sh (app + Ollama up), then"
echo "  venv/bin/python scripts/demo/galileo_experiment_poisoning.py"
