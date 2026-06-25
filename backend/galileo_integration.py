"""Optional Galileo (LLM observability) emission.

Sends each completed chat turn to Galileo as a trace (an LLM span with model +
token usage) carrying the app's **governance metadata** (safety / PII / toxicity /
policy / evaluation), sourced from the governance logger — the one chokepoint that
has all of it.

Why GalileoLogger and not the LangChain ``GalileoCallback``: the governance flags
are computed by the safety/injection/governance graph nodes *after* the domain
agent's LLM call, so a callback (which fires when that call returns) cannot carry
them. ``GalileoLogger`` lets us attach the full governance picture. (Raw gen_ai.*
spans still flow to Galileo via the OTel Collector fan-out — see
``otel-collector-config.yaml`` — for the model/token telemetry view.)

Fully defensive: a no-op when the ``galileo`` package or ``GALILEO_API_KEY`` is
missing, or Galileo is unreachable — it can never break a chat turn. Emission runs
on a daemon thread so it never adds request latency. (TLS to api.galileo.ai uses
the CA bundle that ``backend.config`` sets via ``SSL_CERT_FILE`` at import.)
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Fields from the governance log JSON carried into Galileo as span/trace metadata.
_GOVERNANCE_KEYS = (
    "session_id", "request_id", "conversation_id",
    "provider_name", "request_model", "response_model",
    "service_name", "deployment_id", "enduser_id",
    "safety_violated", "safety_categories", "guardrail_triggered",
    "policy_blocked", "pii_detected", "pii_types",
    "toxic_detected", "toxic_types",
    "evaluation_score_value", "evaluation_score_label",
    "response_finish_reasons", "client_operation_duration",
)


def is_enabled() -> bool:
    """Galileo emission is on only when an API key is configured."""
    return bool(os.getenv("GALILEO_API_KEY"))


def _coerce(value: Any):
    """Galileo span metadata accepts str | bool | int | float | None."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _text(messages: Any) -> str:
    """Flatten input/output_messages ([{role, content}, ...]) into a string."""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, list):
        parts = []
        for m in messages:
            parts.append(str(m.get("content", "")) if isinstance(m, dict) else str(m))
        return "\n".join(p for p in parts if p)
    return str(messages or "")


def _agent_type(role: str):
    """Map an agent_trace role to a Galileo ``AgentType`` (None if unavailable)."""
    try:
        from galileo_core.schemas.logging.agent import AgentType
    except Exception:  # noqa: BLE001 - optional dependency / schema moved
        return None
    return {
        "coordinator": AgentType.supervisor,
        "specialist": AgentType.default,
        "synthesizer": AgentType.default,
    }.get(role, AgentType.default)


def _add_agent_spans(lg, agent_trace, inp: str, out: str, model: str, meta: Dict[str, Any]) -> bool:
    """Rebuild the multi-agent turn as nested spans under the current trace.

    Topology: a ``multi_agent_turn`` workflow span containing one agent span per
    coordinator/specialist/synthesizer call, each wrapping one LLM span with that
    agent's real token usage. Uses the Galileo SDK push/pop parent model
    (add_workflow_span / add_agent_span push and need a matching ``conclude``;
    add_llm_span is a leaf). Returns False when there is no trace or the installed
    SDK lacks agent/workflow spans, so the caller falls back to a single span.
    """
    if not agent_trace or not hasattr(lg, "add_agent_span") or not hasattr(lg, "add_workflow_span"):
        return False
    lg.add_workflow_span(
        input=inp or "(empty)", output=out or "(empty)",
        name="multi_agent_turn", metadata=meta,
    )
    for rec in agent_trace:
        agent_out = rec.get("output_text") or "(empty)"
        lg.add_agent_span(
            input=inp or "(empty)", output=agent_out,
            name=rec.get("name") or "agent",
            agent_type=_agent_type(rec.get("role")), metadata=meta,
        )
        lg.add_llm_span(
            input=inp or "(empty)", output=agent_out,
            model=rec.get("model") or model,
            num_input_tokens=rec.get("input_tokens"),
            num_output_tokens=rec.get("output_tokens"),
            metadata=meta,
        )
        lg.conclude(output=agent_out)  # pop this agent span
    lg.conclude(output=out or "(empty)")  # pop the workflow span
    return True


def _emit(log_data: Dict[str, Any]) -> None:
    try:
        from galileo import GalileoLogger

        inp = _text(log_data.get("input_messages")) or log_data.get("user_prompt", "")
        out = _text(log_data.get("output_messages")) or log_data.get("response_text", "")
        model = log_data.get("response_model") or log_data.get("request_model") or "unknown"
        meta = {k: _coerce(log_data.get(k)) for k in _GOVERNANCE_KEYS if log_data.get(k) is not None}
        request_id = log_data.get("request_id")
        agent_trace = log_data.get("agent_trace") or []

        lg = GalileoLogger()  # reads GALILEO_PROJECT / GALILEO_LOG_STREAM / GALILEO_API_KEY from env
        lg.start_trace(
            input=inp or "(empty)", name="chat turn", metadata=meta,
            external_id=str(request_id) if request_id else None,
        )
        # Preferred: rebuild the nested coordinator -> specialists -> synthesizer
        # trace. Falls back to a single LLM span (legacy behavior) when there is no
        # agent_trace or the SDK lacks agent spans.
        if not _add_agent_spans(lg, agent_trace, inp, out, model, meta):
            lg.add_llm_span(
                input=inp or "(empty)", output=out or "(empty)", model=model,
                num_input_tokens=log_data.get("usage_input_tokens"),
                num_output_tokens=log_data.get("usage_output_tokens"),
                total_tokens=log_data.get("usage_total_tokens"),
                metadata=meta,
            )
        lg.conclude(output=out or "(empty)")  # pop the trace
        traces = lg.flush()
        logger.info(
            "galileo: logged turn (model=%s, agents=%s, project=%s, log_stream=%s, traces=%s)",
            model, len(agent_trace) or 1, os.getenv("GALILEO_PROJECT"),
            os.getenv("GALILEO_LOG_STREAM"), len(traces) if traces else 0,
        )
    except Exception:  # noqa: BLE001 - Galileo must never break a chat turn
        logger.debug("galileo emit failed", exc_info=True)


def maybe_log_turn(log_data: Dict[str, Any]) -> None:
    """Emit a completed chat turn (governance response event) to Galileo on a
    background thread. No-op unless Galileo is configured and this is a chat
    output event."""
    if not is_enabled():
        return
    if log_data.get("operation_name") != "chat" or log_data.get("token_type") != "output":
        return
    try:
        threading.Thread(target=_emit, args=(dict(log_data),), daemon=True).start()
    except Exception:  # noqa: BLE001
        logger.debug("galileo thread spawn failed", exc_info=True)
