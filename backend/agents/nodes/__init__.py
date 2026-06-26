"""Shared specialist node factories for the per-theme agent subgraphs.

Every node is a function ``(state: DemoBotState) -> dict`` returning a partial
state update (LangGraph merges it). Nodes that fully handle the turn set
``terminal=True`` and ``result`` (the ChatResponse-shaped dict) to short-circuit
to END, mirroring the early ``return``s in the legacy pipeline.

Deterministic specialists reuse the existing, battle-tested services
(``EscalationRules``, ``ClarifyingQuestionsService``, ``AIDefenseClient``,
``GovernanceLogger``) and the engine's content helpers, so the Splunk governance
contract is preserved exactly.
"""
