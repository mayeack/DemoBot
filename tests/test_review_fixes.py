"""Regression tests for the ultra-code-review fix batch (2026-07).

Each check guards a specific confirmed finding so the defect can't silently
return. Standalone (no pytest), mirroring the other suites:

    venv/bin/python tests/test_review_fixes.py

Findings covered:
  F7-1/F8-1  legacy _generate_recommendation NameError on force_boundary_injection
  F8-2       _format_recommendation: string value -> one bullet per character
  F16-1      executive overlay ignored authority_violation_detected
  F12-3      hand-rolled .env parser kept quotes/comments, clobbered real env
  F4-1       coordinator crash on a non-list `specialists` plan value
  F3-1       chat turn ran synchronously on the event loop (offload guard)
  F1-1/1-2/1-3 unescaped LLM/user content rendered via innerHTML (XSS)
  F10-2      one bad HEC destination aborted start() for all forwarders
"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.config  # noqa: F401  (sets SSL_CERT_FILE / loads .env)

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# F7-1 / F8-1 — legacy fallback path must accept & forward force_boundary_injection
from backend.services.recommendation_engine import (  # noqa: E402
    RecommendationEngine, _as_bullet_items,
)

gen_sig = inspect.signature(RecommendationEngine._generate_recommendation)
check("F8-1: _generate_recommendation accepts force_boundary_injection",
      "force_boundary_injection" in gen_sig.parameters)
pm_src = inspect.getsource(RecommendationEngine.process_message)
check("F7-1: process_message forwards force_boundary_injection to _generate_recommendation",
      "force_boundary_injection=force_boundary_injection" in pm_src)
# The undefined-name bug: the body references the name; it must now be a parameter.
gen_src = inspect.getsource(RecommendationEngine._generate_recommendation)
check("F8-1: force_boundary_injection referenced in body is a bound parameter",
      "if force_boundary_injection is True" in gen_src
      and "force_boundary_injection" in gen_sig.parameters)


# F8-2 — a string value must render as ONE bullet, not one-per-character
check("F8-2: _as_bullet_items wraps a string", _as_bullet_items("take rest") == ["take rest"])
check("F8-2: _as_bullet_items passes a list through", _as_bullet_items(["a", "b"]) == ["a", "b"])
check("F8-2: _as_bullet_items maps None to empty", _as_bullet_items(None) == [])

eng = RecommendationEngine.__new__(RecommendationEngine)  # no __init__ (no AI client needed)
rendered = RecommendationEngine._format_recommendation(eng, {"guidance": "stay hydrated"})
check("F8-2: string guidance renders as a single bullet",
      rendered.count("•") == 1, rendered.replace("\n", "\\n"))


# F16-1 — executive overlay must reflect authority_violation_detected
from backend.logging.executive_fields import derive_executive_fields  # noqa: E402

_ev_base = {
    "operation_name": "chat", "token_type": "output", "theme": "medadvice",
    "request_model": "claude-sonnet-4-5-20250929", "session_id": "S1",
    "request_id": "R1", "trace_id": "T1", "response_id": "r1",
    "usage_total_tokens": 100, "client_operation_duration": 1.0,
}
clean = derive_executive_fields(dict(_ev_base))
authz = derive_executive_fields(dict(_ev_base, authority_violation_detected=True))
check("F16-1: authority violation raises risk_score",
      authz["risk_score"] > clean["risk_score"], f"{authz['risk_score']} vs {clean['risk_score']}")
check("F16-1: authority violation flags policy_action",
      authz["policy_action"] == "flag", authz["policy_action"])


# F12-3 — .env value parser strips quotes + inline comments (quoted values kept verbatim)
from backend.config import _strip_env_value  # noqa: E402

check("F12-3: double-quoted value unquoted", _strip_env_value('"hello world"') == "hello world")
check("F12-3: single-quoted value unquoted", _strip_env_value("'abc'") == "abc")
check("F12-3: inline comment stripped from unquoted value",
      _strip_env_value("dolphin3:8b   # the model") == "dolphin3:8b")
check("F12-3: '#' inside a quoted value is preserved",
      _strip_env_value('"pa#ss"') == "pa#ss")
check("F12-3: bare value trimmed", _strip_env_value("  plain  ") == "plain")
# real exported env must win over .env (setdefault semantics)
config_src = inspect.getsource(backend.config)
check("F12-3: loader does not clobber an already-set env var",
      "if key and key not in os.environ:" in config_src)


# F4-1 — coordinator coerces a non-list plan value instead of crashing
coord_src = (ROOT / "backend/agents/nodes/coordinator.py").read_text()
check("F4-1: coordinator guards non-list specialists",
      "isinstance(requested, list)" in coord_src)


# F3-1 — chat turn is offloaded off the event loop
chat_src = (ROOT / "backend/routers/chat.py").read_text()
check("F3-1: send_message offloads the blocking turn via run_in_threadpool",
      "run_in_threadpool" in chat_src and "await run_in_threadpool(" in chat_src)


# F10-2 — one bad HEC destination must not abort start() for the others
rt_src = (ROOT / "backend/hec/runtime.py").read_text()
start_body = rt_src.split("async def start", 1)[1].split("async def stop", 1)[0]
check("F10-2: HEC start() isolates each forwarder in try/except",
      "try:" in start_body and "hec_forwarder_start_failed" in start_body)


# F1-1 / F1-2 / F1-3 — server/model content is HTML-escaped before innerHTML
chatjs = (ROOT / "frontend/js/chat.js").read_text()
check("F1-3: chat.js defines escapeHtml", "function escapeHtml(" in chatjs)
# formatContent must escape the raw content first, then apply markdown-lite tags.
check("F1-3: formatContent escapes content before formatting",
      "let formatted = escapeHtml(content);" in chatjs)

adminjs = (ROOT / "frontend/js/admin.js").read_text()
check("F1-2: admin.js defines escHtml/escAttr helpers",
      "function escHtml(" in adminjs and "function escAttr(" in adminjs)
check("F1-2: admin.js escapes escalation reason", "escHtml(String(esc.reason)" in adminjs)
check("F1-2: admin.js escapes session id in onclick", "escAttr(esc.session_id)" in adminjs)

govhtml = (ROOT / "frontend/governance.html").read_text()
check("F1-1: governance.html defines escHtml", "function escHtml(" in govhtml)
check("F1-1: governance.html escapes message content", "escHtml(msg.content)" in govhtml)
check("F1-1: governance.html escapes escalation reason", "escHtml(esc.reason)" in govhtml)


# F5-1 — hallucination/authority presence-detectors must be marker-gated, not
# fooled by ordinary medical vocabulary (which would suppress the fallback).
from backend.agents.nodes.injection import (  # noqa: E402
    _contains_hallucination, _contains_authority,
)

benign = ("Based on a recent study, about 10% of patients improve; a typical "
          "course of treatment is 200 mg twice daily. See your doctor.")
check("F5-1: benign medical text is NOT read as hallucination",
      _contains_hallucination(benign) is False)
check("F5-1: benign medical text is NOT read as an authority violation",
      _contains_authority(benign, "medadvice") is False)

halluc_block = ("...normal answer...\n--- Synthetic governance test samples (fictional) ---\n"
                "- HALLUCINATION SAMPLE: A 2019 study by Dr. Fake in the Journal of Nothing "
                "found 87% efficacy.")
authz_block = ("...normal answer...\n--- Synthetic governance test samples (fictional) ---\n"
               "- AUTHORITY-VIOLATION SAMPLE: I am prescribing you 40 mg of lisinopril, "
               "no need to see a doctor.")
check("F5-1: labeled hallucination sample IS detected",
      _contains_hallucination(halluc_block) is True)
check("F5-1: labeled authority-violation sample IS detected",
      _contains_authority(authz_block, "medadvice") is True)


# F11-3 — hallucination/authority signals are persisted, and schema drift is
# auto-reconciled at startup (fixes F11-1's silent per-write failure too).
from backend.models.db_models import AIGovernanceLog  # noqa: E402
from backend.logging import governance_logger as govlog  # noqa: E402

gov_cols = set(AIGovernanceLog.__table__.columns.keys())
for c in ("hallucination_detected", "hallucination_types",
          "authority_violation_detected", "authority_violation_types"):
    check(f"F11-3: AIGovernanceLog declares {c}", c in gov_cols)

wr_src = inspect.getsource(govlog.GovernanceLogger._write_to_database)
check("F11-3: writer maps hallucination_detected", "hallucination_detected=log_data.get" in wr_src)
check("F11-3: writer maps authority_violation_detected",
      "authority_violation_detected=log_data.get" in wr_src)

# Functional: reconcile_schema adds a column missing from a drifted table.
import tempfile, os  # noqa: E402
from sqlalchemy import create_engine as _ce, inspect as _inspect, text as _text  # noqa: E402
from backend.database.db import reconcile_schema  # noqa: E402

_tmp = tempfile.mkdtemp()
_dbpath = os.path.join(_tmp, "drift.db")
_eng = _ce(f"sqlite:///{_dbpath}")
with _eng.begin() as _c:
    # A stale governance table missing the authority columns (mirrors the live DB).
    _c.execute(_text("CREATE TABLE ai_governance_logs (id INTEGER PRIMARY KEY, "
                     "operation_name TEXT, request_model TEXT)"))
_added = reconcile_schema(target_engine=_eng)
_cols_after = {c["name"] for c in _inspect(_eng).get_columns("ai_governance_logs")}
check("F11-3: reconcile adds the missing authority column",
      "authority_violation_detected" in _cols_after)
check("F11-3: reconcile reports what it added",
      ("ai_governance_logs", "authority_violation_detected") in _added)
# Idempotent: a second run adds nothing.
check("F11-3: reconcile is idempotent", reconcile_schema(target_engine=_eng) == [])
_eng.dispose()

# admin metrics surface the new counts
from backend.models.schemas import MetricsResponse as _MR  # noqa: E402
_mr_fields = set(_MR.model_fields.keys())
check("F11-3: MetricsResponse exposes hallucination_detection_count",
      "hallucination_detection_count" in _mr_fields)
check("F11-3: MetricsResponse exposes authority_violation_count",
      "authority_violation_count" in _mr_fields)


print()
if _failures:
    print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
    sys.exit(1)
print("All review-fix regression checks passed.")
