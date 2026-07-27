#!/usr/bin/env python3
"""Regression: execute_tool spans carry the GenAI tool attributes.

The agentic tool guard (backend/routers/toolguard.py) emits an ``execute_tool``
span per governed tool call via ``otel.tool_span`` / ``otel.record_tool_result``.
Two things must hold for the demo telemetry to work:

  1. The span sets ``gen_ai.operation.name == "execute_tool"`` — WITHOUT it the
     collector's ``filter/genai_only`` processor drops the span before Galileo,
     and it never groups with the other GenAI operations in Splunk AI Agent
     Monitoring. It must also carry ``gen_ai.tool.name`` / ``gen_ai.tool.call.id``
     and the verdict attributes.
  2. ``tool_span`` degrades to a no-op (yields None, never raises) when
     telemetry is disabled — the same defensive contract as every other helper.

Code-level check (no live Splunk): drives the helpers through an in-memory
exporter. Run: venv/bin/python tests/observability/test_tool_span_content.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from backend.telemetry import otel  # noqa: E402

_EXPORTER = InMemorySpanExporter()


def _attr(span, key):
    return (span.attributes or {}).get(key)


def _find(name_prefix):
    for s in _EXPORTER.get_finished_spans():
        if s.name.startswith(name_prefix):
            return s
    return None


def check_tool_span_enabled():
    """With telemetry enabled the execute_tool span carries the tool attrs."""
    _EXPORTER.clear()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    # tool_span uses the _STATE tracer path (not the util-genai handler), so
    # populate it exactly as init_telemetry would.
    otel._STATE["tracer"] = trace.get_tracer("test", tracer_provider=provider)
    otel._STATE["enabled"] = True
    try:
        with otel.tool_span(
            "web_fetch", tool_call_id="call-42", agent_surface="openclaw",
        ) as span:
            assert span is not None, "tool_span yielded None while enabled"
            otel.record_tool_result(
                span, decision="block",
                denied_reason="unapproved egress host: evil.example",
                rule_names=["Egress Allowlist", "General Harms"],
                event_id="evt-1",
            )
    finally:
        otel._STATE["enabled"] = False
        otel._STATE["tracer"] = None

    span = _find("execute_tool ")
    assert span is not None, "no 'execute_tool' span was emitted"
    assert _attr(span, "gen_ai.operation.name") == "execute_tool", \
        "operation.name != execute_tool (Galileo filter would drop this span)"
    assert _attr(span, "gen_ai.tool.name") == "web_fetch", "missing gen_ai.tool.name"
    assert _attr(span, "gen_ai.tool.call.id") == "call-42", "missing gen_ai.tool.call.id"
    assert _attr(span, "demobot.tool.decision") == "block", "missing tool decision"
    assert "evil.example" in str(_attr(span, "demobot.tool.denied_reason") or ""), \
        "missing denied reason"
    assert _attr(span, "demobot.ai_defense.event_id") == "evt-1", "missing AI Defense event id"


def check_tool_span_disabled():
    """With telemetry disabled tool_span is a no-op that yields None."""
    _EXPORTER.clear()
    otel._STATE["enabled"] = False
    otel._STATE["tracer"] = None
    with otel.tool_span("bash", tool_call_id="c1") as span:
        assert span is None, "tool_span must yield None when telemetry is disabled"
        # record_tool_result on None must be a harmless no-op.
        otel.record_tool_result(span, decision="allow")
    assert not _EXPORTER.get_finished_spans(), "no spans should be emitted when disabled"


def main():
    ok = True
    for name, fn in [
        ("execute_tool span carries tool + verdict attrs", check_tool_span_enabled),
        ("tool_span no-op when telemetry disabled", check_tool_span_disabled),
    ]:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            ok = False
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print()
    if ok:
        print("execute_tool span content checks passed.")
        sys.exit(0)
    print("execute_tool span content checks FAILED.")
    sys.exit(1)


if __name__ == "__main__":
    main()
