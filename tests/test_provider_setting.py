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
    check("AI_PROVIDER_CHOICES == the 4 supported providers",
          settings_store.AI_PROVIDER_CHOICES == ["anthropic", "bedrock", "openai", "ollama"])
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


def main() -> int:
    for fn in (
        test_choices_and_get_shape,
        test_apply_mutates_settings_and_clears_caches,
        test_unknown_provider_rejected,
        test_provider_fields_no_secret_leak,
        test_secret_apply_mask_and_blank_keep,
        test_creds_only_never_switches_active_provider,
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
