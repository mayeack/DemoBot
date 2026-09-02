"""Agentic tool-guard endpoints (the OpenClaw before_tool_call seat).

The OpenClaw gateway's ``demobot-toolguard`` plugin POSTs every proposed agent
tool call to ``/api/toolguard/inspect`` *before* the tool runs. This is where
DemoBot extends its governance from "what the model says" to "what the agent
does":

  1. ``tool_policy.evaluate`` — fast, local, deterministic verdict (workspace
     containment, egress allowlist, sensitive-tool classification).
  2. ``nemoclaw_guard.evaluate`` — the NemoClaw Guardrails policy layer (the
     drawer toggle): an OpenShell-shaped sandbox policy over network egress,
     filesystem scopes, process rules and local-only inference, plus an
     optional NeMo rail over the rendered call.
  3. Cisco AI Defense — sensitive/suspicious calls are rendered to text and
     submitted to the Inspection API, so PHI/PII/harm in the *arguments* is
     caught by the same guardrail the chat pipeline uses.
  4. ``governance_logger.log_tool_call`` — the decision is logged (file /
     SQLite / Splunk HEC) and emitted as an ``execute_tool`` GenAI span, so the
     tool action shows up in the governance UI and Splunk AI Agent Monitoring.

Enforcement policy:
  * ``decision`` is always computed honestly. Whether a legacy (policy / AI
    Defense) "block" verdict is *enforced* is gated by
    ``settings.tool_guard_enabled`` — the unguarded control run produces the
    same telemetry while letting the call through. That contrast is the demo.
  * The NemoClaw toggle IS its own enforcement switch: when it is ON, a NemoClaw
    policy block denies the call regardless of ``tool_guard_enabled`` (the
    sandbox would have denied it too). ``enforced_by`` says which layer did.
  * On an AI Defense error we honor the existing ``ai_defense_fail_open``
    policy — deliberately reusing the chat pipeline's fail flag, no second knob.

The NemoClaw RUNTIME (run-nemoclaw.sh) reports the sandbox's own denials here
too: ``/api/toolguard/nemoclaw/events`` (OCSF JSONL from OpenShell) and
``/api/toolguard/observe`` (the plugin's after_tool_call hook), so a real
OpenShell denial lands in governance/telemetry with the same attribution.

Concurrency: ``ai_defense_client`` is synchronous (blocking ``httpx``). It is
invoked via ``asyncio.to_thread`` so it never blocks the uvicorn event loop and
serializes with chat traffic. Benign (non-sensitive) calls short-circuit before
any network hop. Gated by the access-key middleware like every other route.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.config import settings
from backend.services import nemoclaw_guard, tool_policy
from backend.logging.governance_logger import governance_logger
from backend.telemetry import otel

router = APIRouter(prefix="/api/toolguard", tags=["toolguard"])
logger = logging.getLogger(__name__)

NEMOCLAW_GUARDRAIL_ID = "nemoclaw_guardrails"


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
    enforced: bool           # echoes settings.tool_guard_enabled (legacy layers)
    enforced_by: list = Field(default_factory=list)  # "tool_guard" | "nemoclaw"
    request_id: str


async def _inspect_ai_defense(rendered: str, enduser_id: Optional[str]):
    """Run the synchronous AI Defense prompt inspection off the event loop.

    Returns the InspectionResult, or None when AI Defense is not configured /
    disabled for the guard.
    """
    if not settings.tool_guard_ai_defense:
        return None
    from backend.services.ai_defense import InspectionResult, ai_defense_client

    try:
        if not ai_defense_client.is_configured:
            return None
        return await asyncio.to_thread(
            ai_defense_client.inspect_prompt,
            rendered,
            enduser_id=enduser_id,
            src_app="demobot-openclaw",
        )
    except Exception as exc:  # noqa: BLE001 - never let inspection break the guard
        logger.exception("tool-guard AI Defense inspection failed")
        # Return an ERRORED result, not None. None means "AI Defense is not in
        # play" and the caller proceeds to allow — so a raising inspection used
        # to allow the call even with ai_defense_fail_open=False. An errored
        # result routes through InspectionResult.should_block, which honors the
        # configured fail policy (fail-closed by default).
        return InspectionResult(errored=True, error_message=str(exc))


def compose_decision(
    *,
    legacy_block: bool,
    nemoclaw_block: bool,
    tool_guard_enabled: bool,
    nemoclaw_enabled: bool,
) -> Dict[str, Any]:
    """The one place the enforcement rules live (pure, unit-tested).

    ``legacy_block`` = tool_policy and/or AI Defense said block (enforced only
    when the tool guard is on); ``nemoclaw_block`` = the NemoClaw policy layer
    said block (enforced whenever the NemoClaw toggle is on).
    """
    enforced_by: List[str] = []
    if legacy_block and tool_guard_enabled:
        enforced_by.append("tool_guard")
    if nemoclaw_block and nemoclaw_enabled:
        enforced_by.append("nemoclaw")
    decision = "block" if (legacy_block or nemoclaw_block) else "allow"
    guardrail_ids: List[str] = []
    if decision == "block":
        if legacy_block:
            guardrail_ids.append("openclaw_tool_guard")
        if nemoclaw_block:
            guardrail_ids.append(NEMOCLAW_GUARDRAIL_ID)
    return {
        "decision": decision,
        "block": bool(enforced_by),
        "enforced_by": enforced_by,
        "guardrail_ids": guardrail_ids or None,
    }


@router.post("/inspect", response_model=ToolInspectResponse)
async def inspect_tool_call(body: ToolInspectRequest) -> ToolInspectResponse:
    """Inspect a proposed agent tool call and return an allow/block decision."""
    request_id = str(uuid.uuid4())
    session_id = body.session_id or f"openclaw-{body.agent_surface}"

    verdict = tool_policy.evaluate(body.tool_name, body.arguments)
    reasons = list(verdict.reasons)
    rule_names = list(verdict.rule_names)
    ai_event_id: Optional[str] = None

    # NemoClaw policy layer (evaluated whenever the toggle is on; its NeMo rail
    # is an LLM call, so it runs off the event loop like AI Defense).
    nemoclaw_block = False
    if nemoclaw_guard.is_enabled():
        nc = await asyncio.to_thread(
            nemoclaw_guard.evaluate, body.tool_name, body.arguments, verdict.rendered,
            sensitive=verdict.sensitive,
        )
        if nc.should_block:
            nemoclaw_block = True
            rule_names.extend(nc.rule_names)
            reasons.extend(nc.reasons)

    # Escalate sensitive or already-suspicious calls to AI Defense. Benign
    # reads short-circuit locally (no network hop, no added latency).
    if verdict.sensitive or verdict.should_block or nemoclaw_block:
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

    composed = compose_decision(
        legacy_block=verdict.should_block,
        nemoclaw_block=nemoclaw_block,
        tool_guard_enabled=bool(settings.tool_guard_enabled),
        nemoclaw_enabled=nemoclaw_guard.is_enabled(),
    )
    decision = composed["decision"]
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
            guardrail_ids=composed["guardrail_ids"],
            safety_categories=rule_names or None,
        )
    except Exception:  # noqa: BLE001 - logging must never break the decision
        logger.exception("tool-guard governance logging failed")

    return ToolInspectResponse(
        block=composed["block"],
        decision=decision,
        reason=reason_text,
        rule_names=rule_names,
        ai_defense_event_id=ai_event_id,
        enforced=bool(settings.tool_guard_enabled),
        enforced_by=composed["enforced_by"],
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
        "nemoclaw": _nemoclaw_status(),
    }


# ---------------------------------------------------------------------------
# NemoClaw Guardrails: the drawer toggle + the runtime's denial feed
# ---------------------------------------------------------------------------
class NemoClawToggle(BaseModel):
    enabled: bool


def _nemoclaw_status() -> Dict[str, Any]:
    from backend import host_capabilities

    caps = host_capabilities.current()
    return {
        "enabled": nemoclaw_guard.is_enabled(),
        "use_nemo_rails": bool(settings.nemoclaw_use_nemo_rails),
        "policy": nemoclaw_guard.policy_summary(),
        "runtime": nemoclaw_guard.runtime_status(),
        # Can the real NemoClaw runtime run on this host? (package I gating)
        "runtime_supported": (caps.get("gated", {}).get("nemoclaw_runtime") or {}),
    }


@router.get("/nemoclaw")
async def get_nemoclaw() -> Dict[str, Any]:
    return _nemoclaw_status()


@router.put("/nemoclaw")
async def set_nemoclaw(body: NemoClawToggle) -> Dict[str, Any]:
    from backend import settings_store

    settings_store.set_nemoclaw_guardrails(body.enabled)
    logger.info("NemoClaw Guardrails %s", "enabled" if body.enabled else "disabled")
    return _nemoclaw_status()


def _log_runtime_denial(
    *, tool_name: str, reason: str, rule_name: str, session_id: Optional[str],
    tool_call_id: Optional[str], arguments: Optional[str], source: str,
) -> str:
    """One NemoClaw RUNTIME denial -> governance event + execute_tool span, with
    the same attribution the policy layer uses."""
    request_id = str(uuid.uuid4())
    nemoclaw_guard.record_runtime_event()
    with otel.tool_span(tool_name, tool_call_id=tool_call_id, agent_surface="nemoclaw") as span:
        otel.record_tool_result(span, decision="block", denied_reason=reason,
                                rule_names=[rule_name], event_id=None)
    try:
        governance_logger.log_tool_call(
            session_id=session_id or "nemoclaw-sandbox",
            request_id=request_id,
            tool_name=tool_name,
            tool_decision="block",
            tool_call_id=tool_call_id,
            tool_arguments=arguments,
            tool_denied_reason=f"{reason} [source: {source}]",
            agent_surface="nemoclaw",
            guardrail_ids=[NEMOCLAW_GUARDRAIL_ID],
            safety_categories=[rule_name],
        )
    except Exception:  # noqa: BLE001
        logger.exception("NemoClaw runtime denial logging failed")
    return request_id


class NemoClawEvents(BaseModel):
    """OCSF JSONL records from the sandbox's /var/log/openshell-ocsf.*.log."""

    records: List[Dict[str, Any]] = Field(default_factory=list)
    session_id: Optional[str] = Field(default=None, max_length=200)


def ocsf_denial(record: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Map an OCSF record to a denial, or None when it is not a Denied event.

    OpenShell writes class 4001 (Network Activity) / 4002 (HTTP Activity) with
    ``action: Denied`` / ``disposition: Blocked``, the destination under
    ``dst_endpoint``, the binary under ``actor.process.name`` and the matched
    rule under ``firewall_rule.name``.
    """
    action = str(record.get("action") or record.get("action_id") or "").lower()
    disposition = str(record.get("disposition") or "").lower()
    if "denied" not in action and "blocked" not in disposition and "deny" not in action:
        return None
    dst = record.get("dst_endpoint") or {}
    host = dst.get("domain") or dst.get("hostname") or dst.get("ip") or "?"
    port = dst.get("port")
    proc = (record.get("actor") or {}).get("process") or {}
    binary = proc.get("name") or proc.get("cmd_line") or "agent"
    rule = (record.get("firewall_rule") or {}).get("name") or ""
    detail = record.get("status_detail") or record.get("message") or "denied by sandbox policy"
    layer = "network egress"
    class_uid = record.get("class_uid")
    if class_uid == 1007:
        layer = "process"
    elif class_uid == 6003:
        layer = "inference"
    target = f"{host}:{port}" if port else str(host)
    http = record.get("http_request") or {}
    if http.get("url"):
        target = str(http["url"].get("url_string") or http["url"].get("path") or target)
    return {
        "tool_name": str(binary).rsplit("/", 1)[-1],
        "rule_name": f"NemoClaw: {layer}" + (f" ({rule})" if rule else ""),
        "reason": f"OpenShell denied {target}: {detail}",
        "arguments": f"{binary} -> {target}",
    }


@router.post("/nemoclaw/events")
async def nemoclaw_events(body: NemoClawEvents) -> Dict[str, Any]:
    """Ingest OCSF records from the NemoClaw runtime's forwarder; every Denied
    record becomes a nemoclaw_guardrails governance event + execute_tool span."""
    logged: List[str] = []
    for rec in body.records:
        denial = ocsf_denial(rec) if isinstance(rec, dict) else None
        if denial is None:
            continue
        logged.append(_log_runtime_denial(
            tool_name=denial["tool_name"], reason=denial["reason"], rule_name=denial["rule_name"],
            session_id=body.session_id, tool_call_id=None, arguments=denial["arguments"],
            source="openshell-ocsf",
        ))
    return {"received": len(body.records), "denials_logged": len(logged), "request_ids": logged}


class ToolObserveRequest(BaseModel):
    """after_tool_call observation from the gateway plugin (result or error)."""

    tool_name: str = Field(..., min_length=1, max_length=200)
    error: Optional[str] = Field(default=None, max_length=4000)
    result_excerpt: Optional[str] = Field(default=None, max_length=4000)
    session_id: Optional[str] = Field(default=None, max_length=200)
    tool_call_id: Optional[str] = Field(default=None, max_length=200)
    agent_surface: str = Field(default="openclaw", max_length=60)
    duration_ms: Optional[float] = None


def is_policy_denied(text: Optional[str]) -> bool:
    t = (text or "").lower()
    return "policy_denied" in t or "not permitted by policy" in t or "denied by policy" in t


@router.post("/observe")
async def observe_tool_result(body: ToolObserveRequest) -> Dict[str, Any]:
    """A tool that ran but was refused by the sandbox (OpenShell's
    ``{"error":"policy_denied", ...}``) is attributed to NemoClaw immediately,
    before the OCSF tail catches up. Anything else is acknowledged only."""
    text = body.error or body.result_excerpt
    if not is_policy_denied(text):
        return {"attributed": False}
    request_id = _log_runtime_denial(
        tool_name=body.tool_name, reason=(text or "")[:500], rule_name="NemoClaw: network egress",
        session_id=body.session_id, tool_call_id=body.tool_call_id, arguments=None,
        source=f"after_tool_call/{body.agent_surface}",
    )
    return {"attributed": True, "request_id": request_id}


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
