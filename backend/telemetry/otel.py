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
            settings, "otel_service_name", "medadvice-v3"
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
        _STATE["tracer"] = trace.get_tracer("medadvice.agents")
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
            "medadvice.theme": theme,
            "session.id": session_id,
            "medadvice.request_id": request_id,
            "medadvice.trace_id": trace_id,
        },
    )


def agent_span(agent_name: str, *, theme: Optional[str] = None, attributes: Optional[Dict[str, Any]] = None):
    """AgentInvocation span for an agent node."""
    attrs = {
        "gen_ai.agent.name": agent_name,
        "agent_name": agent_name,
        "medadvice.theme": theme,
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
# GenAI token-usage metric.
#
# The Splunk LangChain auto-instrumentation does not surface token usage (or the
# request model) through the create_react_agent + LangChain-1.x path, so AI Agent
# Monitoring's token panels stay empty and gen_ai.client.operation.duration shows
# model="unknown_model". We emit the standard ``gen_ai.client.token.usage``
# histogram ourselves from the app's accurate NormalizedLLMResponse data, using
# the GLOBAL meter that ``opentelemetry-instrument`` configures (so it exports via
# the same OTLP -> collector -> Splunk path). No-op when the app runs without
# instrumentation (the global meter is then a no-op). This is independent of
# settings.otel_enabled / the custom TracerProvider above.
# ---------------------------------------------------------------------------
_TOKEN_HISTOGRAM: Any = None


def _token_histogram():
    global _TOKEN_HISTOGRAM
    if _TOKEN_HISTOGRAM is None:
        try:
            from opentelemetry import metrics

            meter = metrics.get_meter("medadvice.genai")
            _TOKEN_HISTOGRAM = meter.create_histogram(
                "gen_ai.client.token.usage",
                unit="token",
                description="Number of input/output tokens used per GenAI request",
            )
        except Exception:  # noqa: BLE001 - degrade to no-op
            _TOKEN_HISTOGRAM = False
    return _TOKEN_HISTOGRAM or None


def record_genai_tokens(
    *,
    model: str,
    provider: str,
    operation: str = OP_CHAT,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    """Emit ``gen_ai.client.token.usage`` for one model call (input + output series),
    carrying the correct request model and provider as dimensions."""
    hist = _token_histogram()
    if hist is None:
        return
    base = {
        "gen_ai.operation.name": operation,
        "gen_ai.request.model": model or "unknown_model",
        "gen_ai.provider.name": provider,
        "gen_ai.system": provider,
    }
    try:
        if input_tokens:
            hist.record(int(input_tokens), {**base, "gen_ai.token.type": "input"})
        if output_tokens:
            hist.record(int(output_tokens), {**base, "gen_ai.token.type": "output"})
    except Exception:  # noqa: BLE001
        logger.debug("failed to record gen_ai token usage", exc_info=True)


@contextlib.contextmanager
def genai_agent_span(*, agent_name: str, request_model: str, provider: str):
    """Emit a GenAI ``invoke_agent`` span (with gen_ai.agent.name) around an agent
    call, via the GLOBAL tracer that opentelemetry-instrument configures.

    The Splunk LangChain auto-instrumentation does not emit an agent-invocation
    span for create_react_agent (only `chat` + `invoke_workflow`), so Splunk AI
    Agent Monitoring's "AI agents" view shows no agents. This makes the named
    agent first-class. The request model is set correctly here (the auto chat
    span has gen_ai.request.model=unknown_model). No-op (yields None) when the
    app runs without instrumentation. Pair with record_llm_result() to attach the
    response model + token usage on exit."""
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("medadvice.genai")
    except Exception:  # noqa: BLE001
        yield None
        return
    with tracer.start_as_current_span(f"invoke_agent {agent_name}") as span:
        for key, value in (
            ("gen_ai.operation.name", OP_AGENT),
            ("gen_ai.agent.name", agent_name),
            ("gen_ai.request.model", request_model or "unknown_model"),
            ("gen_ai.provider.name", provider),
            ("gen_ai.system", provider),
        ):
            try:
                span.set_attribute(key, value)
            except Exception:  # noqa: BLE001
                pass
        yield span
