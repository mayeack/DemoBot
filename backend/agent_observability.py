"""Splunk Agent Observability emission of completed chat turns.

Each completed chat turn — the governance ``chat``/``output`` event, the one
chokepoint that carries the safety / PII / toxicity / policy / evaluation picture —
is sent to Splunk Agent Observability (the ``splunk-ao`` SDK in Observability
Cloud mode: OTLP/HTTP to ``ingest.<realm>.observability.splunkcloud.com``) as one
trace::

    workflow "chat_turn"                      governance metadata on every span
      agent <coordinator>   (AgentType.supervisor)
        llm  <model>        real token usage + wall time
      agent <specialist> ... / <synthesizer>
        llm  ...

or, for a turn without an ``agent_trace`` (legacy engine, blocked turns), the
workflow span wrapping a single LLM span. Every span carries the governance
metadata plus ``pseudoco_assistant_trace_id`` — the uuid that also rides on the app's own
OTel spans as ``pseudoco-assistant.trace_id`` — so an Agent Observability trace can be joined
back to Splunk APM and the governance logs. PseudoCo Assistant's ``session_id`` (one
conversation) is mapped to an Agent Observability session, best-effort.

Why the SDK logger and not the LangChain callback: the governance flags are
computed by the safety / injection / governance graph nodes *after* the domain
agent's LLM call, so a callback (which fires when that call returns) cannot carry
them. (Raw ``gen_ai.*`` spans still reach Agent Observability through the OTel
Collector overlay ``otel-collector-agent-obs.yaml`` for the model/token view.)

Agent stream per theme: a turn is logged to the stream named after its theme's
own label (``medadvice`` -> ``MedAdvice``), so each vertical is its own Agent
stream in the console rather than every theme sharing one. The label is read from
the theme registry, so a new theme needs no change here; an unresolvable theme
falls back to ``SPLUNK_AO_AGENT_STREAM``, and setting
``SPLUNK_AO_AGENT_STREAM_PER_THEME=False`` pins every turn to that one stream.

Lifecycle: one long-lived ``SplunkAOLogger`` per agent stream (the stream is
fixed at construction), owned by a single daemon worker thread that drains a
bounded queue; the request path does an env check and a ``put_nowait`` — nothing
else. Loggers are built lazily on the first turn of a theme and are bounded by
the registry, so an idle theme costs nothing. Verified against splunk-ao 0.4.0: the
logger is not thread-safe, every instance owns a BatchSpanProcessor thread, a
private TracerProvider and atexit hooks, and the SDK's own singleton keys loggers
by *thread name* — so per-turn construction leaks and a per-turn thread would
mint a new logger every time.

Fully defensive: a no-op when the ``splunk_ao`` package is missing or
``SPLUNK_AO_O11Y_TOKEN`` / ``SPLUNK_AO_REALM`` are unset; never raises into a
chat turn; never adds request latency. Never imports the legacy ``galileo``
package (kept installed only for ``scripts/demo/galileo_*.py``). TLS uses the CA
bundle that ``backend.config`` sets via ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``
at import.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

QUEUE_MAXSIZE = 500
_MAX_CONSECUTIVE_FAILURES = 5      # then terminate + rebuild the logger
_BUILD_BACKOFF_S = 60.0            # after a failed SplunkAOLogger() build
_SESSION_BACKOFF_S = 300.0         # after start_session fails
_SESSION_CACHE_SIZE = 512
_DRAIN_ON_SHUTDOWN_S = 5.0
_FAILURE_TRACEBACK_EVERY_S = 60.0
_SDK_LOGGER = "splunk_ao"
_DEFAULT_PROJECT = "PseudoCo Assistant"
_DEFAULT_AGENT_STREAM = "PseudoCo Assistant"
_ROOT_SPAN_NAME = "chat_turn"
_STOP = object()
_WAKE = object()

# Fields from the governance log JSON carried into Agent Observability as span
# metadata (the SDK does not export the trace envelope, so metadata rides on the
# workflow / agent / llm spans).
_GOVERNANCE_KEYS = (
    "session_id", "request_id", "conversation_id",
    "provider_name", "request_model", "response_model",
    "service_name", "deployment_id", "enduser_id",
    "safety_violated", "safety_categories", "guardrail_triggered",
    "policy_blocked", "pii_detected", "pii_types",
    "toxic_detected", "toxic_types",
    "evaluation_score_value", "evaluation_score_label",
    "response_finish_reasons", "client_operation_duration",
    # The LLM span carries only a flat output-token count, so the cache split
    # rides in the span metadata instead (the two sum to that count).
    "usage_output_tokens_cached", "usage_output_tokens_uncached",
)


class TurnEmitError(RuntimeError):
    """start_trace failed (returned None or raised) — the turn cannot be built."""


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
@dataclass
class _Runtime:
    queue: "queue.Queue[Any]"
    lock: threading.Lock
    stop_event: threading.Event
    thread: Optional[threading.Thread] = None
    generation: int = 0                 # bumped by reconfigure(); read under lock
    dropped: int = 0
    drop_warned: bool = False
    # ---- worker-owned below: touched only on the worker thread ----
    # One SDK logger per agent stream: the stream is fixed at construction, so a
    # per-theme stream means a logger per theme. Bounded by the theme registry
    # (see _stream_for), built lazily on the first turn of that theme.
    loggers: "Dict[str, Any]" = field(default_factory=dict)
    logger_generation: int = -1
    build_backoff_until: float = 0.0
    last_build_error: str = ""
    consecutive_failures: int = 0
    last_failure_traceback_at: float = 0.0
    sessions: "OrderedDict[str, str]" = field(default_factory=OrderedDict)
    sessions_unavailable_until: float = 0.0
    session_warned: bool = False
    # Surfaced by status(): a sessions 401 used to be invisible outside the log,
    # which is how an unset SPLUNK_AO_O11Y_API_TOKEN went unnoticed for weeks
    # (turns kept logging, just ungrouped). None = not attempted yet.
    sessions_ok: Optional[bool] = None
    sessions_last_error: str = ""
    turns_logged: int = 0


def _new_runtime(maxsize: int = QUEUE_MAXSIZE) -> _Runtime:
    return _Runtime(queue=queue.Queue(maxsize=maxsize), lock=threading.Lock(),
                    stop_event=threading.Event())


_rt = _new_runtime()


# ---------------------------------------------------------------------------
# Public API (request path + hooks)
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    """Emission is on only while ``SPLUNK_AO_O11Y_TOKEN`` and ``SPLUNK_AO_REALM``
    are both set in the process environment — read at CALL time, so a Settings
    save applies on the next turn — and the SDK kill switch
    ``SPLUNK_AO_LOGGING_DISABLED`` is not truthy (with it set every SDK method
    returns None and every turn would count as a failure). Explicit variables
    only: no fallback to ``SPLUNK_REALM`` / ``O11Y_INGEST``."""
    if not (os.getenv("SPLUNK_AO_O11Y_TOKEN") and os.getenv("SPLUNK_AO_REALM")):
        return False
    return os.getenv("SPLUNK_AO_LOGGING_DISABLED", "false").strip().lower() not in ("true", "1", "t")


def maybe_log_turn(log_data: Dict[str, Any]) -> None:
    """Entry point called by ``governance_logger._write_log`` for EVERY event.
    Gates to completed chat turns, copies, enqueues. Never blocks, never raises."""
    if not is_enabled():
        return
    if log_data.get("operation_name") != "chat" or log_data.get("token_type") != "output":
        return
    try:
        _ensure_worker()
        _rt.queue.put_nowait(dict(log_data))
    except queue.Full:
        _rt.dropped += 1
        if not _rt.drop_warned:
            logger.warning("agent observability: queue full (%d); dropping turns until it drains",
                           _rt.queue.maxsize)
            _rt.drop_warned = True
        else:
            logger.debug("agent observability: dropped turn (%d total)", _rt.dropped)
    except Exception:  # noqa: BLE001 - must never break a chat turn
        logger.debug("agent observability: enqueue failed", exc_info=True)


def reconfigure() -> None:
    """Settings changed (``settings_store._reconfigure_integration``): retire the
    live logger and rebuild lazily from the CURRENT environment on the next turn
    (new token / realm / project / agent stream). Non-blocking — safe from the
    event loop. Does not start the worker (nothing to retire if it never ran)."""
    with _rt.lock:
        _rt.generation += 1
        started = _rt.thread is not None and _rt.thread.is_alive()
    if started:
        try:
            _rt.queue.put_nowait(_WAKE)   # wake an idle worker so the stale exporter dies now
        except queue.Full:
            pass                          # the generation check runs before the next turn anyway


def shutdown(timeout: float = 10.0) -> None:
    """``main.py`` shutdown hook: drain briefly, terminate the logger. Bounded and
    idempotent."""
    with _rt.lock:
        t = _rt.thread
    if t is None or not t.is_alive():
        return
    _rt.stop_event.set()
    try:
        _rt.queue.put(_STOP, timeout=1.0)
    except queue.Full:
        pass                              # stop_event is checked on every idle tick
    t.join(timeout)
    if t.is_alive():
        logger.warning("agent observability: worker did not stop within %.0fs", timeout)


def status() -> Dict[str, Any]:
    """Diagnostics snapshot (never raises)."""
    with _rt.lock:
        alive = _rt.thread is not None and _rt.thread.is_alive()
    return {
        "enabled": is_enabled(),
        "worker_alive": alive,
        "queued": _rt.queue.qsize(),
        "dropped": _rt.dropped,
        "turns_logged": _rt.turns_logged,
        "logger_ready": bool(_rt.loggers),
        "last_build_error": _rt.last_build_error,
        "sessions_cached": len(_rt.sessions),
        "sessions_ok": _rt.sessions_ok,
        "sessions_last_error": _rt.sessions_last_error,
        "project": os.getenv("SPLUNK_AO_PROJECT") or _DEFAULT_PROJECT,
        "agent_stream": _default_stream(),
        "agent_stream_per_theme": _per_theme_streams(),
        "agent_streams_live": sorted(_rt.loggers),
    }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def _ensure_worker() -> None:
    if _rt.thread is not None and _rt.thread.is_alive():
        return
    with _rt.lock:
        if _rt.thread is not None and _rt.thread.is_alive():
            return
        _rt.stop_event.clear()
        _rt.thread = threading.Thread(target=_worker_loop, name="agent-observability", daemon=True)
        _rt.thread.start()


def _worker_loop() -> None:
    while True:
        try:
            item = _rt.queue.get(timeout=1.0)
        except queue.Empty:
            if _rt.stop_event.is_set():
                break
            _maybe_retire_logger()        # idle tick: a Settings save retires the stale exporter within ~1 s
            continue
        try:
            if item is _STOP:
                break
            if item is _WAKE:
                _maybe_retire_logger()
                continue
            _process_turn(item)
        except BaseException:              # noqa: BLE001 - the loop must survive anything
            logger.warning("agent observability: unexpected worker error", exc_info=True)
        finally:
            _rt.queue.task_done()
    # shutdown: bounded drain, then release the SDK's threads
    deadline = time.monotonic() + _DRAIN_ON_SHUTDOWN_S
    while time.monotonic() < deadline:
        try:
            item = _rt.queue.get_nowait()
        except queue.Empty:
            break
        try:
            if item is not _STOP and item is not _WAKE:
                _process_turn(item)
        except BaseException:              # noqa: BLE001
            logger.debug("agent observability: drain error", exc_info=True)
        finally:
            _rt.queue.task_done()
    _terminate_logger()


# ---------------------------------------------------------------------------
# Agent stream selection (worker thread only)
# ---------------------------------------------------------------------------
def _default_stream() -> str:
    """The stream for turns whose theme cannot be resolved, and the one the
    collector overlay sends its raw gen_ai spans to."""
    return os.getenv("SPLUNK_AO_AGENT_STREAM") or _DEFAULT_AGENT_STREAM


def _per_theme_streams() -> bool:
    """True unless ``SPLUNK_AO_AGENT_STREAM_PER_THEME`` is falsey, which pins
    every turn to ``SPLUNK_AO_AGENT_STREAM``."""
    return os.getenv("SPLUNK_AO_AGENT_STREAM_PER_THEME", "true").strip().lower() \
        not in ("false", "0", "f", "no", "off")


def _stream_for(log_data: Dict[str, Any]) -> str:
    """Agent stream for this turn: the theme's own label — ``medadvice`` ->
    ``MedAdvice`` — so each vertical lands in its own stream in the console
    instead of every theme sharing one.

    The label comes from the theme REGISTRY, never from the raw request value:
    an unknown (or hostile) theme falls back to the default stream, so the
    number of live loggers is bounded by the number of themes + 1."""
    if not _per_theme_streams():
        return _default_stream()
    theme = log_data.get("theme")
    if not theme:
        return _default_stream()
    try:
        from backend.agents.themes import THEMES   # lazy: keeps this module importable standalone
    except Exception:  # noqa: BLE001
        return _default_stream()
    cfg = THEMES.get(str(theme).strip().lower())
    return cfg.label if cfg is not None else _default_stream()


# ---------------------------------------------------------------------------
# Logger lifecycle (worker thread only)
# ---------------------------------------------------------------------------
def _maybe_retire_logger() -> None:
    """Terminate every live logger if ``reconfigure()`` bumped the generation
    since they were built; also forget sessions (they belong to a project/stream),
    failure counters, backoffs and one-time-warning flags."""
    with _rt.lock:
        gen = _rt.generation
    if _rt.loggers and _rt.logger_generation != gen:
        logger.info("agent observability: configuration changed; retiring %d logger(s)",
                    len(_rt.loggers))
        _terminate_logger()
        _rt.sessions.clear()
        _rt.sessions_unavailable_until = 0.0
        _rt.session_warned = False
        _rt.consecutive_failures = 0
        _rt.build_backoff_until = 0.0
        _rt.last_build_error = ""


def _terminate_logger() -> None:
    """Terminate every stream's logger (an ingest problem is never one stream's)."""
    loggers, _rt.loggers = list(_rt.loggers.values()), {}
    for lg in loggers:
        try:
            lg.terminate()                # idempotent; force_flush (<=30 s) + sink shutdown
        except Exception:  # noqa: BLE001
            logger.debug("agent observability: terminate failed", exc_info=True)


def _ensure_logger(stream: str):
    """Return the live ``SplunkAOLogger`` for ``stream``, building it lazily.
    ``None`` when a build failed recently (60 s backoff, shared across streams
    because the cause is the token/realm/ingest) — the caller drops the turn."""
    _maybe_retire_logger()
    lg = _rt.loggers.get(stream)
    if lg is not None:
        return lg
    now = time.monotonic()
    if now < _rt.build_backoff_until:
        return None
    with _rt.lock:
        gen = _rt.generation              # capture BEFORE building: a reconfigure() during the build retires it next tick
    try:
        lg = _build_logger(stream)
    except Exception as exc:  # noqa: BLE001 - MissingConfigurationError, AmbiguousConfigurationError, ImportError, ...
        msg = f"{type(exc).__name__}: {exc}"
        if msg != _rt.last_build_error:   # one WARNING per distinct cause
            logger.warning("agent observability: cannot build SplunkAOLogger (%s); retrying in %.0fs",
                           msg, _BUILD_BACKOFF_S, exc_info=True)
            _rt.last_build_error = msg
        _rt.build_backoff_until = now + _BUILD_BACKOFF_S
        return None
    _rt.loggers[stream] = lg
    _rt.logger_generation, _rt.last_build_error = gen, ""
    logger.info("agent observability: logger ready (realm=%s, project=%s, agent_stream=%s)",
                os.getenv("SPLUNK_AO_REALM"), getattr(lg, "project_name", None),
                getattr(lg, "agent_stream_name", None))
    return lg


def _build_logger(stream: str):
    """Construct the SDK logger from the current environment. Observability Cloud
    mode is auto-detected from ``SPLUNK_AO_REALM`` / ``SPLUNK_AO_O11Y_TOKEN``; no
    network call happens here (the exporter is built, the project and agent
    stream are created server-side on first ingest)."""
    # The SDK mutes its own "splunk_ao" logger tree unless a level is configured;
    # ingest/auth problems would otherwise be invisible in the app log. Set both
    # the env knob the SDK honours and the logger level before the first import.
    os.environ.setdefault("SPLUNK_AO_LOG_LEVEL", "WARNING")
    sdk_log = logging.getLogger(_SDK_LOGGER)
    if sdk_log.level == logging.NOTSET or sdk_log.level > logging.WARNING:
        sdk_log.setLevel(logging.WARNING)
    from splunk_ao import SplunkAOLogger  # lazy: keeps the module importable without the package
    return SplunkAOLogger(
        project=os.getenv("SPLUNK_AO_PROJECT") or _DEFAULT_PROJECT,
        agent_stream=stream,
    )


# ---------------------------------------------------------------------------
# Per-turn processing (worker thread only)
# ---------------------------------------------------------------------------
def _process_turn(log_data: Dict[str, Any]) -> None:
    stream = _stream_for(log_data)
    lg = _ensure_logger(stream)
    if lg is None:
        _rt.dropped += 1
        return
    model = log_data.get("response_model") or log_data.get("request_model") or "unknown"
    agents = len(log_data.get("agent_trace") or []) or 1
    try:
        _recover_dangling(lg)
        ao_sid = _session_for(lg, stream, log_data.get("session_id"))
        if ao_sid:
            lg.set_session(ao_sid)
        else:
            lg.clear_session()            # never let the previous turn's session leak onto this one
        _build_turn(lg, log_data)
        flush_errors: List[BaseException] = []
        lg.flush(on_error=flush_errors.append)
        _rt.consecutive_failures = 0
        _rt.turns_logged += 1
        logger.info(
            "agent observability: logged turn (model=%s, agents=%s, project=%s, agent_stream=%s, export=%s)",
            model, agents, getattr(lg, "project_name", None), getattr(lg, "agent_stream_name", None),
            _export_label(lg, flush_errors),
        )
    except Exception:  # noqa: BLE001 - emission must never escape the worker loop
        _rt.consecutive_failures += 1
        _log_turn_failure(model)
        try:
            lg.reset_parent_tracking()
        except Exception:  # noqa: BLE001
            pass
        if _rt.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "agent observability: %d consecutive failures; terminating and rebuilding the logger in %.0fs",
                _rt.consecutive_failures, _BUILD_BACKOFF_S,
            )
            _terminate_logger()
            _rt.consecutive_failures = 0
            _rt.build_backoff_until = time.monotonic() + _BUILD_BACKOFF_S


def _export_label(lg, flush_errors) -> str:
    """``healthy`` | ``rejected(n)`` | ``unknown`` | ``flush-error(...)`` from the
    SDK's ``export_health``. ``unknown`` also follows transport failures (401,
    DNS, timeout): the SDK records unknown on non-2xx and the real cause is
    logged by the OTel exporter under
    ``opentelemetry.exporter.otlp.proto.http.trace_exporter``."""
    if flush_errors:
        return f"flush-error({flush_errors[-1]})"[:160]
    health = getattr(lg, "export_health", None)
    healthy = getattr(health, "healthy", None)
    if healthy is True:
        return "healthy"
    if healthy is False:
        return f"rejected({getattr(health, 'consecutive_failures', 0)})"
    return "unknown"


def _recover_dangling(lg) -> None:
    """A previous build that died half-way leaves a parent on the logger; in
    splunk-ao 0.4.0 ``start_trace`` then RAISES ``ValueError`` (not swallowed)."""
    try:
        dangling = bool(lg.has_active_trace()) or lg.current_parent() is not None
    except Exception:  # noqa: BLE001
        dangling = True
    if dangling:
        logger.debug("agent observability: concluding a dangling trace")
        lg.conclude(conclude_all=True)
        lg.reset_parent_tracking()


def _session_for(lg, stream: str, session_id) -> Optional[str]:
    """PseudoCo Assistant ``session_id`` -> Agent Observability session id, once per session
    (LRU of 512). Keyed by stream as well: a session belongs to one agent stream,
    so the same chat session seen under two themes needs one session per stream.
    Best-effort: on failure warn once, back off five minutes and return None (the
    turn is still logged, just without a session)."""
    if not session_id:
        return None
    sid = str(session_id)
    key = f"{stream}\x00{sid}"
    cached = _rt.sessions.get(key)
    if cached:
        _rt.sessions.move_to_end(key)
        return cached
    now = time.monotonic()
    if now < _rt.sessions_unavailable_until:
        return None
    try:
        ao = lg.start_session(name=f"chat session {sid[:8]}", external_id=sid)
        if not ao:
            raise RuntimeError("start_session returned None")
    except Exception as exc:  # noqa: BLE001 - CRUD 401/403, project lookup, ...
        # The SDK's own message tells you to set SPLUNK_AO_API_KEY. Do not: that
        # is the standalone-mode variable, and resolve_deployment() raises
        # AmbiguousConfigurationError when it is set alongside an O11y one. The
        # sessions API is reached with SPLUNK_AO_O11Y_API_TOKEN, and when that is
        # unset the SDK silently falls back to the ingest token (crud_token in
        # splunk_ao/deployment.py), which the API rejects. Say so plainly.
        detail = f"{type(exc).__name__}: {exc}"
        if not os.getenv("SPLUNK_AO_O11Y_API_TOKEN"):
            detail = ("SPLUNK_AO_O11Y_API_TOKEN is not set, so the SDK fell back to the "
                      "ingest token, which the sessions API rejects. Set an Observability "
                      "Cloud API token with Agent Observability access and restart the app "
                      f"(underlying error: {detail})")
        _rt.sessions_ok = False
        _rt.sessions_last_error = detail
        if not _rt.session_warned:
            logger.warning(
                "agent observability: sessions unavailable (%s); logging turns without a session, retry in %d min",
                detail, int(_SESSION_BACKOFF_S // 60),
                # Traceback only when the cause is not the known missing-token
                # case; that one is fully explained by the message above.
                exc_info=bool(os.getenv("SPLUNK_AO_O11Y_API_TOKEN")),
            )
            _rt.session_warned = True
        _rt.sessions_unavailable_until = now + _SESSION_BACKOFF_S
        return None
    _rt.sessions_ok = True
    _rt.sessions_last_error = ""
    _rt.session_warned = False
    _rt.sessions[key] = str(ao)
    while len(_rt.sessions) > _SESSION_CACHE_SIZE:
        _rt.sessions.popitem(last=False)
    return str(ao)


def _log_turn_failure(model: str) -> None:
    """WARNING (not debug): an outage leaves the Agent Observability pillar silently
    empty during a workshop, and debug is below the default console threshold. A
    traceback at most once a minute, a one-liner otherwise."""
    now = time.monotonic()
    with_tb = now - _rt.last_failure_traceback_at >= _FAILURE_TRACEBACK_EVERY_S
    if with_tb:
        _rt.last_failure_traceback_at = now
    logger.warning("agent observability: emit failed (model=%s, consecutive=%d)",
                   model, _rt.consecutive_failures, exc_info=with_tb)


# ---------------------------------------------------------------------------
# Pure turn builder (the test seam)
# ---------------------------------------------------------------------------
def _build_turn(lg, log_data: Dict[str, Any]) -> None:
    """Emit one turn through ``lg`` (anything with the SplunkAOLogger span API).
    No module state, no session handling, no flush, no logging — tests drive it
    with a fake logger. Raises ``TurnEmitError`` when ``start_trace`` fails.

    The SDK never exports the trace envelope: the ``chat_turn`` workflow span is
    the root that lands in Agent Observability, so the governance metadata rides on
    it and on every child, and durations/timestamps are set per span (a child's
    default ``created_at`` is "now" at emission time, i.e. after the turn)."""
    inp = _text(log_data.get("input_messages")) or log_data.get("user_prompt", "") or "(empty)"
    out = _text(log_data.get("output_messages")) or log_data.get("response_text", "") or "(empty)"
    model = log_data.get("response_model") or log_data.get("request_model") or "unknown"
    meta = {k: _coerce(log_data.get(k)) for k in _GOVERNANCE_KEYS if log_data.get(k) is not None}
    if log_data.get("trace_id"):
        meta["pseudoco_assistant_trace_id"] = str(log_data["trace_id"])
    agent_trace = log_data.get("agent_trace") or []
    request_id = log_data.get("request_id")
    turn_ns = _seconds_to_ns(log_data.get("client_operation_duration"))
    turn_end = _parse_ts(log_data.get("timestamp"))
    turn_start = (turn_end - timedelta(microseconds=turn_ns / 1000)) if (turn_end and turn_ns) else turn_end

    try:
        trace = lg.start_trace(input=inp, name="chat turn", metadata=meta, created_at=turn_start,
                               external_id=str(request_id) if request_id else None)
    except Exception as exc:  # noqa: BLE001 - ValueError on a dangling parent is NOT swallowed in 0.4.0
        raise TurnEmitError(f"start_trace raised {type(exc).__name__}: {exc}") from exc
    if trace is None:                     # swallowed infrastructure error or SPLUNK_AO_LOGGING_DISABLED
        raise TurnEmitError("start_trace returned None")

    lg.add_workflow_span(input=inp, output=out, name=_ROOT_SPAN_NAME, metadata=meta,
                         created_at=turn_start, duration_ns=turn_ns)
    if agent_trace:
        _add_agent_spans(lg, agent_trace, inp, model, meta, turn_start)
    else:
        lg.add_llm_span(input=inp, output=out, model=model, name="chat",
                        num_input_tokens=log_data.get("usage_input_tokens"),
                        num_output_tokens=log_data.get("usage_output_tokens"),
                        total_tokens=log_data.get("usage_total_tokens"),
                        duration_ns=turn_ns, created_at=turn_start, metadata=meta)
    lg.conclude(output=out, duration_ns=turn_ns)      # pop the workflow span
    lg.conclude(output=out, duration_ns=turn_ns)      # pop the trace envelope


def _add_agent_spans(lg, agent_trace, inp: str, model: str, meta: Dict[str, Any], turn_start) -> None:
    """Rebuild the multi-agent turn as nested spans under the current workflow:
    one agent span per coordinator / specialist / synthesizer call, each wrapping
    one LLM span with that agent's real token usage, back-dated sequentially by
    ``duration_ms`` so the waterfall shows the real turn timeline."""
    cursor = turn_start
    for rec in agent_trace:
        agent_out = rec.get("output_text") or "(empty)"
        dur_ns = _ms_to_ns(rec.get("duration_ms"))
        name = rec.get("name") or "agent"
        lg.add_agent_span(input=inp, output=agent_out, name=name, agent_type=_agent_type(rec.get("role")),
                          metadata=meta, created_at=cursor, duration_ns=dur_ns)
        lg.add_llm_span(input=inp, output=agent_out, model=rec.get("model") or model, name=name,
                        num_input_tokens=rec.get("input_tokens"), num_output_tokens=rec.get("output_tokens"),
                        metadata=meta, created_at=cursor, duration_ns=dur_ns)
        lg.conclude(output=agent_out, duration_ns=dur_ns)      # pop the agent span
        if cursor is not None and dur_ns:
            cursor = cursor + timedelta(microseconds=dur_ns / 1000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _coerce(value: Any):
    """Span metadata accepts str | bool | int | float | None."""
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


def _agent_type(role: Any):
    """Map an agent_trace role to the SDK's ``AgentType`` (None if unavailable)."""
    try:
        from galileo_core.schemas.logging.agent import AgentType
    except Exception:  # noqa: BLE001 - optional dependency / schema moved
        return None
    return {
        "coordinator": AgentType.supervisor,
        "specialist": AgentType.default,
        "synthesizer": AgentType.default,
    }.get(role, AgentType.default)


def _seconds_to_ns(value: Any) -> Optional[int]:
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return None
    return int(secs * 1e9) if secs > 0 else None


def _ms_to_ns(value: Any) -> Optional[int]:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    return int(ms * 1e6) if ms > 0 else None


def _parse_ts(value: Any) -> Optional[datetime]:
    """Governance timestamps are ``datetime.utcnow().isoformat()`` (naive UTC)."""
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test seams
# ---------------------------------------------------------------------------
def _reset_for_tests(maxsize: int = QUEUE_MAXSIZE) -> None:
    global _rt
    shutdown(5.0)
    _rt = _new_runtime(maxsize)


def _drain_for_tests(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _rt.queue.unfinished_tasks == 0:
            return True
        time.sleep(0.02)
    return _rt.queue.unfinished_tasks == 0
