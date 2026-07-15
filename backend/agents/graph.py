"""LangGraph assembly: supervisor + per-theme decomposed subgraphs.

Workshop sections 4.4 ("Defining the Graph") and 4.8 ("Decomposition Pattern").

Topology:

    START -> router -> {theme}_subgraph -> END

Each ``{theme}_subgraph`` is the decomposed multi-agent pipeline:

    policy -> prompt_defense -> intake -> coordinator -> specialists
          -> synthesizer -> safety -> injection -> compliance
          -> response_defense -> governance

The multi-agent core is coordinator -> specialists -> synthesizer: the
coordinator selects 1-N themed specialists for the query, each specialist runs
as its own themed agent, and the synthesizer fuses their findings into the
final answer. Every agent emits its own GenAI AgentInvocation span, so the turn
produces a multi-agent trace.

When the request sets ``multi_agent_mode`` to False (the sidecar "Multi-Agent
Mode" toggle turned off), the edge after ``intake`` bypasses the coordinator
and specialists and routes straight to the synthesizer, which answers alone as
the theme's ``*_domain_agent``. Every guardrail node still runs in both modes.

There are conditional short-circuits to END whenever a node sets ``terminal``
(policy block, AI Defense block, clarifying question, agent generation error).

The compiled workflow is tagged with ``workflow_name`` metadata so Splunk AI
Agent Monitoring promotes it to a recognized workflow.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Iterator, Optional

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


def _route_after_intake(state: Dict[str, Any]) -> str:
    """End if short-circuited; else run the multi-agent core (default) or
    bypass it to the synthesizer when ``multi_agent_mode`` is False.

    None/absent defaults to multi-agent here — the single place the default is
    applied (mirrors ``internal_policy_review`` in nodes/policy.py).
    """
    if state.get("terminal"):
        return "end"
    return "synthesizer" if state.get("multi_agent_mode") is False else "coordinator"


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
    g.add_conditional_edges(
        "intake",
        _route_after_intake,
        {"end": END, "coordinator": "coordinator", "synthesizer": "synthesizer"},
    )
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


def _build_turn_state(
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
    multi_agent_mode: Optional[bool] = None,
    enduser_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the initial workflow state shared by run_turn / run_turn_stream."""
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
        multi_agent_mode=multi_agent_mode,
    )
    state["request_id"] = str(uuid.uuid4())
    state["trace_id"] = str(uuid.uuid4())
    state["start_time"] = time.time()
    return state


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
    multi_agent_mode: Optional[bool] = None,
    enduser_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one chat turn through the multi-agent workflow.

    Drop-in replacement for ``RecommendationEngine.process_message`` - returns a
    dict with the same shape: {message, type, severity, escalated, [policy_blocked],
    metadata}. The whole invocation is wrapped in a GenAI Workflow span.
    """
    runner = get_agentic_runner()

    state = _build_turn_state(
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
        multi_agent_mode=multi_agent_mode,
    )
    request_id = state["request_id"]
    trace_id = state["trace_id"]

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


def run_turn_stream(**kwargs: Any) -> Iterator[Dict[str, Any]]:
    """Run one chat turn, yielding a progress event per completed graph node.

    Same inputs, governance events, and telemetry as ``run_turn`` — only the
    delivery differs: the graph is driven with ``runner.stream`` so callers
    (the SSE chat endpoint) can surface live multi-agent progress instead of
    a 30-40s blank wait. Yields ``{"event": "stage", "node", "elapsed_ms"}``
    per node, then exactly one ``{"event": "final", "result": <run_turn dict>}``.

    No answer text is emitted before the full pipeline (including the
    response_defense output guardrail) has finished — stage events carry node
    names only, so nothing bypasses governance.
    """
    runner = get_agentic_runner()

    state = _build_turn_state(**kwargs)
    request_id = state["request_id"]
    trace_id = state["trace_id"]
    turn_start = state["start_time"]

    result: Optional[Dict[str, Any]] = None
    try:
        with otel.workflow_span(
            workflow_name=settings.agentic_workflow_name,
            theme=kwargs.get("theme"),
            session_id=kwargs.get("session_id"),
            request_id=request_id,
            trace_id=trace_id,
        ):
            # subgraphs=True surfaces the theme subgraph's inner nodes
            # (policy, coordinator, specialists, ...) as they complete; chunks
            # arrive as (namespace, {node: update}) tuples, top-level ones may
            # be bare {node: update} dicts depending on langgraph version.
            for chunk in runner.stream(state, stream_mode="updates", subgraphs=True):
                update = chunk[1] if isinstance(chunk, tuple) and len(chunk) == 2 else chunk
                if not isinstance(update, dict):
                    continue
                for node_name, node_update in update.items():
                    if isinstance(node_update, dict) and node_update.get("result"):
                        result = node_update["result"]
                    yield {
                        "event": "stage",
                        "node": node_name,
                        "elapsed_ms": round((time.time() - turn_start) * 1000, 1),
                    }
        if result is None:
            logger.error("Agentic workflow stream returned no result; using generic reply")
            result = _generic_error_result()
    except Exception as exc:  # noqa: BLE001 - mirror run_turn's top-level handler
        logger.exception("Agentic workflow (stream) failed: %s", exc)
        governance_logger.log_error(
            session_id=kwargs.get("session_id"),
            request_id=request_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            stack_trace=None,
            enduser_id=kwargs.get("enduser_id"),
        )
        result = _generic_error_result()
    yield {"event": "final", "result": result}
