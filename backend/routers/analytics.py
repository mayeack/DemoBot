"""Session analytics — the NVIDIA AI Virtual Assistant blueprint's analytics service.

The reference blueprint runs a separate analytics microservice that supervisors
query ON DEMAND (never per turn): a session summary with overall sentiment,
per-message sentiment, a session list, and feedback endpoints. This router is
that service inside DemoBot, over the same ``Conversation`` rows the chat writes,
with results cached in ``blueprint_analytics``. It uses the active chat model
through the normal LLM boundary (``invoke_chat``), so it is governed and
telemetered like everything else and stubbed by the API suite.

Routes mirror the blueprint's (``/sessions``, ``/session/summary``,
``/session/conversation``, ``/feedback/{sentiment|summary|session}``) under
``/api/analytics``. Gated by the access-key middleware.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database.db import get_db
from backend.models.db_models import BlueprintAnalytics, Conversation

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)

_SENTIMENTS = ("positive", "neutral", "negative")

_SUMMARY_SYSTEM = (
    "You are the analytics service for a governed advice assistant. Given a chat "
    "transcript, produce a concise supervisor summary (2-4 sentences: what the user "
    "needed, what the assistant advised, whether anything was escalated or withheld) "
    "and the user's OVERALL sentiment. Respond with ONLY a JSON object: "
    '{"summary": "<text>", "sentiment": "positive|neutral|negative"}'
)
_PER_MESSAGE_SYSTEM = (
    "You are the analytics service for a governed advice assistant. For EACH user "
    "message in the transcript (in order), classify the user's sentiment. Respond "
    'with ONLY a JSON object: {"sentiments": ["positive|neutral|negative", ...]} with '
    "exactly one entry per user message."
)


# ----------------------------------------------------------------- helpers
def _role(msg: Dict[str, Any]) -> str:
    role = msg.get("role")
    return getattr(role, "value", role) or ""


def _transcript(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for m in messages or []:
        role = _role(m)
        if role in ("user", "assistant"):
            lines.append(f"{role}: {m.get('content', '')}")
    return "\n".join(lines)


def _normalize_sentiment(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in _SENTIMENTS else "neutral"


def _parse_json(text: str) -> Dict[str, Any]:
    text = text or ""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        obj = json.loads(candidate[start:end + 1])
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _llm_json(system: str, transcript: str, max_tokens: int = 400) -> Dict[str, Any]:
    """One analytics call through the normal LLM boundary (stubbable)."""
    from backend.agents.llm import ChatModelError, invoke_chat

    try:
        response = invoke_chat(
            settings, system=system,
            messages=[{"role": "user", "content": f"TRANSCRIPT:\n{transcript}"}],
            max_tokens=max_tokens, temperature=0.0,
        )
    except ChatModelError as exc:
        raise HTTPException(status_code=503, detail=f"analytics model unavailable: {exc}") from exc
    return _parse_json(response.content)


def _conversation(db: Session, session_id: str) -> Conversation:
    conv = db.query(Conversation).filter(Conversation.session_id == session_id).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return conv


def _row(db: Session, session_id: str, create: bool = False) -> Optional[BlueprintAnalytics]:
    row = db.query(BlueprintAnalytics).filter(BlueprintAnalytics.session_id == session_id).first()
    if row is None and create:
        row = BlueprintAnalytics(session_id=session_id, per_message=[], feedback={})
        db.add(row)
    return row


# ----------------------------------------------------------------- routes
@router.get("/health")
async def analytics_health():
    return {"status": "healthy", "service": "analytics"}


@router.get("/sessions")
async def list_sessions(hours: int = 24, db: Session = Depends(get_db)):
    """Sessions active in the last ``hours`` (the blueprint's GET /sessions)."""
    hours = max(1, min(int(hours), 24 * 90))
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (db.query(Conversation)
            .filter((Conversation.updated_at >= cutoff) | (Conversation.created_at >= cutoff))
            .order_by(Conversation.updated_at.desc()).limit(500).all())
    analysed = {r.session_id for r in db.query(BlueprintAnalytics.session_id).all()}
    return {"hours": hours, "sessions": [
        {
            "session_id": c.session_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "turns": sum(1 for m in (c.messages or []) if _role(m) == "user"),
            "escalated": bool(c.escalated),
            "final_severity": c.final_severity,
            "analysed": c.session_id in analysed,
        } for c in rows
    ]}


@router.get("/session/summary")
async def session_summary(session_id: str, regenerate: bool = False, db: Session = Depends(get_db)):
    """Summary + overall sentiment for a session (generated once, cached)."""
    conv = _conversation(db, session_id)
    row = _row(db, session_id)
    if row is not None and row.summary and not regenerate:
        return {"session_id": session_id, "summary": row.summary, "sentiment": row.sentiment,
                "generated_at": row.generated_at.isoformat() if row.generated_at else None, "cached": True}
    transcript = _transcript(conv.messages or [])
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="Session has no messages to summarize")
    data = await run_in_threadpool(_llm_json, _SUMMARY_SYSTEM, transcript)
    row = _row(db, session_id, create=True)
    row.summary = str(data.get("summary") or "").strip() or "No summary could be generated."
    row.sentiment = _normalize_sentiment(data.get("sentiment"))
    row.generated_at = datetime.utcnow()
    db.commit()
    return {"session_id": session_id, "summary": row.summary, "sentiment": row.sentiment,
            "generated_at": row.generated_at.isoformat(), "cached": False}


@router.get("/session/conversation")
async def session_conversation(session_id: str, regenerate: bool = False, db: Session = Depends(get_db)):
    """The transcript with a sentiment per user message (generated once, cached)."""
    conv = _conversation(db, session_id)
    messages = [m for m in (conv.messages or []) if _role(m) in ("user", "assistant")]
    row = _row(db, session_id)
    user_count = sum(1 for m in messages if _role(m) == "user")
    cached = list(row.per_message or []) if row is not None else []
    if not regenerate and cached and sum(1 for m in cached if m.get("role") == "user") == user_count:
        return {"session_id": session_id, "messages": cached, "cached": True}
    sentiments: List[str] = []
    if user_count:
        data = await run_in_threadpool(_llm_json, _PER_MESSAGE_SYSTEM, _transcript(messages), 200)
        got = data.get("sentiments") if isinstance(data.get("sentiments"), list) else []
        sentiments = [_normalize_sentiment(s) for s in got]
        if len(sentiments) != user_count:  # never let a miscount misalign the transcript
            sentiments = ["neutral"] * user_count
    out: List[Dict[str, Any]] = []
    ui = 0
    for i, m in enumerate(messages):
        role = _role(m)
        item = {"index": i, "role": role, "content": m.get("content", ""), "timestamp": m.get("timestamp")}
        if role == "user":
            item["sentiment"] = sentiments[ui] if ui < len(sentiments) else "neutral"
            ui += 1
        out.append(item)
    row = _row(db, session_id, create=True)
    row.per_message = out
    db.commit()
    return {"session_id": session_id, "messages": out, "cached": False}


class FeedbackRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = Field(default=None, max_length=2000)


@router.post("/feedback/{kind}")
async def feedback(kind: str, body: FeedbackRequest, db: Session = Depends(get_db)):
    """Store supervisor feedback on the sentiment / summary / session (the
    blueprint's data-flywheel hooks)."""
    if kind not in ("sentiment", "summary", "session"):
        raise HTTPException(status_code=404, detail="unknown feedback kind")
    _conversation(db, body.session_id)
    row = _row(db, body.session_id, create=True)
    fb = dict(row.feedback or {})
    fb[kind] = {"rating": body.rating, "comment": body.comment, "at": datetime.utcnow().isoformat()}
    row.feedback = fb  # reassign so SQLAlchemy tracks the JSON change
    db.commit()
    return {"ok": True, "session_id": body.session_id, "feedback": fb}
