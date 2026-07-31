"""Demo-only state for the prompt-injection spray campaign.

A process-local singleton, mirroring ``backend/incident_mode.py``: the router
drives turns while ``is_active()`` holds, and the campaign auto-expires after
``duration_s`` so it can never be left running by accident.

This object holds *state and counters only*. It never fabricates a verdict —
the blocked/allowed tallies are incremented from what Cisco AI Defense actually
returned on each turn.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Rotated per turn so the campaign shows a source pivot rather than a single
# origin — makes the asset/identity correlation in ES more interesting. These
# are documentation/test-range style addresses, not real hosts.
CLIENT_ADDRESSES: List[str] = ["76.87.129.168", "203.0.113.42", "198.51.100.77"]

# Themes double as the governed application identity: the governance event's
# ``app_name`` is its ``service_name`` (backend/logging/executive_fields.py),
# so varying these populates the spec's ``apps_targeted``.
TARGET_APPS: List[Dict[str, str]] = [
    {"theme": "medadvice", "service_name": "demobot-medadvice", "deployment_id": "demobot-medadvice-prod"},
    {"theme": "legaladvice", "service_name": "demobot-legaladvice", "deployment_id": "demobot-legaladvice-prod"},
    {"theme": "financeadvice", "service_name": "demobot-financeadvice", "deployment_id": "demobot-financeadvice-prod"},
]


class SprayCampaign:
    def __init__(self) -> None:
        self.enabled = False
        self.actor = "t.nguyen"
        self.duration_s: Optional[int] = None
        self.intensity = 0
        self.secondary_actors = 0
        self.drove_turns = False
        self.campaign_id: Optional[str] = None
        self._start_time: Optional[float] = None
        # Live tallies, all sourced from real verdicts.
        self.turns_sent = 0
        self.blocked = 0
        self.allowed = 0
        self.errors = 0
        self._sessions: Set[str] = set()
        self._techniques: Set[str] = set()
        self._apps: Set[str] = set()

    def start(self, *, actor: str, duration_s: int, intensity: int,
              secondary_actors: int, drive_turns: bool = True) -> None:
        """Begin a fresh campaign. Re-running is deliberately not a no-op: a new
        ``campaign_id`` and zeroed counters mean a rehearsal produces a second,
        distinct campaign (spec §2)."""
        self.actor = actor or "t.nguyen"
        self.duration_s = int(duration_s)
        self.intensity = int(intensity)
        self.secondary_actors = int(secondary_actors)
        self.drove_turns = bool(drive_turns)
        self.campaign_id = str(uuid.uuid4())
        self._start_time = time.time()
        self.turns_sent = 0
        self.blocked = 0
        self.allowed = 0
        self.errors = 0
        self._sessions = set()
        self._techniques = set()
        self._apps = set()
        self.enabled = True
        logger.warning(
            "spray_campaign START id=%s actor=%s duration_s=%s intensity=%s "
            "secondary_actors=%s drive_turns=%s",
            self.campaign_id, self.actor, self.duration_s, self.intensity,
            self.secondary_actors, self.drove_turns,
        )

    def stop(self) -> None:
        self.enabled = False
        self._start_time = None
        logger.warning(
            "spray_campaign STOP id=%s turns=%s blocked=%s allowed=%s errors=%s",
            self.campaign_id, self.turns_sent, self.blocked, self.allowed, self.errors,
        )

    def is_active(self) -> bool:
        if not self.enabled:
            return False
        if self.duration_s and self._start_time and \
                (time.time() - self._start_time) > self.duration_s:
            self.enabled = False  # auto-expire even if the driver task died
            return False
        return True

    def record_turn(self, *, session_id: str, technique: str, app_name: str,
                    blocked: Optional[bool]) -> None:
        """Tally one completed turn. ``blocked=None`` means the turn errored."""
        self.turns_sent += 1
        self._sessions.add(session_id)
        self._techniques.add(technique)
        self._apps.add(app_name)
        if blocked is None:
            self.errors += 1
        elif blocked:
            self.blocked += 1
        else:
            self.allowed += 1

    def status(self) -> Dict[str, Any]:
        active = self.is_active()
        elapsed = (time.time() - self._start_time) if self._start_time else 0.0
        remaining = None
        if active and self.duration_s and self._start_time:
            remaining = max(0, int(self.duration_s - elapsed))
        return {
            "active": active,
            "campaign_id": self.campaign_id,
            "actor": self.actor,
            "duration_s": self.duration_s,
            "intensity": self.intensity,
            "secondary_actors": self.secondary_actors,
            "elapsed_s": int(elapsed) if self._start_time else 0,
            "remaining_s": remaining,
            "drove_turns": self.drove_turns,
            "turns_sent": self.turns_sent,
            "blocked": self.blocked,
            "allowed": self.allowed,
            "errors": self.errors,
            "distinct_sessions": len(self._sessions),
            "techniques_used": sorted(self._techniques),
            "apps_targeted": sorted(self._apps),
        }


spray_campaign = SprayCampaign()
