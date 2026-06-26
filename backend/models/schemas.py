from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

class SeverityLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EMERGENCY = "EMERGENCY"

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

class MessageType(str, Enum):
    USER_MESSAGE = "user_message"
    CLARIFYING_QUESTION = "clarifying_question"
    RECOMMENDATION = "recommendation"
    SAFETY_WARNING = "safety_warning"
    ESCALATION = "escalation"

# Request/Response Models
class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    type: Optional[MessageType] = MessageType.USER_MESSAGE
    timestamp: Optional[datetime] = None

class ChatRequest(BaseModel):
    session_id: str
    message: str
    disclaimer_accepted: bool = False
    theme: Optional[str] = "medadvice"
    force_pii_injection: Optional[bool] = None  # Override PII injection rate
    force_toxic_injection: Optional[bool] = None  # Override toxic response injection rate
    force_hallucination_injection: Optional[bool] = None  # Override hallucination injection rate
    force_boundary_injection: Optional[bool] = None  # Force prescriptive overreach (non-OTC / out-of-scope prescription) in the response
    ai_defense_review: Optional[bool] = None  # Send prompt to Cisco AI Defense for policy review
    internal_policy_review: Optional[bool] = None  # Run the built-in internal policy engine (default on)

class ChatResponse(BaseModel):
    session_id: str
    message: str
    type: MessageType
    severity: Optional[SeverityLevel] = None
    escalated: bool = False
    timestamp: datetime

# Governance Log Models
class GenAILogEntry(BaseModel):
    # Core operation / model identity
    operation_name: str
    provider_name: str = "anthropic"
    request_model: str
    response_model: Optional[str] = None
    response_id: Optional[str] = None
    conversation_id: str
    deployment_id: str = "demobot-v3-prod"
    request_id: str
    session_id: str
    trace_id: str

    # Input / output payload
    input_messages: List[Dict[str, Any]]
    output_messages: Optional[List[Dict[str, Any]]] = None
    system_instructions: Optional[str] = None
    tool_definitions: Optional[List[Dict[str, Any]]] = None
    output_type: str = "text"

    # Request parameters
    token_type: str
    request_max_tokens: Optional[int] = None
    request_temperature: Optional[float] = None
    request_top_p: Optional[float] = None
    request_frequency_penalty: Optional[float] = None
    request_presence_penalty: Optional[float] = None
    request_stop_sequences: Optional[List[str]] = None
    response_finish_reasons: Optional[List[str]] = None
    request_choice_count: Optional[int] = 1
    request_seed: Optional[int] = None

    # Usage, performance, and cost
    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None
    usage_total_tokens: Optional[int] = None
    client_operation_duration: Optional[float] = None  # seconds
    server_time_per_output_token: Optional[float] = None
    server_time_to_first_token: Optional[float] = None

    # Safety, guardrails, and policy
    safety_violated: bool = False
    safety_categories: Optional[List[str]] = None
    guardrail_triggered: bool = False
    guardrail_ids: Optional[List[str]] = None
    pii_detected: bool = False
    pii_types: Optional[List[str]] = None
    policy_blocked: bool = False

    # Evaluation / TEVV
    evaluation_name: Optional[str] = None
    evaluation_score_value: Optional[float] = None
    evaluation_score_label: Optional[str] = None
    evaluation_explanation: Optional[str] = None
    drift_metric_name: Optional[str] = None
    drift_metric_value: Optional[float] = None
    drift_status: Optional[str] = None

    # Error and infra fields
    error_type: Optional[str] = None
    server_address: Optional[str] = None
    server_port: Optional[int] = None

    # Actor / application context
    enduser_id: Optional[str] = None
    service_name: str = "demobot-v3"
    client_address: Optional[str] = None

    # Timestamp
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class EscalationLog(BaseModel):
    escalation_id: str
    session_id: str
    request_id: str
    timestamp: datetime
    reason: str
    severity: SeverityLevel
    conversation_history: List[Dict[str, Any]]
    user_demographics: Optional[Dict[str, Any]] = None
    symptoms: List[str]
    review_status: str = "pending"  # pending, reviewed, resolved
    reviewer_id: Optional[str] = None
    review_notes: Optional[str] = None
    review_timestamp: Optional[datetime] = None

class AuditLogEntry(BaseModel):
    audit_id: str
    session_id: str
    request_id: str
    timestamp: datetime
    action: str
    actor: str
    details: Dict[str, Any]
    ip_address: Optional[str] = None

# Metrics Models
class MetricsResponse(BaseModel):
    total_interactions: int
    escalation_count: int
    escalation_rate: float
    average_latency: float
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    severity_distribution: Dict[str, int]
    pii_detection_count: int
    guardrail_trigger_count: int
    time_period_start: datetime
    time_period_end: datetime

class SessionExport(BaseModel):
    session_id: str
    created_at: datetime
    messages: List[ChatMessage]
    governance_logs: List[GenAILogEntry]
    escalations: List[EscalationLog]
    final_severity: Optional[SeverityLevel] = None
