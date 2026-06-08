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

    # Application
    app_name: str = "MedAdvice v4"
    app_version: str = "4.0.0"
    environment: str = "development"  # "development" or "production"
    debug: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8001

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
    require_disclaimer_acceptance: bool = True
    max_clarifying_questions: int = 3

    # Session
    session_timeout_minutes: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

# Global settings instance
settings = Settings()

# Project paths
BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
DATABASE_DIR = BASE_DIR

# Ensure directories exist
LOGS_DIR.mkdir(exist_ok=True)
