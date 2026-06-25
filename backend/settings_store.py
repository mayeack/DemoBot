"""Runtime-mutable app settings, persisted in a single ``app_settings`` row.

Holds the local log directory and the list of Splunk HEC destinations. This is
MedAdvice's analog of ThreatGenerator's active-config store. Tokens are kept in
the JSON blob (local SQLite, gitignored) and stripped by ``mask`` before they
ever reach an API response.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from backend.database.db import get_db_context
from backend.hec.config import HECConfig
from backend.hec.runtime import hec_runtime
from backend.models.db_models import AppSettings

logger = logging.getLogger(__name__)

_ROW_ID = 1
_DEFAULTS: Dict[str, Any] = {
    "logs_directory": "logs",
    "hec_destinations": [],
    "emit_model": {"enabled": False, "model_name": "", "random": False},
    # Runtime override of the active LLM provider (empty = use .env/config default).
    "ai_provider": {"provider": "", "model": ""},
}
_ID_RE = re.compile(r"[^a-z0-9-]+")

# Supported LLM providers and the ``settings`` attribute that holds each one's
# model id. Keep in sync with backend/agents/llm.py::get_chat_model and
# backend/services/ai_client.py::get_ai_client.
AI_PROVIDER_CHOICES: List[str] = ["anthropic", "bedrock", "openai", "ollama"]
_PROVIDER_MODEL_ATTR: Dict[str, str] = {
    "anthropic": "anthropic_model",
    "bedrock": "bedrock_model_id",
    "openai": "openai_model",
    "ollama": "ollama_model",
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load() -> Dict[str, Any]:
    """Return the persisted settings, seeding defaults on first run."""
    with get_db_context() as db:
        row = db.query(AppSettings).filter(AppSettings.id == _ROW_ID).first()
        if row is None:
            row = AppSettings(id=_ROW_ID, data=dict(_DEFAULTS))
            db.add(row)
            db.commit()
            return dict(_DEFAULTS)
        data = dict(_DEFAULTS)
        data.update(row.data or {})
        return data


def _persist(data: Dict[str, Any]) -> None:
    with get_db_context() as db:
        row = db.query(AppSettings).filter(AppSettings.id == _ROW_ID).first()
        if row is None:
            row = AppSettings(id=_ROW_ID, data=data)
            db.add(row)
        else:
            row.data = data  # reassign so SQLAlchemy tracks the JSON change
        db.commit()


# ---------------------------------------------------------------------------
# Log directory
# ---------------------------------------------------------------------------
def get_logs_directory() -> str:
    return load().get("logs_directory") or "logs"


def set_logs_directory(path: str) -> str:
    path = (path or "").strip() or "logs"
    data = load()
    data["logs_directory"] = path
    _persist(data)
    try:
        from backend.logging.governance_logger import governance_logger
        governance_logger.set_logs_directory(path)
    except Exception:
        logger.exception("failed to apply logs_directory at runtime")
    return path


# ---------------------------------------------------------------------------
# Demo model-name emission override
# ---------------------------------------------------------------------------
def get_emit_model() -> Dict[str, Any]:
    cfg = load().get("emit_model") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "model_name": cfg.get("model_name") or "",
        "random": bool(cfg.get("random", False)),
    }


def set_emit_model(enabled: bool, model_name: str, random_emit: bool) -> Dict[str, Any]:
    cfg = {
        "enabled": bool(enabled),
        "model_name": (model_name or "").strip(),
        "random": bool(random_emit),
    }
    data = load()
    data["emit_model"] = cfg
    _persist(data)
    try:
        from backend.model_emitter import model_emitter
        model_emitter.configure(
            enabled=cfg["enabled"], model_name=cfg["model_name"], random_emit=cfg["random"]
        )
    except Exception:
        logger.exception("failed to apply emit_model at runtime")
    return cfg


# ---------------------------------------------------------------------------
# Active LLM provider selection
# ---------------------------------------------------------------------------
def get_ai_provider() -> Dict[str, Any]:
    """Return the LIVE provider/model in effect plus the per-provider model map.

    Reads the runtime ``settings`` singleton (which reflects .env plus any
    persisted UI override applied at startup), so the UI always shows what is
    actually being used — not just what is stored."""
    from backend.config import settings

    provider = (settings.ai_provider or "anthropic").lower()
    models = {p: (getattr(settings, attr, "") or "") for p, attr in _PROVIDER_MODEL_ATTR.items()}
    return {
        "provider": provider,
        "model": models.get(provider, ""),
        "choices": list(AI_PROVIDER_CHOICES),
        "models": models,
    }


def _apply_ai_provider(provider: str, model: str = "") -> None:
    """Apply the provider/model to the live settings singleton and drop the LLM
    client caches so the next chat turn picks it up (no restart)."""
    from backend.config import settings

    settings.ai_provider = provider
    if model:
        setattr(settings, _PROVIDER_MODEL_ATTR[provider], model)
    try:
        from backend.agents import llm
        llm.clear_caches()
    except Exception:
        logger.exception("failed to clear LLM caches after provider change")


def set_ai_provider(provider: str, model: str = "") -> Dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider not in _PROVIDER_MODEL_ATTR:
        raise ValueError(f"unknown provider: {provider}")
    model = (model or "").strip()
    data = load()
    data["ai_provider"] = {"provider": provider, "model": model}
    _persist(data)
    _apply_ai_provider(provider, model)
    return get_ai_provider()


def apply_ai_provider_from_store() -> None:
    """Startup hook: apply any persisted provider override over the .env default."""
    cfg = load().get("ai_provider") or {}
    provider = (cfg.get("provider") or "").strip().lower()
    if provider in _PROVIDER_MODEL_ATTR:
        _apply_ai_provider(provider, (cfg.get("model") or "").strip())


# ---------------------------------------------------------------------------
# HEC destinations
# ---------------------------------------------------------------------------
def _default_destination() -> Dict[str, Any]:
    c = HECConfig()
    return {
        "id": "", "name": "New destination", "enabled": False, "url": "",
        "token": "", "verify_tls": True, "index": c.index, "source": c.source,
        "sourcetype": c.sourcetype, "host": c.host, "sourcetype_map": {},
        "batch_size": c.batch_size, "flush_interval_s": c.flush_interval_s,
        "queue_max": c.queue_max, "request_timeout_s": c.request_timeout_s,
        "max_retries": c.max_retries,
    }


def _new_id(name: str, existing: set) -> str:
    base = _ID_RE.sub("-", (name or "hec").strip().lower()).strip("-")[:32] or "hec"
    candidate = base
    while not candidate or candidate in existing:
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
    return candidate


def list_destinations() -> List[Dict[str, Any]]:
    return list(load().get("hec_destinations") or [])


def get_destination(dest_id: str) -> Optional[Dict[str, Any]]:
    for d in list_destinations():
        if d.get("id") == dest_id:
            return d
    return None


def add_destination(patch: Dict[str, Any]) -> Dict[str, Any]:
    data = load()
    dests = list(data.get("hec_destinations") or [])
    existing = {d.get("id") for d in dests}
    record = _default_destination()
    record.update({k: v for k, v in (patch or {}).items() if k != "id"})
    record["id"] = _new_id(record.get("name", ""), existing)
    dests.append(record)
    data["hec_destinations"] = dests
    _persist(data)
    return record


def update_destination(dest_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = load()
    dests = list(data.get("hec_destinations") or [])
    updated = None
    for d in dests:
        if d.get("id") == dest_id:
            for k, v in (patch or {}).items():
                if k == "id":
                    continue
                d[k] = v
            updated = d
            break
    if updated is None:
        return None
    data["hec_destinations"] = dests
    _persist(data)
    return updated


def delete_destination(dest_id: str) -> bool:
    data = load()
    dests = list(data.get("hec_destinations") or [])
    new_dests = [d for d in dests if d.get("id") != dest_id]
    if len(new_dests) == len(dests):
        return False
    data["hec_destinations"] = new_dests
    _persist(data)
    return True


# ---------------------------------------------------------------------------
# HEC runtime bridge
# ---------------------------------------------------------------------------
def to_hec_config(dest: Dict[str, Any]) -> HECConfig:
    return HECConfig.from_dict(dest)


def all_configs() -> List[HECConfig]:
    return [to_hec_config(d) for d in list_destinations()]


async def reconfigure_hec() -> None:
    """Push the current destination set into the runtime (restart forwarders)."""
    await hec_runtime.reconfigure(all_configs())


def mask(dest: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the token from a destination for API responses."""
    out = {k: v for k, v in dest.items() if k != "token"}
    token = dest.get("token") or ""
    out["token_present"] = bool(token)
    out["token_last4"] = token[-4:] if token else ""
    return out
