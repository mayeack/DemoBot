"""Deterministic tool-call policy for the agentic (OpenClaw) surface.

First line of the tool guard: fast, local, explainable verdicts on a proposed
agent tool call *before* any network hop to Cisco AI Defense. Three checks:

  1. Sensitive-tool classification — is this tool capable of side effects
     (exec/write/egress)? Drives whether the call is escalated to AI Defense.
  2. Workspace containment — filesystem paths in the arguments must stay inside
     the configured workspace roots (the decoy workspace, container-side).
  3. Egress allowlist — URLs in the arguments may only target approved hosts.
     Empty allowlist = deny-by-default (every egress host is unapproved).

The verdict mirrors the shape of ``InspectionResult`` in
``backend/services/ai_defense.py`` (``.should_block`` / ``.rule_names``) so the
tool-guard router can treat both sources uniformly. Enforcement (whether a
"block" verdict actually denies the call) is the router's decision — this
module only *evaluates*, so the unguarded control run still produces honest
telemetry about what would have been blocked.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from backend.config import settings

logger = logging.getLogger(__name__)

# Matches http(s) URLs in rendered arguments; group 1 is host[:port].
_URL_RE = re.compile(r"https?://([^/\s\"'<>\\]+)", re.IGNORECASE)

# Absolute path or parent-directory traversal inside a string argument.
_PATH_RE = re.compile(r"(?:^|[\s\"'=(])((?:/|(?:\.\./)+)[^\s\"'<>|;)]*)")


@dataclass
class ToolPolicyVerdict:
    """Normalized outcome of a deterministic tool-policy evaluation."""

    should_block: bool = False
    sensitive: bool = False
    reasons: List[str] = field(default_factory=list)
    rule_names: List[str] = field(default_factory=list)
    # Compact rendered form of the call (truncated) for inspection + logging.
    rendered: str = ""


def _render_full(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Render a proposed tool call as text, with NO length bound.

    This is what the policy checks read. Never truncate before a check: see
    ``evaluate``.
    """
    try:
        args_text = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args_text = str(arguments)
    return f"Agent tool call: {tool_name}({args_text})"


def _truncate(text: str) -> str:
    """Bound text to ``settings.tool_guard_max_arg_chars`` for transport/storage."""
    limit = max(200, settings.tool_guard_max_arg_chars)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def render_tool_call(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Bounded rendered form of a tool call, for AI Defense and governance logs.

    Deterministic and length-capped, because it is submitted to the Inspection
    API and stored on every governance event.

    This is a *transport* form, not an analysis form — do not feed it to a
    policy check. ``evaluate`` reads the untruncated text instead.
    """
    return _truncate(_render_full(tool_name, arguments))


def _extract_hosts(text: str) -> List[str]:
    """Hosts (host[:port], lowercased, creds stripped) of every URL in text."""
    hosts = []
    for match in _URL_RE.finditer(text):
        host = match.group(1).split("@")[-1].lower()
        if host:
            hosts.append(host)
    return hosts


def _host_allowed(host: str, allowed: set) -> bool:
    """True when host (host[:port]) matches an allowlist entry with or without
    its port — so "localhost" approves "localhost:8001" and vice versa."""
    bare = host.rsplit(":", 1)[0]
    return host in allowed or bare in allowed


def _extract_paths(text: str) -> List[str]:
    return [m.group(1) for m in _PATH_RE.finditer(text) if len(m.group(1)) > 1]


def _outside_workspace(path: str, roots: List[str]) -> bool:
    """True when an absolute/traversal path escapes every configured root.

    Purely lexical (the app never touches the agent's filesystem — paths are
    container-side): normalize ".." segments, then prefix-match against roots.
    """
    segments: List[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if segments:
                segments.pop()
            continue
        segments.append(part)
    normalized = "/" + "/".join(segments)
    return not any(
        normalized == root or normalized.startswith(root + "/") for root in roots
    )


def evaluate(tool_name: str, arguments: Dict[str, Any]) -> ToolPolicyVerdict:
    """Deterministic verdict for a proposed agent tool call. Never raises."""
    # Checks read `full`; only `verdict.rendered` is bounded. Truncating first
    # was a bypass: json.dumps sorts by key, and the agent chooses the key
    # NAMES, so padding an early-sorting key past tool_guard_max_arg_chars
    # pushed a malicious path or URL out of the text both checks below read —
    # and out of the text submitted to AI Defense. The call then scored clean.
    full = _render_full(tool_name, arguments)
    verdict = ToolPolicyVerdict(rendered=_truncate(full))
    try:
        name = (tool_name or "").strip().lower()
        verdict.sensitive = name in settings.tool_guard_sensitive_tools_set

        # AI Defense only ever sees the bounded form, so a call whose arguments
        # did not fit must not also be able to skip inspection by classifying as
        # non-sensitive. Escalate it regardless of tool name.
        if len(full) > len(verdict.rendered):
            verdict.sensitive = True

        # Workspace containment: paths in the arguments must stay inside the
        # configured (container-side) workspace roots.
        roots = settings.tool_guard_workspace_roots_list
        if roots:
            escapes = [
                p for p in _extract_paths(full)
                if _outside_workspace(p, roots)
            ]
            if escapes:
                verdict.should_block = True
                verdict.reasons.append(
                    f"path outside agent workspace: {escapes[0]}"
                )
                verdict.rule_names.append("Workspace Containment")

        # Egress allowlist: any URL in the arguments must target an approved
        # host. An empty allowlist denies all egress by default.
        hosts = _extract_hosts(full)
        if hosts:
            allowed = settings.tool_guard_egress_hosts_set
            blocked_hosts = [h for h in hosts if not _host_allowed(h, allowed)]
            if blocked_hosts:
                verdict.should_block = True
                verdict.reasons.append(
                    f"unapproved egress host: {blocked_hosts[0]}"
                )
                verdict.rule_names.append("Egress Allowlist")
    except Exception:  # noqa: BLE001 - policy evaluation must never raise
        # Fail toward inspection, not away from it. This used to `pass`, leaving
        # should_block=False AND sensitive=False — and since the router escalates
        # to AI Defense only when one of those is set, a crash here silently
        # allowed ANY tool call, bypassing both guard layers. Marking the verdict
        # sensitive hands the call to AI Defense, which then applies the
        # configured fail policy.
        logger.exception("tool policy evaluation failed for tool %r", tool_name)
        verdict.sensitive = True
    return verdict
