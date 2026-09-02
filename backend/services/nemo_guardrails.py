"""NVIDIA NeMo Guardrails client — the "NeMo Guardrails" drawer toggle.

Runs NeMo's rails IN-PROCESS (``nemoguardrails`` core, never the ``[server]``
extra) over the same message the other guardrails see:

  input rails   -> ``check_input(user_message)``   (after AI Defense prompt inspection)
  output rails  -> ``check_output(user, assistant)`` (after Galileo Agent Control,
                   before AI Defense response inspection — Cisco stays last)
  tool calls    -> ``check_tool_call(rendered)``    (the NemoClaw policy layer)

The judge for the built-in self-check rails is DemoBot's ACTIVE chat model,
injected via ``LLMRails(config, llm=get_chat_model(...))``, so the toggle works
on every provider without a cloud call. NemoGuard content safety is an optional
SECOND local NIM (``nemo_guardrails_content_safety_url``).

Verdict shape mirrors ``ai_defense.InspectionResult`` (``should_block`` honoring
the configured fail policy) so the graph nodes and block handler treat every
guardrail the same way. Rails are built lazily and rebuilt when the provider /
model / rail selection changes (``reconfigure`` is the analog of
``llm.clear_caches``); every failure degrades to an ``errored`` verdict.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.config import BASE_DIR, settings

logger = logging.getLogger(__name__)

RAILS_DIR = Path(BASE_DIR) / "guardrails" / "nemo"

# Rail selector token -> the NeMo flow it activates and which side it runs on.
_RAIL_FLOWS: Dict[str, Tuple[str, str]] = {
    "self_check_input": ("input", "self check input"),
    "self_check_output": ("output", "self check output"),
    "overreach": ("output", "self check output $variant=overreach"),
}
_CONTENT_SAFETY_FLOWS = {
    "input": "content safety check input $model=content_safety",
    "output": "content safety check output $model=content_safety",
}


@dataclass
class RailVerdict:
    """Normalized outcome of one rails evaluation."""

    is_safe: bool = True
    stage: str = "input"  # input | output | tool
    # Names of the rails that fired (e.g. "self check output $variant=overreach").
    rule_names: List[str] = field(default_factory=list)
    reason: Optional[str] = None
    # True when no real verdict could be produced (import/config/judge error).
    errored: bool = False
    error_message: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def should_block(self) -> bool:
        """A real unsafe verdict always blocks; an error honors fail-open/closed."""
        if self.errored:
            return not settings.nemo_guardrails_fail_open
        return not self.is_safe


class NemoGuardrailsError(Exception):
    """Raised for configuration problems (missing package / rails directory)."""


def selected_rails() -> List[str]:
    return [r.strip() for r in (settings.nemo_guardrails_rails or "").split(",") if r.strip()]


class NemoGuardrailsClient:
    """Lazy, cached ``LLMRails`` wrapper bound to the active chat model."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rails: Any = None
        self._rails_key: Optional[tuple] = None
        self._build_error: Optional[str] = None

    # ---------------------------------------------------------------- config
    def reconfigure(self) -> None:
        """Drop the built rails so the next check rebuilds against the current
        settings (provider/model switch, rail selection, content-safety URL).
        Called by settings_store on a Settings save and by llm.clear_caches."""
        with self._lock:
            self._rails = None
            self._rails_key = None
            self._build_error = None

    @property
    def is_configured(self) -> bool:
        return bool(settings.nemo_guardrails_enabled) and (RAILS_DIR / "config.yml").exists()

    @property
    def active_rails(self) -> List[str]:
        return [r for r in selected_rails() if r in _RAIL_FLOWS]

    def _config_key(self) -> tuple:
        from backend.logging.governance_logger import active_response_model

        return (
            settings.ai_provider,
            active_response_model(),
            tuple(self.active_rails),
            settings.nemo_guardrails_content_safety_url or "",
            settings.nemo_guardrails_content_safety_model,
        )

    # ----------------------------------------------------------------- build
    def _build_config(self):
        """RailsConfig from guardrails/nemo, with flows derived from settings."""
        from nemoguardrails import RailsConfig

        config = RailsConfig.from_path(str(RAILS_DIR))
        input_flows: List[str] = []
        output_flows: List[str] = []
        for rail in self.active_rails:
            side, flow = _RAIL_FLOWS[rail]
            (input_flows if side == "input" else output_flows).append(flow)

        cs_url = (settings.nemo_guardrails_content_safety_url or "").strip()
        if cs_url:
            from backend import nvidia_nim

            # Same local-only contract as the inference NIM.
            cs_url = nvidia_nim.validate_nim_base_url(cs_url)
            from nemoguardrails.rails.llm.config import Model

            config.models.append(
                Model(
                    type="content_safety",
                    engine="nim",
                    model=settings.nemo_guardrails_content_safety_model,
                    parameters={"base_url": cs_url},
                )
            )
            input_flows.append(_CONTENT_SAFETY_FLOWS["input"])
            output_flows.append(_CONTENT_SAFETY_FLOWS["output"])

        config.rails.input.flows = input_flows
        config.rails.output.flows = output_flows
        return config

    def _build_rails(self):
        from nemoguardrails import LLMRails

        # The judge: DemoBot's active chat model, deterministic and short.
        judge = _judge_model(int(settings.nemo_guardrails_judge_max_tokens or 64))
        config = self._build_config()
        try:
            return LLMRails(config, llm=judge)
        except Exception as exc:  # noqa: BLE001
            # The $variant overreach flow is the one non-standard piece; if this
            # nemoguardrails build rejects it, keep the standard rails rather
            # than losing the whole toggle.
            if "overreach" in str(exc).lower() or "variant" in str(exc).lower():
                logger.warning("NeMo rails: overreach variant unsupported (%s); loading without it", exc)
                config.rails.output.flows = [
                    f for f in config.rails.output.flows if "$variant=overreach" not in f
                ]
                return LLMRails(config, llm=judge)
            raise

    def _get_rails(self):
        key = self._config_key()
        with self._lock:
            if self._rails is not None and self._rails_key == key:
                return self._rails
            if self._build_error and self._rails_key == key:
                raise NemoGuardrailsError(self._build_error)
            try:
                self._rails = self._build_rails()
                self._rails_key = key
                self._build_error = None
                logger.info("NeMo Guardrails rails built: %s", ", ".join(self.active_rails) or "(none)")
                return self._rails
            except ImportError as exc:
                self._build_error = f"nemoguardrails is not installed: {exc}"
            except Exception as exc:  # noqa: BLE001
                self._build_error = f"NeMo rails config failed to load: {exc}"
            self._rails_key = key
            raise NemoGuardrailsError(self._build_error)

    # ----------------------------------------------------------------- checks
    @staticmethod
    def _run_check(rails, messages: List[Dict[str, str]], rail_types: List[str]) -> RailVerdict:
        """Evaluate rails without generating a reply.

        Prefers the 0.24 ``check()`` API (rails-only, returns a RailsResult);
        falls back to ``generate(options={"rails": [...]})`` on older builds.
        Runs on the graph's worker thread (no event loop), so the sync APIs are
        the right ones; a running-loop RuntimeError falls back to a fresh thread.
        """
        started = time.perf_counter()
        blocked = False
        names: List[str] = []
        reason: Optional[str] = None

        if hasattr(rails, "check"):
            # 0.24's check() wants RailType enum members, not "input"/"output".
            types = _rail_type_enums(rail_types)
            try:
                result = rails.check(messages=messages, rail_types=types)
            except RuntimeError as exc:
                if "async" not in str(exc).lower() and "event loop" not in str(exc).lower():
                    raise
                result = _run_in_fresh_thread(lambda: rails.check(messages=messages, rail_types=types))
            status = getattr(result, "status", None)
            status_name = getattr(status, "name", None) or str(status or "").upper()
            blocked = "BLOCKED" in status_name
            rail = getattr(result, "rail", None)
            if rail:
                names.append(str(getattr(rail, "name", rail)))
            content = getattr(result, "content", None)
            if blocked and content:
                reason = str(content)[:300]
        else:
            options = {"rails": rail_types, "log": {"activated_rails": True},
                       "output_vars": ["triggered_input_rail", "triggered_output_rail"]}
            try:
                response = rails.generate(messages=messages, options=options)
            except RuntimeError as exc:
                if "async" not in str(exc).lower() and "event loop" not in str(exc).lower():
                    raise
                response = _run_in_fresh_thread(lambda: rails.generate(messages=messages, options=options))
            log = getattr(response, "log", None)
            for rail in (getattr(log, "activated_rails", None) or []):
                if getattr(rail, "stop", False):
                    blocked = True
                    names.append(str(getattr(rail, "name", "")))
            out = getattr(response, "output_data", None) or {}
            if out.get("triggered_input_rail") or out.get("triggered_output_rail"):
                blocked = True
                names.extend(str(v) for v in (out.get("triggered_input_rail"), out.get("triggered_output_rail")) if v)

        return RailVerdict(
            is_safe=not blocked,
            stage=rail_types[0] if rail_types else "input",
            rule_names=list(dict.fromkeys(n for n in names if n)),
            reason=reason,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    def _check(self, messages: List[Dict[str, str]], rail_types: List[str], stage: str) -> RailVerdict:
        started = time.perf_counter()
        try:
            rails = self._get_rails()
            verdict = self._run_check(rails, messages, rail_types)
            verdict.stage = stage
            return verdict
        except Exception as exc:  # noqa: BLE001 - never let the judge break a turn
            logger.warning("NeMo Guardrails %s check errored: %s", stage, exc)
            return RailVerdict(
                is_safe=True, stage=stage, errored=True, error_message=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )

    def check_input(self, user_message: str) -> RailVerdict:
        return self._check([{"role": "user", "content": user_message}], ["input"], "input")

    def check_output(self, user_message: str, assistant_message: str) -> RailVerdict:
        # Output rails need the bot turn; the user turn is context for the judge.
        return self._check(
            [{"role": "user", "content": user_message or ""},
             {"role": "assistant", "content": assistant_message}],
            ["output"], "output",
        )

    def check_tool_call(self, rendered: str) -> RailVerdict:
        """Input rails over a rendered agent tool call (the NemoClaw seat)."""
        return self._check([{"role": "user", "content": rendered}], ["input"], "tool")


# Ollama sampling parameters live under the request's "options" object.
_OLLAMA_OPTION_KEYS = {
    "temperature", "top_p", "top_k", "seed", "num_predict", "num_ctx", "repeat_penalty",
    "repeat_last_n", "mirostat", "mirostat_eta", "mirostat_tau", "tfs_z", "num_gpu", "num_thread",
}
_KWARG_TO_OPTION = {"max_tokens": "num_predict", "max_new_tokens": "num_predict", "max_completion_tokens": "num_predict"}
_OLLAMA_REQUEST_KEYS = {"options", "format", "response_format", "stream", "model", "reasoning",
                        "logprobs", "top_logprobs", "keep_alive", "tools", "tool_choice", "strict"}


def _judge_model(max_tokens: int):
    """The rails' judge = DemoBot's active chat model, deterministic and short.

    NeMo's LangChain adapter passes ``temperature`` / ``max_tokens`` as
    per-call kwargs. Cloud chat models absorb those; ``ChatOllama`` spreads
    unknown kwargs into ``ollama.AsyncClient.chat()``, which rejects them
    (``unexpected keyword argument 'temperature'``). For Ollama the judge is
    therefore a subclass that folds sampling kwargs into the request's
    ``options`` (merged with the model's own defaults, since an explicit
    ``options`` replaces them) and drops anything Ollama does not accept.
    """
    from backend.agents.llm import get_chat_model

    if (settings.ai_provider or "").lower() != "ollama":
        return get_chat_model(settings, max_tokens=max_tokens, temperature=0.0)

    from langchain_ollama import ChatOllama

    class _JudgeOllama(ChatOllama):
        def _chat_params(self, messages, stop=None, **kwargs):  # type: ignore[override]
            options = dict(kwargs.pop("options", None) or {})
            for key in list(kwargs):
                opt = _KWARG_TO_OPTION.get(key, key)
                if opt in _OLLAMA_OPTION_KEYS:
                    options[opt] = kwargs.pop(key)
                elif key not in _OLLAMA_REQUEST_KEYS:
                    kwargs.pop(key)  # not an Ollama request field: drop it
            if options:
                base = {k: v for k, v in {
                    "num_ctx": self.num_ctx, "num_predict": self.num_predict,
                    "temperature": self.temperature, "top_p": self.top_p, "top_k": self.top_k,
                    "seed": self.seed, "repeat_penalty": self.repeat_penalty,
                }.items() if v is not None}
                base.update(options)
                kwargs["options"] = base
            return super()._chat_params(messages, stop, **kwargs)

    # Same construction as backend/agents/llm.py's ollama branch (see its notes
    # on num_predict / num_ctx / keep_alive), pinned to the judge's params.
    return _JudgeOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=0.0,
        num_predict=max_tokens,
        num_ctx=settings.ollama_num_ctx,
        keep_alive=settings.ollama_keep_alive,
    )


def _rail_type_enums(rail_types: List[str]) -> List[Any]:
    """Map "input"/"output" to nemoguardrails' RailType members (0.24's check()
    reads ``.value`` on them). Falls back to the strings when the enum is not
    importable (older builds, or the stubbed rails in tests)."""
    try:
        from nemoguardrails.rails.llm.options import RailType
    except Exception:  # noqa: BLE001
        return list(rail_types)
    out: List[Any] = []
    for rt in rail_types:
        if not isinstance(rt, str):
            out.append(rt)
            continue
        try:
            out.append(RailType(rt))
        except ValueError:
            out.append(getattr(RailType, rt.upper(), rt))
    return out


def _run_in_fresh_thread(fn):
    """Run ``fn`` on a thread with no event loop and return its result."""
    box: Dict[str, Any] = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_target, name="nemo-rails", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


# Module-level singleton (mirrors ai_defense_client / agent_control_client).
nemo_guardrails_client = NemoGuardrailsClient()
