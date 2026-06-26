from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Dict, Any
import asyncio
import logging
import uuid
import json
from datetime import datetime

from backend.config import settings
from backend.models.schemas import ChatRequest, ChatResponse, MessageRole, MessageType
from backend.models.db_models import Conversation
from backend.database.db import get_db
from backend.services.recommendation_engine import RecommendationEngine
from backend.services.auto_prompter import auto_prompter
from backend.services.enduser_pool import pick_enduser_id
from backend.logging.governance_logger import governance_logger
from backend.incident_mode import incident_mode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])
recommendation_engine = RecommendationEngine()

# In-memory session store (in production, use Redis or similar)
sessions: Dict[str, Dict[str, Any]] = {}


def _dispatch_chat_turn(**kwargs: Any) -> Dict[str, Any]:
    """Route a chat turn to the agentic LangGraph workflow or the legacy engine.

    When ``settings.use_agentic_engine`` is enabled the request is served by the
    supervisor-routed multi-agent graph (backend/agents). If the agentic
    dependencies are missing or the graph cannot be built, we transparently fall
    back to ``RecommendationEngine.process_message`` so the service stays up.
    Both paths return an identically-shaped dict and emit the same governance
    events, so this switch is invisible to the rest of the request flow.
    """
    if settings.use_agentic_engine:
        try:
            from backend.agents.graph import run_turn

            return run_turn(**kwargs)
        except ImportError as exc:
            logger.warning(
                "Agentic engine unavailable (%s); using legacy RecommendationEngine",
                exc,
            )
        except Exception:  # noqa: BLE001 - never fail the request on build issues
            logger.exception(
                "Agentic engine failed to run; using legacy RecommendationEngine"
            )
    return recommendation_engine.process_message(**kwargs)

@router.post("/message", response_model=ChatResponse)
async def send_message(
    chat_request: ChatRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    """Send a message and get a response"""

    # request.client is Optional per the ASGI spec (None under the TestClient and
    # behind some proxies); guard it like the access-key / request-logging middleware.
    client_host = request.client.host if request.client else None

    # Demo-incident fault injection: inflate latency and/or fail with 5xx so the
    # demobot-v3 APM service breaches its latency / error-rate detectors, giving
    # the AI Troubleshooting Agent an alert to analyze. See backend/incident_mode.py.
    if incident_mode.is_active():
        delay = incident_mode.delay_seconds()
        if delay:
            await asyncio.sleep(delay)
        if incident_mode.should_error():
            raise HTTPException(status_code=500, detail="Simulated demo incident on demobot-v3")

    # Get or create session
    session_id = chat_request.session_id

    # Check if session exists (in-memory or database)
    if session_id not in sessions:
        # Check database first
        existing_conversation = db.query(Conversation).filter(
            Conversation.session_id == session_id
        ).first()

        if existing_conversation:
            # Load existing session from database into memory
            sessions[session_id] = {
                "created_at": existing_conversation.created_at,
                "messages": existing_conversation.messages or [],
                "disclaimer_accepted": existing_conversation.disclaimer_accepted,
                "escalated": existing_conversation.escalated,
                "enduser_id": pick_enduser_id()
            }
        else:
            # New session - require disclaimer
            if not chat_request.disclaimer_accepted:
                raise HTTPException(
                    status_code=400,
                    detail="Medical disclaimer must be accepted before starting consultation"
                )

            sessions[session_id] = {
                "created_at": datetime.utcnow(),
                "messages": [],
                "disclaimer_accepted": True,
                "escalated": False,
                "enduser_id": pick_enduser_id()
            }

            # Create conversation in database
            conversation = Conversation(
                session_id=session_id,
                disclaimer_accepted=True,
                messages=[]
            )
            db.add(conversation)
            db.commit()

            # Log audit event
            governance_logger.log_audit(
                session_id=session_id,
                request_id=str(uuid.uuid4()),
                action="session_started",
                actor="user",
                details={
                    "disclaimer_accepted": True,
                    "client_address": client_host
                },
                ip_address=client_host,
                enduser_id=sessions[session_id]["enduser_id"]
            )

    session = sessions[session_id]

    # Add user message to session
    user_message = {
        "role": MessageRole.USER,
        "content": chat_request.message,
        "timestamp": datetime.utcnow().isoformat(),
        "type": MessageType.USER_MESSAGE
    }
    session["messages"].append(user_message)

    # Process message (agentic LangGraph workflow, with legacy fallback)
    response_data = _dispatch_chat_turn(
        session_id=session_id,
        user_message=chat_request.message,
        conversation_history=session["messages"],
        client_address=client_host,
        theme=chat_request.theme,
        force_pii_injection=chat_request.force_pii_injection,
        force_toxic_injection=chat_request.force_toxic_injection,
        force_hallucination_injection=chat_request.force_hallucination_injection,
        force_boundary_injection=chat_request.force_boundary_injection,
        ai_defense_review=chat_request.ai_defense_review,
        internal_policy_review=chat_request.internal_policy_review,
        enduser_id=session.get("enduser_id")
    )

    # Add assistant message to session
    assistant_message = {
        "role": MessageRole.ASSISTANT,
        "content": response_data["message"],
        "timestamp": datetime.utcnow().isoformat(),
        "type": response_data["type"],
        "severity": response_data.get("severity"),
        "metadata": response_data.get("metadata", {})
    }
    session["messages"].append(assistant_message)

    # Update session escalation status
    if response_data.get("escalated"):
        session["escalated"] = True

    # Update database
    conversation = db.query(Conversation).filter(
        Conversation.session_id == session_id
    ).first()

    if conversation:
        conversation.messages = session["messages"]
        conversation.escalated = session["escalated"]
        conversation.updated_at = datetime.utcnow()
        if response_data.get("severity"):
            conversation.final_severity = response_data["severity"].value
        db.commit()

    # Return response
    return ChatResponse(
        session_id=session_id,
        message=response_data["message"],
        type=response_data["type"],
        severity=response_data.get("severity"),
        escalated=response_data.get("escalated", False),
        timestamp=datetime.utcnow()
    )

@router.get("/session/{session_id}")
async def get_session(session_id: str, db: Session = Depends(get_db)):
    """Get session conversation history"""

    # Check in-memory first
    if session_id in sessions:
        return {
            "session_id": session_id,
            "messages": sessions[session_id]["messages"],
            "escalated": sessions[session_id].get("escalated", False),
            "created_at": sessions[session_id]["created_at"]
        }

    # Check database
    conversation = db.query(Conversation).filter(
        Conversation.session_id == session_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "messages": conversation.messages,
        "escalated": conversation.escalated,
        "created_at": conversation.created_at
    }

@router.post("/session/new")
async def create_session():
    """Create a new session"""
    session_id = str(uuid.uuid4())
    return {"session_id": session_id}

@router.get("/disclaimer")
async def get_disclaimer():
    """Get medical disclaimer text"""
    return {
        "title": "Medical Disclaimer",
        "content": """**IMPORTANT MEDICAL DISCLAIMER**

This service provides general health information and guidance only. It is NOT a substitute for professional medical advice, diagnosis, or treatment.

**Key Points:**

• This is NOT emergency medical care. If you are experiencing a medical emergency, call 911 or go to the nearest emergency room immediately.

• The information provided is for educational purposes only and should not be used to diagnose or treat any health condition.

• Always seek the advice of your physician or other qualified health provider with any questions you may have regarding a medical condition.

• Never disregard professional medical advice or delay in seeking it because of something you have read here.

• This service does NOT provide prescription medication advice or pediatric dosing.

• If you are pregnant, elderly, or have chronic health conditions, consult with a healthcare provider before following any recommendations.

**By continuing, you acknowledge that:**

1. You understand this is not professional medical care
2. You will seek emergency care for urgent symptoms
3. You will consult a healthcare provider for proper diagnosis and treatment
4. You understand the limitations of this service

Do you accept these terms and wish to continue?
""",
        "version": "1.0"
    }


# Auto-prompter endpoints
@router.post("/auto-prompt/start")
async def start_auto_prompter():
    """Start automatic session generation (one session per minute)"""
    if auto_prompter.is_running:
        return {"status": "already_running", "message": "Auto-prompter is already running", **auto_prompter.stats}
    
    await auto_prompter.start()
    return {"status": "started", "message": "Auto-prompter started - will create one session per minute", **auto_prompter.stats}


@router.post("/auto-prompt/stop")
async def stop_auto_prompter():
    """Stop automatic session generation"""
    if not auto_prompter.is_running:
        return {"status": "already_stopped", "message": "Auto-prompter is not running", **auto_prompter.stats}
    
    await auto_prompter.stop()
    return {"status": "stopped", "message": "Auto-prompter stopped", **auto_prompter.stats}


@router.get("/auto-prompt/status")
async def get_auto_prompter_status():
    """Get auto-prompter status"""
    return {"status": "running" if auto_prompter.is_running else "stopped", **auto_prompter.stats}
