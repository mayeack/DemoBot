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
    # API Configuration
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250929"

    # Application
    app_name: str = "MedAdvice v3"
    app_version: str = "3.0.0"
    environment: str = "development"
    debug: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8001

    # Database
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
