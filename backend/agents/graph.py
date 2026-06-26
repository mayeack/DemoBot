"""LangGraph assembly: supervisor + per-theme decomposed subgraphs.

Workshop sections 4.4 ("Defining the Graph") and 4.8 ("Decomposition Pattern").

Topology:

    START -> router -> {theme}_subgraph -> END

Each ``{theme}_subgraph`` is the decomposed multi-agent pipeline:

    policy -> prompt_defense -> intake -> coordinator -> specialists
          -> synthesizer -> safety -> injection -> compliance
          -> response_defense -> governance

The multi-agent core is coordinator -> specialists -> synthesizer: the
coordinator (always) selects 1-N themed specialists for the query, each
specialist runs as its own themed agent, and the synthesizer fuses their
findings into the final answer. Every agent emits its own GenAI AgentInvocation
span, so the turn always produces a multi-agent trace.

There are conditional short-circuits to END whenever a node sets ``terminal``
(policy block, AI Defense block, clarifying question, agent generation error).

The compiled workflow is tagged with ``workflow_name`` metadata so Splunk AI
Agent Monitoring promotes it to a recognized workflow.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from langgraph.graph import END, START, StateGraph

from backend.agents.nodes.clarify import intake_node
from backend.agents.nodes.compliance import compliance_node
from backend.agents.nodes.coordinator import make_coordinator_agent
from backend.agents.nodes.defense import prompt_defense_node, response_defense_node
from backend.agents.nodes.governance import governance_node
from backend.agents.nodes.specialists import make_specialists_agent
from backend.agents.nodes.synthesizer import make_synthesizer_agent
from backend.agents.nodes.injection import injection_node
from backend.agents.nodes.policy import policy_block_node
from backend.agents.nodes.safety import safety_node
from backend.agents.state import DemoBotState, build_initial_state
from backend.agents.supervisor import route_to_theme, router_node
from backend.agents.themes import THEMES
from backend.config import settings
from backend.logging.governance_logger import governance_logger
from backend.models.schemas import MessageType, SeverityLevel
from backend.telemetry import otel

logger = logging.getLogger(__name__)


def _terminal_router(state: Dict[str, Any]) -> str:
    """Conditional-edge function: end the subgraph if a node short-circuited."""
    return "end" if state.get("terminal") else "next"


def build_theme_subgraph(theme_config):
    """Build and compile one theme's decomposed agent subgraph."""
    g = StateGraph(DemoBotState)

    g.add_node("policy", policy_block_node)
    g.add_node("prompt_defense", prompt_defense_node)
    g.add_node("intake", intake_node)
    g.add_node("coordinator", make_coordinator_agent(theme_config))
    g.add_node("specialists", make_specialists_agent(theme_config))
    g.add_node("synthesizer", make_synthesizer_agent(theme_config))
    g.add_node("safety", safety_node)
    g.add_node("injection", injection_node)
    g.add_node("compliance", compliance_node)
    g.add_node("response_defense", response_defense_node)
    g.add_node("governance", governance_node)

    g.add_edge(START, "policy")
    g.add_conditional_edges("policy", _terminal_router, {"end": END, "next": "prompt_defense"})
    g.add_conditional_edges("prompt_defense", _terminal_router, {"end": END, "next": "intake"})
    g.add_conditional_edges("intake", _terminal_router, {"end": END, "next": "coordinator"})
    g.add_conditional_edges("coordinator", _terminal_router, {"end": END, "next": "specialists"})
    g.add_conditional_edges("specialists", _terminal_router, {"end": END, "next": "synthesizer"})
    g.add_conditional_edges("synthesizer", _terminal_router, {"end": END, "next": "safety"})
    g.add_edge("safety", "injection")
    g.add_edge("injection", "compliance")
    g.add_edge("compliance", "response_defense")
    g.add_conditional_edges(
        "response_defense", _terminal_router, {"end": END, "next": "governance"}
    )
    g.add_edge("governance", END)

    return g.compile()


def build_workflow_graph():
    """Build and compile the supervisor-routed multi-agent workflow."""
    g = StateGraph(DemoBotState)
    g.add_node("router", router_node)

    route_map: Dict[str, str] = {}
    for key, theme_config in THEMES.items():
        node_name = f"{key}_subgraph"
        g.add_node(node_name, build_theme_subgraph(theme_config))
        g.add_edge(node_name, END)
        route_map[node_name] = node_name

    g.add_edge(START, "router")
    g.add_conditional_edges("router", route_to_theme, route_map)

    compiled = g.compile().with_config(
        metadata={"workflow_name": settings.agentic_workflow_name}
    )
    logger.info(
        "Agentic workflow compiled with %d theme subgraphs: %s",
        len(THEMES),
        ", ".join(THEMES.keys()),
    )
    return compiled


# Lazily-built, cached compiled workflow.
_COMPILED_WORKFLOW = None


def get_agentic_runner():
    """Return the compiled workflow, building it once on first use."""
    global _COMPILED_WORKFLOW
    if _COMPILED_WORKFLOW is None:
        _COMPILED_WORKFLOW = build_workflow_graph()
    return _COMPILED_WORKFLOW


def _generic_error_result() -> Dict[str, Any]:
    return {
        "message": (
            "I apologize, but I encountered an error. Please try again or seek "
            "immediate medical care if this is urgent."
        ),
        "type": MessageType.SAFETY_WARNING,
        "severity": SeverityLevel.MEDIUM,
        "escalated": True,
    }


def run_turn(
    *,
    session_id: str,
    user_message: str,
    conversation_history,
    client_address: Optional[str] = None,
    theme: Optional[str] = "medadvice",
    force_pii_injection: Optional[bool] = None,
    force_toxic_injection: Optional[bool] = None,
    force_hallucination_injection: Optional[bool] = None,
    force_boundary_injection: Optional[bool] = None,
    ai_defense_review: Optional[bool] = None,
    internal_policy_review: Optional[bool] = None,
    enduser_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one chat turn through the multi-agent workflow.

    Drop-in replacement for ``RecommendationEngine.process_message`` - returns a
    dict with the same shape: {message, type, severity, escalated, [policy_blocked],
    metadata}. The whole invocation is wrapped in a GenAI Workflow span.
    """
    runner = get_agentic_runner()

    request_id = str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    start_time = time.time()

    state = build_initial_state(
        session_id=session_id,
        user_message=user_message,
        conversation_history=conversation_history,
        theme=theme,
        client_address=client_address,
        enduser_id=enduser_id,
        force_pii_injection=force_pii_injection,
        force_toxic_injection=force_toxic_injection,
        force_hallucination_injection=force_hallucination_injection,
        force_boundary_injection=force_boundary_injection,
        ai_defense_review=ai_defense_review,
        internal_policy_review=internal_policy_review,
    )
    state["request_id"] = request_id
    state["trace_id"] = trace_id
    state["start_time"] = start_time

    try:
        with otel.workflow_span(
            workflow_name=settings.agentic_workflow_name,
            theme=theme,
            session_id=session_id,
            request_id=request_id,
            trace_id=trace_id,
        ):
            final_state = runner.invoke(state)
        result = final_state.get("result")
        if result is None:
            logger.error("Agentic workflow returned no result; using generic reply")
            return _generic_error_result()
        return result
    except Exception as exc:  # noqa: BLE001 - mirror legacy top-level handler
        logger.exception("Agentic workflow failed: %s", exc)
        governance_logger.log_error(
            session_id=session_id,
            request_id=request_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            stack_trace=None,
            enduser_id=enduser_id,
        )
        return _generic_error_result()
