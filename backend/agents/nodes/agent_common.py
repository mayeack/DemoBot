"""Shared helpers for the multi-agent stage (coordinator / specialists / synthesizer).

These three nodes replace the former single ``domain`` agent. They share:
- ``request_model`` — the per-provider request-model name for OTel LLM spans.
- ``trace_entry`` — build one ``agent_trace`` record (truncated) from a model
  response, used to rebuild the multi-agent trace for Galileo.
- ``handle_agent_error`` — log a governance error and short-circuit the subgraph
  with the generic safe reply (mirrors the legacy domain-agent handler).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from backend.config import settings
from backend.logging.governance_logger import governance_logger
from backend.models.schemas import MessageType, SeverityLevel

# Per-agent model output stored in agent_trace is capped before it is logged
# (file / HEC / DB) and forwarded to Galileo, to bound governance-event size.
_MAX_TRACE_TEXT = 2000


def request_model(provider: str) -> str:
    """The request-model name reported on the OTel LLM span for a provider."""
    if provider == "bedrock":
        return settings.bedrock_model_id
    if provider == "openai":
        return settings.openai_model
    if provider == "ollama":
        return settings.ollama_model
    return settings.anthropic_model


def trace_entry(
    *, name: str, role: str, response: Optional[Any] = None, status: str = "ok"
) -> Dict[str, Any]:
    """Build one ``agent_trace`` record from a ``NormalizedLLMResponse``.

    ``output_text`` is truncated to ``_MAX_TRACE_TEXT`` chars. On a failed agent
    pass ``response=None, status="error"``.
    """
    if response is None:
        return {
            "name": name,
            "role": role,
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "output_text": "",
            "status": status,
        }
    text = response.content or ""
    if len(text) > _MAX_TRACE_TEXT:
        text = text[:_MAX_TRACE_TEXT] + "...(truncated)"
    return {
        "name": name,
        "role": role,
        "model": response.model,
        "input_tokens": response.input_tokens or 0,
        "output_tokens": response.output_tokens or 0,
        "output_text": text,
        "status": status,
    }


def handle_agent_error(state: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
    """Log a governance error and short-circuit with the generic safe reply.

    Matches the legacy ``domain_agent._handle_generation_error`` /
    ``RecommendationEngine.process_message`` exception handler so an agent failure
    anywhere in the multi-agent stage ends the turn identically.
    """
    governance_logger.log_error(
        session_id=state["session_id"],
        request_id=state["request_id"],
        error_type=type(exc).__name__,
        error_message=str(exc),
        stack_trace=None,
        enduser_id=state.get("enduser_id"),
    )
    return {
        "terminal": True,
        "result": {
            "message": (
                "I apologize, but I encountered an error. Please try again or seek "
                "immediate medical care if this is urgent."
            ),
            "type": MessageType.SAFETY_WARNING,
            "severity": SeverityLevel.MEDIUM,
            "escalated": True,
        },
    }
