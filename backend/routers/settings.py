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
