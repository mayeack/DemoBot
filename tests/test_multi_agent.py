#!/usr/bin/env python3
"""Regression: the multi-agent stage (coordinator -> specialists -> synthesizer).

Guards the re-architecture that replaced the single ``domain`` agent with a
themed multi-agent pipeline that always emits a coordinator + 1..N specialist +
synthesizer trace. Verifies the invariants:
  - coordinator always selects >=1 specialist (defaults to the theme's primary
    on an empty/invalid plan), and a coordinator failure short-circuits safely;
  - specialists run sequentially, sum tokens onto the running total, and
    continue-on-partial-failure (terminate only if ALL fail);
  - the synthesizer produces the structured recommendation (or {reply} for a
    conversational theme), rolls the governance directive exactly once, and sums
    its tokens; ``agent_trace`` accumulates one entry per agent;
  - a full run_turn produces summed usage + a complete agent_trace for Galileo.

DB/network-free: ``invoke_agent`` is faked per node module; no real LLM calls.

    venv/bin/python tests/test_multi_agent.py    # exit 0 = pass
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["OTEL_ENABLED"] = "false"
os.environ.pop("GALILEO_API_KEY", None)

from backend.agents.llm import ChatModelError, NormalizedLLMResponse  # noqa: E402
from backend.agents.nodes import coordinator as coord_mod  # noqa: E402
from backend.agents.nodes import specialists as spec_mod  # noqa: E402
from backend.agents.nodes import synthesizer as synth_mod  # noqa: E402
from backend.agents.themes import THEMES  # noqa: E402

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


def _resp(content: str, in_tok: int, out_tok: int, rid: str = "x") -> NormalizedLLMResponse:
    return NormalizedLLMResponse(
        id=rid, content=content, model="fake-model",
        input_tokens=in_tok, output_tokens=out_tok, stop_reason="end_turn",
    )


# A configurable fake invoke_agent dispatching on the agent name suffix.
class FakeLLM:
    def __init__(self, *, coordinator_content, fail_specialists=(), synth_content):
        self.coordinator_content = coordinator_content
        self.fail_specialists = set(fail_specialists)
        self.synth_content = synth_content
        self.calls = []

    def __call__(self, settings, *, agent_name, system, messages,
                 max_tokens=2048, temperature=0.7, fallback_model=None):
        self.calls.append(agent_name)
        if agent_name.endswith("_coordinator"):
            return _resp(self.coordinator_content, 10, 5, rid="coord")
        if agent_name.endswith("_specialist"):
            if agent_name in self.fail_specialists:
                raise ChatModelError(f"boom: {agent_name}")
            return _resp("- internal finding", 20, 8, rid="spec")
        # synthesizer == {theme}_domain_agent
        return _resp(self.synth_content, 30, 40, rid="synth")


def _install(fake) -> None:
    coord_mod.invoke_agent = fake
    spec_mod.invoke_agent = fake
    synth_mod.invoke_agent = fake


def _base_state(theme_key: str) -> dict:
    return {
        "session_id": "s1",
        "request_id": "r1",
        "trace_id": "t1",
        "theme": theme_key,
        "conversation_history": [{"role": "user", "content": "I have a headache"}],
        "user_message": "I have a headache",
        # Force the directive OFF so the test is deterministic (no random content).
        "force_pii_injection": False,
        "force_toxic_injection": False,
        "force_hallucination_injection": False,
        "force_boundary_injection": False,
    }


def test_coordinator_valid_plan() -> None:
    fake = FakeLLM(
        coordinator_content='{"specialists": ["triage", "symptom_analysis"], "rationale": "x"}',
        synth_content="{}",
    )
    _install(fake)
    node = coord_mod.make_coordinator_agent(THEMES["medadvice"])
    out = node(_base_state("medadvice"))
    check("coordinator selects the planned specialists",
          out["selected_specialists"] == ["triage", "symptom_analysis"])
    check("coordinator emits one agent_trace entry (role=coordinator)",
          len(out["agent_trace"]) == 1 and out["agent_trace"][0]["role"] == "coordinator")
    check("coordinator seeds the running token totals",
          out["llm_input_tokens"] == 10 and out["llm_output_tokens"] == 5)


def test_coordinator_empty_plan_defaults_primary() -> None:
    fake = FakeLLM(coordinator_content='{"specialists": [], "rationale": "n/a"}', synth_content="{}")
    _install(fake)
    node = coord_mod.make_coordinator_agent(THEMES["taxadvice"])
    out = node(_base_state("taxadvice"))
    check("empty plan -> defaults to the theme primary specialist (>=1 guarantee)",
          out["selected_specialists"] == ["deductions"])


def test_coordinator_invalid_keys_and_cap() -> None:
    fake = FakeLLM(
        coordinator_content='{"specialists": ["bogus", "triage", "medication_safety", "care_navigation"]}',
        synth_content="{}",
    )
    _install(fake)
    node = coord_mod.make_coordinator_agent(THEMES["medadvice"])
    out = node(_base_state("medadvice"))
    check("coordinator drops unknown keys and caps the fan-out at 3",
          out["selected_specialists"] == ["triage", "medication_safety", "care_navigation"])


def test_coordinator_error_terminates() -> None:
    def boom(*a, **k):
        raise ChatModelError("coordinator down")
    _install(boom)
    node = coord_mod.make_coordinator_agent(THEMES["medadvice"])
    out = node(_base_state("medadvice"))
    check("coordinator ChatModelError -> terminal safe result",
          out.get("terminal") is True and out["result"]["escalated"] is True)


def test_specialists_sum_and_partial_failure() -> None:
    fail = "medadvice_symptom_analysis_specialist"
    fake = FakeLLM(coordinator_content="{}", fail_specialists=[fail], synth_content="{}")
    _install(fake)
    node = spec_mod.make_specialists_agent(THEMES["medadvice"])
    state = _base_state("medadvice")
    state.update({
        "selected_specialists": ["triage", "symptom_analysis", "medication_safety"],
        "agent_trace": [{"name": "medadvice_coordinator", "role": "coordinator",
                         "model": "fake-model", "input_tokens": 10, "output_tokens": 5,
                         "output_text": "plan", "status": "ok"}],
        "llm_input_tokens": 10, "llm_output_tokens": 5,
    })
    out = node(state)
    check("two specialists succeeded -> two outputs collected",
          len(out["specialist_outputs"]) == 2)
    check("tokens summed onto the running total (10 + 2*20 in, 5 + 2*8 out)",
          out["llm_input_tokens"] == 50 and out["llm_output_tokens"] == 21)
    err = [t for t in out["agent_trace"] if t.get("status") == "error"]
    check("failed specialist recorded with status=error, turn continues",
          len(err) == 1 and err[0]["name"] == fail)
    check("agent_trace now has coordinator + 3 specialist entries",
          len(out["agent_trace"]) == 4)


def test_specialists_all_fail_terminates() -> None:
    fails = ["medadvice_triage_specialist", "medadvice_medication_safety_specialist"]
    fake = FakeLLM(coordinator_content="{}", fail_specialists=fails, synth_content="{}")
    _install(fake)
    node = spec_mod.make_specialists_agent(THEMES["medadvice"])
    state = _base_state("medadvice")
    state.update({"selected_specialists": ["triage", "medication_safety"],
                  "agent_trace": [], "llm_input_tokens": 10, "llm_output_tokens": 5})
    out = node(state)
    check("all specialists fail -> terminal safe result", out.get("terminal") is True)


def test_synthesizer_structured() -> None:
    synth = ('```json\n{"assessment": "a", "guidance": ["g1"], '
             '"seek_care_if": ["s1"], "severity": "LOW", "confidence": 0.8}\n```')
    fake = FakeLLM(coordinator_content="{}", synth_content=synth)
    _install(fake)
    node = synth_mod.make_synthesizer_agent(THEMES["medadvice"])
    state = _base_state("medadvice")
    state.update({
        "specialist_outputs": [{"key": "triage", "label": "Triage", "analysis": "urgent-ish"}],
        "agent_trace": [{"name": "x", "role": "coordinator", "output_text": "p", "status": "ok",
                         "model": "m", "input_tokens": 10, "output_tokens": 5}],
        "llm_input_tokens": 30, "llm_output_tokens": 13,
    })
    out = node(state)
    check("synthesizer parses the structured recommendation",
          out["recommendation"]["assessment"] == "a")
    check("synthesizer normalizes severity", str(out["severity"].value) == "LOW")
    check("synthesizer builds the user-facing final_message", "Assessment" in out["final_message"])
    check("synthesizer sums its tokens onto the running total (30+30 / 13+40)",
          out["llm_input_tokens"] == 60 and out["llm_output_tokens"] == 53)
    check("synthesizer sets requested_categories (one directive roll)",
          isinstance(out["requested_categories"], dict)
          and set(out["requested_categories"]) == {"pii", "toxic", "hallucination", "authority"})
    check("synthesizer is the final agent_trace entry (role=synthesizer)",
          out["agent_trace"][-1]["role"] == "synthesizer")
    check("synthesizer keeps the {theme}_domain_agent name",
          out["agent_trace"][-1]["name"] == "medadvice_domain_agent")


def test_synthesizer_conversational_reply() -> None:
    fake = FakeLLM(coordinator_content="{}",
                   synth_content='{"reply": "Try restarting your router.", "severity": "LOW", "confidence": 0.9}')
    _install(fake)
    node = synth_mod.make_synthesizer_agent(THEMES["telecomchatbot"])
    state = _base_state("telecomchatbot")
    state.update({"specialist_outputs": [], "agent_trace": [],
                  "llm_input_tokens": 0, "llm_output_tokens": 0})
    out = node(state)
    check("conversational theme renders the reply verbatim as final_message",
          out["final_message"] == "Try restarting your router.")


def test_full_run_turn_telecom() -> None:
    """End-to-end through the compiled graph (telecom skips the clarifier)."""
    from backend.logging.governance_logger import governance_logger
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
    orig = governance_logger.log_response
    governance_logger.log_response = capture
    fake = FakeLLM(
        coordinator_content='{"specialists": ["network_diagnostics", "device_setup"]}',
        synth_content='{"reply": "Restart the modem, then your phone.", "severity": "LOW", "confidence": 0.9}',
    )
    _install(fake)
    try:
        from backend.agents.graph import run_turn
        result = run_turn(
            session_id="s1", user_message="My wifi is slow",
            conversation_history=[{"role": "user", "content": "My wifi is slow"}],
            theme="telecomchatbot",
        )
    finally:
        governance_logger.log_response = orig

    check("run_turn returns the synthesized reply",
          result.get("message") == "Restart the modem, then your phone.")
    check("governance received summed usage across all agents "
          "(coord 10/5 + 2 spec 40/16 + synth 30/40 = 80/61)",
          captured.get("usage_data", {}).get("usage_input_tokens") == 80
          and captured.get("usage_data", {}).get("usage_output_tokens") == 61)
    trace = captured.get("agent_trace") or []
    roles = [t["role"] for t in trace]
    check("agent_trace carries coordinator + 2 specialists + synthesizer (4 agents)",
          roles == ["coordinator", "specialist", "specialist", "synthesizer"])
    check("agent names are themed per application",
          trace[0]["name"] == "telecomchatbot_coordinator"
          and trace[-1]["name"] == "telecomchatbot_domain_agent")


def main() -> int:
    for fn in (
        test_coordinator_valid_plan,
        test_coordinator_empty_plan_defaults_primary,
        test_coordinator_invalid_keys_and_cap,
        test_coordinator_error_terminates,
        test_specialists_sum_and_partial_failure,
        test_specialists_all_fail_terminates,
        test_synthesizer_structured,
        test_synthesizer_conversational_reply,
        test_full_run_turn_telecom,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global _fails
            _fails += 1
            print(f"  ERROR {fn.__name__}: {e}")
    print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
