"""Governance logging agent node (happy-path terminal).

Emits the output governance event and (when escalated) the escalation event,
exactly matching the Splunk field contract produced by the legacy
``_generate_recommendation``. Reuses the same ``trace_id`` / ``request_id`` as
the OTel spans so logs and traces correlate in Splunk.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from backend.agents.nodes.shared import clarifying_service, escalation_rules
from backend.config import settings
from backend.logging.governance_logger import governance_logger
from backend.models.schemas import MessageType
from backend.telemetry import otel


def governance_node(state: Dict[str, Any]) -> Dict[str, Any]:
    session_id = state["session_id"]
    request_id = state["request_id"]
    trace_id = state["trace_id"]
    enduser_id = state.get("enduser_id")
    client_address = state.get("client_address")
    conversation_history = state.get("conversation_history", [])
    user_message = state["user_message"]

    messages = state.get("messages", [])
    final_message = state["final_message"]
    complete_display_text = state.get("complete_display_text", final_message)
    severity = state["severity"]
    confidence = state.get("confidence", 0.5)
    should_escalate = state.get("should_escalate", False)
    escalation_reasons = state.get("escalation_reasons", []) or []

    pii_injected = state.get("pii_injected", False)
    pii_types = state.get("pii_types", []) or []
    toxic_injected = state.get("toxic_injected", False)
    toxic_types = state.get("toxic_types", []) or []
    hallucination_injected = state.get("hallucination_injected", False)
    hallucination_types = state.get("hallucination_types", []) or []
    boundary_injected = state.get("boundary_injected", False)
    boundary_types = state.get("boundary_types", []) or []

    workflow_name = settings.agentic_workflow_name
    duration = time.time() - state["start_time"]

    with otel.agent_span("governance_agent", theme=state.get("theme")):
        governance_logger.log_response(
            session_id=session_id,
            request_id=request_id,
            response_id=state["llm_response_id"],
            operation_name="chat",
            input_messages=messages,
            output_messages=[{"role": "assistant", "content": final_message}],
            response_text=complete_display_text,
            usage_data={
                "usage_input_tokens": state.get("llm_input_tokens", 0),
                "usage_output_tokens": state.get("llm_output_tokens", 0),
                "usage_total_tokens": state.get("llm_input_tokens", 0)
                + state.get("llm_output_tokens", 0),
            },
            performance_data={"client_operation_duration": duration},
            response_model=state.get("llm_model"),
            response_finish_reasons=[state.get("llm_stop_reason", "end_turn")],
            safety_violated=should_escalate,
            safety_categories=escalation_reasons if should_escalate else None,
            guardrail_triggered=should_escalate,
            guardrail_ids=["escalation_rules"] if should_escalate else None,
            pii_detected=pii_injected,
            pii_types=pii_types if pii_injected else None,
            toxic_detected=toxic_injected,
            toxic_types=toxic_types if toxic_injected else None,
            evaluation_score_value=confidence,
            evaluation_score_label=(
                "high" if confidence > 0.7 else "medium" if confidence > 0.5 else "low"
            ),
            hallucination_detected=hallucination_injected,
            hallucination_types=hallucination_types if hallucination_injected else None,
            authority_violation_detected=boundary_injected,
            authority_violation_types=boundary_types if boundary_injected else None,
            severity=severity.value if severity else None,
            theme=state.get("theme"),
            agent_name=state.get("agent_name"),
            workflow_name=workflow_name,
            # Per-agent transcript (coordinator + specialists + synthesizer) so the
            # Galileo SDK path can rebuild the multi-agent trace; see
            # backend/galileo_integration.py and log_schemas.create_governance_log.
            agent_trace=state.get("agent_trace"),
            trace_id=trace_id,
            client_address=client_address,
            enduser_id=enduser_id,
        )

        if should_escalate:
            user_info = clarifying_service.extract_user_info(conversation_history)
            symptoms = escalation_rules.extract_symptoms(
                conversation_history + [{"role": "user", "content": user_message}]
            )
            governance_logger.log_escalation(
                session_id=session_id,
                request_id=request_id,
                reason="; ".join(escalation_reasons),
                severity=severity.value,
                conversation_history=conversation_history
                + [
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": final_message},
                ],
                symptoms=symptoms,
                user_demographics=user_info,
                enduser_id=enduser_id,
            )

    return {
        "terminal": True,
        "result": {
            "message": final_message,
            "type": MessageType.ESCALATION if should_escalate else MessageType.RECOMMENDATION,
            "severity": severity,
            "escalated": should_escalate,
            "metadata": {
                "confidence": confidence,
                "escalation_reasons": escalation_reasons if should_escalate else [],
            },
        },
    }
