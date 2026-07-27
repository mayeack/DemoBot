"""Agentic tool-guard endpoints (the OpenClaw before_tool_call seat).

The OpenClaw gateway's ``demobot-toolguard`` plugin POSTs every proposed agent
tool call to ``/api/toolguard/inspect`` *before* the tool runs. This is where
DemoBot extends its governance from "what the model says" to "what the agent
does":

  1. ``tool_policy.evaluate`` — fast, local, deterministic verdict (workspace
     containment, egress allowlist, sensitive-tool classification).
  2. Cisco AI Defense — sensitive/suspicious calls are rendered to text and
     submitted to the Inspection API, so PHI/PII/harm in the *arguments* is
     caught by the same guardrail the chat pipeline uses.
  3. ``governance_logger.log_tool_call`` — the decision is logged (file /
     SQLite / Splunk HEC) and emitted as an ``execute_tool`` GenAI span, so the
     tool action shows up in the governance UI and Splunk AI Agent Monitoring.

Enforcement policy:
  * ``decision`` is always computed honestly. Whether a "block" verdict is
    *enforced* (``block: true`` returned to the plugin) is gated by
    ``settings.tool_guard_enabled`` — so the unguarded control run produces the
    same telemetry while letting the call through. That contrast is the demo.
  * On an AI Defense error we honor the existing ``ai_defense_fail_open``
    policy — deliberately reusing the chat pipeline's fail flag, no second knob.

Concurrency: ``ai_defense_client`` is synchronous (blocking ``httpx``). It is
invoked via ``asyncio.to_thread`` so it never blocks the uvicorn event loop and
serializes with chat traffic. Benign (non-sensitive) calls short-circuit before
any network hop. Gated by the access-key middleware like every other route.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.config import settings
from backend.services import tool_policy
from backend.logging.governance_logger import governance_logger
from backend.telemetry import otel

router = APIRouter(prefix="/api/toolguard", tags=["toolguard"])
logger = logging.getLogger(__name__)


class ToolInspectRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=200)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = Field(default=None, max_length=200)
    tool_call_id: Optional[str] = Field(default=None, max_length=200)
    agent_surface: str = Field(default="openclaw", max_length=60)


class ToolInspectResponse(BaseModel):
    # block == enforce. decision is the honest verdict regardless of enforcement.
    block: bool
    decision: str            # "allow" | "block"
    reason: Optional[str] = None
    rule_names: list = Field(default_factory=list)
    ai_defense_event_id: Optional[str] = None
    enforced: bool           # echoes settings.tool_guard_enabled
    request_id: str


async def _inspect_ai_defense(rendered: str, enduser_id: Optional[str]):
    """Run the synchronous AI Defense prompt inspection off the event loop.

    Returns the InspectionResult, or None when AI Defense is not configured /
    disabled for the guard.
    """
    if not settings.tool_guard_ai_defense:
        return None
    try:
        from backend.services.ai_defense import ai_defense_client

        if not ai_defense_client.is_configured:
            return None
        return await asyncio.to_thread(
            ai_defense_client.inspect_prompt,
            rendered,
            enduser_id=enduser_id,
            src_app="demobot-openclaw",
        )
    except Exception:  # noqa: BLE001 - never let inspection break the guard
        logger.exception("tool-guard AI Defense inspection failed")
        return None


@router.post("/inspect", response_model=ToolInspectResponse)
async def inspect_tool_call(body: ToolInspectRequest) -> ToolInspectResponse:
    """Inspect a proposed agent tool call and return an allow/block decision."""
    request_id = str(uuid.uuid4())
    session_id = body.session_id or f"openclaw-{body.agent_surface}"

    verdict = tool_policy.evaluate(body.tool_name, body.arguments)
    reasons = list(verdict.reasons)
    rule_names = list(verdict.rule_names)
    ai_event_id: Optional[str] = None

    # Escalate sensitive or already-suspicious calls to AI Defense. Benign
    # reads short-circuit locally (no network hop, no added latency).
    if verdict.sensitive or verdict.should_block:
        inspection = await _inspect_ai_defense(verdict.rendered, session_id)
        if inspection is not None:
            ai_event_id = inspection.event_id
            if inspection.should_block:
                verdict.should_block = True
                if inspection.rule_names:
                    rule_names.extend(inspection.rule_names)
                    reasons.append(
                        "AI Defense: " + ", ".join(inspection.rule_names)
                    )
                elif inspection.errored:
                    # Honor the shared fail policy (fail-closed by default).
                    reasons.append(
                        "AI Defense inspection errored; "
                        + ("failing open" if settings.ai_defense_fail_open else "failing closed")
                    )
                else:
                    reasons.append("AI Defense flagged the tool call")

    decision = "block" if verdict.should_block else "allow"
    enforced = bool(settings.tool_guard_enabled)
    reason_text = "; ".join(dict.fromkeys(reasons)) or None
    # De-duplicate rule names, preserving order.
    rule_names = list(dict.fromkeys(rule_names))

    # Emit the execute_tool span + governance event (both defensive no-ops when
    # their subsystems are unavailable).
    with otel.tool_span(
        body.tool_name,
        tool_call_id=body.tool_call_id,
        agent_surface=body.agent_surface,
    ) as span:
        otel.record_tool_result(
            span,
            decision=decision,
            denied_reason=reason_text,
            rule_names=rule_names or None,
            event_id=ai_event_id,
        )

    try:
        governance_logger.log_tool_call(
            session_id=session_id,
            request_id=request_id,
            tool_name=body.tool_name,
            tool_decision=decision,
            tool_call_id=body.tool_call_id,
            tool_arguments=verdict.rendered,
            tool_denied_reason=reason_text,
            agent_surface=body.agent_surface,
            guardrail_ids=(["openclaw_tool_guard"] if decision == "block" else None),
            safety_categories=rule_names or None,
        )
    except Exception:  # noqa: BLE001 - logging must never break the decision
        logger.exception("tool-guard governance logging failed")

    return ToolInspectResponse(
        block=(decision == "block" and enforced),
        decision=decision,
        reason=reason_text,
        rule_names=rule_names,
        ai_defense_event_id=ai_event_id,
        enforced=enforced,
        request_id=request_id,
    )


@router.get("/policy")
async def get_policy() -> Dict[str, Any]:
    """Report the guard's live configuration (for the verify script + demo UI)."""
    return {
        "enabled": settings.tool_guard_enabled,
        "ai_defense": settings.tool_guard_ai_defense,
        "fail_open": settings.ai_defense_fail_open,
        "sensitive_tools": sorted(settings.tool_guard_sensitive_tools_set),
        "egress_allow_hosts": sorted(settings.tool_guard_egress_hosts_set),
        "workspace_roots": settings.tool_guard_workspace_roots_list,
        "max_arg_chars": settings.tool_guard_max_arg_chars,
    }


class DecoySinkPayload(BaseModel):
    model_config = {"extra": "allow"}


@router.post("/decoy-sink")
async def decoy_sink(payload: DecoySinkPayload) -> Dict[str, Any]:
    """Loopback exfiltration sink for the attack scenario.

    The planted document tells the agent to POST stolen data here. It is a
    dead-end on localhost: it records receipt (size only, never the content) so
    the *unguarded* control run demonstrably "completes" the exfiltration
    against a target that can't leak, while the guarded run never reaches it.
    """
    received = payload.model_dump()
    size = len(str(received))
    logger.warning(
        "decoy-sink received %d bytes of would-be-exfiltrated data (contained on loopback)",
        size,
    )
    return {"status": "received", "bytes": size, "note": "loopback decoy — nothing left the host"}
