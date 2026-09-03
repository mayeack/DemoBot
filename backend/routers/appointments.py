"""Appointments API (docs/scheduling.md) — the backend of ``/appointments-ui``.

Every route is scoped by the caller's ``client_id``, the same browser-minted id
the chat sends on each turn. It is a PARTITION key, not authorization: all
visitors share one access key (the middleware gates these routes like every
other path), so a client_id only selects a browser's own bookings.

Mutations from the page are logged as audit events (``appointment_cancelled``
/ ``appointment_rescheduled``, actor ``user``); bookings made in the chat are
logged by the scheduling node as ``tool_call`` governance events.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.agents.themes import get_theme
from backend.logging.governance_logger import governance_logger
from backend.services import scheduling as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/appointments", tags=["appointments"])

_CLIENT_ID = r"^[A-Za-z0-9_.:-]{1,64}$"


class AppointmentUpdate(BaseModel):
    """Body model (not bare scalars — FastAPI would bind those as required query
    params, the escalation-review bug). ``status: cancelled`` cancels;
    ``start_utc`` (naive UTC ISO) reschedules."""
    client_id: str = Field(pattern=_CLIENT_ID)
    status: Optional[Literal["cancelled"]] = None
    start_utc: Optional[str] = Field(default=None, max_length=32)


def _describe(row: Dict[str, Any], tz: Optional[str]) -> Dict[str, Any]:
    profile = get_theme(row.get("theme")).scheduling
    out = svc.describe(row, profile, tz if svc.is_valid_tz(tz) else None)
    out["theme_label"] = get_theme(row.get("theme")).label
    return out


@router.get("")
def list_appointments(
    client_id: str = Query(..., pattern=_CLIENT_ID),
    status: str = Query("scheduled", pattern="^(scheduled|cancelled|all)$"),
    theme: Optional[str] = Query(None, max_length=32),
    tz: Optional[str] = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    """This browser's appointments, soonest first."""
    try:
        rows: List[Dict[str, Any]] = svc.store.list_all(client_id=client_id, status=status, theme=theme, limit=limit)
    except Exception as exc:  # noqa: BLE001 - surface as a clean 503, never a traceback
        logger.exception("appointments list failed")
        raise HTTPException(status_code=503, detail="appointments store unavailable") from exc
    items = [_describe(r, tz) for r in rows]
    return {"client_id": client_id, "status": status, "total": len(items), "appointments": items}


@router.get("/{appointment_id}")
def get_appointment(
    appointment_id: str,
    client_id: str = Query(..., pattern=_CLIENT_ID),
    tz: Optional[str] = Query(None, max_length=64),
) -> Dict[str, Any]:
    row = svc.store.get(appointment_id)
    if row is None or row.get("client_id") != client_id:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return _describe(row, tz)


@router.put("/{appointment_id}")
def update_appointment(appointment_id: str, body: AppointmentUpdate, request: Request) -> Dict[str, Any]:
    row = svc.store.get(appointment_id)
    if row is None or row.get("client_id") != body.client_id:
        raise HTTPException(status_code=404, detail="Appointment not found")
    client_host = request.client.host if request.client else None

    if body.status == "cancelled":
        updated = svc.store.cancel(appointment_id, client_id=body.client_id)
        action = "appointment_cancelled"
        details = {"appointment_id": appointment_id, "client_id": body.client_id, "theme": row.get("theme")}
    elif body.start_utc:
        try:
            start = datetime.fromisoformat(body.start_utc.replace("Z", ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="start_utc must be a naive UTC ISO timestamp") from exc
        if start <= datetime.utcnow():
            raise HTTPException(status_code=422, detail="start_utc must be in the future")
        updated = svc.store.reschedule(appointment_id, start_utc=start, client_id=body.client_id)
        action = "appointment_rescheduled"
        details = {"appointment_id": appointment_id, "client_id": body.client_id, "theme": row.get("theme"),
                   "from": row.get("start_utc"), "to": start.isoformat()}
    else:
        raise HTTPException(status_code=422, detail="nothing to update: send status=cancelled or start_utc")

    if updated is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    try:
        governance_logger.log_audit(
            session_id=row.get("session_id") or "appointments-ui",
            request_id=str(uuid.uuid4()),
            action=action,
            actor="user",
            details=details,
            ip_address=client_host,
        )
    except Exception:  # noqa: BLE001 - logging must never break the update
        logger.exception("appointment audit logging failed")
    return _describe(updated, None)
