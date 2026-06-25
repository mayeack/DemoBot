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
check("collector has the otlphttp/galileo exporter -> multitenant galileocloud",
      "otlphttp/galileo" in collector and "api.multitenant.galileocloud.io/otel/traces" in collector)
check("galileo exporter is on the traces pipeline",
      "otlphttp/galileo" in collector.split("pipelines:", 1)[-1])
# Galileo ingests GenAI spans only; the fan-out must drop non-GenAI (HTTP) spans
# or Galileo answers every batch with a "No GenAI patterns detected" partial drop.
# Splunk APM, on its own pipeline, must still receive the FULL trace.
_pl = collector.split("pipelines:", 1)[-1]
_splunk_block = _pl.split("traces/galileo:", 1)[0]
_galileo_block = _pl.split("traces/galileo:", 1)[-1].split("metrics:", 1)[0]
check("collector defines a GenAI-only filter (drops non-gen_ai spans)",
      "filter/genai_only" in collector and "gen_ai.operation.name" in collector)
check("Galileo export pipeline applies the GenAI-only filter",
      "filter/genai_only" in _galileo_block and "otlphttp/galileo" in _galileo_block)
check("Splunk APM traces pipeline keeps the full trace (no GenAI filter)",
      "otlphttp/traces" in _splunk_block and "filter/genai_only" not in _splunk_block)
check("run.sh exports GALILEO_* to the app process",
      "GALILEO_" in (ROOT / "run.sh").read_text())
check("run-collector.sh injects GALILEO_* into the collector",
      "GALILEO_API_KEY" in (ROOT / "run-collector.sh").read_text())
gov = (ROOT / "backend/logging/governance_logger.py").read_text()
check("governance logger fans completed turns out to Galileo",
      "galileo_integration" in gov and "maybe_log_turn" in gov)

# ---- multi-agent nested emission (agent_trace -> nested Galileo spans) ----
# Fake the galileo SDK so _emit's span calls are captured without a network call.
import types  # noqa: E402

_span_calls = []


class _FakeGalileoLogger:
    def start_trace(self, **k): _span_calls.append(("start_trace", k))
    def add_workflow_span(self, **k): _span_calls.append(("add_workflow_span", k))
    def add_agent_span(self, **k): _span_calls.append(("add_agent_span", k))
    def add_llm_span(self, **k): _span_calls.append(("add_llm_span", k))
    def conclude(self, **k): _span_calls.append(("conclude", k))
    def flush(self): _span_calls.append(("flush", {})); return ["trace"]


_fake_galileo = types.ModuleType("galileo")
_fake_galileo.GalileoLogger = _FakeGalileoLogger
sys.modules["galileo"] = _fake_galileo

_trace = [
    {"name": "medadvice_coordinator", "role": "coordinator", "model": "m",
     "input_tokens": 10, "output_tokens": 5, "output_text": "plan", "status": "ok"},
    {"name": "medadvice_triage_specialist", "role": "specialist", "model": "m",
     "input_tokens": 20, "output_tokens": 8, "output_text": "triage", "status": "ok"},
    {"name": "medadvice_domain_agent", "role": "synthesizer", "model": "m",
     "input_tokens": 30, "output_tokens": 40, "output_text": "final", "status": "ok"},
]
_log = {
    "operation_name": "chat", "token_type": "output", "request_id": "rid",
    "input_messages": [{"role": "user", "content": "headache"}],
    "output_messages": [{"role": "assistant", "content": "final"}],
    "response_model": "m", "usage_input_tokens": 60, "usage_output_tokens": 53,
    "pii_detected": True, "agent_trace": _trace,
}
gi._emit(_log)
_names = [c[0] for c in _span_calls]
check("multi-agent emit nests one workflow span", _names.count("add_workflow_span") == 1)
check("multi-agent emit nests one agent span per agent_trace entry",
      _names.count("add_agent_span") == 3 and _names.count("add_llm_span") == 3)
check("multi-agent emit balances conclude() (3 agents + workflow + trace = 5)",
      _names.count("conclude") == 5 and _names[-1] == "flush")
check("governance metadata (pii_detected) rides on the Galileo spans",
      _span_calls[0][1].get("metadata", {}).get("pii_detected") is True)
check("coordinator agent span tagged with the supervisor AgentType",
      any(c[0] == "add_agent_span"
          and getattr(c[1].get("agent_type"), "value", None) == "supervisor"
          for c in _span_calls))

_span_calls.clear()
gi._emit({**_log, "agent_trace": None})
_fb = [c[0] for c in _span_calls]
check("no agent_trace -> falls back to a single LLM span (back-compat)",
      _fb.count("add_agent_span") == 0 and _fb.count("add_llm_span") == 1
      and _fb.count("conclude") == 1)

del sys.modules["galileo"]

print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
sys.exit(1 if _fails else 0)
