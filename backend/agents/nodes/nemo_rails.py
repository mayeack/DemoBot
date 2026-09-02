"""NVIDIA NeMo Guardrails nodes (input rails + output rails).

The "NeMo Guardrails" toggle in the settings drawer. Both nodes are no-ops
unless the request opted in (``nemo_guardrails_review``) and the client is
configured (``nemo_guardrails_enabled`` master switch + the rails directory).

Placement (see backend/agents/blueprints/guardrails.py):
  policy -> prompt_defense -> **nemo_input_rails** -> intake -> ...
  ... -> compliance -> agent_control -> **nemo_output_rails** -> response_defense -> governance
so Cisco AI Defense inspects the prompt first and stays the last word on output.

A fired rail withholds the turn through the same governance contract as the
other blocking guardrails (``guardrail_ids=["nemo_guardrails"]``, the rails that
fired in ``safety_categories``, ``policy_blocked=True``, a ``response_text``
banner). Errors honor ``nemo_guardrails_fail_open``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from backend.agents.state import governance_identity_overrides
from backend.logging.governance_logger import active_response_model, governance_logger
from backend.models.schemas import MessageType, SeverityLevel
from backend.services.nemo_guardrails import RailVerdict, nemo_guardrails_client
from backend.telemetry import otel

GUARDRAIL_ID = "nemo_guardrails"

_BLOCKED_INPUT = (
    "I can't help with that request. Please rephrase your question. If this "
    "is a medical emergency, call 911 or go to your nearest emergency room."
)
_BLOCKED_OUTPUT = (
    "The assistant's response was withheld by our NeMo Guardrails policy. "
    "Please rephrase your question or try again. If this is a medical "
    "emergency, call 911 or go to your nearest emergency room."
)
_ERRORED = (
    "The assistant's response could not be reviewed by our guardrails service "
    "and was withheld. Please try again in a moment. If this is a medical "
    "emergency, call 911 or go to your nearest emergency room."
)


def _stage_timing(state: Dict[str, Any], stage: str, started: float) -> Dict[str, float]:
    """Merge this stage's elapsed wall-clock into the running stage_timings."""
    timings = dict(state.get("stage_timings") or {})
    timings[f"{stage}_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return timings


def _enabled(state: Dict[str, Any]) -> bool:
    return bool(state.get("nemo_guardrails_review")) and nemo_guardrails_client.is_configured


def verdict_metadata(verdict: RailVerdict) -> Dict[str, Any]:
    return {
        "is_safe": verdict.is_safe,
        "stage": verdict.stage,
        "rails": list(verdict.rule_names),
        "reason": verdict.reason,
        "errored": verdict.errored,
        "error_message": verdict.error_message,
        "duration_ms": verdict.duration_ms,
    }


def _blocked_result(
    state: Dict[str, Any],
    verdict: RailVerdict,
    *,
    stage: str,
    input_messages: List[Dict[str, Any]],
    llm_model: Optional[str] = None,
    usage_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Withhold the turn and log the verdict — shaped exactly like the Agent
    Control / AI Defense block handlers so downstream consumers are indifferent
    to which guardrail fired. Only ``guardrail_ids`` and the metadata block
    attribute it to NeMo Guardrails."""
    duration = time.time() - state["start_time"]
    if verdict.errored:
        reasons = [f"NeMo Guardrails unavailable (fail-closed): {verdict.error_message}"]
        blocked_message = _ERRORED
    else:
        rails = ", ".join(verdict.rule_names) if verdict.rule_names else f"{stage} rails"
        reasons = [f"NVIDIA NeMo Guardrails blocked the {stage} ({rails})"]
        if verdict.reason:
            reasons.append(verdict.reason)
        blocked_message = _BLOCKED_INPUT if stage == "input" else _BLOCKED_OUTPUT

    governance_logger.log_response(
        session_id=state["session_id"],
        request_id=state["request_id"],
        response_id=f"nemo-guardrails-{stage}-blocked",
        operation_name="chat",
        input_messages=input_messages,
        output_messages=[{"role": "assistant", "content": blocked_message}],
        response_text=f"⚠️ POLICY BLOCKED (NVIDIA NeMo Guardrails - {stage})\n{blocked_message}",
        usage_data=usage_data
        or {"usage_input_tokens": 0, "usage_output_tokens": 0, "usage_total_tokens": 0},
        performance_data={"client_operation_duration": duration},
        response_model=llm_model or active_response_model(),
        response_finish_reasons=["policy_blocked"],
        safety_violated=True,
        safety_categories=list(verdict.rule_names) or reasons,
        guardrail_triggered=True,
        guardrail_ids=[GUARDRAIL_ID],
        policy_blocked=True,
        pii_detected=False,
        pii_types=[],
        toxic_detected=False,
        toxic_types=[],
        evaluation_score_value=1.0,
        evaluation_score_label="high",
        theme=state.get("theme"),
        agent_name=f"nemo_guardrails_{stage}_agent",
        trace_id=state["trace_id"],
        client_address=state.get("client_address"),
        enduser_id=state.get("enduser_id"),
        **governance_identity_overrides(state),
    )

    return {
        "message": blocked_message,
        "type": MessageType.SAFETY_WARNING,
        "severity": SeverityLevel.MEDIUM,
        "escalated": False,
        "policy_blocked": True,
        "metadata": {
            "confidence": 1.0,
            "escalation_reasons": reasons,
            "nemo_guardrails": verdict_metadata(verdict),
        },
    }


def nemo_input_rails_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if not _enabled(state):
        return {}

    started = time.perf_counter()
    with otel.agent_span("nemo_guardrails_input_agent", theme=state.get("theme")):
        verdict = nemo_guardrails_client.check_input(state["user_message"])
        if not verdict.should_block:
            return {
                "nemo_guardrails_input": verdict,
                "stage_timings": _stage_timing(state, "nemo_input_rails", started),
            }
        result = _blocked_result(
            state, verdict, stage="input",
            input_messages=[{"role": "user", "content": state["user_message"]}],
        )

    return {
        "terminal": True,
        "result": result,
        "nemo_guardrails_input": verdict,
        "stage_timings": _stage_timing(state, "nemo_input_rails", started),
    }


def nemo_output_rails_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if not _enabled(state):
        return {}

    started = time.perf_counter()
    with otel.agent_span("nemo_guardrails_output_agent", theme=state.get("theme")):
        verdict = nemo_guardrails_client.check_output(
            user_message=state["user_message"],
            assistant_message=state["final_message"],
        )
        if not verdict.should_block:
            return {
                "nemo_guardrails_output": verdict,
                "stage_timings": _stage_timing(state, "nemo_output_rails", started),
            }
        # The model already answered (that answer is what got blocked): report
        # its real id and token spend, like the AI Defense response block does.
        input_tokens = state.get("llm_input_tokens", 0) or 0
        output_tokens = state.get("llm_output_tokens", 0) or 0
        result = _blocked_result(
            state, verdict, stage="output",
            input_messages=state.get("messages", []),
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
        "nemo_guardrails_output": verdict,
        "stage_timings": _stage_timing(state, "nemo_output_rails", started),
    }
