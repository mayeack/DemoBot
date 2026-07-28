#!/usr/bin/env python3
"""Regression: the runtime AI-provider Settings control (settings_store + UI API).

Guards the Settings-page control that switches which LLM backs the chat at runtime
(GET/PUT /api/settings/ai-provider). The risky parts are (1) applying the choice to
the live `settings` singleton AND clearing the LLM client caches so the swap takes
effect on the next turn, and (2) rejecting unknown providers. DB-free: exercises the
pure apply/validate paths without writing to the persisted store.

Run:  venv/bin/python tests/test_provider_setting.py    # exit 0 = pass
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import settings_store  # noqa: E402
from backend.agents import llm  # noqa: E402
from backend.config import settings  # noqa: E402

_fails = 0


def check(name: str, cond: bool) -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails += 1


def test_choices_and_get_shape() -> None:
    check("AI_PROVIDER_CHOICES == the 5 supported providers",
          settings_store.AI_PROVIDER_CHOICES == ["anthropic", "bedrock", "openai", "ollama", "nvidia"])
    d = settings_store.get_ai_provider()
    check("get_ai_provider exposes provider/model/choices/models",
          all(k in d for k in ("provider", "model", "choices", "models")))
    check("models map covers every provider",
          set(d["models"].keys()) == set(settings_store.AI_PROVIDER_CHOICES))
    check("current provider's model mirrors its models-map entry",
          d["model"] == d["models"].get(d["provider"], ""))


def test_apply_mutates_settings_and_clears_caches() -> None:
    orig_provider = settings.ai_provider
    orig_model = settings.anthropic_model
    # Seed the caches so we can prove they get dropped.
    llm._MODEL_CACHE["sentinel"] = object()
    llm._AGENT_CACHE["sentinel"] = object()
    try:
        settings_store._apply_ai_provider("anthropic", "unit-test-model")
        check("apply sets settings.ai_provider", settings.ai_provider == "anthropic")
        check("apply sets the provider's model field", settings.anthropic_model == "unit-test-model")
        check("apply clears _MODEL_CACHE", llm._MODEL_CACHE == {})
        check("apply clears _AGENT_CACHE", llm._AGENT_CACHE == {})
    finally:
        settings.ai_provider = orig_provider
        settings.anthropic_model = orig_model
        llm.clear_caches()


def test_unknown_provider_rejected() -> None:
    raised = False
    try:
        settings_store.set_ai_provider("gpt5-turbo-ultra")
    except ValueError:
        raised = True
    check("set_ai_provider rejects an unknown provider (before any DB write)", raised)


def test_provider_fields_no_secret_leak() -> None:
    """The security guarantee: secret field metadata never carries a value."""
    f = settings_store.get_provider_fields()
    leaks = [(p, it["key"]) for p, items in f.items() for it in items
             if it.get("secret") and "value" in it]
    check("secret fields never expose a value (presence only)", not leaks)
    check("anthropic surfaces a secret api_key field",
          any(it["key"] == "api_key" and it["secret"] for it in f["anthropic"]))
    check("non-secret fields (ollama base_url) do carry a value",
          any(it["key"] == "base_url" and "value" in it for it in f["ollama"]))
    check("nvidia surfaces a secret api_key field",
          any(it["key"] == "api_key" and it["secret"] for it in f["nvidia"]))
    check("nvidia base_url is non-secret and carries a value",
          any(it["key"] == "base_url" and "value" in it for it in f["nvidia"]))


def test_secret_apply_mask_and_blank_keep() -> None:
    """Apply a secret -> mutates settings + persists + masks; blank -> keeps it.
    DB-safe: load/_persist are stubbed to an in-memory dict (no real write)."""
    from backend.config import settings

    mem = {"ai_provider_creds": {}}
    orig_load, orig_persist = settings_store.load, settings_store._persist
    orig_key = settings.openai_api_key
    try:
        settings_store.load = lambda: {k: dict(v) if isinstance(v, dict) else v for k, v in mem.items()}
        def _fake_persist(data):
            mem.clear(); mem.update(data)
        settings_store._persist = _fake_persist

        settings_store.set_provider_creds("openai", {"api_key": "sk-unit-secret"})
        check("secret applied to live settings", settings.openai_api_key == "sk-unit-secret")
        check("secret persisted to the store blob",
              mem["ai_provider_creds"]["openai"]["api_key"] == "sk-unit-secret")
        oi = [it for it in settings_store.get_provider_fields()["openai"] if it["key"] == "api_key"][0]
        check("applied secret reads back masked (present, no value)",
              oi["present"] is True and "value" not in oi)

        settings_store.set_provider_creds("openai", {"api_key": ""})
        check("blank secret keeps the existing value (never wipes)",
              settings.openai_api_key == "sk-unit-secret")
    finally:
        settings_store.load, settings_store._persist = orig_load, orig_persist
        settings.openai_api_key = orig_key


def test_creds_only_never_switches_active_provider() -> None:
    """The /api/settings/provider-creds guarantee: saving one provider's creds
    must NOT change which provider backs the chat (unlike set_ai_provider). Lets
    the Settings creds section update keys for any provider without hijacking the
    active one chosen from the /app header. DB-safe: store I/O stubbed in-memory."""
    from backend.config import settings

    mem = {"ai_provider_creds": {}}
    orig_load, orig_persist = settings_store.load, settings_store._persist
    orig_provider, orig_key = settings.ai_provider, settings.openai_api_key
    try:
        settings.ai_provider = "ollama"  # active provider = ollama
        settings_store.load = lambda: {k: dict(v) if isinstance(v, dict) else v for k, v in mem.items()}
        def _fake_persist(data):
            mem.clear(); mem.update(data)
        settings_store._persist = _fake_persist

        settings_store.set_provider_creds("openai", {"api_key": "sk-creds-only"})
        check("creds-only applies the secret to the named provider",
              settings.openai_api_key == "sk-creds-only")
        check("creds-only leaves the active provider untouched",
              settings.ai_provider == "ollama")
    finally:
        settings_store.load, settings_store._persist = orig_load, orig_persist
        settings.ai_provider, settings.openai_api_key = orig_provider, orig_key


def _capture_blocked_events():
    """Run every blocked-turn governance emitter, returning their log_response kwargs.

    Stubs governance_logger.log_response (and log_escalation, which the policy
    node also calls) so nothing is written; no LLM or AI Defense call is made."""
    from backend.agents.nodes import policy as policy_node
    from backend.agents.nodes.shared import content_engine
    from backend.logging import governance_logger as gl
    from backend.services.ai_defense import InspectionResult

    captured = {}
    orig_log, orig_esc = gl.governance_logger.log_response, gl.governance_logger.log_escalation
    try:
        gl.governance_logger.log_escalation = lambda **kw: None
        inspection = InspectionResult(is_safe=False, severity="HIGH", event_id="evt-1")

        gl.governance_logger.log_response = lambda **kw: captured.__setitem__("prompt_block", kw)
        content_engine._handle_ai_defense_block(
            session_id="S1", request_id="R1", trace_id="T1", user_message="hi",
            conversation_history=[], inspection=inspection, start_time=0.0,
            client_address=None, enduser_id=None,
        )

        # Response block, graph caller: the model already answered, so the real
        # model id and token usage are passed through.
        gl.governance_logger.log_response = lambda **kw: captured.__setitem__("response_block", kw)
        content_engine._handle_ai_defense_response_block(
            session_id="S1", request_id="R1", trace_id="T1", conversation_messages=[],
            inspection=inspection, start_time=0.0, client_address=None, enduser_id=None,
            llm_model="real-model-that-answered",
            usage_data={"usage_input_tokens": 11, "usage_output_tokens": 7,
                        "usage_total_tokens": 18},
        )

        # Response block, legacy caller: no model/usage on hand -> fallbacks.
        gl.governance_logger.log_response = lambda **kw: captured.__setitem__("response_block_legacy", kw)
        content_engine._handle_ai_defense_response_block(
            session_id="S1", request_id="R1", trace_id="T1", conversation_messages=[],
            inspection=inspection, start_time=0.0, client_address=None, enduser_id=None,
        )
    finally:
        gl.governance_logger.log_response = orig_log
        gl.governance_logger.log_escalation = orig_esc

    captured["policy_node_model"] = policy_node._response_model()
    return captured


def test_blocked_events_report_the_active_provider_model() -> None:
    """Blocked turns must not be misattributed to another provider's model.

    These paths never see a real ``response.model`` (nothing was called, or the
    answer was withheld), and they used to hardcode ``settings.anthropic_model``
    -- so on Ollama every blocked event landed in Splunk as a Claude turn while
    ``provider_name``/``request_model`` said ollama. Executive fields and Galileo
    both prefer ``response_model``, so the wrong value won."""
    from backend.logging.governance_logger import active_response_model
    from backend.model_emitter import model_emitter

    orig_provider, orig_ollama = settings.ai_provider, settings.ollama_model
    orig_enabled = model_emitter.enabled
    try:
        model_emitter.enabled = False  # deterministic: no demo name override
        settings.ai_provider, settings.ollama_model = "ollama", "dolphin3:8b"

        check("active_response_model follows the active provider",
              active_response_model() == "dolphin3:8b")
        check("active_response_model is not the anthropic default",
              active_response_model() != settings.anthropic_model)

        ev = _capture_blocked_events()
        check("policy block node reports the ollama model",
              ev["policy_node_model"] == "dolphin3:8b")
        check("AI Defense prompt block reports the ollama model",
              ev["prompt_block"]["response_model"] == "dolphin3:8b")
        check("AI Defense response block echoes the model that actually answered",
              ev["response_block"]["response_model"] == "real-model-that-answered")
        check("AI Defense response block falls back to the active model",
              ev["response_block_legacy"]["response_model"] == "dolphin3:8b")

        # Token spend: the response block happens *after* the call completed.
        check("response block reports the real token usage",
              ev["response_block"]["usage_data"]["usage_total_tokens"] == 18)
        check("response block without usage still defaults to zeros",
              ev["response_block_legacy"]["usage_data"]["usage_total_tokens"] == 0)
        # The prompt block genuinely precedes any model call -> zeros are correct.
        check("prompt block reports zero tokens (nothing was called)",
              ev["prompt_block"]["usage_data"]["usage_total_tokens"] == 0)

        # Switching providers must move the reported model with it.
        settings.ai_provider = "anthropic"
        check("switching to anthropic moves the blocked-event model",
              active_response_model() == settings.anthropic_model)
    finally:
        settings.ai_provider, settings.ollama_model = orig_provider, orig_ollama
        model_emitter.enabled = orig_enabled


def test_model_emitter_override_reaches_blocked_events() -> None:
    """The demo model-name override applies to request_model; blocked events now
    route response_model through the same helper, so it can no longer disagree."""
    from backend.logging.governance_logger import active_response_model
    from backend.model_emitter import model_emitter

    orig_provider = settings.ai_provider
    orig_enabled, orig_name, orig_rand = (
        model_emitter.enabled, model_emitter.model_name, model_emitter.random)
    try:
        settings.ai_provider = "ollama"
        model_emitter.configure(enabled=True, model_name="gpt-4o-mini", random_emit=False)
        check("blocked events honor the demo model-name override",
              active_response_model() == "gpt-4o-mini")
    finally:
        settings.ai_provider = orig_provider
        model_emitter.enabled, model_emitter.model_name, model_emitter.random = (
            orig_enabled, orig_name, orig_rand)


def main() -> int:
    for fn in (
        test_choices_and_get_shape,
        test_apply_mutates_settings_and_clears_caches,
        test_unknown_provider_rejected,
        test_provider_fields_no_secret_leak,
        test_secret_apply_mask_and_blank_keep,
        test_creds_only_never_switches_active_provider,
        test_blocked_events_report_the_active_provider_model,
        test_model_emitter_override_reaches_blocked_events,
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
