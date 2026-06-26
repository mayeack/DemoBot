"""Code-based OpenTelemetry GenAI instrumentation (workshop sections 6-7).

The agentic workflow is instrumented with three nested GenAI span kinds that map
onto Splunk AI Agent Monitoring's data model:

    Workflow         -> the whole graph invocation (root GenAI span)
    AgentInvocation  -> each agent node (intake, domain, safety, compliance)
    LLMInvocation    -> each model call (with token usage + model/provider)

Implementation notes:
- This module is defensive. If the OpenTelemetry SDK (or the optional
  ``opentelemetry-util-genai`` package) is not installed, or telemetry is
  disabled, every helper degrades to a no-op context manager so the application
  runs unchanged.
- Spans carry the OpenTelemetry GenAI semantic-convention attributes
  (``gen_ai.*``) so Splunk Observability Cloud recognizes and groups them. When
  ``opentelemetry-util-genai`` is present its ``TelemetryHandler`` is also
  initialized for richer GenAI emission / evaluations.
- Export endpoint/protocol/headers come from the standard ``OTEL_*`` environment
  variables (e.g. ``OTEL_EXPORTER_OTLP_ENDPOINT``).
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# Module-level telemetry state.
_STATE: Dict[str, Any] = {
    "initialized": False,
    "enabled": False,
    "tracer": None,
    "genai_handler": None,
}

# GenAI semantic-convention operation names.
OP_WORKFLOW = "workflow"
OP_AGENT = "invoke_agent"
OP_CHAT = "chat"


def _build_exporter():
    """Return an OTLP span exporter, preferring gRPC then HTTP."""
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcExporter,
        )

        return GrpcExporter()
    except Exception:  # noqa: BLE001 - fall through to HTTP
        pass
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )

        return HttpExporter()
    except Exception as exc:  # noqa: BLE001
        logger.warning("No OTLP exporter available: %s", exc)
        return None


def init_telemetry(settings) -> None:
    """Initialize the OTel tracer provider and (optional) GenAI handler.

    Idempotent and safe to call once at FastAPI startup.
    """
    if _STATE["initialized"]:
        return
    _STATE["initialized"] = True

    if not getattr(settings, "otel_enabled", False):
        logger.info("OTel GenAI telemetry disabled (settings.otel_enabled=False)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = os.getenv("OTEL_SERVICE_NAME") or getattr(
            settings, "otel_service_name", "demobot-v3"
        )
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
        )
        exporter = _build_exporter() if endpoint else None
        if exporter is None and getattr(settings, "debug", False):
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            exporter = ConsoleSpanExporter()

        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _STATE["tracer"] = trace.get_tracer("demobot.agents")
        _STATE["enabled"] = True

        # Optional: richer GenAI emission via opentelemetry-util-genai.
        try:
            from opentelemetry.util.genai.handler import get_telemetry_handler

            _STATE["genai_handler"] = get_telemetry_handler()
            logger.info("opentelemetry-util-genai TelemetryHandler initialized")
        except Exception:  # noqa: BLE001 - optional dependency
            logger.info(
                "opentelemetry-util-genai not available; using gen_ai.* span attributes"
            )

        logger.info(
            "OTel GenAI telemetry initialized (service=%s, endpoint=%s)",
            service_name,
            endpoint or "console",
        )
    except Exception as exc:  # noqa: BLE001 - never break app startup
        logger.warning("Failed to initialize OTel telemetry: %s", exc)
        _STATE["enabled"] = False


def is_enabled() -> bool:
    return bool(_STATE["enabled"] and _STATE["tracer"] is not None)


def _set_attrs(span, attributes: Optional[Dict[str, Any]]) -> None:
    if not attributes:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, value)
        except Exception:  # noqa: BLE001 - attribute typing edge cases
            span.set_attribute(key, str(value))


@contextlib.contextmanager
def _span(name: str, operation: str, attributes: Optional[Dict[str, Any]]) -> Iterator[Any]:
    if not is_enabled():
        yield None
        return
    tracer = _STATE["tracer"]
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("gen_ai.operation.name", operation)
        _set_attrs(span, attributes)
        try:
            yield span
        except Exception as exc:  # noqa: BLE001 - record + re-raise
            try:
                span.record_exception(exc)
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except Exception:  # noqa: BLE001
                pass
            raise


def workflow_span(
    *,
    workflow_name: str,
    theme: Optional[str] = None,
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
):
    """Root GenAI Workflow span for the whole graph invocation."""
    return _span(
        f"workflow {workflow_name}",
        OP_WORKFLOW,
        {
            "gen_ai.workflow.name": workflow_name,
            "workflow_name": workflow_name,
            "demobot.theme": theme,
            "session.id": session_id,
            "demobot.request_id": request_id,
            "demobot.trace_id": trace_id,
        },
    )


def agent_span(agent_name: str, *, theme: Optional[str] = None, attributes: Optional[Dict[str, Any]] = None):
    """AgentInvocation span for an agent node."""
    attrs = {
        "gen_ai.agent.name": agent_name,
        "agent_name": agent_name,
        "demobot.theme": theme,
    }
    if attributes:
        attrs.update(attributes)
    return _span(f"invoke_agent {agent_name}", OP_AGENT, attrs)


def llm_span(*, request_model: str, provider: str, attributes: Optional[Dict[str, Any]] = None):
    """LLMInvocation span for a single model call."""
    attrs = {
        "gen_ai.request.model": request_model,
        "gen_ai.system": provider,
        "gen_ai.provider.name": provider,
    }
    if attributes:
        attrs.update(attributes)
    return _span(f"chat {request_model}", OP_CHAT, attrs)


def record_llm_result(
    span,
    *,
    response_id: Optional[str] = None,
    response_model: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    finish_reason: Optional[str] = None,
) -> None:
    """Attach GenAI response/usage attributes to an LLM span."""
    if span is None:
        return
    total = None
    if input_tokens is not None or output_tokens is not None:
        total = (input_tokens or 0) + (output_tokens or 0)
    _set_attrs(
        span,
        {
            "gen_ai.response.id": response_id,
            "gen_ai.response.model": response_model,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.usage.total_tokens": total,
            "gen_ai.response.finish_reasons": finish_reason,
        },
    )


# ---------------------------------------------------------------------------
# GenAI emission via the opentelemetry-util-genai TelemetryHandler.
#
# The Splunk LangChain auto-instrumentation (a) does not emit an AgentInvocation
# for create_react_agent, so Splunk's "AI agents" view shows nothing, and (b)
# reports gen_ai.request.model as "unknown" on the create_react_agent +
# LangChain-1.x path, so server-side cost (price x tokens) can't be computed. We
# emit the GenAI entities ourselves from the app's accurate data via the shared
# TelemetryHandler, which routes through the active span_metric/splunk emitters ->
# proper Agent + LLM entities with the real model + token usage. No-op (yields
# None) when opentelemetry-util-genai is unavailable. The buggy auto langchain
# instrumentation is disabled in run.sh so these are the single source of truth.
# ---------------------------------------------------------------------------


def _get_handler():
    try:
        from opentelemetry.util.genai.handler import get_telemetry_handler

        return get_telemetry_handler()
    except Exception:  # noqa: BLE001 - optional dependency / not configured
        return None


def _genai_error(exc: Exception):
    try:
        from opentelemetry.util.genai.types import Error

        return Error(message=str(exc), type=type(exc))
    except Exception:  # noqa: BLE001
        return None


def _input_messages(system: Optional[str], messages: Optional[list]):
    """Build a util-genai InputMessage list (system prompt + conversation turns).

    Splunk AI Agent Monitoring's "AI trace data" view indexes gen_ai spans by their
    message *content* (prompt/response) — it powers the Content column and the
    quality/risk evaluations. Without messages, the spans still reach APM (and show
    in Trace Analyzer) but never surface in AI trace data. Degrades to [] if the
    optional util-genai types are unavailable.
    """
    try:
        from opentelemetry.util.genai.types import InputMessage, Text
    except Exception:  # noqa: BLE001 - optional dependency
        return []
    out = []
    if system:
        out.append(InputMessage(role="system", parts=[Text(content=str(system))]))
    for m in messages or []:
        role = m.get("role")
        role = role.value if hasattr(role, "value") else role
        content = m.get("content", "")
        if content:
            out.append(InputMessage(role=str(role or "user"), parts=[Text(content=str(content))]))
    return out


def record_genai_output(inv, *, text: Optional[str], finish_reason: Optional[str] = None) -> None:
    """Attach the model's response text to an LLM/agent invocation as an
    OutputMessage so the gen_ai span carries the completion (paired with the input
    messages set at creation). No-op when the invocation/types are unavailable."""
    if inv is None or text is None:
        return
    try:
        from opentelemetry.util.genai.types import OutputMessage, Text
    except Exception:  # noqa: BLE001
        return
    inv.output_messages = [
        OutputMessage(role="assistant", parts=[Text(content=str(text))], finish_reason=finish_reason)
    ]


@contextlib.contextmanager
def genai_llm_invocation(
    *, request_model: str, provider: str, operation: str = OP_CHAT,
    system: Optional[str] = None, messages: Optional[list] = None,
):
    """Emit a GenAI LLMInvocation via the util-genai handler, carrying the real
    request model + token usage (so the model is no longer "unknown" and Splunk
    can price it). Pass ``system`` + ``messages`` so the span carries the prompt;
    yields the LLMInvocation — set ``input_tokens`` / ``output_tokens`` /
    ``response_model_name`` / ``response_id`` and call ``record_genai_output`` on it
    inside the block — or None when the handler is unavailable."""
    handler = _get_handler()
    if handler is None:
        yield None
        return
    from opentelemetry.util.genai.types import LLMInvocation

    inv = LLMInvocation(
        request_model=request_model, operation=operation,
        provider=provider, system=provider,
        input_messages=_input_messages(system, messages),
    )
    handler.start_llm(inv)
    try:
        yield inv
    except Exception as exc:  # noqa: BLE001 - mark span errored, then re-raise
        err = _genai_error(exc)
        try:
            handler.fail_llm(inv, err) if err else handler.stop_llm(inv)
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        try:
            handler.stop_llm(inv)
        except Exception:  # noqa: BLE001
            pass


@contextlib.contextmanager
def genai_agent_invocation(
    *, agent_name: str, request_model: str, provider: str, agent_type: Optional[str] = None,
    system: Optional[str] = None, messages: Optional[list] = None,
):
    """Emit a GenAI AgentInvocation (so the named agent appears in Splunk's "AI
    agents" view) wrapping a nested LLMInvocation, via the util-genai handler. The
    handler automatically inherits the agent name/id onto the LLM and nests its
    span under the agent. Pass ``system`` + ``messages`` so both spans carry the
    prompt. Yields the nested LLMInvocation — set token usage / ``response`` via
    ``record_genai_output`` inside the block — or None when the handler is
    unavailable."""
    handler = _get_handler()
    if handler is None:
        yield None
        return
    from opentelemetry.util.genai.types import AgentInvocation, LLMInvocation

    _inputs = _input_messages(system, messages)
    agent = AgentInvocation(
        name=agent_name, model=request_model, agent_type=agent_type,
        provider=provider, system=provider,
        system_instructions=system, input_messages=list(_inputs),
    )
    inv = LLMInvocation(
        request_model=request_model, operation=OP_CHAT,
        provider=provider, system=provider,
        input_messages=list(_inputs),
    )
    handler.start_agent(agent)
    handler.start_llm(inv)
    try:
        yield inv
    except Exception as exc:  # noqa: BLE001 - mark spans errored, then re-raise
        err = _genai_error(exc)
        for fail, stop, obj in (
            (handler.fail_llm, handler.stop_llm, inv),
            (handler.fail_agent, handler.stop_agent, agent),
        ):
            try:
                fail(obj, err) if err else stop(obj)
            except Exception:  # noqa: BLE001
                pass
        raise
    else:
        # Mirror the LLM's response onto the agent span so the agent operation row
        # in AI trace data also carries the completion content.
        try:
            if getattr(inv, "output_messages", None):
                agent.output_messages = list(inv.output_messages)
        except Exception:  # noqa: BLE001
            pass
        try:
            handler.stop_llm(inv)
            handler.stop_agent(agent)
        except Exception:  # noqa: BLE001
            pass
