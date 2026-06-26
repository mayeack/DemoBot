"""Synthesizer agent node (the turn's only user-facing agent).

The final agent of the multi-agent stage. It fuses the specialist findings into
the single structured recommendation the downstream nodes expect, and — because
it is the only user-facing agent — it carries the governance test-content
directive (so any solicited PII/toxic/hallucination/authority content lands in
the final answer for the guardrail/eval demo).

Absorbs the former ``domain_agent`` node: same parsing (``_parse_recommendation``
/ ``_extract_directive_tail``), same formatting (``content_engine``), and it sets
the same state fields. Differences from the old domain agent:
- ``llm_input_tokens`` / ``llm_output_tokens`` are the running SUM across
  coordinator + specialists + synthesizer (the multi-agent token contract).
- ``agent_name`` stays ``{theme}_domain_agent`` for Splunk dashboard continuity.

Telemetry: emits a ``{theme}_domain_agent`` AgentInvocation span wrapping its LLM
call.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List

from backend.agents.llm import ChatModelError, invoke_agent
from backend.agents.nodes.agent_common import (
    handle_agent_error,
    request_model,
    trace_entry,
)
from backend.agents.nodes.injection import build_input_directives
from backend.agents.nodes.shared import build_llm_messages, content_engine
from backend.agents.themes.base import build_synthesizer_prompt
from backend.config import settings
from backend.telemetry import otel

# Recommendation parsing is centralized on RecommendationEngine and reused here
# via ``content_engine._parse_recommendation`` (see backend/agents/nodes/shared.py).


def _extract_directive_tail(output_text: str) -> str:
    """Return any text the model appended AFTER its structured JSON answer.

    Under the governance directive the model emits its normal ```json answer```
    and then appends a "Synthetic governance test samples" block; preserving the
    tail lets a compliant model response stand on its own. (Moved verbatim from
    the domain agent.)
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


def _format_specialist_findings(specialist_outputs: List[Dict[str, Any]]) -> str:
    """Concatenate specialist analyses into one labeled block for the synth prompt."""
    parts: List[str] = []
    for out in specialist_outputs or []:
        label = out.get("label") or out.get("key") or "Specialist"
        analysis = (out.get("analysis") or "").strip()
        if analysis:
            parts.append(f"[{label}]\n{analysis}")
    return "\n\n".join(parts)


def make_synthesizer_agent(theme_config) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build the synthesizer node for a specific theme."""
    base_prompt = build_synthesizer_prompt(theme_config)
    agent_name = theme_config.agent_name  # {key}_domain_agent

    def synthesizer_node(state: Dict[str, Any]) -> Dict[str, Any]:
        messages = build_llm_messages(state.get("conversation_history", []))

        # Fold specialist findings into the system prompt, then append the
        # governance directive (one roll per turn — must live on the user-facing
        # agent so the post-LLM injection node sees the same decision).
        findings = _format_specialist_findings(state.get("specialist_outputs", []))
        directive_text, requested_categories = build_input_directives(state)
        system_prompt = base_prompt
        if findings:
            system_prompt = f"{system_prompt}\n\nSPECIALIST FINDINGS:\n{findings}"
        system_prompt = system_prompt + directive_text
        provider = settings.ai_provider

        with otel.agent_span(agent_name, theme=theme_config.key):
            try:
                with otel.llm_span(
                    request_model=request_model(provider), provider=provider
                ) as llm_sp:
                    response = invoke_agent(
                        settings,
                        agent_name=agent_name,
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
                return handle_agent_error(state, exc)

        recommendation = content_engine._parse_recommendation(
            response.content, theme_config.conversational
        )
        severity = content_engine._normalize_severity(
            recommendation.get("severity", "MEDIUM")
        )
        confidence = content_engine._coerce_confidence(recommendation.get("confidence", 0.5))
        final_message = content_engine._format_recommendation(recommendation)

        # Carry forward any governance test samples the model appended after its
        # JSON answer (structured themes only — conversational replies aren't
        # JSON-parsed, so their samples are already in final_message).
        if not theme_config.conversational:
            tail = _extract_directive_tail(response.content)
            if tail:
                final_message = f"{final_message}\n\n{tail}"

        trace = list(state.get("agent_trace", []))
        trace.append(
            trace_entry(name=agent_name, role="synthesizer", response=response)
        )

        return {
            "messages": messages,
            "requested_categories": requested_categories,
            "recommendation": recommendation,
            "agent_trace": trace,
            "llm_response_id": response.id,
            "llm_model": response.model,
            "llm_input_tokens": (state.get("llm_input_tokens", 0) or 0)
            + (response.input_tokens or 0),
            "llm_output_tokens": (state.get("llm_output_tokens", 0) or 0)
            + (response.output_tokens or 0),
            "llm_stop_reason": response.stop_reason,
            "severity": severity,
            "confidence": confidence,
            "final_message": final_message,
        }

    return synthesizer_node
