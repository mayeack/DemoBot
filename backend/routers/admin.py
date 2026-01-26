from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from typing import Optional, List
from datetime import datetime, timedelta
import json

from backend.database.db import get_db
from backend.models.db_models import AIGovernanceLog, EscalationQueue, AuditLog, Conversation
from backend.models.schemas import MetricsResponse, SessionExport
from backend.config import settings

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/logs/interactions")
async def get_interaction_logs(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session_id: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db)
):
    """Get AI interaction logs"""

    query = db.query(AIGovernanceLog)

    # Apply filters
    if session_id:
        query = query.filter(AIGovernanceLog.session_id == session_id)

    if start_date:
        query = query.filter(AIGovernanceLog.timestamp >= start_date)

    if end_date:
        query = query.filter(AIGovernanceLog.timestamp <= end_date)

    # Get total count
    total_count = query.count()

    # Apply pagination
    logs = query.order_by(AIGovernanceLog.timestamp.desc()).offset(offset).limit(limit).all()

    return {
        "total": total_count,
        "offset": offset,
        "limit": limit,
        "logs": [
            {
                "id": log.id,
                "session_id": log.session_id,
                "request_id": log.request_id,
                "operation_name": log.operation_name,
                "timestamp": log.timestamp,
                "usage_input_tokens": log.usage_input_tokens,
                "usage_output_tokens": log.usage_output_tokens,
                "usage_total_tokens": log.usage_total_tokens,
                "pii_detected": log.pii_detected,
                "safety_violated": log.safety_violated,
                "guardrail_triggered": log.guardrail_triggered,
                "error_type": log.error_type
            }
            for log in logs
        ]
    }

@router.get("/logs/escalations")
async def get_escalation_logs(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
    severity: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get escalation logs"""

    query = db.query(EscalationQueue)

    # Apply filters
    if status:
        query = query.filter(EscalationQueue.review_status == status)

    if severity:
        query = query.filter(EscalationQueue.severity == severity)

    # Get total count
    total_count = query.count()

    # Apply pagination
    escalations = query.order_by(
        EscalationQueue.timestamp.desc()
    ).offset(offset).limit(limit).all()

    return {
        "total": total_count,
        "offset": offset,
        "limit": limit,
        "escalations": [
            {
                "id": esc.id,
                "escalation_id": esc.escalation_id,
                "session_id": esc.session_id,
                "timestamp": esc.timestamp,
                "reason": esc.reason,
                "severity": esc.severity,
                "symptoms": esc.symptoms,
                "review_status": esc.review_status,
                "reviewer_id": esc.reviewer_id,
                "review_timestamp": esc.review_timestamp
            }
            for esc in escalations
        ]
    }

@router.get("/logs/metrics", response_model=MetricsResponse)
async def get_metrics(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db)
):
    """Get system metrics"""

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=days)

    # Total interactions
    total_interactions = db.query(func.count(AIGovernanceLog.id)).filter(
        AIGovernanceLog.timestamp >= start_time
    ).scalar()

    # Escalation metrics
    escalation_count = db.query(func.count(EscalationQueue.id)).filter(
        EscalationQueue.timestamp >= start_time
    ).scalar()

    escalation_rate = (escalation_count / total_interactions * 100) if total_interactions > 0 else 0

    # Performance metrics
    avg_latency = db.query(
        func.avg(AIGovernanceLog.client_operation_duration)
    ).filter(
        AIGovernanceLog.timestamp >= start_time,
        AIGovernanceLog.client_operation_duration.isnot(None)
    ).scalar() or 0

    # Token usage
    token_stats = db.query(
        func.sum(AIGovernanceLog.usage_input_tokens).label('total_input'),
        func.sum(AIGovernanceLog.usage_output_tokens).label('total_output'),
        func.sum(AIGovernanceLog.usage_total_tokens).label('total')
    ).filter(
        AIGovernanceLog.timestamp >= start_time
    ).first()

    # Severity distribution
    severity_query = db.query(
        Conversation.final_severity,
        func.count(Conversation.id).label('count')
    ).filter(
        Conversation.created_at >= start_time,
        Conversation.final_severity.isnot(None)
    ).group_by(Conversation.final_severity).all()

    severity_distribution = {sev: count for sev, count in severity_query}

    # PII detection count
    pii_detection_count = db.query(func.count(AIGovernanceLog.id)).filter(
        AIGovernanceLog.timestamp >= start_time,
        AIGovernanceLog.pii_detected == True
    ).scalar()

    # Guardrail trigger count
    guardrail_trigger_count = db.query(func.count(AIGovernanceLog.id)).filter(
        AIGovernanceLog.timestamp >= start_time,
        AIGovernanceLog.guardrail_triggered == True
    ).scalar()

    return MetricsResponse(
        total_interactions=total_interactions or 0,
        escalation_count=escalation_count or 0,
        escalation_rate=round(escalation_rate, 2),
        average_latency=round(avg_latency, 3),
        total_input_tokens=token_stats.total_input or 0,
        total_output_tokens=token_stats.total_output or 0,
        total_tokens=token_stats.total or 0,
        severity_distribution=severity_distribution,
        pii_detection_count=pii_detection_count or 0,
        guardrail_trigger_count=guardrail_trigger_count or 0,
        time_period_start=start_time,
        time_period_end=end_time
    )

@router.get("/logs/export")
async def export_logs(
    format: str = Query("json", regex="^(json|csv)$"),
    log_type: str = Query("governance", regex="^(governance|escalations|audit)$"),
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db)
):
    """Export logs in various formats"""

    # Select appropriate table
    if log_type == "governance":
        query = db.query(AIGovernanceLog)
        if start_date:
            query = query.filter(AIGovernanceLog.timestamp >= start_date)
        if end_date:
            query = query.filter(AIGovernanceLog.timestamp <= end_date)
        logs = query.all()

        data = [
            {
                "timestamp": log.timestamp.isoformat(),
                "session_id": log.session_id,
                "request_id": log.request_id,
                "operation_name": log.operation_name,
                "usage_total_tokens": log.usage_total_tokens,
                "pii_detected": log.pii_detected,
                "safety_violated": log.safety_violated,
                "guardrail_triggered": log.guardrail_triggered
            }
            for log in logs
        ]

    elif log_type == "escalations":
        query = db.query(EscalationQueue)
        if start_date:
            query = query.filter(EscalationQueue.timestamp >= start_date)
        if end_date:
            query = query.filter(EscalationQueue.timestamp <= end_date)
        escalations = query.all()

        data = [
            {
                "timestamp": esc.timestamp.isoformat(),
                "escalation_id": esc.escalation_id,
                "session_id": esc.session_id,
                "severity": esc.severity,
                "reason": esc.reason,
                "review_status": esc.review_status
            }
            for esc in escalations
        ]

    else:  # audit
        query = db.query(AuditLog)
        if start_date:
            query = query.filter(AuditLog.timestamp >= start_date)
        if end_date:
            query = query.filter(AuditLog.timestamp <= end_date)
        audits = query.all()

        data = [
            {
                "timestamp": audit.timestamp.isoformat(),
                "session_id": audit.session_id,
                "action": audit.action,
                "actor": audit.actor
            }
            for audit in audits
        ]

    if format == "json":
        return {"data": data, "count": len(data)}
    else:
        # CSV format
        import csv
        import io
        from fastapi.responses import StreamingResponse

        output = io.StringIO()
        if data:
            writer = csv.DictWriter(output, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={log_type}_export.csv"}
        )

@router.get("/governance/session/{session_id}")
async def get_session_governance(
    session_id: str,
    db: Session = Depends(get_db)
):
    """Get complete governance data for a session"""

    # Get conversation
    conversation = db.query(Conversation).filter(
        Conversation.session_id == session_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get governance logs
    gov_logs = db.query(AIGovernanceLog).filter(
        AIGovernanceLog.session_id == session_id
    ).order_by(AIGovernanceLog.timestamp).all()

    # Get escalations
    escalations = db.query(EscalationQueue).filter(
        EscalationQueue.session_id == session_id
    ).all()

    # Get audit logs
    audits = db.query(AuditLog).filter(
        AuditLog.session_id == session_id
    ).order_by(AuditLog.timestamp).all()

    return {
        "session_id": session_id,
        "created_at": conversation.created_at,
        "escalated": conversation.escalated,
        "final_severity": conversation.final_severity,
        "messages": conversation.messages,
        "governance_logs": [
            {
                "timestamp": log.timestamp.isoformat(),
                "request_id": log.request_id,
                "operation_name": log.operation_name,
                "usage_total_tokens": log.usage_total_tokens,
                "pii_detected": log.pii_detected,
                "pii_types": log.pii_types,
                "safety_violated": log.safety_violated,
                "safety_categories": log.safety_categories,
                "guardrail_triggered": log.guardrail_triggered,
                "guardrail_ids": log.guardrail_ids
            }
            for log in gov_logs
        ],
        "escalations": [
            {
                "escalation_id": esc.escalation_id,
                "timestamp": esc.timestamp.isoformat(),
                "reason": esc.reason,
                "severity": esc.severity,
                "symptoms": esc.symptoms,
                "review_status": esc.review_status
            }
            for esc in escalations
        ],
        "audit_logs": [
            {
                "timestamp": audit.timestamp.isoformat(),
                "action": audit.action,
                "actor": audit.actor,
                "details": audit.details
            }
            for audit in audits
        ]
    }

@router.put("/escalations/{escalation_id}/review")
async def update_escalation_review(
    escalation_id: str,
    reviewer_id: str,
    review_notes: str,
    new_status: str = Query(..., regex="^(reviewed|resolved)$"),
    db: Session = Depends(get_db)
):
    """Update escalation review status"""

    escalation = db.query(EscalationQueue).filter(
        EscalationQueue.escalation_id == escalation_id
    ).first()

    if not escalation:
        raise HTTPException(status_code=404, detail="Escalation not found")

    escalation.review_status = new_status
    escalation.reviewer_id = reviewer_id
    escalation.review_notes = review_notes
    escalation.review_timestamp = datetime.utcnow()

    db.commit()

    return {
        "escalation_id": escalation_id,
        "review_status": new_status,
        "reviewer_id": reviewer_id,
        "review_timestamp": escalation.review_timestamp
    }
