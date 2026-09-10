#!/usr/bin/env python3
"""Regression: the Splunk Agent Observability integration
(backend/agent_observability.py + wiring).

Guards (1) the no-op safety guarantee — without SPLUNK_AO_O11Y_TOKEN +
SPLUNK_AO_REALM (or with the SDK kill switch set) emission is a silent no-op and
never raises into a chat turn; (2) the shape of the emitted trace (one chat_turn
workflow root, one agent + llm span per agent_trace record, governance metadata on
every span, back-dated timestamps); (3) the worker lifecycle (one logger per
process, sessions cached per PseudoCo Assistant session, dangling-parent recovery, build and
session failures backed off, reconfigure retires the logger, bounded queue drops
instead of blocking, shutdown terminates); (4) the collector + app wiring, so the
integration can't regress into breaking requests or losing its export path.

The splunk_ao SDK is replaced by a recording fake in sys.modules, so nothing here
touches the network.

    venv/bin/python tests/test_agent_observability.py    # exit 0 = pass
"""
import logging
import os
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


# SPLUNK_AO_O11Y_API_TOKEN is scrubbed too: _session_for branches on it to
# explain a sessions failure, so a real value in .env would otherwise change
# which message [6] sees.
for _k in ("SPLUNK_AO_O11Y_TOKEN", "SPLUNK_AO_REALM", "SPLUNK_AO_LOGGING_DISABLED",
           "SPLUNK_AO_PROJECT", "SPLUNK_AO_AGENT_STREAM", "SPLUNK_AO_O11Y_API_TOKEN"):
    os.environ.pop(_k, None)

import backend.agent_observability as ao
from backend.agents.themes import THEMES as _THEMES

_THEME_KEYS = sorted(_THEMES)  # noqa: E402


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level=None, contains=None):
        out = []
        for r in self.records:
            if level is not None and r.levelno != level:
                continue
            msg = r.getMessage()
            if contains is not None and contains not in msg:
                continue
            out.append(r)
        return out

    def clear(self):
        self.records.clear()


_cap = _LogCapture()
_mod_logger = logging.getLogger("backend.agent_observability")
_mod_logger.addHandler(_cap)
_mod_logger.setLevel(logging.DEBUG)


# ---- fake SDK --------------------------------------------------------------
class _FakeLogger:
    instances = []
    ctor_error = None

    def __init__(self, project=None, agent_stream=None, **kw):
        if _FakeLogger.ctor_error is not None:
            raise _FakeLogger.ctor_error
        self.project_name = project
        self.agent_stream_name = agent_stream
        self.calls = []
        self.fail_start = False
        self.raise_start = None
        self.session_error = False
        self.dangling = False
        self.block_event = None
        self.flush_error = None
        self.healthy = True
        _FakeLogger.instances.append(self)

    def _rec(self, name, k):
        self.calls.append((name, k))

    def start_trace(self, **k):
        self._rec("start_trace", k)
        if self.block_event is not None:
            self.block_event.wait(10)
        if self.raise_start is not None:
            raise self.raise_start
        return None if self.fail_start else object()

    def add_workflow_span(self, **k): self._rec("add_workflow_span", k)
    def add_agent_span(self, **k): self._rec("add_agent_span", k)
    def add_llm_span(self, **k): self._rec("add_llm_span", k)
    def conclude(self, **k): self._rec("conclude", k)

    def flush(self, on_error=None):
        self._rec("flush", {})
        if self.flush_error is not None and on_error is not None:
            on_error(self.flush_error)

    def start_session(self, **k):
        self._rec("start_session", k)
        if self.session_error:
            raise RuntimeError("401 unauthorized")
        return "ao-sess-1"

    def set_session(self, session_id): self._rec("set_session", {"session_id": session_id})
    def clear_session(self): self._rec("clear_session", {})

    def reset_parent_tracking(self):
        self._rec("reset_parent_tracking", {})
        self.dangling = False

    def terminate(self): self._rec("terminate", {})
    def has_active_trace(self): return self.dangling
    def current_parent(self): return object() if self.dangling else None

    @property
    def export_health(self):
        return SimpleNamespace(healthy=self.healthy, consecutive_failures=0, last_failure=None)


_fake_sdk = types.ModuleType("splunk_ao")
_fake_sdk.SplunkAOLogger = _FakeLogger
_fake_sdk.__version__ = "0.4.0-fake"
sys.modules["splunk_ao"] = _fake_sdk

_TRACE = [
    {"name": "medadvice_coordinator", "role": "coordinator", "model": "m",
     "input_tokens": 10, "output_tokens": 5, "output_text": "plan", "status": "ok", "duration_ms": 500.0},
    {"name": "medadvice_triage_specialist", "role": "specialist", "model": "m",
     "input_tokens": 20, "output_tokens": 8, "output_text": "triage", "status": "ok", "duration_ms": 1000.0},
    {"name": "medadvice_domain_agent", "role": "synthesizer", "model": "m",
     "input_tokens": 30, "output_tokens": 40, "output_text": "final", "status": "ok", "duration_ms": 1500.0},
]
_LOG = {
    "operation_name": "chat", "token_type": "output", "request_id": "rid",
    "session_id": "sess-abc", "trace_id": "tid-1", "timestamp": "2026-09-08T10:00:03",
    "client_operation_duration": 3.0,
    "input_messages": [{"role": "user", "content": "headache"}],
    "output_messages": [{"role": "assistant", "content": "final"}],
    "response_model": "m", "usage_input_tokens": 60, "usage_output_tokens": 53,
    "usage_total_tokens": 113, "pii_detected": True, "agent_trace": _TRACE,
}


def _names(calls):
    return [c[0] for c in calls]


def _turn(**over):
    d = dict(_LOG)
    d.update(over)
    return d


def _enable():
    os.environ["SPLUNK_AO_O11Y_TOKEN"] = "test-token-unused"
    os.environ["SPLUNK_AO_REALM"] = "us1"


def _disable():
    os.environ.pop("SPLUNK_AO_O11Y_TOKEN", None)
    os.environ.pop("SPLUNK_AO_REALM", None)


def _fresh(maxsize=ao.QUEUE_MAXSIZE):
    ao._reset_for_tests(maxsize)
    _FakeLogger.instances.clear()
    _FakeLogger.ctor_error = None
    _cap.clear()


# ---- 1. no-op safety guarantee (must never raise into a chat turn) -----------
print("\n[1] no-op safety")
_disable()
check("disabled when SPLUNK_AO_O11Y_TOKEN / SPLUNK_AO_REALM unset", ao.is_enabled() is False)
os.environ["SPLUNK_AO_O11Y_TOKEN"] = "t"
check("token without realm stays disabled", ao.is_enabled() is False)
try:
    ao.maybe_log_turn(_turn())
    check("maybe_log_turn is a silent no-op when disabled", True)
except Exception:
    check("maybe_log_turn is a silent no-op when disabled", False)
check("no worker thread is started while disabled", ao._rt.thread is None)
_enable()
check("enabled when token + realm set", ao.is_enabled() is True)
os.environ["SPLUNK_AO_LOGGING_DISABLED"] = "1"
check("SPLUNK_AO_LOGGING_DISABLED is treated as disabled", ao.is_enabled() is False)
os.environ.pop("SPLUNK_AO_LOGGING_DISABLED")
try:
    ao.maybe_log_turn({"operation_name": "prompt", "token_type": "prompt"})
    check("maybe_log_turn ignores non-chat events", ao._rt.queue.qsize() == 0 and ao._rt.thread is None)
except Exception:
    check("maybe_log_turn ignores non-chat events", False)
_disable()

# ---- 2. helpers -------------------------------------------------------------
print("\n[2] helpers")
check("_coerce keeps scalars, stringifies non-scalars",
      ao._coerce(True) is True and ao._coerce(["a", "b"]) == "['a', 'b']")
check("_text flattens message lists + passes strings",
      ao._text([{"role": "user", "content": "abc"}]) == "abc" and ao._text("x") == "x")
check("coordinator role -> supervisor AgentType",
      getattr(ao._agent_type("coordinator"), "value", None) == "supervisor")
check("specialist / synthesizer / unknown roles -> default AgentType",
      all(getattr(ao._agent_type(r), "value", None) == "default" for r in ("specialist", "synthesizer", "??")))
check("_seconds_to_ns", ao._seconds_to_ns(3.0) == 3_000_000_000 and ao._seconds_to_ns(None) is None
      and ao._seconds_to_ns(0) is None)
check("_ms_to_ns", ao._ms_to_ns(12.5) == 12_500_000 and ao._ms_to_ns("x") is None)
_ts = ao._parse_ts("2026-09-08T10:00:03")
check("_parse_ts makes a naive governance timestamp UTC-aware",
      _ts is not None and _ts.tzinfo is not None and _ts == datetime(2026, 9, 8, 10, 0, 3, tzinfo=timezone.utc))
check("_parse_ts rejects garbage", ao._parse_ts("garbage") is None and ao._parse_ts(None) is None)

# ---- 3. builder shape (pure) ------------------------------------------------
print("\n[3] trace shape")
fake = _FakeLogger()
ao._build_turn(fake, _turn())
names = _names(fake.calls)
check("one chat_turn workflow root", names.count("add_workflow_span") == 1
      and fake.calls[1][1].get("name") == "chat_turn")
check("one agent span + one llm span per agent_trace record",
      names.count("add_agent_span") == 3 and names.count("add_llm_span") == 3)
check("conclude() balanced: 3 agents + workflow + trace = 5, trace last",
      names.count("conclude") == 5 and names[-1] == "conclude" and names[0] == "start_trace")
check("builder is pure: no flush / session calls",
      not any(n in ("flush", "set_session", "start_session", "clear_session") for n in names))
_start = fake.calls[0][1]
check("start_trace carries name, external_id and the back-dated created_at",
      _start.get("name") == "chat turn" and _start.get("external_id") == "rid"
      and _start.get("created_at") == datetime(2026, 9, 8, 10, 0, 0, tzinfo=timezone.utc))
_spans = [c for c in fake.calls if c[0] in ("add_workflow_span", "add_agent_span", "add_llm_span")]
check("governance metadata (pii_detected) rides on every span",
      all(c[1].get("metadata", {}).get("pii_detected") is True for c in _spans))
check("pseudoco_assistant_trace_id rides on every span (joins the trace to APM / governance logs)",
      all(c[1].get("metadata", {}).get("pseudoco_assistant_trace_id") == "tid-1" for c in _spans))
_agents = [c for c in fake.calls if c[0] == "add_agent_span"]
check("coordinator agent span tagged with the supervisor AgentType",
      getattr(_agents[0][1].get("agent_type"), "value", None) == "supervisor")
check("agent/llm spans carry duration_ns from duration_ms",
      [c[1].get("duration_ns") for c in _agents] == [500_000_000, 1_000_000_000, 1_500_000_000]
      and [c[1].get("duration_ns") for c in fake.calls if c[0] == "add_llm_span"]
      == [500_000_000, 1_000_000_000, 1_500_000_000])
_t0 = datetime(2026, 9, 8, 10, 0, 0, tzinfo=timezone.utc)
check("agent spans are back-dated sequentially by duration",
      [c[1].get("created_at") for c in _agents]
      == [_t0, _t0 + timedelta(milliseconds=500), _t0 + timedelta(milliseconds=1500)])
_concludes = [c[1] for c in fake.calls if c[0] == "conclude"]
check("workflow + trace concludes carry the turn duration",
      _concludes[-1].get("duration_ns") == 3_000_000_000 and _concludes[-2].get("duration_ns") == 3_000_000_000)
check("agent-span concludes carry the agent duration",
      [c.get("duration_ns") for c in _concludes[:3]] == [500_000_000, 1_000_000_000, 1_500_000_000])

fake = _FakeLogger()
ao._build_turn(fake, _turn(agent_trace=None))
names = _names(fake.calls)
_llm = [c for c in fake.calls if c[0] == "add_llm_span"]
check("no agent_trace -> chat_turn workflow wrapping a single LLM span with the turn's usage",
      names.count("add_workflow_span") == 1 and names.count("add_agent_span") == 0
      and len(_llm) == 1 and _llm[0][1].get("num_input_tokens") == 60
      and _llm[0][1].get("total_tokens") == 113 and _llm[0][1].get("duration_ns") == 3_000_000_000
      and names.count("conclude") == 2)

fake = _FakeLogger()
fake.fail_start = True
try:
    ao._build_turn(fake, _turn())
    check("start_trace returning None raises TurnEmitError", False)
except ao.TurnEmitError:
    check("start_trace returning None raises TurnEmitError", True)
fake = _FakeLogger()
fake.raise_start = ValueError("A trace cannot be created within a parent")
try:
    ao._build_turn(fake, _turn())
    check("start_trace raising (dangling parent) raises TurnEmitError", False)
except ao.TurnEmitError:
    check("start_trace raising (dangling parent) raises TurnEmitError", True)

# ---- 4. worker path ----------------------------------------------------------
print("\n[4] worker: one logger, sessions, log line")
_fresh()
_enable()
ao.maybe_log_turn(_turn())
ao.maybe_log_turn(_turn(request_id="rid2"))
check("worker drains both turns", ao._drain_for_tests(5.0))
check("exactly one SplunkAOLogger per process", len(_FakeLogger.instances) == 1)
lg = _FakeLogger.instances[0]
check("project / agent stream default to PseudoCo Assistant when env unset",
      lg.project_name == "PseudoCo Assistant" and lg.agent_stream_name == "PseudoCo Assistant")
_sessions = [c for c in lg.calls if c[0] == "start_session"]
check("start_session called once per PseudoCo Assistant session (cache reuse) with the external id",
      len(_sessions) == 1 and _sessions[0][1].get("external_id") == "sess-abc"
      and _sessions[0][1].get("name") == "chat session sess-abc")
_n = _names(lg.calls)
_second_start = [i for i, n in enumerate(_n) if n == "start_trace"][1]
check("set_session precedes start_trace on the second turn",
      "set_session" in _n[:_second_start] and lg.calls[_n.index("set_session")][1] == {"session_id": "ao-sess-1"})
check("each turn ends with conclude, flush", _n[-2:] == ["conclude", "flush"])
_info = _cap.messages(logging.INFO, "logged turn")
check("INFO log line per turn with model / agents / project / stream / export",
      len(_info) == 2 and _info[-1].getMessage()
      == "agent observability: logged turn (model=m, agents=3, project=PseudoCo Assistant, agent_stream=PseudoCo Assistant, export=healthy)")
check("logger-ready line logged once", len(_cap.messages(logging.INFO, "logger ready")) == 1)
check("status() counts logged turns", ao.status()["turns_logged"] == 2 and ao.status()["logger_ready"] is True)

print("\n[5] worker: dangling parent recovery")
lg.calls.clear()
lg.dangling = True
ao.maybe_log_turn(_turn())
ao._drain_for_tests(5.0)
_n = _names(lg.calls)
check("a dangling trace is concluded + parent tracking reset before start_trace",
      _n[:2] == ["conclude", "reset_parent_tracking"] and lg.calls[0][1].get("conclude_all") is True
      and "start_trace" in _n)

print("\n[6] worker: sessions unavailable -> turn still logged, backoff")
_fresh()
_enable()
_FakeLogger.session_error_default = True


class _SessionFail(_FakeLogger):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.session_error = True


_fake_sdk.SplunkAOLogger = _SessionFail
ao.maybe_log_turn(_turn())
ao.maybe_log_turn(_turn(request_id="rid2"))
ao._drain_for_tests(5.0)
lg = _FakeLogger.instances[0]
_n = _names(lg.calls)
check("turns are logged without a session when start_session fails",
      _n.count("start_trace") == 2 and "clear_session" in _n)
check("start_session is not retried inside the backoff window",
      _n.count("start_session") == 1 and ao._rt.sessions_unavailable_until > 0)
check("exactly one WARNING about sessions", len(_cap.messages(logging.WARNING, "sessions unavailable")) == 1)
_fake_sdk.SplunkAOLogger = _FakeLogger

print("\n[7] worker: build failure is contained")
_fresh()
_enable()
_FakeLogger.ctor_error = RuntimeError("MissingConfigurationError: O11y deployment requires SPLUNK_AO_O11Y_TOKEN")
try:
    ao.maybe_log_turn(_turn())
    ao.maybe_log_turn(_turn(request_id="rid2"))
    ao._drain_for_tests(5.0)
    check("maybe_log_turn never raises on a build failure", True)
except Exception:
    check("maybe_log_turn never raises on a build failure", False)
check("turns are dropped while the build backs off",
      ao.status()["dropped"] == 2 and ao.status()["logger_ready"] is False and ao.status()["last_build_error"])
check("one WARNING per distinct build failure", len(_cap.messages(logging.WARNING, "cannot build SplunkAOLogger")) == 1)
_FakeLogger.ctor_error = None

print("\n[8] reconfigure retires the logger")
_fresh()
_enable()
ao.maybe_log_turn(_turn())
ao._drain_for_tests(5.0)
os.environ["SPLUNK_AO_PROJECT"] = "P"
ao.reconfigure()
ao.maybe_log_turn(_turn())
ao._drain_for_tests(5.0)
check("the previous logger is terminated and a new one built from the current env",
      len(_FakeLogger.instances) == 2 and "terminate" in _names(_FakeLogger.instances[0].calls)
      and _FakeLogger.instances[1].project_name == "P")
check("the session cache is dropped with the logger",
      "start_session" in _names(_FakeLogger.instances[1].calls))
os.environ.pop("SPLUNK_AO_PROJECT")

print("\n[8b] one agent stream per theme")
_fresh()
_enable()
os.environ.pop("SPLUNK_AO_AGENT_STREAM", None)
os.environ.pop("SPLUNK_AO_AGENT_STREAM_PER_THEME", None)

check("a theme resolves to its own label, from the registry",
      ao._stream_for({"theme": "medadvice"}) == "MedAdvice"
      and ao._stream_for({"theme": "taxadvice"}) == "TaxAdvice")
check("theme matching is case/whitespace tolerant",
      ao._stream_for({"theme": " MedAdvice "}) == "MedAdvice")
check("every registered theme maps to a distinct stream",
      len({ao._stream_for({"theme": k}) for k in _THEME_KEYS}) == len(_THEME_KEYS))
check("an unknown or missing theme falls back to the default stream",
      ao._stream_for({"theme": "not-a-theme"}) == "PseudoCo Assistant"
      and ao._stream_for({}) == "PseudoCo Assistant"
      and ao._stream_for({"theme": None}) == "PseudoCo Assistant")

os.environ["SPLUNK_AO_AGENT_STREAM"] = "Fallback"
check("the fallback stream is SPLUNK_AO_AGENT_STREAM", ao._stream_for({}) == "Fallback")
check("a known theme still wins over the fallback",
      ao._stream_for({"theme": "legaladvice"}) == "LegalAdvice")
os.environ["SPLUNK_AO_AGENT_STREAM_PER_THEME"] = "False"
check("PER_THEME=False pins every turn to the one stream",
      ao._stream_for({"theme": "legaladvice"}) == "Fallback")
os.environ.pop("SPLUNK_AO_AGENT_STREAM_PER_THEME")
os.environ.pop("SPLUNK_AO_AGENT_STREAM")

# turns on two themes -> two loggers, each constructed with its own stream
ao.maybe_log_turn(_turn(request_id="t-med", theme="medadvice"))
ao.maybe_log_turn(_turn(request_id="t-tax", theme="taxadvice"))
ao.maybe_log_turn(_turn(request_id="t-med2", theme="medadvice"))
ao._drain_for_tests(5.0)
_streams = [i.agent_stream_name for i in _FakeLogger.instances]
check("one logger per theme, built with that theme's stream (not one per turn)",
      _streams == ["MedAdvice", "TaxAdvice"], )
check("all three turns were logged", ao._rt.turns_logged == 3)
check("the live streams are reported by status()",
      sorted(ao.status()["agent_streams_live"]) == ["MedAdvice", "TaxAdvice"]
      and ao.status()["agent_stream_per_theme"] is True)
check("each stream keeps its own project", {i.project_name for i in _FakeLogger.instances} == {"PseudoCo Assistant"})
check("sessions are per (stream, session): the same session_id twice, once per stream",
      len(ao._rt.sessions) == 2)
check("reconfigure retires every stream's logger",
      (ao.reconfigure(), ao.maybe_log_turn(_turn(request_id="t-after", theme="medadvice")),
       ao._drain_for_tests(5.0), len(_FakeLogger.instances) == 3)[-1])

print("\n[9] consecutive failures rebuild the logger")
_fresh()
_enable()


class _FailStart(_FakeLogger):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.fail_start = True


_fake_sdk.SplunkAOLogger = _FailStart
for i in range(ao._MAX_CONSECUTIVE_FAILURES):
    ao.maybe_log_turn(_turn(request_id=f"r{i}"))
ao._drain_for_tests(5.0)
check("after N consecutive failures the logger is terminated and rebuilt after a backoff",
      "terminate" in _names(_FakeLogger.instances[0].calls) and not ao._rt.loggers
      and ao._rt.build_backoff_until > 0)
check("one WARNING announces the rebuild", len(_cap.messages(logging.WARNING, "consecutive failures")) == 1)
_fails_w = _cap.messages(logging.WARNING, "emit failed")
check("emit failures are WARNINGs: traceback once, one-liners afterwards",
      len(_fails_w) == ao._MAX_CONSECUTIVE_FAILURES and sum(1 for r in _fails_w if r.exc_info) == 1)
_fake_sdk.SplunkAOLogger = _FakeLogger

print("\n[10] bounded queue drops instead of blocking")
_fresh(maxsize=1)
_enable()
_gate = threading.Event()


class _Blocking(_FakeLogger):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.block_event = _gate


_fake_sdk.SplunkAOLogger = _Blocking
try:
    ao.maybe_log_turn(_turn(request_id="a"))   # occupies the worker (blocked in start_trace)
    import time as _time
    _deadline = _time.monotonic() + 5
    while _time.monotonic() < _deadline and not (_FakeLogger.instances and _FakeLogger.instances[0].calls):
        _time.sleep(0.02)
    ao.maybe_log_turn(_turn(request_id="b"))   # fills the 1-slot queue
    ao.maybe_log_turn(_turn(request_id="c"))   # Full -> dropped
    ao.maybe_log_turn(_turn(request_id="d"))
    check("maybe_log_turn never raises or blocks on a full queue", True)
except Exception:
    check("maybe_log_turn never raises or blocks on a full queue", False)
check("dropped turns are counted and warned once",
      ao.status()["dropped"] >= 1 and len(_cap.messages(logging.WARNING, "queue full")) == 1)
_gate.set()
ao._drain_for_tests(5.0)
_fake_sdk.SplunkAOLogger = _FakeLogger

print("\n[11] shutdown")
_fresh()
_enable()
ao.maybe_log_turn(_turn())
ao._drain_for_tests(5.0)
ao.shutdown(5.0)
check("shutdown stops the worker and terminates the logger",
      (ao._rt.thread is None or not ao._rt.thread.is_alive())
      and "terminate" in _names(_FakeLogger.instances[-1].calls))
try:
    ao.shutdown(1.0)
    check("shutdown is idempotent", True)
except Exception:
    check("shutdown is idempotent", False)
_disable()

# ---- 12. wiring presence (export paths) ------------------------------------
print("\n[12] wiring")
collector = (ROOT / "otel-collector-config.yaml").read_text()
overlay = (ROOT / "otel-collector-agent-obs.yaml").read_text()
check("agent-obs overlay exports to the O11y trace ingest with the SDK's routing headers",
      "otlphttp/agent_obs" in overlay
      and "ingest.${env:SPLUNK_AO_REALM}.observability.splunkcloud.com/v2/trace/otlp" in overlay
      and 'X-SF-Token: "${env:SPLUNK_AO_O11Y_TOKEN}"' in overlay
      and 'project: "${env:SPLUNK_AO_PROJECT}"' in overlay
      and 'logstream: "${env:SPLUNK_AO_AGENT_STREAM}"' in overlay)
_overlay_pipes = overlay.split("pipelines:", 1)[-1]
check("agent-obs exporter is on a GenAI-only traces pipeline in the overlay",
      "traces/agent_obs" in _overlay_pipes and "otlphttp/agent_obs" in _overlay_pipes
      and "filter/genai_only" in _overlay_pipes)
_base_no_comment = collector.replace("# traces/agent_obs is defined in otel-collector-agent-obs.yaml", "")
check("BASE collector config carries NO agent-obs (or galileo) exporter or pipeline",
      "otlphttp/agent_obs" not in collector and "traces/agent_obs" not in _base_no_comment
      and "galileo" not in collector.lower())
check("collector defines a GenAI-only filter (drops non-gen_ai spans)",
      "filter/genai_only" in collector and "gen_ai.operation.name" in collector)
_splunk_block = collector.split("pipelines:", 1)[-1]
check("Splunk APM traces pipeline keeps the full trace (no GenAI filter)",
      "otlphttp/traces" in _splunk_block and "filter/genai_only" not in _splunk_block)
rc = (ROOT / "run-collector.sh").read_text()
check("run-collector.sh layers the overlay only when SPLUNK_AO_O11Y_TOKEN is set",
      'if [ -n "${SPLUNK_AO_O11Y_TOKEN:-}" ]' in rc and "otel-collector-agent-obs.yaml" in rc
      and "otel-collector-galileo.yaml" not in rc)
check("run-collector.sh injects SPLUNK_AO_* into the collector",
      all(k in rc for k in ("SPLUNK_AO_REALM", "SPLUNK_AO_O11Y_TOKEN", "SPLUNK_AO_PROJECT", "SPLUNK_AO_AGENT_STREAM"))
      and "-e SPLUNK_AO_AGENT_STREAM \\" in rc)
run_sh = (ROOT / "run.sh").read_text()
check("run.sh exports SPLUNK_AO_* / AGENT_CONTROL_* to the app process",
      "SPLUNK_AO_" in run_sh and "AGENT_CONTROL_" in run_sh and "GALILEO_" not in run_sh)
gov = (ROOT / "backend/logging/governance_logger.py").read_text()
check("governance logger fans completed turns out to agent_observability",
      "agent_observability" in gov and "maybe_log_turn" in gov and "galileo_integration" not in gov)
src = (ROOT / "backend/agent_observability.py").read_text()
check("the emitter never imports the legacy galileo SDK",
      "from galileo import" not in src and "GalileoLogger" not in src)
check("legacy files are gone",
      not (ROOT / "otel-collector-galileo.yaml").exists() and not (ROOT / "backend/galileo_integration.py").exists()
      and not (ROOT / "tests/test_galileo_integration.py").exists())
check("run_all.sh runs this suite", "tests/test_agent_observability.py" in (ROOT / "tests/run_all.sh").read_text())

ao.shutdown(2.0)
del sys.modules["splunk_ao"]
_disable()
print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
sys.exit(1 if _fails else 0)
