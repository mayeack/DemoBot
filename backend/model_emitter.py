"""Demo control: emit a *static* (or random) model name in telemetry + governance
logs instead of the model actually used.

Splunk O11y Cloud computes GenAI cost server-side from a managed model-pricing
lookup (OpenAI/gpt models are priced; the current Claude models are not). This
lets a demo report a priced model name (e.g. ``gpt-4o-mini``) on the GenAI
telemetry + logs so the Cost KPI populates, while the app keeps calling the real
configured model. Module-level singleton (read on the hot path; no DB per call) —
configured from ``settings_store`` at startup and updated by the settings API.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List

# Common models, with the ones Splunk O11y Cloud prices (OpenAI) first so the
# Cost KPI lights up. The actual model called by the app is unchanged.
MODEL_CHOICES: List[str] = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4-turbo",
    "gpt-4",
    "gpt-3.5-turbo",
    "claude-sonnet-4-5-20250929",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gemini-2.0-flash",
]


class ModelEmitter:
    """Holds the demo model-name override and resolves the name to emit."""

    def __init__(self) -> None:
        self.enabled: bool = False
        self.model_name: str = MODEL_CHOICES[0]
        self.random: bool = False

    def configure(self, enabled: bool = False, model_name: str = "", random_emit: bool = False) -> None:
        self.enabled = bool(enabled)
        if model_name:
            self.model_name = model_name
        self.random = bool(random_emit)

    def is_active(self) -> bool:
        return self.enabled

    def pick(self) -> str:
        """The model name to report (random from the list, or the selected one)."""
        if self.random:
            return random.choice(MODEL_CHOICES)
        return self.model_name or MODEL_CHOICES[0]

    def emit(self, actual: str) -> str:
        """Return the override name when active, else the actual model name."""
        return self.pick() if self.enabled else actual

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model_name": self.model_name,
            "random": self.random,
            "choices": list(MODEL_CHOICES),
        }


# Module-level singleton.
model_emitter = ModelEmitter()
