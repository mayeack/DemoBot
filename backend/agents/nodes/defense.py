"""Cisco AI Defense inspection nodes (prompt + response).

Wrap the existing ``AIDefenseClient`` and reuse the engine's block handlers so
the governance verdict logging is byte-identical to the legacy pipeline. Both
nodes are no-ops unless the request opted in (``ai_defense_review``) and the
server is configured.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from backend.agents.nodes.shared import content_engine
from backend.agents.state import governance_identity_overrides
from backend.services.ai_defense import ai_defense_client
from backend.telemetry import otel


def _stage_timing(state: Dict[str, Any], stage: str, started: float) -> Dict[str, float]:
    """Merge this stage's elapsed wall-clock into the running stage_timings."""
    timings = dict(state.get("stage_timings") or {})
    timings[f"{stage}_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return timings


def prompt_defense_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if not (state.get("ai_defense_review") and ai_defense_client.is_configured):
        return {}

    started = time.perf_counter()
    with otel.agent_span("ai_defense_prompt_agent", theme=state.get("theme")):
        inspection = ai_defense_client.inspect_prompt(
            state["user_message"], enduser_id=state.get("enduser_id")
        )
        if not inspection.should_block:
            return {"stage_timings": _stage_timing(state, "prompt_defense", started)}

        result = content_engine._handle_ai_defense_block(
            session_id=state["session_id"],
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            user_message=state["user_message"],
            conversation_history=state.get("conversation_history", []),
            inspection=inspection,
            start_time=state["start_time"],
            client_address=state.get("client_address"),
            enduser_id=state.get("enduser_id"),
            governance_overrides=governance_identity_overrides(state),
        )

    return {
        "terminal": True,
        "result": result,
        "stage_timings": _stage_timing(state, "prompt_defense", started),
    }


def response_defense_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if not (state.get("ai_defense_review") and ai_defense_client.is_configured):
        return {}

    started = time.perf_counter()
    with otel.agent_span("ai_defense_response_agent", theme=state.get("theme")):
        inspection = ai_defense_client.inspect_response(
            user_message=state["user_message"],
            assistant_message=state["final_message"],
            enduser_id=state.get("enduser_id"),
        )
        if not inspection.should_block:
            return {"stage_timings": _stage_timing(state, "response_defense", started)}

        # The model already answered here (that answer is what got blocked), so
        # report its real id and token spend rather than the zeros/active-model
        # fallback the handler uses for callers that don't track them.
        input_tokens = state.get("llm_input_tokens", 0) or 0
        output_tokens = state.get("llm_output_tokens", 0) or 0
        result = content_engine._handle_ai_defense_response_block(
            session_id=state["session_id"],
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            conversation_messages=state.get("messages", []),
            inspection=inspection,
            start_time=state["start_time"],
            client_address=state.get("client_address"),
            enduser_id=state.get("enduser_id"),
            llm_model=state.get("llm_model"),
            usage_data={
                "usage_input_tokens": input_tokens,
                "usage_output_tokens": output_tokens,
                "usage_total_tokens": input_tokens + output_tokens,
            },
            governance_overrides=governance_identity_overrides(state),
        )

    return {
        "terminal": True,
        "result": result,
        "stage_timings": _stage_timing(state, "response_defense", started),
    }
