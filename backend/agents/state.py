"""Shared state model for the MedAdvice LangGraph workflow.

Maps to the workshop's "Shared State" concept (section 4.2): a single
``TypedDict`` threaded through every node, replacing the local variables that
used to live inside ``RecommendationEngine.process_message``.

Notes on conventions:
- ``total=False`` so nodes can return partial updates (LangGraph merges them).
- ``terminal``/``result`` implement the short-circuit paths (policy block,
  AI Defense block, clarifying question) that used to be early ``return``s.
- Correlation fields (``request_id``, ``trace_id``) are generated once and
  reused across governance logs *and* OTel spans so logs and traces line up in
  Splunk.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class MedAdviceState(TypedDict, total=False):
    # ---- Inputs (set by the router from the inbound request) ----
    session_id: str
    user_message: str
    conversation_history: List[Dict[str, Any]]
    theme: str
    client_address: Optional[str]
    enduser_id: Optional[str]
    force_pii_injection: Optional[bool]
    force_toxic_injection: Optional[bool]
    force_hallucination_injection: Optional[bool]
    force_boundary_injection: Optional[bool]
    ai_defense_review: Optional[bool]
    internal_policy_review: Optional[bool]

    # ---- Correlation / timing (set by the router node) ----
    request_id: str
    trace_id: str
    start_time: float
    system_prompt: str
    agent_name: str
    conversational: bool

    # ---- Governance directive decision (domain agent, one roll per turn) ----
    # Which test-content categories were requested for this turn (pii / toxic /
    # hallucination / authority). Set PRE-LLM so the input directive and the
    # post-LLM injection fallback agree on a single decision.
    requested_categories: Dict[str, bool]

    # ---- Multi-agent stage (coordinator -> specialists -> synthesizer) ----
    # The coordinator picks 1-N specialists per query; the specialists node runs
    # each as its own themed agent; the synthesizer fuses their findings into the
    # final answer. ``agent_trace`` is the per-agent transcript (one entry per
    # coordinator/specialist/synthesizer call) used to rebuild the multi-agent
    # trace for Galileo. ``llm_*_tokens`` below are the SUM across all agents.
    selected_specialists: List[str]
    coordinator_plan: Dict[str, Any]
    specialist_outputs: List[Dict[str, Any]]
    agent_trace: List[Dict[str, Any]]

    # ---- LLM messages + raw model output (synthesizer; tokens summed) ----
    messages: List[Dict[str, Any]]
    recommendation: Dict[str, Any]
    llm_response_id: str
    llm_model: str
    llm_input_tokens: int
    llm_output_tokens: int
    llm_stop_reason: str

    # ---- Derived assessment (domain agent) ----
    severity: Any  # SeverityLevel
    confidence: float
    final_message: str
    complete_display_text: str

    # ---- Safety / escalation (safety agent) ----
    should_escalate: bool
    escalation_reasons: List[str]

    # ---- Test-injection flags (injection agent) ----
    pii_injected: bool
    pii_types: List[str]
    toxic_injected: bool
    toxic_types: List[str]
    hallucination_injected: bool
    hallucination_types: List[str]
    boundary_injected: bool
    boundary_types: List[str]

    # ---- Short-circuit + final result ----
    # ``terminal`` is set by any node that fully handled the turn (policy block,
    # AI Defense block, clarifying question). ``result`` is the
    # ChatResponse-shaped dict the router returns, identical in shape to the
    # legacy ``RecommendationEngine.process_message`` return value:
    #   {message, type, severity, escalated, [policy_blocked], metadata}
    terminal: bool
    result: Dict[str, Any]


def build_initial_state(
    *,
    session_id: str,
    user_message: str,
    conversation_history: List[Dict[str, Any]],
    theme: Optional[str] = "medadvice",
    client_address: Optional[str] = None,
    enduser_id: Optional[str] = None,
    force_pii_injection: Optional[bool] = None,
    force_toxic_injection: Optional[bool] = None,
    force_hallucination_injection: Optional[bool] = None,
    force_boundary_injection: Optional[bool] = None,
    ai_defense_review: Optional[bool] = None,
    internal_policy_review: Optional[bool] = None,
) -> MedAdviceState:
    """Build the initial graph state from an inbound chat request.

    Mirrors the argument list of the legacy
    ``RecommendationEngine.process_message`` so the router integration is a
    drop-in replacement.
    """
    return MedAdviceState(
        session_id=session_id,
        user_message=user_message,
        conversation_history=conversation_history or [],
        theme=theme or "medadvice",
        client_address=client_address,
        enduser_id=enduser_id,
        force_pii_injection=force_pii_injection,
        force_toxic_injection=force_toxic_injection,
        force_hallucination_injection=force_hallucination_injection,
        force_boundary_injection=force_boundary_injection,
        ai_defense_review=ai_defense_review,
        internal_policy_review=internal_policy_review,
        terminal=False,
    )
