#!/usr/bin/env python3
"""Regression: the local NVIDIA NIM provider (AI_PROVIDER=nvidia).

provider=nvidia is LOCAL inference only — a NIM container on this host, never the
hosted API catalog. This guards the contract and the Nemotron 3 handling:

  - the loopback rule: any non-loopback NIM URL is rejected at every seam (chat
    model factory, legacy client factory, Settings store), with the reason;
  - get_chat_model builds a ChatOpenAI against the local NIM with a placeholder
    key, NVIDIA's top_p, and Nemotron 3 reasoning OFF unless enabled (the models
    default it ON and would wrap the JSON answer contract in a trace);
  - a stray inline <think>…</think> trace is stripped before parsing;
  - the catalog lists the featured NIM images first (Nemotron 3 Super stays a
    selectable option before a NIM is up) plus whatever the NIM serves, and the
    provider status the Settings page renders carries the GPU requirements;
  - the Settings fields expose base_url / optional api_key / reasoning, and a
    remote URL saved through them is a ValueError (-> 422), nothing applied.

Offline: every network probe is stubbed. Run:
    venv/bin/python tests/test_nvidia_provider.py    # exit 0 = pass
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from backend import model_catalog, nvidia_nim, settings_store  # noqa: E402
from backend.agents import llm  # noqa: E402
from backend.agents.llm import ChatModelError, _extract_text, get_chat_model  # noqa: E402

_fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _fails += 1


class _Stub:
    """Minimal stand-in for backend.config.settings (only fields the code reads)."""

    ai_provider = "nvidia"
    nvidia_base_url = "http://localhost:8000/v1"
    nvidia_model = "nvidia/nvidia-nemotron-nano-9b-v2"
    nvidia_api_key = ""
    nvidia_reasoning = False
    nvidia_top_p = 0.95
    nvidia_featured_models = (
        "nvidia/nvidia-nemotron-nano-9b-v2|1x A10G / L4 24 GB|22000|1,"
        "nvidia/nemotron-3-super-120b-a12b|8x H100-80GB|76000|8"
    )
    # Fields the provider-fallback ternaries reference for other providers.
    anthropic_model = "claude-sonnet-4-5-20250929"
    bedrock_model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    openai_model = "gpt-4o"
    ollama_model = "mistral-nemo:12b"


def test_loopback_rule() -> None:
    for url in ("http://localhost:8000/v1", "http://127.0.0.1:8000/v1",
                "http://[::1]:8000/v1", "http://127.0.0.2:9000"):
        check(f"loopback accepted: {url}", nvidia_nim.is_loopback_url(url))
    for url in ("https://integrate.api.nvidia.com/v1", "http://10.0.0.5:8000/v1",
                "http://nim-service.local:8000/v1", "", "not a url"):
        check(f"non-loopback rejected: {url!r}", not nvidia_nim.is_loopback_url(url))
    check("validate normalizes a bare server root to /v1",
          nvidia_nim.validate_nim_base_url("http://localhost:8000/") == "http://localhost:8000/v1")
    try:
        nvidia_nim.validate_nim_base_url("https://integrate.api.nvidia.com/v1")
        check("validate rejects the hosted catalog URL", False)
    except ValueError as exc:
        check("validate rejects the hosted catalog URL", "local inference only" in str(exc))


def test_get_chat_model_local_nim() -> None:
    llm._MODEL_CACHE.clear()
    model = get_chat_model(_Stub(), max_tokens=1024, temperature=0.7)
    check("get_chat_model(nvidia) -> ChatOpenAI", type(model).__name__ == "ChatOpenAI")
    check("model id == settings.nvidia_model", model.model_name == _Stub.nvidia_model)
    check("base_url is the local NIM", model.openai_api_base == "http://localhost:8000/v1")
    check("empty api key -> placeholder (SDK refuses empty)",
          model.openai_api_key.get_secret_value() == nvidia_nim.PLACEHOLDER_API_KEY)
    check("NVIDIA top_p applied", model.top_p == 0.95)
    eb = model.extra_body or {}
    check("reasoning OFF by default (enable_thinking=False)",
          eb.get("chat_template_kwargs", {}).get("enable_thinking") is False, str(eb))

    class _Think(_Stub):
        nvidia_reasoning = True

    thinking = get_chat_model(_Think(), max_tokens=1024, temperature=0.7)
    check("reasoning ON when enabled",
          (thinking.extra_body or {}).get("chat_template_kwargs", {}).get("enable_thinking") is True)
    check("reasoning flag is part of the cache key", thinking is not model)

    class _Remote(_Stub):
        nvidia_base_url = "https://integrate.api.nvidia.com/v1"

    llm._MODEL_CACHE.clear()
    try:
        get_chat_model(_Remote(), max_tokens=1024, temperature=0.7)
        check("chat model factory rejects a remote NIM URL", False)
    except ChatModelError as exc:
        check("chat model factory rejects a remote NIM URL", "local inference only" in str(exc))
    llm._MODEL_CACHE.clear()


def test_think_trace_stripped() -> None:
    msg = AIMessage(content="<think>\nreasoning here\n</think>\n{\"severity\": \"LOW\"}")
    check("leading <think> block stripped", _extract_text(msg) == '{"severity": "LOW"}')
    msg2 = AIMessage(content='{"severity": "LOW"}')
    check("answer without a trace untouched", _extract_text(msg2) == '{"severity": "LOW"}')
    msg3 = AIMessage(content=[{"type": "text", "text": "<think>x</think>ok"}])
    check("list content also stripped", _extract_text(msg3) == "ok")


def test_catalog_featured_first_and_status() -> None:
    orig_status = nvidia_nim.status
    orig_available = dict(model_catalog._AVAILABLE)
    try:
        nvidia_nim.status = lambda settings, probe=True: {  # type: ignore[assignment]
            "base_url": settings.nvidia_base_url, "local_only": True, "url_error": None,
            "ready": True, "served": ["nvidia/nvidia-nemotron-nano-9b-v2"],
            "featured": [
                {"id": "nvidia/nvidia-nemotron-nano-9b-v2", "gpu": "1x A10G / L4 24 GB",
                 "min_vram_mb": 24000, "gpus": 1, "served": True},
                {"id": "nvidia/nemotron-3-super-120b-a12b", "gpu": "8x H100-80GB",
                 "min_vram_mb": 640000, "gpus": 8, "served": False},
            ],
            "reasoning": False,
        }
        models = model_catalog._nvidia_models(_Stub())
        check("featured images come first, deduped against the served list",
              models == ["nvidia/nvidia-nemotron-nano-9b-v2", "nvidia/nemotron-3-super-120b-a12b"],
              str(models))
        st = model_catalog.nvidia_status()
        check("nvidia_status caches the probe for the Settings payload",
              st.get("ready") is True and st.get("local_only") is True)
        check("featured entries carry GPU requirements",
              any(f["gpu"] == "8x H100-80GB" and f["served"] is False for f in st["featured"]))

        nvidia_nim.status = lambda settings, probe=True: {  # type: ignore[assignment]
            "base_url": "https://integrate.api.nvidia.com/v1", "local_only": True,
            "url_error": "not on this host", "ready": False, "served": [], "featured": [],
            "reasoning": False,
        }
        model_catalog._PROBES  # noqa: B018 - sanity that the registry still exists
        got = model_catalog.refresh_provider("nvidia")
        check("a remote URL makes the nvidia probe degrade to [] (best-effort)", got == [])
    finally:
        nvidia_nim.status = orig_status
        model_catalog._AVAILABLE.clear()
        model_catalog._AVAILABLE.update(orig_available)


def test_featured_parse_tolerates_junk() -> None:
    parsed = nvidia_nim.parse_featured_models(
        "a/b|1x A10G|24000|1, ,broken|label|notanumber, c/d"
    )
    ids = [p.id for p in parsed]
    check("malformed entries skipped, bare ids kept", ids == ["a/b", "c/d"], str(ids))
    check("defaults for a bare id", parsed[1].gpus == 1 and parsed[1].min_vram_mb == 0)


def test_legacy_client_local_only() -> None:
    from backend.services.ai_client import AIClientError, OpenAIClient, get_ai_client

    client = get_ai_client(_Stub())
    check("get_ai_client(nvidia) -> OpenAIClient", isinstance(client, OpenAIClient))
    check("legacy client targets the local NIM", getattr(client, "base_url", "") == "http://localhost:8000/v1")

    class _Remote(_Stub):
        nvidia_base_url = "http://10.0.0.5:8000/v1"

    try:
        get_ai_client(_Remote())
        check("legacy factory rejects a remote NIM URL", False)
    except AIClientError as exc:
        check("legacy factory rejects a remote NIM URL", "local inference only" in str(exc))


def test_settings_fields_and_remote_rejected() -> None:
    from backend.config import settings

    f = settings_store.get_provider_fields()["nvidia"]
    keys = [it["key"] for it in f]
    check("nvidia fields: base_url, api_key, reasoning", keys == ["base_url", "api_key", "reasoning"], str(keys))
    check("base_url is non-secret and carries a value", any(it["key"] == "base_url" and "value" in it for it in f))
    check("api_key stays secret (presence only)", any(it["key"] == "api_key" and it["secret"] and "value" not in it for it in f))
    check("reasoning is a boolean field", any(it["key"] == "reasoning" and it.get("boolean") for it in f))

    mem = {"ai_provider_creds": {}}
    orig_load, orig_persist = settings_store.load, settings_store._persist
    orig_url, orig_reason = settings.nvidia_base_url, settings.nvidia_reasoning
    try:
        settings_store.load = lambda: {k: dict(v) if isinstance(v, dict) else v for k, v in mem.items()}

        def _fake_persist(data):
            mem.clear(); mem.update(data)
        settings_store._persist = _fake_persist

        try:
            settings_store.set_provider_creds("nvidia", {"base_url": "https://integrate.api.nvidia.com/v1",
                                                         "reasoning": "true"})
            check("remote NIM URL rejected by the store", False)
        except ValueError:
            check("remote NIM URL rejected by the store", True)
        check("a rejected save applies NOTHING (reasoning untouched)",
              settings.nvidia_reasoning == orig_reason and settings.nvidia_base_url == orig_url)
        check("a rejected save persists nothing", mem["ai_provider_creds"] == {})

        settings_store.set_provider_creds("nvidia", {"base_url": "http://127.0.0.1:8000", "reasoning": "true"})
        check("loopback URL applied", settings.nvidia_base_url == "http://127.0.0.1:8000")
        check("boolean reasoning applied as a bool", settings.nvidia_reasoning is True)
        check("boolean persisted as 'true'", mem["ai_provider_creds"]["nvidia"]["reasoning"] == "true")

        # Startup re-apply: a stored remote URL (from before the local-only rule)
        # must not win over the config default.
        mem["ai_provider_creds"]["nvidia"]["base_url"] = "http://10.0.0.5:8000/v1"
        settings.nvidia_base_url = "http://localhost:8000/v1"
        settings_store.apply_provider_creds_from_store()
        check("startup ignores a stored remote URL", settings.nvidia_base_url == "http://localhost:8000/v1")
    finally:
        settings_store.load, settings_store._persist = orig_load, orig_persist
        settings.nvidia_base_url, settings.nvidia_reasoning = orig_url, orig_reason
        llm.clear_caches()


def test_request_model_and_provider_info() -> None:
    from backend.agents.nodes.agent_common import request_model
    from backend.config import settings
    from backend.logging import governance_logger as gl

    orig = settings.ai_provider
    try:
        settings.ai_provider = "nvidia"
        check("request_model('nvidia') is the NIM model", request_model("nvidia") == settings.nvidia_model)
        name, model = gl._active_provider_info()
        check("_active_provider_info reports nvidia + NIM model",
              name == "nvidia" and model == settings.nvidia_model)
    finally:
        settings.ai_provider = orig


def test_local_cost_is_zero() -> None:
    from backend.logging.executive_fields import _estimated_cost

    check("NIM inference is not priced like an API call",
          _estimated_cost("nvidia/nvidia-nemotron-nano-9b-v2", 1000, 1000) == 0.0)


def main() -> int:
    for fn in (
        test_loopback_rule,
        test_get_chat_model_local_nim,
        test_think_trace_stripped,
        test_catalog_featured_first_and_status,
        test_featured_parse_tolerates_junk,
        test_legacy_client_local_only,
        test_settings_fields_and_remote_rejected,
        test_request_model_and_provider_info,
        test_local_cost_is_zero,
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
