"""NemoClaw Guardrails — the policy layer (the "NemoClaw Guardrails" toggle).

NVIDIA NemoClaw runs OpenClaw inside an OpenShell sandbox whose declarative
policy governs the network (deny-by-default egress with host/port/method/path
rules per binary), the filesystem (read-only vs read-write scopes), the process
layer (what may run) and inference routing (the privacy router keeps model calls
on local, managed endpoints). This module evaluates DemoBot's copy of that
policy (``guardrails/nemoclaw/policy.yaml``) on every agent tool call the
gateway submits to ``/api/toolguard/inspect`` — so the demo can show a
NemoClaw-shaped block on any host, and attribute it (``guardrail_ids``
``nemoclaw_guardrails``, rule names ``NemoClaw: …``) exactly like the real
sandbox's OCSF denials that the NemoClaw runtime forwards (see
``/api/toolguard/nemoclaw/events``).

Optionally, a NeMo Guardrails input rail also reviews the rendered tool call
(``nemoclaw_use_nemo_rails``): NemoClaw pairs OpenShell policy with NeMo rails,
and DemoBot mirrors that pairing.

Like ``tool_policy``, this module only EVALUATES. Whether a NemoClaw block is
enforced is the router's decision, and the drawer toggle IS that switch.
"""
from __future__ import annotations

import fnmatch
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from backend.config import BASE_DIR, settings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\"'<>\\)\]]+", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:^|[\s\"'=(])((?:/|(?:\.\./)+)[^\s\"'<>|;)]*)")
_WRITE_TOOLS = {"write", "edit", "apply_patch", "create_file", "delete", "move", "rename", "mkdir"}
_EXEC_TOOLS = {"exec", "bash", "process", "shell", "run", "spawn"}
_SHELL_WRITE_RE = re.compile(r"(?:>>?|\btee\b|\bcp\b|\bmv\b|\brm\b|\bchmod\b|\bchown\b|\bdd\b)")

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass
class NemoClawVerdict:
    """Outcome of one policy evaluation (mirrors ToolPolicyVerdict / InspectionResult)."""

    should_block: bool = False
    rule_names: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    # Which checks ran, for the governance metadata / policy report.
    checks: Dict[str, str] = field(default_factory=dict)
    policy_version: Optional[int] = None


# --------------------------------------------------------------------------- policy
_policy_lock = threading.Lock()
_policy: Optional[Dict[str, Any]] = None
_policy_mtime: float = 0.0
_policy_error: Optional[str] = None


def policy_path() -> Path:
    p = Path(getattr(settings, "nemoclaw_policy_path", "") or "guardrails/nemoclaw/policy.yaml")
    return p if p.is_absolute() else Path(BASE_DIR) / p


def load_policy(force: bool = False) -> Dict[str, Any]:
    """Parse the policy file (cached by mtime). Raises ValueError when missing/invalid."""
    global _policy, _policy_mtime, _policy_error
    path = policy_path()
    with _policy_lock:
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            _policy = None
            _policy_error = f"NemoClaw policy not found at {path}: {exc}"
            raise ValueError(_policy_error) from exc
        if _policy is not None and not force and mtime == _policy_mtime:
            return _policy
        import yaml

        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:  # noqa: BLE001
            _policy = None
            _policy_error = f"NemoClaw policy {path} is not valid YAML: {exc}"
            raise ValueError(_policy_error) from exc
        if not isinstance(data, dict) or "network_policies" not in data:
            _policy = None
            _policy_error = f"NemoClaw policy {path} has no network_policies section"
            raise ValueError(_policy_error)
        _policy = data
        _policy_mtime = mtime
        _policy_error = None
        return data


def policy_summary() -> Dict[str, Any]:
    """For GET /api/toolguard/policy and the drawer card."""
    try:
        p = load_policy()
    except ValueError as exc:
        return {"path": str(policy_path()), "loaded": False, "error": str(exc)}
    nets = p.get("network_policies") or []
    return {
        "path": str(policy_path()),
        "loaded": True,
        "version": p.get("version"),
        "network_policies": [n.get("name") for n in nets if isinstance(n, dict)],
        "endpoints": sum(len(n.get("endpoints") or []) for n in nets if isinstance(n, dict)),
        "read_write": list((p.get("filesystem_policy") or {}).get("read_write") or []),
        "deny_binaries": list((p.get("process") or {}).get("deny_binaries") or []),
        "inference_local_only": bool((p.get("inference") or {}).get("local_only", False)),
    }


def is_enabled() -> bool:
    return bool(getattr(settings, "nemoclaw_guardrails_enabled", False))


# --------------------------------------------------------------------------- runtime feed
# Timestamps of NemoClaw RUNTIME denials forwarded by the OCSF forwarder /
# after_tool_call hook (D2). The drawer pill reads RUNTIME when recent.
_runtime_events: List[float] = []
_runtime_lock = threading.Lock()


def record_runtime_event() -> None:
    with _runtime_lock:
        now = time.time()
        _runtime_events.append(now)
        del _runtime_events[:-500]


def runtime_status(window_s: float = 300.0) -> Dict[str, Any]:
    with _runtime_lock:
        now = time.time()
        recent = [t for t in _runtime_events if now - t <= window_s]
        last = max(_runtime_events) if _runtime_events else None
    return {"events_recent": len(recent), "last_event_at": last, "window_s": window_s}


# --------------------------------------------------------------------------- helpers
def _host_matches(rule_host: str, host: str) -> bool:
    rule_host = (rule_host or "").lower()
    if rule_host.startswith("*."):
        return host == rule_host[2:] or host.endswith(rule_host[1:])
    return host == rule_host


def _path_matches(patterns: List[str], path: str) -> bool:
    if not patterns:
        return True
    for pat in patterns:
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if path == prefix or path.startswith(prefix + "/") or (prefix == "" and True):
                return True
        if fnmatch.fnmatch(path, pat):
            return True
    return False


def _split_url(url: str):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    return host, port, path


def _extract_paths(text: str) -> List[str]:
    return [m.group(1) for m in _PATH_RE.finditer(text) if len(m.group(1)) > 1]


def _normalize(path: str) -> str:
    segments: List[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if segments:
                segments.pop()
            continue
        segments.append(part)
    return "/" + "/".join(segments)


def _under(path: str, roots: List[str]) -> bool:
    norm = _normalize(path)
    for root in roots:
        root = (root or "").rstrip("/") or "/"
        if norm == root or norm.startswith(root + "/"):
            return True
    return False


# --------------------------------------------------------------------------- evaluation
def evaluate(tool_name: str, arguments: Dict[str, Any], rendered: str, *,
             sensitive: bool = False, use_nemo_rails: Optional[bool] = None) -> NemoClawVerdict:
    """Evaluate one proposed tool call against the NemoClaw policy. Never raises."""
    verdict = NemoClawVerdict()
    name = (tool_name or "").strip().lower()
    try:
        policy = load_policy()
    except ValueError as exc:
        # A missing/broken policy is a loud misconfiguration: block and say why
        # (a sandbox with no policy is exactly what NemoClaw refuses to run).
        verdict.should_block = True
        verdict.rule_names.append("NemoClaw: policy")
        verdict.reasons.append(str(exc))
        return verdict
    verdict.policy_version = policy.get("version")

    try:
        import json

        try:
            full = f"{tool_name}({json.dumps(arguments, sort_keys=True, default=str)})"
        except (TypeError, ValueError):
            full = f"{tool_name}({arguments})"

        # ---- network + inference -------------------------------------------
        nets = [n for n in (policy.get("network_policies") or []) if isinstance(n, dict)]
        inference = policy.get("inference") or {}
        api_paths = [p.lower() for p in (inference.get("api_paths") or [])]
        urls = _URL_RE.findall(full)
        verdict.checks["network"] = f"{len(urls)} url(s)"
        for url in urls:
            host, port, path = _split_url(url)
            if not host:
                continue
            allowed = any(
                _host_matches(str(ep.get("host", "")), host)
                and int(ep.get("port", port)) == port
                and _path_matches(list(ep.get("paths") or []), path)
                for n in nets for ep in (n.get("endpoints") or []) if isinstance(ep, dict)
            )
            is_inference = any(path.lower().startswith(p) for p in api_paths)
            if inference.get("local_only") and is_inference and host not in _LOOPBACK \
                    and not host.startswith("127.") and not allowed:
                verdict.should_block = True
                verdict.rule_names.append("NemoClaw: inference")
                verdict.reasons.append(
                    f"privacy router: model call to {host}:{port}{path} is not a local, managed endpoint"
                )
                continue
            if not allowed:
                verdict.should_block = True
                verdict.rule_names.append("NemoClaw: network egress")
                verdict.reasons.append(f"{host}:{port}{path} not permitted by policy (no matching endpoint)")

        # ---- filesystem -------------------------------------------------------
        fs = policy.get("filesystem_policy") or {}
        rw = list(fs.get("read_write") or [])
        ro = list(fs.get("read_only") or []) + rw
        paths = _extract_paths(full)
        verdict.checks["filesystem"] = f"{len(paths)} path(s)"
        writes = name in _WRITE_TOOLS or (name in _EXEC_TOOLS and bool(_SHELL_WRITE_RE.search(full)))
        for p in paths:
            if writes and not _under(p, rw):
                verdict.should_block = True
                verdict.rule_names.append("NemoClaw: filesystem")
                verdict.reasons.append(f"write to {p} is outside the read_write scopes {rw}")
            elif not writes and rw and not _under(p, ro):
                verdict.should_block = True
                verdict.rule_names.append("NemoClaw: filesystem")
                verdict.reasons.append(f"read of {p} is outside the allowed scopes")

        # ---- process ----------------------------------------------------------
        deny = [str(b).lower() for b in ((policy.get("process") or {}).get("deny_binaries") or [])]
        if name in _EXEC_TOOLS and deny:
            tokens = re.findall(r"[A-Za-z0-9_./-]+", full.lower())
            hit = next((t for t in tokens if t.rsplit("/", 1)[-1] in deny), None)
            verdict.checks["process"] = "checked"
            if hit:
                verdict.should_block = True
                verdict.rule_names.append("NemoClaw: process")
                verdict.reasons.append(f"binary {hit.rsplit('/', 1)[-1]!r} is denied by the sandbox policy")

        # ---- NeMo rail on the rendered call (only for sensitive/suspicious calls) --
        want_rails = getattr(settings, "nemoclaw_use_nemo_rails", True) if use_nemo_rails is None else use_nemo_rails
        if want_rails and (sensitive or verdict.should_block):
            from backend.services.nemo_guardrails import nemo_guardrails_client

            if nemo_guardrails_client.is_configured:
                rail = nemo_guardrails_client.check_tool_call(rendered or full)
                verdict.checks["nemo_rail"] = "errored" if rail.errored else ("blocked" if rail.should_block else "passed")
                if rail.should_block:
                    verdict.should_block = True
                    names = ", ".join(rail.rule_names) or "input rails"
                    verdict.rule_names.append(f"NemoClaw: NeMo rail ({names})")
                    verdict.reasons.append(rail.reason or f"NeMo Guardrails flagged the tool call ({names})")
    except Exception:  # noqa: BLE001 - evaluation must never raise into the router
        logger.exception("NemoClaw policy evaluation failed for tool %r", tool_name)
        verdict.should_block = True
        verdict.rule_names.append("NemoClaw: policy")
        verdict.reasons.append("policy evaluation failed (fail-closed)")

    verdict.rule_names = list(dict.fromkeys(verdict.rule_names))
    return verdict
