#!/usr/bin/env python3
"""Regression: the local Ollama provider wiring (AI_PROVIDER=ollama).

Guards the re-architecture that added a local, uncensored open-source model
(served by Ollama, called via langchain-ollama's ChatOllama) as a 4th
``ai_provider`` alongside anthropic/bedrock/openai. The whole point of the local
model is to emit unsafe output the external guardrails then catch, so the
telemetry/governance token+model contract MUST survive the swap unchanged.

This is an offline code-level check (no Ollama daemon / no network):
  - get_chat_model builds a ChatOllama with the right model/base_url and maps
    max_tokens -> num_predict (ChatOllama's output cap) + num_ctx.
  - _extract_metadata honors Ollama's `done_reason` for the stop reason.
  - _extract_usage reads ChatOllama's native usage_metadata token shape.
  - _active_provider_info() reports provider_name="ollama" + the local model.
  - invoke_agent end-to-end (stubbed agent) returns a NormalizedLLMResponse with
    non-zero tokens, the local model, and the Ollama stop reason — i.e. the
    governance/otel/Galileo plumbing gets real values for a local model.

Run:  venv/bin/python tests/test_ollama_provider.py    # exit 0 = pass
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from backend.agents import llm  # noqa: E402
from backend.agents.llm import (  # noqa: E402
    NormalizedLLMResponse,
    _extract_metadata,
    _extract_usage,
    get_chat_model,
)

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


class _Stub:
    """Minimal stand-in for backend.config.settings (only fields the code reads)."""

    ai_provider = "ollama"
    ollama_model = "dolphin3:8b"
    ollama_base_url = "http://localhost:11434"
    ollama_num_ctx = 8192
    ollama_keep_alive = "30m"
    ollama_model_internal = "dolphin3:8b"
    # Cloud fields the fallback ternary references for other providers.
    anthropic_model = "claude-sonnet-4-5-20250929"
    bedrock_model_id = "anthropic.claude-3-sonnet-20240229-v1:0"
    openai_model = "gpt-4o"


def test_get_chat_model() -> None:
    llm._MODEL_CACHE.clear()
    model = get_chat_model(_Stub(), max_tokens=2048, temperature=0.7)
    check("get_chat_model(ollama) -> ChatOllama", type(model).__name__ == "ChatOllama")
    check("ChatOllama.model == settings.ollama_model", model.model == "dolphin3:8b")
    check("ChatOllama.base_url == settings.ollama_base_url",
          model.base_url == "http://localhost:11434")
    # ChatOllama uses num_predict (NOT max_tokens) for the output cap.
    check("max_tokens mapped to num_predict", getattr(model, "num_predict", None) == 2048)
    check("ollama_num_ctx mapped to num_ctx", getattr(model, "num_ctx", None) == 8192)
    check("ollama_keep_alive passed to ChatOllama", getattr(model, "keep_alive", None) == "30m")

    # model_override selects a different model id AND caches as a distinct client
    # (the cache key must include the model name, else the override would no-op).
    override = get_chat_model(_Stub(), max_tokens=2048, temperature=0.7,
                              model_override="dolphin3-medadvice-poisoned")
    check("model_override sets ChatOllama.model",
          override.model == "dolphin3-medadvice-poisoned")
    check("model_override is a distinct cache entry from the default", override is not model)


def test_extract_metadata_done_reason() -> None:
    msg = AIMessage(
        content="x",
        response_metadata={"model": "dolphin3:8b", "done_reason": "stop"},
    )
    meta = _extract_metadata(msg, fallback_model="fallback")
    check("stop_reason falls back to Ollama's done_reason", meta["stop_reason"] == "stop")
    check("model read from response_metadata.model", meta["model"] == "dolphin3:8b")


def test_extract_usage_native_metadata() -> None:
    msg = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    usage = _extract_usage(msg)
    check("usage_metadata input_tokens read", usage["input_tokens"] == 11)
    check("usage_metadata output_tokens read", usage["output_tokens"] == 7)


def test_active_provider_info() -> None:
    from backend.config import settings
    from backend.logging import governance_logger as gl

    orig = settings.ai_provider
    try:
        settings.ai_provider = "ollama"
        name, model = gl._active_provider_info()
        check("_active_provider_info name == 'ollama'", name == "ollama")
        check("_active_provider_info model == settings.ollama_model",
              model == settings.ollama_model)
    finally:
        settings.ai_provider = orig


def test_legacy_get_ai_client_ollama() -> None:
    """The legacy engine is built at import time (backend/routers/chat.py), so
    get_ai_client MUST accept 'ollama' or the whole app fails to import. It routes
    to the OpenAI-compatible client pointed at Ollama's /v1 endpoint."""
    from backend.services.ai_client import OpenAIClient, get_ai_client

    client = get_ai_client(_Stub())
    check("get_ai_client(ollama) -> OpenAIClient", isinstance(client, OpenAIClient))
    check("legacy ollama base_url targets /v1",
          getattr(client, "base_url", "") == "http://localhost:11434/v1")
    check("legacy ollama model == settings.ollama_model",
          getattr(client, "model", None) == "dolphin3:8b")


def test_invoke_agent_end_to_end_stubbed() -> None:
    """invoke_agent with a stubbed react agent: proves the governance/otel values
    (tokens, model, stop reason) populate for a local model with NO network."""

    class _FakeAgent:
        def invoke(self, _payload):
            return {
                "messages": [
                    AIMessage(
                        content='{"assessment": "ok", "severity": "LOW", "confidence": 0.9}',
                        usage_metadata={"input_tokens": 23, "output_tokens": 9, "total_tokens": 32},
                        response_metadata={"model": "dolphin3:8b", "done_reason": "stop"},
                        id="ollama-resp-1",
                    )
                ]
            }

    orig = llm.get_react_agent
    try:
        llm.get_react_agent = lambda *a, **k: _FakeAgent()  # type: ignore[assignment]
        resp = llm.invoke_agent(
            _Stub(),
            agent_name="medadvice_domain_agent",
            system="You are a medical guidance assistant.",
            messages=[{"role": "user", "content": "sore throat"}],
        )
    finally:
        llm.get_react_agent = orig

    check("invoke_agent returns NormalizedLLMResponse", isinstance(resp, NormalizedLLMResponse))
    check("input tokens propagate (23)", resp.input_tokens == 23)
    check("output tokens propagate (9)", resp.output_tokens == 9)
    check("total_tokens computed (32)", resp.total_tokens == 32)
    check("local model name propagates", resp.model == "dolphin3:8b")
    check("Ollama stop reason propagates", resp.stop_reason == "stop")


def main() -> int:
    for fn in (
        test_get_chat_model,
        test_extract_metadata_done_reason,
        test_extract_usage_native_metadata,
        test_active_provider_info,
        test_legacy_get_ai_client_ollama,
        test_invoke_agent_end_to_end_stubbed,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global _fails
            _fails += 1
            print(f"  ERROR {fn.__name__}: {e}")
    print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
