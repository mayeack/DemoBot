#!/usr/bin/env python3
"""Regression: the Galileo integration (backend/galileo_integration.py + wiring).

Guards (1) the no-op safety guarantee — when the galileo package or GALILEO_API_KEY
is absent, emission is a silent no-op and never raises into a chat turn — and (2)
the collector + app wiring, so the integration can't regress into breaking requests
or losing its export path.

    venv/bin/python tests/test_galileo_integration.py    # exit 0 = pass
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


# ---- no-op safety guarantee (must never raise into a chat turn) ----
os.environ.pop("GALILEO_API_KEY", None)
import backend.galileo_integration as gi  # noqa: E402

check("disabled when GALILEO_API_KEY unset", gi.is_enabled() is False)
try:
    gi.maybe_log_turn({"operation_name": "chat", "token_type": "output",
                       "input_messages": [{"role": "user", "content": "hi"}]})
    check("maybe_log_turn is a silent no-op when disabled", True)
except Exception:
    check("maybe_log_turn is a silent no-op when disabled", False)

os.environ["GALILEO_API_KEY"] = "test-key-unused"
check("enabled when GALILEO_API_KEY set", gi.is_enabled() is True)
try:
    # non-chat event must be ignored even when enabled (no network call spawned)
    gi.maybe_log_turn({"operation_name": "prompt", "token_type": "prompt"})
    check("maybe_log_turn ignores non-chat events", True)
except Exception:
    check("maybe_log_turn ignores non-chat events", False)
os.environ.pop("GALILEO_API_KEY", None)

# ---- helpers ----
check("_coerce keeps scalars, stringifies non-scalars",
      gi._coerce(True) is True and gi._coerce(["a", "b"]) == "['a', 'b']")
check("_text flattens message lists + passes strings",
      gi._text([{"role": "user", "content": "abc"}]) == "abc" and gi._text("x") == "x")

# ---- wiring presence (export paths) ----
collector = (ROOT / "otel-collector-config.yaml").read_text()
check("collector has the otlphttp/galileo exporter -> api.galileo.ai",
      "otlphttp/galileo" in collector and "api.galileo.ai/otel/traces" in collector)
check("galileo exporter is on the traces pipeline",
      "otlphttp/galileo" in collector.split("pipelines:", 1)[-1])
check("run.sh exports GALILEO_* to the app process",
      "GALILEO_" in (ROOT / "run.sh").read_text())
check("run-collector.sh injects GALILEO_* into the collector",
      "GALILEO_API_KEY" in (ROOT / "run-collector.sh").read_text())
gov = (ROOT / "backend/logging/governance_logger.py").read_text()
check("governance logger fans completed turns out to Galileo",
      "galileo_integration" in gov and "maybe_log_turn" in gov)

print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
sys.exit(1 if _fails else 0)
