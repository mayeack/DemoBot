"""TaxAdvice theme - general tax information (not CPA advice)."""

from backend.agents.themes.base import (
    SpecialistSpec,
    ThemeConfig,
    is_conversational,
    prompt_for,
)

SPECIALISTS = (
    SpecialistSpec(
        key="deductions",
        label="Deductions & Credits",
        role="common deductions, credits, and recordkeeping awareness",
        focus=(
            "commonly available deductions and credits, eligibility basics, and the "
            "records a taxpayer should keep (general awareness, not filing instructions)"
        ),
    ),
    SpecialistSpec(
        key="filing_status",
        label="Filing Status & Deadlines",
        role="filing status, forms, and key deadlines",
        focus=(
            "filing status options, the common forms involved, and the key deadlines "
            "or extension considerations relevant to the question"
        ),
    ),
    SpecialistSpec(
        key="compliance_risk",
        label="Compliance Risk",
        role="audit-risk, penalties, liens, and when to see a professional",
        focus=(
            "audit/penalty exposure, liens or garnishments, and clear indicators that "
            "the user needs a CPA, enrolled agent, or tax attorney"
        ),
    ),
    SpecialistSpec(
        key="entity_structuring",
        label="Entity & Situation Complexity",
        role="business/multi-state/estate complexity that exceeds general guidance",
        focus=(
            "business, multi-state, international, or estate/gift complexity that "
            "exceeds general guidance and should be flagged for professional help"
        ),
    ),
)

THEME = ThemeConfig(
    key="taxadvice",
    label="TaxAdvice",
    conversational=is_conversational("taxadvice"),
    system_prompt=prompt_for("taxadvice"),
    specialists=SPECIALISTS,
)
