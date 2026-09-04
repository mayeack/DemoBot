"""Output-token cache accounting.

Every governed turn reports how many of its output tokens the provider served
from a cache and how many it decoded fresh. The two always sum to the turn's
``usage_output_tokens``, so the existing field keeps its meaning and the new
``usage_output_tokens_cached`` / ``usage_output_tokens_uncached`` pair only adds
detail (Splunk / Galileo price a cached token differently from a decoded one,
and a cache-hit ratio is the first thing a cost review asks for).

Where the numbers come from, in order:

1. **The provider**, when it reports the split (``usage_metadata``'s
   ``output_token_details``, or an OpenAI-compatible
   ``completion_tokens_details``). Real data always wins.
2. **A random tag**, for local inference (``ollama`` / a local NVIDIA NIM).
   Neither serves cached output nor reports a split, so the demo tags each
   call's output tokens itself — a per-call cached share, occasionally a clean
   miss. It is synthetic, like the rest of the local-inference demo surface, and
   deliberately not deterministic.
3. **All uncached**, for a cloud provider that reports no split — the honest
   reading of "the provider never said any of this was cached".

The split is computed once per model call in ``backend.agents.llm`` and rides on
``NormalizedLLMResponse``, so every consumer (the per-agent ``agent_trace``, the
running state sums, the governance event, the OTel spans) just adds numbers.
"""

from __future__ import annotations

import random
from typing import Any, Dict, Optional, Tuple

# Providers whose inference runs on this host: a local `ollama serve` daemon and
# a local NVIDIA NIM container (backend/config.py — provider=nvidia is never the
# hosted catalog). These are the ones that get the random tag.
LOCAL_PROVIDERS = frozenset({"ollama", "nvidia"})

# Key names seen for "output tokens served from cache" across provider payloads.
_CACHED_OUTPUT_KEYS = ("cache_read", "cached", "cached_tokens", "cache_read_tokens")

# Local-inference tagging: how often a call is a clean miss (nothing cached),
# and the cached share drawn for the rest. Ranges chosen so a demo's cache-hit
# ratio lands in a plausible band instead of pinning at 0% or 100%.
_LOCAL_CACHE_MISS_PROBABILITY = 0.3
_LOCAL_CACHED_SHARE = (0.15, 0.85)


def is_local_provider(provider: Optional[str]) -> bool:
    return (provider or "").lower() in LOCAL_PROVIDERS


def _first_int(source: Any, keys: Tuple[str, ...]) -> Optional[int]:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):  # bools are ints in Python; not a count
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def provider_cached_output_tokens(ai_message: Any) -> Optional[int]:
    """Cached output tokens as *reported by the provider*, or None if it said nothing.

    Reads the LangChain-normalized ``usage_metadata['output_token_details']``
    first, then the raw ``response_metadata`` shapes an OpenAI-compatible server
    uses (``completion_tokens_details``).
    """
    usage = getattr(ai_message, "usage_metadata", None) or {}
    reported = _first_int(usage.get("output_token_details"), _CACHED_OUTPUT_KEYS)
    if reported is not None:
        return reported

    meta = getattr(ai_message, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    for container in ("completion_tokens_details", "output_token_details"):
        reported = _first_int(token_usage.get(container), _CACHED_OUTPUT_KEYS)
        if reported is not None:
            return reported
    return None


def _random_cached_share() -> float:
    if random.random() < _LOCAL_CACHE_MISS_PROBABILITY:
        return 0.0
    return random.uniform(*_LOCAL_CACHED_SHARE)


def split_output_tokens(
    output_tokens: int, *, provider: Optional[str] = None, ai_message: Any = None
) -> Tuple[int, int]:
    """Return ``(cached, uncached)`` output tokens, always summing to ``output_tokens``."""
    total = int(output_tokens or 0)
    if total <= 0:
        return 0, 0

    cached = provider_cached_output_tokens(ai_message) if ai_message is not None else None
    if cached is None and is_local_provider(provider):
        cached = round(total * _random_cached_share())
    if cached is None:
        cached = 0

    cached = max(0, min(int(cached), total))
    return cached, total - cached


class TokenTally:
    """Running token sums for one turn, carrying the output cache split.

    Seeded from the state so a node that appends to an earlier node's spend
    (specialists, synthesizer, scheduling) keeps the running total; pass no state
    for a node that reports only its own call. ``updates()`` returns the four
    state keys to merge into the node's return value.
    """

    __slots__ = ("input_tokens", "output_tokens", "output_tokens_cached",
                 "output_tokens_uncached")

    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        state = state or {}
        self.input_tokens = state.get("llm_input_tokens", 0) or 0
        self.output_tokens = state.get("llm_output_tokens", 0) or 0
        self.output_tokens_cached = state.get("llm_output_tokens_cached", 0) or 0
        self.output_tokens_uncached = state.get("llm_output_tokens_uncached", 0) or 0

    def add(self, response: Any) -> "TokenTally":
        """Accumulate one ``NormalizedLLMResponse`` (None is ignored)."""
        if response is None:
            return self
        self.input_tokens += response.input_tokens or 0
        self.output_tokens += response.output_tokens or 0
        self.output_tokens_cached += getattr(response, "output_tokens_cached", 0) or 0
        self.output_tokens_uncached += getattr(response, "output_tokens_uncached", 0) or 0
        return self

    def updates(self) -> Dict[str, int]:
        return {
            "llm_input_tokens": self.input_tokens,
            "llm_output_tokens": self.output_tokens,
            "llm_output_tokens_cached": self.output_tokens_cached,
            "llm_output_tokens_uncached": self.output_tokens_uncached,
        }


def governance_usage_data(state: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """The ``usage_data`` block of a governance event, read off the turn's state.

    One definition of the block's shape, so the happy path and every
    short-circuiting block handler (AI Defense, Agent Control, NeMo output rails)
    report the same keys. Called with no state — a prompt blocked before any
    model call — it yields the all-zero block.
    """
    state = state or {}
    input_tokens = state.get("llm_input_tokens", 0) or 0
    output_tokens = state.get("llm_output_tokens", 0) or 0
    return {
        "usage_input_tokens": input_tokens,
        "usage_output_tokens": output_tokens,
        "usage_output_tokens_cached": state.get("llm_output_tokens_cached", 0) or 0,
        "usage_output_tokens_uncached": state.get("llm_output_tokens_uncached", 0) or 0,
        "usage_total_tokens": input_tokens + output_tokens,
    }
