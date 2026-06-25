"""BenefitsAdvice theme - employee benefits / HR plan guidance."""

from backend.agents.themes.base import (
    SpecialistSpec,
    ThemeConfig,
    is_conversational,
    prompt_for,
)

SPECIALISTS = (
    SpecialistSpec(
        key="eligibility",
        label="Eligibility & Enrollment",
        role="plan eligibility, enrollment windows, and qualifying life events",
        focus=(
            "eligibility rules, open-enrollment vs special-enrollment windows, and "
            "qualifying life events that affect coverage choices"
        ),
    ),
    SpecialistSpec(
        key="claims",
        label="Claims & Coverage",
        role="what a plan typically covers and how claims generally work",
        focus=(
            "how a benefit plan typically covers a situation and how claims generally "
            "work, without guaranteeing specific coverage amounts or outcomes"
        ),
    ),
    SpecialistSpec(
        key="appeals",
        label="Appeals & Disputes",
        role="denied claims, appeal steps, and escalation paths",
        focus=(
            "denied-claim appeal steps, documentation, and escalation paths to HR or "
            "the plan administrator when an outcome is disputed"
        ),
    ),
    SpecialistSpec(
        key="coordination_of_benefits",
        label="COBRA / FMLA / Coordination",
        role="COBRA, FMLA, disability, retirement, and multi-plan coordination",
        focus=(
            "high-stakes areas (COBRA, FMLA, disability, retirement distributions) and "
            "coordination across multiple plans that need careful, cautious handling"
        ),
    ),
)

THEME = ThemeConfig(
    key="benefitsadvice",
    label="BenefitsAdvice",
    conversational=is_conversational("benefitsadvice"),
    system_prompt=prompt_for("benefitsadvice"),
    specialists=SPECIALISTS,
)
