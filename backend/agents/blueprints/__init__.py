"""Blueprint registry — the selectable agentic architectures.

Each blueprint contributes only a generation core; the guardrail chain is
shared (``guardrails.wire_guardrails``). The active default is chosen from the
chat header's Blueprint dropdown (persisted in settings_store) and can be
overridden per request (``ChatRequest.blueprint``) so tests and demo scripts can
drive both without flipping global state.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from backend.agents.blueprints import demobot_multi_agent
from backend.agents.blueprints.base import CORE_STATE_CONTRACT, Blueprint

DEFAULT_BLUEPRINT = demobot_multi_agent.KEY

_MODULES = [demobot_multi_agent]
try:  # the NVIDIA blueprint is optional at import time so a broken core never takes the default down
    from backend.agents.blueprints import nvidia_virtual_assistant  # noqa: E402

    _MODULES.append(nvidia_virtual_assistant)
except ImportError:  # pragma: no cover - only while the module does not exist yet
    pass

BLUEPRINTS: Dict[str, Blueprint] = {m.BLUEPRINT.key: m.BLUEPRINT for m in _MODULES}


def get_blueprint(key: Optional[str]) -> Blueprint:
    """Resolve a blueprint key, defaulting to the shipped architecture."""
    if not key:
        return BLUEPRINTS[DEFAULT_BLUEPRINT]
    return BLUEPRINTS.get(key, BLUEPRINTS[DEFAULT_BLUEPRINT])


def list_blueprints() -> List[Blueprint]:
    return list(BLUEPRINTS.values())


__all__ = ["BLUEPRINTS", "DEFAULT_BLUEPRINT", "CORE_STATE_CONTRACT", "Blueprint",
           "get_blueprint", "list_blueprints"]
