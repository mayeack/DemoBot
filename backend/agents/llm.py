"""LangChain chat-model factory (workshop section 4.7 "LLM Creation").

Replaces the bespoke ``backend.services.ai_client`` provider abstraction with
LangChain chat models so the agentic graph speaks the LangChain message
abstractions (``SystemMessage`` / ``HumanMessage`` / ``AIMessage``) end-to-end.

The provider is selected from the existing ``settings.ai_provider`` so no new
configuration is required:

    anthropic -> ChatAnthropic            (local development)
    bedrock   -> ChatBedrockConverse      (AWS production)
    openai    -> ChatOpenAI               (OpenAI-compatible APIs)

``invoke_chat`` normalizes the LangChain response into a small dataclass with the
same fields the governance contract needs (id, model, token usage, stop reason),
so the governance node can emit the exact same Splunk schema as before.

LangChain is imported lazily inside the factory so importing this module never
fails when the optional agentic dependencies are not installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class NormalizedLLMResponse:
    """Provider-agnostic view of a chat completion.

    Field names intentionally mirror ``ai_client.AIClientResponse`` so the
    governance logging stays byte-compatible with the legacy pipeline.
    """

    id: str
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class ChatModelError(Exception):
    """Raised when a chat model cannot be created or invoked."""


# Cache the constructed model per (provider, model) so we don't rebuild a client
# on every request.
_MODEL_CACHE: Dict[str, Any] = {}


def clear_caches() -> None:
    """Drop cached chat models / react agents so a runtime provider/model change
    (e.g. from the Settings UI) takes effect on the next call.

    The cache keys include the provider + token/temperature params but NOT the
    model name, so switching the model *within* a provider would otherwise keep
    serving a stale client. Called by ``settings_store.set_ai_provider``.
    """
    _MODEL_CACHE.clear()
    _AGENT_CACHE.clear()


def get_chat_model(settings, *, max_tokens: int = 2048, temperature: float = 0.7,
                   model_override: Optional[str] = None):
    """Return a LangChain chat model for the configured provider.

    ``model_override`` selects a specific model id instead of the provider's
    configured default (used to run the internal agents on the clean Ollama model
    while the user-facing synthesizer uses the selected/poisoned one). It is part
    of the cache key so two model names can coexist in one process.

    CA-bundle / proxy handling is inherited from process environment variables
    (``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``) that ``backend.config`` sets at
    import time, so the underlying SDK HTTP clients pick them up automatically.
    """
    provider = (settings.ai_provider or "anthropic").lower()
    # The model name MUST be in the key: without it, an override (or a runtime
    # model switch) would be served a stale client built for a different model.
    cache_key = f"{provider}:{model_override or '-'}:{max_tokens}:{temperature}"
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(
                model=settings.anthropic_model,
                api_key=settings.anthropic_api_key,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        elif provider == "bedrock":
            from langchain_aws import ChatBedrockConverse

            model = ChatBedrockConverse(
                model=settings.bedrock_model_id,
                region_name=settings.aws_region,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        elif provider == "openai":
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(
                model=settings.openai_model,
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        elif provider == "ollama":
            from langchain_ollama import ChatOllama

            # Local uncensored model served by `ollama serve`. ChatOllama uses
            # num_predict (NOT max_tokens) for the output cap and num_ctx for the
            # context window. usage_metadata is populated natively, so _extract_usage
            # tier-1 and the otel/governance/Galileo token plumbing work unchanged.
            # keep_alive keeps the 5GB model resident between turns (no cold reload).
            # model_override lets the internal agents run on the clean base while the
            # synthesizer runs the selected (possibly poisoned) model.
            model = ChatOllama(
                model=model_override or settings.ollama_model,
                base_url=settings.ollama_base_url,
                temperature=temperature,
                num_predict=max_tokens,
                num_ctx=settings.ollama_num_ctx,
                keep_alive=settings.ollama_keep_alive,
            )
        else:
            raise ChatModelError(
                f"Unknown AI provider: {provider}. "
                "Valid options are 'anthropic', 'bedrock', 'openai', or 'ollama'."
            )
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise ChatModelError(
            f"LangChain integration for provider '{provider}' is not installed: {exc}"
        ) from exc

    _MODEL_CACHE[cache_key] = model
    logger.info("Initialized LangChain chat model for provider: %s", provider)
    return model


def _to_langchain_messages(system: str, messages: List[Dict[str, Any]]):
    """Convert a system prompt + role/content history into LangChain messages."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    lc_messages: List[Any] = []
    if system:
        lc_messages.append(SystemMessage(content=system))

    for msg in messages:
        role = msg.get("role")
        role = role.value if hasattr(role, "value") else role
        content = msg.get("content", "")
        if role == "assistant":
            lc_messages.append(AIMessage(content=content))
        elif role in ("user", "system"):
            # Treat any non-assistant turn as a human turn for the model input.
            lc_messages.append(HumanMessage(content=content))
    return lc_messages


def _extract_text(ai_message) -> str:
    """Pull plain text out of a LangChain AIMessage (content may be a list)."""
    content = getattr(ai_message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", "") or "")
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def _extract_usage(ai_message) -> Dict[str, int]:
    usage = getattr(ai_message, "usage_metadata", None) or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    if not (input_tokens or output_tokens):
        # Fall back to provider response metadata shapes.
        meta = getattr(ai_message, "response_metadata", {}) or {}
        token_usage = meta.get("token_usage") or meta.get("usage") or {}
        input_tokens = int(
            token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)) or 0
        )
        output_tokens = int(
            token_usage.get("completion_tokens", token_usage.get("output_tokens", 0)) or 0
        )
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _extract_metadata(ai_message, fallback_model: str) -> Dict[str, str]:
    meta = getattr(ai_message, "response_metadata", {}) or {}
    response_id = (
        getattr(ai_message, "id", None)
        or meta.get("id")
        or "langchain-response"
    )
    model = meta.get("model_name") or meta.get("model") or fallback_model
    stop_reason = (
        meta.get("stop_reason")
        or meta.get("finish_reason")
        or meta.get("done_reason")  # Ollama reports the stop reason here
        or "end_turn"
    )
    return {"id": str(response_id), "model": str(model), "stop_reason": str(stop_reason)}


def invoke_chat(
    settings,
    *,
    system: str,
    messages: List[Dict[str, Any]],
    max_tokens: int = 2048,
    temperature: float = 0.7,
    fallback_model: Optional[str] = None,
) -> NormalizedLLMResponse:
    """Invoke the configured chat model and normalize the response.

    Raises :class:`ChatModelError` on any failure so callers can map it onto the
    existing error-handling / governance error event.
    """
    from backend.telemetry import otel
    from backend.model_emitter import model_emitter

    model = get_chat_model(settings, max_tokens=max_tokens, temperature=temperature)
    lc_messages = _to_langchain_messages(system, messages)
    provider = (settings.ai_provider or "anthropic").lower()
    fallback = fallback_model or (
        settings.anthropic_model
        if provider == "anthropic"
        else settings.bedrock_model_id
        if provider == "bedrock"
        else settings.openai_model
        if provider == "openai"
        else settings.ollama_model
    )
    # Demo override: report a static/random model name instead of the real one
    # (backend/model_emitter.py). The actual model call below is unchanged.
    emit_model = model_emitter.pick() if model_emitter.is_active() else None

    # Emit a GenAI LLMInvocation with the real model + token usage so Splunk no
    # longer sees model="unknown" and can price the call (server-side cost).
    with otel.genai_llm_invocation(
        request_model=(emit_model or fallback), provider=provider,
        system=system, messages=messages,
    ) as llm_inv:
        try:
            ai_message = model.invoke(lc_messages)
        except Exception as exc:  # noqa: BLE001 - normalize all provider errors
            raise ChatModelError(f"Chat model invocation failed: {exc}") from exc
        usage = _extract_usage(ai_message)
        meta = _extract_metadata(ai_message, fallback)
        reported_model = emit_model or meta["model"]
        response_text = _extract_text(ai_message)
        if llm_inv is not None:
            llm_inv.input_tokens = usage["input_tokens"]
            llm_inv.output_tokens = usage["output_tokens"]
            llm_inv.response_model_name = reported_model
            llm_inv.response_id = meta["id"]
            # Attach prompt+response content so the span surfaces in Splunk AI trace data.
            otel.record_genai_output(llm_inv, text=response_text, finish_reason=meta["stop_reason"])

    return NormalizedLLMResponse(
        id=meta["id"],
        content=response_text,
        model=reported_model,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        stop_reason=meta["stop_reason"],
    )


# ---------------------------------------------------------------------------
# Named agents (LangGraph create_react_agent) for Splunk AI Agent Monitoring.
# ---------------------------------------------------------------------------
# The Splunk LangChain instrumentation surfaces each create_react_agent as a
# named agent-invocation span. We build a *tool-less* react agent per agent name
# (one model call, no tool loop) so the existing single-call JSON/governance
# contract is preserved; the per-request system prompt is supplied as a
# SystemMessage in the input so the cached agent stays prompt-agnostic.
_AGENT_CACHE: Dict[str, Any] = {}


def get_react_agent(settings, *, name: str, max_tokens: int = 2048, temperature: float = 0.7,
                    model_override: Optional[str] = None):
    """Return a cached, tool-less LangGraph react agent for the given name."""
    provider = (settings.ai_provider or "anthropic").lower()
    # model_override is in the key so an internal-vs-synthesizer model split caches
    # distinct agents rather than colliding on (provider, name, tokens, temp).
    cache_key = f"{provider}:{name}:{model_override or '-'}:{max_tokens}:{temperature}"
    cached = _AGENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        from langgraph.prebuilt import create_react_agent
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ChatModelError(f"langgraph is not installed: {exc}") from exc

    model = get_chat_model(settings, max_tokens=max_tokens, temperature=temperature,
                           model_override=model_override)
    # tools=[] -> the agent answers directly (a single model call, no tool loop),
    # so token usage / response id stay 1:1 with the legacy contract. name=... is
    # what Splunk AI Agent Monitoring shows as the agent.
    agent = create_react_agent(model, tools=[], name=name)
    _AGENT_CACHE[cache_key] = agent
    logger.info("Built react agent '%s' for provider: %s", name, settings.ai_provider)
    return agent


def invoke_agent(
    settings,
    *,
    agent_name: str,
    system: str,
    messages: List[Dict[str, Any]],
    max_tokens: int = 2048,
    temperature: float = 0.7,
    fallback_model: Optional[str] = None,
    model_override: Optional[str] = None,
) -> NormalizedLLMResponse:
    """Invoke a named react agent and normalize the response.

    Behaviourally equivalent to :func:`invoke_chat` (one model call, same
    ``NormalizedLLMResponse``) but routed through a named ``create_react_agent``
    so it registers as an agent in Splunk AI Agent Monitoring. The system prompt
    is passed as a ``SystemMessage`` so the cached agent need not bake it in.

    ``model_override`` runs this agent on a specific model id (used to run the
    internal coordinator/specialist agents on the clean Ollama model while the
    synthesizer runs the selected one); it also becomes the reported request-model
    so telemetry labels each span with the model actually used.
    """
    from langchain_core.messages import AIMessage

    from backend.telemetry import otel

    agent = get_react_agent(
        settings, name=agent_name, max_tokens=max_tokens, temperature=temperature,
        model_override=model_override,
    )
    lc_messages = _to_langchain_messages(system, messages)
    provider = (settings.ai_provider or "anthropic").lower()
    fallback = fallback_model or model_override or (
        settings.anthropic_model
        if provider == "anthropic"
        else settings.bedrock_model_id
        if provider == "bedrock"
        else settings.openai_model
        if provider == "openai"
        else settings.ollama_model
    )
    # Demo override: report a static/random model name instead of the real one
    # (backend/model_emitter.py). The actual agent call below is unchanged.
    from backend.model_emitter import model_emitter
    emit_model = model_emitter.pick() if model_emitter.is_active() else None

    # Emit a GenAI AgentInvocation (so the named agent appears in Splunk's "AI
    # agents" view) wrapping a nested LLMInvocation with the real model + token
    # usage — the auto-instrumentation emits no agent span for create_react_agent.
    with otel.genai_agent_invocation(
        agent_name=agent_name, request_model=(emit_model or fallback), provider=provider,
        system=system, messages=messages,
    ) as llm_inv:
        try:
            result = agent.invoke({"messages": lc_messages})
        except Exception as exc:  # noqa: BLE001 - normalize all errors
            raise ChatModelError(f"Agent '{agent_name}' invocation failed: {exc}") from exc

        out_messages = result.get("messages", []) if isinstance(result, dict) else []
        ai_messages = [m for m in out_messages if isinstance(m, AIMessage)]
        if not ai_messages:
            raise ChatModelError(f"Agent '{agent_name}' returned no AI message")
        final = ai_messages[-1]

        # One model call when tool-less, but sum defensively in case of future tools.
        input_tokens = output_tokens = 0
        for msg in ai_messages:
            usage = _extract_usage(msg)
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]

        meta = _extract_metadata(final, fallback)
        reported_model = emit_model or meta["model"]
        final_text = _extract_text(final)
        if llm_inv is not None:
            llm_inv.input_tokens = input_tokens
            llm_inv.output_tokens = output_tokens
            llm_inv.response_model_name = reported_model
            llm_inv.response_id = meta["id"]
            # Attach prompt+response content so the span surfaces in Splunk AI trace data.
            otel.record_genai_output(llm_inv, text=final_text, finish_reason=meta["stop_reason"])

    return NormalizedLLMResponse(
        id=meta["id"],
        content=final_text,
        model=reported_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=meta["stop_reason"],
    )
