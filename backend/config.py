from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os
from pathlib import Path

# Manually load .env file as a workaround
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

# Use custom CA bundle that includes corporate proxy CAs (e.g. Cisco Secure Access)
_ca_bundle = Path(__file__).parent.parent / "ca-bundle.pem"
if _ca_bundle.exists():
    os.environ.setdefault("SSL_CERT_FILE", str(_ca_bundle))
    os.environ.setdefault("REQUESTS_CA_BUNDLE", str(_ca_bundle))

class Settings(BaseSettings):
    # AI Provider Selection (supports multiple providers)
    # "anthropic" = Direct Anthropic API (local development)
    # "bedrock" = AWS Bedrock (production on AWS)
    # "openai" = OpenAI-compatible APIs (OpenAI, DeepSeek, etc.)
    ai_provider: str = "anthropic"

    # Anthropic API Configuration (used when ai_provider="anthropic")
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250929"

    # AWS Bedrock Configuration (used when ai_provider="bedrock")
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "anthropic.claude-3-sonnet-20240229-v1:0"

    # OpenAI-compatible API Configuration (used when ai_provider="openai")
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com/v1"

    # Ollama Configuration (used when ai_provider="ollama")
    # Local, UNCENSORED open-source model served by a local `ollama serve` daemon
    # and called via langchain-ollama's ChatOllama. Workshop intent: an unaligned
    # model that WILL emit unsafe/toxic/PII output so the external guardrails
    # (Cisco AI Defense, Splunk, Galileo) demonstrably catch it. ChatOllama
    # populates usage_metadata natively, so the telemetry/governance token
    # contract is identical to the cloud providers.
    ollama_model: str = "dolphin3:8b"
    ollama_base_url: str = "http://localhost:11434"
    # Context window passed to Ollama (num_ctx); 8192 fits the theme system prompt
    # plus the governance input directives with headroom.
    ollama_num_ctx: int = 8192

    # Application
    app_name: str = "MedAdvice v4"
    app_version: str = "4.0.0"
    environment: str = "development"  # "development" or "production"
    debug: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8001

    # Public access gate. When set, every request (except /health) must send
    # this value as the HTTP Basic Auth password. Empty = gate disabled (local
    # dev). Supply via .env only — never hardcode.
    access_key: str = ""

    # Database (SQLite for local, PostgreSQL for AWS)
    database_url: str = "sqlite:///./medadvice.db"

    # Logging
    log_level: str = "INFO"
    log_to_file: bool = True
    log_to_console: bool = True
    log_to_database: bool = True
    log_rotation_size: int = 10485760  # 10MB
    log_retention_days: int = 90

    # Safety
    pii_injection_rate: float = 0.25  # 25% of responses will include synthetic PII/PHI
    toxic_injection_rate: float = 0.25  # 25% of responses will include toxic content
    hallucination_injection_rate: float = 0.25  # 25% of responses will include hallucinated content
    authority_injection_rate: float = 0.25  # 25% of responses will include outside-of-authority content
    require_disclaimer_acceptance: bool = True
    max_clarifying_questions: int = 3

    # Cisco AI Defense (Inspection API - runtime policy review of user prompts)
    # https://developer.cisco.com/docs/ai-defense-inspection/
    # Master switch: when False the per-request toggle is ignored and no prompt
    # is ever sent off-box, regardless of the UI toggle state.
    ai_defense_enabled: bool = False
    # Inspection API key generated in the AI Defense UI when you create an
    # "API" connection. Sent in the X-Cisco-AI-Defense-API-Key header. Never
    # hardcode this - supply it via the environment / .env only.
    ai_defense_api_key: str = ""
    # Regional deployment of your AI Defense tenant. Drives the base URL:
    #   us -> https://us.api.inspect.aidefense.security.cisco.com
    #   eu -> https://eu.api.inspect.aidefense.security.cisco.com
    #   ap -> https://ap.api.inspect.aidefense.security.cisco.com
    ai_defense_region: str = "us"
    # Optional full base-URL override (takes precedence over region) for private
    # / hybrid deployments. Example: https://us.api.inspect.aidefense.security.cisco.com
    ai_defense_endpoint: str = ""
    # Inspection request timeout in seconds.
    ai_defense_timeout: float = 10.0
    # Behavior when the Inspection API errors or returns a malformed response.
    # False = fail closed (block the prompt) — the documented secure default.
    # True  = fail open (allow the prompt through).
    ai_defense_fail_open: bool = False
    # Comma-separated list of AI Defense guardrails to enable explicitly on every
    # Inspection API call (sent as config.enabled_rules). Passing rules in the
    # request applies them directly instead of relying on the SCC-configured
    # policy, so enforcement is self-contained and direction-independent.
    # Rule names must match the API enum exactly. Leave empty to fall back to the
    # connection's UI-configured policy (config: {}).
    # Valid: Code Detection, Harassment, Hate Speech, PCI, PHI, PII,
    #        Prompt Injection, Profanity, Sexual Content & Exploitation,
    #        Social Division & Polarization, Violence & Public Safety Threats
    ai_defense_enabled_rules: str = (
        "PII,PHI,PCI,Harassment,Hate Speech,Profanity,"
        "Sexual Content & Exploitation,Violence & Public Safety Threats,"
        "Social Division & Polarization,Prompt Injection,Code Detection"
    )
    # Response-direction guardrails. The rules above are appropriate for the
    # *prompt* (user input); the model's *output* is a different risk surface, so
    # the response inspection enforces its own set. Prompt Injection and Code
    # Detection are prompt-direction concerns and are intentionally dropped here
    # (a response that quotes code is not itself an attack), leaving the
    # content-leak / content-harm guardrails that matter for generated output.
    ai_defense_response_enabled_rules: str = (
        "PII,PHI,PCI,Harassment,Hate Speech,Profanity,"
        "Sexual Content & Exploitation,Violence & Public Safety Threats"
    )
    # Custom AI Defense guardrail that flags MedAdvice exceeding its authority —
    # recommending a prescription-only (non-OTC) medication, dosage, or procedure.
    # This is NOT a Cisco standard rule: it must be created as a custom guardrail
    # on the AI Defense connection (SCC policy, enforced on the response/output
    # direction). When set, the name is appended to the response-direction
    # enabled_rules so connections that accept config.enabled_rules enforce it
    # directly. IMPORTANT: leave empty unless the guardrail actually exists on the
    # connection — sending an unknown rule name to a connection that accepts
    # enabled_rules can error and (fail-closed) block every response. On a
    # connection that already has an SCC policy bound, enabled_rules are ignored
    # entirely and enforcement comes from that console-configured policy.
    ai_defense_prescription_guardrail: str = ""

    # Session
    session_timeout_minutes: int = 30

    # -------------------------------------------------------------------------
    # Agentic orchestration (LangChain + LangGraph)
    # -------------------------------------------------------------------------
    # When True, /api/chat/message is served by the LangGraph multi-agent
    # workflow (backend/agents). When the agentic dependencies are unavailable
    # or the graph fails to build, the router transparently falls back to the
    # legacy RecommendationEngine so the service keeps running.
    use_agentic_engine: bool = True
    # Name promoted to the OTel GenAI Workflow span (AI Agent Monitoring groups
    # traces by this workflow name in Splunk Observability Cloud).
    agentic_workflow_name: str = "medadvice_multi_agent"

    # -------------------------------------------------------------------------
    # Agentic observability (OpenTelemetry GenAI -> Splunk Observability Cloud)
    # -------------------------------------------------------------------------
    # Master switch for code-based GenAI tracing. Export endpoint, headers, and
    # protocol are read from the standard OTEL_* environment variables
    # (e.g. OTEL_EXPORTER_OTLP_ENDPOINT). When no endpoint is configured and
    # debug is on, spans are printed to the console.
    otel_enabled: bool = False
    otel_service_name: str = "medadvice-v3"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    @property
    def ai_defense_chat_inspect_url(self) -> str:
        """Full URL of the AI Defense Chat Inspection endpoint.

        Grounded on the documented contract:
        POST {base}/api/v1/inspect/chat where base is the regional host
        https://{region}.api.inspect.aidefense.security.cisco.com.
        An explicit ai_defense_endpoint override wins when provided.
        """
        base = (self.ai_defense_endpoint or "").strip().rstrip("/")
        if not base:
            region = (self.ai_defense_region or "us").strip().lower()
            base = f"https://{region}.api.inspect.aidefense.security.cisco.com"
        return f"{base}/api/v1/inspect/chat"

    @property
    def ai_defense_rule_config(self) -> list[dict]:
        """Parsed enabled_rules for the Inspection API config block.

        Returns a list of ``{"rule_name": <name>}`` dicts built from
        ``ai_defense_enabled_rules``. Empty/whitespace entries are dropped. An
        empty result means callers should send ``config: {}`` and fall back to
        the connection's UI-configured policy.
        """
        return [
            {"rule_name": name}
            for name in (
                part.strip() for part in (self.ai_defense_enabled_rules or "").split(",")
            )
            if name
        ]

    @property
    def ai_defense_response_rule_config(self) -> list[dict]:
        """Parsed enabled_rules for the *response*-direction inspection.

        Built from ``ai_defense_response_enabled_rules`` plus the custom
        ``ai_defense_prescription_guardrail`` (when configured), as
        ``{"rule_name": <name>}`` dicts. An empty result means callers should
        send ``config: {}`` and defer to the connection's UI-configured policy.
        """
        names = [
            part.strip()
            for part in (self.ai_defense_response_enabled_rules or "").split(",")
        ]
        prescription = (self.ai_defense_prescription_guardrail or "").strip()
        if prescription:
            names.append(prescription)
        return [{"rule_name": name} for name in names if name]

# Global settings instance
settings = Settings()

# Project paths
BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
DATABASE_DIR = BASE_DIR

# Ensure directories exist
LOGS_DIR.mkdir(exist_ok=True)
