"""Per-theme domain agent node (the LLM specialist).

This is the theme's core agent: it calls the LLM with the theme's system prompt,
parses the structured recommendation (assessment / guidance / seek_care_if /
severity / confidence), normalizes severity, and formats the base message.

Decomposition note: assessment and guidance are produced by this single
structured call so the governance token/JSON contract stays intact (one model
call -> one set of usage tokens and one response id). The surrounding specialist
agents (intake, safety, injection, compliance, defense) form the rest of the
per-theme decomposition.

Telemetry: wrapped in an ``AgentInvocation`` span (named per theme) containing an
``LLMInvocation`` span carrying model + token usage.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict

from backend.agents.llm import ChatModelError, invoke_agent
from backend.agents.nodes.injection import build_input_directives
from backend.agents.nodes.shared import build_llm_messages, content_engine
from backend.config import settings
from backend.logging.governance_logger import governance_logger
from backend.models.schemas import MessageType, SeverityLevel
from backend.telemetry import otel


def _parse_recommendation(output_text: str, conversational: bool) -> Dict[str, Any]:
    """Parse the model output into a recommendation dict.

    Mirrors the legacy parsing: extract JSON from a fenced code block if present,
    otherwise parse the whole string, with theme-aware fallbacks.
    """
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output_text, re.DOTALL)
    json_text = json_match.group(1) if json_match else output_text

    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        if conversational:
            return {"reply": output_text, "severity": "MEDIUM", "confidence": 0.5}
        return {
            "assessment": output_text[:200],
            "guidance": [output_text],
            "seek_care_if": ["Symptoms persist or worsen"],
            "severity": "MEDIUM",
            "confidence": 0.5,
        }


def _extract_directive_tail(output_text: str) -> str:
    """Return any text the model appended AFTER its structured JSON answer.

    Under the governance directive the model emits its normal ```json answer```
    and then appends a "Synthetic governance test samples" block. The JSON-only
    parse in ``_parse_recommendation`` would discard that block, so the injection
    node would never see the model's own governance content and would wrongly fall
    back. Preserving the tail lets a compliant model response stand on its own.
    """
    text = output_text or ""
    m = re.search(r"```(?:json)?\s*\{.*?\}\s*```", text, re.DOTALL)
    if m:
        return text[m.end():].strip()
    idx = text.lower().find("synthetic governance test samples")
    if idx != -1:
        nl = text.rfind("\n", 0, idx)
        return text[(nl + 1 if nl != -1 else idx):].strip()
    return ""


def make_domain_agent(theme_config) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build the domain-agent node for a specific theme."""

    def domain_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
        messages = build_llm_messages(state.get("conversation_history", []))
        system_prompt = state.get("system_prompt") or theme_config.system_prompt
        # Governance demo: instead of injecting unsafe content into the output,
        # append a directive to the INPUT asking the model to produce the toggled
        # content itself. The decision (one roll per turn) is shared with the
        # post-LLM injection node via ``requested_categories``.
        directive_text, requested_categories = build_input_directives(state)
        system_prompt = system_prompt + directive_text
        provider = settings.ai_provider

        with otel.agent_span(theme_config.agent_name, theme=theme_config.key):
            try:
                with otel.llm_span(
                    request_model=_request_model(provider), provider=provider
                ) as llm_sp:
                    response = invoke_agent(
                        settings,
                        agent_name=theme_config.agent_name,
                        system=system_prompt,
                        messages=messages,
                        max_tokens=2048,
                        temperature=0.7,
                    )
                    otel.record_llm_result(
                        llm_sp,
                        response_id=response.id,
                        response_model=response.model,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        finish_reason=response.stop_reason,
                    )
            except ChatModelError as exc:
                return _handle_generation_error(state, exc)

        recommendation = _parse_recommendation(
            response.content, theme_config.conversational
        )
        severity = content_engine._normalize_severity(
            recommendation.get("severity", "MEDIUM")
        )
        confidence = recommendation.get("confidence", 0.5)
        final_message = content_engine._format_recommendation(recommendation)

        # Carry forward any governance test samples the model appended after its
        # JSON answer (structured themes only — conversational replies aren't
        # JSON-parsed, so their samples are already in final_message).
        if not theme_config.conversational:
            tail = _extract_directive_tail(response.content)
            if tail:
                final_message = f"{final_message}\n\n{tail}"

        return {
            "messages": messages,
            "requested_categories": requested_categories,
            "recommendation": recommendation,
            "llm_response_id": response.id,
            "llm_model": response.model,
            "llm_input_tokens": response.input_tokens,
            "llm_output_tokens": response.output_tokens,
            "llm_stop_reason": response.stop_reason,
            "severity": severity,
            "confidence": confidence,
            "final_message": final_message,
        }

    return domain_agent_node


def _handle_generation_error(state: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
    """Log a governance error and short-circuit with the generic safe reply.

    Matches the legacy ``process_message`` exception handler.
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


def _request_model(provider: str) -> str:
    if provider == "bedrock":
        return settings.bedrock_model_id
    if provider == "openai":
        return settings.openai_model
    if provider == "ollama":
        return settings.ollama_model
    return settings.anthropic_model
