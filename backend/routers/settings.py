"""Settings API: local log directory + Splunk HEC destinations.

Auto-gated by the access-key middleware (not in PUBLIC_PATHS). Tokens are
accepted on write but never returned — reads surface only token_present/last4.
"""
from __future__ import annotations

from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from backend import settings_store
from backend.hec.runtime import hec_runtime
from backend.model_emitter import MODEL_CHOICES, model_emitter

router = APIRouter(prefix="/api", tags=["settings"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LogsSettings(BaseModel):
    logs_directory: str = Field(min_length=1, max_length=512)


def _validate_https(v: Optional[str]) -> Optional[str]:
    if v:
        v = v.strip()
        if v and not v.lower().startswith("https://"):
            raise ValueError("HEC URL must use https://")
    return v


class HECDestinationWrite(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    enabled: Optional[bool] = None
    url: Optional[str] = Field(default=None, max_length=512)
    token: Optional[str] = Field(default=None, max_length=200)
    verify_tls: Optional[bool] = None
    index: Optional[str] = Field(default=None, max_length=80)
    source: Optional[str] = Field(default=None, max_length=200)
    sourcetype: Optional[str] = Field(default=None, max_length=200)
    host: Optional[str] = Field(default=None, max_length=200)
    sourcetype_map: Optional[Dict[str, str]] = None
    batch_size: Optional[int] = Field(default=None, ge=1, le=10000)
    flush_interval_s: Optional[float] = Field(default=None, ge=0.1, le=300.0)
    queue_max: Optional[int] = Field(default=None, ge=1, le=1000000)
    request_timeout_s: Optional[float] = Field(default=None, ge=1.0, le=300.0)
    max_retries: Optional[int] = Field(default=None, ge=0, le=10)

    @field_validator("url")
    @classmethod
    def _url_https(cls, v):
        return _validate_https(v)


# ---------------------------------------------------------------------------
# Local logging settings
# ---------------------------------------------------------------------------
@router.get("/settings")
async def get_settings():
    return {"logs_directory": settings_store.get_logs_directory()}


@router.put("/settings")
async def update_settings(body: LogsSettings):
    path = settings_store.set_logs_directory(body.logs_directory)
    return {"logs_directory": path}


# ---------------------------------------------------------------------------
# Demo model-name emission override
# ---------------------------------------------------------------------------
class EmitModelSettings(BaseModel):
    enabled: bool = False
    model_name: str = Field(default="", max_length=120)
    random: bool = False


@router.get("/settings/emit-model")
async def get_emit_model():
    return model_emitter.status()


# ---------------------------------------------------------------------------
# Active LLM provider selection (which model backs the chat)
# ---------------------------------------------------------------------------
class AiProviderSettings(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(default="", max_length=200)
    # Optional per-provider access fields (e.g. {"api_key": "..."}). Secrets are
    # write-only: blank values are ignored so an existing key is never wiped.
    fields: Optional[Dict[str, str]] = None


def _provider_payload() -> dict:
    """Current provider/model + discovered models + per-provider access-field
    metadata. Secret field values are NEVER included (presence only)."""
    from backend import model_catalog

    payload = settings_store.get_ai_provider()
    payload["available"] = model_catalog.available()
    payload["fields"] = settings_store.get_provider_fields()
    return payload


@router.get("/settings/ai-provider")
async def get_ai_provider():
    from backend import model_catalog

    model_catalog.ensure_loaded()  # bounded sync refresh if startup discovery hasn't finished
    return _provider_payload()


@router.put("/settings/ai-provider")
async def update_ai_provider(body: AiProviderSettings):
    from backend import model_catalog

    provider = body.provider.strip().lower()
    if provider not in settings_store.AI_PROVIDER_CHOICES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider: {provider}. Valid: {', '.join(settings_store.AI_PROVIDER_CHOICES)}",
        )
    try:
        settings_store.set_ai_provider(provider, body.model)
        if body.fields:
            settings_store.set_provider_creds(provider, body.fields)
            # New creds may reveal models — re-scan this so the dropdown updates.
            model_catalog.refresh()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _provider_payload()


@router.post("/settings/ai-provider/refresh")
async def refresh_ai_provider_models():
    """Re-scan every provider for its currently available models (e.g. after
    pulling a new Ollama model). Returns the same shape as GET."""
    from backend import model_catalog

    model_catalog.refresh()
    return _provider_payload()


class ProviderCredsSettings(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    fields: Dict[str, str] = Field(default_factory=dict)


@router.put("/settings/provider-creds")
async def update_provider_creds(body: ProviderCredsSettings):
    """Update one provider's access creds (API key / base URL / region) WITHOUT
    changing the active provider — unlike PUT /settings/ai-provider, which also
    switches the chat to that provider. Backs the Settings-page credentials section
    (provider/model selection itself moved to the /app header). Blank secrets are
    ignored (an existing key is never wiped); new creds may reveal models so the
    catalog is re-scanned."""
    from backend import model_catalog

    provider = body.provider.strip().lower()
    if provider not in settings_store.AI_PROVIDER_CHOICES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider: {provider}. Valid: {', '.join(settings_store.AI_PROVIDER_CHOICES)}",
        )
    settings_store.set_provider_creds(provider, body.fields or {})
    model_catalog.refresh()
    return _provider_payload()


@router.put("/settings/emit-model")
async def update_emit_model(body: EmitModelSettings):
    name = (body.model_name or "").strip()
    if name and name not in MODEL_CHOICES:
        raise HTTPException(status_code=422, detail=f"unknown model_name: {name}")
    if body.enabled and not body.random and not name:
        raise HTTPException(
            status_code=422,
            detail="Select a model to emit (or enable Emit Random Model Name).",
        )
    settings_store.set_emit_model(body.enabled, name, body.random)
    return model_emitter.status()


# ---------------------------------------------------------------------------
# HEC destinations
# ---------------------------------------------------------------------------
@router.get("/hec/destinations")
async def list_hec_destinations():
    return {"destinations": [settings_store.mask(d) for d in settings_store.list_destinations()]}


@router.post("/hec/destinations")
async def create_hec_destination(body: HECDestinationWrite):
    record = settings_store.add_destination(body.model_dump(exclude_unset=True))
    await settings_store.reconfigure_hec()
    return settings_store.mask(record)


@router.get("/hec/destinations/{dest_id}")
async def get_hec_destination(dest_id: str):
    dest = settings_store.get_destination(dest_id)
    if dest is None:
        raise HTTPException(status_code=404, detail="destination not found")
    return settings_store.mask(dest)


@router.put("/hec/destinations/{dest_id}")
async def update_hec_destination(dest_id: str, body: HECDestinationWrite):
    record = settings_store.update_destination(dest_id, body.model_dump(exclude_unset=True))
    if record is None:
        raise HTTPException(status_code=404, detail="destination not found")
    await settings_store.reconfigure_hec()
    return settings_store.mask(record)


@router.delete("/hec/destinations/{dest_id}")
async def delete_hec_destination(dest_id: str):
    if not settings_store.delete_destination(dest_id):
        raise HTTPException(status_code=404, detail="destination not found")
    await settings_store.reconfigure_hec()
    return {"removed": True, "id": dest_id}


@router.post("/hec/destinations/{dest_id}/test")
async def test_hec_destination(dest_id: str):
    dest = settings_store.get_destination(dest_id)
    if dest is None:
        raise HTTPException(status_code=404, detail="destination not found")
    result = await hec_runtime.test_send(settings_store.to_hec_config(dest))
    return {
        "ok": result.ok,
        "status_code": result.status_code,
        "latency_ms": round(result.latency_ms, 1),
        "error": result.error,
    }


# ---------------------------------------------------------------------------
# Live forwarder stats
# ---------------------------------------------------------------------------
@router.get("/hec/stats")
async def hec_stats():
    return {"destinations": [vars(s) for s in hec_runtime.stats()]}


@router.get("/hec/stats/{dest_id}")
async def hec_stats_for(dest_id: str):
    snap = hec_runtime.stats_for(dest_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="destination not found")
    return vars(snap)
