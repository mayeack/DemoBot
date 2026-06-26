"""Demo-incident control endpoints.

Manually inject APM latency/errors and drive sustained authenticated load so the
demobot-v3 service breaches its Splunk APM detectors — letting us demo the AI
Troubleshooting Agent. Auto-gated by the access-key middleware.

The load driver has the app call its own /api/chat endpoints with the access key
(the auto-prompter is unauthenticated and gets 401'd), concurrently (each request
is slow by design), so the latency/error MetricSets stay dense above threshold.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.config import settings
from backend.incident_mode import incident_mode

router = APIRouter(prefix="/api/incident", tags=["incident"])
logger = logging.getLogger(__name__)

_BASE = "http://127.0.0.1:8001"
_expiry_task: Optional[asyncio.Task] = None
_load_task: Optional[asyncio.Task] = None


class IncidentStart(BaseModel):
    latency_ms: int = Field(default=20000, ge=0, le=120000)
    error_rate: float = Field(default=0.6, ge=0.0, le=1.0)
    duration_s: int = Field(default=600, ge=10, le=3600)
    drive_traffic: bool = True


async def _fire_one(auth) -> None:
    """One authenticated chat turn against ourselves (slow + maybe 5xx by design)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(150.0)) as c:
            r = await c.post(f"{_BASE}/api/chat/session/new", auth=auth)
            if r.status_code != 200:
                return
            sid = r.json().get("session_id")
            await c.post(
                f"{_BASE}/api/chat/message", auth=auth,
                json={"session_id": sid, "message": "incident load probe",
                      "disclaimer_accepted": True},
            )
    except Exception:  # noqa: BLE001 - 500s/timeouts are expected and are the point
        pass


async def _drive_load() -> None:
    """Steady, concurrent, authenticated load while the incident is active, so the
    latency/error APM metrics stay dense above their detector thresholds."""
    auth = ("x", settings.access_key) if getattr(settings, "access_key", "") else None
    sem = asyncio.Semaphore(25)

    async def bounded():
        async with sem:
            await _fire_one(auth)

    logger.warning("incident load driver started (authenticated=%s)", bool(auth))
    try:
        while incident_mode.is_active():
            asyncio.create_task(bounded())
            await asyncio.sleep(2.5)
    except asyncio.CancelledError:
        pass
    logger.warning("incident load driver stopped")


def _status() -> dict:
    st = incident_mode.status()
    st["load_driver_running"] = bool(_load_task and not _load_task.done())
    return st


async def _do_stop() -> None:
    global _load_task
    incident_mode.stop()
    if _load_task and not _load_task.done():
        _load_task.cancel()
    _load_task = None


async def _auto_stop_after(duration_s: int) -> None:
    try:
        await asyncio.sleep(duration_s)
    except asyncio.CancelledError:
        return
    await _do_stop()
    logger.warning("incident_mode auto-stopped after %ss", duration_s)


@router.post("/start")
async def start_incident(body: IncidentStart):
    global _expiry_task, _load_task
    incident_mode.start(latency_ms=body.latency_ms, error_rate=body.error_rate,
                        duration_s=body.duration_s, drove_traffic=body.drive_traffic)
    if body.drive_traffic:
        if _load_task and not _load_task.done():
            _load_task.cancel()
        _load_task = asyncio.create_task(_drive_load())
    if _expiry_task and not _expiry_task.done():
        _expiry_task.cancel()
    _expiry_task = asyncio.create_task(_auto_stop_after(body.duration_s))
    return _status()


@router.post("/stop")
async def stop_incident():
    global _expiry_task
    if _expiry_task and not _expiry_task.done():
        _expiry_task.cancel()
    await _do_stop()
    return _status()


@router.get("/status")
async def incident_status():
    return _status()
