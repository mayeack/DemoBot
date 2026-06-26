from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, JSON, Text, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from datetime import datetime

Base = declarative_base()

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    disclaimer_accepted = Column(Boolean, default=False)
    final_severity = Column(String, nullable=True)
    escalated = Column(Boolean, default=False)
    messages = Column(JSON, default=list)
    conversation_metadata = Column(JSON, default=dict)

    __table_args__ = (
        Index('idx_created_at', 'created_at'),
        Index('idx_escalated', 'escalated'),
    )

class AIGovernanceLog(Base):
    __tablename__ = "ai_governance_logs"

    id = Column(Integer, primary_key=True, index=True)

    # Core operation / model identity
    operation_name = Column(String, nullable=False)
    provider_name = Column(String, default="anthropic")
    request_model = Column(String, nullable=False)
    response_model = Column(String, nullable=True)
    response_id = Column(String, nullable=True, index=True)
    conversation_id = Column(String, nullable=False, index=True)
    deployment_id = Column(String, default="demobot-v3-prod")
    request_id = Column(String, nullable=False, index=True)
    session_id = Column(String, nullable=False, index=True)
    trace_id = Column(String, nullable=False, index=True)

    # Input / output payload
    input_messages = Column(JSON, nullable=False)
    output_messages = Column(JSON, nullable=True)
    response_text = Column(Text, nullable=True)  # Final formatted response text shown to user
    system_instructions = Column(Text, nullable=True)
    tool_definitions = Column(JSON, nullable=True)
    output_type = Column(String, default="text")

    # Request parameters
    token_type = Column(String)
    request_max_tokens = Column(Integer, nullable=True)
    request_temperature = Column(Float, nullable=True)
    request_top_p = Column(Float, nullable=True)
    request_frequency_penalty = Column(Float, nullable=True)
    request_presence_penalty = Column(Float, nullable=True)
    request_stop_sequences = Column(JSON, nullable=True)
    response_finish_reasons = Column(JSON, nullable=True)
    request_choice_count = Column(Integer, default=1)
    request_seed = Column(Integer, nullable=True)

    # Usage, performance, and cost
    usage_input_tokens = Column(Integer, nullable=True)
    usage_output_tokens = Column(Integer, nullable=True)
    usage_total_tokens = Column(Integer, nullable=True)
    client_operation_duration = Column(Float, nullable=True)
    server_time_per_output_token = Column(Float, nullable=True)
    server_time_to_first_token = Column(Float, nullable=True)

    # Safety, guardrails, and policy
    safety_violated = Column(Boolean, default=False, index=True)
    safety_categories = Column(JSON, nullable=True)
    guardrail_triggered = Column(Boolean, default=False, index=True)
    guardrail_ids = Column(JSON, nullable=True)
    pii_detected = Column(Boolean, default=False, index=True)
    pii_types = Column(JSON, nullable=True)
    policy_blocked = Column(Boolean, default=False)
    toxic_detected = Column(Boolean, default=False, index=True)
    toxic_types = Column(JSON, nullable=True)
    # Evaluation / TEVV
    evaluation_name = Column(String, nullable=True)
    evaluation_score_value = Column(Float, nullable=True)
    evaluation_score_label = Column(String, nullable=True)
    evaluation_explanation = Column(Text, nullable=True)
    drift_metric_name = Column(String, nullable=True)
    drift_metric_value = Column(Float, nullable=True)
    drift_status = Column(String, nullable=True)

    # Error and infra fields
    error_type = Column(String, nullable=True, index=True)
    server_address = Column(String, nullable=True)
    server_port = Column(Integer, nullable=True)

    # Actor / application context
    enduser_id = Column(String, nullable=True, index=True)
    service_name = Column(String, default="demobot-v3")
    client_address = Column(String, nullable=True)

    # Timestamp
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index('idx_operation_timestamp', 'operation_name', 'timestamp'),
        Index('idx_session_timestamp', 'session_id', 'timestamp'),
        Index('idx_safety_flags', 'safety_violated', 'pii_detected', 'guardrail_triggered', 'toxic_detected'),
    )

class EscalationQueue(Base):
    __tablename__ = "escalation_queue"

    id = Column(Integer, primary_key=True, index=True)
    escalation_id = Column(String, unique=True, index=True, nullable=False)
    session_id = Column(String, nullable=False, index=True)
    request_id = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    reason = Column(Text, nullable=False)
    severity = Column(String, nullable=False, index=True)
    conversation_history = Column(JSON, nullable=False)
    user_demographics = Column(JSON, nullable=True)
    symptoms = Column(JSON, nullable=False)
    review_status = Column(String, default="pending", index=True)
    reviewer_id = Column(String, nullable=True)
    review_notes = Column(Text, nullable=True)
    review_timestamp = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('idx_status_timestamp', 'review_status', 'timestamp'),
        Index('idx_severity_status', 'severity', 'review_status'),
    )

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    audit_id = Column(String, unique=True, index=True, nullable=False)
    session_id = Column(String, nullable=False, index=True)
    request_id = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    actor = Column(String, nullable=False)
    details = Column(JSON, nullable=False)
    ip_address = Column(String, nullable=True)

    __table_args__ = (
        Index('idx_action_timestamp', 'action', 'timestamp'),
        Index('idx_actor_timestamp', 'actor', 'timestamp'),
    )

class AppSettings(Base):
    """Single-row table (id=1) holding runtime-mutable app settings: the local
    log directory and the Splunk HEC destinations. Edited via the Settings page
    / /api/settings. JSON blob mirrors ThreatGenerator's active-config store."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)  # always 1
    data = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
