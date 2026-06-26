"""Demo-only fault-injection flag for triggering RCA-eligible APM alerts.

A process-local singleton checked on the chat request hot path. When active, the
chat endpoint injects latency and/or 5xx errors so the demobot-v3 APM service's
``service.request.duration`` / error rate breach their Splunk detectors — letting
us demo the Splunk Observability AI Troubleshooting Agent. Auto-expires after
``duration_s`` so it can never be left on accidentally.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class IncidentMode:
    def __init__(self) -> None:
        self.enabled = False
        self.latency_ms = 0
        self.error_rate = 0.0  # 0.0 - 1.0
        self.duration_s: Optional[int] = None
        self._start_time: Optional[float] = None
        self.drove_traffic = False  # did this incident start the auto-prompter?

    def start(self, *, latency_ms: int = 0, error_rate: float = 0.0,
              duration_s: Optional[int] = None, drove_traffic: bool = False) -> None:
        self.latency_ms = max(0, int(latency_ms or 0))
        self.error_rate = min(1.0, max(0.0, float(error_rate or 0.0)))
        self.duration_s = int(duration_s) if duration_s else None
        self.drove_traffic = bool(drove_traffic)
        self._start_time = time.time()
        self.enabled = True
        logger.warning("incident_mode START latency_ms=%s error_rate=%s duration_s=%s",
                       self.latency_ms, self.error_rate, self.duration_s)

    def stop(self) -> None:
        self.enabled = False
        self._start_time = None
        logger.warning("incident_mode STOP")

    def is_active(self) -> bool:
        if not self.enabled:
            return False
        if self.duration_s and self._start_time and \
                (time.time() - self._start_time) > self.duration_s:
            self.enabled = False  # auto-expire
            return False
        return True

    def delay_seconds(self) -> float:
        """Latency to inject on this request, in seconds (0 if inactive)."""
        return (self.latency_ms / 1000.0) if (self.is_active() and self.latency_ms) else 0.0

    def should_error(self) -> bool:
        """True if this request should be failed with a 5xx (per error_rate)."""
        return self.is_active() and self.error_rate > 0 and random.random() < self.error_rate

    def status(self) -> Dict[str, Any]:
        active = self.is_active()
        elapsed = (time.time() - self._start_time) if self._start_time else 0.0
        remaining = None
        if active and self.duration_s and self._start_time:
            remaining = max(0, int(self.duration_s - elapsed))
        return {
            "active": active,
            "latency_ms": self.latency_ms,
            "error_rate": self.error_rate,
            "duration_s": self.duration_s,
            "elapsed_s": int(elapsed) if self._start_time else 0,
            "remaining_s": remaining,
            "drove_traffic": self.drove_traffic,
        }


incident_mode = IncidentMode()
