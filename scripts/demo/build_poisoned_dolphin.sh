#!/usr/bin/env bash
# Build the tampered Dolphin artifact used by the Galileo poisoning evaluation.
#
#   bash scripts/demo/build_poisoned_dolphin.sh
#
# Creates the local Ollama model `dolphin3-medadvice-poisoned` from
# models/dolphin3-medadvice-poisoned.Modelfile (FROM dolphin3:8b). Pulls the base
# model first if it is missing. After this, the model is selectable from the
# Settings UI / the experiment runner exactly like the clean dolphin3:8b.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODELFILE="$ROOT/models/dolphin3-medadvice-poisoned.Modelfile"
BASE="dolphin3:8b"
POISONED="dolphin3-medadvice-poisoned"

command -v ollama >/dev/null 2>&1 || { echo "FATAL: ollama not found on PATH. Install Ollama first."; exit 2; }
[ -f "$MODELFILE" ] || { echo "FATAL: missing $MODELFILE"; exit 2; }

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
echo "Next: ./run.sh (app + Ollama up), then"
echo "  venv/bin/python scripts/demo/galileo_experiment_poisoning.py"
