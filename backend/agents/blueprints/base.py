"""Blueprint primitives.

A *blueprint* is the generation core of a theme subgraph: the nodes that turn
the (already policy-screened) user message into ``final_message`` and friends.
Everything around that core — the PRE guardrail chain that screens the prompt
and the POST guardrail chain that screens the answer, injects test content,
formats the display text and logs governance — is shared and wired identically
around every blueprint by :func:`backend.agents.blueprints.guardrails.wire_guardrails`.
That is what "feature parity between blueprints" means mechanically: a
guardrail, toggle or governance field is implemented once, in the chain, and
every blueprint gets it. See CLAUDE.md "Blueprint feature parity".

A core is a function ``build_generation_core(g, theme_config) -> (entry, exit)``
that adds its nodes and internal edges to the ``StateGraph`` and returns the
names of its first and last node. The chain routes into ``entry`` and out of
``exit`` with the usual ``terminal`` short-circuit. The core must leave the
state contract the POST chain and governance read:

    final_message, severity, confidence, messages, requested_categories,
    llm_response_id, llm_model, llm_input_tokens, llm_output_tokens,
    llm_output_tokens_cached, llm_output_tokens_uncached, llm_stop_reason,
    agent_trace   (plus terminal/result on a short-circuit)

and must honor the per-request flags every blueprint honors
(``multi_agent_mode``, the ``force_*_injection`` directives via
``build_input_directives``) so a toggle behaves the same whichever
architecture is selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Tuple

# (StateGraph, ThemeConfig) -> (entry_node_name, exit_node_name)
GenerationCoreBuilder = Callable[[object, object], Tuple[str, str]]

# Keys the POST guardrail chain + governance node read from the state. A core
# that fails to set one of these breaks parity; tests/test_blueprint_parity.py
# asserts each core produces them on a happy-path turn.
CORE_STATE_CONTRACT: Tuple[str, ...] = (
    "final_message",
    "severity",
    "confidence",
    "messages",
    "requested_categories",
    "llm_response_id",
    "llm_model",
    "llm_input_tokens",
    "llm_output_tokens",
    "llm_output_tokens_cached",
    "llm_output_tokens_uncached",
    "llm_stop_reason",
    "agent_trace",
)


@dataclass(frozen=True)
class Blueprint:
    """One selectable agentic architecture."""

    key: str
    label: str
    description: str
    # Name promoted to the OTel GenAI Workflow span + the governance event's
    # workflow_name, so Splunk AI Agent Monitoring / Galileo separate the two
    # architectures.
    workflow_name: str
    build_generation_core: GenerationCoreBuilder
    # Friendly per-node loading labels the chat UI shows for the core's stage
    # frames (the shared guardrail stages have their own labels in chat.js).
    stage_labels: Dict[str, str] = field(default_factory=dict)
    # Node names the core registers, in execution order — for the UI and the
    # parity test's static check.
    core_nodes: Tuple[str, ...] = ()
    # What the Multi-Agent Mode toggle means for this core (shown on the card).
    multi_agent_note: str = ""

    def to_public(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "workflow_name": self.workflow_name,
            "stage_labels": dict(self.stage_labels),
            "core_nodes": list(self.core_nodes),
            "multi_agent_note": self.multi_agent_note,
        }
