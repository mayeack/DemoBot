"""LegalAdvice theme - general legal information (not attorney counsel)."""

from backend.agents.themes.base import (
    SpecialistSpec,
    ThemeConfig,
    is_conversational,
    prompt_for,
)

SPECIALISTS = (
    SpecialistSpec(
        key="issue_spotting",
        label="Issue Spotting",
        role="identifying the legal issues and area of law in play",
        focus=(
            "identifying the underlying legal issues and the area(s) of law involved, "
            "framing what is actually at stake (general information only)"
        ),
    ),
    SpecialistSpec(
        key="rights_obligations",
        label="Rights & Obligations",
        role="general rights, obligations, and common options",
        focus=(
            "the user's general rights and obligations and the common options people "
            "have in this kind of situation, without giving tailored legal advice"
        ),
    ),
    SpecialistSpec(
        key="procedure_deadlines",
        label="Procedure & Deadlines",
        role="processes, filings, and time-sensitive deadlines",
        focus=(
            "relevant processes/filings and any time-sensitive deadlines or statutes "
            "of limitations the user should be aware of"
        ),
    ),
    SpecialistSpec(
        key="referral_scope",
        label="Referral & Scope",
        role="when a licensed attorney is needed and red-line topics",
        focus=(
            "clear indicators the user needs a licensed attorney, and red-line topics "
            "(criminal exposure, court deadlines) that must not be self-handled"
        ),
    ),
)

THEME = ThemeConfig(
    key="legaladvice",
    label="LegalAdvice",
    conversational=is_conversational("legaladvice"),
    system_prompt=prompt_for("legaladvice"),
    specialists=SPECIALISTS,
)
