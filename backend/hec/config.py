"""Splunk HEC destination config (one per integration).

Mirrors the shape of ThreatGenerator's ``HECConfig`` but adapted for DemoBot:
the forwarded "event" is a governance/audit log dict keyed by ``log_type``
(governance|escalation|audit|error), and the token is carried in the config
(persisted in the local SQLite DB) rather than the OS keychain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

DEFAULT_INDEX = "main"
DEFAULT_SOURCE = "medadvice"
DEFAULT_SOURCETYPE = "medadvice:governance"
DEFAULT_HOST = "medadvice"


@dataclass
class HECConfig:
    id: str = ""
    name: str = ""
    enabled: bool = False
    url: str = ""
    token: str = ""
    verify_tls: bool = True
    index: str = DEFAULT_INDEX
    source: str = DEFAULT_SOURCE
    sourcetype: str = DEFAULT_SOURCETYPE
    host: str = DEFAULT_HOST
    # Optional per-log-type sourcetype override, e.g. {"escalation": "medadvice:escalation"}
    sourcetype_map: Dict[str, str] = field(default_factory=dict)
    batch_size: int = 100
    flush_interval_s: float = 2.0
    queue_max: int = 10000
    request_timeout_s: float = 10.0
    max_retries: int = 3

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HECConfig":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
