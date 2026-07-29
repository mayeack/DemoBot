#!/usr/bin/env python3
"""Regression: Galileo Agent Control enforcement ("Agent Observability Controls").

Guards the contract of the toggle end to end without touching the network:

  1. no-op safety — no GALILEO_API_KEY (or master switch off) means the client is
     unconfigured and the graph node never calls out;
  2. verdict semantics — deny blocks, steer/observe do not, and an errored
     evaluation honors ``galileo_agent_control_fail_open``;
  3. the request payload actually submitted (post stage, llm step, input AND
     output) — a control that scores correctness cannot work without both sides;
  4. the node short-circuits the turn on deny with a governance-logged block, and
     stays transparent otherwise;
  5. the wiring: the flag reaches the graph, the node sits between compliance and
     response_defense, and the frontend toggle is present.

    venv/bin/python tests/test_agent_control.py    # exit 0 = pass
"""
import json
import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


class _FakeResponse:
    """Minimal httpx.Response stand-in for the happy path."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


os.environ.pop("GALILEO_API_KEY", None)

from backend.config import settings  # noqa: E402
from backend.services import agent_control as ac  # noqa: E402

# backend.config loads .env at import, which may repopulate GALILEO_API_KEY —
# drop it again so the no-op guarantee is tested against a truly absent key.
os.environ.pop("GALILEO_API_KEY", None)

# ---- 1. no-op safety guarantee -------------------------------------------
print("\n[1] no-op safety")
client = ac.AgentControlClient()
check("unconfigured when GALILEO_API_KEY unset", client.is_configured is False)

os.environ["GALILEO_API_KEY"] = "test-key-unused"
check("configured when GALILEO_API_KEY set", client.is_configured is True)

with mock.patch.object(settings, "galileo_agent_control_enabled", False):
    check(
        "master switch off wins over a present API key",
        ac.AgentControlClient().is_configured is False,
    )

try:
    with mock.patch.object(settings, "galileo_agent_control_enabled", False):
        ac.AgentControlClient().evaluate_response("q", "a")
    check("evaluate_response raises when unconfigured", False)
except ac.AgentControlError:
    check("evaluate_response raises when unconfigured", True)

# console API base is derived from the console URL, not hardcoded
with mock.patch.dict(
    os.environ, {"GALILEO_CONSOLE_URL": "https://console.multitenant.galileocloud.io"}
):
    check(
        "console API url derived from GALILEO_CONSOLE_URL",
        ac.AgentControlClient().console_api_url
        == "https://api.multitenant.galileocloud.io",
    )
with mock.patch.dict(os.environ, {"GALILEO_CONSOLE_URL": ""}):
    check(
        "console API url defaults to the hosted API",
        ac.AgentControlClient().console_api_url == "https://api.galileo.ai",
    )

# ---- 2. verdict semantics ------------------------------------------------
print("\n[2] verdict semantics")
check(
    "deny blocks",
    ac.ControlVerdict(is_safe=False, decisions=["deny"]).should_block is True,
)
check(
    "steer does not block (recorded, not enforced)",
    ac.ControlVerdict(is_safe=False, decisions=["steer"]).should_block is False,
)
check(
    "observe does not block",
    ac.ControlVerdict(is_safe=False, decisions=["observe"]).should_block is False,
)
check("clean verdict does not block", ac.ControlVerdict().should_block is False)
check(
    "deny alongside observe still blocks",
    ac.ControlVerdict(decisions=["observe", "deny"]).should_block is True,
)

errored = ac.ControlVerdict(errored=True, error_message="boom")
with mock.patch.object(settings, "galileo_agent_control_fail_open", True):
    check("errored + fail-open releases the response", errored.should_block is False)
with mock.patch.object(settings, "galileo_agent_control_fail_open", False):
    check("errored + fail-closed withholds the response", errored.should_block is True)

parsed = ac.AgentControlClient._parse_response(
    {
        "is_safe": False,
        "confidence": 0.91,
        "reason": "control matched",
        "matches": [
            {
                "control_name": "DemoBot-block-hallucinated-output",
                "action": "deny",
                "result": {"matched": True, "confidence": 0.91, "message": "score 0.2 < 0.5"},
            }
        ],
        "errors": [{"control_name": "some-broken-control"}],
    }
)
check("parses matched control name", parsed.matched_controls == ["DemoBot-block-hallucinated-output"])
check("parses decision", parsed.decisions == ["deny"])
check("parses evaluator message", parsed.messages == ["score 0.2 < 0.5"])
check("parses evaluator errors", parsed.evaluator_errors == ["some-broken-control"])
check("parsed deny blocks", parsed.should_block is True)
check(
    "a body without is_safe is treated as an error, not as safe",
    ac.AgentControlClient._parse_response({"confidence": 1.0}).errored is True,
)

# ---- 3. submitted payload ------------------------------------------------
print("\n[3] submitted evaluation payload")
posted = {}


def _fake_post(url, **kwargs):
    posted[url] = kwargs
    if url.endswith("/v2/login/api_key"):
        return _FakeResponse({"access_token": "header.eyJleHAiOjk5OTk5OTk5OTl9.sig"})
    if url.endswith("/runtime-token-exchange"):
        return _FakeResponse(
            {"token": "rt.eyJleHAiOjk5OTk5OTk5OTl9.sig", "expires_at": "2099-01-01T00:00:00Z"}
        )
    return _FakeResponse({"is_safe": True, "confidence": 0.0, "matches": []})


with mock.patch.object(ac.httpx, "post", side_effect=_fake_post):
    verdict = ac.AgentControlClient().evaluate_response(
        "How much Tylenol is safe?",
        "Up to 12,000 mg per day.",
        session_id="s-1",
        theme="medadvice",
        model="dolphin3:8b",
        enduser_id="user-7",
    )

eval_url = next((u for u in posted if u.endswith("/api/v1/evaluation")), None)
check("evaluation endpoint called", eval_url is not None)
body = posted.get(eval_url, {}).get("json", {}) if eval_url else {}
step = body.get("step", {})
check("agent_name is the configured agent", body.get("agent_name") == settings.galileo_agent_control_agent_name)
check("stage is post (the response is what gets judged)", body.get("stage") == "post")
check("step type is llm", step.get("type") == "llm")
check("step carries the user prompt as input", step.get("input") == "How much Tylenol is safe?")
check("step carries the generated answer as output", step.get("output") == "Up to 12,000 mg per day.")
check("context carries session/theme/model for triage",
      (step.get("context") or {}).get("session_id") == "s-1"
      and (step.get("context") or {}).get("theme") == "medadvice"
      and (step.get("context") or {}).get("model") == "dolphin3:8b")
check("clean verdict released", verdict.should_block is False)

auth = posted.get(eval_url, {}).get("headers", {}).get("Authorization", "")
check("evaluation presents the minted runtime token as Bearer", auth == "Bearer rt.eyJleHAiOjk5OTk5OTk5OTl9.sig")


# An unavailable runtime-token exchange must fall back to console-token auth
# (the SDK's "auto" mode) instead of failing the turn.
def _fake_post_no_runtime(url, **kwargs):
    if url.endswith("/v2/login/api_key"):
        return _FakeResponse({"access_token": "at.eyJleHAiOjk5OTk5OTk5OTl9.sig"})
    if url.endswith("/runtime-token-exchange"):
        raise ac.httpx.ConnectError("no runtime grant")
    posted["fallback"] = kwargs
    return _FakeResponse({"is_safe": True, "confidence": 0.0})


with mock.patch.object(ac.httpx, "post", side_effect=_fake_post_no_runtime):
    fallback_verdict = ac.AgentControlClient().evaluate_response("q", "a")
check("runtime-token exchange failure does not raise", fallback_verdict.errored is False)
check(
    "falls back to console-token auth when no runtime token can be minted",
    posted.get("fallback", {}).get("headers", {}).get("Authorization")
    == "Bearer at.eyJleHAiOjk5OTk5OTk5OTl9.sig",
)

# A dead server must not raise into the turn — it must produce an errored verdict.
with mock.patch.object(ac.httpx, "post", side_effect=ac.httpx.ConnectError("down")):
    dead = ac.AgentControlClient().evaluate_response("q", "a")
check("unreachable server yields an errored verdict, never an exception", dead.errored is True)

# ---- 4. graph node behavior ---------------------------------------------
print("\n[4] graph node")
from backend.agents.nodes import agent_control as node_mod  # noqa: E402

base_state = {
    "session_id": "s-1",
    "request_id": "r-1",
    "trace_id": "t-1",
    "start_time": 0.0,
    "user_message": "q",
    "final_message": "a",
    "messages": [{"role": "user", "content": "q"}],
    "llm_model": "dolphin3:8b",
    "llm_input_tokens": 11,
    "llm_output_tokens": 22,
}

check(
    "node is a no-op when the request did not opt in",
    node_mod.agent_control_node(dict(base_state)) == {},
)

with mock.patch.object(
    ac.AgentControlClient, "is_configured", new_callable=mock.PropertyMock
) as configured:
    configured.return_value = False
    check(
        "node is a no-op when the client is unconfigured, even if opted in",
        node_mod.agent_control_node(dict(base_state, agent_control_review=True)) == {},
    )

deny_verdict = ac.ControlVerdict(
    is_safe=False,
    confidence=0.9,
    decisions=["deny"],
    matched_controls=["DemoBot-block-hallucinated-output"],
    messages=["correctness 0.2 < 0.5"],
)
with mock.patch.object(
    ac.AgentControlClient, "is_configured", new_callable=mock.PropertyMock
) as configured, mock.patch.object(
    node_mod.agent_control_client, "evaluate_response", return_value=deny_verdict
), mock.patch.object(
    node_mod.content_engine, "_handle_agent_control_block"
) as handler:
    configured.return_value = True
    handler.return_value = {"message": "withheld", "policy_blocked": True}
    out = node_mod.agent_control_node(dict(base_state, agent_control_review=True))

check("deny short-circuits the turn", out.get("terminal") is True)
check("deny returns the block result", out.get("result", {}).get("policy_blocked") is True)
check("deny records the verdict on state", out.get("agent_control") is deny_verdict)
check("deny records stage timing", "agent_control_ms" in (out.get("stage_timings") or {}))
handler_kwargs = handler.call_args.kwargs
check(
    "block handler gets the real model + token spend (not zeros)",
    handler_kwargs.get("llm_model") == "dolphin3:8b"
    and handler_kwargs["usage_data"]["usage_total_tokens"] == 33,
)

allow_verdict = ac.ControlVerdict(is_safe=True, decisions=["observe"], matched_controls=["watch-me"])
with mock.patch.object(
    ac.AgentControlClient, "is_configured", new_callable=mock.PropertyMock
) as configured, mock.patch.object(
    node_mod.agent_control_client, "evaluate_response", return_value=allow_verdict
):
    configured.return_value = True
    allowed = node_mod.agent_control_node(dict(base_state, agent_control_review=True))
check("non-blocking verdict does not terminate the turn", allowed.get("terminal") is None)
check("non-blocking verdict still reaches governance via state", allowed.get("agent_control") is allow_verdict)

# ---- 5. wiring ----------------------------------------------------------
print("\n[5] wiring")
from backend.agents.state import build_initial_state  # noqa: E402
from backend.models.schemas import ChatRequest  # noqa: E402

check(
    "ChatRequest accepts agent_control_review",
    ChatRequest(session_id="s", message="m", agent_control_review=True).agent_control_review
    is True,
)
check(
    "flag defaults to None (off) on the request",
    ChatRequest(session_id="s", message="m").agent_control_review is None,
)
check(
    "flag reaches the graph state",
    build_initial_state(
        session_id="s", user_message="m", conversation_history=[], agent_control_review=True
    )["agent_control_review"]
    is True,
)

graph_src = (ROOT / "backend/agents/graph.py").read_text()
check("node registered on the theme subgraph", 'g.add_node("agent_control", agent_control_node)' in graph_src)
check("compliance routes into agent_control", 'g.add_edge("compliance", "agent_control")' in graph_src)
check(
    "agent_control routes into response_defense (AI Defense stays the last word)",
    '"agent_control", _terminal_router, {"end": END, "next": "response_defense"}' in graph_src,
)

chat_src = (ROOT / "backend/routers/chat.py").read_text()
check("router forwards the flag", "agent_control_review=chat_request.agent_control_review" in chat_src)

engine_src = (ROOT / "backend/services/recommendation_engine.py").read_text()
check(
    "legacy engine path enforces it too (graph-unavailable fallback)",
    "if agent_control_review and agent_control_client.is_configured:" in engine_src,
)
check("legacy engine has the block handler", "def _handle_agent_control_block(" in engine_src)
check(
    "block handler attributes the guardrail to Galileo",
    'guardrail_ids=["galileo_agent_control"]' in engine_src,
)

gov_src = (ROOT / "backend/agents/nodes/governance.py").read_text()
check(
    "non-blocking matches are attributed on the allowed-turn event",
    'guardrail_ids.append("galileo_agent_control")' in gov_src,
)

html = (ROOT / "frontend/index.html").read_text()
check("drawer has the Agent Observability Controls toggle", 'id="agentControlToggle"' in html)
check("toggle is labelled as asked", "Agent Observability Controls" in html)
check(
    "toggle sits directly beneath Cisco AI Defense Policy Review",
    html.index("Cisco AI Defense Policy Review")
    < html.index("Agent Observability Controls")
    < html.index("Internal Policy Engine"),
)

js = (ROOT / "frontend/js/chat.js").read_text()
check("toggle handler exists", "function toggleAgentControl()" in js)
check("toggle state is persisted", "medadvice_agent_control_enabled" in js)
check("flag is sent on every chat turn", "agent_control_review: agentControlEnabled" in js)

os.environ.pop("GALILEO_API_KEY", None)

print(f"\n{'FAILED' if _fails else 'OK'}: {_fails} failure(s)")
raise SystemExit(1 if _fails else 0)
