"""Safety / escalation agent node.

Evaluates the deterministic escalation rules against the conversation, the
model's severity, and its confidence. Sets ``should_escalate`` /
``escalation_reasons`` on the state for the governance node to log.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.agents.nodes.shared import escalation_rules
from backend.telemetry import otel


def safety_node(state: Dict[str, Any]) -> Dict[str, Any]:
    conversation_history = state.get("conversation_history", [])
    user_message = state["user_message"]

    # A scheduling-intent turn is administrative (docs/scheduling.md): the rules
    # would re-read the whole conversation (an earlier emergency already escalated
    # on its own turn) and "see a doctor" phrasing would flag a booking
    # confirmation for review. Answer turns keep the full safety path.
    if (state.get("scheduling_context") or {}).get("action"):
        return {"should_escalate": False, "escalation_reasons": []}

    with otel.agent_span("safety_agent", theme=state.get("theme")):
        should_escalate, escalation_reasons = escalation_rules.should_escalate(
            conversation_history=conversation_history
            + [{"role": "user", "content": user_message}],
            severity=state["severity"],
            user_input=user_message,
            ai_confidence=state.get("confidence", 0.5),
        )

    return {
        "should_escalate": should_escalate,
        "escalation_reasons": escalation_reasons,
    }
