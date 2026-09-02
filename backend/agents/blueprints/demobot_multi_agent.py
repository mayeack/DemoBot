"""The DemoBot multi-agent blueprint — the architecture the app shipped with.

Generation core (between the shared guardrail chains):

    intake -> synthesizer                       (default: one LLM call, the
                                                 theme's ``*_domain_agent``)
    intake -> coordinator -> specialists -> synthesizer
                                                (Multi-Agent Mode ON)

``intake`` is the rule-based clarifier (it may short-circuit with a clarifying
question); the coordinator picks 1-N themed specialists, each runs as its own
named agent, and the synthesizer fuses their findings into the theme's answer
contract. Moved verbatim from the original ``graph.build_theme_subgraph``.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from langgraph.graph import END, StateGraph

from backend.agents.blueprints.base import Blueprint
from backend.agents.nodes.clarify import intake_node
from backend.agents.nodes.coordinator import make_coordinator_agent
from backend.agents.nodes.specialists import make_specialists_agent
from backend.agents.nodes.synthesizer import make_synthesizer_agent

KEY = "demobot_multi_agent"


def _terminal_router(state: Dict[str, Any]) -> str:
    return "end" if state.get("terminal") else "next"


def _route_after_intake(state: Dict[str, Any]) -> str:
    """End if short-circuited; else route straight to the synthesizer (default)
    or run the multi-agent core when ``multi_agent_mode`` is explicitly True.

    None/absent/False defaults to single-agent here — the single place the
    default is applied (mirrors ``internal_policy_review`` in nodes/policy.py).
    """
    if state.get("terminal"):
        return "end"
    return "coordinator" if state.get("multi_agent_mode") is True else "synthesizer"


def build_generation_core(g: StateGraph, theme_config) -> Tuple[str, str]:
    g.add_node("intake", intake_node)
    g.add_node("coordinator", make_coordinator_agent(theme_config))
    g.add_node("specialists", make_specialists_agent(theme_config))
    g.add_node("synthesizer", make_synthesizer_agent(theme_config))

    g.add_conditional_edges(
        "intake",
        _route_after_intake,
        {"end": END, "coordinator": "coordinator", "synthesizer": "synthesizer"},
    )
    g.add_conditional_edges("coordinator", _terminal_router, {"end": END, "next": "specialists"})
    g.add_conditional_edges("specialists", _terminal_router, {"end": END, "next": "synthesizer"})
    return "intake", "synthesizer"


BLUEPRINT = Blueprint(
    key=KEY,
    label="DemoBot Multi-Agent",
    description=(
        "Supervisor-routed theme pipeline: a rule-based intake clarifier, then the "
        "theme's domain agent answers directly — or, with Multi-Agent Mode on, a "
        "coordinator selects themed specialists whose findings a synthesizer fuses."
    ),
    workflow_name="demobot_multi_agent",
    build_generation_core=build_generation_core,
    stage_labels={
        "intake": "Reviewing your message…",
        "coordinator": "Coordinator planning specialists…",
        "specialists": "Specialists analyzing…",
        "synthesizer": "Composing your answer…",
    },
    core_nodes=("intake", "coordinator", "specialists", "synthesizer"),
    multi_agent_note="ON = coordinator → specialists → synthesizer pipeline. OFF = the domain agent answers directly.",
)
