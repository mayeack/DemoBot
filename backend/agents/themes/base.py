"""Per-theme configuration primitives.

Each Application Theme becomes its own multi-agent pipeline (a decomposed
subgraph) selected by the supervisor/router. A :class:`ThemeConfig` carries
everything the graph needs to specialize the shared agents for a domain:

- ``system_prompt``  — the domain answer contract (output JSON format + rules),
  reused as the base for the synthesizer agent.
- ``specialists``    — the themed roster the coordinator picks from per query.

The coordinator and synthesizer system prompts are *derived* from the roster +
``system_prompt`` (see ``build_coordinator_prompt`` / ``build_synthesizer_prompt``)
so there is a single source of truth and no hand-duplicated prompt strings.

Prompt text and the conversational-theme set are sourced from
``RecommendationEngine`` so there is a single source of truth (no duplicated
prompt strings). The injection / formatting pattern dictionaries also remain on
the engine and are reused by the synthesizer / injection nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from backend.services.recommendation_engine import RecommendationEngine


@dataclass(frozen=True)
class SpecialistSpec:
    """One themed specialist agent available to a theme's coordinator.

    ``role`` is a one-line menu description shown to the coordinator when it
    picks specialists; ``focus`` is the scope used to build the specialist's own
    system prompt (see :func:`build_specialist_prompt`).
    """

    key: str
    label: str
    role: str
    focus: str


@dataclass(frozen=True)
class ThemeConfig:
    """Configuration for one Application Theme's multi-agent pipeline."""

    key: str
    label: str
    conversational: bool
    system_prompt: str
    # Tuple (not list) so the frozen dataclass stays hashable.
    specialists: Tuple[SpecialistSpec, ...] = field(default_factory=tuple)

    @property
    def agent_name(self) -> str:
        """Name used for the per-theme *synthesizer* agent's OTel AgentInvocation.

        Kept as ``{key}_domain_agent`` (the historical name of the single answer
        agent) so existing Splunk AI Agent Monitoring dashboards keyed on
        ``*_domain_agent`` keep resolving to the turn's final answer agent.
        """
        return f"{self.key}_domain_agent"

    @property
    def coordinator_agent_name(self) -> str:
        return f"{self.key}_coordinator"

    @property
    def subgraph_name(self) -> str:
        return f"{self.key}_subgraph"

    @property
    def primary_specialist(self) -> Optional[SpecialistSpec]:
        """The default specialist used to guarantee >=1 when the coordinator
        returns an empty/invalid plan. ``None`` only if a theme has no roster."""
        return self.specialists[0] if self.specialists else None

    def specialist(self, key: str) -> Optional[SpecialistSpec]:
        for spec in self.specialists:
            if spec.key == key:
                return spec
        return None

    def specialist_agent_name(self, key: str) -> str:
        return f"{self.key}_{key}_specialist"


def prompt_for(theme_key: str) -> str:
    """Return the system prompt for a theme (defaults to medadvice)."""
    prompts = RecommendationEngine.THEME_PROMPTS
    return prompts.get(theme_key, prompts["medadvice"])


def is_conversational(theme_key: str) -> bool:
    """Whether a theme is conversational (skips the medical clarifier)."""
    return theme_key in RecommendationEngine.CONVERSATIONAL_THEMES


# ---------------------------------------------------------------------------
# Derived agent prompts (single source of truth: the roster + system_prompt).
# ---------------------------------------------------------------------------


def build_coordinator_prompt(theme_config: ThemeConfig) -> str:
    """System prompt for the theme's coordinator agent.

    The coordinator does NOT answer the user; it returns a JSON plan selecting
    1-3 specialists from the theme roster. Themed by the theme label + roster.
    """
    menu = "\n".join(f"- {s.key}: {s.role}" for s in theme_config.specialists)
    return (
        f"You are the coordinating agent for {theme_config.label}, an AI assistant team. "
        "Do NOT answer the user's question. Your only job is to decide which specialist "
        "agents should analyze the user's latest message, based on what the query actually "
        "needs.\n\n"
        f"Available specialists:\n{menu}\n\n"
        "Respond with ONLY a JSON object in exactly this shape (no prose before or after):\n"
        '{\n  "specialists": ["<key>", ...],\n  "rationale": "<one short sentence>"\n}\n\n'
        "Rules: choose between 1 and 3 specialists, most relevant first; use only keys from "
        "the list above; pick only specialists whose expertise the query genuinely needs."
    )


def build_specialist_prompt(theme_config: ThemeConfig, spec: SpecialistSpec) -> str:
    """System prompt for one specialist agent (internal analysis, not user-facing)."""
    return (
        f"You are the {spec.label} specialist on the {theme_config.label} assistant team.\n"
        f"Your focus: {spec.focus}\n\n"
        "Analyze the user's latest message strictly through this lens. Return a brief internal "
        "analysis (3-6 concise bullet points) for the team's synthesizing agent. This is NOT "
        "shown to the user and must NOT be a full answer or address the user directly. Surface "
        "urgency, key facts, risks, and anything outside your scope. No greetings or disclaimers."
    )


def build_synthesizer_prompt(theme_config: ThemeConfig) -> str:
    """System prompt for the synthesizer agent — the theme's answer contract plus
    an instruction to fold in the specialist findings appended at call time."""
    return (
        theme_config.system_prompt
        + "\n\n--- SPECIALIST TEAM INPUT ---\n"
        "A team of specialist agents has already analyzed the user's request. Their internal "
        "findings are appended below under 'SPECIALIST FINDINGS'. Weigh and synthesize those "
        "findings into your single best response, following the response format defined above "
        "EXACTLY. Do not mention the specialists, the team, or that multiple agents were "
        "involved — respond as one assistant."
    )
