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


def get_chat_model(settings, *, max_tokens: int = 2048, temperature: float = 0.7):
    """Return a LangChain chat model for the configured provider.

    CA-bundle / proxy handling is inherited from process environment variables
    (``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``) that ``backend.config`` sets at
    import time, so the underlying SDK HTTP clients pick them up automatically.
    """
    provider = (settings.ai_provider or "anthropic").lower()
    cache_key = f"{provider}:{max_tokens}:{temperature}"
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
        else:
            raise ChatModelError(
                f"Unknown AI provider: {provider}. "
                "Valid options are 'anthropic', 'bedrock', or 'openai'."
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
        or "end_turn"
    )
    return {"id": str(response_id), "model": str(model), "stop_reason": str(stop_reason)}


def _record_tokens(settings, model: str, input_tokens: int, output_tokens: int) -> None:
    """Emit gen_ai.client.token.usage from accurate app data. The Splunk LangChain
    auto-instrumentation misses token usage on the create_react_agent path, so we
    record the standard GenAI token histogram ourselves with the correct model."""
    try:
        from backend.telemetry import otel

        otel.record_genai_tokens(
            model=model,
            provider=(settings.ai_provider or "anthropic"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception:  # noqa: BLE001 - telemetry must never break a chat turn
        pass


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
    model = get_chat_model(settings, max_tokens=max_tokens, temperature=temperature)
    lc_messages = _to_langchain_messages(system, messages)

    try:
        ai_message = model.invoke(lc_messages)
    except Exception as exc:  # noqa: BLE001 - normalize all provider errors
        raise ChatModelError(f"Chat model invocation failed: {exc}") from exc

    fallback = fallback_model or (
        settings.anthropic_model
        if settings.ai_provider == "anthropic"
        else settings.bedrock_model_id
        if settings.ai_provider == "bedrock"
        else settings.openai_model
    )
    usage = _extract_usage(ai_message)
    meta = _extract_metadata(ai_message, fallback)
    _record_tokens(settings, meta["model"], usage["input_tokens"], usage["output_tokens"])

    return NormalizedLLMResponse(
        id=meta["id"],
        content=_extract_text(ai_message),
        model=meta["model"],
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


def get_react_agent(settings, *, name: str, max_tokens: int = 2048, temperature: float = 0.7):
    """Return a cached, tool-less LangGraph react agent for the given name."""
    cache_key = f"{(settings.ai_provider or 'anthropic').lower()}:{name}:{max_tokens}:{temperature}"
    cached = _AGENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        from langgraph.prebuilt import create_react_agent
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ChatModelError(f"langgraph is not installed: {exc}") from exc

    model = get_chat_model(settings, max_tokens=max_tokens, temperature=temperature)
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
) -> NormalizedLLMResponse:
    """Invoke a named react agent and normalize the response.

    Behaviourally equivalent to :func:`invoke_chat` (one model call, same
    ``NormalizedLLMResponse``) but routed through a named ``create_react_agent``
    so it registers as an agent in Splunk AI Agent Monitoring. The system prompt
    is passed as a ``SystemMessage`` so the cached agent need not bake it in.
    """
    from langchain_core.messages import AIMessage

    from backend.telemetry import otel

    agent = get_react_agent(
        settings, name=agent_name, max_tokens=max_tokens, temperature=temperature
    )
    lc_messages = _to_langchain_messages(system, messages)
    provider = (settings.ai_provider or "anthropic").lower()
    fallback = fallback_model or (
        settings.anthropic_model
        if provider == "anthropic"
        else settings.bedrock_model_id
        if provider == "bedrock"
        else settings.openai_model
    )

    # Wrap the agent call in a GenAI invoke_agent span (gen_ai.agent.name) so
    # Splunk AI Agent Monitoring registers the named agent — the auto-
    # instrumentation emits no agent span for create_react_agent.
    with otel.genai_agent_span(
        agent_name=agent_name, request_model=fallback, provider=provider
    ) as span:
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
        otel.record_llm_result(
            span,
            response_id=meta["id"],
            response_model=meta["model"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=meta["stop_reason"],
        )

    _record_tokens(settings, meta["model"], input_tokens, output_tokens)
    return NormalizedLLMResponse(
        id=meta["id"],
        content=_extract_text(final),
        model=meta["model"],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=meta["stop_reason"],
    )
