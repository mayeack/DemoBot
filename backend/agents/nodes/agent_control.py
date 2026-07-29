"""Galileo Agent Control evaluation node (response stage).

The "Agent Observability Controls" toggle in the settings drawer. Submits the
finished answer to Galileo's Agent Control server as a post-stage ``llm`` step
and short-circuits the turn when a control returns ``deny`` — e.g. the
``DemoBot-block-hallucinated-output`` control, which fails a response whose
Galileo Correctness score falls below threshold.

Runs after ``compliance`` (so it sees the final, post-injection text) and before
``response_defense``, which keeps Cisco AI Defense as the last word on output.
A no-op unless the request opted in (``agent_control_review``) and the client is
configured; errors honor ``galileo_agent_control_fail_open`` (default: release
the response and log).
"""

from __future__ import annotations

import time
from typing import Any, Dict

from backend.agents.nodes.shared import content_engine
from backend.services.agent_control import agent_control_client
from backend.telemetry import otel


def _stage_timing(state: Dict[str, Any], stage: str, started: float) -> Dict[str, float]:
    """Merge this stage's elapsed wall-clock into the running stage_timings."""
    timings = dict(state.get("stage_timings") or {})
    timings[f"{stage}_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return timings


def agent_control_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if not (state.get("agent_control_review") and agent_control_client.is_configured):
        return {}

    started = time.perf_counter()
    with otel.agent_span("galileo_agent_control_agent", theme=state.get("theme")):
        verdict = agent_control_client.evaluate_response(
            user_message=state["user_message"],
            assistant_message=state["final_message"],
            enduser_id=state.get("enduser_id"),
            session_id=state.get("session_id"),
            theme=state.get("theme"),
            model=state.get("llm_model"),
        )
        if not verdict.should_block:
            # Non-blocking verdicts (clean, steer, observe, or a fail-open
            # error) still travel to the governance event via agent_control.
            return {
                "agent_control": verdict,
                "stage_timings": _stage_timing(state, "agent_control", started),
            }

        input_tokens = state.get("llm_input_tokens", 0) or 0
        output_tokens = state.get("llm_output_tokens", 0) or 0
        result = content_engine._handle_agent_control_block(
            session_id=state["session_id"],
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            conversation_messages=state.get("messages", []),
            verdict=verdict,
            start_time=state["start_time"],
            client_address=state.get("client_address"),
            enduser_id=state.get("enduser_id"),
            llm_model=state.get("llm_model"),
            usage_data={
                "usage_input_tokens": input_tokens,
                "usage_output_tokens": output_tokens,
                "usage_total_tokens": input_tokens + output_tokens,
            },
        )

    return {
        "terminal": True,
        "result": result,
        "agent_control": verdict,
        "stage_timings": _stage_timing(state, "agent_control", started),
    }
