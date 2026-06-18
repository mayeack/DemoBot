import json
import logging
from pathlib import Path
from typing import Dict, Any
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from backend.config import settings, LOGS_DIR, BASE_DIR


def _resolve_logs_dir(d) -> Path:
    """Resolve a configured log directory; relative paths hang off BASE_DIR."""
    p = Path(d)
    return p if p.is_absolute() else (BASE_DIR / p)

class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging"""

    def format(self, record):
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if hasattr(record, "extra_data"):
            log_data.update(record.extra_data)

        return json.dumps(log_data)

class GovernanceFileHandler:
    """Handler for writing governance logs to files"""

    def __init__(self, logs_dir=LOGS_DIR):
        self.logs_dir = _resolve_logs_dir(logs_dir)
        self._ensure_log_files()

    def repoint(self, new_dir):
        """Point subsequent governance writes at a new directory (runtime)."""
        self.logs_dir = _resolve_logs_dir(new_dir)
        self._ensure_log_files()

    def _ensure_log_files(self):
        """Ensure log files and directory exist"""
        self.logs_dir.mkdir(exist_ok=True)

        # Create .gitkeep
        (self.logs_dir / ".gitkeep").touch()

        # Initialize log files
        self.governance_log = self.logs_dir / "ai_governance.json"
        self.escalation_log = self.logs_dir / "escalations.json"
        self.audit_log = self.logs_dir / "audit_trail.json"
        self.error_log = self.logs_dir / "errors.json"

        for log_file in [self.governance_log, self.escalation_log, self.audit_log, self.error_log]:
            if not log_file.exists():
                log_file.write_text("")

    def write_governance_log(self, log_data: Dict[str, Any]):
        """Write to governance log file"""
        self._append_json_log(self.governance_log, log_data)

    def write_escalation_log(self, log_data: Dict[str, Any]):
        """Write to escalation log file"""
        self._append_json_log(self.escalation_log, log_data)

    def write_audit_log(self, log_data: Dict[str, Any]):
        """Write to audit log file"""
        self._append_json_log(self.audit_log, log_data)

    def write_error_log(self, log_data: Dict[str, Any]):
        """Write to error log file"""
        self._append_json_log(self.error_log, log_data)

    def _append_json_log(self, log_file: Path, log_data: Dict[str, Any]):
        """Append JSON log entry to file"""
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(log_data) + "\n")

            # Check if rotation needed
            self._rotate_if_needed(log_file)
        except Exception as e:
            logging.error(f"Failed to write to log file {log_file}: {e}")

    def _rotate_if_needed(self, log_file: Path):
        """Rotate log file if it exceeds size limit"""
        if log_file.stat().st_size > settings.log_rotation_size:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            rotated_file = log_file.with_name(f"{log_file.stem}_{timestamp}{log_file.suffix}")
            log_file.rename(rotated_file)
            log_file.touch()

            # Clean old rotated files
            self._clean_old_logs(log_file.parent, log_file.stem)

    def _clean_old_logs(self, log_dir: Path, log_stem: str):
        """Remove log files older than retention period"""
        cutoff_date = datetime.utcnow() - timedelta(days=settings.log_retention_days)

        for log_file in log_dir.glob(f"{log_stem}_*.json"):
            try:
                file_timestamp = datetime.fromtimestamp(log_file.stat().st_mtime)
                if file_timestamp < cutoff_date:
                    log_file.unlink()
            except Exception as e:
                logging.error(f"Failed to clean old log file {log_file}: {e}")

def setup_logging():
    """Setup application logging"""
    log_level = getattr(logging, settings.log_level.upper())

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Create governance logger.
    # Governance JSON events are written directly to dedicated files
    # (ai_governance.json, escalations.json, etc.) by GovernanceFileHandler,
    # NOT through the Python logging framework. The Python logger here is
    # only used for console output and operational messages (errors/warnings).
    # Do NOT add a file handler — that causes duplicate events in Splunk.
    governance_logger = logging.getLogger("governance")
    governance_logger.setLevel(log_level)
    governance_logger.propagate = False

    if settings.log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        governance_logger.addHandler(console_handler)

    return governance_logger
