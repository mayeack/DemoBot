from typing import Dict, Any, Optional, List
from datetime import datetime
import uuid

from backend.config import settings

def create_governance_log(
    operation_name: str,
    request_model: str,
    conversation_id: str,
    session_id: str,
    input_messages: List[Dict[str, Any]],
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """Create a standardized governance log entry"""

    log_entry = {
        # Unique identifier for this specific log entry
        "event_id": str(uuid.uuid4()),
        # Core operation / model identity
        "operation_name": operation_name,
        "provider_name": kwargs.get("provider_name", settings.ai_provider),
        "request_model": request_model,
        "response_model": kwargs.get("response_model"),
        "response_id": kwargs.get("response_id"),
        "conversation_id": conversation_id,
        "deployment_id": kwargs.get("deployment_id", "medadvice-v3-prod"),
        "request_id": request_id or str(uuid.uuid4()),
        "session_id": session_id,
        "trace_id": trace_id or str(uuid.uuid4()),

        # Input / output payload
        "input_messages": input_messages,
        "output_messages": kwargs.get("output_messages"),
        "response_text": kwargs.get("response_text"),  # Final formatted response text shown to user
        "system_instructions": kwargs.get("system_instructions"),
        "tool_definitions": kwargs.get("tool_definitions"),
        "output_type": kwargs.get("output_type", "text"),

        # Request parameters
        "token_type": kwargs.get("token_type", "input"),
        "request_max_tokens": kwargs.get("request_max_tokens"),
        "request_temperature": kwargs.get("request_temperature"),
        "request_top_p": kwargs.get("request_top_p"),
        "request_frequency_penalty": kwargs.get("request_frequency_penalty"),
        "request_presence_penalty": kwargs.get("request_presence_penalty"),
        "request_stop_sequences": kwargs.get("request_stop_sequences"),
        "response_finish_reasons": kwargs.get("response_finish_reasons"),
        "request_choice_count": kwargs.get("request_choice_count", 1),
        "request_seed": kwargs.get("request_seed"),

        # Usage, performance, and cost
        "usage_input_tokens": kwargs.get("usage_input_tokens"),
        "usage_output_tokens": kwargs.get("usage_output_tokens"),
        "usage_total_tokens": kwargs.get("usage_total_tokens"),
        "client_operation_duration": kwargs.get("client_operation_duration"),
        "server_time_per_output_token": kwargs.get("server_time_per_output_token"),
        "server_time_to_first_token": kwargs.get("server_time_to_first_token"),

        # Safety, guardrails, and policy
        "safety_violated": kwargs.get("safety_violated", False),
        "safety_categories": kwargs.get("safety_categories"),
        "guardrail_triggered": kwargs.get("guardrail_triggered", False),
        "guardrail_ids": kwargs.get("guardrail_ids"),
        "policy_blocked": kwargs.get("policy_blocked", False),

        # PII detection
        "pii_detected": kwargs.get("pii_detected", False),
        "pii_types": kwargs.get("pii_types"),

        # Toxic content detection
        "toxic_detected": kwargs.get("toxic_detected", False),
        "toxic_types": kwargs.get("toxic_types"),

        # Evaluation / TEVV
        "evaluation_name": kwargs.get("evaluation_name"),
        "evaluation_score_value": kwargs.get("evaluation_score_value"),
        "evaluation_score_label": kwargs.get("evaluation_score_label"),
        "evaluation_explanation": kwargs.get("evaluation_explanation"),
        "drift_metric_name": kwargs.get("drift_metric_name"),
        "drift_metric_value": kwargs.get("drift_metric_value"),
        "drift_status": kwargs.get("drift_status"),

        # Error and infra fields
        "error_type": kwargs.get("error_type"),
        "server_address": kwargs.get("server_address"),
        "server_port": kwargs.get("server_port"),

        # Actor / application context
        "enduser_id": kwargs.get("enduser_id"),
        "service_name": kwargs.get("service_name", "medadvice-v3"),
        "client_address": kwargs.get("client_address"),

        # Timestamp
        "timestamp": kwargs.get("timestamp", datetime.utcnow()).isoformat()
    }

    # Remove None values for cleaner logs
    return {k: v for k, v in log_entry.items() if v is not None}

def create_escalation_log(
    escalation_id: str,
    session_id: str,
    request_id: str,
    reason: str,
    severity: str,
    conversation_history: List[Dict[str, Any]],
    symptoms: List[str],
    **kwargs
) -> Dict[str, Any]:
    """Create a standardized escalation log entry"""

    return {
        "escalation_id": escalation_id,
        "session_id": session_id,
        "request_id": request_id,
        "timestamp": kwargs.get("timestamp", datetime.utcnow()).isoformat(),
        "reason": reason,
        "severity": severity,
        "conversation_history": conversation_history,
        "user_demographics": kwargs.get("user_demographics"),
        "symptoms": symptoms,
        "review_status": kwargs.get("review_status", "pending"),
        "reviewer_id": kwargs.get("reviewer_id"),
        "review_notes": kwargs.get("review_notes"),
        "review_timestamp": kwargs.get("review_timestamp")
    }

def create_audit_log(
    audit_id: str,
    session_id: str,
    request_id: str,
    action: str,
    actor: str,
    details: Dict[str, Any],
    **kwargs
) -> Dict[str, Any]:
    """Create a standardized audit log entry"""

    return {
        "audit_id": audit_id,
        "session_id": session_id,
        "request_id": request_id,
        "timestamp": kwargs.get("timestamp", datetime.utcnow()).isoformat(),
        "action": action,
        "actor": actor,
        "details": details,
        "ip_address": kwargs.get("ip_address")
    }
