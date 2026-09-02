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
    """Minimal httpx.Response stand-in.

    ``raise_for_status`` raises like the real thing for 4xx/5xx, so a test can
    reproduce the actual failure this deployment sees (``/evaluation`` answering
    401 when no runtime token could be minted) instead of a lenient 200.
    """

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self
            )
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
        model="mistral-nemo:12b",
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
      and (step.get("context") or {}).get("model") == "mistral-nemo:12b")
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
    "llm_model": "mistral-nemo:12b",
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
    handler_kwargs.get("llm_model") == "mistral-nemo:12b"
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

# ---- 5. ported Luna comparison semantics --------------------------------
# These pin the bug that made control 259 unable to ever fire: the `correctness`
# scorer emits a BOOLEAN, and a numeric operator over a bool raises inside the
# evaluator (reported as an evaluator error -> fail open). Do not "simplify"
# coerce_number to treat bools as 0/1 — that silently changes every control's
# verdict and diverges from what the server would decide.
print("\n[5] Luna comparison semantics (ported verbatim)")
check("eq/false matches a false score (control 259 as repaired)",
      ac.score_matches(False, "eq", False) is True)
check("eq/false does not match a true score", ac.score_matches(True, "eq", False) is False)
try:
    ac.score_matches(False, "lt", 0.5)
    check("lt over a boolean score raises (the original 259 defect)", False)
except ValueError:
    check("lt over a boolean score raises (the original 259 defect)", True)
check("any is INVERTED for correctness (fires on a correct answer)",
      ac.score_matches(True, "any", None) is True
      and ac.score_matches(False, "any", None) is False)
check("numeric lt still works for float scorers",
      ac.score_matches(0.2, "lt", 0.5) is True and ac.score_matches(0.8, "lt", 0.5) is False)
check("string scores coerce numerically", ac.score_matches("0.2", "lt", 0.5) is True)
check("ne compares directly", ac.score_matches(True, "ne", False) is True)
check("contains over a list score (the PII SLM shape)",
      ac.score_matches(["ssn", "phone_number"], "contains", "ssn") is True
      and ac.score_matches([], "contains", "ssn") is False)
check("coerce_number returns None for bool/None",
      ac.coerce_number(True) is None and ac.coerce_number(None) is None)
try:
    ac.score_matches(0.2, "wat", 0.5)
    check("an unsupported operator raises", False)
except ValueError:
    check("an unsupported operator raises", True)

# A deny control whose evaluator errored comes back under `errors`, not
# `matches`, while the engine still flips is_safe to False. Reading only
# `matches` took that as a clean pass — neither blocking nor honoring fail-open.
errored_deny = ac.AgentControlClient._parse_response(
    {
        "is_safe": False,
        "confidence": 0.0,
        "matches": [],
        "errors": [
            {
                "control_name": "DemoBot-block-hallucinated-output",
                "action": "deny",
                "result": {"matched": False, "confidence": 0.0, "error": "not numeric"},
            }
        ],
    }
)
check("is_safe=false with only errors is an ERROR, not a clean pass",
      errored_deny.errored is True)
check("the errored deny control is named in the message",
      "DemoBot-block-hallucinated-output" in (errored_deny.error_message or ""))
with mock.patch.object(settings, "galileo_agent_control_fail_open", False):
    check("errored deny withholds under fail-closed", errored_deny.should_block is True)
with mock.patch.object(settings, "galileo_agent_control_fail_open", True):
    check("errored deny releases under fail-open", errored_deny.should_block is False)

# ---- 6. client-side execution -------------------------------------------
print("\n[6] client-side execution")

# The live shape of GET /agents/{name}/controls: definition under "control".
CONTROLS_BODY = {
    "controls": [
        {
            "id": 259,
            "name": "DemoBot-block-hallucinated-output",
            "control": {
                "enabled": True,
                "execution": "server",
                "scope": {"step_types": ["llm"], "stages": ["post"]},
                "action": {"decision": "deny"},
                "condition": {
                    "selector": {"path": "*"},
                    "evaluator": {
                        "name": "galileo.luna",
                        "config": {
                            "scorer_label": "correctness",
                            "scorer_id": "89d579ce",
                            "operator": "eq",
                            "threshold": False,
                            "timeout_ms": 15000,
                        },
                    },
                },
            },
        }
    ]
}


def _client_transport(score_body, *, controls=None, runtime=False, calls=None):
    """Build (post, get) fakes for a client-side evaluation.

    runtime=False reproduces today's deployment: the exchange 502s, so the
    server path cannot produce a verdict and the client path takes over.
    """
    calls = calls if calls is not None else []

    def _post(url, **kwargs):
        calls.append(url)
        if url.endswith("/v2/login/api_key"):
            return _FakeResponse({"access_token": "at.eyJleHAiOjk5OTk5OTk5OTl9.sig"})
        if url.endswith("/runtime-token-exchange"):
            if runtime:
                return _FakeResponse({"token": "rt.eyJleHAiOjk5OTk5OTk5OTl9.sig"})
            raise ac.httpx.ConnectError("no runtime grant")
        if url.endswith("/api/v1/evaluation"):
            if not runtime:
                # What the live deployment actually answers when the console
                # token is presented in place of a runtime token.
                return _FakeResponse(
                    {"detail": 'Runtime token is invalid: Token is missing the "iat" claim'},
                    status_code=401,
                )
            return _FakeResponse({"is_safe": True, "confidence": 0.0, "matches": []})
        if url.endswith("/scorers/invoke"):
            _post.invoke_body = kwargs.get("json")
            return _FakeResponse(score_body)
        raise AssertionError(f"unexpected POST {url}")

    def _get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(CONTROLS_BODY if controls is None else controls)

    _post.invoke_body = None
    return _post, _get, calls


def _evaluate(score_body, *, controls=None, runtime=False, execution="auto"):
    post, get, calls = _client_transport(score_body, controls=controls, runtime=runtime)
    client = ac.AgentControlClient()
    with mock.patch.object(ac.httpx, "post", side_effect=post), mock.patch.object(
        ac.httpx, "get", side_effect=get
    ), mock.patch.object(settings, "galileo_agent_control_execution", execution):
        verdict = client.evaluate_response("How much Tylenol is safe?", "12,000 mg a day.")
    return verdict, post, calls, client


# correctness=false -> incorrect -> the deny control matches
deny, post, calls, _ = _evaluate({"score": False, "status": "success"})
check("falls back to client-side when no runtime token can be minted",
      deny.transport == "client")
check("a false correctness score denies", deny.should_block is True)
check("the matched control is named", deny.matched_controls == ["DemoBot-block-hallucinated-output"])
check("decision is deny", deny.decisions == ["deny"])
check("scorer invoked with query + response as plain strings",
      (post.invoke_body or {}).get("inputs")
      == {"query": "How much Tylenol is safe?", "response": "12,000 mg a day."})
check("scorer id/label forwarded from the control definition",
      (post.invoke_body or {}).get("scorer_id") == "89d579ce"
      and (post.invoke_body or {}).get("scorer_label") == "correctness")

allow, _, _, _ = _evaluate({"score": True, "status": "success"})
check("a true correctness score releases", allow.should_block is False)
check("released client verdict is not errored", allow.errored is False)

# Today's real behavior: the LLM-judge scorers fail server-side.
broken, _, _, _ = _evaluate(
    {
        "score": None,
        "status": "failed",
        "error_message": "'str' object has no attribute 'format_content_for_message'",
    }
)
check("a failed scorer invoke is an evaluator error, never a match",
      broken.errored is True and broken.matched_controls == [])
check("the scorer's own error text is preserved",
      "format_content_for_message" in (broken.error_message or ""))
with mock.patch.object(settings, "galileo_agent_control_fail_open", True):
    check("failed scorer releases under fail-open (today's path)", broken.should_block is False)
with mock.patch.object(settings, "galileo_agent_control_fail_open", False):
    check("failed scorer withholds under fail-closed", broken.should_block is True)

# The server path stays preferred when a runtime token IS mintable.
server_verdict, post_srv, calls_srv, _ = _evaluate(
    {"score": False, "status": "success"}, runtime=True
)
check("runtime token available -> server transport", server_verdict.transport == "server")
check("server path does not invoke scorers locally",
      not any(u.endswith("/scorers/invoke") for u in calls_srv))
check("execution=server never falls back",
      _evaluate({"score": False, "status": "success"}, execution="server")[0].transport
      == "server")
check("execution=client skips the server entirely",
      not any(
          u.endswith("/api/v1/evaluation")
          for u in _evaluate({"score": True, "status": "success"}, execution="client")[2]
      ))

# Scope / enabled / evaluator filtering — none of these may invoke a scorer.
def _only(definition_overrides):
    body = json.loads(json.dumps(CONTROLS_BODY))
    body["controls"][0]["control"].update(definition_overrides)
    return body


for label, override in [
    ("stage pre is skipped", {"scope": {"stages": ["pre"], "step_types": ["llm"]}}),
    ("step_type tool is skipped", {"scope": {"stages": ["post"], "step_types": ["tool"]}}),
    ("disabled control is skipped", {"enabled": False}),
]:
    verdict, post_f, calls_f, _ = _evaluate(
        {"score": False, "status": "success"}, controls=_only(override)
    )
    check(f"{label} (no scorer call, no block)",
          not any(u.endswith("/scorers/invoke") for u in calls_f)
          and verdict.should_block is False)

unsupported = _only(
    {"condition": {"selector": {"path": "*"}, "evaluator": {"name": "regex", "config": {}}}}
)
verdict_u, _, calls_u, _ = _evaluate({"score": False, "status": "success"}, controls=unsupported)
check("a non-Luna evaluator is reported unevaluated, not guessed at",
      not any(u.endswith("/scorers/invoke") for u in calls_u)
      and verdict_u.errored is True
      and any("regex" in e for e in verdict_u.evaluator_errors))

# Definition caching: one fetch serves many turns.
post_c, get_c, calls_c = _client_transport({"score": True, "status": "success"})
client_c = ac.AgentControlClient()
with mock.patch.object(ac.httpx, "post", side_effect=post_c), mock.patch.object(
    ac.httpx, "get", side_effect=get_c
), mock.patch.object(settings, "galileo_agent_control_execution", "client"):
    for _ in range(3):
        client_c.evaluate_response("q", "a")
fetches = [u for u in calls_c if u.endswith("/controls")]
check("control definitions are fetched once across turns (TTL cache)", len(fetches) == 1)
check("invoke still runs per turn",
      len([u for u in calls_c if u.endswith("/scorers/invoke")]) == 3)

# A refresh failure serves the stale set rather than failing the turn.
fail_get = mock.Mock(side_effect=ac.httpx.ConnectError("management api down"))
with mock.patch.object(ac.httpx, "post", side_effect=post_c), mock.patch.object(
    ac.httpx, "get", fail_get
), mock.patch.object(settings, "galileo_agent_control_execution", "client"), mock.patch.object(
    settings, "galileo_agent_control_refresh_seconds", 0.0
):
    stale = client_c.evaluate_response("q", "a")
check("a definition-refresh failure reuses the cached set", stale.errored is False)

# Cold cache + fetch failure = errored verdict (fail-open decides).
post_e, _, _ = _client_transport({"score": True, "status": "success"})
client_e = ac.AgentControlClient()
with mock.patch.object(ac.httpx, "post", side_effect=post_e), mock.patch.object(
    ac.httpx, "get", mock.Mock(side_effect=ac.httpx.ConnectError("down"))
), mock.patch.object(settings, "galileo_agent_control_execution", "client"):
    cold = client_e.evaluate_response("q", "a")
check("cold cache + unreachable management API -> errored verdict", cold.errored is True)
with mock.patch.object(settings, "galileo_agent_control_fail_open", True):
    check("...which fail-open releases", cold.should_block is False)

# ---- 6b. composite conditions + the per-evaluation scorer memo ----------
# The output-PII control ORs one `contains` leaf per category, because the
# Output PII (SLM) scorer returns a LIST (["ssn","name",…]) and `contains` takes a
# single category. Without a memo that shape costs one judge call per leaf.
print("\n[6b] composite conditions + scorer memo")

PII_CATS = ["ssn", "date_of_birth", "email", "phone_number"]
PII_CONTROLS = {
    "controls": [
        {
            "id": 263,
            "name": "DemoBot-block-output-pii",
            "control": {
                "enabled": True,
                "execution": "server",
                "scope": {"step_types": ["llm"], "stages": ["post"]},
                "action": {"decision": "deny"},
                "condition": {
                    "or": [
                        {
                            "selector": {"path": "*"},
                            "evaluator": {
                                "name": "galileo.luna",
                                "config": {
                                    "scorer_label": "Output PII (SLM)",
                                    "scorer_id": "88eada48",
                                    "operator": "contains",
                                    "threshold": cat,
                                },
                            },
                        }
                        for cat in PII_CATS
                    ]
                },
            },
        }
    ]
}

# A response the scorer flags with a category NOT in the first leaf, to prove the
# OR keeps walking rather than stopping at the first non-match.
pii_hit, post_pii, calls_pii, _ = _evaluate(
    {"score": ["name", "phone_number"], "status": "success"}, controls=PII_CONTROLS
)
check("an OR of contains leaves denies on a later category",
      pii_hit.should_block is True and pii_hit.matched_controls == ["DemoBot-block-output-pii"])
check("the deciding category is reported in the message",
      "phone_number" in " ".join(pii_hit.messages))
check("OR over one scorer costs ONE invoke, not one per leaf (memo)",
      len([u for u in calls_pii if u.endswith("/scorers/invoke")]) == 1)

# `name` alone must not trigger: a normal DemoBot answer naming a care provider
# scores ["name"], so including that category would withhold good responses.
pii_name_only, _, calls_name, _ = _evaluate(
    {"score": ["name"], "status": "success"}, controls=PII_CONTROLS
)
check("a bare name does not trigger the PII control (no over-blocking)",
      pii_name_only.should_block is False)
check("a clean scorer result releases",
      _evaluate({"score": [], "status": "success"}, controls=PII_CONTROLS)[0].should_block
      is False)

# AND short-circuits, NOT inverts.
def _tree(node):
    body = json.loads(json.dumps(PII_CONTROLS))
    body["controls"][0]["control"]["condition"] = node
    return body


LEAF_HIT = {
    "selector": {"path": "*"},
    "evaluator": {
        "name": "galileo.luna",
        "config": {"scorer_id": "88eada48", "operator": "contains", "threshold": "ssn"},
    },
}
LEAF_MISS = {
    "selector": {"path": "*"},
    "evaluator": {
        "name": "galileo.luna",
        "config": {"scorer_id": "88eada48", "operator": "contains", "threshold": "password"},
    },
}
check("AND of hit+miss does not match",
      _evaluate({"score": ["ssn"], "status": "success"},
                controls=_tree({"and": [LEAF_HIT, LEAF_MISS]}))[0].should_block is False)
check("AND of hit+hit matches",
      _evaluate({"score": ["ssn"], "status": "success"},
                controls=_tree({"and": [LEAF_HIT, LEAF_HIT]}))[0].should_block is True)
check("NOT inverts a leaf",
      _evaluate({"score": ["ssn"], "status": "success"},
                controls=_tree({"not": LEAF_HIT}))[0].should_block is False)

# A failing scorer inside a tree is memoized too: still one call, still an error.
tree_err, _, calls_err, _ = _evaluate(
    {"status": "failed", "error_message": "boom"}, controls=PII_CONTROLS
)
check("a failing scorer in a tree errors once, not per leaf",
      tree_err.errored is True
      and len([u for u in calls_err if u.endswith("/scorers/invoke")]) == 1)

# ---- 7. wiring ----------------------------------------------------------
print("\n[7] wiring")
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

# The guardrail chain is wired once for every blueprint (backend/agents/blueprints/
# guardrails.py), so assert the COMPILED graph rather than source strings.
from backend.agents.blueprints import list_blueprints  # noqa: E402
from backend.agents.graph import build_theme_subgraph  # noqa: E402
from backend.agents.themes import THEMES  # noqa: E402

for _bp in list_blueprints():
    _edges = {(e.source, e.target) for e in build_theme_subgraph(THEMES["medadvice"], _bp).get_graph().edges}
    _nodes = set(build_theme_subgraph(THEMES["medadvice"], _bp).get_graph().nodes)
    check(f"[{_bp.key}] node registered on the theme subgraph", "agent_control" in _nodes)
    check(f"[{_bp.key}] compliance routes into agent_control", ("compliance", "agent_control") in _edges)
    # NeMo Guardrails' output rails sit between Agent Control and AI Defense, so
    # the chain is agent_control -> nemo_output_rails -> response_defense: Cisco
    # is still the last word on output.
    check(
        f"[{_bp.key}] agent_control -> nemo_output_rails -> response_defense (AI Defense stays the last word)",
        ("agent_control", "nemo_output_rails") in _edges and ("nemo_output_rails", "response_defense") in _edges,
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
check(
    "block metadata records which transport decided",
    '"transport": verdict.transport' in engine_src,
)

check(
    "new settings exposed with documented defaults",
    settings.galileo_agent_control_execution == "auto"
    and settings.galileo_agent_control_refresh_seconds == 300.0,
)

script_src = (ROOT / "scripts/demo/register_agent_control.py").read_text()
check(
    "setup script reads the 'control' key this endpoint actually returns",
    'control.get("control")' in script_src,
)
check("setup script can replace a control definition", "--set-data" in script_src)
control_json = json.loads(
    (ROOT / "scripts/demo/controls/259-block-hallucinated-output.json").read_text()
)
cfg_259 = control_json["condition"]["evaluator"]["config"]
check(
    "checked-in control 259 uses eq/false, not a numeric operator",
    cfg_259["operator"] == "eq" and cfg_259["threshold"] is False,
)
check(
    "checked-in control 259 stays execution=server (inherits the server path later)",
    control_json["execution"] == "server",
)

pii_json = json.loads(
    (ROOT / "scripts/demo/controls/block-output-pii.json").read_text()
)
pii_leaves = pii_json["condition"]["or"]
pii_thresholds = {
    leaf["evaluator"]["config"]["threshold"] for leaf in pii_leaves
}
check(
    "checked-in output-PII control ORs contains leaves over the PII SLM scorer",
    all(
        leaf["evaluator"]["config"]["operator"] == "contains"
        and leaf["evaluator"]["config"]["scorer_id"] == "88eada48-109f-4f4e-8dc6-bf65799a331e"
        for leaf in pii_leaves
    ),
)
check(
    "output-PII control covers the sensitive identifiers",
    {"ssn", "date_of_birth", "email", "phone_number"} <= pii_thresholds,
)
check(
    "output-PII control excludes the bare 'name' category (would over-block "
    "answers naming a care provider)",
    "name" not in pii_thresholds,
)
check("output-PII control denies", pii_json["action"]["decision"] == "deny")

check(
    "the block message does not claim a specific cause (any deny control lands there)",
    "factually reliable" not in engine_src
    and "withheld by our agent control" in engine_src,
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
