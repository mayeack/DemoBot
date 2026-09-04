#!/usr/bin/env python3
"""Regression: gen_ai spans must carry message CONTENT (prompt + response).

The bug this guards (fixed 2026-06-23): the app emitted gen_ai spans with only
metadata (model / token usage) and never set the conversation messages, so Splunk
Observability Cloud's "AI trace data" view — which indexes gen_ai spans by their
input/output *content* and runs the Content / quality / risk evaluations on it —
stayed EMPTY even though the spans reached APM (and were visible in Trace
Analyzer). The metrics-only checks (verify_observability.sh Tier 3 /
check_o11y_metadata.py) passed throughout, which is exactly why this slipped by.

This is a code-level check (no live Splunk needed): it drives the util-genai
invocation helpers exactly as backend/agents/llm.py does, emits the spans through
an in-memory exporter, and asserts the emitted span attributes carry the prompt
(gen_ai.input.messages) and response (gen_ai.output.messages). It therefore fails
loudly if anyone stops populating message content on the gen_ai invocations.

Run:  venv/bin/python tests/observability/test_genai_span_content.py
Exit 0 = pass, non-zero = fail.
"""
import os
import sys

# Mirror the app's telemetry env (run.sh / .env) BEFORE the util-genai handler is
# created: content capture on, and the same emitters the app uses (the emitter is
# what writes input at span-start and output at span-stop).
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_ONLY"
os.environ["OTEL_INSTRUMENTATION_GENAI_EMITTERS"] = "span_metric,splunk"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from backend.telemetry import otel  # noqa: E402

# Quote/brace-free so the raw substring survives JSON-escaping inside the span
# attribute (gen_ai.*.messages serialize as JSON, escaping any embedded quotes).
SYS = "You are a medical guidance assistant providing general health information."
USER = "I have a sore throat and mild fever"
RESP = "Likely a viral upper respiratory infection; severity LOW. Rest and hydrate."

_EXPORTER = InMemorySpanExporter()


def _setup_tracer():
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(provider)


def _attr(span, key):
    return (span.attributes or {}).get(key)


def _find(name_prefix):
    for s in _EXPORTER.get_finished_spans():
        if s.name.startswith(name_prefix):
            return s
    return None


def check_llm():
    _EXPORTER.clear()
    with otel.genai_llm_invocation(
        request_model="claude-sonnet-4-5-20250929", provider="anthropic",
        system=SYS, messages=[{"role": "user", "content": USER}],
    ) as inv:
        assert inv is not None, "util-genai handler unavailable (splunk-otel-util-genai installed?)"
        otel.record_genai_output(inv, text=RESP, finish_reason="end_turn")
    span = _find("chat ")
    assert span is not None, "no 'chat' gen_ai span was emitted"
    in_msgs = _attr(span, "gen_ai.input.messages")
    out_msgs = _attr(span, "gen_ai.output.messages")
    assert in_msgs and USER in str(in_msgs), "gen_ai.input.messages missing the user prompt"
    assert out_msgs and RESP in str(out_msgs), "gen_ai.output.messages missing the response"


def check_output_token_cache_split():
    """The cache split rides in the invocation's custom attributes dict (the
    handler has no field for it), so it has to actually reach the span."""
    _EXPORTER.clear()
    with otel.genai_llm_invocation(
        request_model="mistral-nemo:12b", provider="ollama",
        system=SYS, messages=[{"role": "user", "content": USER}],
    ) as inv:
        assert inv is not None, "util-genai handler unavailable"
        inv.input_tokens, inv.output_tokens = 120, 260
        otel.record_output_token_cache_split(inv, 90, 170)
        otel.record_genai_output(inv, text=RESP, finish_reason="end_turn")
    span = _find("chat ")
    assert span is not None, "no 'chat' gen_ai span was emitted"
    cached = _attr(span, otel.ATTR_OUTPUT_TOKENS_CACHED)
    uncached = _attr(span, otel.ATTR_OUTPUT_TOKENS_UNCACHED)
    assert cached == 90, f"{otel.ATTR_OUTPUT_TOKENS_CACHED} missing/wrong: {cached}"
    assert uncached == 170, f"{otel.ATTR_OUTPUT_TOKENS_UNCACHED} missing/wrong: {uncached}"
    total = _attr(span, "gen_ai.usage.output_tokens")
    assert cached + uncached == total, f"split {cached}+{uncached} != output_tokens {total}"


def check_agent():
    _EXPORTER.clear()
    with otel.genai_agent_invocation(
        agent_name="medadvice_domain_agent", request_model="claude-sonnet-4-5-20250929",
        provider="anthropic", system=SYS, messages=[{"role": "user", "content": USER}],
    ) as inv:
        assert inv is not None, "util-genai handler unavailable"
        otel.record_genai_output(inv, text=RESP, finish_reason="end_turn")
    chat = _find("chat ")
    agent = _find("invoke_agent ")
    assert chat is not None, "no nested 'chat' span under the agent"
    assert agent is not None, "no 'invoke_agent' gen_ai span was emitted"
    assert USER in str(_attr(chat, "gen_ai.input.messages") or ""), "agent's LLM span missing prompt content"
    assert RESP in str(_attr(chat, "gen_ai.output.messages") or ""), "agent's LLM span missing response content"


def main():
    _setup_tracer()
    ok = True
    for name, fn in [("LLM span carries prompt+response", check_llm),
                     ("LLM span carries the output-token cache split", check_output_token_cache_split),
                     ("Agent span carries prompt+response", check_agent)]:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            ok = False
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ERROR {name}: {e}")
    print(f"\nRESULT: {'passed' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
