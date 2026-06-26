"""Regression tests for the executive AI-governance field overlay.

Asserts the board-level normalized fields (risk_score, policy_action,
business_outcome, estimated_cost, contains_phi, audit_status, ...) that power
the Splunk "Executive AI Governance Overview" dashboard (workshop Section 0).

Standalone (no pytest required), mirroring tests/test_api.py:
    venv/bin/python tests/test_executive_fields.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.config  # noqa: F401  (sets SSL_CERT_FILE / loads .env)
from backend.logging.executive_fields import derive_executive_fields
from backend.logging.log_schemas import create_governance_log

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def _base(**over):
    """A minimal governance output event, overridable per scenario."""
    log = {
        "operation_name": "chat",
        "token_type": "output",
        "service_name": "demobot-v3",
        "request_model": "claude-sonnet-4-5-20250929",
        "response_model": "claude-sonnet-4-5-20250929",
        "session_id": "S1",
        "request_id": "R1",
        "trace_id": "T1",
        "response_id": "resp-1",
        "usage_input_tokens": 663,
        "usage_output_tokens": 410,
        "usage_total_tokens": 1073,
        "client_operation_duration": 2.5,
        "evaluation_score_value": 0.92,
        "theme": "medadvice",
        "agent_name": "medadvice_domain_agent",
    }
    log.update(over)
    return log


# 1. Clean advice -------------------------------------------------------------
f = derive_executive_fields(_base())
check("clean: policy_action=allow", f["policy_action"] == "allow", f["policy_action"])
check("clean: business_outcome=advice_delivered", f["business_outcome"] == "advice_delivered")
check("clean: user_type=patient", f["user_type"] == "patient")
check("clean: app_name mapped from service_name", f["app_name"] == "demobot-v3")
check("clean: latency_ms from duration", f["latency_ms"] == 2500.0, f["latency_ms"])
check("clean: token_count", f["token_count"] == 1073)
check("clean: low risk", f["risk_score"] < 25, f["risk_score"])
check("clean: audit complete", f["audit_status"] == "complete", f["audit_status"])
check("clean: estimated_cost computed", f["estimated_cost"] and f["estimated_cost"] > 0)
# Sonnet pricing: 663*3 + 410*15 = 1989 + 6150 = 8139 / 1e6 = 0.008139
check("clean: cost math", abs(f["estimated_cost"] - 0.008139) < 1e-6, str(f["estimated_cost"]))

# 2. Emergency escalation -----------------------------------------------------
f = derive_executive_fields(_base(
    severity="EMERGENCY", safety_violated=True, guardrail_triggered=True,
    guardrail_ids=["escalation_rules"], safety_categories=["Emergency symptoms detected"],
))
check("emergency: human_escalation", f["human_escalation"] is True)
check("emergency: outcome escalated", f["business_outcome"] == "escalated_to_human")
check("emergency: prompt_category", f["prompt_category"] == "emergency_symptom", f["prompt_category"])
check("emergency: policy_action=warn", f["policy_action"] == "warn")
check("emergency: high risk", f["risk_score"] >= 60, f["risk_score"])

# 3. Internal policy block (self-harm) ---------------------------------------
f = derive_executive_fields(_base(
    severity="EMERGENCY", policy_blocked=True, safety_violated=True,
    guardrail_triggered=True, guardrail_ids=["policy_block"],
    response_finish_reasons=["policy_blocked"],
))
check("policyblock: action=block", f["policy_action"] == "block")
check("policyblock: outcome", f["business_outcome"] == "blocked_unsafe", f["business_outcome"])
check("policyblock: category self_harm", f["prompt_category"] == "self_harm_crisis", f["prompt_category"])
check("policyblock: very high risk", f["risk_score"] >= 90, f["risk_score"])
check("policyblock: policy_name set", "Self-harm" in (f["policy_name"] or ""), f["policy_name"])

# 4. Cisco AI Defense block ---------------------------------------------------
f = derive_executive_fields(_base(
    policy_blocked=True, guardrail_triggered=True, guardrail_ids=["cisco_ai_defense"],
    safety_categories=["classifications: PRIVACY_VIOLATION, SECURITY_VIOLATION"],
    response_finish_reasons=["policy_blocked"],
))
check("aidefense: action=block", f["policy_action"] == "block")
check("aidefense: outcome blocked_by_ai_defense", f["business_outcome"] == "blocked_by_ai_defense", f["business_outcome"])
check("aidefense: policy_name carries classification",
      "PRIVACY_VIOLATION" in (f["policy_name"] or ""), f["policy_name"])

# 5. PHI exposure (medical theme) --------------------------------------------
f = derive_executive_fields(_base(pii_detected=True, pii_types=["ssn", "diagnosis"]))
check("phi: contains_pii", f["contains_pii"] is True)
check("phi: contains_phi (medical theme)", f["contains_phi"] is True)
check("phi: category phi_pii_exposure", f["prompt_category"] == "phi_pii_exposure")

# 6. PII on a non-medical theme is not PHI unless type matches ---------------
f = derive_executive_fields(_base(theme="telecomchatbot", pii_detected=True, pii_types=["email"]))
check("nonmedical: contains_pii", f["contains_pii"] is True)
check("nonmedical: not phi", f["contains_phi"] is False)
check("nonmedical: user_type=customer", f["user_type"] == "customer")

# 7. Hallucination signal raises risk ----------------------------------------
clean_risk = derive_executive_fields(_base())["risk_score"]
f = derive_executive_fields(_base(hallucination_detected=True,
                                  hallucination_types=["fabricated_fact"]))
check("halluc: raises risk", f["risk_score"] > clean_risk, f"{f['risk_score']} vs {clean_risk}")

# 8. Partial audit when usage missing ----------------------------------------
f = derive_executive_fields(_base(usage_total_tokens=None, evaluation_score_value=None))
check("audit: partial when evidence missing", f["audit_status"] == "partial", f["audit_status"])

# 9. End-to-end through create_governance_log --------------------------------
log = create_governance_log(
    operation_name="chat", request_model="claude-sonnet-4-5-20250929",
    conversation_id="S1", session_id="S1", input_messages=[],
    usage_total_tokens=1000, usage_input_tokens=600, usage_output_tokens=400,
    client_operation_duration=1.0, evaluation_score_value=0.9,
    response_id="r", trace_id="t", service_name="demobot-v3",
    theme="medadvice", severity="LOW", token_type="output",
)
check("e2e: overlay present in create_governance_log", "risk_score" in log and "business_outcome" in log)
check("e2e: existing fields untouched", log["request_model"] == "claude-sonnet-4-5-20250929")

# 10. Never raises on garbage -------------------------------------------------
check("robust: empty dict -> dict", isinstance(derive_executive_fields({}), dict))
check("robust: junk types -> dict",
      isinstance(derive_executive_fields({"usage_total_tokens": "x", "guardrail_ids": 5}), dict))

print()
if _failures:
    print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
    sys.exit(1)
print("All executive-field regression checks passed.")
