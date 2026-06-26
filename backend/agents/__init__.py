"""Agentic orchestration for DemoBot.

This package rebuilds the chatbot's orchestration on LangChain + LangGraph as a
supervisor-routed, per-theme multi-agent system. The legacy
``RecommendationEngine`` is retained as a content/patterns library (PII / toxic /
hallucination injection, formatting, severity normalization, AI Defense block
handlers) so there is a single source of truth and the Splunk governance log
contract is preserved byte-for-byte.

Public entry point: :func:`backend.agents.graph.get_agentic_runner`.
"""
