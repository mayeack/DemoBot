"""MedAdvice theme - general medical guidance (default)."""

from backend.agents.themes.base import (
    SpecialistSpec,
    ThemeConfig,
    is_conversational,
    prompt_for,
)

SPECIALISTS = (
    SpecialistSpec(
        key="triage",
        label="Triage",
        role="urgency assessment and emergency red-flag detection",
        focus=(
            "assessing how urgent the situation is and spotting life-threatening "
            "red-flag symptoms that warrant 911 or immediate in-person care"
        ),
    ),
    SpecialistSpec(
        key="symptom_analysis",
        label="Symptom Analysis",
        role="interpreting described symptoms and likely benign vs concerning patterns",
        focus=(
            "interpreting the described symptoms, their duration and severity, and "
            "common benign vs concerning explanations (without diagnosing)"
        ),
    ),
    SpecialistSpec(
        key="medication_safety",
        label="Medication Safety",
        role="OTC options, interactions, dosing cautions, and prescribing boundaries",
        focus=(
            "appropriate over-the-counter options, interaction/contraindication "
            "cautions, and the firm boundary against prescription/controlled dosing"
        ),
    ),
    SpecialistSpec(
        key="care_navigation",
        label="Care Navigation",
        role="self-care, when/where to seek professional care, and follow-up",
        focus=(
            "home/self-care guidance and clear indicators of when and where to seek "
            "professional medical care or follow-up"
        ),
    ),
)

THEME = ThemeConfig(
    key="medadvice",
    label="MedAdvice",
    conversational=is_conversational("medadvice"),
    system_prompt=prompt_for("medadvice"),
    specialists=SPECIALISTS,
)
