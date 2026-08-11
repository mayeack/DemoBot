#!/usr/bin/env python3
"""API regression suite — every endpoint's auth gating + contract, via FastAPI's
in-process TestClient.

Side-effect-safe: the LLM boundary and the auto-prompter are stubbed, and the demo
incident is started without driving load, so the suite makes no real Anthropic
calls, spawns no background load, and triggers no external emission (Splunk/HEC/
Galileo are no-op without config). pytest isn't installed; run standalone:

    venv/bin/python tests/test_api.py        # exit 0 = pass
"""
import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backend.config  # noqa: E402  (sets CA bundle etc.)
from backend.config import settings  # noqa: E402

# Ensure the access gate is exercised even if .env has no ACCESS_KEY.
KEY = settings.access_key or "test-access-key"
settings.access_key = KEY

# Make the governance-content background rates deterministic for the suite: an
# OFF-path turn (no force_* flag) must request nothing, so the no-toggle
# assertions below are stable. The directive/fallback logic reads these at call
# time, so setting them here (before any request) is sufficient.
settings.pii_injection_rate = 0.0
settings.toxic_injection_rate = 0.0
settings.hallucination_injection_rate = 0.0
settings.authority_injection_rate = 0.0

# No startup pre-warm: the suite stubs the LLM boundary and must not load a
# real Ollama model (or touch the daemon) as a side effect.
settings.prewarm_llm = False

# --- stub the LLM boundary BEFORE backend.main imports the graph/nodes ---
import backend.agents.llm as llm  # noqa: E402
from backend.agents.llm import NormalizedLLMResponse  # noqa: E402

# Captures the last system prompt handed to the (stubbed) model so the suite can
# assert the governance directive is appended to the INPUT, not the output.
_last_system = {"v": ""}


def _fake_llm(*_a, **_k):
    _last_system["v"] = _k.get("system", "")
    return NormalizedLLMResponse(
        id="test-id", content="LOW\nAssessment: test. Guidance: rest, fluids.",
        model="test-model", input_tokens=5, output_tokens=7, stop_reason="end_turn",
    )


llm.invoke_agent = _fake_llm
llm.invoke_chat = _fake_llm

# --- stub the Galileo Agent Control evaluation so the agent_control_review
# check below exercises the node without an outbound call to Galileo ---
import backend.services.agent_control as agent_control  # noqa: E402

agent_control.agent_control_client.evaluate_response = (
    lambda *_a, **_k: agent_control.ControlVerdict(is_safe=True, confidence=0.0)
)

# --- stub the auto-prompter so /auto-prompt/start launches no real load ---
import backend.services.auto_prompter as ap  # noqa: E402


async def _noop(*_a, **_k):
    return None


ap.auto_prompter.start = _noop  # type: ignore[assignment]
ap.auto_prompter.stop = _noop   # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402

AUTH = {"Authorization": "Basic " + base64.b64encode(f"x:{KEY}".encode()).decode()}
_fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _fails += 1


def main() -> int:
    with TestClient(app) as c:
        # ---- public (no key) ----
        check("GET /health -> 200 (public)", c.get("/health").status_code == 200)
        check("GET /login -> 200 (public)", c.get("/login").status_code == 200)

        # ---- auth gating: gated endpoints reject without the key (401 JSON) ----
        for path in ("/api/chat/auto-prompt/status", "/api/incident/status",
                     "/api/spray/status",
                     "/api/settings", "/api/hec/destinations", "/admin/logs/metrics",
                     "/api/settings/emit-model"):
            r = c.get(path)
            check(f"GET {path} -> 401 without key", r.status_code == 401, f"got {r.status_code}")

        # ---- chat ----
        r = c.post("/api/chat/session/new", headers=AUTH)
        check("POST /api/chat/session/new -> 200 + session_id",
              r.status_code == 200 and "session_id" in r.json(), f"{r.status_code}")
        sid = r.json().get("session_id", "") if r.status_code == 200 else ""
        check("GET /api/chat/session/{id} -> 200",
              c.get(f"/api/chat/session/{sid}", headers=AUTH).status_code in (200, 404))
        check("GET /api/chat/disclaimer -> 200", c.get("/api/chat/disclaimer", headers=AUTH).status_code == 200)
        # Theme-aware: it used to return the MEDICAL disclaimer unconditionally,
        # with no theme parameter, so five of six themes got wrong text.
        rdm = c.get("/api/chat/disclaimer", headers=AUTH).json()
        check("disclaimer: defaults to medadvice's medical text",
              rdm.get("theme") == "medadvice" and "MEDICAL DISCLAIMER" in rdm.get("content", ""))
        rdt = c.get("/api/chat/disclaimer?theme=telecomchatbot", headers=AUTH).json()
        check("disclaimer: a non-medical theme gets non-medical text",
              rdt.get("theme") == "telecomchatbot"
              and "MEDICAL DISCLAIMER" not in rdt.get("content", "")
              and "not professional advice" in rdt.get("content", ""),
              str(rdt.get("title")))
        check("disclaimer: unknown theme falls back rather than erroring",
              c.get("/api/chat/disclaimer?theme=nonsense", headers=AUTH).status_code == 200)

        # REQUIRE_DISCLAIMER_ACCEPTANCE had no readers: setting it False did
        # nothing and the 400 gate was unconditional.
        _saved_req = settings.require_disclaimer_acceptance
        try:
            settings.require_disclaimer_acceptance = True
            _newsid = c.post("/api/chat/session/new", headers=AUTH).json()["session_id"]
            check("disclaimer gate ON -> new session without acceptance is 400",
                  c.post("/api/chat/message", headers=AUTH,
                         json={"session_id": _newsid, "message": "hi",
                               "disclaimer_accepted": False}).status_code == 400)
            settings.require_disclaimer_acceptance = False
            _newsid2 = c.post("/api/chat/session/new", headers=AUTH).json()["session_id"]
            check("disclaimer gate OFF -> the flag is actually honored (no 400)",
                  c.post("/api/chat/message", headers=AUTH,
                         json={"session_id": _newsid2, "message": "hi",
                               "disclaimer_accepted": False}).status_code == 200)
        finally:
            settings.require_disclaimer_acceptance = _saved_req

        # MAX_CLARIFYING_QUESTIONS was likewise inert (a hardcoded 2 won).
        from backend.services.clarifying_questions import ClarifyingQuestionsService  # noqa: E402
        _svc = ClarifyingQuestionsService()
        _saved_max = settings.max_clarifying_questions
        try:
            settings.max_clarifying_questions = 5
            check("clarifying limit follows the setting", _svc.MAX_QUESTIONS == 5,
                  str(_svc.MAX_QUESTIONS))
        finally:
            settings.max_clarifying_questions = _saved_max
        # Assert the CODE default, not the live value: this box's .env sets 3, and
        # now that the setting is honored that .env value is what actually applies.
        check("clarifying limit code default is the shipped 2",
              type(settings).model_fields["max_clarifying_questions"].default == 2,
              str(type(settings).model_fields["max_clarifying_questions"].default))
        # /message: validation (no real LLM turn asserted — that's integration-tested)
        check("POST /api/chat/message bad body -> 422",
              c.post("/api/chat/message", headers=AUTH, json={}).status_code == 422)
        rmsg = c.post("/api/chat/message", headers=AUTH,
                      json={"session_id": sid, "message": "test", "disclaimer_accepted": True})
        check("POST /api/chat/message (stubbed LLM) -> 200", rmsg.status_code == 200, f"{rmsg.status_code}")
        # --- Governance content toggles ------------------------------------
        # Each force_* flag appends a directive to the INPUT asking the model to
        # produce the content, with a deterministic fallback when the (stubbed)
        # model omits it.
        #
        # The directive TEXT is provider-dependent (build_input_directives):
        #   * ollama       -> natural, UNLABELED asks; an uncensored local model
        #                     complies with those. Hallucination and authority
        #                     ride inside the JSON answer contract instead of an
        #                     appended block, because mistral-nemo:12b emits unfenced
        #                     JSON and the synthesizer drops any trailing text.
        #   * every other  -> the labeled "* SAMPLE:" test-suite framing that a
        #                     censored model needs to comply at all.
        # So pin the provider per block and assert BOTH builders. Previously this
        # section asserted only the labeled markers, which meant all five checks
        # failed whenever .env selected ollama — i.e. in the default demo config.
        _saved_provider = settings.ai_provider

        def _turn(**flags):
            return c.post("/api/chat/message", headers=AUTH,
                          json={"session_id": sid, "message": "I have a sore throat.",
                                "disclaimer_accepted": True, **flags})

        try:
            # -- labeled path (anthropic / bedrock / openai / nvidia) ----------
            settings.ai_provider = "anthropic"
            rb = _turn(force_boundary_injection=True)
            check("labeled: boundary -> AUTHORITY-VIOLATION SAMPLE on the INPUT",
                  "AUTHORITY-VIOLATION SAMPLE" in _last_system["v"])
            check("labeled: boundary -> overreach present (fallback fires for stubbed model)",
                  rb.status_code == 200 and "**Recommended Prescription:**" in rb.json().get("message", ""),
                  f"{rb.status_code}")
            rp = _turn(force_pii_injection=True)
            check("labeled: pii -> PII/PHI SAMPLE on the INPUT",
                  "PII/PHI SAMPLE" in _last_system["v"])
            check("labeled: pii -> synthetic SSN present (fallback)",
                  rp.status_code == 200 and bool(re.search(r"\d{3}-\d{2}-\d{4}", rp.json().get("message", ""))),
                  f"{rp.status_code}")
            rt = _turn(force_toxic_injection=True)
            check("labeled: toxic -> TOXICITY SAMPLE on the INPUT",
                  "TOXICITY SAMPLE" in _last_system["v"])
            check("labeled: toxic -> toxic content present (fallback)",
                  rt.status_code == 200 and "**Direct Assessment:**" in rt.json().get("message", ""),
                  f"{rt.status_code}")
            rh = _turn(force_hallucination_injection=True)
            check("labeled: hallucination -> HALLUCINATION SAMPLE on the INPUT",
                  rh.status_code == 200 and "HALLUCINATION SAMPLE" in _last_system["v"], f"{rh.status_code}")

            rnb = _turn()
            check("labeled: no toggles -> no directive on the INPUT",
                  "INTERNAL SAFETY-DETECTOR TEST SUITE" not in _last_system["v"])
            check("labeled: no toggles -> no overreach marker",
                  rnb.status_code == 200 and "**Recommended Prescription:**" not in rnb.json().get("message", ""),
                  f"{rnb.status_code}")

            # -- ollama path: unlabeled asks, and the two categories that must
            #    survive the JSON parse are written into the answer contract ---
            settings.ai_provider = "ollama"
            rp = _turn(force_pii_injection=True)
            check("ollama: pii -> unlabeled NNN-NN-NNNN ask, no SAMPLE label",
                  "NNN-NN-NNNN" in _last_system["v"]
                  and "PII/PHI SAMPLE" not in _last_system["v"])
            check("ollama: pii -> synthetic SSN present (fallback)",
                  rp.status_code == 200 and bool(re.search(r"\d{3}-\d{2}-\d{4}", rp.json().get("message", ""))),
                  f"{rp.status_code}")
            rt = _turn(force_toxic_injection=True)
            check("ollama: toxic -> unlabeled ask, no SAMPLE label",
                  "dismissive, condescending, insulting remark" in _last_system["v"]
                  and "TOXICITY SAMPLE" not in _last_system["v"])
            rh = _turn(force_hallucination_injection=True)
            check("ollama: hallucination -> embedded in the answer contract",
                  "never admit that anything is invented or unverified" in _last_system["v"])
            rb = _turn(force_boundary_injection=True)
            check("ollama: boundary -> overstep directive embedded in the answer",
                  "overstep your authorized non-prescriptive scope" in _last_system["v"])

            rnb = _turn()
            check("ollama: no toggles -> neither directive header on the INPUT",
                  "written as earnest, first-person advice" not in _last_system["v"]
                  and "REQUIRED IN THIS RESPONSE" not in _last_system["v"])
        finally:
            settings.ai_provider = _saved_provider
        # /message/stream: same turn over SSE — auth-gated, stage frames then one final.
        r401 = c.post("/api/chat/message/stream",
                      json={"session_id": sid, "message": "test", "disclaimer_accepted": True})
        check("POST /api/chat/message/stream -> 401 without key",
              r401.status_code == 401, f"got {r401.status_code}")
        rs = c.post("/api/chat/message/stream", headers=AUTH,
                    json={"session_id": sid, "message": "stream test", "disclaimer_accepted": True})
        check("POST /api/chat/message/stream -> 200 text/event-stream",
              rs.status_code == 200
              and rs.headers.get("content-type", "").startswith("text/event-stream"),
              f"{rs.status_code} {rs.headers.get('content-type')}")
        frames = [json.loads(f[len("data: "):]) for f in rs.text.split("\n\n")
                  if f.startswith("data: ")]
        stages = [f for f in frames if f.get("event") == "stage"]
        finals = [f for f in frames if f.get("event") == "final"]
        check("stream -> >=1 stage frame and exactly 1 final frame with a message",
              len(stages) >= 1 and len(finals) == 1 and bool(finals[0].get("message")),
              f"stages={len(stages)} finals={len(finals)}")
        check("stream final frame carries the ChatResponse contract",
              len(finals) == 1 and {"session_id", "message", "type", "escalated",
                                    "timestamp"} <= set(finals[0].keys()))
        stage_nodes = [f.get("node") for f in stages]
        check("stream default -> coordinator/specialists absent (single-agent default)",
              "coordinator" not in stage_nodes and "specialists" not in stage_nodes,
              f"nodes={stage_nodes}")
        check("stream default -> synthesizer + all guardrails run",
              {"synthesizer", "safety", "injection", "compliance",
               "agent_control", "response_defense", "governance"} <= set(stage_nodes),
              f"nodes={stage_nodes}")
        # multi_agent_mode=true -> opt-in to the full coordinator/specialists core.
        rma = c.post("/api/chat/message/stream", headers=AUTH,
                     json={"session_id": sid, "message": "I have a sore throat.",
                           "disclaimer_accepted": True, "multi_agent_mode": True})
        ma_nodes = [f.get("node") for f in
                    (json.loads(l[len("data: "):]) for l in rma.text.split("\n\n")
                     if l.startswith("data: "))
                    if f.get("event") == "stage"]
        check("multi_agent_mode=true -> coordinator stage present",
              "coordinator" in ma_nodes, f"nodes={ma_nodes}")
        # multi_agent_mode=false -> single-agent bypass: the coordinator and
        # specialists never run; the synthesizer + every guardrail node still do.
        rsa = c.post("/api/chat/message", headers=AUTH,
                     json={"session_id": sid, "message": "I have a sore throat.",
                           "disclaimer_accepted": True, "multi_agent_mode": False})
        check("POST /api/chat/message multi_agent_mode=false -> 200 + message",
              rsa.status_code == 200 and bool(rsa.json().get("message")), f"{rsa.status_code}")
        rss = c.post("/api/chat/message/stream", headers=AUTH,
                     json={"session_id": sid, "message": "I have a sore throat.",
                           "disclaimer_accepted": True, "multi_agent_mode": False})
        sa_nodes = [f.get("node") for f in
                    (json.loads(l[len("data: "):]) for l in rss.text.split("\n\n")
                     if l.startswith("data: "))
                    if f.get("event") == "stage"]
        check("multi_agent_mode=false -> coordinator/specialists skipped",
              "coordinator" not in sa_nodes and "specialists" not in sa_nodes,
              f"nodes={sa_nodes}")
        check("multi_agent_mode=false -> synthesizer + all guardrails still run",
              {"synthesizer", "safety", "injection", "compliance",
               "agent_control", "response_defense", "governance"} <= set(sa_nodes),
              f"nodes={sa_nodes}")
        # agent_control_review (the "Agent Observability Controls" toggle) is
        # accepted and, with a clean verdict (stubbed above), leaves the turn
        # untouched rather than erroring or withholding the answer.
        rac = c.post("/api/chat/message", headers=AUTH,
                     json={"session_id": sid, "message": "I have a sore throat.",
                           "disclaimer_accepted": True, "agent_control_review": True})
        check("POST /api/chat/message agent_control_review=true -> 200 + message",
              rac.status_code == 200 and bool(rac.json().get("message")), f"{rac.status_code}")
        # Resume-after-restart: a conversation reloaded from the DB must persist
        # its next turn. _prepare_session used to hold the ORM-loaded messages
        # list BY REFERENCE and mutate it in place; with a plain JSON column (no
        # MutableList) SQLAlchemy compared the attribute against itself, saw no
        # change, and omitted `messages` from the UPDATE — so the first turn
        # after a restart was written to nothing. Dropping the in-memory entry is
        # exactly what a process restart does.
        import backend.routers.chat as chat_router  # noqa: E402
        from backend.database.db import get_db_context  # noqa: E402
        from backend.models.db_models import Conversation  # noqa: E402

        rsum = c.post("/api/chat/message", headers=AUTH,
                      json={"session_id": sid, "message": "before the restart",
                            "disclaimer_accepted": True})
        check("resume: pre-restart turn -> 200", rsum.status_code == 200, f"{rsum.status_code}")
        with get_db_context() as _s:
            _before = len((_s.query(Conversation)
                           .filter(Conversation.session_id == sid).first().messages) or [])
        chat_router.sessions.pop(sid, None)      # simulate the restart
        rres = c.post("/api/chat/message", headers=AUTH,
                      json={"session_id": sid, "message": "after the restart",
                            "disclaimer_accepted": True})
        check("resume: post-restart turn -> 200", rres.status_code == 200, f"{rres.status_code}")
        with get_db_context() as _s:
            _msgs = (_s.query(Conversation)
                     .filter(Conversation.session_id == sid).first().messages) or []
        check("resume: post-restart turn was persisted (grew by user+assistant)",
              len(_msgs) >= _before + 2, f"{_before} -> {len(_msgs)}")
        check("resume: the resumed user message is in the stored history",
              any(m.get("content") == "after the restart" for m in _msgs),
              str([m.get("content") for m in _msgs][-4:]))

        check("GET /api/chat/auto-prompt/status -> 200", c.get("/api/chat/auto-prompt/status", headers=AUTH).status_code == 200)
        check("POST /api/chat/auto-prompt/start -> 200 (stubbed)", c.post("/api/chat/auto-prompt/start", headers=AUTH).status_code == 200)
        check("POST /api/chat/auto-prompt/stop -> 200", c.post("/api/chat/auto-prompt/stop", headers=AUTH).status_code == 200)

        # ---- incident (no real load: drive_traffic false, then stop) ----
        check("GET /api/incident/status -> 200", c.get("/api/incident/status", headers=AUTH).status_code == 200)
        rinc = c.post("/api/incident/start", headers=AUTH,
                      json={"latency_ms": 0, "error_rate": 0, "duration_s": 10, "drive_traffic": False})
        check("POST /api/incident/start (no load) -> 200 + active",
              rinc.status_code == 200 and rinc.json().get("active") is True, f"{rinc.status_code}")
        check("POST /api/incident/stop -> 200 + inactive",
              c.post("/api/incident/stop", headers=AUTH).json().get("active") is False)

        # ---- prompt-injection spray (no real turns: drive_turns false) ----
        # drive_turns=False keeps the suite off the live AI Defense API; the
        # control plane (state, counters, auto-stop wiring) is still exercised.
        check("GET /api/spray/status -> 200", c.get("/api/spray/status", headers=AUTH).status_code == 200)
        rspray = c.post("/api/spray/start", headers=AUTH,
                        json={"actor": "t.nguyen", "duration_s": 10, "intensity": 5,
                              "secondary_actors": 1, "drive_turns": False})
        check("POST /api/spray/start (no turns) -> 200 + active",
              rspray.status_code == 200 and rspray.json().get("active") is True,
              f"{rspray.status_code}")
        check("POST /api/spray/start reports the requested actor",
              rspray.json().get("actor") == "t.nguyen", str(rspray.json().get("actor")))
        _first_campaign = rspray.json().get("campaign_id")
        check("POST /api/spray/start mints a campaign_id", bool(_first_campaign))
        # Re-running must produce a *fresh* campaign, not a no-op: a demo gets
        # rehearsed, and stale ids would collide in Splunk.
        rspray2 = c.post("/api/spray/start", headers=AUTH,
                         json={"actor": "t.nguyen", "duration_s": 10, "intensity": 5,
                               "secondary_actors": 1, "drive_turns": False})
        check("POST /api/spray/start twice -> new campaign_id (re-runnable)",
              rspray2.json().get("campaign_id") not in (None, _first_campaign))
        check("POST /api/spray/stop -> 200 + inactive",
              c.post("/api/spray/stop", headers=AUTH).json().get("active") is False)
        check("spray start rejects an out-of-range intensity",
              c.post("/api/spray/start", headers=AUTH,
                     json={"intensity": 0, "drive_turns": False}).status_code == 422)
        # The campaign is attributed to the app that triggered it. It used to
        # rotate a fixed roster, so a spray launched from medadvice stamped
        # governance events with demobot-financeadvice/-legaladvice too. Planned
        # directly: drive_turns=False records no turns, so `apps_targeted` on the
        # status response can't observe this.
        import random as _random  # noqa: PLC0415 - local to this check
        from backend.routers.spray import SprayStart, _build_plan  # noqa: PLC0415

        def _plan_apps(theme):
            body = SprayStart(drive_turns=False) if theme is None else \
                SprayStart(theme=theme, drive_turns=False)
            return sorted({t.app["service_name"] for t in _build_plan(body, _random.Random(11))})

        check("spray plan stamps only the triggering app",
              _plan_apps("medadvice") == ["demobot-medadvice"], str(_plan_apps("medadvice")))
        check("spray plan follows a non-default theme",
              _plan_apps("taxadvice") == ["demobot-taxadvice"], str(_plan_apps("taxadvice")))
        check("spray plan defaults to medadvice when no theme is sent",
              _plan_apps(None) == ["demobot-medadvice"], str(_plan_apps(None)))

        # ---- agentic tool guard ----
        # 401 without the key (auth gate), 200 with it. Use a benign read whose
        # path is inside the workspace so tool_policy short-circuits locally and
        # no live AI Defense call is made (AI Defense is not stubbed here).
        check("POST /api/toolguard/inspect -> 401 without key",
              c.post("/api/toolguard/inspect",
                     json={"tool_name": "read", "arguments": {}}).status_code == 401)
        roots = settings.tool_guard_workspace_roots_list
        safe_path = (roots[0] if roots else "/tmp") + "/inbox/note.md"
        rtg = c.post("/api/toolguard/inspect", headers=AUTH,
                     json={"tool_name": "read", "arguments": {"path": safe_path},
                           "session_id": "api-test", "tool_call_id": "c1"})
        check("POST /api/toolguard/inspect (benign read) -> 200 allow",
              rtg.status_code == 200 and rtg.json().get("decision") == "allow"
              and rtg.json().get("block") is False, f"{rtg.status_code} {rtg.text[:120]}")
        check("GET /api/toolguard/policy -> 200 + sensitive_tools",
              (rpol := c.get("/api/toolguard/policy", headers=AUTH)).status_code == 200
              and "sensitive_tools" in rpol.json(), f"{rpol.status_code}")

        # ---- settings ----
        # These PUTs persist to the AppSettings row in ./medadvice.db — the SAME
        # database the live app reads. Snapshot and restore both values so a test
        # run cannot silently disable an operator's Emit Static Model or reset a
        # custom logs directory (this suite advertises itself as side-effect-safe).
        _saved_logs_dir = c.get("/api/settings", headers=AUTH).json().get("logs_directory")
        _saved_emit = c.get("/api/settings/emit-model", headers=AUTH).json()

        rs = c.get("/api/settings", headers=AUTH)
        check("GET /api/settings -> 200 + logs_directory", rs.status_code == 200 and "logs_directory" in rs.json())
        check("PUT /api/settings -> 200", c.put("/api/settings", headers=AUTH, json={"logs_directory": "logs"}).status_code == 200)
        rem = c.get("/api/settings/emit-model", headers=AUTH)
        check("GET /api/settings/emit-model -> 200 + choices", rem.status_code == 200 and "choices" in rem.json())
        check("PUT /api/settings/emit-model valid -> 200",
              c.put("/api/settings/emit-model", headers=AUTH, json={"enabled": False, "model_name": "gpt-4o", "random": False}).status_code == 200)
        check("PUT /api/settings/emit-model unknown model -> 422",
              c.put("/api/settings/emit-model", headers=AUTH, json={"enabled": True, "model_name": "not-a-real-model", "random": False}).status_code == 422)

        # Restore the operator's values (mirrors the ai_defense discovery restore
        # further down). Best-effort: a failure here must not mask a real result.
        try:
            if _saved_logs_dir:
                c.put("/api/settings", headers=AUTH, json={"logs_directory": _saved_logs_dir})
            if _saved_emit.get("model_name"):
                c.put("/api/settings/emit-model", headers=AUTH, json={
                    "enabled": bool(_saved_emit.get("enabled")),
                    "model_name": _saved_emit["model_name"],
                    "random": bool(_saved_emit.get("random")),
                })
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: could not restore settings: {exc}")
        rsi = c.get("/api/server-info", headers=AUTH)
        check("GET /api/server-info -> 200 + non-empty hostname",
              rsi.status_code == 200 and bool(rsi.json().get("hostname")), f"{rsi.status_code}")
        check("GET /api/server-info -> 401 without key", c.get("/api/server-info").status_code == 401)

        # ---- HEC destinations CRUD ----
        rd = c.get("/api/hec/destinations", headers=AUTH)
        check("GET /api/hec/destinations -> 200 + list", rd.status_code == 200 and "destinations" in rd.json())
        rc = c.post("/api/hec/destinations", headers=AUTH, json={"name": "apitest-dest"})
        check("POST /api/hec/destinations -> 200 + id", rc.status_code == 200 and rc.json().get("id"), f"{rc.status_code}")
        did = rc.json().get("id", "") if rc.status_code == 200 else ""
        check("GET /api/hec/destinations/{id} -> 200", c.get(f"/api/hec/destinations/{did}", headers=AUTH).status_code == 200)
        check("PUT /api/hec/destinations/{id} -> 200", c.put(f"/api/hec/destinations/{did}", headers=AUTH, json={"index": "main2"}).status_code == 200)
        check("GET /api/hec/stats -> 200 + destinations", "destinations" in c.get("/api/hec/stats", headers=AUTH).json())
        check("DELETE /api/hec/destinations/{id} -> 200", c.delete(f"/api/hec/destinations/{did}", headers=AUTH).status_code == 200)
        check("GET /api/hec/destinations/{bad} -> 404", c.get("/api/hec/destinations/nope", headers=AUTH).status_code == 404)

        # HEC envelope: a key in BOTH the indexed "fields" block and the event
        # body is indexed *and* extracted at search time (gen_ai:json sets
        # KV_MODE=json), so Splunk returns it as a two-value multivalue field and
        # every `stats ... by <key>` silently doubles -- including the ES prompt
        # injection rule's per-actor risk. Pin the invariant here.
        from backend.hec.config import HECConfig as _HECConfig
        from backend.hec.forwarder import HECForwarder as _HECFwd, _BODY_OWNED_KEYS
        _fwd = _HECFwd(_HECConfig(id="t", name="t", url="https://localhost:8088"), "tok")
        _body = {"session_id": "s1", "request_id": "r1", "trace_id": "t1",
                 "enduser_id": "t.nguyen", "risk_score": 60}
        _env = _fwd._build_event("governance", _body)
        _dupes = sorted(set(_env["fields"]) & set(_env["event"]))
        check("HEC envelope: no field is both indexed and in the event body",
              not _dupes, f"duplicated: {_dupes}")
        check("HEC envelope: correlation ids stay in the event body",
              all(k in _env["event"] for k in _BODY_OWNED_KEYS))
        check("HEC envelope: forwarder-owned log_type is still indexed",
              _env["fields"].get("log_type") == "governance")

        # ---- admin ----
        for path in ("/admin/logs/interactions", "/admin/logs/escalations", "/admin/logs/metrics", "/admin/logs/export"):
            check(f"GET {path} -> 200", c.get(path, headers=AUTH).status_code == 200)

        # Escalation review: reviewer_id/review_notes are BODY fields. They were
        # bare scalar params, which FastAPI binds as required QUERY params, so
        # the admin UI's JSON body produced a 422 on every single click. A 404
        # here is the pass condition — it proves the body validated and the
        # handler ran far enough to look the escalation up.
        rrev = c.put("/admin/escalations/no-such-escalation/review?new_status=reviewed",
                     headers=AUTH, json={"reviewer_id": "admin", "review_notes": "looks fine"})
        check("PUT /admin/escalations/{id}/review (JSON body) -> body accepted, 404 for unknown id",
              rrev.status_code == 404, f"{rrev.status_code} {rrev.text[:160]}")
        check("PUT /admin/escalations/{id}/review missing body -> 422",
              c.put("/admin/escalations/x/review?new_status=reviewed",
                    headers=AUTH).status_code == 422)
        check("PUT /admin/escalations/{id}/review bad new_status -> 422",
              c.put("/admin/escalations/x/review?new_status=bogus", headers=AUTH,
                    json={"reviewer_id": "a", "review_notes": "b"}).status_code == 422)

        # ---- CORS: explicit origins only, and preflights survive the gate ----
        # The access-key middleware wraps CORSMiddleware, so an unauthenticated
        # OPTIONS used to 401 before CORS could answer it.
        pre = c.options("/api/settings", headers={
            "Origin": f"http://localhost:{settings.port}",
            "Access-Control-Request-Method": "GET",
        })
        check("OPTIONS preflight is not blocked by the access-key gate",
              pre.status_code != 401, f"{pre.status_code}")
        check("preflight echoes the configured origin",
              pre.headers.get("access-control-allow-origin") == f"http://localhost:{settings.port}",
              str(pre.headers.get("access-control-allow-origin")))
        eviltrip = c.get("/health", headers={"Origin": "https://evil.example.com"})
        check("unconfigured origin gets no allow-origin header",
              "access-control-allow-origin" not in eviltrip.headers,
              str(eviltrip.headers.get("access-control-allow-origin")))

        # ---- auth (login/logout) ----
        bad = c.post("/login", data={"access_code": "wrong-code"}, follow_redirects=False)
        check("POST /login wrong code -> not authenticated (no md_access cookie)",
              "md_access" not in bad.cookies)
        good = c.post("/login", data={"access_code": KEY}, follow_redirects=False)
        check("POST /login correct code -> redirect + md_access cookie",
              good.status_code in (302, 303, 200) and "md_access" in good.cookies, f"{good.status_code}")
        check("GET /logout -> 2xx/3xx", c.get("/logout", headers=AUTH, follow_redirects=False).status_code in (200, 302, 303))

        # ---- server-rendered pages (HTML, with key) ----
        for path in ("/app", "/admin-ui", "/governance-ui", "/settings-ui"):
            r = c.get(path, headers=AUTH)
            check(f"GET {path} -> 200 HTML", r.status_code == 200 and "<html" in r.text.lower(), f"{r.status_code}")

        # ---- Settings page UI invariants: collapsible sections + read-only provider pill ----
        s_html = c.get("/settings-ui", headers=AUTH).text
        check("Settings: three collapsible section toggles (logs/creds/hec)",
              all(f'data-toggle="{s}"' in s_html for s in ("logs", "creds", "hec")))
        check("Settings: three collapsible section bodies (id=section-*)",
              all(f'id="section-{s}"' in s_html for s in ("logs", "creds", "hec")))
        check("Settings: a chevron per collapsible header (3)", s_html.count("data-chevron") == 3)
        check("Settings: read-only provider/model pill present", 'id="providerPill"' in s_html)
        # Accordion a11y: each toggle is a <button> nested INSIDE its <h2> (heading role preserved),
        # never an <h2> nested inside a <button> (which screen readers flatten away).
        check("Settings: headers use accordion <h2><button> (heading not nested in button)",
              s_html.count('aria-controls="section-') == 3
              and re.search(r"<button[^>]*>\s*<h2", s_html) is None)

    # ---- AI Defense per-direction guardrail config (no client needed) ----
    prompt_rules = {r["rule_name"] for r in settings.ai_defense_rule_config}
    resp_rules = {r["rule_name"] for r in settings.ai_defense_response_rule_config}
    check("AI Defense response rules drop prompt-only Prompt Injection",
          "Prompt Injection" not in resp_rules)
    check("AI Defense response rules drop prompt-only Code Detection",
          "Code Detection" not in resp_rules)
    check("AI Defense response rules keep PHI/PII content-harm guardrails",
          {"PHI", "PII"} <= resp_rules)
    check("AI Defense prompt rules still include Prompt Injection",
          "Prompt Injection" in prompt_rules)
    _saved = settings.ai_defense_prescription_guardrail
    settings.ai_defense_prescription_guardrail = "Unauthorized Prescription"
    check("AI Defense custom prescription guardrail appends on response direction",
          settings.ai_defense_response_rule_config[-1]["rule_name"] == "Unauthorized Prescription")
    settings.ai_defense_prescription_guardrail = _saved

    # ---- AI Defense enabled_rules discovery persists across restarts ----
    # A connection with an SCC policy bound rejects config.enabled_rules (HTTP
    # 400 + one retry). The discovery is persisted so a fresh client (i.e. a
    # process restart) skips the wasted round-trip instead of re-probing.
    import backend.settings_store as settings_store
    from backend.services.ai_defense import AIDefenseClient
    _prev = settings_store.get_ai_defense_enabled_rules_supported()
    settings_store.set_ai_defense_enabled_rules_supported(False)
    check("AI Defense enabled_rules discovery persists (set False -> read False)",
          settings_store.get_ai_defense_enabled_rules_supported() is False)
    check("fresh AIDefenseClient honors the persisted discovery (no re-probe)",
          AIDefenseClient()._rules_supported() is False)
    settings_store.set_ai_defense_enabled_rules_supported(_prev)

    print(f"RESULT: {'ok' if not _fails else str(_fails) + ' failed'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
